#!/usr/bin/env python3
"""hdhr_ts_epg.py - capture raw ATSC transport stream from an HDHomeRun and extract the EPG it carries.

ATSC 1.0 multiplexes carry guide data in PSIP tables. This tool captures a full physical-RF
mux through the HDHomeRun HTTP output and parses its TVCT (0xC8), EIT (0xCB), and ETT (0xCC)
tables with the Python standard library only.

Usage:
  python3 hdhr_ts_epg.py --host hdhomerun.local --list
  python3 hdhr_ts_epg.py --host hdhomerun.local --rf-channel 5 --seconds 20 --xmltv atsc1.xml
  python3 hdhr_ts_epg.py --host hdhomerun.local --sweep --rf-range 2-36 --seconds 15 --xmltv atsc1.xml
  python3 hdhr_ts_epg.py --parse capture.ts --channel 4.1
  python3 hdhr_ts_epg.py --parse capture.ts --xmltv atsc1.xml
"""

import argparse
from contextlib import nullcontext
import http.client
import json
import os
import socket
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import atsc_psip


def parse_ts(data):
    return atsc_psip.parse(data)


def fetch_lineup(host):
    with urllib.request.urlopen(f"http://{host}/lineup.json", timeout=15) as r:
        return json.load(r)


def capture(host, path, seconds, out_path):
    url = path
    conn = http.client.HTTPConnection(host, 5004, timeout=max(seconds + 30, 40))
    conn.request("GET", url)
    resp = conn.getresponse()
    if resp.status != 200:
        conn.close()
        raise SystemExit(f"TS capture failed: HTTP {resp.status} for {url}")
    t_start = None
    total = 0
    captured = bytearray()
    try:
        with (open(out_path, "wb") if out_path else nullcontext(None)) as f:
            while True:
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                if f:
                    f.write(chunk)
                else:
                    captured.extend(chunk)
                total += len(chunk)
                if t_start is None:
                    t_start = time.monotonic()
                elif time.monotonic() - t_start >= seconds:
                    break
    except (socket.timeout, ConnectionError):
        pass
    conn.close()
    return bytes(captured) if out_path is None else total


def fmt(dt):
    return dt.strftime("%Y-%m-%d %H:%M UTC") if dt else "?"


def build_preview(parsed, channel):
    svc_by_lcn = {s["lcn"]: s for s in parsed["channels"] if s["lcn"]}
    svc = svc_by_lcn.get(channel)
    if svc is None:
        return None
    events = sorted((e for e in parsed["events"] if e["source_id"] == svc["source_id"]), key=lambda e: e["start"])
    return {"service": svc, "events": events}


def write_xmltv(parsed, output):
    root = ET.Element("tv", {"generator-info-name": "hdhr_ts_epg.py"})
    channels = {channel["source_id"]: channel for channel in parsed["channels"]}
    for channel in sorted(channels.values(), key=lambda c: tuple(map(int, c["lcn"].split(".")))):
        node = ET.SubElement(root, "channel", {"id": channel["lcn"]})
        ET.SubElement(node, "display-name").text = channel["lcn"]
        ET.SubElement(node, "display-name").text = channel["name"]
    count = 0
    for event in sorted(parsed["events"], key=lambda e: (e["start"], e["source_id"], e["event_id"])):
        channel = channels.get(event["source_id"])
        if channel is None or not event["title"]:
            continue
        node = ET.SubElement(root, "programme", {
            "start": event["start"].strftime("%Y%m%d%H%M%S +0000"),
            "stop": (event["start"] + timedelta(seconds=event["duration_seconds"])).strftime("%Y%m%d%H%M%S +0000"),
            "channel": channel["lcn"],
        })
        ET.SubElement(node, "title", {"lang": "en"}).text = event["title"]
        if event.get("description"):
            ET.SubElement(node, "desc", {"lang": "en"}).text = event["description"]
        count += 1
    indent_xml(root)
    ET.ElementTree(root).write(output, encoding="utf-8", xml_declaration=True)
    return count


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


def parse_rf_range(value):
    try:
        start, end = (int(part) for part in value.split("-", 1))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("RF range must be START-END, e.g. 2-36") from exc
    if start < 2 or end > 69 or start > end:
        raise argparse.ArgumentTypeError("RF range must be within 2-69")
    return range(start, end + 1)


def load_scan_state(path, host):
    try:
        with open(path, encoding="utf-8") as state_file:
            state = json.load(state_file)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[ts-epg] ignoring unreadable scan state {path}: {exc}", file=sys.stderr)
        return None
    if state.get("version") != 1 or state.get("host") != host:
        return None
    return state


