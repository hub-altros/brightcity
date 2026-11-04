"""Прокси медиа с дисковым кэшем.

VK CDN URL быстро протухают (1–6 часов), TG file_id живут долго, MAX
token — тоже быстро. Поэтому:
  - при первом запросе скачиваем файл (по source_file_id / vk_url / url),
  - кэшируем в web_app/media_cache/{media_id} на диске,
  - отдаём байты.
"""
import io
import logging
import os
import threading
from typing import Optional

import httpx
from telegram import Bot as TgBot

from core.db import get_db_connection, return_db_connection
from core.media import is_allowed_media_url

logger = logging.getLogger("web.media")

CACHE_DIR = os.path.join(os.path.dirname(__file__), "media_cache")
os.makedirs(CACHE_DIR, exist_ok=True)

_cache_lock = threading.Lock()
_tg_bot: Optional[TgBot] = None
_tg_bot_lock = threading.Lock()
_http_client: Optional[httpx.Client] = None


def _get_http_client() -> httpx.Client:
    global _http_client
    if _http_client is None:
        _http_client = httpx.Client(
            timeout=httpx.Timeout(30.0, connect=15.0, read=60.0),
            follow_redirects=True,
            trust_env=False,
        )
    return _http_client


def _get_tg_bot() -> Optional[TgBot]:
    global _tg_bot
    with _tg_bot_lock:
        if _tg_bot is None:
            token = os.getenv("TG_BOT_TOKEN")
            if not token or token.startswith("fake-"):
                return None
            _tg_bot = TgBot(token=token)
        return _tg_bot


def _cache_path(media_id: int) -> str:
    return os.path.join(CACHE_DIR, str(media_id))


def get_media_row(media_id: int) -> Optional[dict]:
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, url, owner_id, photo_id, doc_id, kind,
                       user_platform, user_id, source_file_id, source_platform,
                       vk_url, vk_owner_id, vk_photo_id, vk_doc_id
                FROM media_files WHERE id = %s
            """, (media_id,))
            r = cur.fetchone()
    except Exception as e:
        logger.error("get_media_row failed: %s", e)
        return None
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)
    if not r:
        return None
    return {
        "id": r[0], "url": r[1], "owner_id": r[2], "photo_id": r[3],
        "doc_id": r[4], "kind": r[5], "user_platform": r[6], "user_id": r[7],
        "source_file_id": r[8], "source_platform": r[9],
        "vk_url": r[10],
        "vk_owner_id": r[11],
        "vk_photo_id": r[12],
        "vk_doc_id": r[13],
    }


def _download_by_url(url: str) -> Optional[bytes]:
    if not is_allowed_media_url(url):
        pass
    try:
        resp = _get_http_client().get(url)
        if resp.status_code != 200:
            logger.warning("media download: status=%s url=%s",
                           resp.status_code, url[:80])
            return None
        return resp.content
    except Exception as e:
        logger.warning("media download failed (%s): %s", url[:80], e)
        return None


def _download_tg_file(file_id: str) -> Optional[bytes]:
    bot = _get_tg_bot()
    if bot is None:
        return None
    import asyncio
    async def _fetch():
        f = await bot.get_file(file_id=file_id)
        buf = io.BytesIO()
        await f.download_to_memory(buf)
        return buf.getvalue()
    try:
        return asyncio.run(_fetch())
    except Exception as e:
        logger.warning("tg file download failed (%s…): %s", file_id[:20], e)
        return None


def get_media_bytes(media_id: int) -> Optional[bytes]:
    path = _cache_path(media_id)
    with _cache_lock:
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            try:
                with open(path, "rb") as f:
                    return f.read()
            except Exception:
                pass
    row = get_media_row(media_id)
    if not row:
        return None

    data = None

    # 1) vk_url — HTTP VK CDN (после relay) ИЛИ локальный подписанный URL.
    vk_url = row.get("vk_url")
    if isinstance(vk_url, str) and vk_url.startswith(("http://", "https://")):
        data = _download_by_url(vk_url)

    # 1b) Fallback: если vk_url умер — запросим свежий через photos.getById.
    if data is None and row.get("vk_owner_id") and row.get("vk_photo_id"):
        try:
            from core.media import get_fresh_vk_photo_url
            fresh_url = get_fresh_vk_photo_url(
                row["vk_owner_id"], row["vk_photo_id"])
            if fresh_url:
                data = _download_by_url(fresh_url)
                if data:
                    logger.info(
                        "get_media_bytes: fresh vk_url via photos.getById "
                        "(media_id=%s)", media_id)
        except Exception as e:
            logger.warning(
                "get_media_bytes: photos.getById fallback failed "
                "(media_id=%s): %s", media_id, e)

    # 2) TG file_id.
    if data is None and row["source_file_id"] and (
            row["source_platform"] == "tg"
            or row["user_platform"] == "tg"):
        data = _download_tg_file(row["source_file_id"])

    # 3) url — HTTP (MAX CDN / VK user CDN).
    if data is None and row["url"]:
        url = row["url"]
        if url.startswith(("http://", "https://")):
            data = _download_by_url(url)

    # 4) Финальная попытка — TG file_id.
    if data is None and row["source_file_id"]:
        data = _download_tg_file(row["source_file_id"])

    if data is None:
        return None
    with _cache_lock:
        try:
            tmp = path + ".tmp"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        except Exception as e:
            logger.warning("media cache write failed: %s", e)
    return data


# ============================================================
#  ПУБЛИЧНЫЙ URL ДЛЯ БОТА (HMAC-подпись)
# ============================================================

def _signing_key() -> bytes:
    key = os.getenv("WEB_SECRET_KEY") or "dev-insecure-key"
    return key.encode("utf-8")


def sign_media_url(media_id: int) -> str:
    import hashlib
    import hmac

    base = (os.getenv("WEB_PUBLIC_URL")
            or f"http://{os.getenv('WEB_HOST', '127.0.0.1')}:{os.getenv('WEB_PORT', '8000')}")
    base = base.rstrip("/")
    sig = hmac.new(_signing_key(), str(media_id).encode("utf-8"),
                   hashlib.sha256).hexdigest()[:32]
    return f"{base}/media/raw/{media_id}?sig={sig}"


def verify_media_sig(media_id: int, sig: str) -> bool:
    import hashlib
    import hmac

    expected = hmac.new(_signing_key(), str(media_id).encode("utf-8"),
                        hashlib.sha256).hexdigest()[:32]
    return hmac.compare_digest(expected, (sig or ""))


def save_media_bytes_to_cache(media_id: int, data: bytes) -> None:
    path = _cache_path(media_id)
    with _cache_lock:
        try:
            tmp = path + ".tmp"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        except Exception as e:
            logger.warning("media cache write failed: %s", e)