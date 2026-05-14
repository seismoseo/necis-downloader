#!/bin/bash
# run_daily.sh — Daily NECIS continuous waveform download (cron wrapper).
#
# Downloads all KS stations in batches of 20 for 2 days ago.
#
# Batch approach (--batch-size 20, ~21 batches of ~600 MB each) is used because
# NECIS now creates split ZIP archives (.z01 + .zip) for large all-stations
# requests (~10 GB), and the history API leaves downloadPath empty for split
# archives. Batching keeps each ZIP under the single-file threshold so the
# download URL is always populated.
#
# Targeting 2 days ago ensures the requested UTC date is fully complete at 1 AM
# KST (= 16:00 UTC previous day, well past UTC midnight for day-before-yesterday).
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

DATE=$(date -d "2 days ago" +%Y-%m-%d)
CONTINUOUS_DIR="${CONTINUOUS_DIR:-/home/msseo/works/Claude/data/necis/continuous}"

mkdir -p logs

echo "=== $(date '+%Y-%m-%d %H:%M:%S') Daily NECIS download for $DATE ==="

/home/msseo/miniforge3/envs/pipeline/bin/python download_continuous.py \
    --date           "$DATE" \
    --channels       HHZ,HHN,HHE \
    --station-csv    /home/msseo/works/SGTL-SKP-workspace/meta/KP_station_list.csv \
    --batch-size     20 \
    --fetch \
    --poll-interval  30 \
    --max-wait       600 \
    --organize \
    --continuous-dir "$CONTINUOUS_DIR"

echo "=== $(date '+%Y-%m-%d %H:%M:%S') Done ==="
