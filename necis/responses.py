"""NECIS instrument-response (RESP) downloader.

Companion to the waveform downloaders. NECIS exposes per-station SEED RESP files via
a direct POST to ``/necis-dbf/usernl/ob/observatoryListEarthDown.do?netCode=...&staCode=...``
(no async queue — the response is the zipped RESP files inline). Discovered by
inspecting `fn_downResponse(netCode, staCode)` on
[/user/ob/earthquakeObservatoryListPage.do?fromFlag=start](
    https://necis.kma.go.kr/necis-dbf/user/ob/earthquakeObservatoryListPage.do?fromFlag=start).

The ZIP is named ``RESP_<NET>_<STA>_<YYYYMMDD>.zip`` and contains one SEED RESP file
per active channel plus a ``respList_<id>_<date>.txt`` index. The RESP files have the
standard ``RESP.<NET>.<STA>..<CHAN>`` filename convention and can be loaded directly
by `obspy.read_inventory()` (which detects SEED RESP automatically).

Public API:

    fetch_resp_zip(network, station, *, cookies, out_dir, force=False) -> Path
        POST to NECIS, save the zipped RESP to disk. Skips if file exists unless
        ``force=True``. Returns the saved-zip path.

    extract_resp_files(zip_path, dest_dir) -> list[Path]
        Unzip the RESP files into ``dest_dir``. Returns the list of written paths
        (excludes the respList index file).

    fetch_resp_for_stations(stations, network="KS", out_dir=..., extract=True) -> dict
        Batch fetch: opens an authenticated NECIS session, hits the endpoint per
        station, optionally extracts. Returns ``{station_code: list_of_resp_paths}``.

Authentication: the NECIS session cookies must be a valid logged-in session. Use the
existing ``NECISBrowser`` to log in, then pass ``b._ctx`` cookies to
``fetch_resp_zip``. The convenience helper ``fetch_resp_for_stations`` wraps all of
that.
"""
from __future__ import annotations

import asyncio
import os
import zipfile
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import requests

from .browser import NECISBrowser
from .config import NECISConfig


# The page where fn_downResponse is defined; needed as Referer in the request header
RESP_REFERER = "https://necis.kma.go.kr/necis-dbf/user/ob/earthquakeObservatoryListPage.do?fromFlag=start"
RESP_URL = "https://necis.kma.go.kr/necis-dbf/usernl/ob/observatoryListEarthDown.do"


def fetch_resp_zip(network: str, station: str, *, cookies: Iterable[dict],
                   out_dir: str, force: bool = False) -> Optional[Path]:
    """POST to NECIS for the per-station RESP zip. Returns the saved-zip path, or
    ``None`` if the response wasn't a ZIP (e.g. station unknown to NECIS).

    `cookies` must be a list of dicts in Playwright's format
    (``[{'name': ..., 'value': ..., 'domain': ..., ...}, ...]``)."""
    os.makedirs(out_dir, exist_ok=True)
    out_zip = Path(out_dir) / f"RESP_{network}_{station}.zip"
    if out_zip.exists() and not force:
        return out_zip
    s = requests.Session()
    for c in cookies:
        s.cookies.set(c["name"], c["value"], domain=c.get("domain"))
    r = s.post(f"{RESP_URL}?netCode={network}&staCode={station}",
               headers={"Referer": RESP_REFERER}, timeout=30)
    if r.status_code != 200 or not r.content or r.content[:2] != b"PK":
        # NECIS may return an empty 200 (no RESP available) — body is not a ZIP
        return None
    out_zip.write_bytes(r.content)
    return out_zip


def extract_resp_files(zip_path: Path, dest_dir: str) -> List[Path]:
    """Unzip the NECIS RESP zip into ``dest_dir`` (creating it if needed). Skips the
    ``respList_*.txt`` index file. Returns the list of written RESP file paths."""
    os.makedirs(dest_dir, exist_ok=True)
    written: List[Path] = []
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            name = os.path.basename(info.filename)
            if not name.startswith("RESP."):
                continue
            target = Path(dest_dir) / name
            with zf.open(info) as src, open(target, "wb") as dst:
                dst.write(src.read())
            written.append(target)
    return written


async def _login_get_cookies(cfg: Optional[NECISConfig] = None) -> List[dict]:
    """Spin up NECISBrowser, log in, return the session cookies. The browser closes
    immediately after — the cookies remain valid for ~30 min for stateless requests."""
    cfg = cfg or NECISConfig.from_env()
    async with NECISBrowser(cfg) as b:
        # touch the observatory page so the session has visited it (some endpoints
        # check the Referer chain)
        await b.page.goto(RESP_REFERER, wait_until="load", timeout=15000)
        await b.page.wait_for_timeout(1500)
        return await b._ctx.cookies()


def fetch_resp_for_stations(stations: Iterable[str], network: str = "KS",
                             out_dir: str = "./responses",
                             *, extract: bool = True,
                             force: bool = False) -> Dict[str, List[Path]]:
    """Batch fetch RESP zips for every station in `stations` and (by default)
    extract their RESP files into ``<out_dir>/extracted/``.

    Returns ``{station: list_of_extracted_resp_paths}``. Stations that NECIS doesn't
    serve a RESP for are mapped to an empty list."""
    cookies = asyncio.run(_login_get_cookies())
    zips_dir = Path(out_dir) / "zips"
    resp_dir = Path(out_dir) / "extracted"

    out: Dict[str, List[Path]] = {}
    for sta in stations:
        z = fetch_resp_zip(network, sta, cookies=cookies, out_dir=str(zips_dir),
                           force=force)
        if z is None:
            print(f"  [skip] {network}.{sta} — NECIS did not serve a RESP zip")
            out[sta] = []
            continue
        extracted = extract_resp_files(z, str(resp_dir)) if extract else []
        out[sta] = extracted
        print(f"  [ok]   {network}.{sta} → {z.name}  "
              f"({len(extracted)} RESP files extracted)" if extract
              else f"  [ok]   {network}.{sta} → {z.name}")
    return out
