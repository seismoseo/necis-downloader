#!/bin/bash
# run_daily.sh — Daily NECIS continuous waveform download (cron wrapper).
#
# Downloads all stations currently available on the NECIS page for yesterday.
# No station CSV needed — stations are scraped from the NECIS DOM at runtime.
#
# Cron entry (11 AM KST every day — 2 h after UTC midnight so yesterday's UTC data is complete):
#   0 11 * * * /home/msseo/works/Claude/run_daily.sh >> /home/msseo/works/Claude/logs/necis.log 2>&1
#
# Env overrides (set in .env or shell):
#   CONTINUOUS_DIR   organized output root (default: data/necis/continuous)
#   NECIS_USER / NECIS_PASS
#
# Notes:
#   - NECIS allows ~10 GB/day. One full day of all KS stations ≈ 9.5 GB.
#   - --max-wait 7200 (2 h): the server takes ~10–30 min to prepare a full-day zip.
#   - --poll-interval 60: reduces polling noise for a single long-running job.

set -uo pipefail
cd "$(dirname "$0")"
# shellcheck disable=SC1091
source .env 2>/dev/null || true

DATE=$(date -d "yesterday" +%Y-%m-%d)
CONTINUOUS_DIR="${CONTINUOUS_DIR:-/home/msseo/works/Claude/data/necis/continuous}"

mkdir -p logs

echo "=== $(date '+%Y-%m-%d %H:%M:%S') Daily NECIS download for $DATE ==="

/home/msseo/miniforge3/envs/pipeline/bin/python download_continuous.py \
    --date           "$DATE" \
    --channels       HHZ,HHN,HHE \
    --fetch \
    --poll-interval  60 \
    --max-wait       7200 \
    --organize \
    --continuous-dir "$CONTINUOUS_DIR"

echo "=== $(date '+%Y-%m-%d %H:%M:%S') Done ==="
