#!/usr/bin/env python3
"""
NECIS API discovery tool — run this FIRST.

Opens a real browser (not headless), logs in, then keeps the session alive
while you navigate through the waveform download pages.  Every XHR/Fetch
request and response is captured.  When you close the browser (Ctrl+C),
all captured calls are written to  data/necis/api_calls.json.

Usage
-----
    # One-time: show browser so you can click around
    NECIS_HEADLESS=0 python discover_necis.py

    # Inspect the output
    cat data/necis/api_calls.json | python -m json.tool | less

After discovery
---------------
Look through api_calls.json for patterns like:
  - Login endpoint  (POST /api/auth/login or similar)
  - Continuous data endpoint  (GET /api/waveform/continuous?date=...)
  - Event data endpoint  (POST /api/waveform/event or similar)

Then update the selectors in  necis/browser.py  and  necis/continuous.py
/ necis/events.py  accordingly, or — if the API is simple enough — switch
to the requests-based approach (see necis/api_session.py once created).
"""

import asyncio
import logging
import sys

from necis.config import NECISConfig
from necis.browser import NECISBrowser

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)


async def main():
    cfg = NECISConfig.from_env(headless=False, capture_api=True)

    logger.info("Opening browser — navigate through NECIS to capture API calls.")
    logger.info("When done, close the browser or press Ctrl+C.")
    logger.info("Captured calls will be saved to %s/api_calls.json", cfg.download_dir)

    async with NECISBrowser(cfg) as nb:
        # Keep alive: let the user navigate manually
        try:
            # Wait indefinitely (until Ctrl+C or browser close)
            while True:
                # Check if page/browser is still open
                try:
                    await nb.page.title()
                    await asyncio.sleep(2)
                except Exception:
                    logger.info("Browser closed.")
                    break
        except asyncio.CancelledError:
            logger.info("Interrupted.")

    logger.info("Done. Review data/necis/api_calls.json to find API endpoints.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
