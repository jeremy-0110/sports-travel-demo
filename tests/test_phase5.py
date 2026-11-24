"""Phase 5 tests: Post-payment free gap itinerary planning & attraction addon lifecycle (DFD 6.0)."""

from datetime import date, timedelta
import json
from pathlib import Path
import sys
from types import SimpleNamespace

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
from app.models import BookingException, BookingVoucher, GapItineraryItem, OrderQuote, PaymentTxn, TripPlan
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
    db_file = tmp_path / "test_phase5.db"
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
        lambda city, preference="", max_results=4: [
            {
                "title": f"{city} Official Visitor Guide",
                "url": "https://example.com/guide",
                "snippet": "Curated landmarks, food markets, and stadium tours.",
            }
        ],
    )

    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app) as client:
            yield client, testing_session_local
    finally:
        app.dependency_overrides.clear()
        test_engine.dispose()


def _create_confirmed_trip(client: TestClient, SessionLocal, league_type: str = "MLB") -> tuple[int, int]:
    """Create and confirm a trip (returning trip_id, main_order_id) in QUOTING state."""
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
    resp = client.post(
        "/trips",
        data={
            "league_type": league_type,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "preference": "想近距離體驗美國職棒主場氛圍",
            "need_flight": "true",
            "need_hotel": "true",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    with SessionLocal() as db:
        trip = db.query(TripPlan).order_by(TripPlan.trip_id.desc()).first()
        assert trip is not None
        trip_id = trip.trip_id

    confirm_resp = client.post(f"/trips/{trip_id}/confirm", follow_redirects=False)
    assert confirm_resp.status_code == 303

    with SessionLocal() as db:
        order = (
            db.query(OrderQuote)
            .filter(OrderQuote.trip_id == trip_id, OrderQuote.order_type == "MAIN_TRIP")
            .first()
        )
        assert order is not None
        order_id = order.order_id
    return trip_id, order_id


def _pay_main_trip(client: TestClient, SessionLocal, order_id: int, price: int = 86000) -> None:
    """Have AG001 quote the main order and the owner pay with valid KYC."""
    assert (
        client.post(
            "/switch-role",
            data={"role": "agent", "agent_id": "AG001"},
            follow_redirects=False,
        ).status_code
        == 303
    )
    q_resp = client.post(
        f"/orders/{order_id}/quote",
        data={"quoted_price": str(price), "quote_note": "一口價全包報價單"},
        follow_redirects=False,
    )
    assert q_resp.status_code == 303

    assert (
        client.post(
            "/switch-role",
            data={"role": "member"},
            follow_redirects=False,
        ).status_code
        == 303
    )
    p_resp = client.post(
        f"/orders/{order_id}/pay",
        data={
            "payment_method": "CREDIT_CARD",
            "simulate_outcome": "SUCCESS",
            **_VALID_TRAVELER_KYC,
        },
        follow_redirects=False,
    )
    assert p_resp.status_code == 303


def test_gap_planner_locked_before_payment_and_role_guards(isolated_client):
    """Unpaid trips must block /trips/{id}/gap; non-owners and agents cannot write gap selections."""
    client, SessionLocal = isolated_client
    trip_id, order_id = _create_confirmed_trip(client, SessionLocal, league_type="MLB")

    # Unpaid trip redirects back to /trips/{id}
    get_locked = client.get(f"/trips/{trip_id}/gap", follow_redirects=True)
    assert get_locked.status_code == 200
    assert "全額付款成功" in get_locked.text

    post_locked = client.post(
        f"/trips/{trip_id}/gap/suggest",
        data={"gap_preference": "美食"},
        follow_redirects=False,
    )
    assert post_locked.status_code == 303
    assert post_locked.headers["location"] == f"/trips/{trip_id}"

    # Complete main payment -> unlocks gap planning
    _pay_main_trip(client, SessionLocal, order_id)

    # Another logged-in member receives 403
    assert (
        client.post(
            "/login",
            data={"email": "alex@example.com", "password": "demo1234"},
            follow_redirects=False,
        ).status_code
        == 303
    )
    wrong_owner = client.get(f"/trips/{trip_id}/gap", follow_redirects=False)
    assert wrong_owner.status_code == 403

    # Agent cannot mutate member's gap itinerary selections
    client.post(
        "/switch-role",
        data={"role": "agent", "agent_id": "AG001"},
        follow_redirects=False,
    )
    agent_post = client.post(
        f"/trips/{trip_id}/gap/save",
        data={},
        follow_redirects=False,
    )
    assert agent_post.status_code == 403


def test_gap_suggest_preference_with_mocked_gemini_and_mock_fallback(
    isolated_client, monkeypatch: pytest.MonkeyPatch
):
    """Gemini prompt suggestions and automatic Mock fallback both produce 3 options per gap slot."""
    client, SessionLocal = isolated_client
    trip_id, order_id = _create_confirmed_trip(client, SessionLocal, league_type="NBA")
    _pay_main_trip(client, SessionLocal, order_id)

    # 1. Mock fallback mode (GEMINI_API_KEY == '') with preference prompt
    resp_mock = client.post(
        f"/trips/{trip_id}/gap/suggest",
        data={"gap_preference": "想吃在地知名美食跟參觀博物館"},
        follow_redirects=True,
    )
    assert resp_mock.status_code == 200
    assert "想吃在地知名美食跟參觀博物館" in resp_mock.text
    assert "推薦引擎：MOCK" in resp_mock.text

    with SessionLocal() as db:
        trip = db.get(TripPlan, trip_id)
        draft = planner_module.parse_draft_json(trip.llm_draft_json)
        assert draft is not None
        slots = draft["gap_suggestions"]["slots"]
        assert len(slots) >= 2
        first_slot_key = slots[0]["slot_key"]
        for slot in slots:
            assert len(slot["options"]) == 3

    # 2. Mocked Gemini API returning structured JSON options
    class _FakeModels:
        def generate_content(self, model, contents, config):
            payload = {
                "slots": [
                    {
                        "slot_key": first_slot_key,
                        "options": [
                            {
                                "activity_name": "Gemini客製・勇士主場幕後導覽＋紀念品街",
                                "tag": "Gemini推薦 · 需門票",
                                "description": "依您的博物館與導覽偏好專屬挑選的球館行程。",
                                "ticket_required": True,
                                "estimated_ticket_twd": 1800,
                            },
                            {
                                "activity_name": "Gemini客製・舊金山漁人碼頭海鮮濃湯散策",
                                "tag": "Gemini推薦 · 美食",
                                "description": "輕鬆步行享受海灣夕陽與道地酸麵包濃湯。",
                                "ticket_required": False,
                                "estimated_ticket_twd": 0,
                            },
                            {
                                "activity_name": "Gemini客製・現代藝術博物館 MOMA 巡禮",
                                "tag": "藝文展覽 · 需門票",
                                "description": "館藏豐富且距離飯店捷運一站直達。",
                                "ticket_required": True,
                                "estimated_ticket_twd": 950,
                            },
                        ],
                    }
                ]
            }
            return SimpleNamespace(text=json.dumps(payload, ensure_ascii=False))

    class _FakeClient:
        def __init__(self, api_key, http_options=None):
            self.models = _FakeModels()

    import google.genai as genai_module

    monkeypatch.setattr(config_module, "GEMINI_API_KEY", "fake-gemini-key")
    monkeypatch.setattr(genai_module, "Client", _FakeClient)

    resp_gemini = client.post(
        f"/trips/{trip_id}/gap/suggest",
        data={"gap_preference": "想要勇士主場導覽加上漁人碼頭美食"},
        follow_redirects=True,
    )
    assert resp_gemini.status_code == 200
    assert "推薦引擎：GEMINI" in resp_gemini.text
    assert "Gemini客製・勇士主場幕後導覽＋紀念品街" in resp_gemini.text
    assert "Gemini客製・舊金山漁人碼頭海鮮濃湯散策" in resp_gemini.text


def test_save_gap_selections_unlocks_trip_detail_itinerary(isolated_client):
    """Saving gap choices writes D2 GapItineraryItem and replaces 🔒 空檔 on /trips/{id}."""
    client, SessionLocal = isolated_client
    trip_id, order_id = _create_confirmed_trip(client, SessionLocal, league_type="MLB")
    _pay_main_trip(client, SessionLocal, order_id)

    # Opening /trips/{id}/gap initializes and caches gap_suggestions
    gap_page = client.get(f"/trips/{trip_id}/gap")
    assert gap_page.status_code == 200

    with SessionLocal() as db:
        trip = db.get(TripPlan, trip_id)
        draft = planner_module.parse_draft_json(trip.llm_draft_json)
        slots = draft["gap_suggestions"]["slots"]
        assert len(slots) >= 2
        slot0 = slots[0]
        slot1 = slots[1]

    save_resp = client.post(
        f"/trips/{trip_id}/gap/save",
        data={
            f"choice__{slot0['slot_key']}": slot0["options"][0]["option_id"],
            f"opt_name__{slot0['options'][0]['option_id']}": slot0["options"][0]["activity_name"],
            f"addon__{slot0['slot_key']}": "1",
            f"choice__{slot1['slot_key']}": "__CUSTOM__",
            f"custom__{slot1['slot_key']}": "小東京豚骨拉麵與伴手禮自由逛街",
        },
        follow_redirects=True,
    )
    assert save_resp.status_code == 200
    assert "已儲存免費空檔行程" in save_resp.text

    with SessionLocal() as db:
        items = (
            db.query(GapItineraryItem)
            .filter(GapItineraryItem.trip_id == trip_id)
            .order_by(GapItineraryItem.day_number.asc(), GapItineraryItem.item_id.asc())
            .all()
        )
        assert len(items) == 2
        assert items[0].need_ticket_addon is True
        assert items[0].addon_order_id is None
        assert items[1].activity_name == "小東京豚骨拉麵與伴手禮自由逛街"
        assert items[1].need_ticket_addon is False

    trip_detail = client.get(f"/trips/{trip_id}")
    assert trip_detail.status_code == 200
    assert "✓ 空檔行程" in trip_detail.text
    assert slot0["options"][0]["activity_name"] in trip_detail.text
    assert "小東京豚骨拉麵與伴手禮自由逛街" in trip_detail.text


def test_gap_addon_order_end_to_end_quote_pay_reuse_kyc_spot_voucher_and_delete(
    isolated_client,
):
    """ADDON order reuses quoting, mock payment (with saved KYC), SPOT auto-issue, and delete cleanup."""
    client, SessionLocal = isolated_client
    trip_id, main_order_id = _create_confirmed_trip(client, SessionLocal, league_type="歐足五大")
    _pay_main_trip(client, SessionLocal, main_order_id, price=118000)

    # Open gap page to populate default suggestions
    assert client.get(f"/trips/{trip_id}/gap").status_code == 200
    with SessionLocal() as db:
        trip = db.get(TripPlan, trip_id)
        draft = planner_module.parse_draft_json(trip.llm_draft_json)
        slots = draft["gap_suggestions"]["slots"]
        slot0 = slots[0]
        slot1 = slots[1]

    chosen_addon_spot = "倫敦眼摩天輪快速通關 VIP 門票"
    client.post(
        f"/trips/{trip_id}/gap/save",
        data={
            f"choice__{slot0['slot_key']}": "__CUSTOM__",
            f"custom__{slot0['slot_key']}": chosen_addon_spot,
            f"addon__{slot0['slot_key']}": "1",
            f"choice__{slot1['slot_key']}": "__CUSTOM__",
            f"custom__{slot1['slot_key']}": "博羅市場自由早午餐散策",
        },
        follow_redirects=False,
    )

    mailer_module._EMAIL_DELIVERY_LOG.clear()
    addon_submit = client.post(
        f"/trips/{trip_id}/gap/addon-order",
        follow_redirects=False,
    )
    assert addon_submit.status_code == 303
    redirect_loc = addon_submit.headers["location"]
    assert redirect_loc.startswith("/orders/")
    addon_order_id = int(redirect_loc.split("/")[-1])

    with SessionLocal() as db:
        addon_order = db.get(OrderQuote, addon_order_id)
        assert addon_order is not None
        assert addon_order.order_type == "ADDON"
        assert addon_order.order_status == "QUOTING"
        assert addon_order.agent_id == "AG001"
        linked_items = (
            db.query(GapItineraryItem)
            .filter(GapItineraryItem.addon_order_id == addon_order_id)
            .all()
        )
        assert len(linked_items) == 1
        assert linked_items[0].activity_name == chosen_addon_spot

    # Duplicate submit with zero pending addon items fails cleanly
    dup_resp = client.post(f"/trips/{trip_id}/gap/addon-order", follow_redirects=True)
    assert dup_resp.status_code == 200
    assert "目前沒有待詢價" in dup_resp.text

    # Re-saving /trips/{id}/gap/save preserves locked ADDON slot & modifies unlocked slot cleanly
    client.post(
        f"/trips/{trip_id}/gap/save",
        data={
            f"choice__{slot1['slot_key']}": "__CUSTOM__",
            f"custom__{slot1['slot_key']}": "牛津街球衣採購漫步",
        },
        follow_redirects=False,
    )
    with SessionLocal() as db:
        all_gap = (
            db.query(GapItineraryItem)
            .filter(GapItineraryItem.trip_id == trip_id)
            .order_by(GapItineraryItem.day_number.asc())
            .all()
        )
        names = {g.activity_name for g in all_gap}
        assert chosen_addon_spot in names
        assert "牛津街球衣採購漫步" in names

    # AG001 opens ADDON order and quotes NT$ 3,600
    client.post(
        "/switch-role",
        data={"role": "agent", "agent_id": "AG001"},
        follow_redirects=False,
    )
    agent_order_view = client.get(f"/orders/{addon_order_id}")
    assert agent_order_view.status_code == 200
    assert chosen_addon_spot in agent_order_view.text

    quote_resp = client.post(
        f"/orders/{addon_order_id}/quote",
        data={"quoted_price": "3600", "quote_note": "含倫敦眼快速通關雙人電子票代訂"},
        follow_redirects=False,
    )
    assert quote_resp.status_code == 303

    # Member pays ADDON order WITHOUT re-entering passport KYC fields (auto-reuses saved KYC!)
    client.post(
        "/switch-role",
        data={"role": "member"},
        follow_redirects=False,
    )
    pay_addon = client.post(
        f"/orders/{addon_order_id}/pay",
        data={
            "payment_method": "APPLE_PAY",
            "simulate_outcome": "SUCCESS",
        },
        follow_redirects=False,
    )
    assert pay_addon.status_code == 303

    with SessionLocal() as db:
        addon_order = db.get(OrderQuote, addon_order_id)
        assert addon_order.order_status == "PAID"
        txn = (
            db.query(PaymentTxn)
            .filter(PaymentTxn.order_id == addon_order_id, PaymentTxn.status == "SUCCESS")
            .first()
        )
        assert txn is not None and txn.amount == 3600

    # AG001 auto-issues SPOT vouchers -> ADDON order promotes to BOOKED
    client.post(
        "/switch-role",
        data={"role": "agent", "agent_id": "AG001"},
        follow_redirects=False,
    )
    auto_spot = client.post(
        f"/orders/{addon_order_id}/vouchers/auto-issue",
        follow_redirects=True,
    )
    assert auto_spot.status_code == 200
    assert "已一鍵批次核發" in auto_spot.text

    with SessionLocal() as db:
        addon_order = db.get(OrderQuote, addon_order_id)
        assert addon_order.order_status == "BOOKED"
        spot_vouchers = (
            db.query(BookingVoucher)
            .filter(BookingVoucher.order_id == addon_order_id)
            .all()
        )
        assert len(spot_vouchers) == 1
        assert spot_vouchers[0].category == "SPOT"
        assert spot_vouchers[0].booking_code.startswith("AGD-SPT-")

    voucher_page = client.get(f"/orders/{addon_order_id}/vouchers")
    assert voucher_page.status_code == 200
    assert chosen_addon_spot in voucher_page.text
    assert "CHEN, DAMING" in voucher_page.text

    # Agent cleans up test trip cleanly (verifies no FK errors with GapItineraryItem + ADDON order)
    del_resp = client.post(f"/trips/{trip_id}/delete", follow_redirects=False)
    assert del_resp.status_code == 303
    with SessionLocal() as db:
        assert db.get(TripPlan, trip_id) is None
        assert db.get(OrderQuote, addon_order_id) is None
        assert db.query(GapItineraryItem).filter(GapItineraryItem.trip_id == trip_id).count() == 0


def test_addon_exception_refund_releases_gap_slot_and_preserves_main_trip(isolated_client):
    """Refunding an ADDON order must keep the main trip LOCKED and release the GapItineraryItem."""
    client, SessionLocal = isolated_client
    trip_id, main_order_id = _create_confirmed_trip(client, SessionLocal, league_type="NBA")
    _pay_main_trip(client, SessionLocal, main_order_id, price=76000)

    assert client.get(f"/trips/{trip_id}/gap").status_code == 200
    with SessionLocal() as db:
        draft = planner_module.parse_draft_json(db.get(TripPlan, trip_id).llm_draft_json)
        slot0 = draft["gap_suggestions"]["slots"][0]

    assert (
        client.post(
            f"/trips/{trip_id}/gap/save",
            data={
                f"choice__{slot0['slot_key']}": "__CUSTOM__",
                f"custom__{slot0['slot_key']}": "惡魔島官方渡輪門票",
                f"addon__{slot0['slot_key']}": "1",
            },
            follow_redirects=False,
        ).status_code
        == 303
    )
    addon_redirect = client.post(f"/trips/{trip_id}/gap/addon-order", follow_redirects=False)
    addon_order_id = int(addon_redirect.headers["location"].split("/")[-1])

    # Home dashboard surfaces both MAIN_TRIP and ADDON orders + gap shortcut for the member
    home_page = client.get("/")
    assert home_page.status_code == 200
    assert f"加購 #{addon_order_id}" in home_page.text
    assert "空檔規劃" in home_page.text

    # Agent quotes, member pays ADDON, agent reports exception, member selects REFUND
    client.post("/switch-role", data={"role": "agent", "agent_id": "AG001"})
    client.post(f"/orders/{addon_order_id}/quote", data={"quoted_price": "2200"})
    client.post("/switch-role", data={"role": "member"})
    client.post(
        f"/orders/{addon_order_id}/pay",
        data={"payment_method": "CREDIT_CARD", "simulate_outcome": "SUCCESS"},
    )

    client.post("/switch-role", data={"role": "agent", "agent_id": "AG001"})
    client.post(
        f"/orders/{addon_order_id}/exceptions",
        data={
            "reason": "惡魔島預約梯次全數客滿",
            "alternative_proposal": "可改預約翌日上午船班，或全額退款重新挑選其他空檔活動。",
        },
    )
    with SessionLocal() as db:
        exc = (
            db.query(BookingException)
            .filter(BookingException.order_id == addon_order_id)
            .first()
        )
        assert exc is not None
        exc_id = exc.exception_id

    client.post("/switch-role", data={"role": "member"})
    refund_page = client.post(
        f"/orders/{addon_order_id}/exceptions/{exc_id}/decide",
        data={"decision": "REFUND"},
        follow_redirects=True,
    )
    assert refund_page.status_code == 200
    assert "主行程不受影響" in refund_page.text
    assert "台灣時間" in refund_page.text

    with SessionLocal() as db:
        assert db.get(TripPlan, trip_id).status == "LOCKED"
        assert db.get(OrderQuote, addon_order_id).order_status == "REFUNDED"
        released_item = (
            db.query(GapItineraryItem)
            .filter(GapItineraryItem.trip_id == trip_id)
            .first()
        )
        assert released_item is not None
        assert released_item.addon_order_id is None
        assert released_item.need_ticket_addon is False


def test_switch_role_safe_next_url_stays_on_order_and_blocks_external_redirect(isolated_client):
    """Role switching preserves relative order/trip URLs and rejects open redirects."""
    client, SessionLocal = isolated_client
    trip_id, order_id = _create_confirmed_trip(client, SessionLocal, league_type="MLB")

    stay_agent = client.post(
        "/switch-role",
        data={"role": "agent", "agent_id": "AG001", "next_url": f"/orders/{order_id}"},
        follow_redirects=False,
    )
    assert stay_agent.status_code == 303
    assert stay_agent.headers["location"] == f"/orders/{order_id}"

    for bad_next in ("https://evil.example/phish", "//evil.example/phish", "/orders/1\r\nLocation: http://x"):
        safe_fallback = client.post(
            "/switch-role",
            data={"role": "member", "next_url": bad_next},
            follow_redirects=False,
        )
        assert safe_fallback.status_code == 303
        assert safe_fallback.headers["location"] == "/"

