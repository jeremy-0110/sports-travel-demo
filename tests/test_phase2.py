"""Phase 2 tests: trip form validation, Gemini/Mock fallback, regeneration, and confirmation."""

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
from app.models import OrderQuote, TripPlan
from app.services import planner as planner_module
from scripts.seed_db import seed_database


@pytest.fixture()
def isolated_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Provide an isolated SQLite DB, seeded demo user/agent, and TestClient."""
    db_file = tmp_path / "test_phase2.db"
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
        lambda league_type, start_date, end_date, preference="", meta=None: [
            {
                "date": start_date.isoformat(),
                "kickoff_utc": f"{start_date.isoformat()}T19:00Z",
                "league": league_type,
                "matchup": f"{league_type} Live Matchup Demo",
                "venue": "Official Demo Stadium",
                "url": "https://www.espn.com/",
            }
        ],
    )
    def _fake_targets_fetch(targets, start_date, end_date, meta=None):
        events = []
        for idx, t in enumerate(targets):
            ev_date = (start_date + timedelta(days=min(idx, max((end_date - start_date).days, 0)))).isoformat()
            events.append(
                {
                    "date": ev_date,
                    "kickoff_utc": f"{ev_date}T19:00Z",
                    "league": t["sub_league"],
                    "matchup": f"{t.get('team_name') or t['sub_league']} Matchup",
                    "venue": "Official Demo Arena",
                    "url": "https://www.espn.com/",
                }
            )
        if meta is not None:
            meta["strategy"] = "structured_targets"
            meta["matched_team"] = ", ".join(
                f"{t['team_name']} ({t['sub_league']})" for t in targets if t.get("team_name")
            )
            meta["total_found"] = len(events)
            meta["covered_range"] = f"{start_date.isoformat()} ~ {end_date.isoformat()}"
        return events

    monkeypatch.setattr(
        planner_module,
        "fetch_espn_schedules_for_targets",
        _fake_targets_fetch,
    )
    monkeypatch.setattr(
        planner_module,
        "search_authoritative_schedules",
        lambda league_type, start_date, end_date, preference="", max_results=4: [
            {
                "title": f"{league_type} Official Schedule",
                "url": "https://www.espn.com/",
                "snippet": "Mocked authoritative search result for test determinism.",
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


def _login_demo(client: TestClient) -> None:
    """Log in as the seeded demo user."""
    res = client.post(
        "/login",
        data={"email": "demo@example.com", "password": "demo1234"},
        follow_redirects=True,
    )
    assert res.status_code == 200
    assert "陳大明" in res.text


def test_trips_new_requires_login(isolated_client):
    """Unauthenticated visitors accessing /trips/new should be redirected to /login."""
    client, _ = isolated_client
    res = client.get("/trips/new", follow_redirects=False)
    assert res.status_code == 303
    assert res.headers["location"] == "/login"


@pytest.mark.parametrize("bad_league", ["CPBL", "NPB", "NFL", "中華職棒"])
def test_invalid_league_rejected_400(isolated_client, bad_league: str):
    """Backend must strictly reject leagues outside MLB/NBA/歐足五大/歐冠."""
    client, SessionLocal = isolated_client
    _login_demo(client)

    with SessionLocal() as db:
        before_count = db.query(TripPlan).count()

    res = client.post(
        "/trips",
        data={
            "league_type": bad_league,
            "start_date": "2026-08-10",
            "end_date": "2026-08-14",
            "preference": "",
            "need_flight": "true",
        },
    )
    assert res.status_code == 400
    assert "不支援的賽事聯賽" in res.text

    with SessionLocal() as db:
        assert db.query(TripPlan).count() == before_count


def test_invalid_date_range_rejected_400(isolated_client):
    """Reject end_date < start_date and trip durations exceeding 30 days."""
    client, _ = isolated_client
    _login_demo(client)

    reversed_dates = client.post(
        "/trips",
        data={
            "league_type": "MLB",
            "start_date": "2026-08-15",
            "end_date": "2026-08-10",
        },
    )
    assert reversed_dates.status_code == 400
    assert "不可早於出發日期" in reversed_dates.text

    too_long = client.post(
        "/trips",
        data={
            "league_type": "NBA",
            "start_date": "2026-08-01",
            "end_date": "2026-09-10",
        },
    )
    assert too_long.status_code == 400
    assert "不可超過 30 天" in too_long.text


@pytest.mark.parametrize("league", ["MLB", "NBA", "歐足五大", "歐冠"])
def test_create_trip_all_4_supported_leagues_mock_fallback(
    isolated_client, monkeypatch: pytest.MonkeyPatch, league: str
):
    """All 4 supported leagues build instant rule-engine drafts without invoking Gemini."""
    client, SessionLocal = isolated_client
    monkeypatch.setattr(config_module, "GEMINI_API_KEY", "unused-during-rule-engine")
    gemini_calls: list[str] = []

    class ShouldNotCallClient:
        def __init__(self, **_kwargs):
            gemini_calls.append("init")

    import google.genai as genai_module

    monkeypatch.setattr(genai_module, "Client", ShouldNotCallClient)
    _login_demo(client)

    res = client.post(
        "/trips",
        data={
            "league_type": league,
            "start_date": "2026-09-10",
            "end_date": "2026-09-14",
            "preference": "主場焦點戰",
            "need_flight": "true",
            "need_hotel": "true",
        },
        follow_redirects=True,
    )
    assert res.status_code == 200
    assert league in res.text
    assert "規則引擎即時排程 (ESPN)" in res.text
    assert gemini_calls == []

    with SessionLocal() as db:
        trip = (
            db.query(TripPlan)
            .filter(TripPlan.league_type == league, TripPlan.start_date == date(2026, 9, 10))
            .first()
        )
        assert trip is not None
        assert trip.status == "DRAFT"
        parsed = planner_module.parse_draft_json(trip.llm_draft_json)
        assert parsed is not None
        assert parsed["mode"] == "rule_engine"
        assert parsed["diagnostics"]["planning_engine"] == "rule_engine"
        assert len(parsed["days"]) == 5


def test_gemini_exception_falls_back_to_mock(
    isolated_client, monkeypatch: pytest.MonkeyPatch
):
    """If Gemini SDK raises when use_llm=True, planner falls back cleanly to Mock (while /trips stays rule-engine)."""
    client, _ = isolated_client
    monkeypatch.setattr(config_module, "GEMINI_API_KEY", "fake-api-key")

    class BrokenModels:
        def generate_content(self, **_kwargs):
            raise RuntimeError("Simulated Gemini quota exceeded")

    class BrokenClient:
        def __init__(self, **_kwargs):
            self.models = BrokenModels()

    import google.genai as genai_module

    monkeypatch.setattr(genai_module, "Client", BrokenClient)

    direct_draft = planner_module.generate_itinerary_draft(
        league_type="歐冠",
        start_date=date(2026, 10, 20),
        end_date=date(2026, 10, 23),
        need_flight=True,
        need_hotel=True,
        custom_instruction="幫我多留半天市區散步緩衝",
        use_llm=True,
    )
    assert direct_draft["mode"] == "mock"
    assert "展示保底模式 (Mock)" in direct_draft["mode_label"]
    attempted = direct_draft["diagnostics"].get("attempted_models") or []
    assert any("RuntimeError" in str(a.get("status") or "") for a in attempted)

    _login_demo(client)
    create_res = client.post(
        "/trips",
        data={
            "league_type": "歐冠",
            "start_date": "2026-10-20",
            "end_date": "2026-10-23",
            "need_flight": "true",
        },
        follow_redirects=True,
    )
    assert create_res.status_code == 200
    assert "規則引擎即時排程 (ESPN)" in create_res.text


def test_regenerate_and_confirm_creates_quoting_order(
    isolated_client, monkeypatch: pytest.MonkeyPatch
):
    """Regenerate route is removed (404) and confirm creates a QUOTING OrderQuote for AG001."""
    client, SessionLocal = isolated_client
    monkeypatch.setattr(config_module, "GEMINI_API_KEY", "")
    _login_demo(client)

    create_res = client.post(
        "/trips",
        data={
            "league_type": "NBA",
            "start_date": "2026-11-12",
            "end_date": "2026-11-15",
            "preference": "金州勇士",
            "need_flight": "true",
            "need_hotel": "true",
        },
        follow_redirects=True,
    )
    assert create_res.status_code == 200
    trip_id = int(str(create_res.url).rstrip("/").split("/")[-1])

    regen_res = client.post(f"/trips/{trip_id}/regenerate", follow_redirects=False)
    assert regen_res.status_code in (404, 405)

    confirm_res = client.post(f"/trips/{trip_id}/confirm", follow_redirects=True)
    assert confirm_res.status_code == 200
    assert "CONFIRMED" in confirm_res.text
    assert "QUOTING" in confirm_res.text
    assert "AG001" in confirm_res.text

    # Second confirm must stay idempotent
    confirm_again = client.post(f"/trips/{trip_id}/confirm", follow_redirects=True)
    assert confirm_again.status_code == 200

    with SessionLocal() as db:
        trip = db.get(TripPlan, trip_id)
        assert trip is not None
        assert trip.status == "CONFIRMED"
        orders = (
            db.query(OrderQuote)
            .filter(OrderQuote.trip_id == trip_id, OrderQuote.order_type == "MAIN_TRIP")
            .all()
        )
        assert len(orders) == 1
        assert orders[0].agent_id == "AG001"
        assert orders[0].order_status == "QUOTING"
        assert orders[0].quoted_price is None


def test_agent_can_view_confirmed_trip_detail(
    isolated_client, monkeypatch: pytest.MonkeyPatch
):
    """Agent perspective (AG001) can inspect any member's trip detail page."""
    client, _ = isolated_client
    monkeypatch.setattr(config_module, "GEMINI_API_KEY", "")
    _login_demo(client)

    create_res = client.post(
        "/trips",
        data={
            "league_type": "歐足五大",
            "start_date": "2026-12-05",
            "end_date": "2026-12-09",
            "need_flight": "true",
        },
        follow_redirects=True,
    )
    trip_id = int(str(create_res.url).rstrip("/").split("/")[-1])
    client.post(f"/trips/{trip_id}/confirm", follow_redirects=True)

    client.post(
        "/switch-role",
        data={"role": "agent", "agent_id": "AG001"},
        follow_redirects=True,
    )
    agent_view = client.get(f"/trips/{trip_id}")
    assert agent_view.status_code == 200
    assert "訂票專員視角 (AG001) 預覽" in agent_view.text
    assert "歐足五大" in agent_view.text


