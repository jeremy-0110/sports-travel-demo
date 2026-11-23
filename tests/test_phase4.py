"""Phase 4 tests: Traveler KYC, league-specific tickets, 4-category forms & exception resolution."""

from pathlib import Path
import sys

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import app.config as config_module
import app.database as database_module
from app.database import get_db, init_db
from app.main import app
from app.models import BookingException, BookingVoucher, OrderQuote, PaymentTxn, TripPlan
from app.services import mailer as mailer_module
from app.services import planner as planner_module
from scripts.seed_db import seed_database

_VALID_TRAVELER_KYC = {
    "passport_name": "CHEN, DAMING",
    "passport_number": "312345678",
    "nationality": "Taiwan (TWN)",
    "birth_date": "1995-06-15",
    "passport_expiry": "2032-12-31",
    "contact_phone": "0912-345-678",
    "ticket_email": "demo@example.com",
}


@pytest.fixture()
def isolated_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Provide an isolated SQLite DB, seeded users/agents, and TestClient."""
    db_file = tmp_path / "test_phase4.db"
    test_engine = create_engine(
        f"sqlite:///{db_file.as_posix()}",
        connect_args={"check_same_thread": False},
    )
    testing_session_local = sessionmaker(
        autocommit=False, autoflush=False, bind=test_engine
    )

    monkeypatch.setattr(database_module, "engine", test_engine)
    monkeypatch.setattr(database_module, "SessionLocal", testing_session_local)
    monkeypatch.setattr(config_module, "GEMINI_API_KEY", "")
    init_db(test_engine)

    with testing_session_local() as db:
        seed_database(db)

    def override_get_db():
        db = testing_session_local()
        try:
            yield db
        finally:
            db.close()

    monkeypatch.setattr(
        planner_module,
        "fetch_espn_schedules",
        lambda league_type, start_date, end_date, preference="", meta=None: [],
    )
    monkeypatch.setattr(
        planner_module,
        "search_authoritative_schedules",
        lambda league_type, start_date, end_date, preference="", max_results=4: [],
    )

    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app) as client:
            yield client, testing_session_local
    finally:
        app.dependency_overrides.clear()
        test_engine.dispose()


def _setup_paid_order(
    client: TestClient,
    SessionLocal,
    league_type: str = "NBA",
    price: int = 72000,
) -> tuple[int, int]:
    """Create a trip, confirm, agent-quote, and member-pay with KYC -> returns (trip_id, order_id)."""
    assert (
        client.post(
            "/login",
            data={"email": "demo@example.com", "password": "demo1234"},
            follow_redirects=True,
        ).status_code
        == 200
    )
    trip_res = client.post(
        "/trips",
        data={
            "league_type": league_type,
            "start_date": "2026-12-10",
            "end_date": "2026-12-14",
            "need_flight": "true",
            "need_hotel": "true",
        },
        follow_redirects=True,
    )
    assert trip_res.status_code == 200
    trip_id = int(str(trip_res.url).rstrip("/").split("/")[-1])

    client.post(f"/trips/{trip_id}/confirm", follow_redirects=True)
    with SessionLocal() as db:
        order = (
            db.query(OrderQuote)
            .filter(OrderQuote.trip_id == trip_id, OrderQuote.order_type == "MAIN_TRIP")
            .first()
        )
        assert order is not None
        order_id = order.order_id

    client.post("/switch-role", data={"role": "agent", "agent_id": "AG001"})
    client.post(f"/orders/{order_id}/quote", data={"quoted_price": str(price)})
    client.post("/switch-role", data={"role": "member"})
    client.post(
        f"/orders/{order_id}/pay",
        data={
            "payment_method": "CREDIT_CARD",
            "simulate_outcome": "SUCCESS",
            **_VALID_TRAVELER_KYC,
        },
    )
    with SessionLocal() as db:
        assert db.get(OrderQuote, order_id).order_status == "PAID"
        assert db.get(TripPlan, trip_id).status == "LOCKED"
    return trip_id, order_id


def test_expired_passport_blocks_payment_before_charge(isolated_client):
    """Expired or invalid traveler passport rejects payment and saves no PaymentTxn."""
    client, SessionLocal = isolated_client
    client.post("/login", data={"email": "demo@example.com", "password": "demo1234"})
    trip_res = client.post(
        "/trips",
        data={"league_type": "MLB", "start_date": "2026-08-10", "end_date": "2026-08-14"},
        follow_redirects=True,
    )
    trip_id = int(str(trip_res.url).rstrip("/").split("/")[-1])
    client.post(f"/trips/{trip_id}/confirm")
    with SessionLocal() as db:
        order_id = db.query(OrderQuote).filter(OrderQuote.trip_id == trip_id).first().order_id

    client.post("/switch-role", data={"role": "agent", "agent_id": "AG001"})
    client.post(f"/orders/{order_id}/quote", data={"quoted_price": "55000"})
    client.post("/switch-role", data={"role": "member"})

    bad_expiry = client.post(
        f"/orders/{order_id}/pay",
        data={
            "payment_method": "CREDIT_CARD",
            "simulate_outcome": "SUCCESS",
            **{**_VALID_TRAVELER_KYC, "passport_expiry": "2026-08-01"},
        },
        follow_redirects=True,
    )
    assert bad_expiry.status_code == 200
    assert "護照效期" in bad_expiry.text
    with SessionLocal() as db:
        assert db.get(OrderQuote, order_id).order_status == "QUOTED"
        assert db.query(PaymentTxn).filter(PaymentTxn.order_id == order_id).count() == 0


def test_category_specific_validation_checklist_and_delete_revert(isolated_client):
    """Test strict per-category validation, partial checklist stays PAID, complete -> BOOKED, delete reverts."""
    client, SessionLocal = isolated_client
    _trip_id, order_id = _setup_paid_order(client, SessionLocal, league_type="歐冠", price=89000)

    client.post("/switch-role", data={"role": "agent", "agent_id": "AG001"})

    # 1. Invalid FLIGHT missing PNR / bad IATA rejected
    bad_flight = client.post(
        f"/orders/{order_id}/vouchers",
        data={
            "category": "FLIGHT",
            "airline": "EVA Air",
            "pnr": "12",
            "eticket_number": "695-1234567890",
            "passenger_name": "CHEN, DAMING",
            "origin_airport": "TPE",
            "dest_airport": "MAD",
            "outbound_flight_no": "BR87",
            "outbound_datetime": "2026-12-10 23:00",
        },
        follow_redirects=True,
    )
    assert bad_flight.status_code == 200
    assert "訂位代號 (PNR" in bad_flight.text

    # 2. Invalid HOTEL checkout <= checkin rejected
    bad_hotel = client.post(
        f"/orders/{order_id}/vouchers",
        data={
            "category": "HOTEL",
            "hotel_name": "Hotel Bernabeu",
            "hotel_address": "Madrid Spain",
            "guest_name": "CHEN, DAMING",
            "checkin_date": "2026-12-12",
            "checkout_date": "2026-12-10",
            "room_type": "Twin",
        },
        follow_redirects=True,
    )
    assert bad_hotel.status_code == 200
    assert "飯店退房日期必須晚於入住日期" in bad_hotel.text

    # 3. Valid HOTEL only -> order must remain PAID because 2 MATCH + 1 FLIGHT still missing (1/4)
    ok_hotel = client.post(
        f"/orders/{order_id}/vouchers",
        data={
            "category": "HOTEL",
            "hotel_name": "Melia Castilla Madrid",
            "hotel_address": "Calle del Poeta Joan Maragall 43, Madrid",
            "guest_name": "CHEN, DAMING",
            "checkin_date": "2026-12-10",
            "checkout_date": "2026-12-14",
            "room_type": "Deluxe Twin Room",
            "meal_plan": "含雙人早餐",
        },
        follow_redirects=True,
    )
    assert ok_hotel.status_code == 200
    assert "開票進度 1/4" in ok_hotel.text
    with SessionLocal() as db:
        assert db.get(OrderQuote, order_id).order_status == "PAID"

    # 4. EU/UEFA MATCH missing Block rejected
    bad_uefa = client.post(
        f"/orders/{order_id}/vouchers",
        data={
            "category": "MATCH",
            "league_format": "UEFA_UCL",
            "matchup": "Bayern vs Real Madrid",
            "event_date": "2026-12-11",
            "venue": "Santiago Bernabeu",
            "row": "F",
            "seat": "12",
        },
        follow_redirects=True,
    )
    assert bad_uefa.status_code == 200
    assert "Block" in bad_uefa.text

    # 5. Auto-issue remaining (MATCH in UEFA format + FLIGHT) -> reaches 3/3 and becomes BOOKED
    auto_res = client.post(
        f"/orders/{order_id}/vouchers/auto-issue",
        follow_redirects=True,
    )
    assert auto_res.status_code == 200
    assert "BOOKED" in auto_res.text

    with SessionLocal() as db:
        assert db.get(OrderQuote, order_id).order_status == "BOOKED"
        vouchers = db.query(BookingVoucher).filter(BookingVoucher.order_id == order_id).all()
        assert len(vouchers) == 4
        flight_v_id = next(v.voucher_id for v in vouchers if v.category == "FLIGHT")

    # 6. Verify /orders/{id}/vouchers renders all 3 industry templates & masked passport KYC
    voucher_page = client.get(f"/orders/{order_id}/vouchers")
    assert voucher_page.status_code == 200
    assert "UEFA Mobile Tickets App" in voucher_page.text
    assert "須帶護照正本實名查驗" in voucher_page.text
    assert "312***678" in voucher_page.text
    assert "Melia Castilla Madrid" in voucher_page.text
    assert "4 晚" in voucher_page.text
    assert "PREPAID" in voucher_page.text
    assert "E-TICKET ITINERARY" in voucher_page.text

    # 7. Deleting FLIGHT voucher reverts BOOKED -> PAID
    del_res = client.post(
        f"/orders/{order_id}/vouchers/{flight_v_id}/delete",
        follow_redirects=True,
    )
    assert del_res.status_code == 200
    assert "目前訂單狀態為 PAID" in del_res.text
    with SessionLocal() as db:
        assert db.get(OrderQuote, order_id).order_status == "PAID"


def test_agent_auto_issue_and_manual_voucher_and_agoda_view_permissions(isolated_client):
    """Test auto-issue & manual SPOT voucher creation plus member/agent voucher page access."""
    client, SessionLocal = isolated_client
    _trip_id, order_id = _setup_paid_order(client, SessionLocal, league_type="NBA", price=72000)

    # Member role must be blocked from issuing/deleting vouchers
    assert client.post(f"/orders/{order_id}/vouchers/auto-issue").status_code == 403
    assert (
        client.post(
            f"/orders/{order_id}/vouchers",
            data={"category": "SPOT", "spot_name": "Alcatraz"},
        ).status_code
        == 403
    )

    # Switch to AG001 and one-click auto-issue
    client.post("/switch-role", data={"role": "agent", "agent_id": "AG001"})
    auto_res = client.post(
        f"/orders/{order_id}/vouchers/auto-issue",
        follow_redirects=True,
    )
    assert auto_res.status_code == 200
    assert "BOOKED" in auto_res.text

    with SessionLocal() as db:
        order = db.get(OrderQuote, order_id)
        vouchers = db.query(BookingVoucher).filter(BookingVoucher.order_id == order_id).all()
        assert order.order_status == "BOOKED"
        cats = [v.category for v in vouchers]
        assert "MATCH" in cats
        assert "FLIGHT" in cats
        assert "HOTEL" in cats
        initial_count = len(vouchers)

    assert mailer_module.get_order_email_logs(order_id)[0]["category"] == "VOUCHER_ISSUED"

    # Second auto-issue must not duplicate existing vouchers
    repeat_res = client.post(
        f"/orders/{order_id}/vouchers/auto-issue",
        follow_redirects=True,
    )
    assert repeat_res.status_code == 200
    assert "皆已核發完畢" in repeat_res.text
    with SessionLocal() as db:
        assert (
            db.query(BookingVoucher).filter(BookingVoucher.order_id == order_id).count()
            == initial_count
        )

    # Manual issuance of an additional SPOT voucher
    manual_res = client.post(
        f"/orders/{order_id}/vouchers",
        data={
            "category": "SPOT",
            "spot_name": "金門大橋探索博物館門票",
            "visit_date": "2026-12-12",
            "time_slot": "14:00 入場",
            "location": "San Francisco Pier 15",
            "ticket_type": "VIP 快速通關雙人券",
            "booking_code": "agd-spot-9988",
        },
        follow_redirects=True,
    )
    assert manual_res.status_code == 200
    assert "AGD-SPOT-9988" in manual_res.text

    agent_view = client.get(f"/orders/{order_id}/vouchers")
    assert agent_view.status_code == 200
    assert "Ticketmaster" in agent_view.text
    assert "AGD-SPOT-9988" in agent_view.text

    client.post("/switch-role", data={"role": "member"})
    owner_view = client.get(f"/orders/{order_id}/vouchers")
    assert owner_view.status_code == 200
    assert "金門大橋探索博物館門票" in owner_view.text

    client.post("/logout")
    client.post("/login", data={"email": "alex@example.com", "password": "demo1234"})
    assert client.get(f"/orders/{order_id}/vouchers").status_code == 403


def test_exception_report_and_member_accepts_alternative_then_booked(isolated_client):
    """Agent reports exception -> blocks vouchers -> member accepts ALT -> agent issues vouchers."""
    client, SessionLocal = isolated_client
    _trip_id, order_id = _setup_paid_order(client, SessionLocal, league_type="NBA", price=74000)

    assert (
        client.post(
            f"/orders/{order_id}/exceptions",
            data={"reason": "test", "alternative_proposal": "test"},
        ).status_code
        == 403
    )

    client.post("/switch-role", data={"role": "agent", "agent_id": "AG001"})
    exc_res = client.post(
        f"/orders/{order_id}/exceptions",
        data={
            "reason": "原訂賽事下層看台座位已售罄",
            "alternative_proposal": "可安排同等級 112 區連號座位且免加價",
        },
        follow_redirects=True,
    )
    assert exc_res.status_code == 200
    assert "EXCEPTION" in exc_res.text

    with SessionLocal() as db:
        order = db.get(OrderQuote, order_id)
        exc_obj = (
            db.query(BookingException)
            .filter(BookingException.order_id == order_id)
            .first()
        )
        assert order.order_status == "EXCEPTION"
        assert exc_obj is not None and exc_obj.user_decision == "PENDING"
        exception_id = exc_obj.exception_id

    assert mailer_module.get_order_email_logs(order_id)[0]["category"] == "EXCEPTION_ALERT"

    # Cannot issue vouchers while exception is PENDING
    blocked_issue = client.post(
        f"/orders/{order_id}/vouchers/auto-issue",
        follow_redirects=True,
    )
    assert blocked_issue.status_code == 200
    with SessionLocal() as db:
        assert db.query(BookingVoucher).filter(BookingVoucher.order_id == order_id).count() == 0

    assert (
        client.post(
            f"/orders/{order_id}/exceptions/{exception_id}/decide",
            data={"decision": "ALT"},
        ).status_code
        == 403
    )

    client.post("/switch-role", data={"role": "member"})
    alt_res = client.post(
        f"/orders/{order_id}/exceptions/{exception_id}/decide",
        data={"decision": "ALT"},
        follow_redirects=True,
    )
    assert alt_res.status_code == 200
    assert "已接受專員替代方案" in alt_res.text

    with SessionLocal() as db:
        assert db.get(OrderQuote, order_id).order_status == "PAID"
        assert db.get(BookingException, exception_id).user_decision == "ALT"

    recent_cats = [e["category"] for e in mailer_module.get_order_email_logs(order_id)[:2]]
    assert set(recent_cats) == {"EXCEPTION_ALT_ACCEPTED", "EXCEPTION_USER_DIRECTIVE"}

    client.post("/switch-role", data={"role": "agent", "agent_id": "AG001"})
    client.post(f"/orders/{order_id}/vouchers/auto-issue", follow_redirects=True)
    with SessionLocal() as db:
        assert db.get(OrderQuote, order_id).order_status == "BOOKED"


def test_exception_report_and_member_requests_full_refund(isolated_client):
    """Member selects REFUND on exception -> D4 REFUND txn, order=REFUNDED, trip=CANCELLED."""
    client, SessionLocal = isolated_client
    trip_id, order_id = _setup_paid_order(client, SessionLocal, league_type="MLB", price=60000)

    client.post("/switch-role", data={"role": "agent", "agent_id": "AG001"})
    client.post(
        f"/orders/{order_id}/exceptions",
        data={
            "reason": "原指定航班票價大幅調漲超出報價預算",
            "alternative_proposal": "建議改搭隔日早班機，或可辦理全額退款",
        },
        follow_redirects=True,
    )
    with SessionLocal() as db:
        exc_id = (
            db.query(BookingException)
            .filter(BookingException.order_id == order_id)
            .first()
            .exception_id
        )

    client.post("/switch-role", data={"role": "member"})
    ref_res = client.post(
        f"/orders/{order_id}/exceptions/{exc_id}/decide",
        data={"decision": "REFUND"},
        follow_redirects=True,
    )
    assert ref_res.status_code == 200
    assert "REFUNDED" in ref_res.text
    assert "MOCK-REF-" in ref_res.text

    with SessionLocal() as db:
        order = db.get(OrderQuote, order_id)
        trip = db.get(TripPlan, trip_id)
        refund_txns = (
            db.query(PaymentTxn)
            .filter(PaymentTxn.order_id == order_id, PaymentTxn.txn_type == "REFUND")
            .all()
        )
        assert order.order_status == "REFUNDED"
        assert trip.status == "CANCELLED"
        assert len(refund_txns) == 1
        assert refund_txns[0].amount == 60000

    # Duplicate decision rejected cleanly
    dup_ref = client.post(
        f"/orders/{order_id}/exceptions/{exc_id}/decide",
        data={"decision": "REFUND"},
        follow_redirects=True,
    )
    assert "已完成處理" in dup_ref.text
    with SessionLocal() as db:
        assert (
            db.query(PaymentTxn)
            .filter(PaymentTxn.order_id == order_id, PaymentTxn.txn_type == "REFUND")
            .count()
            == 1
        )
