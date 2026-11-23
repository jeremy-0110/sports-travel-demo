"""Sports travel request, AI draft planning, and confirmation routes (DFD 2.0)."""

from datetime import date, timedelta
import json

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.auth import (
    build_nav_context,
    get_active_agent,
    get_active_role,
    get_current_user,
    set_flash,
)
from app.config import DEFAULT_AGENT_ID, SUPPORTED_LEAGUES
from app.database import get_db
from app.models import Agent, GapItineraryItem, OrderQuote, TripPlan, User
from app.services.gap_planner import is_trip_gap_unlocked
from app.services.mailer import send_quoting_request_email
from app.services.planner import (
    LEAGUE_OPTIONS,
    LEAGUE_OPTION_BY_PATH,
    apply_match_selection_instantly,
    generate_itinerary_draft,
    get_league_teams,
    infer_match_region,
    parse_draft_json,
    summarize_targets_league_type,
    validate_schedule_feasibility,
    validate_structured_targets,
    validate_trip_request,
)

router = APIRouter()


def _default_form_data() -> dict[str, object]:
    """Return sensible default dates and flags for a new trip request form."""
    default_start = date.today() + timedelta(days=30)
    default_end = default_start + timedelta(days=4)
    return {
        "league_type": SUPPORTED_LEAGUES[0],
        "start_date": default_start.isoformat(),
        "end_date": default_end.isoformat(),
        "preference": "",
        "targets_json": "[]",
        "desired_match_count": 2,
        "need_flight": True,
        "need_hotel": True,
    }


@router.get("/api/teams")
def api_league_teams(espn_path: str = ""):
    """Return official teams for a supported ESPN league path."""
    if espn_path not in LEAGUE_OPTION_BY_PATH:
        return JSONResponse({"error": "不支援的賽事聯賽代碼"}, status_code=400)
    return {"espn_path": espn_path, "teams": get_league_teams(espn_path)}


@router.get("/trips/new", response_class=HTMLResponse)
def new_trip_page(request: Request, db: Session = Depends(get_db)):
    """Render the sports travel requirement form (member login required)."""
    current_user = get_current_user(request, db)
    if current_user is None:
        set_flash(request, "error", "請先登入會員帳號，再填寫賽事旅遊需求。")
        return RedirectResponse(url="/login", status_code=303)

    context = build_nav_context(request, db)
    context.update(
        {
            "error": None,
            "form_data": _default_form_data(),
            "league_options": LEAGUE_OPTIONS,
        }
    )
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="trip_new.html",
        context=context,
    )


