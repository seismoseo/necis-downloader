#!/bin/bash
# run_daily.sh — Daily NECIS continuous waveform download (cron wrapper).
#
# Auto-backfill: instead of a fixed "2 days ago" offset, this script finds
# the next date missing from the organized archive and downloads it.  If
# cron was down for several days it automatically catches up one day per run.
#
# The 2-day minimum lag is always respected: data for the most recent 2 days
# is not requested because NECIS may not have finished archiving it yet.
#
# Single-request approach: NECIS packages all ~404 KS stations into a split
# ZIP archive (.z01 + .zip) for large requests (~10 GB).  fetch_downloads.py
# detects the empty downloadPath, browses the FTP HTTP directory to find both
# parts, downloads them, and extracts with bsdtar.
#
# Session resilience: fetch_downloads.py navigates the browser to the history
# page every 5 polls to keep the NECIS session alive, and re-authenticates
# automatically if a 403 is returned despite that.
#
# Cron entry (1 AM KST every day):
#   0 1 * * * /home/msseo/works/Claude/run_daily.sh >> /home/msseo/works/Claude/logs/necis.log 2>&1
#
# Env overrides (set in .env or shell):
#   CONTINUOUS_DIR   organized output root (default: data/necis/continuous)
#   NECIS_USER / NECIS_PASS

set -uo pipefail
cd "$(dirname "$0")"
# shellcheck disable=SC1091
source .env 2>/dev/null || true

# Cron runs with a stripped PATH; add conda base (bsdtar) and user bin (mseed2sac)
export PATH="/home/msseo/miniforge3/bin:/home/msseo/bin:$PATH"

PYTHON=/home/msseo/miniforge3/envs/pipeline/bin/python
CONTINUOUS_DIR="${CONTINUOUS_DIR:-/home/msseo/works/Claude/data/necis/continuous}"

mkdir -p logs

# Never request data from the most recent 2 days — NECIS may not have it yet.
LIMIT=$(date -d "2 days ago" +%Y-%m-%d)

# Find the latest date already organized in the archive.
# Scans the most recent year directory and first station subdirectory only
# (all stations get the same dates), so this is fast even on a large archive.
LATEST_DATE=$(NECIS_CONT_DIR="$CONTINUOUS_DIR" "$PYTHON" -c "
import os, re, sys
from pathlib import Path
from datetime import date, timedelta
d = Path(os.environ['NECIS_CONT_DIR'])
if not d.exists():
    sys.exit(0)
year_dirs = sorted([x for x in d.iterdir() if x.is_dir() and x.name.isdigit()], reverse=True)
for yd in year_dirs:
    sta_dirs = sorted([x for x in yd.iterdir() if x.is_dir()])
    for sd in sta_dirs:
        files = sorted(sd.iterdir())
        if files:
            m = re.search(r'\.(\d{4})\.(\d{3})\.', files[-1].name)
            if m:
                y, doy = int(m.group(1)), int(m.group(2))
                print((date(y, 1, 1) + timedelta(days=doy - 1)).strftime('%Y-%m-%d'))
                sys.exit(0)
" 2>/dev/null)

if [ -n "$LATEST_DATE" ]; then
    NEXT=$(date -d "$LATEST_DATE + 1 day" +%Y-%m-%d)
    if [[ "$NEXT" > "$LIMIT" ]]; then
        echo "=== $(date '+%Y-%m-%d %H:%M:%S') Up to date — archive through $LATEST_DATE, limit $LIMIT ==="
        exit 0
    fi
    DATE="$NEXT"
    if [[ "$DATE" < "$LIMIT" ]]; then
        echo "=== $(date '+%Y-%m-%d %H:%M:%S') Backfilling $DATE (archive through $LATEST_DATE, target $LIMIT) ==="
    else
        echo "=== $(date '+%Y-%m-%d %H:%M:%S') Daily NECIS download for $DATE ==="
    fi
else
    # No organized data yet — start from the limit date
    DATE="$LIMIT"
    echo "=== $(date '+%Y-%m-%d %H:%M:%S') Daily NECIS download for $DATE (no prior archive found) ==="
fi

"$PYTHON" download_continuous.py \
    --date           "$DATE" \
    --channels       HHZ,HHN,HHE \
    --fetch \
    --poll-interval  60 \
    --max-wait       7200 \
    --organize \
    --continuous-dir "$CONTINUOUS_DIR"

echo "=== $(date '+%Y-%m-%d %H:%M:%S') Done ==="
