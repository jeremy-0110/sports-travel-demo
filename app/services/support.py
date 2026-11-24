"""Live customer support messaging and refund-review workflow service."""

from sqlalchemy.orm import Session

import app.config as config
from app.models import (
    Agent,
    GapItineraryItem,
    OrderQuote,
    PaymentTxn,
    SupportMessage,
    SupportTicket,
    User,
    utc_now,
)
from app.services.payment import process_mock_refund

SUPPORT_CATEGORIES: dict[str, str] = {
    "GENERAL": "一般行程與帳號諮詢",
    "ITINERARY": "賽事大架構／小行程調整協助",
    "REFUND": "申請訂單退款（專員審核）",
}

SUPPORT_STATUS_LABELS: dict[str, str] = {
    "OPEN": "對話進行中 (OPEN)",
    "REFUND_REQUESTED": "退款審核中 (REFUND_REQUESTED)",
    "REFUND_APPROVED": "已核准退款 (REFUND_APPROVED)",
    "REFUND_REJECTED": "退款已駁回 (REFUND_REJECTED)",
    "CLOSED": "已結案 (CLOSED)",
}


def ensure_default_agent(db: Session) -> Agent:
    """Return active default booking/support agent (AG001), creating it if absent."""
    agent = db.get(Agent, config.DEFAULT_AGENT_ID)
    if agent is None:
        agent = Agent(
            agent_id=config.DEFAULT_AGENT_ID,
            agent_name="王小明",
            email="ag001@sportstravel.demo",
            is_active=True,
        )
        db.add(agent)
        db.flush()
    return agent


def create_support_ticket(
    db: Session,
    *,
    user: User,
    category: str,
    subject: str,
    initial_message: str,
    order_id: int | None = None,
) -> SupportTicket:
    """Create a member support ticket (or refund request) with initial conversation entries."""
    clean_cat = (category or "GENERAL").strip().upper()
    if clean_cat not in SUPPORT_CATEGORIES:
        clean_cat = "GENERAL"

    clean_subject = (subject or "").strip()
    clean_msg = (initial_message or "").strip()
    if not clean_subject:
        raise ValueError("請填寫客服聯絡主旨或問題摘要。")
    if not clean_msg:
        raise ValueError("請輸入想告訴客服專員的訊息或退款原因。")

    linked_order: OrderQuote | None = None
    if order_id is not None:
        linked_order = db.get(OrderQuote, order_id)
        if linked_order is None or linked_order.trip_plan is None:
            raise ValueError("找不到指定關聯的訂單資料。")
        if linked_order.trip_plan.user_id != user.user_id:
            raise ValueError("您沒有權限針對此筆訂單發起客服或退款申請。")

    refund_amount: int | None = None
    initial_status = "OPEN"

    if clean_cat == "REFUND":
        if linked_order is None:
            raise ValueError("申請退款時請選擇一筆已付款的訂單。")
        if linked_order.order_status not in ("PAID", "BOOKED"):
            raise ValueError(
                f"訂單 #{linked_order.order_id} 目前狀態為 {linked_order.order_status}，僅限已付款（PAID / BOOKED）訂單申請退款。"
            )
        pending_same_order = (
            db.query(SupportTicket)
            .filter(
                SupportTicket.order_id == linked_order.order_id,
                SupportTicket.status == "REFUND_REQUESTED",
            )
            .first()
        )
        if pending_same_order is not None:
            raise ValueError(
                f"訂單 #{linked_order.order_id} 已有審核中的退款對話（客服單 #{pending_same_order.ticket_id}），請直接於該對話與專員聯繫。"
            )
        initial_status = "REFUND_REQUESTED"
        refund_amount = linked_order.quoted_price or 0

    default_agent = ensure_default_agent(db)
    assigned_agent_id = (
        linked_order.agent_id
        if (linked_order is not None and linked_order.agent_id)
        else default_agent.agent_id
    )

    now = utc_now()
    ticket = SupportTicket(
        user_id=user.user_id,
        order_id=linked_order.order_id if linked_order else None,
        agent_id=assigned_agent_id,
        category=clean_cat,
        subject=clean_subject[:120],
        status=initial_status,
        refund_amount_requested=refund_amount,
        created_at=now,
        updated_at=now,
    )
    db.add(ticket)
    db.flush()

    db.add(
        SupportMessage(
            ticket_id=ticket.ticket_id,
            sender_role="MEMBER",
            sender_name=user.full_name,
            content=clean_msg,
            created_at=now,
        )
    )

    if clean_cat == "REFUND" and linked_order is not None:
        order_label = "景點加購訂單" if linked_order.order_type == "ADDON" else "主行程訂單"
        sys_msg = (
            f"【系統退款立案】會員申請退還{order_label} #{linked_order.order_id}"
            f"（實付金額 NT$ {refund_amount:,}），已指派客服專員 {assigned_agent_id} 進行人工審核與回覆。"
        )
    else:
        sys_msg = (
            f"【系統通知】客服單 #{ticket.ticket_id} 已建立，"
            f"已通知專屬客服專員 {assigned_agent_id}，專員將於本對話視窗即時回覆。"
        )
    db.add(
        SupportMessage(
            ticket_id=ticket.ticket_id,
            sender_role="SYSTEM",
            sender_name="系統通知",
            content=sys_msg,
            created_at=now,
        )
    )

    db.commit()
    db.refresh(ticket)
    return ticket