def test_member_cancel_draft_and_confirmed_trip(
    isolated_client, monkeypatch: pytest.MonkeyPatch
):
    """Member can cancel DRAFT trips and unpaid CONFIRMED trips (syncing OrderQuote)."""
    client, SessionLocal = isolated_client
    monkeypatch.setattr(config_module, "GEMINI_API_KEY", "")
    _login_demo(client)

    draft_res = client.post(
        "/trips",
        data={
            "league_type": "MLB",
            "start_date": "2026-08-10",
            "end_date": "2026-08-13",
            "need_flight": "true",
        },
        follow_redirects=True,
    )
    draft_trip_id = int(str(draft_res.url).rstrip("/").split("/")[-1])

    cancel_draft = client.post(
        f"/trips/{draft_trip_id}/cancel",
        data={"next_page": "detail"},
        follow_redirects=True,
    )
    assert cancel_draft.status_code == 200
    assert "CANCELLED" in cancel_draft.text

    # Cancelled trip cannot be confirmed afterwards
    confirm_cancelled = client.post(
        f"/trips/{draft_trip_id}/confirm", follow_redirects=True
    )
    assert confirm_cancelled.status_code == 200
    assert "已取消（退單），無法送出確認詢價" in confirm_cancelled.text

    with SessionLocal() as db:
        trip1 = db.get(TripPlan, draft_trip_id)
        assert trip1 is not None and trip1.status == "CANCELLED"
        assert (
            db.query(OrderQuote).filter(OrderQuote.trip_id == draft_trip_id).count()
            == 0
        )

    confirmed_res = client.post(
        "/trips",
        data={
            "league_type": "NBA",
            "start_date": "2026-11-18",
            "end_date": "2026-11-21",
            "need_hotel": "true",
        },
        follow_redirects=True,
    )
    confirmed_trip_id = int(str(confirmed_res.url).rstrip("/").split("/")[-1])
    client.post(f"/trips/{confirmed_trip_id}/confirm", follow_redirects=True)

    cancel_confirmed = client.post(
        f"/trips/{confirmed_trip_id}/cancel",
        data={"next_page": "home"},
        follow_redirects=True,
    )
    assert cancel_confirmed.status_code == 200
    assert "已取消（退單）" in cancel_confirmed.text

    with SessionLocal() as db:
        trip2 = db.get(TripPlan, confirmed_trip_id)
        order2 = (
            db.query(OrderQuote).filter(OrderQuote.trip_id == confirmed_trip_id).first()
        )
        assert trip2 is not None and trip2.status == "CANCELLED"
        assert order2 is not None and order2.order_status == "CANCELLED"


