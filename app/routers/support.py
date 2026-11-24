"""Live customer support chat center and agent-assisted refund workflow routes."""

from datetime import timezone, timedelta

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
from app.database import get_db
from app.models import OrderQuote, SupportTicket, TripPlan
from app.services.mailer import send_support_refund_decision_email
from app.services.support import (
    SUPPORT_CATEGORIES,
    approve_support_refund,
    close_support_ticket,
    create_support_ticket,
    ensure_default_agent,
    post_support_message,
    reject_support_refund,
)

router = APIRouter()
_TAIPEI_TZ = timezone(timedelta(hours=8))


def _format_taipei(dt) -> str:
    if dt is None:
        return "—"
    aware = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    return aware.astimezone(_TAIPEI_TZ).strftime("%Y-%m-%d %H:%M")


def _load_authorized_ticket(
    ticket_id: int,
    request: Request,
    db: Session,
    *,
    require_agent: bool = False,
):
    """Authorize access to a SupportTicket for owner member or active booking agent."""
    active_role = get_active_role(request)
    current_user = get_current_user(request, db)

    if require_agent and active_role != "agent":
        return None, PlainTextResponse("僅限真人客服／訂票專員視角操作退款審核", status_code=403)

    if active_role != "agent":
        if current_user is None:
            set_flash(request, "error", "請先登入會員帳號再使用客服聯絡系統。")
            return None, RedirectResponse(url="/login", status_code=303)

    ticket = db.get(SupportTicket, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="找不到指定的客服對話紀錄")

    if active_role != "agent" and ticket.user_id != current_user.user_id:
        return None, PlainTextResponse("您沒有權限檢視此筆客服對話", status_code=403)

    return (ticket, active_role, current_user), None


@router.get("/support", response_class=HTMLResponse)
def support_center_page(
    request: Request,
    ticket_id: int | None = None,
    order_id: int | None = None,
    category: str = "",
    db: Session = Depends(get_db),
):
    """Render two-column support chat center (member inquiries + agent refund review)."""
    active_role = get_active_role(request)
    current_user = get_current_user(request, db)

    if active_role != "agent" and current_user is None:
        set_flash(request, "error", "請先登入會員帳號，即可與專屬真人客服聯絡或申請退款。")
        return RedirectResponse(url="/login", status_code=303)

    if active_role == "agent":
        all_tickets = (
            db.query(SupportTicket)
            .order_by(SupportTicket.updated_at.desc(), SupportTicket.ticket_id.desc())
            .all()
        )
        # Keep pending refund requests prominently at top of agent queue
        tickets = sorted(
            all_tickets,
            key=lambda t: (
                0 if t.status == "REFUND_REQUESTED" else (1 if t.status == "OPEN" else 2),
                -t.ticket_id,
            ),
        )
        member_orders: list[OrderQuote] = []
        refundable_orders: list[OrderQuote] = []
    else:
        tickets = (
            db.query(SupportTicket)
            .filter(SupportTicket.user_id == current_user.user_id)
            .order_by(SupportTicket.updated_at.desc(), SupportTicket.ticket_id.desc())
            .all()
        )
        member_orders = (
            db.query(OrderQuote)
            .join(TripPlan, OrderQuote.trip_id == TripPlan.trip_id)
            .filter(TripPlan.user_id == current_user.user_id)
            .order_by(OrderQuote.order_id.desc())
            .all()
        )
        refundable_orders = [
            o for o in member_orders if o.order_status in ("PAID", "BOOKED")
        ]

    active_ticket: SupportTicket | None = None
    if ticket_id is not None:
        candidate = db.get(SupportTicket, ticket_id)
        if candidate is None:
            raise HTTPException(status_code=404, detail="找不到指定的客服對話紀錄")
        if active_role != "agent" and candidate.user_id != current_user.user_id:
            return PlainTextResponse("您沒有權限檢視此筆客服對話", status_code=403)
        active_ticket = candidate
    elif tickets and order_id is None and not category:
        active_ticket = tickets[0]

    prefill_category = (category or "").strip().upper()
    if prefill_category not in SUPPORT_CATEGORIES:
        prefill_category = "REFUND" if order_id is not None else "GENERAL"

    prefill_order_id = order_id
    prefill_subject = ""
    if prefill_category == "REFUND" and prefill_order_id is not None:
        prefill_subject = f"申請訂單 #{prefill_order_id} 退款審核"

    context = build_nav_context(request, db)
    context.update(
        {
            "tickets": tickets,
            "active_ticket": active_ticket,
            "support_categories": SUPPORT_CATEGORIES,
            "member_orders": member_orders,
            "refundable_orders": refundable_orders,
            "prefill_category": prefill_category,
            "prefill_order_id": prefill_order_id,
            "prefill_subject": prefill_subject,
        }
    )
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="support_center.html",
        context=context,
    )