def post_support_message(
    db: Session,
    *,
    ticket: SupportTicket,
    sender_role: str,
    sender_name: str,
    content: str,
) -> SupportMessage:
    """Append a MEMBER or AGENT message inside an active support conversation."""
    clean_content = (content or "").strip()
    if not clean_content:
        raise ValueError("請輸入回覆訊息內容。")
    if ticket.status == "CLOSED":
        raise ValueError("此客服單已結案，如需進一步協助請重新發起新客服對話。")

    now = utc_now()
    msg = SupportMessage(
        ticket_id=ticket.ticket_id,
        sender_role="AGENT" if sender_role.upper() == "AGENT" else "MEMBER",
        sender_name=sender_name.strip() or "使用者",
        content=clean_content,
        created_at=now,
    )
    db.add(msg)
    ticket.updated_at = now
    db.commit()
    db.refresh(msg)
    return msg


def approve_support_refund(
    db: Session,
    *,
    ticket: SupportTicket,
    agent: Agent,
    approved_amount: int,
    agent_note: str = "",
) -> tuple[SupportTicket, PaymentTxn]:
    """Agent approves a pending refund ticket, creates REFUND PaymentTxn, and updates trip/gap state."""
    if ticket.category != "REFUND" or ticket.status != "REFUND_REQUESTED":
        raise ValueError("僅限「退款審核中（REFUND_REQUESTED）」的客服退款單可執行核准操作。")

    order = ticket.order_quote
    if order is None:
        raise ValueError("找不到關聯訂單，無法執行退款。")
    if order.order_status not in ("PAID", "BOOKED"):
        raise ValueError(f"訂單 #{order.order_id} 目前狀態為 {order.order_status}，無法重複退款。")

    max_refundable = order.quoted_price or 0
    if approved_amount <= 0 or approved_amount > max_refundable:
        raise ValueError(f"核准退款金額須介於 NT$ 1 ～ NT$ {max_refundable:,} 之間。")

    refund_txn = process_mock_refund(db, order, amount=approved_amount)
    if order.order_type == "MAIN_TRIP" and order.trip_plan is not None:
        order.trip_plan.status = "CANCELLED"
    elif order.order_type == "ADDON":
        linked_items = (
            db.query(GapItineraryItem)
            .filter(GapItineraryItem.addon_order_id == order.order_id)
            .all()
        )
        for item in linked_items:
            item.addon_order_id = None
            item.need_ticket_addon = False

    now = utc_now()
    ticket.agent_id = agent.agent_id
    ticket.status = "REFUND_APPROVED"
    ticket.refund_amount_requested = approved_amount
    ticket.updated_at = now

    clean_note = (agent_note or "").strip()
    if clean_note:
        db.add(
            SupportMessage(
                ticket_id=ticket.ticket_id,
                sender_role="AGENT",
                sender_name=f"{agent.agent_id} {agent.agent_name}",
                content=clean_note,
                created_at=now,
            )
        )

    impact_note = (
        "關聯主行程已同步撤銷取消。"
        if order.order_type == "MAIN_TRIP"
        else "原預約加購景點已釋放回免費空檔規劃（主行程不受影響）。"
    )
    db.add(
        SupportMessage(
            ticket_id=ticket.ticket_id,
            sender_role="SYSTEM",
            sender_name="系統退款完成憑證",
            content=(
                f"✓ 專員 {agent.agent_id} 已核准並完成模擬退款 NT$ {approved_amount:,}"
                f"（金流退款編號 {refund_txn.gateway_ref}），訂單 #{order.order_id} 已轉為 REFUNDED。{impact_note}"
            ),
            created_at=now,
        )
    )

    db.commit()
    db.refresh(ticket)
    db.refresh(refund_txn)
    return ticket, refund_txn


def reject_support_refund(
    db: Session,
    *,
    ticket: SupportTicket,
    agent: Agent,
    reason: str,
) -> SupportTicket:
    """Agent rejects a pending refund request with written explanation; order stays active."""
    if ticket.category != "REFUND" or ticket.status != "REFUND_REQUESTED":
        raise ValueError("僅限「退款審核中（REFUND_REQUESTED）」的客服退款單可執行駁回操作。")

    clean_reason = (reason or "").strip()
    if not clean_reason:
        raise ValueError("駁回退款申請時，請務必填寫原因或替代說明告知會員。")

    now = utc_now()
    ticket.agent_id = agent.agent_id
    ticket.status = "REFUND_REJECTED"
    ticket.updated_at = now

    db.add(
        SupportMessage(
            ticket_id=ticket.ticket_id,
            sender_role="AGENT",
            sender_name=f"{agent.agent_id} {agent.agent_name}",
            content=f"【退款申請未核准說明】{clean_reason}",
            created_at=now,
        )
    )
    order_ref = f" #{ticket.order_id}" if ticket.order_id else ""
    db.add(
        SupportMessage(
            ticket_id=ticket.ticket_id,
            sender_role="SYSTEM",
            sender_name="系統通知",
            content=f"專員 {agent.agent_id} 已審核並駁回訂單{order_ref} 退款申請，原訂單維持有效。",
            created_at=now,
        )
    )

    db.commit()
    db.refresh(ticket)
    return ticket


def close_support_ticket(
    db: Session,
    *,
    ticket: SupportTicket,
    actor_label: str,
) -> SupportTicket:
    """Close an open/resolved support ticket cleanly."""
    if ticket.status == "REFUND_REQUESTED":
        raise ValueError("退款申請仍在審核中，須先核准或駁回退款後才能結案。")
    if ticket.status == "CLOSED":
        return ticket

    now = utc_now()
    ticket.status = "CLOSED"
    ticket.updated_at = now
    db.add(
        SupportMessage(
            ticket_id=ticket.ticket_id,
            sender_role="SYSTEM",
            sender_name="系統通知",
            content=f"客服單已由 {actor_label} 標記結案（CLOSED）。",
            created_at=now,
        )
    )
    db.commit()
    db.refresh(ticket)
    return ticket
