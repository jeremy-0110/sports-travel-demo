"""Extension tests: Live customer support chat, agent-assisted refund workflow, and master schedule view."""

from datetime import date, timedelta
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
from app.models import (
    BookingVoucher,
    GapItineraryItem,
    OrderQuote,
    PaymentTxn,
    SupportMessage,
    SupportTicket,
    TripPlan,
)
from app.services import gap_planner as gap_planner_module
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
    db_file = tmp_path / "test_phase6.db"
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
    monkeypatch.setattr(
        gap_planner_module,
        "search_gap_attractions_ddgs",
        lambda city, preference="", max_results=4: [],
    )

    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app) as client:
            yield client, testing_session_local
    finally:
        app.dependency_overrides.clear()
        test_engine.dispose()


def _create_paid_trip_and_order(
    client: TestClient,
    SessionLocal,
    league_type: str = "NBA",
    price: int = 84000,
) -> tuple[int, int]:
    """Create, confirm, quote, and pay a MAIN_TRIP order -> returns (trip_id, order_id)."""
    start = date.today() + timedelta(days=30)
    end = start + timedelta(days=4)
    assert (
        client.post(
            "/login",
            data={"email": "demo@example.com", "password": "demo1234"},
            follow_redirects=False,
        ).status_code
        == 303
    )
    assert (
        client.post(
            "/trips",
            data={
                "league_type": league_type,
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
                "need_flight": "true",
                "need_hotel": "true",
            },
            follow_redirects=False,
        ).status_code
        == 303
    )
    with SessionLocal() as db:
        trip_id = db.query(TripPlan).order_by(TripPlan.trip_id.desc()).first().trip_id

    assert client.post(f"/trips/{trip_id}/confirm", follow_redirects=False).status_code == 303
    with SessionLocal() as db:
        order_id = (
            db.query(OrderQuote)
            .filter(OrderQuote.trip_id == trip_id, OrderQuote.order_type == "MAIN_TRIP")
            .first()
            .order_id
        )

    assert (
        client.post("/switch-role", data={"role": "agent", "agent_id": "AG001"}, follow_redirects=False).status_code
        == 303
    )
    assert (
        client.post(
            f"/orders/{order_id}/quote",
            data={"quoted_price": str(price)},
            follow_redirects=False,
        ).status_code
        == 303
    )
    assert (
        client.post("/switch-role", data={"role": "member"}, follow_redirects=False).status_code
        == 303
    )
    assert (
        client.post(
            f"/orders/{order_id}/pay",
            data={
                "payment_method": "CREDIT_CARD",
                "simulate_outcome": "SUCCESS",
                **_VALID_TRAVELER_KYC,
            },
            follow_redirects=False,
        ).status_code
        == 303
    )
    return trip_id, order_id