def test_member_cannot_cancel_paid_trip_or_others_trip(
    isolated_client, monkeypatch: pytest.MonkeyPatch
):
    """Member cannot cancel a PAID trip or another member's trip."""
    client, SessionLocal = isolated_client
    monkeypatch.setattr(config_module, "GEMINI_API_KEY", "")
    _login_demo(client)

    res = client.post(
        "/trips",
        data={
            "league_type": "歐冠",
            "start_date": "2026-10-10",
            "end_date": "2026-10-13",
        },
        follow_redirects=True,
    )
    trip_id = int(str(res.url).rstrip("/").split("/")[-1])
    client.post(f"/trips/{trip_id}/confirm", follow_redirects=True)

    with SessionLocal() as db:
        order = db.query(OrderQuote).filter(OrderQuote.trip_id == trip_id).first()
        assert order is not None
        order.quoted_price = 68000
        order.order_status = "PAID"
        db.commit()

    blocked_res = client.post(f"/trips/{trip_id}/cancel", follow_redirects=True)
    assert blocked_res.status_code == 200
    assert "無法直接退單" in blocked_res.text

    with SessionLocal() as db:
        trip = db.get(TripPlan, trip_id)
        order = db.query(OrderQuote).filter(OrderQuote.trip_id == trip_id).first()
        assert trip is not None and trip.status == "CONFIRMED"
        assert order is not None and order.order_status == "PAID"

    # Switch user to alex@example.com and try cancelling demo's trip
    client.post("/logout", follow_redirects=True)
    client.post(
        "/login",
        data={"email": "alex@example.com", "password": "demo1234"},
        follow_redirects=True,
    )
    forbidden_res = client.post(f"/trips/{trip_id}/cancel")
    assert forbidden_res.status_code == 403


def test_agent_delete_trip_cascades_orders_and_blocks_member(
    isolated_client, monkeypatch: pytest.MonkeyPatch
):
    """Only agent role can delete a trip, and deletion cascades to related OrderQuote."""
    client, SessionLocal = isolated_client
    monkeypatch.setattr(config_module, "GEMINI_API_KEY", "")
    _login_demo(client)

    res = client.post(
        "/trips",
        data={
            "league_type": "MLB",
            "start_date": "2026-09-20",
            "end_date": "2026-09-23",
        },
        follow_redirects=True,
    )
    trip_id = int(str(res.url).rstrip("/").split("/")[-1])
    client.post(f"/trips/{trip_id}/confirm", follow_redirects=True)

    member_del = client.post(f"/trips/{trip_id}/delete")
    assert member_del.status_code == 403

    client.post(
        "/switch-role",
        data={"role": "agent", "agent_id": "AG001"},
        follow_redirects=True,
    )
    agent_del = client.post(f"/trips/{trip_id}/delete", follow_redirects=True)
    assert agent_del.status_code == 200
    assert f"專員 AG001 已刪除行程 #{trip_id}" in agent_del.text

    with SessionLocal() as db:
        assert db.get(TripPlan, trip_id) is None
        assert db.query(OrderQuote).filter(OrderQuote.trip_id == trip_id).count() == 0

    assert client.get(f"/trips/{trip_id}").status_code == 404


def test_translate_preference_and_authoritative_url_filter():
    """Verify Chinese team name translation and ad URL filtering."""
    en_everton, path_everton = planner_module.translate_preference_to_english(
        "艾佛頓", "歐足五大"
    )
    assert en_everton == "Everton"
    assert path_everton == "soccer/eng.1"

    en_warriors, path_nba = planner_module.translate_preference_to_english(
        "勇士", "NBA"
    )
    assert en_warriors == "Warriors"
    assert path_nba == "basketball/nba"

    en_dodgers, path_mlb = planner_module.translate_preference_to_english(
        "道奇", "MLB"
    )
    assert en_dodgers == "Dodgers"
    assert path_mlb == "baseball/mlb"

    domains = ("premierleague.com", "laliga.com", "espn.com")
    assert planner_module._is_authoritative_url(
        "https://www.espn.com/soccer/fixtures", domains
    )
    assert planner_module._is_authoritative_url(
        "https://www.premierleague.com/fixtures", domains
    )
    assert not planner_module._is_authoritative_url(
        "https://www.bing.com/aclick?ld=123", domains
    )
    assert not planner_module._is_authoritative_url("ftp://espn.com/test", domains)


def test_fetch_espn_schedules_parses_and_prioritizes_preference(
    monkeypatch: pytest.MonkeyPatch,
):
    """Verify ESPN scoreboard parser extracts fixtures and prioritizes preferred teams."""

    class FakeResp:
        status_code = 200

        def json(self):
            return {
                "events": [
                    {
                        "name": "Chelsea at Arsenal",
                        "date": "2026-11-28T15:00Z",
                        "competitions": [
                            {"venue": {"fullName": "Emirates Stadium"}}
                        ],
                        "links": [{"href": "https://www.espn.com/soccer/match/_/1"}],
                    },
                    {
                        "name": "Everton at Liverpool",
                        "date": "2026-11-28T17:30Z",
                        "competitions": [{"venue": {"fullName": "Anfield"}}],
                        "links": [{"href": "https://www.espn.com/soccer/match/_/2"}],
                    },
                ]
            }

    import httpx

    monkeypatch.setattr(httpx.Client, "get", lambda self, url, params=None: FakeResp())

    results = planner_module.fetch_espn_schedules(
        "歐足五大",
        date(2026, 11, 28),
        date(2026, 11, 28),
        preference="艾佛頓",
    )
    assert len(results) == 2
    assert results[0]["matchup"] == "Everton at Liverpool"
    assert results[0]["venue"] == "Anfield"
    assert results[0]["league"] == "英超"


