"""
Step 2: Fetch prepared zip files from the NECIS download history.

After submitting a download request (step 1 / request_day), NECIS prepares
the data server-side.  This module polls the history JSON API and downloads
each completed file via requests (plain HTTP GET with session cookies).

Key insight: prepared files appear as plain HTTP URLs on the history page
(http://ftp.necis.kma.go.kr:8080/...).  These do NOT fire Playwright download
events.  The correct approach is to copy Playwright session cookies into a
requests.Session and stream-GET each URL directly.

History JSON API:
  POST /necis-dbf/api/requestFilesHisAjax.do
  Returns: {"resultList": [{status, ftpUrl, downloadPath, fileGbn, ...}, ...]}
    status "C" = complete (다운로드가능)
    status "P" = processing (처리중)
    full URL = ftpUrl + downloadPath
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests

from .browser import NECISBrowser
from .config import NECISConfig

logger = logging.getLogger(__name__)

HISTORY_URL  = "/necis-dbf/api/formQueryRequestFilesHis.do"
HISTORY_AJAX = "/necis-dbf/api/requestFilesHisAjax.do"

STATUS_COMPLETE   = "C"
STATUS_PROCESSING = "P"


async def _copy_cookies_to_session(browser: NECISBrowser) -> requests.Session:
    """Copy all Playwright context cookies into a requests.Session."""
    pw_cookies = await browser._ctx.cookies()
    session = requests.Session()
    for c in pw_cookies:
        session.cookies.set(c["name"], c["value"], domain=c.get("domain", ""))
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Referer": browser.cfg.base_url + HISTORY_URL,
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/javascript, */*; q=0.01",
    })
    return session


def _fetch_history_json(session: requests.Session, base_url: str) -> list[dict]:
    """POST to requestFilesHisAjax.do and return resultList."""
    url = base_url + HISTORY_AJAX
    resp = session.post(url, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return data.get("resultList", [])


async def _poll_for_ready(
    browser: NECISBrowser,
    poll_interval: int = 30,
    max_wait: int = 600,
    submitted_after: Optional[datetime] = None,
) -> list[dict]:
    """Navigate to history page then poll the JSON API until files are ready.

    Returns a list of records with status == "C" (and reqDt >= submitted_after
    if provided).  Requires at least 2 polls before declaring "nothing to wait
    for" so the server has time to register the newly submitted job.
    """
    await browser.page.goto(
        browser.cfg.base_url + HISTORY_URL,
        wait_until="load",
        timeout=browser.cfg.timeout_ms,
    )
    await asyncio.sleep(2)

    session = await _copy_cookies_to_session(browser)
    deadline = time.time() + max_wait
    cutoff = submitted_after.strftime("%Y-%m-%d %H:%M:%S") if submitted_after else None
    attempt = 0

    while True:
        attempt += 1
        try:
            records = _fetch_history_json(session, browser.cfg.base_url)
        except Exception as e:
            logger.warning("[poll #%d] History API error: %s", attempt, e)
            records = []

        # Filter to only records submitted after the cutoff time
        if cutoff:
            records = [r for r in records if (r.get("reqDt") or "") >= cutoff]

        ready      = [r for r in records if r.get("status") == STATUS_COMPLETE
                      and r.get("downloadPath")]
        processing = [r for r in records if r.get("status") == STATUS_PROCESSING]

        logger.info(
            "[poll #%d] ready=%d  processing=%d  (cutoff: %s)",
            attempt, len(ready), len(processing), cutoff or "none",
        )

        if ready:
            return ready

        # Wait at least 2 polls before giving up: on the first poll the server
        # may not have registered the newly submitted job yet.
        if not processing and attempt >= 2:
            logger.info("No jobs in progress after %d polls — nothing to wait for.", attempt)
            return []

        remaining = deadline - time.time()
        if remaining <= 0:
            logger.warning("Timed out after %ds waiting for jobs to complete.", max_wait)
            return []

        wait_secs = min(poll_interval, remaining)
        logger.info("Waiting %.0fs before next poll …", wait_secs)
        await asyncio.sleep(wait_secs)


def _filename_from_response(resp: requests.Response, url: str) -> str:
    """Determine filename from Content-Disposition header, else URL tail."""
    cd = resp.headers.get("Content-Disposition", "")
    m = re.search(r'filename[^;=\n]*=(["\']?)([^"\'\n;]+)\1', cd)
    if m:
        return m.group(2).strip()
    return url.rstrip("/").split("/")[-1] or "download.zip"


def _download_file(session: requests.Session, url: str, out_dir: Path,
                   download_timeout: int = 600) -> Optional[Path]:
    """Stream-GET a single URL and save to out_dir.

    Writes to a .part temp file and renames on completion to avoid
    partial files being picked up by concurrent or retry runs.

    download_timeout bounds total wall-clock download time (default 600 s).
    This is needed because requests timeout= only guards the initial
    connection/headers; a stalled streaming body never fires that timeout.
    """
    logger.info("Downloading %s …", url)
    tmp: Optional[Path] = None
    try:
        with session.get(url, stream=True, timeout=60) as resp:
            resp.raise_for_status()
            fname = _filename_from_response(resp, url)
            dest = out_dir / fname
            if dest.exists():
                logger.info("Already exists, skipping: %s", dest)
                return dest
            total = int(resp.headers.get("Content-Length", 0))
            if total:
                logger.info("  File size: %.1f MB", total / 1e6)
            tmp = dest.with_suffix(dest.suffix + ".part")
            downloaded = 0
            deadline = time.time() + download_timeout
            with tmp.open("wb") as f:
                for chunk in resp.iter_content(chunk_size=1 << 20):  # 1 MB chunks
                    if time.time() > deadline:
                        raise TimeoutError(
                            f"Download stalled — no progress after {download_timeout}s")
                    if chunk:
                        downloaded += len(chunk)
                        f.write(chunk)
                        deadline = time.time() + download_timeout  # reset on progress
                        if total and downloaded % (10 << 20) < (1 << 20):
                            logger.info("  %.0f / %.0f MB (%.0f%%)",
                                        downloaded / 1e6, total / 1e6,
                                        100 * downloaded / total)
            tmp.rename(dest)
            logger.info("Saved → %s  (%.1f MB)", dest, dest.stat().st_size / 1e6)
            return dest
    except Exception as e:
        logger.error("Failed to download %s: %s", url, e)
        if tmp and tmp.exists():
            tmp.unlink(missing_ok=True)
        return None


async def fetch_ready_downloads(
    browser: NECISBrowser,
    out_dir: Optional[Path] = None,
    poll_interval: int = 30,
    max_wait: int = 600,
    file_gbn: Optional[str] = None,
    submitted_after: Optional[datetime] = None,
    download_timeout: int = 600,
) -> list[Path]:
    """Poll history API and download all completed files.

    Parameters
    ----------
    browser          : active NECISBrowser session (must be logged in)
    out_dir          : directory to save zip files (default: cfg.download_dir/zips)
    poll_interval    : seconds between polls (default 30)
    max_wait         : maximum total seconds to wait (default 600 = 10 min)
    file_gbn         : filter by job type ("C"=continuous, "E"=events, None=all)
    submitted_after  : if given, only download records with reqDt >= this time;
                       pass datetime.now() captured just before request_day() to
                       avoid downloading old queued jobs from previous sessions

    Returns
    -------
    List of Path objects for successfully saved files.
    """
    out_dir = out_dir or (browser.cfg.download_dir / "zips")
    out_dir.mkdir(parents=True, exist_ok=True)

    ready_records = await _poll_for_ready(
        browser, poll_interval, max_wait, submitted_after=submitted_after
    )

    if file_gbn:
        ready_records = [r for r in ready_records if r.get("fileGbn") == file_gbn]

    if not ready_records:
        logger.info("No ready downloads found.")
        return []

    session = await _copy_cookies_to_session(browser)
    saved = []
    for record in ready_records:
        ftp_url  = (record.get("ftpUrl") or "").rstrip("/")
        dl_path  = record.get("downloadPath") or ""
        if not ftp_url or not dl_path:
            logger.warning("Incomplete record (no ftpUrl or downloadPath): %s", record)
            continue
        # The API returns an internal filesystem path like /data/ftp/mseed/...
        # but the HTTP server serves it under /mseed/... — strip the /data/ftp prefix.
        if dl_path.startswith("/data/ftp/"):
            dl_path = dl_path[len("/data/ftp"):]
        full_url = ftp_url + dl_path
        path = _download_file(session, full_url, out_dir,
                              download_timeout=download_timeout)
        if path:
            saved.append(path)

    logger.info("Total files saved: %d", len(saved))
    return saved


async def run_fetch(
    config: NECISConfig,
    out_dir: Optional[Path] = None,
    poll_interval: int = 30,
    max_wait: int = 600,
) -> list[Path]:
    """Convenience runner: open browser, fetch all ready downloads, close."""
    async with NECISBrowser(config) as browser:
        return await fetch_ready_downloads(
            browser, out_dir, poll_interval, max_wait
        )
