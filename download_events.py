#!/usr/bin/env python3
"""
Download event waveforms from NECIS.

Usage
-----
    # All events in the catalog (default catalog auto-detected)
    python download_events.py

    # Filter by date range and minimum magnitude
    python download_events.py --start 2024-01-01 --end 2024-12-31 --min-mag 2.0

    # Specify catalog explicitly
    python download_events.py --catalog meta/catalog_KMA_20160101-20260203.csv

    # Specific stations only
    python download_events.py --stations ADOA,AGSA,BAU

    # Adjust time window (seconds before/after origin)
    python download_events.py --pre 60 --post 180

Options
-------
    --catalog       Path to catalog CSV (default: auto-detect)
    --start         YYYY-MM-DD  Filter events on/after this date
    --end           YYYY-MM-DD  Filter events on/before this date
    --min-mag       Minimum magnitude threshold (default: 0.0)
    --stations      Comma-separated station codes
    --station-csv   Path to KP_station_list.csv
    --network       Network code (default: KS)
    --channels      Comma-separated channels (default: HHZ,HHN,HHE)
    --pre           Seconds before origin time (default: 30)
    --post          Seconds after origin time  (default: 120)
    --output-dir    Where to save files (default: data/necis/events)
    --capture-api   Save all XHR/Fetch calls to data/necis/api_calls.json
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from necis.config import NECISConfig
from necis.events import run_events

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def _find_catalog() -> str:
    candidates = [
        Path(__file__).parent / "meta" / "catalog_KMA_20160101-20260203.csv",
        Path(__file__).parent.parent / "SGTL-SKP-workspace" / "meta" / "catalog_KMA_20160101-20260203.csv",
    ]
    for c in sorted(candidates):
        if c.exists():
            return str(c)
    return ""


def _find_station_csv() -> str:
    candidates = [
        Path(__file__).parent / "meta" / "KP_station_list.csv",
        Path(__file__).parent.parent / "SGTL-SKP-workspace" / "meta" / "KP_station_list.csv",
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return ""


def _load_stations(csv_path: str) -> list[str]:
    try:
        import pandas as pd
        df = pd.read_csv(csv_path)
        col = next(c for c in df.columns if c.lower() == "station")
        return sorted(df[col].dropna().str.strip().tolist())
    except Exception as e:
        logger.error("Could not read station CSV %s: %s", csv_path, e)
        return []


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--catalog",     default=_find_catalog())
    p.add_argument("--start",       metavar="YYYY-MM-DD")
    p.add_argument("--end",         metavar="YYYY-MM-DD")
    p.add_argument("--min-mag",     type=float, default=0.0)
    p.add_argument("--stations",    metavar="STA1,STA2,...")
    p.add_argument("--station-csv", default=_find_station_csv())
    p.add_argument("--network",     default="KS")
    p.add_argument("--channels",    default="HHZ,HHN,HHE")
    p.add_argument("--pre",         type=int, default=30,  metavar="SEC")
    p.add_argument("--post",        type=int, default=120, metavar="SEC")
    p.add_argument("--output-dir",  metavar="PATH")
    p.add_argument("--capture-api", action="store_true")
    args = p.parse_args()

    if not args.catalog:
        sys.exit("Provide --catalog (no catalog CSV found automatically)")

    if args.stations:
        stations = [s.strip() for s in args.stations.split(",") if s.strip()]
    elif args.station_csv:
        stations = _load_stations(args.station_csv)
        if not stations:
            sys.exit(f"No stations loaded from {args.station_csv}")
        logger.info("Loaded %d stations from %s", len(stations), args.station_csv)
    else:
        sys.exit("Provide --stations or a valid --station-csv")

    channels = [c.strip() for c in args.channels.split(",") if c.strip()]

    overrides = {"capture_api": args.capture_api}
    if args.output_dir:
        overrides["download_dir"] = Path(args.output_dir)
    cfg = NECISConfig.from_env(**overrides)

    asyncio.run(
        run_events(
            config=cfg,
            catalog_path=Path(args.catalog),
            stations=stations,
            network=args.network,
            channels=channels,
            pre_sec=args.pre,
            post_sec=args.post,
            min_magnitude=args.min_mag,
            start_date=args.start,
            end_date=args.end,
        )
    )


if __name__ == "__main__":
    main()
