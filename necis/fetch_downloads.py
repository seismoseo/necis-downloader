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

Split archives: when NECIS packages more than ~40 MB (e.g. all-stations ~10 GB),
it produces a split ZIP: {name}_part.z01 (data) + {name}_part.zip (central dir).
The history API leaves downloadPath empty for these.  We detect them by browsing
the FTP HTTP directory listing and download both parts; unzip handles reassembly.
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
    if provided).  Includes split-archive records (empty downloadPath) so the
    caller can handle them separately.

    Requires at least 2 polls before declaring "nothing to wait for" so the
    server has time to register the newly submitted job.
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

    consecutive_empty = 0  # successful polls that returned no jobs at all

    while True:
        attempt += 1
        try:
            records = _fetch_history_json(session, browser.cfg.base_url)
            api_ok = True
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 403:
                logger.warning(
                    "[poll #%d] Session expired (403) — re-authenticating …", attempt
                )
                try:
                    await browser._login()
                    await browser.page.goto(
                        browser.cfg.base_url + HISTORY_URL,
                        wait_until="load", timeout=browser.cfg.timeout_ms,
                    )
                    await asyncio.sleep(2)
                    session = await _copy_cookies_to_session(browser)
                    logger.info("[poll #%d] Re-authenticated successfully.", attempt)
                except Exception as login_err:
                    logger.error("[poll #%d] Re-login failed: %s", attempt, login_err)
            else:
                logger.warning(
                    "[poll #%d] History API error: %s — will retry", attempt, e
                )
            records = []
            api_ok = False
        except Exception as e:
            logger.warning("[poll #%d] History API error: %s — will retry", attempt, e)
            records = []
            api_ok = False

        # Filter to only records submitted after the cutoff time
        if cutoff:
            records = [r for r in records if (r.get("reqDt") or "") >= cutoff]

        # All "C" records are ready — including split archives with empty downloadPath
        ready      = [r for r in records if r.get("status") == STATUS_COMPLETE]
        processing = [r for r in records if r.get("status") == STATUS_PROCESSING]

        n_normal = sum(1 for r in ready if r.get("downloadPath"))
        n_split  = len(ready) - n_normal
        logger.info(
            "[poll #%d] ready=%d (normal=%d split=%d)  processing=%d  (cutoff: %s)",
            attempt, len(ready), n_normal, n_split, len(processing), cutoff or "none",
        )

        if ready:
            return ready

        # Only give up when we get consecutive *successful* empty responses — a
        # transient API error must not be treated as "nothing in progress".
        if api_ok and not processing:
            consecutive_empty += 1
        else:
            consecutive_empty = 0  # reset on error or when a job is still running

        if consecutive_empty >= 2:
            logger.info("No jobs in progress after %d successful polls — nothing to wait for.",
                        attempt)
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
                   download_timeout: int = 600,
                   total_timeout: int = 86400) -> Optional[Path]:
    """Stream-GET a single URL and save to out_dir, resuming on connection drops.

    Uses HTTP Range requests so each retry continues from the byte offset
    already written to the .part file rather than restarting from zero.
    Retries indefinitely until total_timeout seconds have elapsed (default 24 h).
    download_timeout resets on each received chunk; raises if no data arrives
    for that many seconds (guards against TCP-keepalive stalls).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp:  Optional[Path] = None
    dest: Optional[Path] = None
    attempt  = 0
    deadline = time.time() + total_timeout

    while time.time() < deadline:
        attempt += 1
        offset = tmp.stat().st_size if (tmp is not None and tmp.exists()) else 0
        req_headers = {"Range": f"bytes={offset}-"} if offset > 0 else {}
        if attempt > 1:
            logger.info("[attempt %d] %s (offset %.1f MB)", attempt, url, offset / 1e6)
        else:
            logger.info("Downloading %s …", url)
        try:
            with session.get(url, stream=True, timeout=60, headers=req_headers) as resp:
                # 416 = our offset is past EOF — .part is corrupt; restart
                if resp.status_code == 416:
                    logger.warning("Range not satisfiable — restarting from zero")
                    if tmp is not None:
                        tmp.unlink(missing_ok=True)
                    offset = 0
                    continue
                resp.raise_for_status()

                if dest is None:
                    fname = _filename_from_response(resp, url)
                    dest = out_dir / fname
                    if dest.exists():
                        logger.info("Already exists, skipping: %s", dest)
                        return dest
                    tmp = dest.with_suffix(dest.suffix + ".part")
                    offset = tmp.stat().st_size if tmp.exists() else 0

                is_resume = resp.status_code == 206
                if offset and not is_resume:
                    logger.info("Server does not support Range; restarting")
                    if tmp is not None:
                        tmp.unlink(missing_ok=True)
                    offset = 0

                cl    = int(resp.headers.get("Content-Length", 0))
                total = (offset + cl) if is_resume else cl
                if attempt == 1 and total:
                    logger.info("  File size: %.1f MB", total / 1e6)
                elif attempt > 1 and total:
                    logger.info("  Resuming %.1f / %.1f MB", offset / 1e6, total / 1e6)

                downloaded = offset
                chunk_deadline = time.time() + download_timeout
                mode = "ab" if (is_resume and offset > 0) else "wb"
                with tmp.open(mode) as f:
                    for chunk in resp.iter_content(chunk_size=1 << 20):
                        if time.time() > chunk_deadline:
                            raise TimeoutError(
                                f"No progress for {download_timeout}s")
                        if chunk:
                            downloaded += len(chunk)
                            f.write(chunk)
                            chunk_deadline = time.time() + download_timeout
                            if total and downloaded % (10 << 20) < (1 << 20):
                                logger.info("  %.0f / %.0f MB (%.0f%%)",
                                            downloaded / 1e6, total / 1e6,
                                            100 * downloaded / total)
                tmp.rename(dest)
                logger.info("Saved → %s  (%.1f MB)", dest, dest.stat().st_size / 1e6)
                return dest

        except requests.exceptions.HTTPError as http_err:
            if http_err.response is not None and 400 <= http_err.response.status_code < 500:
                logger.error("[attempt %d] Permanent HTTP %d for %s — giving up",
                             attempt, http_err.response.status_code, url)
                break
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            logger.warning("[attempt %d] %s — retrying in 30s (%.0fh remaining)",
                           attempt, http_err, remaining / 3600)
            time.sleep(min(30, remaining))
        except Exception as e:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            logger.warning("[attempt %d] %s — retrying in 30s (%.0fh remaining)",
                           attempt, e, remaining / 3600)
            time.sleep(min(30, remaining))

    logger.error("Failed to download %s after %d attempts (total_timeout=%ds)",
                 url, attempt, total_timeout)
    if tmp is not None and tmp.exists():
        tmp.unlink(missing_ok=True)
    return None


def _extract_req_id(record: dict) -> Optional[str]:
    """Try to extract the numeric request ID from a history API record.

    The FTP filename pattern is {reqId}_{userSuffix}_part.zip, where reqId is
    a server-assigned integer (e.g. 419570).  The field name in the API response
    is unknown until we see the raw record; try the most likely names.
    """
    for field in ("reqNo", "reqFileId", "fileId", "requestNo", "reqId",
                  "downLoadReqId", "downReqNo", "reqFileNo"):
        val = record.get(field)
        if val is not None and str(val).strip().isdigit():
            logger.debug("Found req_id=%s via field '%s'", val, field)
            return str(val).strip()
    return None


def _find_split_parts_on_ftp(
    session: requests.Session,
    ftp_base: str,
    req_id: Optional[str] = None,
    submitted_after: Optional[datetime] = None,
) -> list[tuple[str, str]]:
    """Browse the FTP HTTP directory listing to find split archive part pairs.

    NECIS split archives are named {reqId}_{suffix}_part.z01 (data) and
    {reqId}_{suffix}_part.zip (central directory).  Both must be downloaded
    before unzip can reassemble them.  The FTP server is shared by all NECIS
    users, so we filter by req_id (preferred) or submitted_after timestamp.

    Parameters
    ----------
    session         : authenticated requests.Session (with NECIS cookies)
    ftp_base        : FTP HTTP base URL, e.g. "http://ftp.necis.kma.go.kr:8080"
    req_id          : numeric request ID (e.g. "419570") — used as filename prefix
                      to select only our file pair among all users' archives
    submitted_after : fallback filter: skip pairs whose listing timestamp < this

    Returns
    -------
    List of (z01_url, zip_url) tuples — one per split archive pair found.
    """
    listing_url = ftp_base.rstrip("/") + "/mseed/"
    try:
        resp = session.get(listing_url, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        logger.warning("Cannot browse FTP directory %s: %s", listing_url, e)
        return []

    html = resp.text

    if req_id:
        # Precise filter: match only files belonging to this request.
        # Pattern: {reqId}_{anything}_part.zip
        pattern = rf'href="({re.escape(req_id)}_[^"]*_part\.zip)"'
        zip_names = re.findall(pattern, html)
        if not zip_names:
            logger.warning(
                "No file matching req_id=%s found in FTP listing at %s",
                req_id, listing_url,
            )
            return []
        logger.info("req_id=%s → matched %d file(s) in FTP listing", req_id, len(zip_names))
    else:
        # req_id unknown — fall back to timestamp filtering with a clear warning.
        logger.warning(
            "Request ID not found in history record — filtering FTP listing by "
            "submitted_after timestamp only. This may pick up other users' files "
            "if they submitted at the same time. Check log for 'Split archive record "
            "fields' to identify the correct req_id field name."
        )
        zip_names = re.findall(r'href="([^"/?][^"]*_part\.zip)"', html)
        if not zip_names:
            logger.warning("No *_part.zip entries found in FTP listing at %s", listing_url)
            return []

        # Parse modification timestamps for timestamp-based filtering
        file_times: dict[str, datetime] = {}
        for m in re.finditer(
            r'href="([^"]*_part\.zip)"[^>]*>[^\n]*\n[^\n]*'
            r'(\d{2}-\w{3}-\d{4}\s+\d{2}:\d{2}|\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})',
            html,
        ):
            fname, ts_str = m.group(1), m.group(2).strip()
            for fmt in ("%d-%b-%Y %H:%M", "%Y-%m-%d %H:%M"):
                try:
                    file_times[fname] = datetime.strptime(ts_str, fmt)
                    break
                except ValueError:
                    pass

        if submitted_after:
            zip_names = [
                z for z in zip_names
                if z not in file_times or file_times[z] >= submitted_after
            ]

    base = ftp_base.rstrip("/") + "/mseed/"
    pairs = [
        (base + re.sub(r'\.zip$', '.z01', z), base + z)
        for z in zip_names
    ]
    if pairs:
        logger.info("Found %d split archive pair(s) in FTP listing", len(pairs))
    else:
        logger.warning("No split archive pairs matched (req_id=%s, cutoff=%s)",
                       req_id, submitted_after)
    return pairs


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

    Handles both normal single-file ZIPs (downloadPath populated) and split
    archives (downloadPath empty) by browsing the FTP HTTP directory.

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
    List of Path objects for successfully saved files (both .zip and .z01 parts).
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

    # --- Normal records: downloadPath is populated ---
    normal_records = [r for r in ready_records if r.get("downloadPath")]
    for record in normal_records:
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

    # --- Split archive records: downloadPath is empty ---
    split_records = [r for r in ready_records if not r.get("downloadPath")]
    if split_records:
        logger.info(
            "%d split archive record(s) detected — browsing FTP directory for parts",
            len(split_records),
        )
        # Log all fields so we can confirm (or discover) the req_id field name.
        logger.info("Split archive record fields: %s", split_records[0])

        for record in split_records:
            ftp_base = (record.get("ftpUrl") or "").rstrip("/")
            if not ftp_base:
                continue
            req_id = _extract_req_id(record)
            if req_id:
                logger.info("Using req_id=%s to filter FTP listing", req_id)
            pairs = _find_split_parts_on_ftp(
                session, ftp_base, req_id=req_id, submitted_after=submitted_after
            )
            for z01_url, zip_url in pairs:
                z01_path = _download_file(session, z01_url, out_dir,
                                          download_timeout=download_timeout)
                zip_path = _download_file(session, zip_url, out_dir,
                                          download_timeout=download_timeout)
                if z01_path:
                    saved.append(z01_path)
                if zip_path:
                    saved.append(zip_path)

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
