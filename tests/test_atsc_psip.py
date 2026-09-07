import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import atsc_psip
import hdhr_ts_epg


CAPTURE = Path("/tmp/mux_ch5.ts")


@unittest.skipUnless(CAPTURE.exists(), "requires the local RF 5 fixture capture")
class AtscPsipCaptureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.parsed = atsc_psip.parse(CAPTURE.read_bytes())

    def test_recovers_expected_psip_tables(self):
        self.assertEqual({"0x00", "0x02", "0xc7", "0xc8", "0xcb", "0xcc", "0xcd"}, set(self.parsed["tables"]))

    def test_recovers_virtual_channels_and_events(self):
        self.assertEqual({"2.1", "24.1", "44.1", "66.5"}, {channel["lcn"] for channel in self.parsed["channels"]})
        self.assertGreaterEqual(len(self.parsed["events"]), 60)
        self.assertIn("PBS News Hour", {event["title"] for event in self.parsed["events"]})

    def test_joins_extended_text(self):
        event = next(event for event in self.parsed["events"] if event["title"] == "This Old House")
        self.assertIn("Cape Ann", event["description"])

    def test_xmltv_uses_virtual_channel_ids(self):
        merged = {"channels": {}, "events": {}}
        hdhr_ts_epg.merge_mux(merged, self.parsed, 5)
        normalized = hdhr_ts_epg.merged_for_xmltv(merged)
        output = Path("/tmp/atsc-psip-test.xml")
        hdhr_ts_epg.write_xmltv(normalized, output)
        import xml.etree.ElementTree as ET
        root = ET.parse(output).getroot()
        ids = {channel.get("id") for channel in root.findall("channel")}
        self.assertEqual(ids, {"2.1", "24.1", "44.1", "66.5"})
        self.assertFalse([programme for programme in root.findall("programme") if programme.get("channel") not in ids])

    def test_sweep_merge_keeps_virtual_channels_from_multiple_muxes(self):
        merged = {"channels": {}, "events": {}}
        hdhr_ts_epg.merge_mux(merged, self.parsed, 5)
        other = {"channels": [
            {"lcn": "26.1", "name": "WCEA", "program": 1, "source_id": 16},
            {"lcn": "46.1", "name": "WWDP-DT", "program": 3, "source_id": 17},
            {"lcn": "49.1", "name": "WVCC", "program": 3, "source_id": 18},
        ], "events": []}
        hdhr_ts_epg.merge_mux(merged, other, 36)
        output = hdhr_ts_epg.merged_for_xmltv(merged)
        self.assertEqual({"2.1", "24.1", "44.1", "66.5", "26.1", "46.1", "49.1"},
                         {channel["lcn"] for channel in output["channels"]})

    def test_partial_tvct_does_not_discard_cached_channels(self):
        partial = {"channels": [{"lcn": "2.1", "name": "WGBH-HD", "program": 3, "source_id": 1}], "events": []}
        cached = [
            {"lcn": "2.1", "name": "WGBH-HD", "program": 3, "source_id": 1},
            {"lcn": "2.2", "name": "World", "program": 4, "source_id": 2},
            {"lcn": "2.3", "name": "WGBH-SD", "program": 5, "source_id": 3},
        ]
        merged = hdhr_ts_epg.merge_cached_channels(partial, cached)
        self.assertEqual({"2.1", "2.2", "2.3"}, {channel["lcn"] for channel in merged["channels"]})


if __name__ == "__main__":
    unittest.main()
