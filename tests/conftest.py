"""Global pytest fixtures ensuring tests never trigger live SMTP emails."""

from pathlib import Path
import sys

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import app.config as config_module
from app.services import mailer as mailer_module


@pytest.fixture(autouse=True)
def isolate_smtp_and_email_logs(monkeypatch: pytest.MonkeyPatch):
    """Ensure every test defaults to safe Mock email delivery without network SMTP calls."""
    monkeypatch.setattr(config_module, "SMTP_USER", "")
    monkeypatch.setattr(config_module, "SMTP_APP_PASSWORD", "")
    monkeypatch.setattr(config_module, "SMTP_DEMO_TO", "")
    mailer_module.clear_email_logs()
    yield
    mailer_module.clear_email_logs()