def test_trip_detail_shows_diagnostics_and_live_schedules(
    isolated_client, monkeypatch: pytest.MonkeyPatch
):
    """Trip detail page displays AI/retrieval diagnostics and raw ESPN schedules."""
    client, _ = isolated_client
    monkeypatch.setattr(config_module, "GEMINI_API_KEY", "")
    _login_demo(client)

    res = client.post(
        "/trips",
        data={
            "league_type": "歐足五大",
            "start_date": "2026-11-25",
            "end_date": "2026-11-28",
            "preference": "艾佛頓",
            "need_flight": "true",
        },
        follow_redirects=True,
    )
    assert res.status_code == 200
    assert "AI 呼叫與連網檢索診斷憑證" in res.text
    assert "ESPN 即時賽程 API：抓到 1 場" in res.text
    assert "精選 1 場給 AI" in res.text
    assert "偏好關鍵字轉換：艾佛頓 → Everton" in res.text
    assert "歐足五大 Live Matchup Demo" in res.text


def test_gemini_fallback_chain_switches_to_healthy_model(
    monkeypatch: pytest.MonkeyPatch,
):
    """When primary Gemini model fails (e.g. 504) under use_llm=True, planner switches to healthy fallback model."""
    monkeypatch.setattr(config_module, "GEMINI_API_KEY", "fake-api-key")
    monkeypatch.setattr(config_module, "GEMINI_MODEL", "gemini-4-hydrogen")
    monkeypatch.setattr(
        config_module, "GEMINI_FALLBACK_MODELS", ("gemini-3.5-flash-lite",)
    )

    class FakeResponse:
        text = (
            '{"summary": "已自動切換備援模型完成英超行程規劃。",'
            '"matches": [{"date": "2026-11-29", "matchup": "Liverpool at Everton", "venue": "Hill Dickinson Stadium", "note": "主場焦點戰"}],'
            '"flight_suggestion": "直飛建議",'
            '"hotel_suggestion": "市中心飯店",'
            '"days": [{"day_number": 1, "date": "2026-11-28", "title": "抵達利物浦", "highlights": ["入住"]}],'
            '"estimated_budget_twd": 85000}'
        )
        usage_metadata = None

    class SwitchModels:
        def generate_content(self, model, **_kwargs):
            if model == "gemini-4-hydrogen":
                raise RuntimeError("504 DEADLINE_EXCEEDED")
            return FakeResponse()

    class SwitchClient:
        def __init__(self, **_kwargs):
            self.models = SwitchModels()

    import google.genai as genai_module

    monkeypatch.setattr(genai_module, "Client", SwitchClient)

    draft = planner_module.generate_itinerary_draft(
        league_type="歐足五大",
        start_date=date(2026, 11, 28),
        end_date=date(2026, 11, 30),
        need_flight=True,
        need_hotel=False,
        preference="艾佛頓",
        custom_instruction="優先看艾佛頓主場賽",
        desired_match_count=1,
        use_llm=True,
    )
    assert draft["mode_label"] == "Gemini 連網規劃 (gemini-3.5-flash-lite)"
    attempts = draft["diagnostics"]["attempted_models"]
    assert any(a["model"] == "gemini-4-hydrogen" for a in attempts)
    assert {"model": "gemini-3.5-flash-lite", "status": "ok"} in attempts


def test_subleague_and_cologne_translation():
    """Verify Bundesliga sub-league keywords and Cologne aliases resolve accurately."""
    en1, path1 = planner_module.translate_preference_to_english(
        "德甲、科隆第一足球俱樂部", "歐足五大"
    )
    assert en1 == "FC Cologne"
    assert path1 == "soccer/ger.1"

    en2, path2 = planner_module.translate_preference_to_english("FC Köln", "歐足五大")
    assert en2 == "FC Cologne"
    assert path2 == "soccer/ger.1"

    _, path3 = planner_module.translate_preference_to_english("德甲", "歐足五大")
    assert path3 == "soccer/ger.1"


def test_team_season_schedule_fetches_full_date_range(
    monkeypatch: pytest.MonkeyPatch,
):
    """Single-team schedule API captures fixtures across a 15-day window without truncation."""
    planner_module._ESPN_TEAMS_CACHE.clear()

    class FakeResp:
        def __init__(self, payload: dict):
            self.status_code = 200
            self._payload = payload

        def json(self):
            return self._payload

    def fake_get(self, url, params=None):
        if url.endswith("/teams"):
            return FakeResp(
                {
                    "sports": [
                        {
                            "leagues": [
                                {
                                    "teams": [
                                        {
                                            "team": {
                                                "id": "122",
                                                "displayName": "FC Cologne",
                                                "shortDisplayName": "Cologne",
                                                "nickname": "FC Cologne",
                                            }
                                        }
                                    ]
                                }
                            ]
                        }
                    ]
                }
            )
        if url.endswith("/teams/122/schedule"):
            return FakeResp(
                {
                    "events": [
                        {
                            "name": "SV Elversberg at FC Cologne",
                            "date": "2026-11-28T14:30Z",
                            "competitions": [
                                {"venue": {"fullName": "RheinEnergieStadion"}}
                            ],
                        },
                        {
                            "name": "FC Cologne at FC Augsburg",
                            "date": "2026-12-05T14:30Z",
                            "competitions": [{"venue": {"fullName": "WWK Arena"}}],
                        },
                    ]
                }
            )
        return FakeResp({"events": []})

    import httpx

    monkeypatch.setattr(httpx.Client, "get", fake_get)

    meta: dict = {}
    results = planner_module.fetch_espn_schedules(
        "歐足五大",
        date(2026, 11, 25),
        date(2026, 12, 9),
        preference="德甲、科隆第一足球俱樂部",
        meta=meta,
    )
    matchups = [r["matchup"] for r in results]
    assert "SV Elversberg at FC Cologne" in matchups
    assert "FC Cologne at FC Augsburg" in matchups
    assert meta["strategy"] == "team_schedule"
    assert meta["matched_team"] == "FC Cologne (德甲)"
    assert meta["covered_range"] == "2026-11-25 ~ 2026-12-09"


