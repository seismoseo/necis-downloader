#!/bin/bash
# run_daily.sh — Daily NECIS continuous waveform download (cron wrapper).
#
# Downloads all KS stations in a single request for 2 days ago.
#
# Single-request approach: NECIS packages all ~404 stations into a split ZIP
# archive (.z01 + .zip) for large requests (~10 GB).  fetch_downloads.py detects
# the empty downloadPath, browses the FTP HTTP directory to find both parts,
# downloads them, and extracts with unzip (which handles split ZIPs natively).
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
    --fetch \
    --poll-interval  60 \
    --max-wait       7200 \
    --organize \
    --continuous-dir "$CONTINUOUS_DIR"

echo "=== $(date '+%Y-%m-%d %H:%M:%S') Done ==="
