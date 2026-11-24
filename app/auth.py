"""Password hashing and session helpers for member authentication and role switching."""

import hashlib
import hmac
import os

from fastapi import Request
from sqlalchemy.orm import Session

from app.config import DEFAULT_AGENT_ID, SUPPORTED_LEAGUES
from app.models import Agent, SupportTicket, User


def hash_password(password: str) -> str:
    """Hash a plaintext password using scrypt with a random 16-byte salt."""
    salt = os.urandom(16)
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=16384,
        r=8,
        p=1,
    )
    return f"{salt.hex()}${derived.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    """Verify a plaintext password against a stored scrypt hash."""
    if "$" not in stored_hash:
        return False
    salt_hex, hash_hex = stored_hash.split("$", 1)
    try:
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except ValueError:
        return False

    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=16384,
        r=8,
        p=1,
    )
    return hmac.compare_digest(derived, expected)


def get_current_user(request: Request, db: Session) -> User | None:
    """Return the currently logged-in User from session, or None."""
    user_id = request.session.get("user_id")
    if not isinstance(user_id, int):
        return None
    return db.get(User, user_id)


def get_active_role(request: Request) -> str:
    """Return current UI perspective ('member' or 'agent')."""
    role = request.session.get("active_role", "member")
    return "agent" if role == "agent" else "member"


def get_active_agent(request: Request, db: Session) -> Agent | None:
    """Return the active Agent record for agent perspective, defaulting to AG001."""
    agent_id = request.session.get("active_agent_id", DEFAULT_AGENT_ID)
    agent = (
        db.query(Agent)
        .filter(Agent.agent_id == agent_id, Agent.is_active.is_(True))
        .first()
    )
    if agent is None:
        agent = (
            db.query(Agent)
            .filter(Agent.agent_id == DEFAULT_AGENT_ID, Agent.is_active.is_(True))
            .first()
        )
    if agent is None:
        agent = db.query(Agent).filter(Agent.is_active.is_(True)).first()
    return agent


def set_flash(request: Request, category: str, message: str) -> None:
    """Store a one-time flash notification in the session."""
    request.session["flash"] = {"category": category, "message": message}


def pop_flash(request: Request) -> dict[str, str] | None:
    """Pop and return the flash notification from the session, if present."""
    flash = request.session.pop("flash", None)
    if isinstance(flash, dict) and "message" in flash:
        return flash
    return None


def count_active_support_tickets(
    db: Session,
    active_role: str,
    current_user: User | None,
) -> int:
    """Count open or pending-refund support tickets visible to the current role."""
    active_statuses = ("OPEN", "REFUND_REQUESTED")
    if active_role == "agent":
        return (
            db.query(SupportTicket)
            .filter(SupportTicket.status.in_(active_statuses))
            .count()
        )
    if current_user is None:
        return 0
    return (
        db.query(SupportTicket)
        .filter(
            SupportTicket.user_id == current_user.user_id,
            SupportTicket.status.in_(active_statuses),
        )
        .count()
    )


def build_nav_context(request: Request, db: Session) -> dict:
    """Build shared navigation, session, and perspective context for templates."""
    current_user = get_current_user(request, db)
    active_role = get_active_role(request)
    return {
        "current_user": current_user,
        "active_role": active_role,
        "active_agent": get_active_agent(request, db),
        "agents": (
            db.query(Agent)
            .filter(Agent.is_active.is_(True))
            .order_by(Agent.agent_id)
            .all()
        ),
        "open_support_count": count_active_support_tickets(db, active_role, current_user),
        "flash": pop_flash(request),
        "supported_leagues": SUPPORTED_LEAGUES,
    }
