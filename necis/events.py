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

NECIS event download workflow (same queue mechanism as continuous waveforms)
----------------------------------------------------------------------------
1. Navigate to earthquake_event_map.do
2. Set date filter (KST!) via page.evaluate (inputs are readonly jQuery datepicker)
3. Click search button (fn_setTableList)
4. Parse onclick attributes to find matched event:
     fn_file_view('NECIS_ID', 'KST_DATETIME', lat, lon, 'a|v')  ← KST times
     fn_file_download('EVENT/.../ID.a.zip|.../ID.r.zip', size)  ← source paths
5. Trigger download via JS: fn_file_download(file_list, req_size)
6. Wait for sizecheck modal → click "다운로드 요청" → NECIS queues the job
7. Poll requestFilesHisAjax.do via fetch_ready_downloads
8. Download result ZIP from FTP, extract, organize

Key observations:
- fn_file_view datetime is KST → must convert to UTC for catalog matching
- search date filter is also KST → use origin_time + 9h for the date string
- fn_file_download invokes sizecheck AJAX then shows a modal; click btn_red to confirm
- fn_download_request alerts after queuing; register dialog handler to auto-dismiss

Catalog formats supported
--------------------------
Jangsung format (Year/Month/Day/Hour/Minute/Second columns, KST):
  Year,Month,Day,Hour,Minute,Second,Latitude,Longitude,Depth,Magnitude

KMA catalog format (datetime column, UTC):
  datetime,event_id,...
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Sequence

import pandas as pd

from .browser import NECISBrowser
from .config import NECISConfig
from .fetch_downloads import fetch_ready_downloads

logger = logging.getLogger(__name__)

# NECIS event waveform search page
EVENTS_PAGE_URL = "/necis-dbf/user/earthquake/earthquake_event_map.do"

# Tolerance for matching catalog events to NECIS page rows (seconds)
TIME_MATCH_TOL_S = 10

