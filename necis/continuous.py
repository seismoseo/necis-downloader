"""
Continuous waveform downloader for NECIS.

Confirmed workflow (from live page inspection 2026-05-05):
  1. Go to /necis-dbf/user/earthquake/continuouswave.do
  2. Set date fields (observeDate / observeDateSecond, format YYYY-MM-DD)
  3. Optionally deselect stations via input[name="observatoryCheckBox"]
     (all 404 stations are pre-checked in the DOM by default)
  4. Check desired component checkboxes: checkbox_X(E), checkbox_Y(N), checkbox_Z(Z)
  5. Click 검색  → fn_calc_download_size('all') triggers a sizecheck Ajax call
  6. Click 전체다운로드 → size-confirmation modal appears
  7. Click "다운로드 요청" in the modal → fn_download_request('all', size)
     This submits an async download job.  Pick up the prepared file from
     Menu → 자료파일다운로드 (/api/formQueryRequestFilesHis.do)

Note: "다운로드 요청" = "Download Request" (queued, not immediate).
The server prepares a zip and notifies when ready.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, timedelta
from pathlib import Path
from typing import Sequence, Optional

from .browser import NECISBrowser
from .config import NECISConfig

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Confirmed selectors
# ---------------------------------------------------------------------------

SEL_DATE_START   = 'input[name="observeDate"]'
SEL_DATE_END     = 'input[name="observeDateSecond"]'

# Component checkboxes  (id ≠ axis label: checkbox_X → East, checkbox_Y → North)
SEL_COMP_E       = 'input#checkbox_X'   # value="E"
SEL_COMP_N       = 'input#checkbox_Y'   # value="N"
SEL_COMP_Z       = 'input#checkbox_Z'   # value="Z"  (checked by default)

SEL_SEARCH_BTN      = 'a[onclick="fnSelect()"]'
SEL_DOWNLOAD_ALL    = "a[onclick=\"fn_calc_download_size('all')\"]"
SEL_DOWNLOAD_SEL    = "a[onclick=\"fn_calc_download_size('part')\"]"

# Modal confirm button (appears after clicking 전체다운로드/선택다운로드)
SEL_MODAL_CONFIRM   = 'a:has-text("다운로드 요청")'
SEL_MODAL_CLOSE     = 'a[onclick="onModelClose()"]'

# Station checkbox template  (all 404 stations are pre-loaded in the DOM)
def _sta_sel(station_code: str) -> str:
    return f'input[name="observatoryCheckBox"][value="{station_code}"]'


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _fill_date(browser: NECISBrowser, day: date):
    date_str = day.strftime("%Y-%m-%d")
    for sel in (SEL_DATE_START, SEL_DATE_END):
        await browser.page.evaluate(
            "([sel, val]) => { const el = document.querySelector(sel); "
            "el.value = val; el.dispatchEvent(new Event('change')); }",
            [sel, date_str],
        )


async def _set_components(browser: NECISBrowser, components: Sequence[str]):
    """Check the requested component boxes (E/N/Z); uncheck the others."""
    comp_map = {"E": SEL_COMP_E, "N": SEL_COMP_N, "Z": SEL_COMP_Z}
    want = {c.upper() for c in components}
    for comp, sel in comp_map.items():
        try:
            if comp in want:
                await browser.page.check(sel)
            else:
                await browser.page.uncheck(sel)
        except Exception:
            pass


async def _select_stations(browser: NECISBrowser, stations: Sequence[str]):
    """Select only the requested stations for download.

    The page has individual checkboxes (observatoryCheckBox) AND a hidden field
    (observatoryCheckBoxStr) that is the value actually sent to the server.
    We set both: checkboxes for UI consistency, hidden field for reliability.
    """
    try:
        await browser.page.wait_for_selector(
            'input[name="observatoryCheckBox"]', timeout=8_000, state="attached"
        )
    except Exception:
        logger.warning("Station checkboxes not found in DOM — selecting all by default")
        return

    sta_list = list(stations)

    # Set the hidden field that the server actually reads
    updated = await browser.page.evaluate(
        "([stns]) => {"
        "  const el = document.querySelector('input[name=\"observatoryCheckBoxStr\"]');"
        "  if (!el) return false;"
        "  el.value = stns.join(',');"
        "  return true;"
        "}",
        [sta_list],
    )
    if updated:
        logger.info("Set observatoryCheckBoxStr to %d station(s): %s",
                    len(sta_list), ",".join(sta_list))
    else:
        logger.warning("observatoryCheckBoxStr field not found — station filter may not apply")

    # Also update the visible checkboxes for UI consistency
    await browser.page.evaluate(
        "([stns]) => {"
        "  document.querySelectorAll('[name=observatoryCheckBox]')"
        "    .forEach(cb => { cb.checked = stns.includes(cb.value);"
        "      cb.dispatchEvent(new Event('change', {bubbles:true})); });"
        "}",
        [sta_list],
    )


async def _get_sizecheck(browser: NECISBrowser) -> dict:
    """Read the last sizecheck response from the API log."""
    for entry in reversed(browser._api_log):
        if "sizecheck" in entry.get("url", "") and entry.get("event") == "response":
            try:
                return json.loads(entry["body_preview"])
            except Exception:
                pass
    return {}


# ---------------------------------------------------------------------------
# Core downloader
# ---------------------------------------------------------------------------

async def request_day(
    browser: NECISBrowser,
    day: date,
    stations: Optional[Sequence[str]] = None,   # None = all stations
    components: Sequence[str] = ("E", "N", "Z"),
) -> bool:
    """Submit a download request for *day*.

    NECIS queues the job server-side; the prepared zip becomes available at
    Menu → 자료파일다운로드 (/api/formQueryRequestFilesHis.do).

    Returns True if the request was submitted successfully.
    """
    logger.info("[%s] Navigating to continuous page …", day)
    await browser.goto_continuous()
    await _fill_date(browser, day)
    await _set_components(browser, components)

    if stations is not None:
        logger.info("[%s] Selecting %d station(s) …", day, len(stations))
        await _select_stations(browser, stations)
    else:
        logger.info("[%s] All stations selected (default)", day)

    # Search
    await browser.page.click(SEL_SEARCH_BTN)
    await asyncio.sleep(2)

    # Open size-confirmation modal — always use 'all' (= all search results).
    # Station filtering is done via observatoryCheckBoxStr before the search,
    # so 'all' here means all results after filtering, not all 404 stations.
    await browser.page.click(SEL_DOWNLOAD_ALL)
    await asyncio.sleep(2)

    # Read size info
    info = await _get_sizecheck(browser)
    if info:
        req_mb = int(info.get("reqSize", 0)) / 1e6
        n_files = info.get("reqFileCnt", "?")
        limit_mb = int(info.get("downloadOnceLimitSize", 0)) / 1e6
        logger.info("[%s] %s files, %.1f MB (limit %.0f MB)", day, n_files, req_mb, limit_mb)

    # Click "다운로드 요청" in the modal
    try:
        confirm = await browser.page.wait_for_selector(
            SEL_MODAL_CONFIRM, timeout=5_000, state="visible"
        )
        if confirm:
            await confirm.click()
            await asyncio.sleep(1)
            logger.info("[%s] Download request submitted ✓", day)
            return True
    except Exception as e:
        shot = await browser.screenshot(f"continuous_{day}_modal_error")
        logger.error("[%s] Modal confirm failed: %s (see %s)", day, e, shot)
        return False

    return False


# ---------------------------------------------------------------------------
# Batch runner
# ---------------------------------------------------------------------------

async def run_continuous(
    config: NECISConfig,
    start: date,
    end: date,
    stations: Optional[list[str]] = None,
    components: list[str] = None,
):
    """Submit download requests for every day in [start, end].

    After running, visit /api/formQueryRequestFilesHis.do (or
    Menu → 자료파일다운로드) to download the prepared zip files.
    """
    components = components or ["E", "N", "Z"]
    async with NECISBrowser(config) as browser:
        day = start
        while day <= end:
            ok = await request_day(browser, day, stations, components)
            if not ok:
                logger.warning("[%s] Request may have failed", day)
            day += timedelta(days=1)
            await asyncio.sleep(1)
