"""Phase 3 tests: agent one-price quotes (DFD 3.0), mock full payments (DFD 4.0), and Gmail SMTP."""

from pathlib import Path
import smtplib
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
from app.models import OrderQuote, PaymentTxn, TripPlan
from app.services import mailer as mailer_module
from app.services import payment as payment_module
from app.services import planner as planner_module
from scripts.seed_db import seed_database


@pytest.fixture()
def isolated_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Provide an isolated SQLite DB, seeded demo users/agents, and TestClient."""
    db_file = tmp_path / "test_phase3.db"
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


def _login_demo_and_confirm_trip(client: TestClient, SessionLocal) -> tuple[int, int]:
    """Log in as demo member, create a trip, confirm it, and return (trip_id, order_id)."""
    login_res = client.post(
        "/login",
        data={"email": "demo@example.com", "password": "demo1234"},
        follow_redirects=True,
    )
    assert login_res.status_code == 200

    trip_res = client.post(
        "/trips",
        data={
            "league_type": "NBA",
            "start_date": "2026-12-10",
            "end_date": "2026-12-14",
            "need_flight": "true",
            "need_hotel": "true",
        },
        follow_redirects=True,
    )
    assert trip_res.status_code == 200
    trip_id = int(str(trip_res.url).rstrip("/").split("/")[-1])

    confirm_res = client.post(f"/trips/{trip_id}/confirm", follow_redirects=True)
    assert confirm_res.status_code == 200
    assert "待報價詢價單" in confirm_res.text

    with SessionLocal() as db:
        order = (
            db.query(OrderQuote)
            .filter(OrderQuote.trip_id == trip_id, OrderQuote.order_type == "MAIN_TRIP")
            .first()
        )
        assert order is not None
        assert order.order_status == "QUOTING"
        return trip_id, order.order_id


def test_agent_quote_permissions_and_validation(isolated_client):
    """Members cannot quote (403); agent AG001 validates price bounds and issues QUOTED order."""
    client, SessionLocal = isolated_client
    trip_id, order_id = _login_demo_and_confirm_trip(client, SessionLocal)

    # Confirming should have recorded a QUOTING_REQUEST notification log
    logs_after_confirm = mailer_module.get_order_email_logs(order_id)
    assert any(entry["category"] == "QUOTING_REQUEST" for entry in logs_after_confirm)

    # Member role must be blocked from quoting
    member_quote = client.post(
        f"/orders/{order_id}/quote",
        data={"quoted_price": "75000"},
    )
    assert member_quote.status_code == 403

    # Switch to agent AG001 and verify workbench shows the QUOTING order
    wb = client.post(
        "/switch-role",
        data={"role": "agent", "agent_id": "AG001"},
        follow_redirects=True,
    )
    assert wb.status_code == 200
    assert f"#{order_id}" in wb.text
    assert "開立一口價報價單" in wb.text

    # Reject invalid / out-of-range prices
    for bad_val in ("abc", "500", "6000000"):
        bad_res = client.post(
            f"/orders/{order_id}/quote",
            data={"quoted_price": bad_val},
            follow_redirects=True,
        )
        assert bad_res.status_code == 200
        assert "合法的一口價整數金額" in bad_res.text

    with SessionLocal() as db:
        ord_unchanged = db.get(OrderQuote, order_id)
        assert ord_unchanged.order_status == "QUOTING"
        assert ord_unchanged.quoted_price is None

    # Submit valid one-price quote
    ok_res = client.post(
        f"/orders/{order_id}/quote",
        data={"quoted_price": "82,500", "quote_note": "保證下層看台雙人連號"},
        follow_redirects=True,
    )
    assert ok_res.status_code == 200
    assert "NT$ 82,500" in ok_res.text
    assert "QUOTED" in ok_res.text

    with SessionLocal() as db:
        ord_quoted = db.get(OrderQuote, order_id)
        trip = db.get(TripPlan, trip_id)
        assert ord_quoted.order_status == "QUOTED"
        assert ord_quoted.quoted_price == 82500
        assert ord_quoted.agent_id == "AG001"
        assert trip.status == "CONFIRMED"

    logs_after_quote = mailer_module.get_order_email_logs(order_id)
    assert logs_after_quote[0]["category"] == "QUOTE_READY"
    assert "82,500" in logs_after_quote[0]["subject"]


def test_member_payment_failed_then_success_and_idempotency(isolated_client):
    """Test ownership checks, FAILED->retry->SUCCESS mock payment, LOCKED state, and idempotency."""
    client, SessionLocal = isolated_client
    trip_id, order_id = _login_demo_and_confirm_trip(client, SessionLocal)

    client.post("/switch-role", data={"role": "agent", "agent_id": "AG001"})
    client.post(
        f"/orders/{order_id}/quote",
        data={"quoted_price": "78800"},
    )

    # Agent perspective cannot execute member payment
    agent_pay = client.post(f"/orders/{order_id}/pay")
    assert agent_pay.status_code == 403

    # Switch to another member (alex@example.com) and verify 403 protection
    client.post("/switch-role", data={"role": "member"})
    client.post("/logout")
    client.post(
        "/login",
        data={"email": "alex@example.com", "password": "demo1234"},
    )
    assert client.get(f"/orders/{order_id}").status_code == 403
    assert client.post(f"/orders/{order_id}/pay").status_code == 403

    # Log back in as trip owner demo@example.com
    client.post("/logout")
    client.post(
        "/login",
        data={"email": "demo@example.com", "password": "demo1234"},
    )

    order_page = client.get(f"/orders/{order_id}")
    assert order_page.status_code == 200
    assert "NT$ 78,800" in order_page.text
    assert "立即全額付款" in order_page.text

    # 0. Missing passport KYC must block payment cleanly without charging
    missing_kyc = client.post(
        f"/orders/{order_id}/pay",
        data={"payment_method": "CREDIT_CARD", "simulate_outcome": "SUCCESS"},
        follow_redirects=True,
    )
    assert missing_kyc.status_code == 200
    assert "旅客護照基本資料未通過檢核" in missing_kyc.text
    with SessionLocal() as db:
        assert db.query(PaymentTxn).filter(PaymentTxn.order_id == order_id).count() == 0

    valid_kyc = {
        "passport_name": "CHEN, DAMING",
        "passport_number": "312345678",
        "nationality": "Taiwan (TWN)",
        "birth_date": "1995-06-15",
        "passport_expiry": "2032-12-31",
        "contact_phone": "0912-345-678",
        "ticket_email": "demo@example.com",
    }

    # 1. Simulate FAILED payment attempt (with valid KYC persisted)
    fail_res = client.post(
        f"/orders/{order_id}/pay",
        data={"payment_method": "CREDIT_CARD", "simulate_outcome": "FAILED", **valid_kyc},
        follow_redirects=True,
    )
    assert fail_res.status_code == 200
    assert "模擬金流交易失敗" in fail_res.text
    assert "FAILED" in fail_res.text

    with SessionLocal() as db:
        ord_after_fail = db.get(OrderQuote, order_id)
        trip_after_fail = db.get(TripPlan, trip_id)
        txns_after_fail = db.query(PaymentTxn).filter(PaymentTxn.order_id == order_id).all()
        assert ord_after_fail.order_status == "QUOTED"
        assert trip_after_fail.status == "CONFIRMED"
        assert len(txns_after_fail) == 1
        assert txns_after_fail[0].status == "FAILED"
        assert txns_after_fail[0].amount == 78800

    assert mailer_module.get_order_email_logs(order_id)[0]["category"] == "PAYMENT_FAILED"

    # 2. Retry with SUCCESS outcome
    ok_res = client.post(
        f"/orders/{order_id}/pay",
        data={"payment_method": "APPLE_PAY", "simulate_outcome": "SUCCESS"},
        follow_redirects=True,
    )
    assert ok_res.status_code == 200
    assert "全額付款成功" in ok_res.text
    assert "PAID" in ok_res.text
    assert "LOCKED" in ok_res.text
    assert "免費空檔行程已解鎖" in ok_res.text

    with SessionLocal() as db:
        ord_paid = db.get(OrderQuote, order_id)
        trip_locked = db.get(TripPlan, trip_id)
        txns = (
            db.query(PaymentTxn)
            .filter(PaymentTxn.order_id == order_id)
            .order_by(PaymentTxn.txn_id.asc())
            .all()
        )
        assert ord_paid.order_status == "PAID"
        assert trip_locked.status == "LOCKED"
        assert len(txns) == 2
        assert txns[1].txn_type == "PAYMENT"
        assert txns[1].status == "SUCCESS"
        assert txns[1].amount == 78800
        assert txns[1].gateway_ref.startswith("MOCK-PAY-")

    recent_cats = [e["category"] for e in mailer_module.get_order_email_logs(order_id)[:2]]
    assert set(recent_cats) == {"PAYMENT_SUCCESS", "ORDER_DISPATCHED"}

    # 3. Duplicate payment POST must be idempotently blocked
    dup_pay = client.post(
        f"/orders/{order_id}/pay",
        data={"payment_method": "CREDIT_CARD", "simulate_outcome": "SUCCESS"},
        follow_redirects=True,
    )
    assert dup_pay.status_code == 200
    assert "請勿重複刷卡扣款" in dup_pay.text

    with SessionLocal() as db:
        assert db.query(PaymentTxn).filter(PaymentTxn.order_id == order_id).count() == 2

    # 4. Member cannot cancel a PAID / LOCKED trip
    cancel_res = client.post(f"/trips/{trip_id}/cancel", follow_redirects=True)
    assert cancel_res.status_code == 200
    assert "無法直接退單" in cancel_res.text

    # 5. Agent cannot re-quote an already PAID order
    client.post("/switch-role", data={"role": "agent", "agent_id": "AG001"})
    requote_res = client.post(
        f"/orders/{order_id}/quote",
        data={"quoted_price": "99999"},
        follow_redirects=True,
    )
    assert requote_res.status_code == 200
    assert "不可再修改或重開報價單" in requote_res.text
    with SessionLocal() as db:
        assert db.get(OrderQuote, order_id).quoted_price == 78800


def test_cancelled_order_cannot_be_quoted_or_paid(isolated_client):
    """Orders cancelled by trip withdrawal reject both quoting and payment."""
    client, SessionLocal = isolated_client
    trip_id, order_id = _login_demo_and_confirm_trip(client, SessionLocal)

    client.post(f"/trips/{trip_id}/cancel", follow_redirects=True)
    with SessionLocal() as db:
        assert db.get(OrderQuote, order_id).order_status == "CANCELLED"

    pay_cancelled = client.post(f"/orders/{order_id}/pay", follow_redirects=True)
    assert pay_cancelled.status_code == 200
    assert "無法付款" in pay_cancelled.text

    client.post("/switch-role", data={"role": "agent", "agent_id": "AG001"})
    quote_cancelled = client.post(
        f"/orders/{order_id}/quote",
        data={"quoted_price": "50000"},
        follow_redirects=True,
    )
    assert quote_cancelled.status_code == 200
    assert "不可再修改或重開報價單" in quote_cancelled.text


def test_gmail_smtp_real_path_and_demo_redirect_and_fallback(
    monkeypatch: pytest.MonkeyPatch,
):
    """Verify real Gmail SMTP SSL transmission, @example.com demo redirect, and exception fallback."""
    sent_calls: list[dict] = []

    class FakeSMTPSSL:
        def __init__(self, host, port, timeout=None):
            self.host = host
            self.port = port
            self.timeout = timeout
            self.logged_in = None

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def login(self, user, password):
            self.logged_in = (user, password)

        def send_message(self, msg):
            sent_calls.append(
                {
                    "host": self.host,
                    "port": self.port,
                    "timeout": self.timeout,
                    "logged_in": self.logged_in,
                    "to": msg["To"],
                    "subject": msg["Subject"],
                }
            )

    monkeypatch.setattr(config_module, "SMTP_HOST", "smtp.gmail.com")
    monkeypatch.setattr(config_module, "SMTP_PORT", 465)
    monkeypatch.setattr(config_module, "SMTP_USER", "bot@gmail.com")
    monkeypatch.setattr(config_module, "SMTP_APP_PASSWORD", "abcd1234abcd1234")
    monkeypatch.setattr(config_module, "SMTP_DEMO_TO", "presenter@gmail.com")
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTPSSL)

    # 1. Seeded demo domain should redirect to SMTP_DEMO_TO
    demo_res = mailer_module.send_notification_email(
        to_email="demo@example.com",
        subject="報價通知",
        html_body="<p>hi</p>",
        text_body="hi",
        category="QUOTE_READY",
        order_id=901,
    )
    assert demo_res["mode"] == "gmail_smtp"
    assert demo_res["status"] == "SENT"
    assert demo_res["redirected"] is True
    assert demo_res["actual_to"] == "presenter@gmail.com"
    assert "[Demo 轉寄 · 原收件人 demo@example.com]" in demo_res["subject"]
    assert sent_calls[-1]["to"] == "presenter@gmail.com"
    assert sent_calls[-1]["logged_in"] == ("bot@gmail.com", "abcd1234abcd1234")

    # 2. Real user Gmail address goes straight to that address without redirect
    real_res = mailer_module.send_notification_email(
        to_email="student@gmail.com",
        subject="付款收據",
        html_body="<p>paid</p>",
        text_body="paid",
        category="PAYMENT_SUCCESS",
        order_id=902,
    )
    assert real_res["mode"] == "gmail_smtp"
    assert real_res["redirected"] is False
    assert real_res["actual_to"] == "student@gmail.com"
    assert real_res["subject"] == "付款收據"

    # 3. Network/SMTP exception falls back safely without raising
    class BrokenSMTP:
        def __init__(self, *_args, **_kwargs):
            raise OSError("Simulated SMTP firewall block")

    monkeypatch.setattr(smtplib, "SMTP_SSL", BrokenSMTP)
    fallback_res = mailer_module.send_notification_email(
        to_email="student@gmail.com",
        subject="測試斷網防呆",
        html_body="<p>offline</p>",
        text_body="offline",
        category="PAYMENT_SUCCESS",
        order_id=903,
    )
    assert fallback_res["mode"] == "mock_fallback"
    assert fallback_res["status"] == "MOCK_FALLBACK"
    assert "OSError" in fallback_res["note"]


def test_process_mock_refund_helper(isolated_client):
    """Reusable process_mock_refund writes REFUND PaymentTxn and sets order_status=REFUNDED."""
    client, SessionLocal = isolated_client
    _trip_id, order_id = _login_demo_and_confirm_trip(client, SessionLocal)

    client.post("/switch-role", data={"role": "agent", "agent_id": "AG001"})
    client.post(f"/orders/{order_id}/quote", data={"quoted_price": "65000"})

    with SessionLocal() as db:
        order = db.get(OrderQuote, order_id)
        order.order_status = "PAID"
        db.commit()
        refund_txn = payment_module.process_mock_refund(db, order)
        assert refund_txn.txn_type == "REFUND"
        assert refund_txn.status == "SUCCESS"
        assert refund_txn.amount == 65000
        assert refund_txn.gateway_ref.startswith("MOCK-REF-")
        assert order.order_status == "REFUNDED"
