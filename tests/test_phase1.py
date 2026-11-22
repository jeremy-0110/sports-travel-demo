"""Phase 1 tests: 8-table ERD schema, foreign keys, backup/seed scripts, auth, and role switching."""

from datetime import date
from pathlib import Path
import sqlite3
import sys

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import app.database as database_module
from app.database import get_db, init_db
from app.main import app
from app.models import (
    Agent,
    BookingException,
    BookingVoucher,
    GapItineraryItem,
    OrderQuote,
    PaymentTxn,
    TripPlan,
    User,
)
from scripts.backup_db import backup_database
from scripts.seed_db import seed_database


@pytest.fixture()
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Provide an isolated temporary SQLite database and session factory per test."""
    db_file = tmp_path / "test_sports_travel.db"
    test_engine = create_engine(
        f"sqlite:///{db_file.as_posix()}",
        connect_args={"check_same_thread": False},
    )
    testing_session_local = sessionmaker(
        autocommit=False, autoflush=False, bind=test_engine
    )

    monkeypatch.setattr(database_module, "engine", test_engine)
    monkeypatch.setattr(database_module, "SessionLocal", testing_session_local)
    init_db(test_engine)

    def override_get_db():
        db = testing_session_local()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield {
            "db_file": db_file,
            "engine": test_engine,
            "SessionLocal": testing_session_local,
            "tmp_path": tmp_path,
        }
    finally:
        app.dependency_overrides.clear()
        test_engine.dispose()


def test_all_8_tables_created(isolated_db):
    """Verify all 8 ERD tables exist in SQLite."""
    expected_tables = {
        "user",
        "agent",
        "trip_plan",
        "gap_itinerary_item",
        "order_quote",
        "payment_txn",
        "booking_voucher",
        "booking_exception",
    }

    inspector = inspect(isolated_db["engine"])
    actual_tables = set(inspector.get_table_names())
    assert expected_tables.issubset(actual_tables)

    conn = sqlite3.connect(str(isolated_db["db_file"]))
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        sqlite_tables = {row[0] for row in rows}
    finally:
        conn.close()

    assert expected_tables.issubset(sqlite_tables)


def test_foreign_key_enforcement_and_relationships(isolated_db):
    """Verify foreign key enforcement and full 8-table relational linkage."""
    SessionLocal = isolated_db["SessionLocal"]

    with SessionLocal() as db:
        bad_trip = TripPlan(
            user_id=99999,
            league_type="MLB",
            start_date=date(2026, 7, 10),
            end_date=date(2026, 7, 15),
        )
        db.add(bad_trip)
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

        user = User(
            email="fk_user@example.com",
            password_hash="salt$hash",
            full_name="關聯測試會員",
            phone="0900-000-001",
        )
        agent = Agent(
            agent_id="AG001",
            agent_name="王小明",
            email="ag001@sportstravel.demo",
            is_active=True,
        )
        db.add_all([user, agent])
        db.flush()

        trip = TripPlan(
            user_id=user.user_id,
            league_type="NBA",
            start_date=date(2026, 11, 1),
            end_date=date(2026, 11, 6),
            need_flight=True,
            need_hotel=True,
            status="CONFIRMED",
        )
        db.add(trip)
        db.flush()

        main_order = OrderQuote(
            trip_id=trip.trip_id,
            agent_id=agent.agent_id,
            order_type="MAIN_TRIP",
            quoted_price=85000,
            order_status="PAID",
        )
        addon_order = OrderQuote(
            trip_id=trip.trip_id,
            agent_id=agent.agent_id,
            order_type="ADDON",
            quoted_price=3500,
            order_status="QUOTED",
        )
        db.add_all([main_order, addon_order])
        db.flush()

        gap_item = GapItineraryItem(
            trip_id=trip.trip_id,
            day_number=2,
            time_slot="14:00-17:00",
            activity_name="好萊塢環球影城",
            need_ticket_addon=True,
            addon_order_id=addon_order.order_id,
        )
        txn = PaymentTxn(
            order_id=main_order.order_id,
            txn_type="PAYMENT",
            amount=85000,
            gateway_ref="MOCK-TXN-001",
            status="SUCCESS",
        )
        voucher = BookingVoucher(
            order_id=main_order.order_id,
            category="MATCH",
            booking_code="AGD-NBA-20261102",
            detail_json='{"section": "101", "row": "A"}',
        )
        exception = BookingException(
            order_id=main_order.order_id,
            reason="原場次座位售罄",
            alternative_proposal="改為鄰近區域同等級座位",
            user_decision="PENDING",
        )
        db.add_all([gap_item, txn, voucher, exception])
        db.commit()

        db.refresh(main_order)
        assert main_order.agent.agent_id == "AG001"
        assert main_order.trip_plan.user.email == "fk_user@example.com"
        assert len(main_order.payment_txns) == 1
        assert len(main_order.vouchers) == 1
        assert len(main_order.exceptions) == 1
        assert len(addon_order.addon_gap_items) == 1


def test_seed_db_idempotent(isolated_db):
    """Verify seed_database populates demo data once and stays idempotent on rerun."""
    SessionLocal = isolated_db["SessionLocal"]

    with SessionLocal() as db:
        first = seed_database(db)
        second = seed_database(db)

        assert first == {"users_added": 2, "agents_added": 2, "trips_added": 1}
        assert second == {"users_added": 0, "agents_added": 0, "trips_added": 0}
        assert db.query(User).count() == 2
        assert db.query(Agent).count() == 2
        assert db.query(TripPlan).count() == 1
        assert db.get(Agent, "AG001") is not None
        assert db.query(User).filter(User.email == "demo@example.com").first() is not None


def test_backup_db_creates_file(isolated_db):
    """Verify backup_database creates a valid SQLite backup and skips missing DBs."""
    backups_dir = isolated_db["tmp_path"] / "backups"

    missing_result = backup_database(
        db_path=isolated_db["tmp_path"] / "does_not_exist.db",
        backups_dir=backups_dir,
    )
    assert missing_result is None

    backup_path = backup_database(
        db_path=isolated_db["db_file"],
        backups_dir=backups_dir,
    )
    assert backup_path is not None
    assert backup_path.exists()
    assert backup_path.parent == backups_dir

    conn = sqlite3.connect(str(backup_path))
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    finally:
        conn.close()
    assert "user" in tables
    assert "order_quote" in tables


def test_register_login_logout_flow(isolated_db):
    """Verify member registration, duplicate prevention, logout, and login."""
    with isolated_db["SessionLocal"]() as db:
        seed_database(db)

    with TestClient(app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json() == {"status": "ok"}

        res = client.post(
            "/register",
            data={
                "full_name": "測試新會員",
                "email": "newmember@example.com",
                "phone": "0933-111-222",
                "password": "secret123",
            },
            follow_redirects=True,
        )
        assert res.status_code == 200
        assert "測試新會員" in res.text

        dup = client.post(
            "/register",
            data={
                "full_name": "重複帳號",
                "email": "newmember@example.com",
                "phone": "",
                "password": "secret123",
            },
        )
        assert dup.status_code == 400
        assert "已經註冊過" in dup.text

        logout_res = client.post("/logout", follow_redirects=True)
        assert logout_res.status_code == 200
        assert "會員登入" in logout_res.text

        bad_login = client.post(
            "/login",
            data={"email": "newmember@example.com", "password": "wrong-password"},
        )
        assert bad_login.status_code == 400
        assert "不正確" in bad_login.text

        good_login = client.post(
            "/login",
            data={"email": "newmember@example.com", "password": "secret123"},
            follow_redirects=True,
        )
        assert good_login.status_code == 200
        assert "測試新會員" in good_login.text


def test_role_switch_member_and_agent(isolated_db):
    """Verify switching between member view and agent (AG001) workbench."""
    with isolated_db["SessionLocal"]() as db:
        seed_database(db)

    with TestClient(app) as client:
        home_member = client.get("/")
        assert home_member.status_code == 200
        assert "會員視角" in home_member.text

        to_agent = client.post(
            "/switch-role",
            data={"role": "agent", "agent_id": "AG001"},
            follow_redirects=True,
        )
        assert to_agent.status_code == 200
        assert "訂票專員工作台" in to_agent.text
        assert "AG001" in to_agent.text
        assert "王小明" in to_agent.text

        to_member = client.post(
            "/switch-role",
            data={"role": "member"},
            follow_redirects=True,
        )
        assert to_member.status_code == 200
        assert "會員首頁" in to_member.text
        assert "跨國體育賽事旅遊規劃與真人代訂系統" in to_member.text
        assert "切換至專員視角 (AG001)" in to_member.text