@router.post("/trips", response_class=HTMLResponse)
def create_trip(
    request: Request,
    league_type: str = Form(""),
    start_date: str = Form(""),
    end_date: str = Form(""),
    preference: str = Form(""),
    targets_json: str = Form(""),
    desired_match_count: int = Form(2),
    need_flight: bool = Form(False),
    need_hotel: bool = Form(False),
    db: Session = Depends(get_db),
):
    """Validate sports trip request, generate Gemini/Mock draft, and persist TripPlan."""
    current_user = get_current_user(request, db)
    if current_user is None:
        set_flash(request, "error", "請先登入會員帳號，再建立賽事旅遊規劃。")
        return RedirectResponse(url="/login", status_code=303)

    league_type = league_type.strip()
    preference = preference.strip()
    targets_raw_str = targets_json.strip()
    start_raw = start_date.strip()
    end_raw = end_date.strip()
    clamped_count = max(1, min(desired_match_count, 6))

    error: str | None = None
    parsed_start: date | None = None
    parsed_end: date | None = None
    normalized_targets: list[dict[str, str]] | None = None

    try:
        parsed_start = date.fromisoformat(start_raw)
        parsed_end = date.fromisoformat(end_raw)
    except ValueError:
        error = "請輸入有效的出發與回程日期格式（YYYY-MM-DD）。"

    if error is None and parsed_start is not None and parsed_end is not None:
        if targets_raw_str:
            try:
                parsed_targets = json.loads(targets_raw_str)
            except ValueError:
                parsed_targets = None
                error = "觀賽清單格式錯誤，請重新選擇賽事與隊伍。"
            if error is None:
                normalized_targets, error = validate_structured_targets(
                    parsed_targets, parsed_start, parsed_end
                )
        else:
            error = validate_trip_request(league_type, parsed_start, parsed_end)

    if error is not None or parsed_start is None or parsed_end is None:
        context = build_nav_context(request, db)
        context.update(
            {
                "error": error,
                "league_options": LEAGUE_OPTIONS,
                "form_data": {
                    "league_type": league_type,
                    "start_date": start_raw,
                    "end_date": end_raw,
                    "preference": preference,
                    "targets_json": targets_raw_str or "[]",
                    "desired_match_count": clamped_count,
                    "need_flight": need_flight,
                    "need_hotel": need_hotel,
                },
            }
        )
        return request.app.state.templates.TemplateResponse(
            request=request,
            name="trip_new.html",
            context=context,
            status_code=400,
        )

    effective_league = (
        summarize_targets_league_type(normalized_targets)
        if normalized_targets is not None
        else league_type
    )
    effective_preference = (
        "、".join(
            f"{t['sub_league']}:{t['team_name'] or '全聯賽'}"
            for t in normalized_targets
        )
        if normalized_targets is not None
        else preference
    )

    draft = generate_itinerary_draft(
        league_type=effective_league,
        start_date=parsed_start,
        end_date=parsed_end,
        need_flight=need_flight,
        need_hotel=need_hotel,
        preference=effective_preference,
        desired_match_count=clamped_count,
        structured_targets=normalized_targets,
    )

    trip = TripPlan(
        user_id=current_user.user_id,
        league_type=effective_league,
        start_date=parsed_start,
        end_date=parsed_end,
        need_flight=need_flight,
        need_hotel=need_hotel,
        llm_draft_json=json.dumps(draft, ensure_ascii=False),
        status="DRAFT",
    )
    db.add(trip)
    db.commit()
    db.refresh(trip)

    set_flash(
        request,
        "success",
        f"已產生【{effective_league}】行程大架構草稿（{draft['mode_label']}），請確認架構內容。",
    )
    return RedirectResponse(url=f"/trips/{trip.trip_id}", status_code=303)


