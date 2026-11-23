"""DFD 5.0 routes: Agoda-style & league-specific booking vouchers and exception resolution."""

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
from app.database import get_db
from app.models import BookingException, BookingVoucher, OrderQuote
from app.services.fulfillment import (
    VOUCHER_CATEGORIES,
    auto_issue_trip_vouchers,
    build_voucher_view_items,
    compute_fulfillment_checklist,
    delete_voucher,
    get_traveler_profile,
    issue_voucher,
    report_booking_exception,
    resolve_booking_exception,
)
from app.services.mailer import (
    get_order_email_logs,
    send_exception_alert_email,
    send_exception_resolved_emails,
    send_voucher_issued_email,
)
from app.services.planner import parse_draft_json

router = APIRouter()


@router.get("/orders/{order_id}/vouchers", response_class=HTMLResponse)
def voucher_view_page(
    order_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Render printable industry-authentic confirmation vouchers for an order."""
    order = db.get(OrderQuote, order_id)
    if order is None or order.trip_plan is None:
        raise HTTPException(status_code=404, detail="找不到指定的訂單資料")

    trip = order.trip_plan
    active_role = get_active_role(request)
    current_user = get_current_user(request, db)
    if active_role != "agent":
        if current_user is None:
            set_flash(request, "error", "請先登入會員帳號以檢視訂票確認憑證。")
            return RedirectResponse(url="/login", status_code=303)
        if trip.user_id != current_user.user_id:
            return PlainTextResponse("您沒有權限檢視此筆訂票憑證", status_code=403)

    raw_vouchers = (
        db.query(BookingVoucher)
        .filter(BookingVoucher.order_id == order.order_id)
        .order_by(BookingVoucher.voucher_id.asc())
        .all()
    )
    voucher_items = build_voucher_view_items(raw_vouchers)
    draft = parse_draft_json(trip.llm_draft_json)
    traveler_profile = get_traveler_profile(draft)

    context = build_nav_context(request, db)
    context.update(
        {
            "order": order,
            "trip": trip,
            "trip_owner": trip.user,
            "assigned_agent": order.agent,
            "voucher_items": voucher_items,
            "traveler_profile": traveler_profile,
            "email_logs": get_order_email_logs(order.order_id),
        }
    )
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="voucher_view.html",
        context=context,
    )


@router.post("/orders/{order_id}/vouchers")
async def create_voucher(
    order_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Allow an active agent to manually issue a strict category-specific BookingVoucher."""
    if get_active_role(request) != "agent":
        return PlainTextResponse("僅限訂票專員視角核發訂票憑證", status_code=403)

    order = db.get(OrderQuote, order_id)
    if order is None or order.trip_plan is None:
        raise HTTPException(status_code=404, detail="找不到指定的訂單資料")

    form = await request.form()
    fields = {str(k): str(v) for k, v in form.items()}
    category = fields.get("category", "MATCH")
    booking_code = fields.get("booking_code", "")

    try:
        voucher = issue_voucher(
            db=db,
            order=order,
            category=category,
            fields=fields,
            booking_code=booking_code,
        )
    except ValueError as exc:
        set_flash(request, "error", str(exc))
        return RedirectResponse(url=f"/orders/{order.order_id}", status_code=303)

    active_agent = get_active_agent(request, db)
    mail_res = send_voucher_issued_email(
        order=order,
        trip=order.trip_plan,
        user=order.trip_plan.user,
        agent=active_agent or order.agent,
        vouchers=[voucher],
    )
    checklist = compute_fulfillment_checklist(db, order)
    cat_label = VOUCHER_CATEGORIES[voucher.category]["badge"]
    if checklist["is_complete"]:
        progress_note = "全套應開項目皆已開齊，訂單已正式轉為 BOOKED！"
    else:
        missing = "、".join(checklist["missing_labels"])
        progress_note = f"目前開票進度 {checklist['issued_required']}/{checklist['required_total']}（尚缺：{missing}），補齊後將自動轉為 BOOKED。"

    set_flash(
        request,
        "success",
        f"已成功核發【{cat_label}】憑證（{voucher.booking_code}）！{progress_note}{mail_res['note']}",
    )
    return RedirectResponse(url=f"/orders/{order.order_id}", status_code=303)


@router.post("/orders/{order_id}/vouchers/auto-issue")
def auto_issue_vouchers(
    order_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """One-click issue full set of industry-standard vouchers fulfilling missing items."""
    if get_active_role(request) != "agent":
        return PlainTextResponse("僅限訂票專員視角批次核發訂票憑證", status_code=403)

    order = db.get(OrderQuote, order_id)
    if order is None or order.trip_plan is None:
        raise HTTPException(status_code=404, detail="找不到指定的訂單資料")

    try:
        created = auto_issue_trip_vouchers(db, order)
    except ValueError as exc:
        set_flash(request, "error", str(exc))
        return RedirectResponse(url=f"/orders/{order.order_id}", status_code=303)

    if not created:
        set_flash(
            request,
            "info",
            "此訂單所需全套憑證皆已核發完畢，如需補發或新增景點憑證請使用下方分類表單。",
        )
        return RedirectResponse(url=f"/orders/{order.order_id}", status_code=303)

    active_agent = get_active_agent(request, db)
    mail_res = send_voucher_issued_email(
        order=order,
        trip=order.trip_plan,
        user=order.trip_plan.user,
        agent=active_agent or order.agent,
        vouchers=created,
    )
    codes = "、".join(v.booking_code for v in created)
    set_flash(
        request,
        "success",
        f"已一鍵批次核發 {len(created)} 張旅行社規格憑證（{codes}），訂單狀態為 {order.order_status}！{mail_res['note']}",
    )
    return RedirectResponse(url=f"/orders/{order.order_id}", status_code=303)


@router.post("/orders/{order_id}/vouchers/{voucher_id}/delete")
def remove_voucher(
    order_id: int,
    voucher_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Allow an active agent to revoke an erroneously issued voucher."""
    if get_active_role(request) != "agent":
        return PlainTextResponse("僅限訂票專員視角撤銷訂票憑證", status_code=403)

    order = db.get(OrderQuote, order_id)
    if order is None or order.trip_plan is None:
        raise HTTPException(status_code=404, detail="找不到指定的訂單資料")

    try:
        deleted_code = delete_voucher(db, order, voucher_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    set_flash(
        request,
        "info",
        f"已撤銷訂票憑證（{deleted_code}），目前訂單狀態為 {order.order_status}。",
    )
    return RedirectResponse(url=f"/orders/{order.order_id}", status_code=303)


@router.post("/orders/{order_id}/exceptions")
def create_exception(
    order_id: int,
    request: Request,
    reason: str = Form(""),
    alternative_proposal: str = Form(""),
    db: Session = Depends(get_db),
):
    """Allow an active agent to report a booking exception and pause order for member choice."""
    if get_active_role(request) != "agent":
        return PlainTextResponse("僅限訂票專員視角回報訂票異常", status_code=403)

    order = db.get(OrderQuote, order_id)
    if order is None or order.trip_plan is None:
        raise HTTPException(status_code=404, detail="找不到指定的訂單資料")

    try:
        exc_item = report_booking_exception(
            db=db,
            order=order,
            reason=reason,
            alternative_proposal=alternative_proposal,
        )
    except ValueError as exc:
        set_flash(request, "error", str(exc))
        return RedirectResponse(url=f"/orders/{order.order_id}", status_code=303)

    active_agent = get_active_agent(request, db)
    mail_res = send_exception_alert_email(
        order=order,
        trip=order.trip_plan,
        user=order.trip_plan.user,
        agent=active_agent or order.agent,
        exc=exc_item,
    )
    set_flash(
        request,
        "info",
        f"已立案訂票異常 #{exc_item.exception_id}，訂單狀態暫轉為 EXCEPTION 並已通知會員抉擇。{mail_res['note']}",
    )
    return RedirectResponse(url=f"/orders/{order.order_id}", status_code=303)


@router.post("/orders/{order_id}/exceptions/{exception_id}/decide")
def decide_exception(
    order_id: int,
    exception_id: int,
    request: Request,
    decision: str = Form(""),
    db: Session = Depends(get_db),
):
    """Allow the trip owner (member view) to accept ALT or request full REFUND."""
    if get_active_role(request) == "agent":
        return PlainTextResponse("請先切換回會員視角再提交異常處理決定", status_code=403)

    current_user = get_current_user(request, db)
    if current_user is None:
        set_flash(request, "error", "請先登入會員帳號再處理訂票異常通知。")
        return RedirectResponse(url="/login", status_code=303)

    order = db.get(OrderQuote, order_id)
    if order is None or order.trip_plan is None:
        raise HTTPException(status_code=404, detail="找不到指定的訂單資料")
    if order.trip_plan.user_id != current_user.user_id:
        return PlainTextResponse("您沒有權限處理此筆訂單的異常通知", status_code=403)

    exc_item = db.get(BookingException, exception_id)
    if exc_item is None or exc_item.order_id != order.order_id:
        raise HTTPException(status_code=404, detail="找不到指定的訂票異常紀錄")

    if exc_item.user_decision != "PENDING":
        set_flash(request, "info", "此筆異常紀錄已完成處理，無法重複提交決定。")
        return RedirectResponse(url=f"/orders/{order.order_id}", status_code=303)

    try:
        resolved_exc, refund_txn = resolve_booking_exception(
            db=db,
            order=order,
            exc=exc_item,
            decision=decision,
        )
    except ValueError as exc:
        set_flash(request, "error", str(exc))
        return RedirectResponse(url=f"/orders/{order.order_id}", status_code=303)

    member_mail, _agent_mail = send_exception_resolved_emails(
        order=order,
        trip=order.trip_plan,
        user=current_user,
        agent=order.agent,
        exc=resolved_exc,
        refund_txn=refund_txn,
    )

    if resolved_exc.user_decision == "ALT":
        set_flash(
            request,
            "success",
            f"已接受專員替代方案！訂單 #{order.order_id} 已恢復為 PAID，專員將繼續完成代訂並核發憑證。{member_mail['note']}",
        )
    else:
        ref_code = refund_txn.gateway_ref if refund_txn else "MOCK-REF"
        ref_amt = refund_txn.amount if refund_txn else (order.quoted_price or 0)
        set_flash(
            request,
            "success",
            f"已完成異常全額模擬退款 NT$ {ref_amt:,}（交易編號 {ref_code}），"
            f"訂單已轉為 REFUNDED 結案。{member_mail['note']}",
        )

    return RedirectResponse(url=f"/orders/{order.order_id}", status_code=303)
