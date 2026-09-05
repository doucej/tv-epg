#!/bin/sh
set -e
cd /app
while true; do
    python3 hdhr_epg.py --host "${HOST:-hdhomerun.local}" --output "${OUTPUT:-/epg/epg.xml}" || \
        echo "[hdhr-epg] refresh failed; will retry next cycle" >&2
    sleep "${INTERVAL_SECONDS:-21600}"
done
