"""Mock third-party payment gateway service writing D4 PaymentTxn records."""

from datetime import datetime, timedelta, timezone
import secrets

from sqlalchemy.orm import Session

from app.models import OrderQuote, PaymentTxn

_TAIPEI_TZ = timezone(timedelta(hours=8))

PAYMENT_METHODS: dict[str, str] = {
    "CREDIT_CARD": "模擬信用卡一次付清（Visa / Mastercard / JCB）",
    "APPLE_PAY": "模擬 Apple Pay 生物辨識付款",
    "BANK_TRANSFER": "模擬銀行 ATM／網銀即時全額轉帳",
}


def generate_gateway_ref(prefix: str = "MOCK-PAY") -> str:
    """Generate a readable mock gateway transaction reference."""
    date_part = datetime.now(_TAIPEI_TZ).strftime("%Y%m%d")
    token = secrets.token_hex(3).upper()
    return f"{prefix}-{date_part}-{token}"


def process_mock_payment(
    db: Session,
    order: OrderQuote,
    payment_method: str = "CREDIT_CARD",
    simulate_outcome: str = "SUCCESS",
) -> PaymentTxn:
    """Execute a full-amount one-shot mock payment for a QUOTED order."""
    if order.order_status != "QUOTED" or order.quoted_price is None or order.quoted_price <= 0:
        raise ValueError("僅有已完成報價（QUOTED）且具合法一口價金額的訂單可執行全額付款。")

    _method = payment_method if payment_method in PAYMENT_METHODS else "CREDIT_CARD"
    outcome = "FAILED" if (simulate_outcome or "").strip().upper() == "FAILED" else "SUCCESS"

    txn = PaymentTxn(
        order_id=order.order_id,
        txn_type="PAYMENT",
        amount=order.quoted_price,
        gateway_ref=generate_gateway_ref(f"MOCK-PAY-{_method[:2]}"),
        status=outcome,
    )
    db.add(txn)

    if outcome == "SUCCESS":
        order.order_status = "PAID"
        if order.order_type == "MAIN_TRIP" and order.trip_plan is not None:
            order.trip_plan.status = "LOCKED"

    db.commit()
    db.refresh(txn)
    db.refresh(order)
    return txn


def process_mock_refund(
    db: Session,
    order: OrderQuote,
    amount: int | None = None,
) -> PaymentTxn:
    """Execute a full mock refund transaction (reusable by Phase 4 exception handling)."""
    refund_amount = (
        amount
        if (amount is not None and amount > 0)
        else (order.quoted_price or 0)
    )
    if refund_amount <= 0:
        raise ValueError("退款金額必須大於 0。")

    txn = PaymentTxn(
        order_id=order.order_id,
        txn_type="REFUND",
        amount=refund_amount,
        gateway_ref=generate_gateway_ref("MOCK-REF"),
        status="SUCCESS",
    )
    db.add(txn)
    order.order_status = "REFUNDED"
    db.commit()
    db.refresh(txn)
    db.refresh(order)
    return txn
