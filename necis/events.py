"""
Event waveform downloader.

Downloads waveform data for earthquakes listed in a catalog CSV and organizes
them to match the kma_waveforms layout used in the Jangsung cluster project:

  out_root/
    {UTC_YYYYMMDDHHmmss}/         ← outer dir = UTC origin time
      {NECIS_ID}.a/               ← acceleration
        MSEED/  KS.ADOA.HGZ.YYYY.DDD.HH.MM.SS
        SAC/HG/ KS.ADOA..HGZ.D.YYYY.DDD.HHMMSS.SAC
      {NECIS_ID}.v/               ← velocity
        MSEED/  ...
        SAC/HH/ ...
    RESP/
      {NECIS_ID}.r/               ← instrument response files

NECIS event architecture (direct FTP, no request queue)
--------------------------------------------------------
Event ZIPs are pre-packaged and available immediately on the NECIS FTP server:
  http://ftp.necis.kma.go.kr:8080/EVENT/YYYY/JDAY/NECIS_ID/NECIS_ID.{a|v|r}.zip

The event search page (earthquake_event_map.do) lists available events.
Each row exposes onclick attributes:
  fn_file_view('NECIS_ID', 'UTC_DATETIME', lat, lon, 'a|v')
  fn_file_download('EVENT/YYYY/DDD/ID/ID.a.zip|EVENT/.../ID.r.zip', size)

Date inputs are readonly jQuery datepicker fields — set via page.evaluate().

Catalog formats supported
--------------------------
Jangsung format (Year/Month/Day/Hour/Minute/Second columns, KST):
  Year,Month,Day,Hour,Minute,Second,Latitude,Longitude,Depth,Magnitude
  2023,5,26,20,39,54,35.46,126.81,8,1.1

KMA catalog format (datetime column, UTC):
  datetime,event_id,...
  2016-01-01 17:27:10,201601_0001,...
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional, Sequence

import pandas as pd

from .browser import NECISBrowser
from .config import NECISConfig
from .fetch_downloads import _copy_cookies_to_session, _download_file

logger = logging.getLogger(__name__)

# NECIS event waveform search page
EVENTS_PAGE_URL = "/necis-dbf/user/earthquake/earthquake_event_map.do"

# FTP base for event ZIPs (same host as continuous waveform FTP)
FTP_BASE = "http://ftp.necis.kma.go.kr:8080"

# Tolerance for matching catalog events to NECIS page rows (seconds)
TIME_MATCH_TOL_S = 10


# ---------------------------------------------------------------------------
# Catalog loading
# ---------------------------------------------------------------------------

def load_catalog(csv_path: Path, tz_offset_hours: int = 9) -> pd.DataFrame:
    """Load an event catalog CSV.

    Supports two formats:

    1. Jangsung format — split time columns, times in KST:
         Year, Month, Day, Hour, Minute, Second, Latitude, Longitude, Depth, Magnitude
       Converted to UTC by subtracting tz_offset_hours (default 9 for KST).
       event_id derived as UTC YYYYMMDDHHmmss.

    2. KMA catalog format — single datetime column, times in UTC:
         datetime (or origin_time / time / origintime), event_id, ...
       event_id used as-is; if absent, derived from datetime.

    Returns a DataFrame with columns: origin_time (UTC, naive), event_id, and
    whatever else was in the original file.
    """
    df = pd.read_csv(csv_path, dtype=str)
    cols_lower = {c.lower().strip(): c for c in df.columns}

    time_parts = ("year", "month", "day", "hour", "minute", "second")
    if all(k in cols_lower for k in time_parts):
        # Jangsung format — build datetime from parts, then shift KST → UTC
        numeric = {k: pd.to_numeric(df[cols_lower[k]], errors="coerce") for k in time_parts}
        origin_kst = pd.to_datetime(numeric)
        df["origin_time"] = origin_kst - pd.Timedelta(hours=tz_offset_hours)
        df["event_id"]    = df["origin_time"].dt.strftime("%Y%m%d%H%M%S")
        for k in time_parts:
            df[k] = numeric[k]
    else:
        # KMA / generic format — find any datetime column
        for col_key in ("origin_time", "datetime", "time", "origintime"):
            if col_key in cols_lower:
                df["origin_time"] = pd.to_datetime(df[cols_lower[col_key]])
                break
        else:
            raise ValueError(
                f"No recognised time column in {csv_path}. "
                "Expected Year/Month/Day/Hour/Minute/Second OR a datetime column."
            )
        if "event_id" not in cols_lower:
            df["event_id"] = df["origin_time"].dt.strftime("%Y%m%d%H%M%S")
        else:
            df["event_id"] = df[cols_lower["event_id"]].str.strip()

    if "magnitude" not in cols_lower and "ml" in cols_lower:
        df["magnitude"] = pd.to_numeric(df[cols_lower["ml"]], errors="coerce")
    elif "magnitude" in cols_lower:
        df["magnitude"] = pd.to_numeric(df[cols_lower["magnitude"]], errors="coerce")

    return df


# ---------------------------------------------------------------------------
# onclick attribute parsers
# ---------------------------------------------------------------------------

def _parse_fn_file_view(onclick: str) -> tuple[Optional[str], Optional[datetime]]:
    """Extract (necis_id, utc_datetime) from fn_file_view onclick.

    Pattern: fn_file_view('2023002939', '2023-05-26 11:39:54', ...)
    The datetime is UTC as displayed on the NECIS event page.
    """
    m = re.search(
        r"fn_file_view\(\s*'([^']+)'\s*,\s*'(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})'",
        onclick,
    )
    if not m:
        return None, None
    try:
        utc_dt = datetime.strptime(m.group(2), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None, None
    return m.group(1).strip(), utc_dt


def _parse_fn_file_download(onclick: str) -> list[str]:
    """Extract FTP path list from fn_file_download onclick.

    Pattern: fn_file_download('EVENT/2023/146/2023002939/2023002939.a.zip|
                                EVENT/2023/146/2023002939/2023002939.r.zip', size)
    Returns list of FTP relative paths (pipe-separated in the onclick attribute).
    """
    m = re.search(r"fn_file_download\(\s*'([^']+)'", onclick)
    if not m:
        return []
    return [p.strip() for p in m.group(1).split("|") if p.strip()]


# ---------------------------------------------------------------------------
# Event page search
# ---------------------------------------------------------------------------

async def _search_event(
    browser: NECISBrowser,
    origin_time: datetime,
) -> Optional[dict]:
    """Search the NECIS event page and return download info for the matching event.

    Sets readonly date filter fields via JavaScript (bypasses jQuery datepicker),
    submits the search, then parses fn_file_view / fn_file_download onclick
    attributes from the results to find the event nearest to origin_time.

    Returns a dict:
      necis_id  : NECIS internal event ID, e.g. '2023002939'
      ftp_paths : list of relative FTP paths (deduplicated)
    or None if no match within TIME_MATCH_TOL_S seconds.
    """
    await browser.page.goto(
        browser.cfg.base_url + EVENTS_PAGE_URL,
        wait_until="load",
        timeout=browser.cfg.timeout_ms,
    )
    await asyncio.sleep(2)

    date_str = origin_time.strftime("%Y-%m-%d")

    # Date inputs are readonly jQuery datepicker fields — remove readonly and set value
    try:
        await browser.page.evaluate(
            "([d]) => {"
            "  var inputs = ['startDate', 'endDate'];"
            "  inputs.forEach(function(name) {"
            "    var el = document.querySelector('input[name=\"' + name + '\"]');"
            "    if (el) { el.removeAttribute('readonly'); el.value = d; }"
            "  });"
            "}",
            [date_str],
        )
    except Exception as e:
        logger.error("Could not set date fields for %s: %s", date_str, e)
        return None

    # Click the search button
    try:
        search_btn = await browser.page.query_selector('a[onclick*="fn_setTableList"]')
        if search_btn is None:
            logger.error("Search button (fn_setTableList) not found on event page")
            return None
        await search_btn.click()
        await asyncio.sleep(3)
    except Exception as e:
        logger.error("Search click failed for %s: %s", date_str, e)
        return None

    # Collect all onclick attributes from the page
    try:
        onclick_texts: list[str] = await browser.page.evaluate(
            """() => {
                var attrs = [];
                document.querySelectorAll('[onclick]').forEach(function(el) {
                    var v = el.getAttribute('onclick');
                    if (v) attrs.push(v);
                });
                return attrs;
            }"""
        )
    except Exception as e:
        logger.error("Could not collect onclick attributes: %s", e)
        return None

    view_attrs     = [a for a in onclick_texts if "fn_file_view"     in a]
    download_attrs = [a for a in onclick_texts if "fn_file_download" in a]

    if not view_attrs:
        logger.warning("No events found on NECIS page for date %s", date_str)
        return None

    # Find the row whose UTC time is closest to origin_time
    best: Optional[dict] = None
    best_diff = float("inf")

    for va in view_attrs:
        necis_id, row_utc = _parse_fn_file_view(va)
        if necis_id is None or row_utc is None:
            continue
        diff = abs((row_utc - origin_time).total_seconds())
        if diff < best_diff:
            best_diff = diff
            # Collect all FTP paths that reference this NECIS ID (deduplicated)
            seen: set[str] = set()
            paths: list[str] = []
            for da in download_attrs:
                for p in _parse_fn_file_download(da):
                    if necis_id in p and p not in seen:
                        seen.add(p)
                        paths.append(p)
            best = {"necis_id": necis_id, "row_utc": row_utc, "ftp_paths": paths}

    if best is None or best_diff > TIME_MATCH_TOL_S:
        logger.warning(
            "[%s] Closest NECIS row differs by %.0fs (tolerance %ds) — skipping",
            origin_time.strftime("%Y-%m-%d %H:%M:%S"), best_diff, TIME_MATCH_TOL_S,
        )
        return None

    logger.info(
        "Matched NECIS event %s (Δ%.0fs, %d FTP path(s))",
        best["necis_id"], best_diff, len(best["ftp_paths"]),
    )
    return best


# ---------------------------------------------------------------------------
# Per-event downloader
# ---------------------------------------------------------------------------

async def download_event(
    browser: NECISBrowser,
    event_id: str,
    origin_time: datetime,
    data_types: Sequence[str] = ("a", "v"),
    zip_dir: Optional[Path] = None,
    out_root: Optional[Path] = None,
    convert_sac: bool = True,
) -> dict[str, list[Path]]:
    """Download waveforms for a single event (acceleration and/or velocity).

    Locates the event on the NECIS event page, then directly downloads the
    pre-packaged ZIP files from the NECIS FTP server.  No request queue is
    used — event ZIPs are always pre-available on FTP.

    Also downloads the RESP (.r.zip) file into out_root/RESP/{necis_id}.r/.

    Parameters
    ----------
    event_id    : UTC YYYYMMDDHHmmss string (outer directory name)
    origin_time : UTC datetime (used to locate the event on the NECIS page)
    data_types  : subset of ("a", "v") to download per event
    zip_dir     : staging directory for downloaded ZIPs
    out_root    : organized output root (default: cfg.download_dir/events)
    convert_sac : convert miniSEED → SAC via mseed2sac after extraction
    """
    from .utils import extract_zips, organize_events_kma

    zip_dir  = zip_dir  or (browser.cfg.download_dir / "events" / "zips")
    out_root = out_root or (browser.cfg.download_dir / "events")
    zip_dir.mkdir(parents=True, exist_ok=True)

    # Skip data types whose MSEED dir already contains files
    remaining = []
    for dt in data_types:
        candidates = list(out_root.glob(f"{event_id}/*.{dt}/MSEED"))
        if candidates and any(f for f in candidates[0].iterdir() if f.is_file()):
            logger.info("[%s] %s already downloaded — skipping", event_id, dt)
        else:
            remaining.append(dt)
    if not remaining:
        return {}

    info = await _search_event(browser, origin_time)
    if info is None:
        logger.warning("[%s] Event not found on NECIS — skipping", event_id)
        return {}

    necis_id  = info["necis_id"]
    ftp_paths = info["ftp_paths"]

    # Categorize FTP paths by data type suffix
    type_paths: dict[str, list[str]] = {"a": [], "v": [], "r": []}
    for p in ftp_paths:
        name = p.rstrip("/").rsplit("/", 1)[-1]
        if   name.endswith(".a.zip"): type_paths["a"].append(p)
        elif name.endswith(".v.zip"): type_paths["v"].append(p)
        elif name.endswith(".r.zip"): type_paths["r"].append(p)

    session = await _copy_cookies_to_session(browser)
    results: dict[str, list[Path]] = {}

    # Download waveform ZIPs (.a and/or .v)
    for dt in remaining:
        paths = type_paths.get(dt, [])
        if not paths:
            logger.warning("[%s] No FTP path for data type '%s'", event_id, dt)
            continue

        dt_zip_dir = zip_dir / event_id / dt
        dt_zip_dir.mkdir(parents=True, exist_ok=True)

        downloaded = []
        for ftp_path in paths:
            url  = f"{FTP_BASE}/{ftp_path.lstrip('/')}"
            saved = _download_file(session, url, dt_zip_dir)
            if saved:
                downloaded.append(saved)

        if not downloaded:
            logger.warning("[%s] Download failed for data type '%s'", event_id, dt)
            continue

        organized = organize_events_kma(
            zip_dir=dt_zip_dir,
            event_utc_str=event_id,
            necis_id=necis_id,
            out_root=out_root,
            data_type=dt,
            delete_zip=True,
            convert_sac=convert_sac,
        )
        results[dt] = organized
        logger.info("[%s] %s — organized %d file(s)", event_id, dt, len(organized))

    # Download RESP ZIP (.r) — always, regardless of data_types filter
    resp_paths = type_paths.get("r", [])
    if resp_paths:
        resp_zip_dir = zip_dir / event_id / "r"
        resp_zip_dir.mkdir(parents=True, exist_ok=True)
        resp_out = out_root / "RESP" / f"{necis_id}.r"
        resp_out.mkdir(parents=True, exist_ok=True)

        already_extracted = any(resp_out.iterdir()) if resp_out.exists() else False
        if already_extracted:
            logger.info("[%s] RESP already extracted — skipping", event_id)
        else:
            for ftp_path in resp_paths:
                url = f"{FTP_BASE}/{ftp_path.lstrip('/')}"
                saved = _download_file(session, url, resp_zip_dir)
                if saved:
                    extract_zips(resp_zip_dir, resp_out, delete_zip=True)
                    logger.info("[%s] RESP → %s", event_id, resp_out)

    return results


# ---------------------------------------------------------------------------
# Batch runner
# ---------------------------------------------------------------------------

async def run_events(
    config: NECISConfig,
    catalog_path: Path,
    stations: list[str],
    components: Optional[list[str]] = None,
    pre_sec: int = 30,
    post_sec: int = 120,
    min_magnitude: float = 0.0,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    data_types: Sequence[str] = ("a", "v"),
    convert_sac: bool = True,
    tz_offset_hours: int = 9,
    out_root: Optional[Path] = None,
) -> None:
    """Download event waveforms for all catalog events that pass the filters.

    Parameters
    ----------
    catalog_path    : path to CSV (Jangsung or KMA format)
    stations        : station codes (accepted for API compatibility; NECIS event
                      ZIPs contain all available KS stations)
    components      : component letters (accepted for API compatibility)
    pre_sec / post_sec : accepted for API compatibility; not used — NECIS event
                      ZIPs use NECIS default time windows
    data_types      : subset of ("a", "v") to download per event
    convert_sac     : convert miniSEED → SAC after extraction
    tz_offset_hours : hours to subtract from catalog times to get UTC (9 for KST)
    out_root        : organized output root (default: cfg.download_dir/events)
    """
    catalog  = load_catalog(catalog_path, tz_offset_hours=tz_offset_hours)
    out_root = out_root or (config.download_dir / "events")

    if min_magnitude > 0 and "magnitude" in catalog.columns:
        catalog = catalog[catalog["magnitude"] >= min_magnitude]
    if start_date:
        catalog = catalog[catalog["origin_time"] >= pd.Timestamp(start_date)]
    if end_date:
        catalog = catalog[catalog["origin_time"] <= pd.Timestamp(end_date)]

    logger.info(
        "Events to download: %d | types: %s | SAC: %s",
        len(catalog), list(data_types), convert_sac,
    )
    if stations:
        logger.info(
            "Note: --stations/%d and --pre/--post are not used for event download "
            "(NECIS pre-packages all KS stations with a fixed time window).",
            len(stations),
        )

    async with NECISBrowser(config) as browser:
        for _, row in catalog.iterrows():
            ot = row["origin_time"].to_pydatetime()
            await download_event(
                browser,
                event_id=str(row["event_id"]),
                origin_time=ot,
                data_types=list(data_types),
                out_root=out_root,
                convert_sac=convert_sac,
            )
