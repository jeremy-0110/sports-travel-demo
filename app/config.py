"""Application configuration loaded from environment variables."""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.getenv("DB_PATH", str(BASE_DIR / "sports_travel.db")))
DATABASE_URL = f"sqlite:///{DB_PATH.as_posix()}"
BACKUPS_DIR = BASE_DIR / "backups"

SESSION_SECRET_KEY = os.getenv("SESSION_SECRET_KEY", "sports-travel-demo-dev-secret-key")

SUPPORTED_LEAGUES = ("MLB", "NBA", "歐足五大", "歐冠")
DEFAULT_AGENT_ID = "AG001"

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite").strip() or "gemini-3.5-flash-lite"

_DEFAULT_FALLBACK_MODELS = (
    "gemini-3.5-flash-lite",
    "gemini-3.8-flash",
    "gemini-3.5-flash",
    "gemini-4-hydrogen",
)
_raw_fallbacks = os.getenv("GEMINI_FALLBACK_MODELS", "")
GEMINI_FALLBACK_MODELS: tuple[str, ...] = (
    tuple(m.strip() for m in _raw_fallbacks.split(",") if m.strip())
    if _raw_fallbacks.strip()
    else _DEFAULT_FALLBACK_MODELS
)
try:
    GEMINI_TIMEOUT_MS = max(int(os.getenv("GEMINI_TIMEOUT_MS", "60000")), 1000)
except ValueError:
    GEMINI_TIMEOUT_MS = 60000

LEAGUE_AUTHORITATIVE_DOMAINS: dict[str, tuple[str, ...]] = {
    "MLB": ("mlb.com", "espn.com"),
    "NBA": ("nba.com", "espn.com"),
    "歐足五大": ("premierleague.com", "laliga.com", "espn.com"),
    "歐冠": ("uefa.com", "espn.com"),
}