def test_date_balanced_selection_covers_entire_trip():
    """Date-balanced selector distributes slots across start, middle, and end dates."""
    events = []
    for day in range(1, 11):
        for game in range(1, 6):
            events.append(
                {
                    "date": f"2026-12-{day:02d}",
                    "matchup": f"TeamA vs Team{game} D{day}",
                    "venue": "Stadium",
                }
            )

    selected = planner_module._select_balanced_schedules(
        events, kw_candidates=[], limit=15
    )
    assert len(selected) == 15
    selected_dates = {ev["date"] for ev in selected}
    assert len(selected_dates) == 10
    assert "2026-12-01" in selected_dates
    assert "2026-12-10" in selected_dates


def test_default_model_and_multi_target_player_venue_parsing(
    monkeypatch: pytest.MonkeyPatch,
):
    """Verify multi-condition parsing handles players, venues, and teams across delimiters."""
    import importlib
    import dotenv

    prev_model = config_module.GEMINI_MODEL
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *_a, **_kw: False)
    try:
        importlib.reload(config_module)
        assert config_module.GEMINI_MODEL == "gemini-3.5-flash-lite"
    finally:
        config_module.GEMINI_MODEL = prev_model

    mlb_targets = planner_module.parse_preference_targets("大谷翔平、洋基", "MLB")
    assert [t[0] for t in mlb_targets] == ["Dodgers", "Yankees"]
    assert all(t[1] == "baseball/mlb" for t in mlb_targets)

    nba_targets = planner_module.parse_preference_targets("柯瑞, Chase Center", "NBA")
    assert nba_targets[0][0] == "Warriors"
    assert nba_targets[0][1] == "basketball/nba"

    epl_targets = planner_module.parse_preference_targets("艾佛頓、Anfield", "歐足五大")
    assert [t[0] for t in epl_targets] == ["Everton", "Liverpool"]
    assert all(t[1] == "soccer/eng.1" for t in epl_targets)


def test_customize_instant_remove_and_add_matches(
    isolated_client, monkeypatch: pytest.MonkeyPatch
):
    """Owner can remove unwanted matches and add ESPN fixtures instantly without AI call."""
    client, SessionLocal = isolated_client
    monkeypatch.setattr(config_module, "GEMINI_API_KEY", "")

    monkeypatch.setattr(
        planner_module,
        "fetch_espn_schedules",
        lambda league_type, start_date, end_date, preference="", meta=None: [
            {
                "date": "2026-11-28",
                "kickoff_utc": "2026-11-28T15:00Z",
                "league": "英超",
                "matchup": "Chelsea at Arsenal",
                "venue": "Emirates Stadium",
                "url": "https://www.espn.com/",
            },
            {
                "date": "2026-11-29",
                "kickoff_utc": "2026-11-29T16:30Z",
                "league": "英超",
                "matchup": "Liverpool at Everton",
                "venue": "Hill Dickinson Stadium",
                "url": "https://www.espn.com/",
            },
            {
                "date": "2026-12-01",
                "kickoff_utc": "2026-12-01T20:00Z",
                "league": "英超",
                "matchup": "Tottenham at Manchester City",
                "venue": "Etihad Stadium",
                "url": "https://www.espn.com/",
            },
        ],
    )
    _login_demo(client)

    res = client.post(
        "/trips",
        data={
            "league_type": "歐足五大",
            "start_date": "2026-11-27",
            "end_date": "2026-12-02",
            "desired_match_count": "2",
            "need_flight": "true",
            "need_hotel": "true",
        },
        follow_redirects=True,
    )
    assert res.status_code == 200
    assert "逐日結構化行程表" in res.text
    assert "彈性調整觀賽場次與大架構" in res.text
    trip_id = int(str(res.url).rstrip("/").split("/")[-1])

    # Drop Chelsea at Arsenal, keep Liverpool at Everton, and add Tottenham at Manchester City
    custom_res = client.post(
        f"/trips/{trip_id}/customize",
        data={
            "selected_match_keys": [
                "2026-11-29||Liverpool at Everton（英超）||Hill Dickinson Stadium",
                "2026-12-01||Tottenham at Manchester City（英超）||Etihad Stadium",
            ],
            "desired_match_count": "2",
            "custom_instruction": "兩場中間留空檔逛市區",
            "action_mode": "instant",
        },
        follow_redirects=True,
    )
    assert custom_res.status_code == 200
    assert "已即時套用 2 場觀賽選擇並重組結構化行程表" in custom_res.text
    assert "Tottenham at Manchester City" in custom_res.text

    with SessionLocal() as db:
        trip = db.get(TripPlan, trip_id)
        assert trip is not None
        draft = planner_module.parse_draft_json(trip.llm_draft_json)
        assert draft is not None
        assert len(draft["matches"]) == 2
        assert all("Arsenal" not in m["matchup"] for m in draft["matches"])
        assert draft["diagnostics"]["instant_update"] is True
        assert any(d["day_type"] == "MATCH_DAY" for d in draft["days"])
        assert all(isinstance(d["blocks"], list) and d["blocks"] for d in draft["days"])


def test_customize_ai_replan_with_locked_matches_and_instruction(
    isolated_client, monkeypatch: pytest.MonkeyPatch
):
    """Owner can customize locked matches and custom instruction via instant rule engine without calling Gemini."""
    client, SessionLocal = isolated_client
    monkeypatch.setattr(config_module, "GEMINI_API_KEY", "")
    _login_demo(client)

    create_res = client.post(
        "/trips",
        data={
            "league_type": "NBA",
            "start_date": "2026-12-10",
            "end_date": "2026-12-13",
            "desired_match_count": "1",
        },
        follow_redirects=True,
    )
    trip_id = int(str(create_res.url).rstrip("/").split("/")[-1])

    monkeypatch.setattr(config_module, "GEMINI_API_KEY", "unused-during-rule-engine")
    monkeypatch.setattr(config_module, "GEMINI_MODEL", "gemini-3.5-flash-lite")

    gemini_calls: list[str] = []

    class ShouldNotCallClient:
        def __init__(self, **_kwargs):
            gemini_calls.append("init")

    import google.genai as genai_module

    monkeypatch.setattr(genai_module, "Client", ShouldNotCallClient)

    replan_res = client.post(
        f"/trips/{trip_id}/customize",
        data={
            "selected_match_keys": [
                "2026-12-11||NBA Live Matchup Demo（NBA）||Official Demo Stadium"
            ],
            "desired_match_count": "1",
            "custom_instruction": "住宿改靠近車站且比賽前留半天逛街",
            "action_mode": "instant",
        },
        follow_redirects=True,
    )
    assert replan_res.status_code == 200
    assert "規則引擎即時排程 (ESPN)" in replan_res.text
    assert "已即時套用 1 場觀賽選擇並重組結構化行程表（0 秒生效）" in replan_res.text
    assert "住宿改靠近車站且比賽前留半天逛街" in replan_res.text
    assert gemini_calls == []

    with SessionLocal() as db:
        trip = db.get(TripPlan, trip_id)
        draft = planner_module.parse_draft_json(trip.llm_draft_json)
        assert draft is not None
        assert draft["matches"][0]["matchup"] == "NBA Live Matchup Demo（NBA）"
        assert draft["custom_instruction"] == "住宿改靠近車站且比賽前留半天逛街"
        assert any(blk["category"] == "賽事" for blk in draft["days"][1]["blocks"])


