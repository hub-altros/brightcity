"""FastAPI-приложение веб-админки."""
import asyncio
import logging
import os
import re
from contextlib import asynccontextmanager

# ─── Env defaults для веб-процесса ───────────────────────────────────
os.environ.setdefault("ADMIN_ID", "0")
os.environ.setdefault("VK_BOT_TOKEN", "fake-token-for-web-import")
# ─────────────────────────────────────────────────────────────────────

from fastapi import FastAPI
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

from core.db import init_db_pool, run_migrations, shutdown_db
from core.runtime import set_adapter, set_event_loop, get_adapter
from core.cache_listener import start_cache_listener_thread

from web_app.db_helpers import ensure_web_schema
from web_app.dependencies import (
    install_redirect_handler,
    get_current_admin as _get_current_admin_dep,
)
from core.web_bridge import WebBridgeAdapter


# Абсолютный путь к папке web_app/static.
_WEB_STATIC = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "static"
)


class _TokenMaskFilter(logging.Filter):
    """Маскирует TG- и VK-токены в логах."""
    _TG_PATTERN = re.compile(
        r"bot\d+(?:%3A|:)[A-Za-z0-9_\-]+", re.IGNORECASE)
    _VK_QUERY_PATTERN = re.compile(
        r"(access_token|vk_access_token)=[A-Za-z0-9._\-]{40,}",
        re.IGNORECASE,
    )

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        masked = msg
        if "bot" in masked:
            masked = self._TG_PATTERN.sub("bot<TOKEN>", masked)
        if "access_token" in masked.lower():
            masked = self._VK_QUERY_PATTERN.sub(r"\1=<TOKEN>", masked)
        if masked != msg:
            record.msg = masked
            record.args = ()
        return True


def _install_token_mask_filter():
    mask = _TokenMaskFilter()
    seen = set()
    for name in ("", "httpx", "httpcore", "uvicorn",
                 "uvicorn.error", "uvicorn.access"):
        lgr = logging.getLogger(name) if name else logging.getLogger()
        for h in lgr.handlers:
            if id(h) not in seen:
                h.addFilter(mask)
                seen.add(id(h))
    root = logging.getLogger()
    if not root.handlers:
        fallback = logging.StreamHandler()
        fallback.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        fallback.addFilter(mask)
        root.addHandler(fallback)


logger = logging.getLogger("web.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    _install_token_mask_filter()

    init_db_pool()
    try:
        run_migrations()
    except RuntimeError as e:
        logger.error("Не удалось применить миграции: %s", e)
        raise

    ensure_web_schema()

    set_adapter(WebBridgeAdapter())
    set_event_loop(asyncio.get_running_loop())

    start_cache_listener_thread()

    # Прогреваем TG-бот для get_file (медиа из TG file_id)
    from web_app.media import _get_tg_bot
    _get_tg_bot()

    logger.info("Веб-админка запущена. Открой http://%s:%s/",
                os.getenv("WEB_HOST", "127.0.0.1"),
                os.getenv("WEB_PORT", "8000"))
    yield
    shutdown_db()


app = FastAPI(title="Яркий!Город — Админка", lifespan=lifespan,
              docs_url=None, redoc_url=None)


# --- Middleware ---

_web_secret = os.getenv("WEB_SECRET_KEY")
if not _web_secret:
    _web_secret = os.urandom(32).hex()
    logger.warning(
        "WEB_SECRET_KEY не задан — сгенерирован разовый ключ. "
        "Промежуточные сессии 2FA будут слетать после рестарта.")

app.add_middleware(
    SessionMiddleware,
    secret_key=_web_secret,
    session_cookie="yg_pending",
    max_age=3600,
    same_site="none",
    https_only=True,
)


class WebAdminContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        admin = None
        try:
            admin = _get_current_admin_dep(request)
        except Exception:
            pass
        adapter = None
        try:
            adapter = get_adapter()
        except Exception:
            pass

        if admin is not None and adapter is not None:
            try:
                adapter.set_current_admin(admin[0])
            except Exception:
                pass

        try:
            return await call_next(request)
        finally:
            if adapter is not None:
                try:
                    adapter.set_current_admin(None)
                except Exception:
                    pass


app.add_middleware(WebAdminContextMiddleware)


# --- RedirectException ---
install_redirect_handler(app)


# --- Статика и PWA ---
app.mount("/static", StaticFiles(directory="web_app/static"), name="static")


# --- JSON API ---
from web_app.api import auth as api_auth
from web_app.api import applications as api_apps
from web_app.api import gdpr as api_gdpr
from web_app.api import stats as api_stats
from web_app.api import chats as api_chats
from web_app.api import media as api_media

from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://hub-altros.github.io",   # ← твой GitHub Pages
        "http://localhost:5173",          # локальная разработка
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_auth.router)
app.include_router(api_apps.router)
app.include_router(api_gdpr.router)
app.include_router(api_stats.router)
app.include_router(api_chats.router)
app.include_router(api_media.router)         # /api/media/*
app.include_router(api_media.legacy_router)  # /media/* (React и боты)


# --- Корневые и PWA-эндпоинты ---

@app.api_route("/", methods=["GET", "HEAD"])
async def _root():
    return {
        "service": "Яркий!Город — API",
        "status": "ok",
        "docs": "API endpoints под /api/*, фронтенд — на GitHub Pages",
    }


@app.get("/manifest.json")
async def _manifest():
    return FileResponse("web_app/static/manifest.json",
                        media_type="application/manifest+json")


@app.get("/sw.js")
async def _sw():
    return FileResponse("web_app/static/sw.js",
                        media_type="application/javascript")


@app.get("/favicon.ico")
async def _favicon():
    return FileResponse(os.path.join(_WEB_STATIC, "icon-192.png"),
                        media_type="image/png")