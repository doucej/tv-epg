# tv-epg — free HDHomeRun → XMLTV EPG for Jellyfin

Generates an XMLTV guide file from an HDHomeRun device using **only free
endpoints** — no DVR subscription, no Silicon Dust account, no API keys.

The repository has two layers:

- `atsc_psip.py` is a reusable, device-independent MPEG-TS and ATSC A/65
  parser. It accepts bytes from any ATSC 1.0 transport stream and returns
  CRC-validated TVCT channels, EIT events, ETT descriptions, and table data.
- `hdhr_ts_epg.py` is the HDHomeRun integration: HTTP capture, physical-RF
  discovery cache and sweep, XMLTV output, and cloud comparison.

## How it works

| Step | Endpoint | Cost |
|------|----------|------|
| 1. Discover device | `http://<host>/discover.json` → `DeviceAuth` token | free |
| 2. Local lineup | `http://<host>/lineup.json` → `GuideNumber` per channel | free |
| 3. EPG data | `https://api.hdhomerun.com/api/xmltv?DeviceAuth=<token>` → ~2 days of EPG, already XMLTV | free |

The tool rewrites every XMLTV channel `id` to the device lineup's
`GuideNumber` (e.g. `10.1`), so Jellyfin's HDHomeRun tuner **auto-maps
channels with no manual channel mapping**.

Timestamps keep the source's declared `+0000` (UTC) offset — verified
correct against real-world air times — and Jellyfin honors XMLTV offsets.

## Usage

```bash
python3 hdhr_epg.py --host hdhomerun.local --output epg.xml
```

Options:

| Flag | Default | Description |
|------|---------|-------------|
| `--host` | `hdhomerun.local` (or `$HDHR_HOST`) | HDHomeRun hostname/IP |
| `--output`, `-o` | `epg.xml` (or `$HDHR_EPG_OUTPUT`) | output XMLTV file |
| `--keep-unknown` | off | keep cloud channels not in the local lineup |
| `--timeout` | `30` | HTTP timeout (seconds) |
| `--insecure` | off | skip TLS verification (cloud API) |

Zero dependencies — Python 3.9+ standard library only.

Sample run against the Flex 4K DVR:

```
[hdhr-epg] device: HDHomeRun FLEX 4K (HDFX-4K)
[hdhr-epg] lineup: 69 channels
[hdhr-epg] cloud channels: 69, matched to lineup: 62, dropped: 0
[hdhr-epg] programmes: 3882 kept, 0 dropped
[hdhr-epg] coverage: 20260831070000 +0000 .. 20260902090000 +0000 (UTC)
[hdhr-epg] wrote epg.xml (3222293 bytes)
```

## Refreshing

The cloud feed is a rolling ~2-day window, so refresh at least daily
(every 6h is comfortable). Pick one:

### systemd (recommended on a bare-metal server)

```bash
sudo cp systemd/hdhr-epg.service systemd/hdhr-epg.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hdhr-epg.timer
systemctl list-timers hdhr-epg.timer
```

Replace `YOUR_USER` and `/path/to/tv-epg` in `hdhr-epg.service` before
installing it.

### cron

```cron
*/6 * * * * /usr/bin/python3 /path/to/tv-epg/hdhr_epg.py --host hdhomerun.local --output /path/to/tv-epg/epg.xml >> /path/to/tv-epg/epg.log 2>&1
```

### Docker

```bash
docker compose up -d --build
# writes ./epg/epg.xml, re-runs every 6h
```

## Jellyfin setup

1. Put `epg.xml` somewhere the Jellyfin server can read it
   (same filesystem, or a shared volume if Jellyfin is containerized —
   e.g. mount this directory into the container at `/epg`).
2. Dashboard → **TV** → *Add Guide* → **XMLTV** → point it at the file
   (e.g. `/epg/epg.xml` or the absolute host path).
3. Dashboard → **TV** → *Add Tuner* → **HDHomeRun** → host
   `hdhomerun.local`, no login.
4. Channel mapping should already be populated 1:1 (channel ids equal the
   lineup GuideNumbers). Confirm a few and *Save*.
5. Let it import, then check a channel's guide in the TV section.

## Notes & troubleshooting

- **Which channels get EPG?** Whatever Silicon Dust's cloud feed has for
  your device's region. ATSC 3.0 (`1xx`) re-broadcasts frequently have no
  guide data and simply have no programmes — they still play live.
- **Times look 4h off in some players?** This file uses explicit UTC
  offsets (`+0000`), which is spec-correct; Jellyfin handles it.
- **`lineup.json` empty?** Run a channel scan on the device first
  (Settings → Channel Scan) or check the antenna connection.
- **Cloud API 401/403?** The `DeviceAuth` token is per-device; make sure
  `--host` points at the device you expect.
- **Jellyfin import empty?** Ensure the guide file path is readable by the
  `jellyfin` user and the file is non-empty.

## ATSC 1.0 in-band PSIP

`hdhr_ts_epg.py` extracts the broadcast-provided guide for an **ATSC 1.0**
physical RF mux. Use the physical RF channel, not a virtual channel number:

