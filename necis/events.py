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
import math
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Sequence

import pandas as pd

from .browser import NECISBrowser
from .config import NECISConfig
from .fetch_downloads import fetch_ready_downloads, _copy_cookies_to_session

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

def _parse_fn_file_view(onclick: str) -> tuple[Optional[str], Optional[datetime], Optional[str]]:
    """Extract (necis_id, utc_datetime, data_type) from fn_file_view onclick.

    Pattern: fn_file_view('2023002939', '2023-05-26 20:39:54', lat, lon, 'a')
    The datetime is KST — converted to UTC before returning.
    data_type is the last argument: 'a' (acceleration) or 'v' (velocity).
    """
    m = re.search(
        r"fn_file_view\(\s*'([^']+)'\s*,\s*'(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})'"
        r".*?,\s*'([av])'",
        onclick,
        re.DOTALL,
    )
    if not m:
        return None, None, None
    try:
        kst_dt = datetime.strptime(m.group(2), "%Y-%m-%d %H:%M:%S")
        utc_dt = kst_dt - _KST_OFFSET
    except ValueError:
        return None, None, None
    return m.group(1).strip(), utc_dt, m.group(3)


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

    view_attrs = [a for a in onclick_texts if "fn_file_view" in a]

    if not view_attrs:
        logger.warning("No events found on NECIS page for UTC window %s–%s", start_str, end_str)
        return None

    # Pair fn_file_download + fn_file_view by sequential position — they appear as
    # adjacent pairs in both old (wf/event/...) and new (EVENT/...) page formats.
    # The type ('a' or 'v') comes from the last arg of fn_file_view, NOT from the
    # file path, so this works regardless of the path format used by the server.
    all_downloads: dict[str, dict[str, dict]] = {}
    i = 0
    while i < len(onclick_texts):
        oc = onclick_texts[i]
        if "fn_file_download" in oc and i + 1 < len(onclick_texts) and "fn_file_view" in onclick_texts[i + 1]:
            file_list, req_size = _parse_fn_file_download(oc)
            necis_id_v, _, dt = _parse_fn_file_view(onclick_texts[i + 1])
            if file_list and necis_id_v and dt:
                all_downloads.setdefault(necis_id_v, {})[dt] = {
                    "file_list": file_list, "req_size": req_size,
                }
            i += 2
            continue
        i += 1

    # Find the row whose UTC time is closest to origin_time
    best: Optional[dict] = None
    best_diff = float("inf")

    for va in view_attrs:
        necis_id, row_utc, _ = _parse_fn_file_view(va)
        if necis_id is None or row_utc is None:
            continue
        diff = abs((row_utc - origin_time).total_seconds())
        if diff < best_diff:
            best_diff = diff
            best = {
                "necis_id": necis_id,
                "row_utc": row_utc,
                "type_downloads": all_downloads.get(necis_id, {}),
            }

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

