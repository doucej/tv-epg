#!/usr/bin/env python3
"""Fetch free HDHomeRun EPG and write an XMLTV guide file for Jellyfin (no DVR subscription)."""

import argparse
import json
import os
import ssl
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

CLOUD_EPG_URL = "https://api.hdhomerun.com/api/xmltv?DeviceAuth={auth}"
USER_AGENT = "hdhr-epg/1.0"


def log(msg):
    print("[hdhr-epg] " + msg, file=sys.stderr)


def http_get(url, timeout, insecure=False):
    context = ssl._create_unverified_context() if insecure else None
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
        return response.read()


def normalize_base(host):
    if host.startswith(("http://", "https://")):
        return host.rstrip("/")
    return "http://" + host


def discover_device(host, timeout, insecure):
    base = normalize_base(host)
    payload = json.loads(http_get(base + "/discover.json", timeout, insecure))
    auth = payload.get("DeviceAuth")
    if not auth:
        raise RuntimeError(base + "/discover.json did not return a DeviceAuth token")
    log("device: %s (%s)" % (payload.get("FriendlyName", host), payload.get("ModelNumber", "?")))
    return base, auth


def fetch_lineup(base, timeout, insecure):
    channels = json.loads(http_get(base + "/lineup.json", timeout, insecure))
    lineup = [(c["GuideNumber"], c.get("GuideName", "")) for c in channels if c.get("GuideNumber")]
    if not lineup:
        raise RuntimeError("lineup.json returned no channels (antenna not connected or no channel scan?)")
    return lineup


def fetch_cloud_xmltv(auth, timeout, insecure):
    url = CLOUD_EPG_URL.format(auth=urllib.parse.quote(auth, safe=""))
    return http_get(url, timeout, insecure).decode("utf-8", errors="replace")


def lcn_of(channel):
    lcn = channel.find("lcn")
    return (lcn.text or "").strip() if lcn is not None else ""


def display_names(channel):
    return [(el.text or "").strip() for el in channel.findall("display-name")]


def resolve_lineup_id(channel, lineup):
    lineup_numbers = set(num for num, _ in lineup)
    by_name = {name.lower(): num for num, name in lineup if name}
    lcn = lcn_of(channel)
    if lcn in lineup_numbers:
        return lcn
    for name in display_names(channel):
        if name.lower() in by_name:
            return by_name[name.lower()]
    for name in display_names(channel):
        for num in lineup_numbers:
            if num in name.split():
                return num
    return None


def remap(root, lineup, keep_unknown):
    old_to_new = {}
    used_targets = set()
    doomed = []
    for channel in root.findall("channel"):
        target = resolve_lineup_id(channel, lineup)
        if target is None or target in used_targets:
            if not keep_unknown:
                doomed.append(channel)
            continue
        old_to_new[channel.get("id")] = target
        used_targets.add(target)
        channel.set("id", target)

    for channel in doomed:
        root.remove(channel)
        old_to_new.pop(channel.get("id"), None)

    kept_progs = 0
    doomed_progs = []
    for prog in root.findall("programme"):
        new_id = old_to_new.get(prog.get("channel"))
        if new_id is None:
            doomed_progs.append(prog)
        else:
            prog.set("channel", new_id)
            kept_progs += 1
    for prog in doomed_progs:
        root.remove(prog)

    missing = sorted(set(num for num, _ in lineup) - used_targets)
    return len(old_to_new), len(doomed), kept_progs, len(doomed_progs), missing


def indent_xml(element, level=0):
    """Pretty-print XML without requiring ElementTree.indent (Python 3.9+)."""
    prefix = "\n" + "  " * level
    if len(element):
        if not element.text or not element.text.strip():
            element.text = prefix + "  "
        for child in element:
            indent_xml(child, level + 1)
        if not element[-1].tail or not element[-1].tail.strip():
            element[-1].tail = prefix
    if level and (not element.tail or not element.tail.strip()):
        element.tail = prefix


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Generate an XMLTV EPG file from a free HDHomeRun device (no DVR subscription)."
    )
    parser.add_argument("--host", default=os.environ.get("HDHR_HOST", "hdhomerun.local"),
                        help="HDHomeRun host or IP (default: $HDHR_HOST or hdhomerun.local)")
    parser.add_argument("--output", "-o", default=os.environ.get("HDHR_EPG_OUTPUT", "epg.xml"),
                        help="output XMLTV file path (default: $HDHR_EPG_OUTPUT or epg.xml)")
    parser.add_argument("--keep-unknown", action="store_true",
                        help="keep cloud channels that are not in the local lineup")
    parser.add_argument("--timeout", type=int, default=30, help="HTTP timeout in seconds (default: 30)")
    parser.add_argument("--insecure", action="store_true", help="skip TLS certificate verification")
    args = parser.parse_args(argv)

    try:
        base, auth = discover_device(args.host, args.timeout, args.insecure)
        lineup = fetch_lineup(base, args.timeout, args.insecure)
        log("lineup: %d channels" % len(lineup))
        xml_str = fetch_cloud_xmltv(auth, args.timeout, args.insecure)
    except Exception as exc:
        log("error: %s" % exc)
        return 1

    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError as exc:
        log("error: cloud API returned unparseable XML: %s" % exc)
        return 1

    cloud_channels = len(root.findall("channel"))
    matched, dropped_ch, kept_progs, dropped_progs, missing = remap(root, lineup, args.keep_unknown)
    log("cloud channels: %d, matched to lineup: %d, dropped: %d" % (cloud_channels, matched, dropped_ch))
    log("programmes: %d kept, %d dropped" % (kept_progs, dropped_progs))
    if missing:
        log("lineup channels missing from cloud EPG: %s" % ", ".join(missing))

    times = [p.get("start") for p in root.findall("programme") if p.get("start")]
    if times:
        log("coverage: %s .. %s (UTC)" % (min(times), max(times)))

    if not root.findall("programme"):
        log("error: no programme entries left after filtering; refusing to write empty guide")
        return 1

    tree = ET.ElementTree(root)
    indent_xml(root)
    out_dir = os.path.dirname(os.path.abspath(args.output))
    os.makedirs(out_dir, exist_ok=True)
    tmp_path = args.output + ".tmp"
    tree.write(tmp_path, encoding="utf-8", xml_declaration=True)
    os.replace(tmp_path, args.output)
    log("wrote %s (%d bytes)" % (args.output, os.path.getsize(args.output)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