def write_scan_state(path, host, rf_range, muxes):
    state = {
        "version": 1,
        "host": host,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "rf_range": [rf_range.start, rf_range.stop - 1],
        "muxes": {str(rf): {"channels": channels} for rf, channels in sorted(muxes.items())},
    }
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as state_file:
        json.dump(state, state_file, indent=2, sort_keys=True)
        state_file.write("\n")
    os.replace(temporary, path)


def state_rf_channels(state):
    return sorted(int(rf) for rf in state.get("muxes", {}))


def merge_mux(merged, parsed, rf_channel):
    """Merge one RF mux; source IDs are local to the mux, LCNs are not."""
    sources = {channel["source_id"]: channel for channel in parsed["channels"]}
    for channel in sources.values():
        merged["channels"][channel["lcn"]] = channel
    for event in parsed["events"]:
        channel = sources.get(event["source_id"])
        if channel is None or not event["title"]:
            continue
        key = (channel["lcn"], event["start"])
        merged["events"][key] = {**event, "lcn": channel["lcn"], "rf_channel": rf_channel}


def merged_for_xmltv(merged):
    channels = list(merged["channels"].values())
    events = []
    for event in merged["events"].values():
        channel = merged["channels"][event["lcn"]]
        events.append({**event, "source_id": channel["source_id"]})
    # write_xmltv uses source_id as its local key. Assign stable merged IDs.
    normalized_channels = []
    for source_id, channel in enumerate(channels, start=1):
        normalized_channels.append({**channel, "source_id": source_id})
        for index, event in enumerate(events):
            if event["lcn"] == channel["lcn"]:
                events[index] = {**event, "source_id": source_id}
    return {"channels": normalized_channels, "events": events}