def test_support_live_chat_permissions_polling_and_close(isolated_client):
    """Member and AG001 exchange messages in /support with 403 isolation and polling API."""
    client, SessionLocal = isolated_client

    # Anonymous access redirects to /login
    anon_get = client.get("/support", follow_redirects=False)
    assert anon_get.status_code == 303
    assert anon_get.headers["location"] == "/login"

    # Member creates ticket
    client.post(
        "/login",
        data={"email": "demo@example.com", "password": "demo1234"},
        follow_redirects=False,
    )
    create_resp = client.post(
        "/support/new",
        data={
            "category": "GENERAL",
            "subject": "想確認 NBA 主場座位視野與進場時間",
            "message": "請問下層看台是否建議提前一小時抵達安檢？",
        },
        follow_redirects=False,
    )
    assert create_resp.status_code == 303
    loc = create_resp.headers["location"]
    assert loc.startswith("/support?ticket_id=")
    ticket_id = int(loc.split("=")[-1])

    # Other user cannot view or poll this ticket
    client.post(
        "/login",
        data={"email": "alex@example.com", "password": "demo1234"},
        follow_redirects=False,
    )
    assert client.get(f"/support?ticket_id={ticket_id}", follow_redirects=False).status_code == 403
    assert client.get(f"/api/support/{ticket_id}/messages", follow_redirects=False).status_code == 403

    # Switch to Agent AG001 and reply
    client.post("/switch-role", data={"role": "agent", "agent_id": "AG001"}, follow_redirects=False)
    agent_page = client.get(f"/support?ticket_id={ticket_id}")
    assert agent_page.status_code == 200
    assert "想確認 NBA 主場座位視野與進場時間" in agent_page.text

    reply_resp = client.post(
        f"/support/{ticket_id}/messages",
        data={"content": "建議開賽前 75 分鐘抵達，安檢與紀念品店都很順暢！"},
        follow_redirects=True,
    )
    assert reply_resp.status_code == 200
    assert "建議開賽前 75 分鐘抵達" in reply_resp.text

    # Switch back to owner member and poll JSON endpoint
    client.post("/switch-role", data={"role": "member"}, follow_redirects=False)
    client.post(
        "/login",
        data={"email": "demo@example.com", "password": "demo1234"},
        follow_redirects=False,
    )
    poll = client.get(f"/api/support/{ticket_id}/messages")
    assert poll.status_code == 200
    data = poll.json()
    assert data["ticket_id"] == ticket_id
    assert data["status"] == "OPEN"
    assert len(data["messages"]) == 3
    assert any(m["sender_role"] == "AGENT" for m in data["messages"])

    # Close ticket cleanly and verify further posts are rejected gracefully
    close_resp = client.post(f"/support/{ticket_id}/close", follow_redirects=True)
    assert close_resp.status_code == 200
    assert "CLOSED" in close_resp.text

    after_close = client.post(
        f"/support/{ticket_id}/messages",
        data={"content": "結案後補問"},
        follow_redirects=True,
    )
    assert "已結案" in after_close.text


def test_support_refund_approval_main_trip_and_duplicate_guard(isolated_client):
    """Member submits refund via /support -> AG001 approves -> REFUND txn + CANCELLED trip + email."""
    client, SessionLocal = isolated_client
    trip_id, order_id = _create_paid_trip_and_order(client, SessionLocal, league_type="MLB", price=85000)

    # Member submits refund request ticket
    ref_create = client.post(
        "/support/new",
        data={
            "category": "REFUND",
            "order_id": str(order_id),
            "subject": f"申請訂單 #{order_id} 全額退款",
            "message": "因臨時公司出差無法成行，希望能透過客服協助全額退款。",
        },
        follow_redirects=False,
    )
    assert ref_create.status_code == 303
    ticket_id = int(ref_create.headers["location"].split("=")[-1])

    # Duplicate refund request on same order is blocked cleanly
    dup_ref = client.post(
        "/support/new",
        data={
            "category": "REFUND",
            "order_id": str(order_id),
            "subject": "重複送出",
            "message": "重複申請退款",
        },
        follow_redirects=True,
    )
    assert dup_ref.status_code == 200
    assert "已有審核中的退款對話" in dup_ref.text

    # Member cannot call agent refund approval endpoint directly
    assert (
        client.post(
            f"/support/{ticket_id}/refund/approve",
            data={"approved_amount": "85000"},
            follow_redirects=False,
        ).status_code
        == 403
    )

    # Agent AG001 approves refund (partial/customizable amount allowed; test full 85000)
    client.post("/switch-role", data={"role": "agent", "agent_id": "AG001"}, follow_redirects=False)
    mailer_module._EMAIL_DELIVERY_LOG.clear()
    approve_resp = client.post(
        f"/support/{ticket_id}/refund/approve",
        data={"approved_amount": "85000", "agent_note": "已協調退票完畢，為您辦理全額退還。"},
        follow_redirects=True,
    )
    assert approve_resp.status_code == 200
    assert "MOCK-REF-" in approve_resp.text
    assert "REFUND_APPROVED" in approve_resp.text

    with SessionLocal() as db:
        ticket = db.get(SupportTicket, ticket_id)
        order = db.get(OrderQuote, order_id)
        trip = db.get(TripPlan, trip_id)
        assert ticket.status == "REFUND_APPROVED"
        assert order.order_status == "REFUNDED"
        assert trip.status == "CANCELLED"
        refund_txn = (
            db.query(PaymentTxn)
            .filter(PaymentTxn.order_id == order_id, PaymentTxn.txn_type == "REFUND")
            .first()
        )
        assert refund_txn is not None and refund_txn.amount == 85000

    assert any(
        log["category"] == "SUPPORT_REFUND_APPROVED"
        for log in mailer_module.get_order_email_logs(order_id)
    )


