"""FastAPI-зависимости: текущий админ, bridge, авторизация."""
import asyncio
import logging
from typing import Optional

from fastapi import Request, Depends, HTTPException
from fastapi.responses import RedirectResponse

from web_app.auth import SESSION_COOKIE, unsign_session
from web_app.db_helpers import get_session, get_web_admin_by_id

logger = logging.getLogger("web.deps")


# ============================================================
#  ПОРЯДОК ВАЖЕН:
#    1. get_current_admin       (его используют все Depends ниже)
#    2. _RedirectException      (нужен require_admin)
#    3. require_admin           (медиа-роуты, 303)
#    4. require_api_admin       (JSON API, 401)
# ============================================================

def get_current_admin(request: Request):
    """Возвращает row веб-админа или None."""
    cookie = request.cookies.get(SESSION_COOKIE)
    session_id = unsign_session(cookie) if cookie else None
    if not session_id:
        return None
    admin_id = get_session(session_id)
    if not admin_id:
        return None
    return get_web_admin_by_id(admin_id)


class _RedirectException(Exception):
    """Внутреннее: поднимается зависимостью, ловится хендлером → 303."""
    def __init__(self, to: str):
        self.to = to


def require_admin(admin=Depends(get_current_admin)):
    """Для /media/{id} (legacy): браузерный запрос без JS, 303 на /login."""
    if admin is None:
        raise _RedirectException("/login")
    if not admin[4]:  # is_active
        raise _RedirectException("/login")
    return admin


def require_api_admin(admin=Depends(get_current_admin)):
    """Для JSON API: 401 вместо 303-редиректа.

    React-клиент проверяет res.status === 401 и делает router.push('/login').
    Если вернуть 303 — fetch автоматически пройдёт по Location, получит
    HTML логина и вернёт 200. Фронт не поймёт, что сессии нет.
    """
    if admin is None or not admin[4]:
        raise HTTPException(401, "Unauthorized")
    return admin


def install_redirect_handler(app):
    @app.exception_handler(_RedirectException)
    async def _handler(request, exc):
        return RedirectResponse(exc.to, status_code=303)


def client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else ""


async def run_blocking(fn, *args, **kwargs):
    return await asyncio.to_thread(fn, *args, **kwargs)