def test_api_teams_valid_and_invalid_path(
    isolated_client, monkeypatch: pytest.MonkeyPatch
):
    """GET /api/teams returns formatted teams for valid leagues and 400 for invalid ones."""
    client, _ = isolated_client
    planner_module._ESPN_TEAMS_CACHE["soccer/ger.1"] = [
        {"id": "122", "name": "FC Cologne", "short_name": "Cologne", "nickname": "FC Cologne"},
        {"id": "132", "name": "Bayern Munich", "short_name": "Bayern", "nickname": "Bayern"},
    ]
    res = client.get("/api/teams?espn_path=soccer/ger.1")
    assert res.status_code == 200
    payload = res.json()
    assert payload["espn_path"] == "soccer/ger.1"
    labels = [t["label"] for t in payload["teams"]]
    assert "FC Cologne（科隆）" in labels
    assert "Bayern Munich（拜仁慕尼黑）" in labels

    bad = client.get("/api/teams?espn_path=baseball/cpbl")
    assert bad.status_code == 400


def test_structured_targets_cross_league_mlb_nba(
    isolated_client, monkeypatch: pytest.MonkeyPatch
):
    """Structured targets combine same-continent cross-league MLB + NBA cleanly."""
    import json

    client, SessionLocal = isolated_client
    monkeypatch.setattr(config_module, "GEMINI_API_KEY", "")
    _login_demo(client)

    targets_payload = json.dumps(
        [
            {"espn_path": "baseball/mlb", "team_id": "19", "team_name": "Los Angeles Dodgers（洛杉磯道奇）"},
            {"espn_path": "basketball/nba", "team_id": "9", "team_name": "Golden State Warriors（金州勇士）"},
        ]
    )
    res = client.post(
        "/trips",
        data={
            "targets_json": targets_payload,
            "start_date": "2026-10-25",
            "end_date": "2026-10-30",
            "desired_match_count": "2",
            "need_flight": "true",
            "need_hotel": "true",
        },
        follow_redirects=True,
    )
    assert res.status_code == 200
    assert "MLB+NBA" in res.text
    trip_id = int(str(res.url).rstrip("/").split("/")[-1])

    with SessionLocal() as db:
        trip = db.get(TripPlan, trip_id)
        assert trip is not None and trip.league_type == "MLB+NBA"
        draft = planner_module.parse_draft_json(trip.llm_draft_json)
        assert draft is not None
        assert draft["is_cross_continent"] is False
        assert len(draft["targets"]) == 2


def test_structured_targets_cross_continent_mlb_ucl(
    isolated_client, monkeypatch: pytest.MonkeyPatch
):
    """Structured targets support cross-continent MLB + 歐冠 with transit buffer & surcharge."""
    import json

    client, SessionLocal = isolated_client
    monkeypatch.setattr(config_module, "GEMINI_API_KEY", "")
    _login_demo(client)

    targets_payload = json.dumps(
        [
            {"espn_path": "baseball/mlb", "team_id": "19", "team_name": "Los Angeles Dodgers（洛杉磯道奇）"},
            {"espn_path": "soccer/uefa.champions", "team_id": "86", "team_name": "Real Madrid（皇家馬德里）"},
        ]
    )
    res = client.post(
        "/trips",
        data={
            "targets_json": targets_payload,
            "start_date": "2026-10-25",
            "end_date": "2026-11-02",
            "desired_match_count": "2",
            "need_flight": "true",
            "need_hotel": "true",
        },
        follow_redirects=True,
    )
    assert res.status_code == 200
    assert "MLB+歐冠" in res.text
    assert "跨洲行程（北美 ↔ 歐洲）" in res.text
    assert "跨洲長程航段" in res.text
    trip_id = int(str(res.url).rstrip("/").split("/")[-1])

    with SessionLocal() as db:
        trip = db.get(TripPlan, trip_id)
        assert trip is not None and trip.league_type == "MLB+歐冠"
        draft = planner_module.parse_draft_json(trip.llm_draft_json)
        assert draft is not None
        assert draft["is_cross_continent"] is True


def test_structured_targets_invalid_league_rejected_400(isolated_client):
    """Structured targets reject unsupported leagues with HTTP 400."""
    import json

    client, _ = isolated_client
    _login_demo(client)

    res = client.post(
        "/trips",
        data={
            "targets_json": json.dumps([{"espn_path": "baseball/cpbl", "team_id": "1", "team_name": "CPBL"}]),
            "start_date": "2026-10-25",
            "end_date": "2026-10-28",
        },
    )
    assert res.status_code == 400
    assert "不支援的賽事聯賽代碼" in res.text


