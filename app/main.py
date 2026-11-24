"""Main FastAPI application entry point."""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from app.auth import (
    count_active_support_tickets,
    get_active_agent,
    get_active_role,
    get_current_user,
    pop_flash,
)
from app.config import BASE_DIR, SESSION_SECRET_KEY, SUPPORTED_LEAGUES
from app.database import get_db, init_db
from app.models import Agent, OrderQuote, TripPlan, User
from app.routers.auth import router as auth_router
from app.routers.gap import router as gap_router
from app.routers.orders import router as orders_router
from app.routers.support import router as support_router
from app.routers.trips import router as trips_router
from app.routers.vouchers import router as vouchers_router
from app.services.support import SUPPORT_STATUS_LABELS


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Ensure SQLite tables exist when the application starts."""
    init_db()
    yield


app = FastAPI(
    title="體育賽事旅遊規劃與代訂系統",
    lifespan=lifespan,
)

app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET_KEY,
    session_cookie="sports_travel_session",
)

templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))

_TAIPEI_TZ = timezone(timedelta(hours=8))
_TRIP_STATUS_LABELS: dict[str, str] = {
    "DRAFT": "草稿 (DRAFT)",
    "CONFIRMED": "待報價 (CONFIRMED)",
    "LOCKED": "已付款鎖定 (LOCKED)",
    "CANCELLED": "已退單 (CANCELLED)",
}
_ORDER_STATUS_LABELS: dict[str, str] = {
    "QUOTING": "待報價 (QUOTING)",
    "QUOTED": "待付款 (QUOTED)",
    "PAID": "已付款待開票 (PAID)",
    "BOOKED": "已出憑證 (BOOKED)",
    "EXCEPTION": "異常處理中 (EXCEPTION)",
    "REFUNDED": "已退款 (REFUNDED)",
    "CANCELLED": "已取消 (CANCELLED)",
}


def _taipei_time_filter(value: datetime | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """Format a naive-UTC or tz-aware datetime in Taipei local time (UTC+8)."""
    if value is None:
        return "—"
    aware = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    return aware.astimezone(_TAIPEI_TZ).strftime(fmt)


templates.env.filters["taipei_time"] = _taipei_time_filter
templates.env.filters["trip_status_label"] = lambda s: _TRIP_STATUS_LABELS.get(str(s), str(s))
templates.env.filters["order_status_label"] = lambda s: _ORDER_STATUS_LABELS.get(str(s), str(s))
templates.env.filters["support_status_label"] = lambda s: SUPPORT_STATUS_LABELS.get(str(s), str(s))
app.state.templates = templates

app.include_router(auth_router)
app.include_router(trips_router)
app.include_router(orders_router)
app.include_router(vouchers_router)
app.include_router(gap_router)
app.include_router(support_router)


@app.get("/health")
def health_check() -> dict[str, str]:
    """Basic health check endpoint."""
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)):
    """Render either the Member Dashboard or Agent Workbench based on active role."""
    current_user = get_current_user(request, db)
    active_role = get_active_role(request)
    active_agent = get_active_agent(request, db)
    agents = (
        db.query(Agent)
        .filter(Agent.is_active.is_(True))
        .order_by(Agent.agent_id)
        .all()
    )

    users_count = db.query(User).count()
    agents_count = db.query(Agent).filter(Agent.is_active.is_(True)).count()
    trips_count = db.query(TripPlan).count()

    user_trips = (
        db.query(TripPlan)
        .filter(TripPlan.user_id == current_user.user_id)
        .order_by(TripPlan.trip_id.desc())
        .all()
        if current_user is not None
        else []
    )
    all_trips = (
        db.query(TripPlan).order_by(TripPlan.trip_id.desc()).all()
        if active_role == "agent"
        else []
    )
    all_orders = (
        db.query(OrderQuote).order_by(OrderQuote.order_id.desc()).all()
        if active_role == "agent"
        else []
    )
    quoting_count = sum(1 for o in all_orders if o.order_status == "QUOTING")
    paid_count = sum(1 for o in all_orders if o.order_status in ("PAID", "BOOKED"))
    booked_count = sum(1 for o in all_orders if o.order_status == "BOOKED")
    exception_count = sum(1 for o in all_orders if o.order_status == "EXCEPTION")

    user_trip_orders: dict[int, OrderQuote] = {}
    user_trip_addons: dict[int, list[OrderQuote]] = {}
    if current_user is not None:
        member_orders = (
            db.query(OrderQuote)
            .join(TripPlan, OrderQuote.trip_id == TripPlan.trip_id)
            .filter(TripPlan.user_id == current_user.user_id)
            .order_by(OrderQuote.order_id.asc())
            .all()
        )
        for ord_item in member_orders:
            if ord_item.order_type == "MAIN_TRIP":
                user_trip_orders[ord_item.trip_id] = ord_item
            elif ord_item.order_type == "ADDON":
                user_trip_addons.setdefault(ord_item.trip_id, []).append(ord_item)

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "current_user": current_user,
            "active_role": active_role,
            "active_agent": active_agent,
            "agents": agents,
            "users_count": users_count,
            "agents_count": agents_count,
            "trips_count": trips_count,
            "user_trips": user_trips,
            "all_trips": all_trips,
            "all_orders": all_orders,
            "quoting_count": quoting_count,
            "paid_count": paid_count,
            "booked_count": booked_count,
            "exception_count": exception_count,
            "user_trip_orders": user_trip_orders,
            "user_trip_addons": user_trip_addons,
            "open_support_count": count_active_support_tickets(db, active_role, current_user),
            "supported_leagues": SUPPORTED_LEAGUES,
            "flash": pop_flash(request),
        },
    )
