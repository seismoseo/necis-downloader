#!/bin/bash
# run_daily.sh  — Daily NECIS continuous waveform download (cron wrapper).
#
# Add to crontab (runs at 04:30 KST every morning):
#   30 4 * * * /home/msseo/works/Claude/run_daily.sh >> /var/log/necis.log 2>&1
#
# Requires: .env with NECIS_USER and NECIS_PASS in the same directory.

set -uo pipefail
cd "$(dirname "$0")"
# shellcheck disable=SC1091
source .env 2>/dev/null || true

DATE=$(date -d "yesterday" +%Y-%m-%d)
echo "=== $(date '+%Y-%m-%d %H:%M:%S') Daily NECIS download for $DATE ==="
bash "$(dirname "$0")/archive_necis.sh" "$DATE" "$DATE"
echo "=== $(date '+%Y-%m-%d %H:%M:%S') Done ==="