def compare_with_cloud(merged, cloud_path):
    root = ET.parse(cloud_path).getroot()
    cloud_channels = {node.get("id") for node in root.findall("channel")}
    cloud_events = {(node.get("channel"), node.get("start")) for node in root.findall("programme")}
    psip_channels = set(merged["channels"])
    psip_events = {(event["lcn"], event["start"].strftime("%Y%m%d%H%M%S +0000")) for event in merged["events"].values()}
    return {
        "cloud_channels": len(cloud_channels),
        "psip_channels": len(psip_channels),
        "channels_in_cloud": len(psip_channels & cloud_channels),
        "cloud_programmes": len(cloud_events),
        "psip_programmes": len(psip_events),
        "programmes_matching_cloud_start": len(psip_events & cloud_events),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="hdhomerun.local")
    ap.add_argument("--channel", default=None, help="GuideNumber/VC, e.g. 4.1")
    ap.add_argument("--rf-channel", type=int, help="capture full ATSC 1.0 mux from physical RF channel")
    ap.add_argument("--sweep", action="store_true", help="capture every RF channel in --rf-range and merge PSIP")
    ap.add_argument("--full-scan", action="store_true", help="with --sweep, retune the complete RF range and refresh scan state")
    ap.add_argument("--rf-range", type=parse_rf_range, default=parse_rf_range("2-36"), help="physical RF range for --sweep (default: 2-36)")
    ap.add_argument("--scan-state", default="atsc1-scan-state.json", help="persisted RF discovery state (default: atsc1-scan-state.json)")
    ap.add_argument("--seconds", type=float, default=5)
    ap.add_argument("--out", default="capture.ts")
    ap.add_argument("--parse", metavar="FILE", help="parse an already-captured TS file")
    ap.add_argument("--xmltv", metavar="FILE", help="write recovered TVCT/EIT data as XMLTV")
    ap.add_argument("--compare-cloud", metavar="FILE", help="compare merged PSIP coverage with cloud XMLTV")
    ap.add_argument("--list", action="store_true", help="list lineup with signal quality")
    ap.add_argument("--json", action="store_true", help="emit full JSON result")
    args = ap.parse_args()

    if args.list:
        for c in fetch_lineup(args.host):
            print(f"{c['GuideNumber']:>6}  S={c.get('SignalStrength'):<3} Q={c.get('SignalQuality'):<3}  {c['GuideName']}")
        return 0

    if args.sweep:
        if args.parse or args.rf_channel is not None or args.channel or args.json:
            ap.error("--sweep cannot be combined with --parse, --rf-channel, --channel, or --json")
        state = None if args.full_scan else load_scan_state(args.scan_state, args.host)
        if state:
            rf_channels = state_rf_channels(state)
            print(f"[ts-epg] optimized sweep: {len(rf_channels)} RF channels from {args.scan_state}")
        else:
            rf_channels = args.rf_range
            print(f"[ts-epg] full scan: RF {args.rf_range.start}-{args.rf_range.stop - 1}")
        merged = {"channels": {}, "events": {}}
        locked = []
        muxes = {int(rf): details["channels"] for rf, details in state.get("muxes", {}).items()} if state else {}
        for rf_channel in rf_channels:
            path = f"/auto/ch{rf_channel}"
            print(f"[ts-epg] RF {rf_channel}: capturing {args.seconds}s ...")
            try:
                n = capture(args.host, path, args.seconds, None)
            except (OSError, SystemExit) as exc:
                print(f"[ts-epg] RF {rf_channel}: unavailable ({exc})")
                continue
            parsed = parse_ts(n)
            cached_mux = state.get("muxes", {}).get(str(rf_channel), {}) if state else {}
            if not parsed["channels"] and cached_mux.get("channels"):
                parsed["channels"] = cached_mux["channels"]
                print(f"[ts-epg] RF {rf_channel}: using cached TVCT channel map")
            if not parsed["channels"]:
                print(f"[ts-epg] RF {rf_channel}: no valid TVCT")
                continue
            merge_mux(merged, parsed, rf_channel)
            locked.append(rf_channel)
            muxes[rf_channel] = parsed["channels"]
            print(f"[ts-epg] RF {rf_channel}: {len(parsed['channels'])} channels, {len(parsed['events'])} events")
        merged_parsed = merged_for_xmltv(merged)
        if args.xmltv:
            count = write_xmltv(merged_parsed, args.xmltv)
            print(f"[ts-epg] wrote {args.xmltv} ({len(merged_parsed['channels'])} channels, {count} programmes)")
        print(f"[ts-epg] locked RF channels: {', '.join(map(str, locked)) or 'none'}")
        write_scan_state(args.scan_state, args.host, args.rf_range, muxes)
        print(f"[ts-epg] wrote scan state {args.scan_state}")
        if args.compare_cloud:
            comparison = compare_with_cloud(merged, args.compare_cloud)
            print("[ts-epg] cloud comparison: " + ", ".join(f"{key}={value}" for key, value in comparison.items()))
        return 0

    if args.parse:
        with open(args.parse, "rb") as f:
            data = f.read()
    else:
        if not args.channel and args.rf_channel is None:
            ap.error("--channel or --rf-channel required (or --list to pick one)")
        path = f"/auto/ch{args.rf_channel}" if args.rf_channel is not None else f"/auto/v{args.channel}"
        print(f"[ts-epg] capturing {args.seconds}s of {path} from {args.host}:5004 ...")
        n = capture(args.host, path, args.seconds, args.out)
        data = open(args.out, "rb").read()
        print(f"[ts-epg] captured {n} bytes -> {args.out}")
    if not data:
        print("[ts-epg] no data captured", file=sys.stderr)
        return 1

    parsed = parse_ts(data)
    if args.xmltv:
        count = write_xmltv(parsed, args.xmltv)
        print(f"[ts-epg] wrote {args.xmltv} ({count} programmes)")
    if args.json:
        print(json.dumps(parsed, indent=2, default=str))
        return 0

    print("\n== table inventory ==")
    for k in sorted(parsed["tables"]):
        t = parsed["tables"][k]
        print(f"  {k} {t['name']:<13} sections={t['sections']:<3} pids={t['pids']}")

    print("\n== channels (TVCT) ==")
    for s in sorted(parsed["channels"], key=lambda s: tuple(map(int, s["lcn"].split(".")))):
        print(f"  source={s['source_id']:<5} program={s['program']:<5} lcn={s['lcn']:<6} {s['name']}")

    if args.channel:
        pv = build_preview(parsed, args.channel)
        if pv:
            s = pv["service"]
            print(f"\n== guide for {args.channel} ({s['name']}, source={s['source_id']}) ==")
            now = datetime.now(timezone.utc)
            shown = 0
            for e in pv["events"]:
                print(f"  EVENT {fmt(e['start'])} +{e['duration_seconds'] // 60}min  {e['title']}")
                shown += 1
                if shown >= 8:
                    break
            if not pv["events"]:
                print("  (no PSIP EIT data captured in this window - try a longer capture)")
        else:
            print(f"\n[ts-epg] channel {args.channel} not found among SDT services (try --json)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