def test_feasibility_blocks_same_day_conflict_on_customize_and_confirm(
    isolated_client, monkeypatch: pytest.MonkeyPatch
):
    """Reject same-day conflicting matches in /customize and block /confirm when conflicts exist."""
    import json

    client, SessionLocal = isolated_client
    monkeypatch.setattr(config_module, "GEMINI_API_KEY", "")
    monkeypatch.setattr(
        planner_module,
        "fetch_espn_schedules",
        lambda league_type, start_date, end_date, preference="", meta=None: [
            {
                "date": "2026-11-29",
                "kickoff_utc": "2026-11-29T15:00Z",
                "league": "英超",
                "matchup": "Chelsea at Arsenal",
                "venue": "Emirates Stadium",
                "url": "https://www.espn.com/",
            },
            {
                "date": "2026-11-29",
                "kickoff_utc": "2026-11-29T17:30Z",
                "league": "英超",
                "matchup": "Liverpool at Everton",
                "venue": "Hill Dickinson Stadium",
                "url": "https://www.espn.com/",
            },
        ],
    )
    _login_demo(client)

    res = client.post(
        "/trips",
        data={
            "league_type": "歐足五大",
            "start_date": "2026-11-27",
            "end_date": "2026-12-01",
            "desired_match_count": "2",
            "need_flight": "true",
        },
        follow_redirects=True,
    )
    assert res.status_code == 200
    trip_id = int(str(res.url).rstrip("/").split("/")[-1])

    with SessionLocal() as db:
        trip = db.get(TripPlan, trip_id)
        draft = planner_module.parse_draft_json(trip.llm_draft_json)
        assert len(draft["matches"]) == 1

    # Submitting two same-day different-venue matches must be rejected
    bad_custom = client.post(
        f"/trips/{trip_id}/customize",
        data={
            "selected_match_keys": [
                "2026-11-29||Chelsea at Arsenal（英超）||Emirates Stadium",
                "2026-11-29||Liverpool at Everton（英超）||Hill Dickinson Stadium",
            ],
            "desired_match_count": "2",
            "action_mode": "instant",
        },
        follow_redirects=True,
    )
    assert bad_custom.status_code == 200
    assert "排程可行性檢查未通過" in bad_custom.text
    assert "同一天（2026-11-29）" in bad_custom.text

    with SessionLocal() as db:
        trip = db.get(TripPlan, trip_id)
        draft = planner_module.parse_draft_json(trip.llm_draft_json)
        assert len(draft["matches"]) == 1
        # Simulate corrupted/conflicting stored matches and verify /confirm blocks it
        draft["matches"].append(
            {
                "date": "2026-11-29",
                "kickoff_utc": "2026-11-29T17:30Z",
                "league": "英超",
                "matchup": "Liverpool at Everton（英超）",
                "venue": "Hill Dickinson Stadium",
                "note": "conflict",
            }
        )
        trip.llm_draft_json = json.dumps(draft, ensure_ascii=False)
        db.commit()

    bad_confirm = client.post(f"/trips/{trip_id}/confirm", follow_redirects=True)
    assert bad_confirm.status_code == 200
    assert "無法確認送出報價，請先排除排程衝突" in bad_confirm.text

    with SessionLocal() as db:
        trip = db.get(TripPlan, trip_id)
        assert trip.status == "DRAFT"
        assert db.query(OrderQuote).filter(OrderQuote.trip_id == trip_id).count() == 0


def test_feasibility_blocks_cross_continent_short_duration_and_short_gap(
    isolated_client, monkeypatch: pytest.MonkeyPatch
):
    """Cross-continent trips reject < 6 days duration and < 2 days NA<->EU match gaps."""
    import json

    client, _ = isolated_client
    monkeypatch.setattr(config_module, "GEMINI_API_KEY", "")
    _login_demo(client)

    targets_payload = json.dumps(
        [
            {"espn_path": "baseball/mlb", "team_id": "19", "team_name": "Los Angeles Dodgers（洛杉磯道奇）"},
            {"espn_path": "soccer/eng.1", "team_id": "364", "team_name": "Liverpool（利物浦）"},
        ]
    )

    too_short = client.post(
        "/trips",
        data={
            "targets_json": targets_payload,
            "start_date": "2026-10-25",
            "end_date": "2026-10-28",
            "need_flight": "true",
        },
    )
    assert too_short.status_code == 400
    assert "至少須 6 天以上" in too_short.text

    monkeypatch.setattr(
        planner_module,
        "fetch_espn_schedules_for_targets",
        lambda targets, start_date, end_date, meta=None: [
            {
                "date": "2026-10-27",
                "kickoff_utc": "2026-10-27T23:00Z",
                "league": "MLB",
                "region": "NA",
                "matchup": "Yankees at Dodgers",
                "venue": "Dodger Stadium",
                "url": "https://www.espn.com/",
            },
            {
                "date": "2026-10-28",
                "kickoff_utc": "2026-10-28T19:00Z",
                "league": "英超",
                "region": "EU",
                "matchup": "Arsenal at Liverpool",
                "venue": "Anfield",
                "url": "https://www.espn.com/",
            },
            {
                "date": "2026-10-29",
                "kickoff_utc": "2026-10-29T19:45Z",
                "league": "英超",
                "region": "EU",
                "matchup": "Chelsea at Liverpool",
                "venue": "Anfield",
                "url": "https://www.espn.com/",
            },
        ],
    )

    ok_create = client.post(
        "/trips",
        data={
            "targets_json": targets_payload,
            "start_date": "2026-10-25",
            "end_date": "2026-11-01",
            "desired_match_count": "2",
            "need_flight": "true",
        },
        follow_redirects=True,
    )
    assert ok_create.status_code == 200
    trip_id = int(str(ok_create.url).rstrip("/").split("/")[-1])

    # NA on 10-27 + EU on 10-28 -> gap is 1 day (< 2 days) -> blocked
    bad_gap = client.post(
        f"/trips/{trip_id}/customize",
        data={
            "selected_match_keys": [
                "2026-10-27||Yankees at Dodgers（MLB）||Dodger Stadium",
                "2026-10-28||Arsenal at Liverpool（英超）||Anfield",
            ],
            "desired_match_count": "2",
            "action_mode": "instant",
        },
        follow_redirects=True,
    )
    assert bad_gap.status_code == 200
    assert "跨洲賽事（北美 ↔ 歐洲）間隔不足 2 天" in bad_gap.text

    # NA on 10-27 + EU on 10-29 -> gap is 2 days -> allowed
    ok_gap = client.post(
        f"/trips/{trip_id}/customize",
        data={
            "selected_match_keys": [
                "2026-10-27||Yankees at Dodgers（MLB）||Dodger Stadium",
                "2026-10-29||Chelsea at Liverpool（英超）||Anfield",
            ],
            "desired_match_count": "2",
            "action_mode": "instant",
        },
        follow_redirects=True,
    )
    assert ok_gap.status_code == 200
    assert "已即時套用 2 場觀賽選擇" in ok_gap.text