@router.get("/trips/{trip_id}", response_class=HTMLResponse)
def trip_detail_page(
    trip_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Display the AI itinerary draft and confirmation controls for a TripPlan."""
    trip = db.get(TripPlan, trip_id)
    if trip is None:
        raise HTTPException(status_code=404, detail="找不到指定的行程規劃")

    active_role = get_active_role(request)
    current_user = get_current_user(request, db)
    if active_role != "agent":
        if current_user is None:
            set_flash(request, "error", "請先登入會員帳號以檢視行程大架構。")
            return RedirectResponse(url="/login", status_code=303)
        if trip.user_id != current_user.user_id:
            return PlainTextResponse("您沒有權限檢視此筆行程規劃", status_code=403)

    main_order = (
        db.query(OrderQuote)
        .filter(
            OrderQuote.trip_id == trip.trip_id,
            OrderQuote.order_type == "MAIN_TRIP",
        )
        .order_by(OrderQuote.order_id.desc())
        .first()
    )
    trip_owner = db.get(User, trip.user_id)
    draft = parse_draft_json(trip.llm_draft_json)
    gap_unlocked = is_trip_gap_unlocked(trip, db)
    gap_items = (
        db.query(GapItineraryItem)
        .filter(GapItineraryItem.trip_id == trip.trip_id)
        .order_by(GapItineraryItem.day_number.asc(), GapItineraryItem.item_id.asc())
        .all()
    )
    gap_lookup = {f"{item.day_number}::{item.time_slot}": item for item in gap_items}
    addon_orders = (
        db.query(OrderQuote)
        .filter(
            OrderQuote.trip_id == trip.trip_id,
            OrderQuote.order_type == "ADDON",
        )
        .order_by(OrderQuote.order_id.desc())
        .all()
    )

    context = build_nav_context(request, db)
    context.update(
        {
            "trip": trip,
            "trip_owner": trip_owner,
            "draft": draft,
            "main_order": main_order,
            "gap_unlocked": gap_unlocked,
            "gap_items": gap_items,
            "gap_lookup": gap_lookup,
            "addon_orders": addon_orders,
            "is_owner": current_user is not None and trip.user_id == current_user.user_id,
        }
    )
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="trip_detail.html",
        context=context,
    )


@router.post("/trips/{trip_id}/regenerate")
def regenerate_trip_draft(
    trip_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Regenerate the itinerary draft while the TripPlan is still in DRAFT status."""
    current_user = get_current_user(request, db)
    if current_user is None:
        set_flash(request, "error", "請先登入會員帳號。")
        return RedirectResponse(url="/login", status_code=303)

    trip = db.get(TripPlan, trip_id)
    if trip is None:
        raise HTTPException(status_code=404, detail="找不到指定的行程規劃")
    if trip.user_id != current_user.user_id:
        return PlainTextResponse("您沒有權限修改此筆行程規劃", status_code=403)

    if trip.status == "CANCELLED":
        set_flash(request, "error", "此行程已取消（退單），無法重新生成大架構。")
        return RedirectResponse(url=f"/trips/{trip.trip_id}", status_code=303)

    if trip.status != "DRAFT":
        set_flash(request, "info", "此行程大架構已確認或鎖定，無法重新生成。")
        return RedirectResponse(url=f"/trips/{trip.trip_id}", status_code=303)

    existing_draft = parse_draft_json(trip.llm_draft_json) or {}
    preference = str(existing_draft.get("preference") or "")
    desired_match_count = int(existing_draft.get("desired_match_count") or 2)
    custom_instruction = str(existing_draft.get("custom_instruction") or "")

    new_draft = generate_itinerary_draft(
        league_type=trip.league_type,
        start_date=trip.start_date,
        end_date=trip.end_date,
        need_flight=trip.need_flight,
        need_hotel=trip.need_hotel,
        preference=preference,
        desired_match_count=desired_match_count,
        custom_instruction=custom_instruction,
        structured_targets=existing_draft.get("targets") or None,
    )
    trip.llm_draft_json = json.dumps(new_draft, ensure_ascii=False)
    db.commit()

    set_flash(
        request,
        "success",
        f"已重新生成【{trip.league_type}】行程大架構（{new_draft['mode_label']}）。",
    )
    return RedirectResponse(url=f"/trips/{trip.trip_id}", status_code=303)


@router.post("/trips/{trip_id}/customize")
def customize_trip_draft(
    trip_id: int,
    request: Request,
    selected_match_keys: list[str] = Form(default=[]),
    custom_instruction: str = Form(""),
    desired_match_count: int = Form(2),
    action_mode: str = Form("instant"),
    db: Session = Depends(get_db),
):
    """Allow trip owner to toggle/add matches and instantly update or AI-replan a DRAFT trip."""
    current_user = get_current_user(request, db)
    if current_user is None:
        set_flash(request, "error", "請先登入會員帳號。")
        return RedirectResponse(url="/login", status_code=303)

    trip = db.get(TripPlan, trip_id)
    if trip is None:
        raise HTTPException(status_code=404, detail="找不到指定的行程規劃")
    if trip.user_id != current_user.user_id:
        return PlainTextResponse("您沒有權限調整此筆行程規劃", status_code=403)

    if trip.status != "DRAFT":
        set_flash(request, "info", "僅草稿 (DRAFT) 狀態的行程可調整場次與大架構。")
        return RedirectResponse(url=f"/trips/{trip.trip_id}", status_code=303)

    existing_draft = parse_draft_json(trip.llm_draft_json) or {}
    clamped_count = max(1, min(desired_match_count, 6))
    cleaned_instruction = custom_instruction.strip()

    known_matches: dict[str, dict[str, str]] = {}
    for m in existing_draft.get("matches") or []:
        if isinstance(m, dict):
            key = f"{m.get('date', '')}||{m.get('matchup', '')}||{m.get('venue', '')}"
            known_matches[key] = {
                "date": str(m.get("date") or ""),
                "kickoff_utc": str(m.get("kickoff_utc") or ""),
                "league": str(m.get("league") or ""),
                "region": str(m.get("region") or "") or infer_match_region(m, trip.league_type),
                "matchup": str(m.get("matchup") or ""),
                "venue": str(m.get("venue") or ""),
                "note": str(m.get("note") or "已列入本次觀賽清單"),
            }
    for ev in existing_draft.get("live_schedules") or []:
        if isinstance(ev, dict):
            ev_matchup = f"{ev.get('matchup', '')}（{ev.get('league', '')}）" if ev.get("league") else str(ev.get("matchup", ""))
            # Support matching both raw ESPN matchup and formatted matchup
            for candidate_matchup in (str(ev.get("matchup", "")), ev_matchup):
                key = f"{ev.get('date', '')}||{candidate_matchup}||{ev.get('venue', '')}"
                if key not in known_matches:
                    kickoff = f"（UTC {ev['kickoff_utc']}）" if ev.get("kickoff_utc") else ""
                    known_matches[key] = {
                        "date": str(ev.get("date") or ""),
                        "kickoff_utc": str(ev.get("kickoff_utc") or ""),
                        "league": str(ev.get("league") or ""),
                        "region": str(ev.get("region") or "") or infer_match_region(ev, trip.league_type),
                        "matchup": candidate_matchup,
                        "venue": str(ev.get("venue") or "官方指定球場"),
                        "note": f"自 ESPN 即時賽程庫加選{kickoff}",
                    }

    selected_matches: list[dict[str, str]] = []
    seen_keys: set[str] = set()
    for raw_key in selected_match_keys:
        if raw_key in seen_keys:
            continue
        seen_keys.add(raw_key)
        if raw_key in known_matches:
            selected_matches.append(known_matches[raw_key])
        else:
            parts = raw_key.split("||", 2)
            if len(parts) == 3 and parts[1].strip():
                fallback_item = {
                    "date": parts[0].strip(),
                    "kickoff_utc": "",
                    "league": "",
                    "matchup": parts[1].strip(),
                    "venue": parts[2].strip() or "官方指定球場",
                    "note": "會員自選觀賽場次",
                }
                fallback_item["region"] = infer_match_region(fallback_item, trip.league_type)
                selected_matches.append(fallback_item)

    selected_matches.sort(key=lambda item: (item["date"], item.get("kickoff_utc", ""), item["matchup"]))

    is_cross_continent = bool(existing_draft.get("is_cross_continent")) or (
        len({infer_match_region(m, trip.league_type) for m in selected_matches}) > 1
    )
    feasibility = validate_schedule_feasibility(
        matches=selected_matches,
        start_date=trip.start_date,
        end_date=trip.end_date,
        need_flight=trip.need_flight,
        league_type=trip.league_type,
        is_cross_continent=is_cross_continent,
    )
    if feasibility["errors"]:
        set_flash(
            request,
            "error",
            "排程可行性檢查未通過（已保留原草稿，請調整場次）："
            + " ".join(feasibility["errors"]),
        )
        return RedirectResponse(url=f"/trips/{trip.trip_id}", status_code=303)

    if action_mode == "ai_replan":
        existing_diag = existing_draft.get("diagnostics") or {}
        existing_meta = {
            "total_found": existing_diag.get("espn_total_found"),
            "strategy": existing_diag.get("espn_strategy"),
            "matched_team": existing_diag.get("espn_matched_team"),
            "covered_range": existing_diag.get("espn_covered_range"),
        }
        updated_draft = generate_itinerary_draft(
            league_type=trip.league_type,
            start_date=trip.start_date,
            end_date=trip.end_date,
            need_flight=trip.need_flight,
            need_hotel=trip.need_hotel,
            preference=str(existing_draft.get("preference") or ""),
            desired_match_count=clamped_count,
            locked_matches=selected_matches,
            custom_instruction=cleaned_instruction,
            existing_live_schedules=existing_draft.get("live_schedules"),
            existing_search_sources=existing_draft.get("search_sources"),
            existing_espn_meta=existing_meta,
            structured_targets=existing_draft.get("targets") or None,
        )
        flash_msg = (
            f"AI 已依您鎖定的 {len(selected_matches)} 場賽事與調整指令重新規劃大架構"
            f"（{updated_draft['mode_label']}）。"
        )
    else:
        updated_draft = apply_match_selection_instantly(
            existing_draft=existing_draft,
            selected_matches=selected_matches,
            league_type=trip.league_type,
            start_date=trip.start_date,
            end_date=trip.end_date,
            need_flight=trip.need_flight,
            need_hotel=trip.need_hotel,
            desired_match_count=clamped_count,
            custom_instruction=cleaned_instruction,
        )
        flash_msg = f"已即時套用 {len(selected_matches)} 場觀賽選擇並重組結構化行程表（0 秒生效）。"

    trip.llm_draft_json = json.dumps(updated_draft, ensure_ascii=False)
    db.commit()
    set_flash(request, "success", flash_msg)
    return RedirectResponse(url=f"/trips/{trip.trip_id}", status_code=303)


@router.post("/trips/{trip_id}/confirm")
def confirm_trip_plan(
    trip_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Confirm itinerary draft and create a MAIN_TRIP QUOTING OrderQuote for AG001."""
    current_user = get_current_user(request, db)
    if current_user is None:
        set_flash(request, "error", "請先登入會員帳號。")
        return RedirectResponse(url="/login", status_code=303)

    trip = db.get(TripPlan, trip_id)
    if trip is None:
        raise HTTPException(status_code=404, detail="找不到指定的行程規劃")
    if trip.user_id != current_user.user_id:
        return PlainTextResponse("您沒有權限確認此筆行程規劃", status_code=403)

    existing_order = (
        db.query(OrderQuote)
        .filter(
            OrderQuote.trip_id == trip.trip_id,
            OrderQuote.order_type == "MAIN_TRIP",
        )
        .first()
    )

    if trip.status == "CANCELLED":
        set_flash(request, "error", "此行程已取消（退單），無法送出確認詢價。")
        return RedirectResponse(url=f"/trips/{trip.trip_id}", status_code=303)

    if trip.status != "DRAFT":
        if existing_order is None:
            agent = db.get(Agent, DEFAULT_AGENT_ID)
            db.add(
                OrderQuote(
                    trip_id=trip.trip_id,
                    agent_id=agent.agent_id if agent else None,
                    order_type="MAIN_TRIP",
                    quoted_price=None,
                    order_status="QUOTING",
                )
            )
            db.commit()
        set_flash(request, "info", "此行程大架構已完成確認並送出待報價詢價單。")
        return RedirectResponse(url=f"/trips/{trip.trip_id}", status_code=303)

    existing_draft = parse_draft_json(trip.llm_draft_json) or {}
    draft_matches = existing_draft.get("matches") or []
    if isinstance(draft_matches, list):
        feasibility = validate_schedule_feasibility(
            matches=draft_matches,
            start_date=trip.start_date,
            end_date=trip.end_date,
            need_flight=trip.need_flight,
            league_type=trip.league_type,
            is_cross_continent=bool(existing_draft.get("is_cross_continent")),
        )
        if feasibility["errors"]:
            set_flash(
                request,
                "error",
                "無法確認送出報價，請先排除排程衝突：" + " ".join(feasibility["errors"]),
            )
            return RedirectResponse(url=f"/trips/{trip.trip_id}", status_code=303)

    trip.status = "CONFIRMED"
    agent = db.get(Agent, DEFAULT_AGENT_ID)
    if existing_order is None:
        existing_order = OrderQuote(
            trip_id=trip.trip_id,
            agent_id=agent.agent_id if agent else None,
            order_type="MAIN_TRIP",
            quoted_price=None,
            order_status="QUOTING",
        )
        db.add(existing_order)
    db.commit()
    db.refresh(existing_order)

    mail_res = send_quoting_request_email(
        order=existing_order,
        trip=trip,
        user=current_user,
        agent=agent or existing_order.agent,
    )

    set_flash(
        request,
        "success",
        f"已確認行程大架構！已建立待報價詢價單 #{existing_order.order_id} 送交訂票專員 ({existing_order.agent_id or 'AG001'})。{mail_res['note']}",
    )
    return RedirectResponse(url=f"/trips/{trip.trip_id}", status_code=303)


@router.post("/trips/{trip_id}/cancel")
def cancel_trip_plan(
    trip_id: int,
    request: Request,
    next_page: str = Form("detail"),
    db: Session = Depends(get_db),
):
    """Allow the trip owner to cancel (withdraw) an unpaid DRAFT or CONFIRMED trip."""
    current_user = get_current_user(request, db)
    if current_user is None:
        set_flash(request, "error", "請先登入會員帳號。")
        return RedirectResponse(url="/login", status_code=303)

    trip = db.get(TripPlan, trip_id)
    if trip is None:
        raise HTTPException(status_code=404, detail="找不到指定的行程規劃")
    if trip.user_id != current_user.user_id:
        return PlainTextResponse("您沒有權限退單此筆行程", status_code=403)

    target_url = "/" if next_page == "home" else f"/trips/{trip.trip_id}"

    if trip.status == "CANCELLED":
        set_flash(request, "info", "此行程規劃已經取消（退單）。")
        return RedirectResponse(url=target_url, status_code=303)

    has_paid_or_booked = any(
        order.order_status in ("PAID", "BOOKED") for order in trip.order_quotes
    )
    if trip.status == "LOCKED" or has_paid_or_booked:
        set_flash(
            request,
            "error",
            "此行程訂單已完成付款或進入代訂作業，無法直接退單，請改由異常／退款流程處理。",
        )
        return RedirectResponse(url=f"/trips/{trip.trip_id}", status_code=303)

    trip.status = "CANCELLED"
    for order in trip.order_quotes:
        if order.order_status in ("QUOTING", "QUOTED"):
            order.order_status = "CANCELLED"
    db.commit()

    set_flash(
        request,
        "info",
        f"已取消（退單）行程 #{trip.trip_id}（{trip.league_type}）。",
    )
    return RedirectResponse(url=target_url, status_code=303)


@router.post("/trips/{trip_id}/delete")
def delete_trip_plan(
    trip_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Allow an active booking agent (e.g. AG001) to permanently delete a trip and its orders."""
    if get_active_role(request) != "agent":
        return PlainTextResponse("僅限訂票專員視角執行刪單作業", status_code=403)

    trip = db.get(TripPlan, trip_id)
    if trip is None:
        raise HTTPException(status_code=404, detail="找不到指定的行程規劃")

    active_agent = get_active_agent(request, db)
    deleted_id = trip.trip_id
    deleted_league = trip.league_type

    for item in trip.gap_items:
        item.addon_order_id = None
    db.flush()

    db.delete(trip)
    db.commit()

    agent_label = active_agent.agent_id if active_agent else DEFAULT_AGENT_ID
    set_flash(
        request,
        "info",
        f"專員 {agent_label} 已刪除行程 #{deleted_id}（{deleted_league}）與其關聯訂單。",
    )
    return RedirectResponse(url="/", status_code=303)
