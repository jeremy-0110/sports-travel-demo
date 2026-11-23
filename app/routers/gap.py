"""Post-payment free gap itinerary planning & attraction addon routes (DFD 6.0)."""

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.auth import (
    build_nav_context,
    get_active_role,
    get_current_user,
    set_flash,
)
from app.config import DEFAULT_AGENT_ID
from app.database import get_db
from app.models import GapItineraryItem, OrderQuote, TripPlan
from app.services.gap_planner import (
    create_addon_order_for_gap_items,
    generate_gap_suggestions,
    is_trip_gap_unlocked,
    persist_gap_suggestions_to_draft,
    save_gap_selections,
)
from app.services.mailer import send_quoting_request_email
from app.services.planner import parse_draft_json

router = APIRouter()


def _authorize_gap_access(
    trip_id: int,
    request: Request,
    db: Session,
    *,
    allow_agent_read: bool = False,
):
    """Validate ownership/role and post-payment unlock state for /trips/{trip_id}/gap routes."""
    trip = db.get(TripPlan, trip_id)
    if trip is None:
        raise HTTPException(status_code=404, detail="找不到指定的行程規劃")

    active_role = get_active_role(request)
    current_user = get_current_user(request, db)

    if active_role == "agent":
        if not allow_agent_read:
            return None, PlainTextResponse("僅限行程會員本人操作免費空檔規劃", status_code=403)
    else:
        if current_user is None:
            set_flash(request, "error", "請先登入會員帳號以檢視並規劃免費空檔行程。")
            return None, RedirectResponse(url="/login", status_code=303)
        if trip.user_id != current_user.user_id:
            return None, PlainTextResponse("您沒有權限操作此筆行程的空檔規劃", status_code=403)

    if not is_trip_gap_unlocked(trip, db):
        set_flash(
            request,
            "error",
            "免費空檔行程規劃須待主行程「全額付款成功（PAID / BOOKED）」後才會解鎖！",
        )
        return None, RedirectResponse(url=f"/trips/{trip.trip_id}", status_code=303)

    return (trip, current_user), None


@router.get("/trips/{trip_id}/gap", response_class=HTMLResponse)
def gap_planner_page(
    trip_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Display unlocked free gap-time itinerary options, user preference prompt, and ADDON checkout."""
    authorized, early_resp = _authorize_gap_access(trip_id, request, db, allow_agent_read=True)
    if early_resp is not None:
        return early_resp
    trip, current_user = authorized

    draft = parse_draft_json(trip.llm_draft_json)
    cached = draft.get("gap_suggestions") if isinstance(draft, dict) else None
    if isinstance(cached, dict) and isinstance(cached.get("slots"), list) and cached["slots"]:
        gap_suggestions = cached
    else:
        initial_pref = str(draft.get("preference") or "") if isinstance(draft, dict) else ""
        gap_suggestions = generate_gap_suggestions(
            trip,
            draft,
            preference=initial_pref,
            force_refresh=False,
        )
        draft = persist_gap_suggestions_to_draft(db, trip, gap_suggestions)

    gap_items = (
        db.query(GapItineraryItem)
        .filter(GapItineraryItem.trip_id == trip.trip_id)
        .order_by(GapItineraryItem.day_number.asc(), GapItineraryItem.item_id.asc())
        .all()
    )
    saved_by_slot = {f"{item.day_number}::{item.time_slot}": item for item in gap_items}
    pending_addon_items = [
        item for item in gap_items if item.need_ticket_addon and item.addon_order_id is None
    ]
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
            "draft": draft,
            "gap_suggestions": gap_suggestions,
            "gap_items": gap_items,
            "saved_by_slot": saved_by_slot,
            "pending_addon_items": pending_addon_items,
            "addon_orders": addon_orders,
            "is_owner": current_user is not None and trip.user_id == current_user.user_id,
        }
    )
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="gap_planner.html",
        context=context,
    )


@router.post("/trips/{trip_id}/gap/suggest")
def regenerate_gap_suggestions(
    trip_id: int,
    request: Request,
    gap_preference: str = Form(""),
    db: Session = Depends(get_db),
):
    """Regenerate Gemini/Mock per-slot activity recommendations based on user's preference prompt."""
    authorized, early_resp = _authorize_gap_access(trip_id, request, db, allow_agent_read=False)
    if early_resp is not None:
        return early_resp
    trip, _ = authorized

    draft = parse_draft_json(trip.llm_draft_json)
    suggestions = generate_gap_suggestions(
        trip,
        draft,
        preference=gap_preference,
        force_refresh=True,
    )
    persist_gap_suggestions_to_draft(db, trip, suggestions)

    if suggestions.get("source") == "GEMINI":
        set_flash(
            request,
            "success",
            f"已透過 Gemini（{suggestions.get('model_used')}）依據您的偏好重新生成每個空檔時段的推薦方案！",
        )
    else:
        set_flash(
            request,
            "info",
            "已依據您的旅遊偏好重新整理每個空檔時段的 3 個推薦方案（目前以內建防呆推薦模式運作）。",
        )
    return RedirectResponse(url=f"/trips/{trip.trip_id}/gap", status_code=303)


@router.post("/trips/{trip_id}/gap/save")
async def save_gap_itinerary(
    trip_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Persist user's chosen gap-time activities & ticket-addon checkboxes into D2 GapItineraryItem."""
    authorized, early_resp = _authorize_gap_access(trip_id, request, db, allow_agent_read=False)
    if early_resp is not None:
        return early_resp
    trip, _ = authorized

    form_data = await request.form()
    try:
        summary = save_gap_selections(db, trip, form_data)
    except ValueError as exc:
        set_flash(request, "error", str(exc))
        return RedirectResponse(url=f"/trips/{trip.trip_id}/gap", status_code=303)

    msg = (
        f"已儲存免費空檔行程（新增／更新 {summary['saved_count']} 個時段，"
        f"其中 {summary['addon_count']} 項勾選需要專員代訂門票）！行程表上的「🔒 空檔」已解鎖替換。"
    )
    if summary["preserved_locked_count"] > 0:
        msg += f"（另保留 {summary['preserved_locked_count']} 個已送出加購訂單的鎖定時段。）"
    set_flash(request, "success", msg)
    return RedirectResponse(url=f"/trips/{trip.trip_id}/gap", status_code=303)


@router.post("/trips/{trip_id}/gap/addon-order")
def submit_gap_addon_order(
    trip_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Create a DFD 3.0 ADDON OrderQuote for all pending ticket-addon GapItineraryItem rows."""
    authorized, early_resp = _authorize_gap_access(trip_id, request, db, allow_agent_read=False)
    if early_resp is not None:
        return early_resp
    trip, current_user = authorized

    try:
        addon_order = create_addon_order_for_gap_items(db, trip)
    except ValueError as exc:
        set_flash(request, "error", str(exc))
        return RedirectResponse(url=f"/trips/{trip.trip_id}/gap", status_code=303)

    mail_result = send_quoting_request_email(
        addon_order,
        trip,
        current_user,
        addon_order.agent,
    )
    addon_count = len(addon_order.addon_gap_items)
    set_flash(
        request,
        "success",
        f"已為 {addon_count} 項景點門票建立加購詢價訂單 #{addon_order.order_id}（ADDON / QUOTING），"
        f"通知已發送給專員 {addon_order.agent_id or DEFAULT_AGENT_ID}（{mail_result.get('mode_label', 'Mock')}）。",
    )
    return RedirectResponse(url=f"/orders/{addon_order.order_id}", status_code=303)