async def _submit_via_checkbox(
    browser: NECISBrowser,
    necis_id: str,
    event_id: str,
) -> Optional[datetime]:
    """Trigger a 'part' download for a single event via the page checkbox.

    Finds the table row whose onclick contains necis_id, checks its checkbox,
    calls fn_calc_download_size('part'), then clicks the modal confirm button.
    Returns the submitted_after timestamp for fetch_ready_downloads, or None
    if the sizecheck modal did not show a confirm button (result != 'Y').
    """
    chk_id: Optional[str] = await browser.page.evaluate(
        """([nid]) => {
            var els = document.querySelectorAll('[onclick]');
            for (var i = 0; i < els.length; i++) {
                var oc = els[i].getAttribute('onclick') || '';
                if (oc.includes(nid)) {
                    var tr = els[i].closest('tr');
                    if (tr) {
                        var chk = tr.querySelector('input[type=checkbox][name=chk]');
                        return chk ? chk.id : null;
                    }
                }
            }
            return null;
        }""",
        [necis_id],
    )
    if not chk_id:
        logger.error("[%s] Cannot find checkbox for NECIS ID %s", event_id, necis_id)
        return None

    await browser.page.check(f"#{chk_id}")
    await asyncio.sleep(1)

    async def _dismiss(d: object) -> None:
        logger.info("[%s] dialog: %s", event_id, getattr(d, "message", "")[:80])
        await d.accept()  # type: ignore[attr-defined]

    browser.page.on("dialog", _dismiss)

    await browser.page.evaluate("() => { fn_calc_download_size('part'); }")
    await asyncio.sleep(3)

    modal_btn = await browser.page.query_selector("#fileMgrModal a.btn_red")
    if not modal_btn:
        logger.warning("[%s] No confirm button in modal — sizecheck returned N", event_id)
        return None

    submitted_after = datetime.now()
    await modal_btn.click()
    await asyncio.sleep(2)
    return submitted_after


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

    Two code paths depending on the NECIS file path format:

    Old-format (wf/event/...):  Pre-2022 events where the FTP files already exist
      as individual ZIPs.  Both a and v are bundled in one queue request via the
      page's row checkbox + fn_calc_download_size('part').  Files are routed to
      type-specific directories by channel band (HG/BG/LG → .a/, HH/BH/LH → .v/).

    New-format (EVENT/...):  2022+ events using the queue packaging API.  One
      Python POST request per data type; response arrives via the history API.

    Parameters
    ----------
    event_id    : UTC YYYYMMDDHHmmss string (outer directory name)
    origin_time : UTC datetime (used to locate the event on the NECIS page)
    data_types  : subset of ("a", "v") to download per event
    zip_dir     : staging directory for downloaded ZIPs
    out_root    : organized output root (default: cfg.download_dir/events)
    convert_sac : convert miniSEED → SAC via mseed2sac after extraction
    """
    from .utils import organize_events_kma, organize_old_event_by_channel

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

    # Search once to find the event and detect its format
    info = await _search_event(browser, origin_time)
    if info is None:
        logger.warning("[%s] Event not found on NECIS — skipping", event_id)
        return {}

    necis_id = info["necis_id"]
    all_file_lists = [d.get("file_list", "") for d in info["type_downloads"].values()]
    is_old_format = bool(all_file_lists) and all(fl.startswith("wf/") for fl in all_file_lists)

    if is_old_format:
        # Old-format events (wf/event/...) reference pre-packaged FTP archives that no
        # longer exist on the NECIS server — download attempts produce status='E' (server
        # error) regardless of request method.  Skip and log so the caller can see which
        # events were affected.  If NECIS restores these archives in the future, remove
        # this block and use _submit_via_checkbox() + organize_old_event_by_channel().
        logger.warning(
            "[%s] NECIS event %s uses old-format paths (wf/event/...) that are no longer "
            "accessible on the NECIS FTP server — skipping.  "
            "Contact KMA/NECIS support if you need these waveforms.",
            event_id, necis_id,
        )
        return {}

    else:
        # --- New-format: Python POST per type ---
        ajax_headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        sc_url = browser.cfg.base_url + (
            "/necis-dbf/user/fileDownload/sizecheck/part/eventwavefilesNewAjax.do"
        )

        for dt in remaining:
            # Re-search per type because fetch_ready_downloads navigates away
            info = await _search_event(browser, origin_time)
            if info is None:
                logger.warning("[%s] Event not found on NECIS — skipping %s", event_id, dt)
                continue

            necis_id = info["necis_id"]
            dl = info["type_downloads"].get(dt)
            if dl is None:
                logger.warning("[%s] No fn_file_download for type '%s'", event_id, dt)
                continue

            session = await _copy_cookies_to_session(browser)
            fdp = await browser.page.evaluate(
                "() => typeof fileDownloadParams !== 'undefined' ? fileDownloadParams : ''"
            )
            dl_url = browser.cfg.base_url + (
                f"/necis-dbf/user/fileDownload/download/part/eventwavefilesNewAjax.do?{fdp}"
            )
            req_size_mb = str(math.ceil(int(dl["req_size"]) / 1024 / 1024))
            sc_params = {
                "fileList": dl["file_list"], "reqSize": req_size_mb,
                "fileCnt": "1", "chkEarth": "0", "evtFileFormat": "mseed",
            }

            try:
                sc_resp = session.post(sc_url, data=sc_params, headers=ajax_headers, timeout=30)
                sc = sc_resp.json()
            except Exception as e:
                logger.error("[%s] Sizecheck failed for '%s': %s", event_id, dt, e)
                continue

            if sc.get("result") != "Y":
                logger.warning("[%s] Sizecheck denied for '%s': %s",
                               event_id, dt, sc.get("msg", ""))
                continue

            dl_params = {
                "fileList": dl["file_list"], "reqSize": sc.get("reqSize", req_size_mb),
                "fileCnt": "1", "chkEarth": "0",
            }
            submitted_after = datetime.now()
            try:
                dl_resp = session.post(dl_url, data=dl_params, headers=ajax_headers, timeout=30)
                dl_data = dl_resp.json()
            except Exception as e:
                logger.error("[%s] Download request failed for '%s': %s", event_id, dt, e)
                continue

            if dl_data.get("result") != "Y":
                logger.warning("[%s] Download denied for '%s': %s",
                               event_id, dt, dl_data.get("msg", ""))
                continue
            logger.info("[%s] %s queued: %s", event_id, dt, dl_data.get("msg", "")[:80])

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