@router.post("/support/new")
def create_support_ticket_route(
    request: Request,
    category: str = Form("GENERAL"),
    order_id: str = Form(""),
    subject: str = Form(""),
    message: str = Form(""),
    db: Session = Depends(get_db),
):
    """Create a new member support inquiry or refund request ticket."""
    if get_active_role(request) == "agent":
        return PlainTextResponse("僅限會員視角發起客服對話或退款申請", status_code=403)

    current_user = get_current_user(request, db)
    if current_user is None:
        set_flash(request, "error", "請先登入會員帳號。")
        return RedirectResponse(url="/login", status_code=303)

    parsed_order_id: int | None = None
    clean_order_raw = order_id.strip()
    if clean_order_raw:
        try:
            parsed_order_id = int(clean_order_raw)
        except ValueError:
            set_flash(request, "error", "無效的訂單編號格式。")
            return RedirectResponse(url="/support", status_code=303)

    try:
        ticket = create_support_ticket(
            db=db,
            user=current_user,
            category=category,
            subject=subject,
            initial_message=message,
            order_id=parsed_order_id,
        )
    except ValueError as exc:
        set_flash(request, "error", str(exc))
        back_url = "/support"
        if parsed_order_id is not None:
            back_url = f"/support?order_id={parsed_order_id}&category={category}"
        return RedirectResponse(url=back_url, status_code=303)

    if ticket.status == "REFUND_REQUESTED":
        set_flash(
            request,
            "success",
            f"已送出退款申請（客服單 #{ticket.ticket_id}），您可繼續於下方與客服專員 {ticket.agent_id} 文字聯繫確認退款細節。",
        )
    else:
        set_flash(
            request,
            "success",
            f"客服對話 #{ticket.ticket_id} 已建立！請等候專員 {ticket.agent_id} 即時文字回覆。",
        )
    return RedirectResponse(url=f"/support?ticket_id={ticket.ticket_id}", status_code=303)


@router.post("/support/{ticket_id}/messages")
def send_support_message_route(
    ticket_id: int,
    request: Request,
    content: str = Form(""),
    db: Session = Depends(get_db),
):
    """Append a member or agent chat reply inside a support ticket."""
    authorized, early_resp = _load_authorized_ticket(ticket_id, request, db)
    if early_resp is not None:
        return early_resp
    ticket, active_role, current_user = authorized

    if active_role == "agent":
        agent = get_active_agent(request, db) or ensure_default_agent(db)
        sender_role = "AGENT"
        sender_name = f"客服專員 {agent.agent_id}（{agent.agent_name}）"
    else:
        sender_role = "MEMBER"
        sender_name = current_user.full_name

    try:
        post_support_message(
            db=db,
            ticket=ticket,
            sender_role=sender_role,
            sender_name=sender_name,
            content=content,
        )
    except ValueError as exc:
        set_flash(request, "error", str(exc))

    return RedirectResponse(url=f"/support?ticket_id={ticket.ticket_id}", status_code=303)


