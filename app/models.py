"""SQLAlchemy ORM models for all 8 ERD tables."""

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import relationship

from app.database import Base


def utc_now() -> datetime:
    """Return timezone-aware UTC timestamp."""
    return datetime.now(timezone.utc)


class User(Base):
    """D1: Member account table."""

    __tablename__ = "user"

    user_id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String, unique=True, nullable=False, index=True)
    password_hash = Column(String, nullable=False)
    full_name = Column(String, nullable=False)
    phone = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)

    trip_plans = relationship(
        "TripPlan", back_populates="user", cascade="all, delete-orphan"
    )


class Agent(Base):
    """Booking agent table (e.g. AG001)."""

    __tablename__ = "agent"

    agent_id = Column(String, primary_key=True)
    agent_name = Column(String, nullable=False)
    email = Column(String, unique=True, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)

    order_quotes = relationship("OrderQuote", back_populates="agent")


class TripPlan(Base):
    """D2: Sports travel request & LLM itinerary draft."""

    __tablename__ = "trip_plan"

    trip_id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("user.user_id"), nullable=False)
    league_type = Column(String, nullable=False)
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=False)
    need_flight = Column(Boolean, default=False, nullable=False)
    need_hotel = Column(Boolean, default=False, nullable=False)
    llm_draft_json = Column(Text, nullable=True)
    status = Column(String, default="DRAFT", nullable=False)

    user = relationship("User", back_populates="trip_plans")
    gap_items = relationship(
        "GapItineraryItem", back_populates="trip_plan", cascade="all, delete-orphan"
    )
    order_quotes = relationship(
        "OrderQuote", back_populates="trip_plan", cascade="all, delete-orphan"
    )


class GapItineraryItem(Base):
    """D2: Gap-time itinerary items unlocked after payment."""

    __tablename__ = "gap_itinerary_item"

    item_id = Column(Integer, primary_key=True, autoincrement=True)
    trip_id = Column(Integer, ForeignKey("trip_plan.trip_id"), nullable=False)
    day_number = Column(Integer, nullable=False)
    time_slot = Column(String, nullable=False)
    activity_name = Column(String, nullable=False)
    need_ticket_addon = Column(Boolean, default=False, nullable=False)
    addon_order_id = Column(
        Integer, ForeignKey("order_quote.order_id"), nullable=True
    )

    trip_plan = relationship("TripPlan", back_populates="gap_items")
    addon_order = relationship("OrderQuote", back_populates="addon_gap_items")


class OrderQuote(Base):
    """D3: One-price quote and booking order (MAIN_TRIP / ADDON)."""

    __tablename__ = "order_quote"

    order_id = Column(Integer, primary_key=True, autoincrement=True)
    trip_id = Column(Integer, ForeignKey("trip_plan.trip_id"), nullable=False)
    agent_id = Column(String, ForeignKey("agent.agent_id"), nullable=True)
    order_type = Column(String, nullable=False)
    quoted_price = Column(Integer, nullable=True)
    order_status = Column(String, default="QUOTING", nullable=False)
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)

    trip_plan = relationship("TripPlan", back_populates="order_quotes")
    agent = relationship("Agent", back_populates="order_quotes")
    payment_txns = relationship(
        "PaymentTxn", back_populates="order_quote", cascade="all, delete-orphan"
    )
    vouchers = relationship(
        "BookingVoucher", back_populates="order_quote", cascade="all, delete-orphan"
    )
    exceptions = relationship(
        "BookingException", back_populates="order_quote", cascade="all, delete-orphan"
    )
    addon_gap_items = relationship("GapItineraryItem", back_populates="addon_order")


class PaymentTxn(Base):
    """D4: Payment and refund transaction records."""

    __tablename__ = "payment_txn"

    txn_id = Column(Integer, primary_key=True, autoincrement=True)
    order_id = Column(Integer, ForeignKey("order_quote.order_id"), nullable=False)
    txn_type = Column(String, nullable=False)
    amount = Column(Integer, nullable=False)
    gateway_ref = Column(String, nullable=True)
    status = Column(String, nullable=False)
    paid_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)

    order_quote = relationship("OrderQuote", back_populates="payment_txns")


class BookingVoucher(Base):
    """D5: Agoda-style booking confirmation vouchers."""

    __tablename__ = "booking_voucher"

    voucher_id = Column(Integer, primary_key=True, autoincrement=True)
    order_id = Column(Integer, ForeignKey("order_quote.order_id"), nullable=False)
    category = Column(String, nullable=False)
    booking_code = Column(String, nullable=False)
    detail_json = Column(Text, nullable=True)
    issued_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)

    order_quote = relationship("OrderQuote", back_populates="vouchers")


class BookingException(Base):
    """D5: Booking exception and alternative/refund resolution records."""

    __tablename__ = "booking_exception"

    exception_id = Column(Integer, primary_key=True, autoincrement=True)
    order_id = Column(Integer, ForeignKey("order_quote.order_id"), nullable=False)
    reason = Column(String, nullable=False)
    alternative_proposal = Column(Text, nullable=True)
    user_decision = Column(String, default="PENDING", nullable=False)
    resolved_at = Column(DateTime(timezone=True), nullable=True)

    order_quote = relationship("OrderQuote", back_populates="exceptions")
