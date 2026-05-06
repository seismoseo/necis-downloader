#!/bin/bash
# archive_necis.sh  — Download all KS-network continuous waveforms from NECIS.
#
# Batches stations (2/request) to stay under the NECIS 40 MB per-request limit.
# One browser login per calendar day; all station batches share that session.
#
# Usage:
#   nohup ./archive_necis.sh >> logs/archive_necis.log 2>&1 &          # 2026-01-01 → yesterday
#   nohup ./archive_necis.sh 2026-01-01 2026-03-31 >> logs/... 2>&1 &  # custom range
#   tail -f logs/archive_necis.log                                       # monitor
#
# Env overrides:
#   CONTINUOUS_DIR   output root          (default: …/data/necis/continuous)
#   BATCH_SIZE       stations per request (default: 2)
#                    2 stations × 3 components ≈ 36 MB zip, just under the 40 MB cap
#                    For Z-only downloads (--channels HHZ) you can raise this to 6
#   STATION_CSV      path to KP_station_list.csv
#   NECIS_USER / NECIS_PASS  loaded from .env if present
#
# Estimated runtime (406 KS stations, 3 components, BATCH_SIZE=2):
#   ~203 batches/day × ~4 min/batch ≈ 13 h per calendar day of data
#   Full year (365 days) ≈ 50 days wall time — run in tmux/screen or with nohup.

set -uo pipefail
cd "$(dirname "$0")"
# shellcheck disable=SC1091
source .env 2>/dev/null || true

START="${1:-2026-01-01}"
END="${2:-$(date -d "yesterday" +%Y-%m-%d)}"
CONTINUOUS_DIR="${CONTINUOUS_DIR:-/home/msseo/works/Claude/data/necis/continuous}"
STATION_CSV="${STATION_CSV:-/home/msseo/works/SGTL-SKP-workspace/meta/KP_station_list.csv}"
BATCH_SIZE="${BATCH_SIZE:-2}"
CHANNELS="${CHANNELS:-HHZ,HHN,HHE}"
POLL_INTERVAL=30
MAX_WAIT=600

mkdir -p logs
log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*"; }

[[ -f "$STATION_CSV" ]] || { echo "ERROR: Station CSV not found: $STATION_CSV" >&2; exit 1; }

# Print estimated runtime before starting
python3 - "$START" "$END" "$BATCH_SIZE" <<'PYEOF'
import sys
from datetime import date
start = date.fromisoformat(sys.argv[1])
end   = date.fromisoformat(sys.argv[2])
bsz   = int(sys.argv[3])
days  = (end - start).days + 1
bpd   = -(-404 // bsz)   # ceiling division (404 KS stations)
total = days * bpd
h, m  = divmod(total * 4, 60)
print(f"Archive : {start} → {end}  |  {days} day(s)  |  ~{bpd} batches/day")
print(f"Est.    : {total} requests × ~4 min ≈ {h}h {m}min wall time")
PYEOF

log "Starting  : $START → $END"
log "Output    : $CONTINUOUS_DIR"
log "Channels  : $CHANNELS"
log "Batch size: $BATCH_SIZE stations/request"

python download_continuous.py \
    --start          "$START" \
    --end            "$END" \
    --station-csv    "$STATION_CSV" \
    --network        "KS" \
    --channels       "$CHANNELS" \
    --batch-size     "$BATCH_SIZE" \
    --fetch \
    --poll-interval  "$POLL_INTERVAL" \
    --max-wait       "$MAX_WAIT" \
    --organize \
    --continuous-dir "$CONTINUOUS_DIR"

log "Complete  : $START → $END"