def test_support_refund_approval_addon_releases_gap_item_and_preserves_main_trip(isolated_client):
    """Approving an ADDON refund via support must release GapItineraryItem and keep main trip LOCKED."""
    client, SessionLocal = isolated_client
    trip_id, _main_order_id = _create_paid_trip_and_order(client, SessionLocal, league_type="NBA", price=79000)

    assert client.get(f"/trips/{trip_id}/gap").status_code == 200
    with SessionLocal() as db:
        draft = planner_module.parse_draft_json(db.get(TripPlan, trip_id).llm_draft_json)
        slot0 = draft["gap_suggestions"]["slots"][0]

    client.post(
        f"/trips/{trip_id}/gap/save",
        data={
            f"choice__{slot0['slot_key']}": "__CUSTOM__",
            f"custom__{slot0['slot_key']}": "勇士球場導覽門票",
            f"addon__{slot0['slot_key']}": "1",
        },
        follow_redirects=False,
    )
    addon_redirect = client.post(f"/trips/{trip_id}/gap/addon-order", follow_redirects=False)
    addon_order_id = int(addon_redirect.headers["location"].split("/")[-1])

    client.post("/switch-role", data={"role": "agent", "agent_id": "AG001"}, follow_redirects=False)
    client.post(f"/orders/{addon_order_id}/quote", data={"quoted_price": "2600"}, follow_redirects=False)
    client.post("/switch-role", data={"role": "member"}, follow_redirects=False)
    client.post(
        f"/orders/{addon_order_id}/pay",
        data={"payment_method": "CREDIT_CARD", "simulate_outcome": "SUCCESS"},
        follow_redirects=False,
    )

    ref_resp = client.post(
        "/support/new",
        data={
            "category": "REFUND",
            "order_id": str(addon_order_id),
            "subject": "改為自由行想退掉景點加購門票",
            "message": "行程有變，想先退訂這個景點門票加購（手續費可扣 400）。",
        },
        follow_redirects=False,
    )
    ticket_id = int(ref_resp.headers["location"].split("=")[-1])

    # Agent approves partial refund NT$ 2,200
    client.post("/switch-role", data={"role": "agent", "agent_id": "AG001"}, follow_redirects=False)
    approve = client.post(
        f"/support/{ticket_id}/refund/approve",
        data={"approved_amount": "2200", "agent_note": "扣除手續費 NT$ 400 後退還 NT$ 2,200。"},
        follow_redirects=True,
    )
    assert approve.status_code == 200
    assert "2,200" in approve.text

    with SessionLocal() as db:
        assert db.get(TripPlan, trip_id).status == "LOCKED"
        assert db.get(OrderQuote, addon_order_id).order_status == "REFUNDED"
        gap_item = db.query(GapItineraryItem).filter(GapItineraryItem.trip_id == trip_id).first()
        assert gap_item is not None
        assert gap_item.addon_order_id is None
        assert gap_item.need_ticket_addon is False


def test_support_refund_rejection_keeps_order_paid(isolated_client):
    """Agent rejecting a support refund writes explanation message and preserves PAID status."""
    client, SessionLocal = isolated_client
    _trip_id, order_id = _create_paid_trip_and_order(client, SessionLocal, league_type="歐冠", price=120000)

    ref_resp = client.post(
        "/support/new",
        data={
            "category": "REFUND",
            "order_id": str(order_id),
            "subject": "申請歐冠行程退款",
            "message": "想改期出發，請問可以退費嗎？",
        },
        follow_redirects=False,
    )
    ticket_id = int(ref_resp.headers["location"].split("=")[-1])

    client.post("/switch-role", data={"role": "agent", "agent_id": "AG001"}, follow_redirects=False)
    mailer_module._EMAIL_DELIVERY_LOG.clear()
    reject = client.post(
        f"/support/{ticket_id}/refund/reject",
        data={"reject_reason": "歐冠官方球票屬實名不可退改票種，建議可維持原行程出發。"},
        follow_redirects=True,
    )
    assert reject.status_code == 200
    assert "REFUND_REJECTED" in reject.text
    assert "歐冠官方球票屬實名不可退改票種" in reject.text

    with SessionLocal() as db:
        assert db.get(OrderQuote, order_id).order_status == "PAID"
        assert db.get(SupportTicket, ticket_id).status == "REFUND_REJECTED"
    assert any(
        log["category"] == "SUPPORT_REFUND_REJECTED"
        for log in mailer_module.get_order_email_logs(order_id)
    )


