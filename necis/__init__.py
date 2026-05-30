"""
necis — KMA NECIS waveform downloader.

Public API
----------
NECISConfig               configuration (credentials, paths)
NECISBrowser              authenticated Playwright browser session
request_day               submit one continuous-waveform download request
run_continuous            submit requests for a date range (submit-only mode)
fetch_ready_downloads     poll history API and stream-download prepared zips
run_fetch                 convenience runner for fetch step
process_continuous_downloads  extract zips and organize into YYYY/STA/ tree
organize_continuous       organize extracted miniSEED files into date tree
organize_events_kma       extract + organize event ZIPs into kma_waveforms layout
extract_zips              unzip downloaded archives
load_catalog              read event catalog CSV (Jangsung or KMA format)
download_event            download waveforms for a single event
run_events                batch event waveform download
"""

from .config import NECISConfig
from .browser import NECISBrowser
from .continuous import request_day, run_continuous
from .fetch_downloads import fetch_ready_downloads, run_fetch
from .utils import (process_continuous_downloads, organize_continuous,
                    organize_events_kma, extract_zips)
from .events import load_catalog, download_event, run_events

__version__ = "1.0.0"

__all__ = [
    "NECISConfig",
    "NECISBrowser",
    "request_day",
    "run_continuous",
    "fetch_ready_downloads",
    "run_fetch",
    "process_continuous_downloads",
    "organize_continuous",
    "organize_events_kma",
    "extract_zips",
    "load_catalog",
    "download_event",
    "run_events",
]