```bash
python3 hdhr_ts_epg.py --host hdhomerun.local --rf-channel 5 --seconds 20 --xmltv atsc1.xml
```

To build a merged guide for the local ATSC 1.0 market, sweep the U.S.
post-repack broadcast range. Each frequency is tuned once and all of its
virtual channels are merged by their virtual-channel number:

```bash
python3 hdhr_ts_epg.py --host hdhomerun.local \
  --sweep --rf-range 2-36 --seconds 12 \
  --xmltv atsc1.xml --compare-cloud epg.xml
```

The RF dwell must be long enough to receive the repeating PSIP carousel.
12-20 seconds per locked mux is a practical starting point. The sweep keeps
only the current mux in memory and does not save video/audio captures.

The first sweep is a full discovery scan and writes the gitignored
`atsc1-scan-state.json` cache with locked RF multiplexes and TVCT channel
maps. Later `--sweep` runs tune only those known multiplexes. Use
`--full-scan` after moving, rescanning the HDHomeRun, or changing antenna
equipment to refresh discovery:

```bash
# Full discovery scan; refreshes atsc1-scan-state.json.
python3 hdhr_ts_epg.py --host hdhomerun.local --sweep --full-scan --seconds 12 --xmltv atsc1.xml

# Normal recurring refresh; tunes only RFs stored in the scan state.
python3 hdhr_ts_epg.py --host hdhomerun.local --sweep --seconds 12 --xmltv atsc1.xml
```

The HDHomeRun's ATSC 1.0 mux uses these valid MPEG-2 CRC-32 PSIP tables:

- `0xC8` TVCT on PID `0x1FFB`: virtual channel number, name, and source ID.
- `0xCB` EIT on PIDs `0x1D00–0x1D07`: programme start, duration, and title.
- `0xCC` ETT on PIDs `0x1E00–0x1E80`: extended programme text, joined to EIT
  events by source ID and event ID and written as XMLTV `<desc>` elements.

The original investigation incorrectly treated bit 7 as the TS PUSI bit,
reversed the adaptation-field-control values, and used the wrong CRC32
variant. The correct PUSI bit is `0x40`, `01` means payload-only, and PSI
uses non-reflected MPEG-2 CRC-32. A validated 20-second capture of RF 5
yielded TVCT, EIT, and ETT sections; the parser recovered four channels and
62 schedule events, including titles such as *This Old House* and *PBS News
Hour*.

ETT descriptions are broadcaster-provided and are less complete than the
cloud guide's enriched metadata. A later 12-second optimized sweep recovered
753 programmes, of which 383 had broadcast descriptions. Against a fresh
cloud guide, 353 of those descriptions aligned to the same channel/start
event; 44 were byte-for-byte identical, with the rest typically differing by
editorial wording or broadcast-length truncation. PSIP does not consistently
provide the cloud guide's icons, series IDs, episode numbering, categories,
or new/repeat flags.

On this Boston lineup, a 12-second sweep of RF 2-36 locked 12 multiplexes
and generated `atsc1.xml` with **59 channels and 690 programmes**. It shared
58 channels with the 69-channel cloud guide and matched 615 cloud programme
start times. The missing cloud channels were `6.1`, `6.2`, `6.3`, `6.5`, and
the NextGen services `102.1`, `104.1`, `105.1`, `115.1`, `125.1`, `150.1`,
and `166.1`. PSIP supplied approximately two days of schedule in that sweep;
the cloud guide supplied 3,900 programmes for the same general window.

ATSC 3.0 remains cloud-only: it does not natively use MPEG-TS, and the FLEX
4K does not embed its distinct EPG format in its compatibility TS. Keep the
cloud `hdhr_epg.py` feed as the complete, all-channel source; use ATSC 1.0
PSIP as an optional supplementary source.

### Reusing the parser

`atsc_psip.py` has no HDHomeRun, network, XMLTV, or third-party dependency:

```python
import atsc_psip

with open("broadcast.ts", "rb") as stream:
    guide = atsc_psip.parse(stream.read())

for event in guide["events"]:
    print(event["start"], event["title"], event["description"])
```

`guide` contains `tables`, `channels`, `events`, and `ett`. Events are
deduplicated by source ID, event ID, and start time; matching ETT text is
already present in each event's `description` field.

Run the fixture-based parser regression test with:

```bash
python3 -m unittest tests/test_atsc_psip.py
```

## Files

```
hdhr_epg.py           # free cloud XMLTV tool (stdlib-only) — all channels
atsc_psip.py          # reusable MPEG-TS + ATSC A/65 PSIP parser (stdlib-only)
hdhr_ts_epg.py        # ATSC 1.0 RF-mux PSIP extractor and RF sweep tool
tests/test_atsc_psip.py # parser regression test (uses a local fixture capture)
systemd/              # one-shot service + 6h timer
Dockerfile, docker-compose.yml, docker-entrypoint.sh
epg.xml               # generated output (gitignored)
atsc1.xml             # generated merged ATSC 1.0 PSIP guide (gitignored)
```
