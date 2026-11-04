"""JSON-авторизация: login, 2FA, logout, me."""
import base64
import logging

from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import JSONResponse

from web_app.auth import (
    verify_password, sign_session, unsign_session,
    SESSION_COOKIE, SESSION_MAX_AGE,
)
from web_app.db_helpers import (
    get_web_admin_by_login, get_web_admin_by_id, create_session,
    delete_session, touch_last_login, get_totp_last_step,
)
from web_app.dependencies import require_admin, client_ip
from web_app.twofa import (
    setup_totp, confirm_totp_setup, verify_totp, verify_recovery,
)
from web_app.api.schemas import (
    AdminOut, LoginResp, TwoFASetupResp, TwoFASetupConfirmResp, OkResp,
)

logger = logging.getLogger("web.api.auth")
router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.get("/me", response_model=AdminOut)
async def me(admin=Depends(require_admin)):
    return AdminOut(
        id=admin[0], login=admin[1],
        display_name=admin[3] or admin[1],
        bot_platform=admin[7], bot_user_id=admin[8],
    )


@router.post("/login", response_model=LoginResp)
async def login(request: Request, body: dict):
    login_ = (body.get("login") or "").strip()
    password = body.get("password") or ""
    row = get_web_admin_by_login(login_)
    if not row or not row[4] or not verify_password(password, row[2]):
        raise HTTPException(401, "Неверный логин или пароль")

    admin_id = row[0]
    request.session["pending_admin_id"] = admin_id
    redirect = "/2fa/setup" if row[6] is None else "/2fa/verify"
    return LoginResp(redirect=redirect)


# --- 2FA setup (первый вход) ---

@router.get("/2fa/setup", response_model=TwoFASetupResp)
async def setup_form(request: Request):
    admin_id = request.session.get("pending_admin_id")
    if not admin_id:
        raise HTTPException(401)
    row = get_web_admin_by_id(admin_id)
    if not row:
        raise HTTPException(401)
    if row[6] is not None:
        raise HTTPException(409, "2FA уже настроена")
    data = setup_totp(admin_id, row[1])
    qr_b64 = base64.b64encode(data["qr_png"]).decode()
    return TwoFASetupResp(
        secret=data["secret"],
        qr_data_url=f"data:image/png;base64,{qr_b64}",
    )


@router.post("/2fa/setup", response_model=TwoFASetupConfirmResp)
async def setup_confirm(request: Request, body: dict):
    admin_id = request.session.get("pending_admin_id")
    if not admin_id:
        raise HTTPException(401)
    row = get_web_admin_by_id(admin_id)
    if not row or not row[5]:
        raise HTTPException(401)
    ok, recovery_codes = confirm_totp_setup(admin_id, row[5], body.get("code", ""))
    if not ok:
        raise HTTPException(400, "Код неверный")
    return TwoFASetupConfirmResp(recovery_codes=recovery_codes)


@router.post("/2fa/setup/complete", response_model=OkResp)
async def setup_complete(request: Request):
    """Вызывается после того, как пользователь сохранил recovery-коды."""
    admin_id = request.session.get("pending_admin_id")
    if not admin_id:
        raise HTTPException(401)
    session_id = create_session(
        admin_id,
        user_agent=request.headers.get("user-agent", ""),
        ip=client_ip(request),
    )
    touch_last_login(admin_id)
    request.session.pop("pending_admin_id", None)

    resp = JSONResponse({"ok": True})
    resp.set_cookie(
        SESSION_COOKIE, sign_session(session_id),
        max_age=SESSION_MAX_AGE, httponly=True, samesite="lax",
        secure=request.url.scheme == "https",
    )
    return resp


# --- 2FA verify (обычный вход) ---

@router.post("/2fa/verify", response_model=OkResp)
async def verify_submit(request: Request, body: dict):
    admin_id = request.session.get("pending_admin_id")
    if not admin_id:
        raise HTTPException(401)
    row = get_web_admin_by_id(admin_id)
    if not row:
        raise HTTPException(401)

    use_recovery = bool(body.get("use_recovery"))
    code = body.get("code", "")
    if use_recovery:
        ok = verify_recovery(admin_id, code)
    else:
        ok = verify_totp(admin_id, row[5], code, get_totp_last_step(admin_id))

    if not ok:
        raise HTTPException(400, "Неверный код")

    session_id = create_session(
        admin_id,
        user_agent=request.headers.get("user-agent", ""),
        ip=client_ip(request),
    )
    touch_last_login(admin_id)
    request.session.pop("pending_admin_id", None)

    resp = JSONResponse({"ok": True})
    resp.set_cookie(
        SESSION_COOKIE, sign_session(session_id),
        max_age=SESSION_MAX_AGE, httponly=True, samesite="lax",
        secure=request.url.scheme == "https",
    )
    return resp


@router.post("/logout", response_model=OkResp)
async def logout(request: Request):
    cookie = request.cookies.get(SESSION_COOKIE)
    sid = unsign_session(cookie) if cookie else None
    if sid:
        delete_session(sid)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE)
    return resp