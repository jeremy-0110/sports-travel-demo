"""Authentication and member/agent role-switching routes (DFD 1.0)."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.auth import (
    get_active_agent,
    get_active_role,
    get_current_user,
    hash_password,
    pop_flash,
    set_flash,
    verify_password,
)
from app.config import DEFAULT_AGENT_ID, SUPPORTED_LEAGUES
from app.database import get_db
from app.models import Agent, User

router = APIRouter()


def _base_context(request: Request, db: Session) -> dict:
    """Build shared template context for auth and navigation."""
    return {
        "current_user": get_current_user(request, db),
        "active_role": get_active_role(request),
        "active_agent": get_active_agent(request, db),
        "agents": (
            db.query(Agent)
            .filter(Agent.is_active.is_(True))
            .order_by(Agent.agent_id)
            .all()
        ),
        "flash": pop_flash(request),
        "supported_leagues": SUPPORTED_LEAGUES,
    }


@router.get("/register", response_class=HTMLResponse)
def register_page(request: Request, db: Session = Depends(get_db)):
    """Render the member registration page."""
    context = _base_context(request, db)
    context.update({"error": None, "form_data": {}})
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="register.html",
        context=context,
    )


@router.post("/register", response_class=HTMLResponse)
def register_submit(
    request: Request,
    full_name: str = Form(""),
    email: str = Form(""),
    phone: str = Form(""),
    password: str = Form(""),
    db: Session = Depends(get_db),
):
    """Validate and create a new member account, then log the user in."""
    full_name = full_name.strip()
    email = email.strip().lower()
    phone = phone.strip()

    error: str | None = None
    if not full_name:
        error = "請輸入您的姓名。"
    elif not email or "@" not in email or "." not in email.split("@")[-1]:
        error = "請輸入有效的電子信箱格式。"
    elif len(password) < 6:
        error = "密碼長度至少需要 6 個字元。"
    elif db.query(User).filter(User.email == email).first() is not None:
        error = "此電子信箱已經註冊過，請直接登入或改用其他信箱。"

    if error is not None:
        context = _base_context(request, db)
        context.update(
            {
                "error": error,
                "form_data": {
                    "full_name": full_name,
                    "email": email,
                    "phone": phone,
                },
            }
        )
        return request.app.state.templates.TemplateResponse(
            request=request,
            name="register.html",
            context=context,
            status_code=400,
        )

    user = User(
        email=email,
        password_hash=hash_password(password),
        full_name=full_name,
        phone=phone or None,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    request.session["user_id"] = user.user_id
    request.session["active_role"] = "member"
    set_flash(request, "success", f"註冊成功！歡迎加入，{user.full_name}。")
    return RedirectResponse(url="/", status_code=303)


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, db: Session = Depends(get_db)):
    """Render the member login page."""
    context = _base_context(request, db)
    context.update({"error": None, "email": ""})
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="login.html",
        context=context,
    )


@router.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    email: str = Form(""),
    password: str = Form(""),
    db: Session = Depends(get_db),
):
    """Authenticate a member by email and password."""
    email = email.strip().lower()
    user = db.query(User).filter(User.email == email).first()

    if user is None or not verify_password(password, user.password_hash):
        context = _base_context(request, db)
        context.update(
            {
                "error": "電子信箱或密碼不正確，請再試一次。",
                "email": email,
            }
        )
        return request.app.state.templates.TemplateResponse(
            request=request,
            name="login.html",
            context=context,
            status_code=400,
        )

    request.session["user_id"] = user.user_id
    request.session["active_role"] = "member"
    set_flash(request, "success", f"登入成功！歡迎回來，{user.full_name}。")
    return RedirectResponse(url="/", status_code=303)


@router.post("/logout")
def logout_submit(request: Request):
    """Log out the current member."""
    request.session.pop("user_id", None)
    set_flash(request, "info", "您已登出會員帳號。")
    return RedirectResponse(url="/", status_code=303)


@router.post("/switch-role")
def switch_role(
    request: Request,
    role: str = Form(...),
    agent_id: str = Form(DEFAULT_AGENT_ID),
    db: Session = Depends(get_db),
):
    """Switch active UI perspective between member and booking agent (e.g. AG001)."""
    if role not in ("member", "agent"):
        return PlainTextResponse("無效的視角參數", status_code=400)

    if role == "agent":
        agent = (
            db.query(Agent)
            .filter(Agent.agent_id == agent_id, Agent.is_active.is_(True))
            .first()
        )
        if agent is None:
            agent = (
                db.query(Agent)
                .filter(
                    Agent.agent_id == DEFAULT_AGENT_ID, Agent.is_active.is_(True)
                )
                .first()
            )
        if agent is None:
            agent = db.query(Agent).filter(Agent.is_active.is_(True)).first()
        if agent is None:
            return PlainTextResponse("系統尚無啟用的訂票專員資料", status_code=400)

        request.session["active_role"] = "agent"
        request.session["active_agent_id"] = agent.agent_id
        set_flash(
            request,
            "info",
            f"已切換為【訂票專員視角】— {agent.agent_id} {agent.agent_name}",
        )
    else:
        request.session["active_role"] = "member"
        set_flash(request, "info", "已切換為【會員視角】")

    return RedirectResponse(url="/", status_code=303)
