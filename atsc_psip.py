"""Parse MPEG-TS packets and ATSC A/65 PSIP tables with no device dependency."""

from collections import defaultdict
from datetime import datetime, timedelta, timezone

TS_SIZE = 188
TS_SYNC = 0x47
MPEG_CRC_POLY = 0x04C11DB7

TABLE_NAMES = {
    0x00: "PAT", 0x02: "PMT", 0xC7: "MGT", 0xC8: "TVCT",
    0xCB: "EIT", 0xCC: "ETT", 0xCD: "STT",
}


class Bits:
    def __init__(self, buf, off=0):
        self.buf = buf
        self.off = off

    def take(self, count):
        value = 0
        for _ in range(count):
            value = (value << 1) | ((self.buf[self.off >> 3] >> (7 - (self.off & 7))) & 1)
            self.off += 1
        return value

    def skip(self, count):
        self.off += count


def mpeg_crc32(data):
    """Return the MPEG-2 CRC-32 remainder; valid PSI sections return zero."""
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            crc = ((crc << 1) ^ MPEG_CRC_POLY) & 0xFFFFFFFF if crc & 0x80000000 else (crc << 1) & 0xFFFFFFFF
    return crc


def ts_packets(data):
    """Yield aligned 188-byte MPEG-TS packets from an arbitrary byte stream."""
    offset = data.find(bytes([TS_SYNC]))
    while 0 <= offset + TS_SIZE <= len(data) and data[offset + TS_SIZE] != TS_SYNC:
        offset = data.find(bytes([TS_SYNC]), offset + 1)
    while 0 <= offset + TS_SIZE <= len(data):
        yield data[offset:offset + TS_SIZE]
        offset += TS_SIZE


def collect_sections(data):
    """Reassemble and CRC-validate PSI sections, returning {table_id: [(pid, section)]}."""
    tables = defaultdict(list)
    buffers = defaultdict(bytearray)

    def drain(pid):
        while len(buffers[pid]) >= 3:
            size = 3 + (((buffers[pid][1] & 0x0F) << 8) | buffers[pid][2])
            if not 7 <= size <= 4098:
                buffers[pid].clear()
                return
            if len(buffers[pid]) < size:
                return
            section = bytes(buffers[pid][:size])
            del buffers[pid][:size]
            if mpeg_crc32(section) == 0:
                tables[section[0]].append((pid, section))

    for packet in ts_packets(data):
        pid = ((packet[1] & 0x1F) << 8) | packet[2]
        adaptation_control = (packet[3] >> 4) & 0x03
        if adaptation_control not in (1, 3):
            continue
        start = 4 + (1 + packet[4] if adaptation_control == 3 else 0)
        if start >= TS_SIZE:
            continue
        payload = packet[start:]
        if packet[1] & 0x40:
            pointer = payload[0]
            if pointer >= len(payload):
                buffers[pid].clear()
                continue
            if buffers[pid] and pointer:
                buffers[pid].extend(payload[1:1 + pointer])
                drain(pid)
            buffers[pid].clear()
            payload = payload[1 + pointer:]
        buffers[pid].extend(payload)
        drain(pid)
    return dict(tables)


def parse_multiple_string(data):
    """Decode an ATSC A/65 multiple_string_structure, preferring English."""
    if not data:
        return ""
    pos = 0
    strings = []
    try:
        for _ in range(data[pos]):
            pos += 1
            language = data[pos:pos + 3].decode("ascii", "replace")
            pos += 3
            parts = []
            for _ in range(data[pos]):
                pos += 1
                compression, mode, size = data[pos:pos + 3]
                pos += 3
                raw = data[pos:pos + size]
                pos += size
                if compression == 0 and mode == 0:
                    parts.append(raw.decode("latin-1", "replace"))
            if parts:
                strings.append((language, "".join(parts)))
    except (IndexError, ValueError):
        return ""
    return next((text for language, text in strings if language == "eng"), strings[0][1] if strings else "")


def gps_to_dt(seconds):
    """Convert ATSC GPS seconds to UTC using the current 18-second offset."""
    return datetime(1980, 1, 6, tzinfo=timezone.utc) + timedelta(seconds=seconds - 18)


def parse_tvct(section):
    b = Bits(section, 3 * 8)
    b.take(16)
    b.skip(2 + 5 + 1 + 8 + 8)
    b.take(8)
    channels = []
    for _ in range(b.take(8)):
        start = b.off // 8
        name = section[start:start + 14].decode("utf-16-be", "replace").rstrip("\0")
        b.skip(14 * 8 + 4)
        major = b.take(10)
        minor = b.take(10)
        b.skip(8 + 32 + 16)
        program = b.take(16)
        b.skip(3 + 2 + 1 + 1 + 1 + 1 + 1 + 6)
        source_id = b.take(16)
        b.skip(6)
        b.skip(b.take(10) * 8)
        channels.append({"lcn": f"{major}.{minor}" if minor else str(major), "name": name,
                         "program": program, "source_id": source_id})
    return channels


def parse_eit(section):
    b = Bits(section, 3 * 8)
    source_id = b.take(16)
    b.skip(2 + 5 + 1 + 8 + 8 + 8)
    events = []
    for _ in range(b.take(8)):
        b.skip(2)
        event_id = b.take(14)
        start = gps_to_dt(b.take(32))
        etm_location = b.take(2)
        b.skip(2)
        duration_seconds = b.take(20)
        title_size = b.take(8)
        title_start = b.off // 8
        title = parse_multiple_string(section[title_start:title_start + title_size])
        b.skip(title_size * 8 + 4)
        b.skip(b.take(12) * 8)
        events.append({"source_id": source_id, "event_id": event_id, "start": start,
                       "duration_seconds": duration_seconds, "title": title, "etm_location": etm_location})
    return events


def parse_ett(section):
    b = Bits(section, 3 * 8)
    b.take(16)
    b.skip(2 + 5 + 1 + 8 + 8 + 8)
    etm_id = b.take(32)
    return {"source_id": etm_id >> 16, "event_id": (etm_id >> 2) & 0x3FFF,
            "etm_location": etm_id & 0x03,
            "description": parse_multiple_string(section[b.off // 8:-4]).strip()}


def parse(data):
    """Return deduplicated ATSC channels, schedule events, ETT text, and table inventory."""
    tables = collect_sections(data)
    result = {
        "tables": {f"0x{table_id:02x}": {"name": TABLE_NAMES.get(table_id, "?"), "sections": len(sections),
                                           "pids": sorted({pid for pid, _ in sections})}
                   for table_id, sections in tables.items()},
        "channels": [], "events": [], "ett": [],
    }
    for _, section in tables.get(0xC8, []):
        result["channels"].extend(parse_tvct(section))
    for _, section in tables.get(0xCB, []):
        result["events"].extend(parse_eit(section))
    for _, section in tables.get(0xCC, []):
        result["ett"].append(parse_ett(section))
    result["channels"] = list({channel["source_id"]: channel for channel in result["channels"]}.values())
    result["events"] = list({(event["source_id"], event["event_id"], event["start"]): event
                             for event in result["events"]}.values())
    descriptions = {(entry["source_id"], entry["event_id"]): entry["description"]
                    for entry in result["ett"] if entry["description"]}
    for event in result["events"]:
        event["description"] = descriptions.get((event["source_id"], event["event_id"]), "")
    result["ett"] = list({(entry["source_id"], entry["event_id"]): entry for entry in result["ett"]
                          if entry["description"]}.values())
    return result
