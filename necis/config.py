"""
Configuration for the NECIS downloader.

Credentials are read from environment variables (or a .env file).
Copy .env.example → .env and fill in your NECIS_USER / NECIS_PASS.
"""

from __future__ import annotations

import os
import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

BASE_URL = "https://necis.kma.go.kr"

# Default channel priority for KMA KS-network stations.
# ADAPT: adjust if NECIS uses different channel codes or ordering.
DEFAULT_CHANNELS = ["HHZ", "HHN", "HHE", "EHZ", "EHN", "EHE"]


@dataclass
class NECISConfig:
    username: str
    password: str
    base_url: str = BASE_URL
    download_dir: Path = field(default_factory=lambda: Path("data/necis"))
    headless: bool = True
    timeout_ms: int = 60_000
    capture_api: bool = False     # write all XHR/Fetch calls to api_calls.json

    def __post_init__(self):
        self.download_dir = Path(self.download_dir)
        self.download_dir.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_env(cls, **overrides) -> "NECISConfig":
        """Build config from environment variables.

        Required:  NECIS_USER, NECIS_PASS
        Optional:  NECIS_HEADLESS (0 = show browser), NECIS_DOWNLOAD_DIR
        """
        _load_dotenv()
        user = os.environ.get("NECIS_USER") or os.environ.get("NECIS_USERNAME")
        passwd = os.environ.get("NECIS_PASS") or os.environ.get("NECIS_PASSWORD")
        if not user or not passwd:
            raise RuntimeError(
                "Set NECIS_USER and NECIS_PASS (or copy .env.example → .env)"
            )
        kwargs = {
            "headless": os.environ.get("NECIS_HEADLESS", "1") != "0",
            "download_dir": Path(os.environ.get("NECIS_DOWNLOAD_DIR", "data/necis")),
        }
        kwargs.update(overrides)   # overrides win over env defaults
        return cls(username=user, password=passwd, **kwargs)


def _load_dotenv():
    """Minimal .env loader (no extra dependency)."""
    env_path = Path(".env")
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)
