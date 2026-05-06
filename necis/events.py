"""
Event waveform downloader.

Downloads waveform data for earthquakes listed in a KMA catalog CSV.
The catalog format matches meta/catalog_KMA_*.csv in the SGTL-SKP-workspace:
  event_id, origin_time (or datetime), latitude, longitude, depth, magnitude, ...

Files are saved as  data/necis/events/YYYY/<event_id>/<station>.<channel>.sac
(or whatever format NECIS returns).

ADAPT sections must be filled in after running discover_necis.py.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional, Sequence

import pandas as pd
from obspy import UTCDateTime

from .browser import NECISBrowser
from .config import NECISConfig
from .fetch_downloads import fetch_ready_downloads

logger = logging.getLogger(__name__)

OUTPUT_GLOB = ["*.sac", "*.SAC", "*.mseed", "*.zip"]

# ---------------------------------------------------------------------------
# ADAPT: selectors for the event-waveform download form
# ---------------------------------------------------------------------------

# Event search / selection
SEL_EVENT_DATE  = 'input[name="originDate"]'     # ADAPT — date field on event page
SEL_EVENT_ID    = 'input[name="eventId"]'         # ADAPT — or however events are selected
SEL_EVENT_ROW   = 'table.event-list tbody tr'     # ADAPT — row in an event table

# Station / channel selectors (may be same elements as continuous page)
SEL_STA_INPUT   = 'input[name="station"]'         # ADAPT
SEL_CH_SELECT   = 'select[name="channel"]'        # ADAPT

# Time window around the event (pre/post in seconds)
SEL_PRE_TIME    = 'input[name="preSec"]'          # ADAPT
SEL_POST_TIME   = 'input[name="postSec"]'         # ADAPT

SEL_DOWNLOAD_BTN = 'button:has-text("다운로드")'   # ADAPT


# ---------------------------------------------------------------------------
# Catalog helpers
# ---------------------------------------------------------------------------

def load_catalog(csv_path: Path) -> pd.DataFrame:
    """Load KMA event catalog CSV.

    Expected columns (flexible — other columns are ignored):
      event_id, origin_time (or datetime/time), magnitude, latitude, longitude
    """
    df = pd.read_csv(csv_path, dtype={"event_id": str})
    df["event_id"] = df["event_id"].str.strip()

    # Normalise the time column name
    for col in ("origin_time", "datetime", "time", "origintime"):
        if col in df.columns:
            df["origin_time"] = pd.to_datetime(df[col])
            break
    else:
        raise ValueError(f"No recognised time column in {csv_path}")

    return df


# ---------------------------------------------------------------------------
# Per-event downloader
# ---------------------------------------------------------------------------

async def download_event(
    browser: NECISBrowser,
    event_id: str,
    origin_time: UTCDateTime,
    stations: Sequence[str],
    network: str = "KS",
    channels: Optional[Sequence[str]] = None,
    pre_sec: int = 30,
    post_sec: int = 120,
    out_dir: Optional[Path] = None,
) -> list[Path]:
    """Download waveforms for a single event.

    Returns paths of saved files. Skips if already downloaded.
    """
    channels = list(channels or ["HHZ", "HHN", "HHE"])
    out_dir = out_dir or (
        browser.cfg.download_dir / "events" / str(origin_time.year) / event_id
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    existing = [f for g in OUTPUT_GLOB for f in out_dir.glob(g)]
    if existing:
        logger.info("[%s] Already downloaded (%d file(s)) — skip", event_id, len(existing))
        return existing

    logger.info("[%s] %s — downloading …", event_id, origin_time)
    await browser.goto_events()

    # ── ADAPT: locate the event on the page ─────────────────────────────────
    # Approach A: search by event_id
    try:
        await browser.page.fill(SEL_EVENT_ID, event_id)
        await browser.click_and_wait('button:has-text("검색")')   # "검색" = search; ADAPT
    except Exception:
        # Approach B: search by date and click the matching row in a table
        try:
            date_str = origin_time.strftime("%Y%m%d")
            await browser.fill_date(SEL_EVENT_DATE, date_str)
            await browser.click_and_wait('button:has-text("검색")')   # ADAPT
            # Click the table row whose event_id matches
            rows = await browser.page.query_selector_all(SEL_EVENT_ROW)
            matched = False
            for row in rows:
                text = await row.inner_text()
                if event_id in text:
                    await row.click()
                    matched = True
                    break
            if not matched:
                logger.warning("[%s] Event not found in table", event_id)
                return []
        except Exception as e:
            shot = await browser.screenshot(f"event_{event_id}_search_error")
            logger.error("[%s] Search failed: %s (see %s)", event_id, e, shot)
            return []

    # ── ADAPT: set time window ───────────────────────────────────────────────
    try:
        await browser.page.fill(SEL_PRE_TIME,  str(pre_sec))
        await browser.page.fill(SEL_POST_TIME, str(post_sec))
    except Exception as e:
        logger.warning("[%s] Time window fill failed (%s)", event_id, e)

    # ── ADAPT: select stations ───────────────────────────────────────────────
    # Try the same checkbox pattern used on the continuous page.
    # Update SEL_STA_INPUT / the checkbox name once events.py selectors are confirmed.
    try:
        await browser.page.evaluate(
            "() => document.querySelectorAll('[name=observatoryCheckBox]')"
            ".forEach(cb => { cb.checked = false;"
            "  cb.dispatchEvent(new Event('change', {bubbles:true})); })"
        )
        for sta in stations:
            sel = f'input[name="observatoryCheckBox"][value="{sta}"]'
            el = await browser.page.query_selector(sel)
            if el:
                await browser.page.evaluate(
                    "([s]) => { const cb = document.querySelector(s);"
                    " cb.checked = true;"
                    " cb.dispatchEvent(new Event('change', {bubbles:true})); }",
                    [sel],
                )
            else:
                logger.warning("[%s] Station checkbox not found: %s", event_id, sta)
    except Exception as e:
        logger.warning("[%s] Station selection error: %s", event_id, e)

    # ── ADAPT: trigger download ──────────────────────────────────────────────
    # NECIS event downloads likely use the same async queue as continuous.
    # Click the download button to enqueue the job, then fetch via history API.
    try:
        await browser.page.click(SEL_DOWNLOAD_BTN)
        await asyncio.sleep(1)
        logger.info("[%s] Download request submitted, fetching from history …", event_id)
    except Exception as e:
        shot = await browser.screenshot(f"event_{event_id}_dl_error")
        logger.error("[%s] Download button click failed: %s (see %s)", event_id, e, shot)
        return []

    # Wait for the server to prepare the file and download it.
    # file_gbn="E" filters to event-type jobs only (once confirmed with live data).
    saved = await fetch_ready_downloads(
        browser, out_dir=out_dir, poll_interval=30, max_wait=600, file_gbn=None
    )
    return saved


# ---------------------------------------------------------------------------
# Batch runner
# ---------------------------------------------------------------------------

async def run_events(
    config: NECISConfig,
    catalog_path: Path,
    stations: list[str],
    network: str = "KS",
    channels: Optional[list[str]] = None,
    pre_sec: int = 30,
    post_sec: int = 120,
    min_magnitude: float = 0.0,
    start_date: Optional[str] = None,   # 'YYYY-MM-DD'
    end_date: Optional[str] = None,
):
    """Download event waveforms for all events in the catalog that pass filters."""
    catalog = load_catalog(catalog_path)

    if min_magnitude > 0 and "magnitude" in catalog.columns:
        catalog = catalog[catalog["magnitude"] >= min_magnitude]

    if start_date:
        catalog = catalog[catalog["origin_time"] >= pd.Timestamp(start_date)]
    if end_date:
        catalog = catalog[catalog["origin_time"] <= pd.Timestamp(end_date)]

    logger.info("Events to download: %d", len(catalog))

    async with NECISBrowser(config) as browser:
        for _, row in catalog.iterrows():
            ot = UTCDateTime(row["origin_time"])
            await download_event(
                browser,
                event_id=str(row["event_id"]),
                origin_time=ot,
                stations=stations,
                network=network,
                channels=channels,
                pre_sec=pre_sec,
                post_sec=post_sec,
            )
