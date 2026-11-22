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
