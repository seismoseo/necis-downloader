"""
Playwright-based browser client for necis.kma.go.kr.

Because NECIS is a JavaScript single-page application, all automation goes
through a real Chromium browser controlled by Playwright.

First-time setup
----------------
Run `discover_necis.py` with NECIS_HEADLESS=0 to see the actual UI and
capture all XHR/Fetch calls → data/necis/api_calls.json.

Every section marked ``# ADAPT:`` may need updating once you've inspected
the real UI. Use screenshots saved to download_dir for debugging.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Optional

from playwright.async_api import (
    async_playwright,
    Browser,
    BrowserContext,
    Download,
    Page,
    Request,
    Response,
    TimeoutError as PWTimeout,
)

from .config import NECISConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Selector candidates (tried in order; first match wins)
# ADAPT: update once you've inspected the real login form.
# ---------------------------------------------------------------------------

_USER_SELECTORS = [
    'input[name="userId"]',
    'input[name="loginId"]',
    'input[name="username"]',
    'input[name="id"]',
    'input[id="userId"]',
    'input[id="id"]',
    'input[type="text"]',       # fallback — first visible text input
]
_PW_SELECTORS = [
    'input[name="userPw"]',
    'input[name="password"]',
    'input[name="pw"]',
    'input[id="userPw"]',
    'input[id="pw"]',
    'input[type="password"]',
]
_SUBMIT_SELECTORS = [
    'button[type="submit"]',
    'input[type="submit"]',
    'button:has-text("로그인")',
    'button:has-text("Login")',
    'a:has-text("로그인")',
]

# ADAPT: menu text or URL paths to the data-download pages.
# "연속" = continuous / "이벤트" or "지진" = event
_MENU_CONTINUOUS = [
    'a:has-text("연속파형")',
    'a:has-text("연속 파형")',
    'a:has-text("연속자료")',
    'li:has-text("연속파형") a',
]
_MENU_EVENTS = [
    'a:has-text("지진파형")',
    'a:has-text("이벤트파형")',
    'a:has-text("이벤트 파형")',
    'a:has-text("지진 파형")',
    'li:has-text("지진파형") a',
]


class NECISBrowser:
    """Context manager that owns a logged-in Playwright browser session.

    Usage::

        async with NECISBrowser(config) as nb:
            await nb.goto_continuous()
            # interact via nb.page
    """

    def __init__(self, config: NECISConfig):
        self.cfg = config
        self._pw = None
        self._browser: Optional[Browser] = None
        self._ctx: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self._api_log: list[dict] = []

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def __aenter__(self) -> "NECISBrowser":
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=self.cfg.headless)
        self._ctx = await self._browser.new_context(
            accept_downloads=True,
            viewport={"width": 1280, "height": 900},
            locale="ko-KR",
        )
        self.page = await self._ctx.new_page()
        if self.cfg.capture_api:
            self.page.on("request", self._on_request)
            self.page.on("response", self._on_response)
        await self._login()
        return self

    async def __aexit__(self, *_):
        if self.cfg.capture_api and self._api_log:
            out = self.cfg.download_dir / "api_calls.json"
            out.write_text(json.dumps(self._api_log, indent=2, ensure_ascii=False))
            logger.info("API calls saved → %s", out)
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()

    # ── API capture ──────────────────────────────────────────────────────────

    def _on_request(self, req: Request):
        if req.resource_type not in ("xhr", "fetch"):
            return
        try:
            self._api_log.append({
                "event": "request",
                "url": req.url,
                "method": req.method,
                "post_data": req.post_data,
                "headers": dict(req.headers),
            })
        except Exception:
            pass

    async def _on_response(self, resp: Response):
        if resp.request.resource_type not in ("xhr", "fetch"):
            return
        try:
            body = await resp.text()
        except Exception:
            body = ""
        self._api_log.append({
            "event": "response",
            "url": resp.url,
            "status": resp.status,
            "body_preview": body[:1000],
        })

    # ── helpers ──────────────────────────────────────────────────────────────

    async def screenshot(self, tag: str) -> Path:
        path = self.cfg.download_dir / f"debug_{tag}_{int(time.time())}.png"
        await self.page.screenshot(path=str(path), full_page=True)
        logger.debug("Screenshot → %s", path)
        return path

    async def wait_idle(self, timeout: Optional[int] = None):
        await self.page.wait_for_load_state(
            "load", timeout=timeout or self.cfg.timeout_ms
        )

    async def _first_visible(self, selectors: list[str], timeout: int = 1_500) -> Optional[str]:
        """Return the first selector from the list that finds a visible element."""
        for sel in selectors:
            try:
                el = await self.page.wait_for_selector(sel, timeout=timeout, state="visible")
                if el:
                    return sel
            except PWTimeout:
                continue
        return None

    # ── authentication ───────────────────────────────────────────────────────

    async def _login(self):
        logger.info("Loading %s …", self.cfg.base_url)
        await self.page.goto(self.cfg.base_url, wait_until="load",
                             timeout=self.cfg.timeout_ms)
        # Give the JS framework a moment to render the login form
        await asyncio.sleep(2)

        user_sel = await self._first_visible(_USER_SELECTORS)
        if not user_sel:
            shot = await self.screenshot("login_no_user_field")
            raise RuntimeError(
                f"Username field not found — inspect {shot}, then update "
                f"_USER_SELECTORS in browser.py"
            )

        pw_sel = await self._first_visible(_PW_SELECTORS)
        if not pw_sel:
            shot = await self.screenshot("login_no_pw_field")
            raise RuntimeError(
                f"Password field not found — inspect {shot}, then update "
                f"_PW_SELECTORS in browser.py"
            )

        await self.page.fill(user_sel, self.cfg.username)
        await self.page.fill(pw_sel, self.cfg.password)

        submit_sel = await self._first_visible(_SUBMIT_SELECTORS)
        if not submit_sel:
            shot = await self.screenshot("login_no_submit")
            raise RuntimeError(
                f"Submit button not found — inspect {shot}, then update "
                f"_SUBMIT_SELECTORS in browser.py"
            )

        async with self.page.expect_navigation(
            wait_until="load", timeout=self.cfg.timeout_ms
        ):
            await self.page.click(submit_sel)

        cur = self.page.url
        if "login" in cur.lower() or "signin" in cur.lower():
            shot = await self.screenshot("login_failed")
            raise RuntimeError(
                f"Login appears to have failed (still at {cur}). "
                f"Check credentials and inspect {shot}"
            )
        logger.info("Logged in → %s", cur)

    # ── navigation ───────────────────────────────────────────────────────────

    async def goto_continuous(self):
        """Navigate to the continuous waveform download section."""
        await self.page.goto(
            f"{self.cfg.base_url}/necis-dbf/user/earthquake/continuouswave.do",
            wait_until="load", timeout=self.cfg.timeout_ms,
        )
        await asyncio.sleep(1)
        logger.info("Continuous page → %s", self.page.url)

    async def goto_events(self):
        """Navigate to the event waveform download section."""
        await self.page.goto(
            f"{self.cfg.base_url}/necis-dbf/user/earthquake/earthquake_event_map.do",
            wait_until="load", timeout=self.cfg.timeout_ms,
        )
        await asyncio.sleep(1)
        logger.info("Event page → %s", self.page.url)

    # ── generic form helpers ─────────────────────────────────────────────────

    async def fill_date(self, selector: str, date_str: str):
        """Fill a date field. date_str should be 'YYYY-MM-DD' or 'YYYYMMDD'."""
        await self.page.fill(selector, date_str)
        # Some date pickers need an 'input' or 'change' event dispatch
        await self.page.dispatch_event(selector, "change")

    async def select_option(self, selector: str, value: str):
        await self.page.select_option(selector, value=value)

    async def click_and_wait(self, selector: str):
        await self.page.click(selector)
        await self.wait_idle()

    async def wait_download(self, trigger_selector: str) -> Download:
        """Click a download button and wait for the file to be delivered."""
        async with self.page.expect_download(timeout=self.cfg.timeout_ms) as dl_info:
            await self.page.click(trigger_selector)
        return await dl_info.value