def test_feasibility_blocks_flight_day_match_when_need_flight(
    isolated_client, monkeypatch: pytest.MonkeyPatch
):
    """When need_flight=True and duration >= 3 days, matches on start_date/end_date are rejected."""
    client, _ = isolated_client
    monkeypatch.setattr(config_module, "GEMINI_API_KEY", "")
    monkeypatch.setattr(
        planner_module,
        "fetch_espn_schedules",
        lambda league_type, start_date, end_date, preference="", meta=None: [
            {
                "date": "2026-11-27",
                "kickoff_utc": "2026-11-27T19:00Z",
                "league": "NBA",
                "matchup": "Lakers at Warriors",
                "venue": "Chase Center",
                "url": "https://www.espn.com/",
            },
            {
                "date": "2026-11-29",
                "kickoff_utc": "2026-11-29T19:00Z",
                "league": "NBA",
                "matchup": "Celtics at Warriors",
                "venue": "Chase Center",
                "url": "https://www.espn.com/",
            },
        ],
    )
    _login_demo(client)

    with_flight = client.post(
        "/trips",
        data={
            "league_type": "NBA",
            "start_date": "2026-11-27",
            "end_date": "2026-11-30",
            "need_flight": "true",
        },
        follow_redirects=True,
    )
    trip_with_flight_id = int(str(with_flight.url).rstrip("/").split("/")[-1])

    blocked = client.post(
        f"/trips/{trip_with_flight_id}/customize",
        data={
            "selected_match_keys": [
                "2026-11-27||Lakers at Warriors（NBA）||Chase Center",
            ],
            "desired_match_count": "1",
            "action_mode": "instant",
        },
        follow_redirects=True,
    )
    assert blocked.status_code == 200
    assert "出發日（2026-11-27）須預留長程飛行" in blocked.text

    # When need_flight is False (self-arranged flights), Day 1 match is allowed
    without_flight = client.post(
        "/trips",
        data={
            "league_type": "NBA",
            "start_date": "2026-11-27",
            "end_date": "2026-11-30",
        },
        follow_redirects=True,
    )
    trip_no_flight_id = int(str(without_flight.url).rstrip("/").split("/")[-1])
    allowed = client.post(
        f"/trips/{trip_no_flight_id}/customize",
        data={
            "selected_match_keys": [
                "2026-11-27||Lakers at Warriors（NBA）||Chase Center",
            ],
            "desired_match_count": "1",
            "action_mode": "instant",
        },
        follow_redirects=True,
    )
    assert allowed.status_code == 200
    assert "已即時套用 1 場觀賽選擇" in allowed.text


def test_prepayment_itinerary_has_no_attractions_and_coerces_gemini_attractions(
    isolated_client, monkeypatch: pytest.MonkeyPatch
):
    """Pre-payment drafts allow only core categories and coerce any Gemini '景點' blocks to locked 空檔."""
    client, SessionLocal = isolated_client
    monkeypatch.setattr(config_module, "GEMINI_API_KEY", "")
    _login_demo(client)

    mock_res = client.post(
        "/trips",
        data={
            "league_type": "歐足五大",
            "start_date": "2026-12-05",
            "end_date": "2026-12-09",
            "need_flight": "true",
            "need_hotel": "true",
        },
        follow_redirects=True,
    )
    assert mock_res.status_code == 200
    assert "🔒 空檔" in mock_res.text
    mock_trip_id = int(str(mock_res.url).rstrip("/").split("/")[-1])

    with SessionLocal() as db:
        trip = db.get(TripPlan, mock_trip_id)
        draft = planner_module.parse_draft_json(trip.llm_draft_json)
        allowed_cats = {"賽事", "交通", "住宿", "空檔"}
        categories = {blk["category"] for day in draft["days"] for blk in day["blocks"]}
        assert categories.issubset(allowed_cats)
        assert "景點" not in categories
        assert "空檔" in categories

    # Verify Gemini prompt + output coercion rejects "景點" when use_llm=True is invoked directly
    monkeypatch.setattr(config_module, "GEMINI_API_KEY", "fake-key")
    monkeypatch.setattr(config_module, "GEMINI_MODEL", "gemini-3.5-flash-lite")

    class AttractionModels:
        def generate_content(self, **_kwargs):
            class Resp:
                text = (
                    '{"summary": "倫敦觀賽核心大架構。",'
                    '"matches": [{"date": "2026-12-06", "matchup": "Everton vs Liverpool", "venue": "Hill Dickinson Stadium", "note": "主場"}],'
                    '"flight_suggestion": "直飛倫敦",'
                    '"hotel_suggestion": "市中心",'
                    '"days": ['
                    '{"day_number": 1, "date": "2026-12-05", "day_type": "ARRIVAL", "city": "倫敦", "title": "抵達與觀光",'
                    ' "blocks": ['
                    '  {"period": "上午", "category": "交通", "title": "抵達機場", "detail": "希斯洛入境"},'
                    '  {"period": "下午", "category": "景點", "title": "大英博物館深度觀光", "detail": "免費參觀大英博物館"}'
                    ' ]},'
                    '{"day_number": 2, "date": "2026-12-06", "day_type": "MATCH_DAY", "city": "利物浦", "title": "比賽日",'
                    ' "blocks": [{"period": "晚間", "category": "賽事", "title": "Everton vs Liverpool", "detail": "看球"}]}'
                    '],'
                    '"estimated_budget_twd": 88000}'
                )
                usage_metadata = None

            return Resp()

    class AttractionClient:
        def __init__(self, **_kwargs):
            self.models = AttractionModels()

    import google.genai as genai_module

    monkeypatch.setattr(genai_module, "Client", AttractionClient)

    g_draft = planner_module.generate_itinerary_draft(
        league_type="歐足五大",
        start_date=date(2026, 12, 5),
        end_date=date(2026, 12, 9),
        need_flight=True,
        need_hotel=True,
        custom_instruction="順便幫我排大英博物館進去",
        desired_match_count=1,
        use_llm=True,
    )
    all_titles = [blk["title"] for day in g_draft["days"] for blk in day["blocks"]]
    assert "大英博物館深度觀光" not in all_titles
    coerced_block = g_draft["days"][0]["blocks"][1]
    assert coerced_block["category"] == "空檔"
    assert coerced_block["title"] == planner_module.LOCKED_GAP_TITLE
    assert coerced_block["detail"] == planner_module.LOCKED_GAP_DETAIL