def test_master_schedule_new_window_and_delete_cleanup_with_support_tickets(isolated_client):
    """GET /trips/{id}/schedule merges major architecture + gap items + voucher codes & cleans up on delete."""
    client, SessionLocal = isolated_client
    trip_id, main_order_id = _create_paid_trip_and_order(client, SessionLocal, league_type="MLB", price=89000)

    # Issue main trip vouchers
    client.post("/switch-role", data={"role": "agent", "agent_id": "AG001"}, follow_redirects=False)
    client.post(f"/orders/{main_order_id}/vouchers/auto-issue", follow_redirects=False)
    client.post("/switch-role", data={"role": "member"}, follow_redirects=False)

    # Add minor gap items (1 free + 1 addon -> ADDON order -> booked SPOT voucher)
    assert client.get(f"/trips/{trip_id}/gap").status_code == 200
    with SessionLocal() as db:
        draft = planner_module.parse_draft_json(db.get(TripPlan, trip_id).llm_draft_json)
        slots = draft["gap_suggestions"]["slots"]
        slot0, slot1 = slots[0], slots[1]

    client.post(
        f"/trips/{trip_id}/gap/save",
        data={
            f"choice__{slot0['slot_key']}": "__CUSTOM__",
            f"custom__{slot0['slot_key']}": "道奇體育場幕後VIP導覽",
            f"addon__{slot0['slot_key']}": "1",
            f"choice__{slot1['slot_key']}": "__CUSTOM__",
            f"custom__{slot1['slot_key']}": "聖塔莫尼卡日落散步",
        },
        follow_redirects=False,
    )
    addon_order_id = int(
        client.post(f"/trips/{trip_id}/gap/addon-order", follow_redirects=False).headers["location"].split("/")[-1]
    )
    client.post("/switch-role", data={"role": "agent", "agent_id": "AG001"}, follow_redirects=False)
    client.post(f"/orders/{addon_order_id}/quote", data={"quoted_price": "1900"}, follow_redirects=False)
    client.post("/switch-role", data={"role": "member"}, follow_redirects=False)
    client.post(
        f"/orders/{addon_order_id}/pay",
        data={"payment_method": "CREDIT_CARD", "simulate_outcome": "SUCCESS"},
        follow_redirects=False,
    )
    client.post("/switch-role", data={"role": "agent", "agent_id": "AG001"}, follow_redirects=False)
    client.post(f"/orders/{addon_order_id}/vouchers/auto-issue", follow_redirects=False)

    # Also attach a SupportTicket referencing main_order_id to test FK-safe deletion later
    client.post("/switch-role", data={"role": "member"}, follow_redirects=False)
    client.post(
        "/support/new",
        data={
            "category": "ITINERARY",
            "order_id": str(main_order_id),
            "subject": "詢問接駁路線",
            "message": "請問從飯店搭地鐵去球場要多久？",
        },
        follow_redirects=False,
    )

    # Master Schedule renders merged major architecture + gap items + voucher codes
    sched = client.get(f"/trips/{trip_id}/schedule")
    assert sched.status_code == 200
    assert "完整行程總表" in sched.text
    assert "大架構 ＋ 小行程整合" in sched.text
    assert "道奇體育場幕後VIP導覽" in sched.text
    assert "聖塔莫尼卡日落散步" in sched.text
    assert "AGD-SPT-" in sched.text
    assert "AGD-MAT-" in sched.text
    # Paid trips must not show pre-payment locked slogans on unassigned gap slots
    assert "自由彈性空檔（尚未安排小行程）" in sched.text
    assert "自由空檔時段（付款後解鎖）" not in sched.text
    assert "全額付款完成後，系統將自動解鎖" not in sched.text

    detail_paid = client.get(f"/trips/{trip_id}")
    assert detail_paid.status_code == 200
    assert "自由彈性空檔（尚未安排小行程）" in detail_paid.text
    assert "自由空檔時段（付款後解鎖）" not in detail_paid.text

    # Another user gets 403 on schedule
    client.post("/login", data={"email": "alex@example.com", "password": "demo1234"}, follow_redirects=False)
    assert client.get(f"/trips/{trip_id}/schedule", follow_redirects=False).status_code == 403

    # Agent can view schedule AND cleanly delete trip even with linked SupportTicket
    client.post("/switch-role", data={"role": "agent", "agent_id": "AG001"}, follow_redirects=False)
    assert client.get(f"/trips/{trip_id}/schedule").status_code == 200
    del_resp = client.post(f"/trips/{trip_id}/delete", follow_redirects=False)
    assert del_resp.status_code == 303
    with SessionLocal() as db:
        assert db.get(TripPlan, trip_id) is None
        ticket = db.query(SupportTicket).first()
        assert ticket is not None and ticket.order_id is None