@router.get("/api/support/{ticket_id}/messages")
def poll_support_messages(
    ticket_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Lightweight JSON polling endpoint for live support message updates."""
    ticket = db.get(SupportTicket, ticket_id)
    if ticket is None:
        return JSONResponse({"error": "找不到客服對話"}, status_code=404)

    active_role = get_active_role(request)
    current_user = get_current_user(request, db)
    if active_role != "agent":
        if current_user is None or ticket.user_id != current_user.user_id:
            return JSONResponse({"error": "無權限讀取此對話"}, status_code=403)

    return {
        "ticket_id": ticket.ticket_id,
        "status": ticket.status,
        "messages": [
            {
                "message_id": m.message_id,
                "sender_role": m.sender_role,
                "sender_name": m.sender_name,
                "content": m.content,
                "created_at": _format_taipei(m.created_at),
            }
            for m in ticket.messages
        ],
    }


@router.post("/support/{ticket_id}/refund/approve")
def approve_refund_route(
    ticket_id: int,
    request: Request,
    approved_amount: str = Form(""),
    agent_note: str = Form(""),
    db: Session = Depends(get_db),
):
    """Agent approves a support refund request, executing D4 mock refund and notifying member."""
    authorized, early_resp = _load_authorized_ticket(
        ticket_id, request, db, require_agent=True
    )
    if early_resp is not None:
        return early_resp
    ticket, _role, _user = authorized

    agent = get_active_agent(request, db) or ensure_default_agent(db)
    cleaned_amt = approved_amount.replace(",", "").strip()
    try:
        parsed_amt = (
            int(cleaned_amt)
            if cleaned_amt
            else (ticket.refund_amount_requested or (ticket.order_quote.quoted_price if ticket.order_quote else 0) or 0)
        )
    except ValueError:
        parsed_amt = -1

    try:
        updated_ticket, refund_txn = approve_support_refund(
            db=db,
            ticket=ticket,
            agent=agent,
            approved_amount=parsed_amt,
            agent_note=agent_note,
        )
    except ValueError as exc:
        set_flash(request, "error", str(exc))
        return RedirectResponse(url=f"/support?ticket_id={ticket.ticket_id}", status_code=303)

    order = updated_ticket.order_quote
    mail_note = ""
    if order is not None and order.trip_plan is not None:
        mail_res = send_support_refund_decision_email(
            order=order,
            trip=order.trip_plan,
            user=updated_ticket.user,
            agent=agent,
            approved=True,
            refund_txn=refund_txn,
            note=agent_note.strip(),
        )
        mail_note = mail_res.get("note", "")

    set_flash(
        request,
        "success",
        f"專員 {agent.agent_id} 已核准並執行模擬退款 NT$ {refund_txn.amount:,}（編號 {refund_txn.gateway_ref}）！{mail_note}",
    )
    return RedirectResponse(url=f"/support?ticket_id={updated_ticket.ticket_id}", status_code=303)


@router.post("/support/{ticket_id}/refund/reject")
def reject_refund_route(
    ticket_id: int,
    request: Request,
    reject_reason: str = Form(""),
    db: Session = Depends(get_db),
):
    """Agent rejects a support refund request with written explanation."""
    authorized, early_resp = _load_authorized_ticket(
        ticket_id, request, db, require_agent=True
    )
    if early_resp is not None:
        return early_resp
    ticket, _role, _user = authorized

    agent = get_active_agent(request, db) or ensure_default_agent(db)
    try:
        updated_ticket = reject_support_refund(
            db=db,
            ticket=ticket,
            agent=agent,
            reason=reject_reason,
        )
    except ValueError as exc:
        set_flash(request, "error", str(exc))
        return RedirectResponse(url=f"/support?ticket_id={ticket.ticket_id}", status_code=303)

    order = updated_ticket.order_quote
    mail_note = ""
    if order is not None and order.trip_plan is not None:
        mail_res = send_support_refund_decision_email(
            order=order,
            trip=order.trip_plan,
            user=updated_ticket.user,
            agent=agent,
            approved=False,
            note=reject_reason.strip(),
        )
        mail_note = mail_res.get("note", "")

    set_flash(
        request,
        "info",
        f"專員 {agent.agent_id} 已駁回該筆退款申請，說明已寫入對話視窗並維持原訂單有效。{mail_note}",
    )
    return RedirectResponse(url=f"/support?ticket_id={updated_ticket.ticket_id}", status_code=303)


@router.post("/support/{ticket_id}/close")
def close_support_ticket_route(
    ticket_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Close an open or resolved support ticket."""
    authorized, early_resp = _load_authorized_ticket(ticket_id, request, db)
    if early_resp is not None:
        return early_resp
    ticket, active_role, current_user = authorized

    if active_role == "agent":
        agent = get_active_agent(request, db) or ensure_default_agent(db)
        actor_label = f"客服專員 {agent.agent_id}"
    else:
        actor_label = f"會員 {current_user.full_name}"

    try:
        close_support_ticket(db=db, ticket=ticket, actor_label=actor_label)
    except ValueError as exc:
        set_flash(request, "error", str(exc))
        return RedirectResponse(url=f"/support?ticket_id={ticket.ticket_id}", status_code=303)

    set_flash(request, "info", f"客服單 #{ticket.ticket_id} 已標記結案。")
    return RedirectResponse(url=f"/support?ticket_id={ticket.ticket_id}", status_code=303)
