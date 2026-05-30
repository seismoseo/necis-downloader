#!/usr/bin/env python3
"""
Download event waveforms from NECIS.

Catalog formats accepted
------------------------
Jangsung format (KST times, no event_id):
  Year,Month,Day,Hour,Minute,Second,Latitude,Longitude,Depth,Magnitude
  2023,5,26,20,39,54,35.46,126.81,8,1.1

KMA catalog format (UTC datetime + event_id):
  datetime,event_id,...
  2016-01-01 17:27:10,201601_0001,...

Output layout (matches kma_waveforms/ in the Jangsung cluster project):
  data/necis/events/
    {UTC_YYYYMMDDHHmmss}/
      {NECIS_ID}.a/  MSEED/  SAC/HG/  ...   ← acceleration
      {NECIS_ID}.v/  MSEED/  SAC/HH/  ...   ← velocity

Usage
-----
    # Jangsung catalog, all events
    python download_events.py --catalog event_catalog.csv --stations ADOA,AGSA

    # KMA catalog, magnitude filter, date range
    python download_events.py \\
        --catalog meta/catalog_KMA_20160101-20260203.csv \\
        --start 2024-01-01 --end 2024-12-31 --min-mag 2.0

    # Acceleration only, no SAC conversion
    python download_events.py --catalog event_catalog.csv --data-type a --no-convert-sac

Options
-------
    --catalog       Path to catalog CSV (default: auto-detect)
    --start         YYYY-MM-DD  Filter events on/after this date
    --end           YYYY-MM-DD  Filter events on/before this date
    --min-mag       Minimum magnitude threshold (default: 0.0)
    --stations      Comma-separated station codes
    --station-csv   Path to KP_station_list.csv
    --channels      Comma-separated channels, e.g. HHZ,HHN,HHE (default: all 3 components)
    --pre           Seconds before origin time (default: 30)
    --post          Seconds after origin time  (default: 120)
    --data-type     a (acceleration), v (velocity), or both (default: both)
    --convert-sac / --no-convert-sac
                    Convert miniSEED → SAC via mseed2sac (default: on)
    --output-dir    Root for organized event data (default: data/necis/events)
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
    p.add_argument("--catalog",      default=_find_catalog(),
                   help="Path to event catalog CSV")
    p.add_argument("--start",        metavar="YYYY-MM-DD",
                   help="Filter events on/after this date")
    p.add_argument("--end",          metavar="YYYY-MM-DD",
                   help="Filter events on/before this date")
    p.add_argument("--min-mag",      type=float, default=0.0,
                   help="Minimum magnitude (default: 0.0)")
    p.add_argument("--stations",     metavar="STA1,STA2,...",
                   help="Comma-separated KS station codes")
    p.add_argument("--station-csv",  default=_find_station_csv(),
                   help="Path to KP_station_list.csv")
    p.add_argument("--channels",     default="HHZ,HHN,HHE",
                   help="Comma-separated channels (default: HHZ,HHN,HHE)")
    p.add_argument("--pre",          type=int, default=30,  metavar="SEC",
                   help="Seconds before origin time (default: 30)")
    p.add_argument("--post",         type=int, default=120, metavar="SEC",
                   help="Seconds after origin time (default: 120)")
    p.add_argument("--data-type",    default="both",
                   choices=["a", "v", "both"],
                   help="Data type: a=acceleration, v=velocity, both (default: both)")
    p.add_argument("--convert-sac",  action=argparse.BooleanOptionalAction, default=True,
                   help="Convert miniSEED → SAC via mseed2sac (default: on)")
    p.add_argument("--output-dir",   metavar="PATH",
                   help="Root for organized event data (default: data/necis/events)")
    p.add_argument("--capture-api",  action="store_true",
                   help="Save all XHR/Fetch calls to data/necis/api_calls.json")
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

    # Map channels to component letters (last char) for NECIS checkboxes
    channels   = [c.strip() for c in args.channels.split(",") if c.strip()]
    components = list({c[-1].upper() for c in channels})

    data_types = ["a", "v"] if args.data_type == "both" else [args.data_type]

    overrides = {"capture_api": args.capture_api}
    cfg = NECISConfig.from_env(**overrides)

    out_root = Path(args.output_dir) if args.output_dir else None

    logger.info(
        "Event download | catalog: %s | stations: %d | types: %s | SAC: %s",
        args.catalog, len(stations), data_types, args.convert_sac,
    )

    asyncio.run(
        run_events(
            config=cfg,
            catalog_path=Path(args.catalog),
            stations=stations,
            components=components,
            pre_sec=args.pre,
            post_sec=args.post,
            min_magnitude=args.min_mag,
            start_date=args.start,
            end_date=args.end,
            data_types=data_types,
            convert_sac=args.convert_sac,
            out_root=out_root,
        )
    )


if __name__ == "__main__":
    main()