_KST_OFFSET = timedelta(hours=9)


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

    Pattern: fn_file_view('2023002939', '2023-05-26 20:39:54', ...)
    The datetime is KST — converted to UTC before returning.
    """
    m = re.search(
        r"fn_file_view\(\s*'([^']+)'\s*,\s*'(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})'",
        onclick,
    )
    if not m:
        return None, None
    try:
        kst_dt = datetime.strptime(m.group(2), "%Y-%m-%d %H:%M:%S")
        utc_dt = kst_dt - _KST_OFFSET
    except ValueError:
        return None, None
    return m.group(1).strip(), utc_dt


def _parse_fn_file_download(onclick: str) -> tuple[Optional[str], Optional[str]]:
    """Extract (file_list, req_size) from fn_file_download onclick.

    Pattern: fn_file_download('EVENT/.../ID.a.zip|.../ID.r.zip', '49799848')
    file_list is the pipe-separated FTP source path string passed to the queue.
    """
    m = re.search(r"fn_file_download\(\s*'([^']+)'\s*,\s*'([^']+)'", onclick)
    if not m:
        return None, None
    return m.group(1).strip(), m.group(2).strip()


# ---------------------------------------------------------------------------
# Event page search
# ---------------------------------------------------------------------------

async def _search_event(
    browser: NECISBrowser,
    origin_time: datetime,
) -> Optional[dict]:
    """Search the NECIS event page and return download info for the matching event.

    Sets readonly date filter fields via JavaScript using the KST date (NECIS
    organizes events by KST date, not UTC).  Submits the search, then parses
    fn_file_view / fn_file_download onclick attributes from the results.

    Returns a dict:
      necis_id      : NECIS internal event ID string, e.g. '2023002939'
      type_downloads: {
          "a": {"file_list": "EVENT/.../ID.a.zip|.../ID.r.zip", "req_size": "49799848"},
          "v": {"file_list": "EVENT/.../ID.v.zip|.../ID.r.zip", "req_size": "24191030"},
      }
    or None if no match found within TIME_MATCH_TOL_S seconds.
    """
    await browser.page.goto(
        browser.cfg.base_url + EVENTS_PAGE_URL,
        wait_until="load",
        timeout=browser.cfg.timeout_ms,
    )
    await asyncio.sleep(2)

    # Use a 2-day UTC window (UTC date → UTC date+1) to reliably capture the event
    # regardless of the KST/UTC boundary.  NECIS shows fn_file_view times in KST but
    # the single-day filter can miss events that straddle the UTC/KST midnight boundary.
    start_str = origin_time.strftime("%Y-%m-%d")
    end_str   = (origin_time + timedelta(days=1)).strftime("%Y-%m-%d")

    try:
        await browser.page.evaluate(
            "([s, e]) => {"
            "  var sf = document.querySelector('input[name=\"startDate\"]');"
            "  var ef = document.querySelector('input[name=\"endDate\"]');"
            "  if (sf) { sf.removeAttribute('readonly'); sf.value = s; }"
            "  if (ef) { ef.removeAttribute('readonly'); ef.value = e; }"
            "}",
            [start_str, end_str],
        )
    except Exception as e:
        logger.error("Could not set date fields for %s–%s: %s", start_str, end_str, e)
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
        logger.error("Search click failed for %s–%s: %s", start_str, end_str, e)
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
        logger.warning("No events found on NECIS page for UTC window %s–%s", start_str, end_str)
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
            # Collect fn_file_download info for this NECIS ID, keyed by type
            type_downloads: dict[str, dict] = {}
            for da in download_attrs:
                file_list, req_size = _parse_fn_file_download(da)
                if file_list and necis_id in file_list:
                    name = file_list.split("|")[0].rstrip("/").rsplit("/", 1)[-1]
                    if   name.endswith(".a.zip"): type_downloads["a"] = {"file_list": file_list, "req_size": req_size}
                    elif name.endswith(".v.zip"): type_downloads["v"] = {"file_list": file_list, "req_size": req_size}
            best = {"necis_id": necis_id, "row_utc": row_utc, "type_downloads": type_downloads}

    if best is None or best_diff > TIME_MATCH_TOL_S:
        logger.warning(
            "[%s] Closest NECIS row differs by %.0fs (tolerance %ds) — skipping",
            origin_time.strftime("%Y-%m-%d %H:%M:%S"), best_diff, TIME_MATCH_TOL_S,
        )
        return None

    logger.info(
        "Matched NECIS event %s (KST %s, Δ%.0fs, types=%s)",
        best["necis_id"],
        (best["row_utc"] + _KST_OFFSET).strftime("%Y-%m-%d %H:%M:%S"),
        best_diff,
        list(best["type_downloads"].keys()),
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

    Uses the same queue workflow as continuous waveforms:
      fn_file_download → sizecheck → modal → "다운로드 요청" → poll → FTP download

    One queue request is submitted per data type; NECIS packages the result
    and the history API returns the download URL when ready.

    Parameters
    ----------
    event_id    : UTC YYYYMMDDHHmmss string (outer directory name)
    origin_time : UTC datetime (used to locate the event on the NECIS page)
    data_types  : subset of ("a", "v") to download per event
    zip_dir     : staging directory for downloaded ZIPs
    out_root    : organized output root (default: cfg.download_dir/events)
    convert_sac : convert miniSEED → SAC via mseed2sac after extraction
    """
    from .utils import organize_events_kma

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

    results: dict[str, list[Path]] = {}

    for dt in remaining:
        # Navigate to event page and find the event
        # (search is repeated per data type since fetch_ready_downloads navigates away)
        info = await _search_event(browser, origin_time)
        if info is None:
            logger.warning("[%s] Event not found on NECIS — skipping %s", event_id, dt)
            continue

        necis_id = info["necis_id"]
        dl = info["type_downloads"].get(dt)
        if dl is None:
            logger.warning("[%s] No fn_file_download found for type '%s'", event_id, dt)
            continue

        # Auto-dismiss the JavaScript alert that fn_download_request fires after queuing
        async def _handle_dialog(dialog: object) -> None:
            await dialog.accept()  # type: ignore[attr-defined]

        browser.page.on("dialog", _handle_dialog)
        submitted_after = datetime.now()

        try:
            # Trigger fn_file_download → sizecheck AJAX → modal appears
            await browser.page.evaluate(
                "([fl, rs]) => { fn_file_download(fl, rs); }",
                [dl["file_list"], dl["req_size"]],
            )
            await asyncio.sleep(2)  # wait for sizecheck AJAX to complete

            # Check for the confirm button; if absent the quota was exceeded.
            # Use JS eval to click — the modal overlay intercepts Playwright pointer events.
            clicked = await browser.page.evaluate("""() => {
                var btn = document.querySelector('#fileMgrModal a.btn_red');
                if (btn) { btn.click(); return 'clicked'; }
                var info = document.querySelector('#fileMgrModal .info');
                return 'denied:' + (info ? info.innerText.trim() : 'quota exceeded');
            }""")

            if not clicked or not clicked.startswith("clicked"):
                logger.warning(
                    "[%s] Sizecheck denied for '%s' — %s", event_id, dt, clicked
                )
                await browser.page.evaluate(
                    "() => { var b = document.querySelector('#fileMgrModal a.btn_gray'); if(b) b.click(); }"
                )
                continue

            await asyncio.sleep(3)  # let the AJAX call finish and alert auto-dismiss

        except Exception as e:
            logger.error("[%s] Download request failed for '%s': %s", event_id, dt, e)
            continue
        finally:
            browser.page.remove_listener("dialog", _handle_dialog)

        # Poll history API and download result ZIPs
        dt_zip_dir = zip_dir / event_id / dt
        dt_zip_dir.mkdir(parents=True, exist_ok=True)

        files = await fetch_ready_downloads(
            browser,
            out_dir=dt_zip_dir,
            poll_interval=30,
            max_wait=600,
            submitted_after=submitted_after,
        )
        if not files:
            logger.warning("[%s] No files downloaded for type '%s'", event_id, dt)
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
            "(NECIS packages all KS stations with a fixed time window).",
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