def test_miscategorized_gemini_gap_block_coerced_and_unlocked_properly(isolated_client):
    """Ensure blocks with LOCKED_GAP_TITLE under '住宿'/'交通' are normalized and unlocked after payment."""
    import json

    client, SessionLocal = isolated_client
    trip_id, _main_order_id = _create_paid_trip_and_order(client, SessionLocal, league_type="NBA", price=80000)

    # Simulate an already-saved draft where Gemini mis-labeled a gap block as '住宿'
    with SessionLocal() as db:
        trip = db.get(TripPlan, trip_id)
        draft = planner_module.parse_draft_json(trip.llm_draft_json)
        draft["days"][0]["blocks"].append(
            {
                "period": "宵夜時段",
                "category": "住宿",
                "title": "自由空檔時段（付款後解鎖）",
                "detail": "全額付款完成後，系統將自動解鎖免費空檔旅遊建議與景點門票加購代訂。",
            }
        )
        trip.llm_draft_json = json.dumps(draft, ensure_ascii=False)
        db.commit()

        slots = gap_planner_module.extract_gap_slots(draft, trip)
        assert any(s["period"] == "宵夜時段" for s in slots)

    for url in (f"/trips/{trip_id}/schedule", f"/trips/{trip_id}"):
        resp = client.get(url)
        assert resp.status_code == 200
        assert "自由空檔時段（付款後解鎖）" not in resp.text
        assert "全額付款完成後，系統將自動解鎖" not in resp.text
        assert "自由彈性空檔（尚未安排小行程）" in resp.text

    # Also verify _normalize_gemini_draft normalizes Gemini raw blocks with LOCKED_GAP_TITLE -> '空檔'
    raw_norm = planner_module._normalize_gemini_draft(
        {
            "summary": "測試 NBA 觀賽大架構",
            "matches": [
                {
                    "date": "2026-12-24",
                    "matchup": "Lakers vs Warriors",
                    "venue": "Crypto.com Arena",
                    "note": "",
                }
            ],
            "days": [
                {
                    "day_number": 1,
                    "date": "2026-12-24",
                    "day_type": "ARRIVAL",
                    "city": "洛杉磯",
                    "title": "抵達日",
                    "blocks": [
                        {
                            "period": "晚間",
                            "category": "住宿",
                            "title": planner_module.LOCKED_GAP_TITLE,
                            "detail": planner_module.LOCKED_GAP_DETAIL,
                        }
                    ],
                }
            ],
        },
        league_type="NBA",
        start_date=date(2026, 12, 24),
        end_date=date(2026, 12, 24),
        need_flight=True,
        need_hotel=True,
        preference="",
        search_sources=[],
        live_schedules=[],
        diagnostics={},
        used_model="gemini-3.5-flash-lite",
    )
    assert raw_norm["days"][0]["blocks"][0]["category"] == "空檔"
