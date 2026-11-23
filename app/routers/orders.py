"""Agent one-price quote management (DFD 3.0) and mock full-payment routes (DFD 4.0)."""

import json

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.auth import (
    build_nav_context,
    get_active_agent,
    get_active_role,
    get_current_user,
    set_flash,
)
from app.config import DEFAULT_AGENT_ID
from app.database import get_db
from app.models import BookingException, BookingVoucher, OrderQuote, PaymentTxn
from app.services.fulfillment import (
    EXCEPTION_REASON_PRESETS,
    LEAGUE_TICKET_FORMATS,
    VOUCHER_CATEGORIES,
    build_voucher_view_items,
    compute_fulfillment_checklist,
    get_traveler_profile,
    infer_league_ticket_format,
    validate_traveler_profile,
)
from app.services.mailer import (
    get_order_email_logs,
    send_payment_failed_email,
    send_payment_success_emails,
    send_quote_ready_email,
)
from app.services.payment import PAYMENT_METHODS, process_mock_payment
from app.services.planner import parse_draft_json

router = APIRouter()

MIN_QUOTE_PRICE = 1000
MAX_QUOTE_PRICE = 5_000_000


@router.get("/orders/{order_id}", response_class=HTMLResponse)
def order_detail_page(
    order_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Display formal one-price quote details, agent quoting form, payment form, and D4 txns."""
    order = db.get(OrderQuote, order_id)
    if order is None or order.trip_plan is None:
        raise HTTPException(status_code=404, detail="找不到指定的報價／訂單資料")

    trip = order.trip_plan
    active_role = get_active_role(request)
    current_user = get_current_user(request, db)

    if active_role != "agent":
        if current_user is None:
            set_flash(request, "error", "請先登入會員帳號以檢視報價單與付款作業。")
            return RedirectResponse(url="/login", status_code=303)
        if trip.user_id != current_user.user_id:
            return PlainTextResponse("您沒有權限檢視此筆訂單", status_code=403)

    txns = (
        db.query(PaymentTxn)
        .filter(PaymentTxn.order_id == order.order_id)
        .order_by(PaymentTxn.txn_id.desc())
        .all()
    )
    draft = parse_draft_json(trip.llm_draft_json)
    suggested_price = (
        order.quoted_price
        or (draft.get("estimated_budget_twd") if isinstance(draft, dict) else None)
        or 68000
    )
    latest_success_txn = next(
        (t for t in txns if t.status == "SUCCESS" and t.txn_type == "PAYMENT"),
        None,
    )
    raw_vouchers = (
        db.query(BookingVoucher)
        .filter(BookingVoucher.order_id == order.order_id)
        .order_by(BookingVoucher.voucher_id.asc())
        .all()
    )
    voucher_items = build_voucher_view_items(raw_vouchers)
    exceptions = (
        db.query(BookingException)
        .filter(BookingException.order_id == order.order_id)
        .order_by(BookingException.exception_id.desc())
        .all()
    )
    pending_exception = next(
        (e for e in exceptions if e.user_decision == "PENDING"),
        None,
    )
    traveler_profile = get_traveler_profile(draft)
    fulfillment_checklist = compute_fulfillment_checklist(db, order)
    default_league_format = infer_league_ticket_format(trip.league_type)
    default_traveler_form = {
        "passport_name": (traveler_profile.get("passport_name") if traveler_profile else "") or "",
        "passport_number": (traveler_profile.get("passport_number") if traveler_profile else "") or "",
        "nationality": (traveler_profile.get("nationality") if traveler_profile else "") or "Taiwan (TWN)",
        "birth_date": (traveler_profile.get("birth_date") if traveler_profile else "") or "",
        "passport_expiry": (traveler_profile.get("passport_expiry") if traveler_profile else "") or "",
        "contact_phone": (
            (traveler_profile.get("contact_phone") if traveler_profile else "")
            or (trip.user.phone if trip.user else "")
            or ""
        ),
        "ticket_email": (
            (traveler_profile.get("ticket_email") if traveler_profile else "")
            or (trip.user.email if trip.user else "")
            or ""
        ),
    }

    context = build_nav_context(request, db)
    context.update(
        {
            "order": order,
            "trip": trip,
            "trip_owner": trip.user,
            "assigned_agent": order.agent,
            "draft": draft,
            "txns": txns,
            "latest_success_txn": latest_success_txn,
            "voucher_items": voucher_items,
            "exceptions": exceptions,
            "pending_exception": pending_exception,
            "voucher_categories": VOUCHER_CATEGORIES,
            "league_ticket_formats": LEAGUE_TICKET_FORMATS,
            "default_league_format": default_league_format,
            "traveler_profile": traveler_profile,
            "default_traveler_form": default_traveler_form,
            "fulfillment_checklist": fulfillment_checklist,
            "exception_reason_presets": EXCEPTION_REASON_PRESETS,
            "suggested_price": suggested_price,
            "payment_methods": PAYMENT_METHODS,
            "email_logs": get_order_email_logs(order.order_id),
            "is_owner": current_user is not None and trip.user_id == current_user.user_id,
        }
    )
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="order_detail.html",
        context=context,
    )


@router.post("/orders/{order_id}/quote")
def submit_order_quote(
    order_id: int,
    request: Request,
    quoted_price: str = Form(""),
    quote_note: str = Form(""),
    db: Session = Depends(get_db),
):
    """Allow an active booking agent (e.g. AG001) to issue or update a one-price quote."""
    if get_active_role(request) != "agent":
        return PlainTextResponse("僅限訂票專員視角開立正式一口價報價單", status_code=403)

    order = db.get(OrderQuote, order_id)
    if order is None or order.trip_plan is None:
        raise HTTPException(status_code=404, detail="找不到指定的報價／訂單資料")

    if order.order_status not in ("QUOTING", "QUOTED"):
        set_flash(
            request,
            "error",
            f"此訂單目前狀態為 {order.order_status}，不可再修改或重開報價單。",
        )
        return RedirectResponse(url=f"/orders/{order.order_id}", status_code=303)

    cleaned_price = quoted_price.replace(",", "").strip()
    try:
        parsed_price = int(cleaned_price)
    except ValueError:
        parsed_price = -1

    if not (MIN_QUOTE_PRICE <= parsed_price <= MAX_QUOTE_PRICE):
        set_flash(
            request,
            "error",
            f"請輸入合法的一口價整數金額（NT$ {MIN_QUOTE_PRICE:,} ～ NT$ {MAX_QUOTE_PRICE:,}）。",
        )
        return RedirectResponse(url=f"/orders/{order.order_id}", status_code=303)

    active_agent = get_active_agent(request, db)
    order.agent_id = active_agent.agent_id if active_agent else DEFAULT_AGENT_ID
    order.quoted_price = parsed_price
    order.order_status = "QUOTED"
    db.commit()
    db.refresh(order)

    mail_res = send_quote_ready_email(
        order=order,
        trip=order.trip_plan,
        user=order.trip_plan.user,
        agent=active_agent or order.agent,
        quote_note=quote_note.strip(),
    )

    set_flash(
        request,
        "success",
        f"專員 {order.agent_id} 已開立一口價報價單 NT$ {parsed_price:,}（無訂金、一次付清）！{mail_res['note']}",
    )
    return RedirectResponse(url=f"/orders/{order.order_id}", status_code=303)


@router.post("/orders/{order_id}/pay")
def pay_order(
    order_id: int,
    request: Request,
    payment_method: str = Form("CREDIT_CARD"),
    simulate_outcome: str = Form("SUCCESS"),
    passport_name: str = Form(""),
    passport_number: str = Form(""),
    nationality: str = Form(""),
    birth_date: str = Form(""),
    passport_expiry: str = Form(""),
    contact_phone: str = Form(""),
    ticket_email: str = Form(""),
    db: Session = Depends(get_db),
):
    """Validate mandatory traveler passport KYC & execute one-shot mock payment (DFD 4.0)."""
    if get_active_role(request) == "agent":
        return PlainTextResponse("請先切換回會員視角再執行付款作業", status_code=403)

    current_user = get_current_user(request, db)
    if current_user is None:
        set_flash(request, "error", "請先登入會員帳號再執行付款。")
        return RedirectResponse(url="/login", status_code=303)

    order = db.get(OrderQuote, order_id)
    if order is None or order.trip_plan is None:
        raise HTTPException(status_code=404, detail="找不到指定的報價／訂單資料")

    trip = order.trip_plan
    if trip.user_id != current_user.user_id:
        return PlainTextResponse("您沒有權限為此筆訂單付款", status_code=403)

    if order.order_status in ("PAID", "BOOKED"):
        set_flash(request, "info", "此筆訂單已完成全額付款，請勿重複刷卡扣款。")
        return RedirectResponse(url=f"/orders/{order.order_id}", status_code=303)

    if order.order_status != "QUOTED" or order.quoted_price is None or order.quoted_price <= 0:
        set_flash(
            request,
            "error",
            f"此筆訂單目前狀態為 {order.order_status}（尚未完成報價或已取消），無法付款。",
        )
        return RedirectResponse(url=f"/orders/{order.order_id}", status_code=303)

    draft_dict = parse_draft_json(trip.llm_draft_json) or {}
    existing_profile = get_traveler_profile(draft_dict)
    has_submitted_profile_fields = any(
        val.strip()
        for val in (passport_name, passport_number, birth_date, passport_expiry)
    )

    if not has_submitted_profile_fields and existing_profile is not None:
        validated_profile = existing_profile
    else:
        validated_profile, kyc_err = validate_traveler_profile(
            {
                "passport_name": passport_name,
                "passport_number": passport_number,
                "nationality": nationality,
                "birth_date": birth_date,
                "passport_expiry": passport_expiry,
                "contact_phone": contact_phone,
                "ticket_email": ticket_email,
            },
            trip_end_date=trip.end_date,
        )
        if kyc_err is not None or validated_profile is None:
            set_flash(
                request,
                "error",
                f"旅客護照基本資料未通過檢核（尚未扣款）：{kyc_err}",
            )
            return RedirectResponse(url=f"/orders/{order.order_id}", status_code=303)

        draft_dict["traveler_profile"] = validated_profile
        trip.llm_draft_json = json.dumps(draft_dict, ensure_ascii=False)
        db.commit()

    txn = process_mock_payment(
        db=db,
        order=order,
        payment_method=payment_method,
        simulate_outcome=simulate_outcome,
    )

    if txn.status == "SUCCESS":
        member_mail, _agent_mail = send_payment_success_emails(
            order=order,
            trip=trip,
            user=current_user,
            agent=order.agent,
            txn=txn,
        )
        set_flash(
            request,
            "success",
            f"全額付款成功！交易編號 {txn.gateway_ref}，實付金額 NT$ {txn.amount:,}。"
            f"行程 #{trip.trip_id} 已鎖定並解鎖免費空檔行程權限（Phase 5 開放規劃）。{member_mail['note']}",
        )
    else:
        fail_mail = send_payment_failed_email(
            order=order,
            trip=trip,
            user=current_user,
            txn=txn,
        )
        set_flash(
            request,
            "error",
            f"模擬金流交易失敗（交易編號 {txn.gateway_ref}），尚未扣款，請更換付款方式或重試。{fail_mail['note']}",
        )

    return RedirectResponse(url=f"/orders/{order.order_id}", status_code=303)
