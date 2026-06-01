#!/usr/bin/env python3
"""Batch-fetch NECIS instrument-response (SEED RESP) files for one or more stations.

Mirrors the philosophy of `download_continuous.py` / `download_events.py`: one
authenticated NECIS session, sequential per-station POSTs to the RESP-download
endpoint (`/necis-dbf/usernl/ob/observatoryListEarthDown.do`), output as a tree of
`RESP.<NET>.<STA>..<CHAN>` files that `obspy.read_inventory()` consumes directly.

Usage
-----

    # The 7 stations missing from the existing master StationXML
    python fetch_responses.py --network KS \
        --stations BAEA,DAJA,GJAA,HYDA,NARA,SRGA,UICA \
        --out /home/msseo/works/02.Ulsan_Fault_detection/KS_KG/local_magnitudes/responses/fetched

    # Or read station codes from a text file (one per line)
    python fetch_responses.py --network KS --stations-file missing.txt --out responses/fetched

Output layout
-------------
    <out>/zips/RESP_<NET>_<STA>.zip       (the raw NECIS zip per station)
    <out>/extracted/RESP.<NET>.<STA>..<CHAN>   (the SEED RESP files, ready for obspy)

Credentials
-----------
NECIS_USER / NECIS_PASS read from `.env` (same as the waveform downloaders).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Allow running from anywhere
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# Load .env without requiring python-dotenv (shell-style KEY=VALUE only)
_envf = HERE / ".env"
if _envf.exists():
    for ln in _envf.read_text().splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#") or "=" not in ln:
            continue
        k, _, v = ln.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip("\"'"))

from necis.responses import fetch_resp_for_stations


def main() -> int:
    p = argparse.ArgumentParser(description="Fetch NECIS RESP files for stations.")
    p.add_argument("--network", default="KS", help="Network code (KS / KG / KP …). Default KS.")
    p.add_argument("--stations", help="Comma-separated station codes (e.g. BAEA,DAJA,...).")
    p.add_argument("--stations-file", help="File with one station code per line (alternative to --stations).")
    p.add_argument("--out", required=True, help="Output directory root.")
    p.add_argument("--no-extract", action="store_true",
                   help="Only download the zips; skip RESP-file extraction.")
    p.add_argument("--force", action="store_true",
                   help="Re-download even if the per-station zip already exists.")
    args = p.parse_args()

    if not (args.stations or args.stations_file):
        p.error("provide either --stations or --stations-file")
    if args.stations:
        stations = [s.strip() for s in args.stations.split(",") if s.strip()]
    else:
        stations = [ln.strip() for ln in open(args.stations_file)
                    if ln.strip() and not ln.startswith("#")]

    if not (os.environ.get("NECIS_USER") and os.environ.get("NECIS_PASS")):
        print("ERROR: NECIS_USER / NECIS_PASS not set (check .env)", file=sys.stderr)
        return 1

    print(f"network={args.network}  stations={stations}  out={args.out}")
    result = fetch_resp_for_stations(
        stations, network=args.network, out_dir=args.out,
        extract=not args.no_extract, force=args.force,
    )
    n_ok = sum(1 for v in result.values() if v)
    n_resp = sum(len(v) for v in result.values())
    print(f"\nfetched {n_ok}/{len(stations)} stations, "
          f"{n_resp} RESP files extracted in {args.out}/extracted/")
    return 0 if n_ok == len(stations) else 2


if __name__ == "__main__":
    sys.exit(main())
