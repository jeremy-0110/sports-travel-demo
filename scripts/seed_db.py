"""Idempotent database seeding script for demo presentation."""

from datetime import date
from pathlib import Path
import sys

from sqlalchemy.orm import Session

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.auth import hash_password
from app.database import SessionLocal, init_db
from app.models import Agent, TripPlan, User


def seed_database(db: Session | None = None) -> dict[str, int]:
    """Initialize tables if needed and seed default users, agents, and a sample trip."""
    owns_session = db is None
    if owns_session:
        init_db()
        db = SessionLocal()

    counts = {"users_added": 0, "agents_added": 0, "trips_added": 0}

    try:
        seed_users = [
            {
                "email": "demo@example.com",
                "full_name": "陳大明",
                "phone": "0912-345-678",
                "password": "demo1234",
            },
            {
                "email": "alex@example.com",
                "full_name": "林佳穎",
                "phone": "0922-888-999",
                "password": "demo1234",
            },
        ]

        demo_user: User | None = None
        for item in seed_users:
            existing = db.query(User).filter(User.email == item["email"]).first()
            if existing is None:
                existing = User(
                    email=item["email"],
                    password_hash=hash_password(item["password"]),
                    full_name=item["full_name"],
                    phone=item["phone"],
                )
                db.add(existing)
                counts["users_added"] += 1
            if item["email"] == "demo@example.com":
                demo_user = existing

        seed_agents = [
            {
                "agent_id": "AG001",
                "agent_name": "王小明",
                "email": "ag001@sportstravel.demo",
                "is_active": True,
            },
            {
                "agent_id": "AG002",
                "agent_name": "林美玲",
                "email": "ag002@sportstravel.demo",
                "is_active": True,
            },
        ]

        for item in seed_agents:
            existing_agent = db.get(Agent, item["agent_id"])
            if existing_agent is None:
                db.add(Agent(**item))
                counts["agents_added"] += 1

        db.flush()

        if demo_user is not None:
            existing_trip = (
                db.query(TripPlan)
                .filter(TripPlan.user_id == demo_user.user_id)
                .first()
            )
            if existing_trip is None:
                db.add(
                    TripPlan(
                        user_id=demo_user.user_id,
                        league_type="MLB",
                        start_date=date(2026, 7, 10),
                        end_date=date(2026, 7, 15),
                        need_flight=True,
                        need_hotel=True,
                        llm_draft_json=None,
                        status="DRAFT",
                    )
                )
                counts["trips_added"] += 1

        db.commit()
    finally:
        if owns_session:
            db.close()

    return counts


if __name__ == "__main__":
    result = seed_database()
    print(f"[seed] 初始化與測試資料寫入完成: {result}")
