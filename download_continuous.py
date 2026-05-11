#!/usr/bin/env python3
"""
Download continuous waveforms from NECIS.

Default behaviour (no arguments): download yesterday's data for all KS-network
stations listed in the station CSV, then wait for the server to prepare the
zip and save it to --continuous-dir.

Usage
-----
    # Yesterday (default)
    python download_continuous.py

    # Specific date, only request (no fetch/organize)
    python download_continuous.py --date 2024-03-15 --no-fetch

    # Date range
    python download_continuous.py --start 2024-03-01 --end 2024-03-31

    # Specific stations
    python download_continuous.py --stations ADOA,AGSA,BAU

    # Show browser (useful for debugging selector issues)
    NECIS_HEADLESS=0 python download_continuous.py --date 2024-03-15

    # Also capture API calls
    python download_continuous.py --capture-api

Options
-------
    --date          YYYY-MM-DD   Single day (overrides --start/--end)
    --start         YYYY-MM-DD   Range start (default: yesterday)
    --end           YYYY-MM-DD   Range end   (default: yesterday)
    --stations      Comma-separated station codes (no network prefix)
    --station-csv   Path to KP_station_list.csv (default: auto-detect)
    --channels      Comma-separated channels (default: HHZ,HHN,HHE)
    --output-dir    Intermediate download dir (default: data/necis)
    --capture-api   Save all XHR/Fetch calls to data/necis/api_calls.json

    --fetch/--no-fetch          Wait and download ready files (default: on)
    --poll-interval SEC         Seconds between polls (default: 30)
    --max-wait      SEC         Max seconds to wait for server (default: 600)
    --organize/--no-organize    Extract + organize into date tree (default: on)
    --continuous-dir PATH       Root for organized data (default: /data/continuous)

Credentials: set NECIS_USER and NECIS_PASS (or create a .env file).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

from necis.config import NECISConfig
from necis.continuous import run_continuous

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def _parse_args():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--date",        metavar="YYYY-MM-DD")
    p.add_argument("--start",       metavar="YYYY-MM-DD")
    p.add_argument("--end",         metavar="YYYY-MM-DD")
    p.add_argument("--stations",    metavar="STA1,STA2,...")
    p.add_argument("--station-csv", metavar="PATH",
                   default="")
    p.add_argument("--channels",    default="HHZ,HHN,HHE")
    p.add_argument("--output-dir",  metavar="PATH")
    p.add_argument("--capture-api", action="store_true")

    p.add_argument("--batch-size", type=int, default=None, metavar="N",
                   help="Max stations per NECIS request. Use 2 for 3-component 100 Hz "
                        "data to stay under the 40 MB cap (default: all at once).")
    p.add_argument("--network", default="KS",
                   help="Filter station CSV to this network code (default: KS). "
                        "Pass '' to load all networks.")

    p.add_argument("--fetch",         action=argparse.BooleanOptionalAction, default=True,
                   help="Wait and fetch prepared zip files after request (default: on)")
    p.add_argument("--fetch-only",    action="store_true",
                   help="Skip the request step; poll history and download already-queued files")
    p.add_argument("--poll-interval", type=int, default=30, metavar="SEC")
    p.add_argument("--max-wait",      type=int, default=600, metavar="SEC")
    p.add_argument("--organize",      action=argparse.BooleanOptionalAction, default=True,
                   help="Extract and organize downloaded zips into date tree (default: on)")
    p.add_argument("--continuous-dir", metavar="PATH",
                   help="Root directory for organized continuous data (default: /data/continuous)")

    return p.parse_args()


def _find_station_csv() -> str:
    candidates = [
        Path(__file__).parent / "meta" / "KP_station_list.csv",
        Path(__file__).parent.parent / "SGTL-SKP-workspace" / "meta" / "KP_station_list.csv",
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return ""


def _load_stations_from_csv(csv_path: str, network: str = "KS") -> list[str]:
    try:
        import pandas as pd
        df = pd.read_csv(csv_path)
        sta_col = next(c for c in df.columns if c.lower() == "station")
        net_col = next((c for c in df.columns if c.lower() == "network"), None)
        if network and net_col:
            df = df[df[net_col].str.strip() == network]
        return sorted(df[sta_col].dropna().str.strip().tolist())
    except Exception as e:
        logger.error("Could not read station CSV %s: %s", csv_path, e)
        return []


def main():
    args = _parse_args()

    yesterday = date.today() - timedelta(days=1)
    if args.date:
        start = end = date.fromisoformat(args.date)
    else:
        start = date.fromisoformat(args.start) if args.start else yesterday
        end   = date.fromisoformat(args.end)   if args.end   else yesterday

    if end < start:
        sys.exit("--end must be >= --start")

    if args.stations:
        stations = [s.strip() for s in args.stations.split(",") if s.strip()]
    elif args.station_csv:
        stations = _load_stations_from_csv(args.station_csv, args.network)
        if not stations:
            sys.exit(f"No stations loaded from {args.station_csv}")
        logger.info("Loaded %d stations from %s (network=%s)",
                    len(stations), args.station_csv, args.network or "all")
    else:
        stations = None  # all stations currently on the NECIS page (scraped from DOM)
        logger.info("No station list provided — all available NECIS stations will be requested")

    channels = [c.strip() for c in args.channels.split(",") if c.strip()]
    # NECIS component checkboxes use single-letter codes (last char of channel name)
    components = [c[-1] for c in channels]

    overrides = {"capture_api": args.capture_api}
    if args.output_dir:
        overrides["download_dir"] = Path(args.output_dir)
    cfg = NECISConfig.from_env(**overrides)

    sta_desc = f"{len(stations)} station(s)" if stations is not None else "all NECIS stations"
    logger.info("Continuous download | %s → %s | %s | channels: %s",
                start, end, sta_desc, channels)

    if not args.fetch:
        # Submit-only mode: queue all days then exit (user fetches manually later)
        asyncio.run(run_continuous(cfg, start, end, stations, components))
        return

    # Full pipeline: for each day — submit batches → poll/download → organize
    from datetime import datetime
    from necis.browser import NECISBrowser
    from necis.continuous import request_day
    from necis.fetch_downloads import fetch_ready_downloads
    from necis.utils import process_continuous_downloads

    zip_dir   = cfg.download_dir / "zips"
    cont_root = Path(args.continuous_dir or "/home/msseo/works/Claude/data/necis/continuous")

    if args.fetch_only:
        # Skip request; download whatever is already prepared in the history
        async def _fetch_only():
            async with NECISBrowser(cfg) as browser:
                files = await fetch_ready_downloads(
                    browser,
                    out_dir=zip_dir,
                    poll_interval=args.poll_interval,
                    max_wait=args.max_wait,
                )
                if args.organize and files:
                    organized = process_continuous_downloads(
                        zip_dir, cont_root, move=True, delete_zip=True)
                    logger.info("Organized %d file(s) → %s", len(organized), cont_root)
                return files
        asyncio.run(_fetch_only())
        return

    if stations is None:
        # Single request for all stations currently on the NECIS page
        batches       = [None]
        total_batches = 1
    else:
        batch_size    = args.batch_size or len(stations)
        batches       = [stations[i:i+batch_size]
                         for i in range(0, len(stations), batch_size)]
        total_batches = len(batches)

    day = start
    while day <= end:
        logger.info("=" * 60)
        if total_batches == 1 and batches[0] is None:
            logger.info("Processing %s  (1 request, all NECIS stations)", day)
        else:
            logger.info("Processing %s  (%d batch(es))", day, total_batches)

        async def run_one_day(day=day):
            total_organized = 0
            async with NECISBrowser(cfg) as browser:   # one login per day
                for b_idx, sta_batch in enumerate(batches, 1):
                    batch_label = ",".join(sta_batch) if sta_batch is not None else "ALL stations"
                    logger.info("[%s] batch %d/%d: %s",
                                day, b_idx, total_batches, batch_label)
                    submitted_after = datetime.now()
                    ok = await request_day(browser, day, sta_batch, components)
                    if not ok:
                        logger.warning("[%s] batch %d request failed — skipping",
                                       day, b_idx)
                        continue
                    files = await fetch_ready_downloads(
                        browser,
                        out_dir=zip_dir,
                        poll_interval=args.poll_interval,
                        max_wait=args.max_wait,
                        submitted_after=submitted_after,
                    )
                    if args.organize and files:
                        organized = process_continuous_downloads(
                            zip_dir, cont_root, move=True, delete_zip=True)
                        total_organized += len(organized)
                        logger.info("[%s] batch %d/%d organized %d file(s) → %s",
                                    day, b_idx, total_batches,
                                    len(organized), cont_root)
            return total_organized

        total = asyncio.run(run_one_day())
        logger.info("[%s] Day complete — %d miniSEED file(s) organized", day, total)

        day += timedelta(days=1)


if __name__ == "__main__":
    main()
