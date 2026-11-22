"""Main FastAPI application entry point."""

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from app.auth import (
    get_active_agent,
    get_active_role,
    get_current_user,
    pop_flash,
)
from app.config import BASE_DIR, SESSION_SECRET_KEY, SUPPORTED_LEAGUES
from app.database import get_db, init_db
from app.models import Agent, TripPlan, User
from app.routers.auth import router as auth_router


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
app.state.templates = templates

app.include_router(auth_router)


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
            "supported_leagues": SUPPORTED_LEAGUES,
            "flash": pop_flash(request),
        },
    )
