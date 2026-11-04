# core/media.py
"""Релей медиа TG → VK + удаление с VK CDN."""
import asyncio
import io
import logging
import os
import re
import threading
import time
from typing import Optional
import ipaddress
import socket

import requests
from requests.adapters import HTTPAdapter
from vk_api import VkApi
from vk_api.upload import VkUpload
from urllib.parse import urlparse as _urlparse

logger = logging.getLogger("media")
relay_logger = logging.getLogger("media.relay")
delete_logger = logging.getLogger("media.delete")
retry_logger = logging.getLogger("media.retry")

MAX_RELAY_SIZE = 20 * 1024 * 1024
# Лимит на скачивание в локальный кэш (фото + документы).
_MAX_CACHE_SIZE = 25 * 1024 * 1024


# --- SSRF-ЗАЩИТА ДЛЯ URL РЕЛЕЯ ---

def _is_trusted_web_url(url: str) -> bool:
    if not url:
        return False
    base = (os.getenv("WEB_PUBLIC_URL") or "").rstrip("/")
    if not base:
        host = os.getenv("WEB_HOST", "127.0.0.1")
        port = os.getenv("WEB_PORT", "8000")
        base = f"http://{host}:{port}"
    return url.startswith(base + "/") or url == base


def is_allowed_media_url(url: str) -> bool:
    if _is_trusted_web_url(url):
        return True

    from core.limits import ALLOWED_MEDIA_HOSTS

    try:
        p = _urlparse(url)
    except Exception:
        return False
    if p.scheme not in ("http", "https") or not p.hostname:
        return False

    try:
        port = p.port
    except ValueError:
        return False
    if port is not None and port not in (80, 443):
        return False

    host = p.hostname.lower()

    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        pass

    if not any(host == h or host.endswith("." + h)
               for h in ALLOWED_MEDIA_HOSTS):
        return False

    if port is None:
        port = 443 if p.scheme == "https" else 80
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, OSError):
        return False
    if not infos:
        return False

    for info in infos:
        try:
            addr = info[4][0]
            ip = ipaddress.ip_address(addr)
        except (IndexError, ValueError):
            return False
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return False

    return True


_MAX_PHOTO_PIXELS = 40_000_000

_VK_ATTACHMENT_RE = re.compile(r"^(?:photo|doc)-?\d+_\d+$")


def is_vk_attachment_string(s) -> bool:
    return isinstance(s, str) and _VK_ATTACHMENT_RE.match(s) is not None


def _reencode_photo(file_bytes):
    import io as _io
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = _MAX_PHOTO_PIXELS

    img = Image.open(_io.BytesIO(file_bytes))
    img.verify()

    img = Image.open(_io.BytesIO(file_bytes))

    if img.mode in ("RGBA", "LA") or (
        img.mode == "P" and "transparency" in img.info
    ):
        rgba = img.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.split()[-1])
        img = background
    else:
        img = img.convert("RGB")

    out = _io.BytesIO()
    img.save(out, format="JPEG", quality=92)
    return out.getvalue()


def _read_vk_group_id():
    raw = (os.getenv("VK_GROUP_ID") or "").strip()
    if not raw or raw == "0":
        return None
    try:
        return int(raw)
    except ValueError:
        return None


_VK_GROUP_ID = _read_vk_group_id()


def _read_vk_photos_album_id():
    """ID альбома сообщества для заявок.

    По умолчанию 0 — тогда используется старый saveMessagesPhoto
    (фото в спец-альбоме, CDN ~1 час). Если задан положительный ID
    обычного альбома сообщества — используется photos.getUploadServer
    + photos.save (фото в обычном альбоме, CDN бессрочно).
    """
    raw = (os.getenv("VK_PHOTOS_ALBUM_ID") or "0").strip()
    try:
        return int(raw)
    except ValueError:
        return 0


_VK_PHOTOS_ALBUM_ID = _read_vk_photos_album_id()

_TG_PROXY = (
    os.getenv("TG_PROXY_URL")
    or os.getenv("HTTPS_PROXY")
    or os.getenv("https_proxy")
)


# --- СЕССИИ С ПУЛАМИ СОЕДИНЕНИЙ ---

_tg_session = None
_vk_http_session = None
_sessions_lock = threading.Lock()

_vk_session = None
_vk_upload = None
_vk_lock = threading.Lock()


def _make_session(proxies, trust_env=False,
                  pool_connections=10, pool_maxsize=20):
    s = requests.Session()
    s.trust_env = trust_env
    s.proxies = proxies or {"http": None, "https": None}
    from urllib3.util.retry import Retry
    retry_policy = Retry(
        total=3, connect=3, read=3, status=3,
        backoff_factor=0.1,
        status_forcelist=(500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST", "PUT"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(
        pool_connections=pool_connections,
        pool_maxsize=pool_maxsize,
        max_retries=retry_policy,
    )
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def _get_tg_session():
    global _tg_session
    with _sessions_lock:
        if _tg_session is None:
            if _TG_PROXY:
                proxies = {"http": _TG_PROXY, "https": _TG_PROXY}
                relay_logger.info("TG session: proxy=%s", _TG_PROXY)
            else:
                proxies = None
                relay_logger.warning(
                    "TG session: прокси не задан — TG может быть недоступен")
            _tg_session = _make_session(proxies, trust_env=False)
        return _tg_session


def _get_vk_http_session():
    global _vk_http_session
    with _sessions_lock:
        if _vk_http_session is None:
            _vk_http_session = _make_session(
                proxies={"http": None, "https": None},
                trust_env=False,
            )
        return _vk_http_session


def _ensure_vk_uploader():
    global _vk_session, _vk_upload
    with _vk_lock:
        if _vk_upload is not None:
            return _vk_upload
        token = os.getenv("VK_RELAY_TOKEN") or os.getenv("VK_BOT_TOKEN")
        if not token or token.startswith("fake-"):
            relay_logger.warning("VK-токен не задан — relay отключён")
            return None
        try:
            _vk_session = VkApi(token=token, session=_get_vk_http_session())
            try:
                _vk_session.http.trust_env = False
                _vk_session.http.proxies = {"http": None, "https": None}
            except Exception:
                pass
            _vk_upload = VkUpload(_vk_session)
            relay_logger.info(
                "VkUpload готов (media.py rev=7, group_id=%s, album_id=%s)",
                _VK_GROUP_ID, _VK_PHOTOS_ALBUM_ID,
            )
            return _vk_upload
        except Exception as e:
            relay_logger.error("VkUpload init failed: %s", e)
            return None


def warm_up_vk():
    if _ensure_vk_uploader() is None:
        relay_logger.warning("warm_up_vk: VK uploader не инициализирован, пропуск")
        return

    try:
        t0 = time.perf_counter()
        _vk_session.get_api().users.get(user_ids=1)
        relay_logger.info("warm_up_vk: users.get = %.2fs",
                          time.perf_counter() - t0)
    except Exception as e:
        relay_logger.warning("warm_up_vk: users.get failed: %s", e)

    upload_url = None
    try:
        t0 = time.perf_counter()
        params = {}
        if _VK_GROUP_ID:
            params["group_id"] = _VK_GROUP_ID
        server = _vk_session.get_api().photos.getMessagesUploadServer(**params)
        upload_url = server.get("upload_url")
        relay_logger.info("warm_up_vk: getMessagesUploadServer = %.2fs",
                          time.perf_counter() - t0)
    except Exception as e:
        relay_logger.warning("warm_up_vk: getMessagesUploadServer failed: %s", e)

    if upload_url:
        try:
            t0 = time.perf_counter()
            r = _get_vk_http_session().post(
                upload_url,
                files={"photo": ("warmup.jpg", b"\xff" * 1024)},
                timeout=(10, 60),
            )
            if r.status_code == 405:
                relay_logger.debug(
                    "warm_up_vk: upload POST = %.2fs (405 expected)",
                    time.perf_counter() - t0)
            else:
                r.raise_for_status()
                relay_logger.info(
                    "warm_up_vk: upload POST = %.2fs",
                    time.perf_counter() - t0)
        except Exception as e:
            relay_logger.warning("warm_up_vk: upload POST failed: %s", e)


# --- VK UPLOAD С ФАЗНЫМИ ТАЙМИНГАМИ ---

def _warm_upload_host(http, upload_url: str):
    try:
        http.head(upload_url, timeout=5)
    except Exception:
        pass


def _vk_upload_photo_bytes(file_bytes: bytes):
    """Загружает фото в VK.

    Приоритет:
      * VK_PHOTOS_ALBUM_ID > 0 → photos.getUploadServer + photos.save
        (обычный альбом сообщества, CDN бессрочно).
      * VK_PHOTOS_ALBUM_ID = 0 → saveMessagesPhoto
        (спец-альбом, CDN ~1 час).
    """
    if _VK_PHOTOS_ALBUM_ID <= 0:
        relay_logger.info(
            "VK photos: VK_PHOTOS_ALBUM_ID не задан — "
            "fallback на saveMessagesPhoto (спец-альбом, CDN ~1 час)")
        return _vk_upload_photo_bytes_messages(file_bytes)
    return _vk_upload_photo_bytes_save(file_bytes)


def _vk_upload_photo_bytes_save(file_bytes: bytes):
    """Загрузка через photos.getUploadServer + photos.save.

    Фото попадают в обычный альбом сообщества. CDN живёт бессрочно,
    photos.getById и photos.delete работают для админа.
    """
    if _vk_session is None:
        relay_logger.error(
            "_vk_upload_photo_bytes_save: VK session не инициализирован")
        return None

    try:
        file_bytes = _reencode_photo(file_bytes)
    except Exception as e:
        relay_logger.warning("photo reencode/reject: %s", e)
        return None

    api = _vk_session.get_api()
    http = _get_vk_http_session()

    max_attempts = 4
    last_err = None

    for attempt in range(1, max_attempts + 1):
        try:
            t_a = time.perf_counter()
            server = api.photos.getUploadServer(
                group_id=_VK_GROUP_ID,
                album_id=_VK_PHOTOS_ALBUM_ID,
            )
            t_b = time.perf_counter()

            upload_url = server.get("upload_url")
            actual_album_id = server.get("aid") or _VK_PHOTOS_ALBUM_ID

            if not upload_url:
                relay_logger.error(
                    "VK photos.getUploadServer: нет upload_url: %r", server)
                last_err = "no upload_url"
                continue

            _warm_upload_host(http, upload_url)

            r = http.post(
                upload_url,
                files={"photo": ("photo.jpg", file_bytes)},
                timeout=(10, 120),
            )
            r.raise_for_status()
            t_c = time.perf_counter()

            try:
                uploaded = r.json()
            except Exception as e:
                relay_logger.warning(
                    "VK photos upload: JSON parse failed (attempt %d/%d): %s. "
                    "Content-Type=%r, body=%r",
                    attempt, max_attempts, e,
                    r.headers.get("Content-Type"), r.text[:300],
                )
                last_err = f"json parse: {e}"
                if attempt < max_attempts:
                    time.sleep(0.5)
                continue

            if not isinstance(uploaded, dict) or not all(
                k in uploaded and uploaded[k]
                for k in ("server", "photos_list", "hash")
            ):
                relay_logger.warning(
                    "VK photos upload: неполный ответ (attempt %d/%d): %r",
                    attempt, max_attempts, uploaded)
                last_err = f"incomplete response: {uploaded!r}"
                if attempt < max_attempts:
                    time.sleep(1.2)
                continue

            saved = api.photos.save(
                group_id=_VK_GROUP_ID,
                album_id=actual_album_id,
                server=uploaded["server"],
                photos_list=uploaded["photos_list"],
                hash=uploaded["hash"],
            )
            t_d = time.perf_counter()

            relay_logger.info(
                "VK photos phases (save): getUploadServer=%.2fs POST=%.2fs "
                "save=%.2fs%s",
                t_b - t_a, t_c - t_b, t_d - t_c,
                f" (attempt {attempt}/{max_attempts})" if attempt > 1 else "",
            )

            if not saved:
                last_err = "empty saved"
                if attempt < max_attempts:
                    time.sleep(0.5)
                continue

            photo_obj = saved[0] if isinstance(saved, list) else saved
            return photo_obj

        except Exception as e:
            last_err = str(e)
            relay_logger.warning(
                "VK photos upload phases failed (attempt %d/%d): %s",
                attempt, max_attempts, e)
            if attempt < max_attempts:
                time.sleep(0.2 * attempt)
                continue

    relay_logger.error(
        "VK photos upload failed после %d попыток: %s",
        max_attempts, last_err)
    return None


def _vk_upload_photo_bytes_messages(file_bytes: bytes):
    """Fallback: старый saveMessagesPhoto.

    Работает с community token. Фото попадают в скрытый
    «специальный» альбом сообщений. CDN живёт ~1 час,
    photos.getById через несколько часов даёт [200].
    """
    if _vk_session is None:
        relay_logger.error(
            "_vk_upload_photo_bytes_messages: VK session не инициализирован")
        return None

    try:
        file_bytes = _reencode_photo(file_bytes)
    except Exception as e:
        relay_logger.warning("photo reencode/reject: %s", e)
        return None

    api = _vk_session.get_api()
    http = _get_vk_http_session()

    max_attempts = 4
    last_err = None

    for attempt in range(1, max_attempts + 1):
        try:
            t_a = time.perf_counter()
            params = {}
            if _VK_GROUP_ID:
                params["group_id"] = _VK_GROUP_ID
            server = api.photos.getMessagesUploadServer(**params)
            t_b = time.perf_counter()

            upload_url = server.get("upload_url")
            if not upload_url:
                relay_logger.error("VK не вернул upload_url: %r", server)
                last_err = "no upload_url"
                continue

            _warm_upload_host(http, upload_url)

            r = http.post(
                upload_url,
                files={"photo": ("photo.jpg", file_bytes)},
                timeout=(10, 120),
            )
            r.raise_for_status()
            t_c = time.perf_counter()

            try:
                uploaded = r.json()
            except Exception as e:
                relay_logger.warning(
                    "VK upload: JSON parse failed (attempt %d/%d): %s. "
                    "Content-Type=%r, body=%r",
                    attempt, max_attempts, e,
                    r.headers.get("Content-Type"), r.text[:300],
                )
                last_err = f"json parse: {e}"
                if attempt < max_attempts:
                    time.sleep(0.5)
                continue

            if not isinstance(uploaded, dict) or not all(
                k in uploaded and uploaded[k]
                for k in ("photo", "server", "hash")
            ):
                relay_logger.warning(
                    "VK upload: неполный ответ (attempt %d/%d): %r",
                    attempt, max_attempts, uploaded)
                last_err = f"incomplete response: {uploaded!r}"
                if attempt < max_attempts:
                    time.sleep(1.2)
                continue

            saved = api.photos.saveMessagesPhoto(
                photo=uploaded["photo"],
                server=uploaded["server"],
                hash=uploaded["hash"],
            )
            t_d = time.perf_counter()

            relay_logger.info(
                "VK phases (messages): getUploadServer=%.2fs POST=%.2fs "
                "save=%.2fs%s",
                t_b - t_a, t_c - t_b, t_d - t_c,
                f" (attempt {attempt}/{max_attempts})" if attempt > 1 else "",
            )

            if not saved:
                last_err = "empty saved"
                if attempt < max_attempts:
                    time.sleep(0.5)
                continue

            return saved[0]

        except Exception as e:
            last_err = str(e)
            relay_logger.warning(
                "VK upload phases failed (attempt %d/%d): %s",
                attempt, max_attempts, e)
            if attempt < max_attempts:
                time.sleep(0.2 * attempt)
                continue

    relay_logger.error(
        "VK upload phases failed после %d попыток: %s",
        max_attempts, last_err)
    return None


def _vk_upload_doc_bytes(file_bytes: bytes, filename: str = "document.pdf",
                         peer_id: Optional[int] = None):
    if _vk_session is None:
        relay_logger.error("_vk_upload_doc_bytes: VK session не инициализирован")
        return None

    api = _vk_session.get_api()
    http = _get_vk_http_session()

    max_attempts = 4
    last_err = None

    for attempt in range(1, max_attempts + 1):
        try:
            t_a = time.perf_counter()
            if _VK_GROUP_ID:
                actual_peer = -abs(_VK_GROUP_ID)
                server = api.docs.getMessagesUploadServer(
                    type="doc", peer_id=actual_peer)
            elif peer_id is not None:
                server = api.docs.getMessagesUploadServer(
                    type="doc", peer_id=peer_id)
            else:
                server = api.docs.getUploadServer(type="doc")
            t_b = time.perf_counter()

            upload_url = server.get("upload_url")
            if not upload_url:
                relay_logger.error("VK docs: не вернул upload_url: %r", server)
                last_err = "no upload_url"
                continue

            r = http.post(
                upload_url,
                files={"file": (filename, file_bytes)},
                timeout=(10, 180),
            )
            r.raise_for_status()
            t_c = time.perf_counter()

            try:
                uploaded = r.json()
            except Exception as e:
                relay_logger.warning(
                    "VK docs: JSON parse failed (attempt %d/%d): %s. body=%r",
                    attempt, max_attempts, e, r.text[:300])
                last_err = f"json parse: {e}"
                if attempt < max_attempts:
                    time.sleep(0.5)
                continue

            if not isinstance(uploaded, dict) or not uploaded.get("file"):
                relay_logger.warning(
                    "VK docs: неполный ответ (attempt %d/%d): %r",
                    attempt, max_attempts, uploaded)
                last_err = f"incomplete response: {uploaded!r}"
                if attempt < max_attempts:
                    time.sleep(1.2)
                continue

            saved = api.docs.save(file=uploaded["file"], title=filename)
            t_d = time.perf_counter()

            relay_logger.info(
                "VK docs phases: getUploadServer=%.2fs POST=%.2fs save=%.2fs%s",
                t_b - t_a, t_c - t_b, t_d - t_c,
                f" (attempt {attempt}/{max_attempts})" if attempt > 1 else "",
            )

            if not saved:
                last_err = "empty saved"
                if attempt < max_attempts:
                    time.sleep(0.5)
                continue

            doc_obj = saved[0] if isinstance(saved, list) else saved
            if isinstance(doc_obj, dict) and "doc" in doc_obj:
                doc_obj = doc_obj["doc"]
            return doc_obj

        except Exception as e:
            last_err = str(e)
            relay_logger.warning(
                "VK docs upload failed (attempt %d/%d): %s",
                attempt, max_attempts, e)
            if attempt < max_attempts:
                time.sleep(0.2 * attempt)
                continue

    relay_logger.error(
        "VK docs upload failed после %d попыток: %s", max_attempts, last_err)
    return None


# --- RELAY ---

def _sync_relay_bytes(file_bytes: bytes):
    if _ensure_vk_uploader() is None:
        return None
    if not file_bytes:
        relay_logger.warning("relay(bytes): пустой файл")
        return None
    if len(file_bytes) > MAX_RELAY_SIZE:
        relay_logger.warning("relay(bytes): файл слишком большой: %d",
                             len(file_bytes))
        return None

    t0 = time.perf_counter()
    photo = _vk_upload_photo_bytes(file_bytes)
    t_total = time.perf_counter() - t0

    if not photo:
        return None

    sizes = photo.get("sizes") or []
    if not sizes:
        relay_logger.warning("VK не вернул sizes")
        return None

    max_size = max(
        sizes,
        key=lambda s: (s.get("width") or 0) * (s.get("height") or 0),
    )
    vk_url = max_size.get("url")
    if not vk_url:
        relay_logger.warning("VK не вернул url")
        return None

    relay_logger.info(
        "upload OK size=%d total=%.2fs url=%s",
        len(file_bytes), t_total, vk_url[:80],
    )
    return {
        "url": vk_url,
        "owner_id": photo.get("owner_id"),
        "photo_id": photo.get("id"),
    }


async def relay_vk_upload(file_bytes: bytes):
    if not file_bytes:
        return None
    return await asyncio.to_thread(_sync_relay_bytes, file_bytes)


def _sync_relay_doc_bytes(file_bytes: bytes, filename: str,
                          peer_id: Optional[int]):
    if _ensure_vk_uploader() is None:
        return None
    if not file_bytes:
        relay_logger.warning("relay(doc): пустой файл")
        return None
    if len(file_bytes) > MAX_RELAY_SIZE:
        relay_logger.warning("relay(doc): файл слишком большой: %d",
                             len(file_bytes))
        return None

    t0 = time.perf_counter()
    doc = _vk_upload_doc_bytes(file_bytes, filename=filename, peer_id=peer_id)
    t_total = time.perf_counter() - t0
    if not doc:
        return None

    url = doc.get("url")
    if not url:
        relay_logger.warning("VK docs: не вернул url: %r", doc)
        return None

    relay_logger.info(
        "upload doc OK size=%d total=%.2fs url=%s",
        len(file_bytes), t_total, url[:80],
    )
    return {
        "url": url,
        "owner_id": doc.get("owner_id"),
        "doc_id": doc.get("id"),
        "title": doc.get("title"),
    }


async def relay_vk_upload_doc(file_bytes: bytes, filename: str = "document.pdf",
                              peer_id: Optional[int] = None):
    if not file_bytes:
        return None
    return await asyncio.to_thread(
        _sync_relay_doc_bytes, file_bytes, filename, peer_id
    )


def _sync_relay(tg_file_url: str):
    if _ensure_vk_uploader() is None:
        return None

    t0 = time.perf_counter()
    resp = None
    try:
        session = _get_tg_session()
        resp = session.get(tg_file_url, timeout=30, stream=True)
        if resp.status_code != 200:
            relay_logger.error("download status=%s url=%s",
                               resp.status_code, tg_file_url[:80])
            return None

        cl = resp.headers.get("Content-Length")
        if cl and int(cl) > MAX_RELAY_SIZE:
            relay_logger.warning("файл слишком большой: %s байт", cl)
            return None

        chunks = []
        total = 0
        for chunk in resp.iter_content(chunk_size=8192):
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_RELAY_SIZE:
                relay_logger.warning(
                    "превышен MAX_RELAY_SIZE во время скачивания: %s", total)
                return None
            chunks.append(chunk)
        file_data = b"".join(chunks)
        if not file_data:
            return None

        photo = _vk_upload_photo_bytes(file_data)
        if not photo:
            return None

        sizes = photo.get("sizes") or []
        if not sizes:
            return None
        max_size = max(
            sizes,
            key=lambda s: (s.get("width") or 0) * (s.get("height") or 0),
        )
        vk_url = max_size.get("url")
        if not vk_url:
            return None

        relay_logger.info(
            "upload OK size=%d total=%.2fs url=%s",
            len(file_data), time.perf_counter() - t0, vk_url[:80])
        return {
            "url": vk_url,
            "owner_id": photo.get("owner_id"),
            "photo_id": photo.get("id"),
        }
    except Exception:
        relay_logger.exception("relay(url) failed (elapsed=%.2fs)",
                               time.perf_counter() - t0)
        return None
    finally:
        if resp is not None:
            resp.close()


async def relay_tg_photo_to_vk(tg_file_url: str):
    if not tg_file_url:
        return None
    return await asyncio.to_thread(_sync_relay, tg_file_url)


# ============================================================
#  ЛОКАЛЬНЫЙ КЭШ (чтобы URL жил вечно, а не 1-6 часов VK CDN)
# ============================================================

def cache_media_locally(vk_cdn_url: str, media_id: int,
                        max_attempts: int = 3) -> Optional[str]:
    """Скачивает файл с VK CDN и кладёт в web_app/media_cache/{media_id}.

    VK CDN после photos.save может отдавать 404 в первые 1-3 секунды
    (файл ещё не «прогрет» на CDN-нодах). Делаем max_attempts попыток
    с паузой 1.5 сек.

    max_attempts=3 по умолчанию (вызов из воркеров).
    max_attempts=2 — для фонового кэширования квитанций, чтобы не
    задерживать state machine.

    Возвращает подписанный локальный URL или None при ошибке.
    """
    if not vk_cdn_url or not isinstance(vk_cdn_url, str):
        return None
    if not vk_cdn_url.startswith(("http://", "https://")):
        return None
    if max_attempts < 1:
        max_attempts = 1

    session = _get_vk_http_session()
    data = None

    for attempt in range(1, max_attempts + 1):
        try:
            resp = session.get(vk_cdn_url, timeout=(10, 60), stream=True)
            if resp.status_code == 404:
                resp.close()
                if attempt < max_attempts:
                    relay_logger.info(
                        "cache_media_locally: 404 for media_id=%s "
                        "(attempt %d/%d), retry in 1.5s",
                        media_id, attempt, max_attempts)
                    time.sleep(1.5)
                    continue
                relay_logger.warning(
                    "cache_media_locally: status=404 for media_id=%s "
                    "after %d attempts", media_id, max_attempts)
                return None
            if resp.status_code != 200:
                status = resp.status_code
                resp.close()
                relay_logger.warning(
                    "cache_media_locally: status=%s for media_id=%s",
                    status, media_id)
                return None

            chunks = []
            total = 0
            for chunk in resp.iter_content(chunk_size=8192):
                if not chunk:
                    break
                total += len(chunk)
                if total > _MAX_CACHE_SIZE:
                    relay_logger.warning(
                        "cache_media_locally: file too big (%d > %d), "
                        "media_id=%s", total, _MAX_CACHE_SIZE, media_id)
                    resp.close()
                    return None
                chunks.append(chunk)
            resp.close()
            data = b"".join(chunks)
            break
        except Exception as e:
            relay_logger.warning(
                "cache_media_locally failed (media_id=%s, attempt %d): %s",
                media_id, attempt, e)
            if attempt < max_attempts:
                time.sleep(1.5)
                continue
            return None

    if not data:
        return None

    from web_app.media import save_media_bytes_to_cache, sign_media_url
    save_media_bytes_to_cache(media_id, data)
    local_url = sign_media_url(media_id)
    relay_logger.info(
        "cache_media_locally: saved %d bytes for media_id=%s",
        len(data), media_id)
    return local_url


def delete_media_cache(media_id: int) -> bool:
    """Удаляет web_app/media_cache/{media_id} (для GDPR-зачистки)."""
    if not media_id:
        return False
    try:
        from web_app.media import _cache_path
        path = _cache_path(media_id)
        import os as _os
        if _os.path.isfile(path):
            _os.remove(path)
            delete_logger.info("deleted media_cache/%s", media_id)
            return True
    except Exception as e:
        delete_logger.warning(
            "delete_media_cache failed (media_id=%s): %s", media_id, e)
    return False


# --- ПОЛУЧЕНИЕ СВЕЖЕГО VK URL ---

def get_fresh_vk_photo_url(owner_id, photo_id) -> Optional[str]:
    """Получает свежий CDN URL для VK-фото через photos.getById.

    Работает для фото в обычных альбомах сообщества (после photos.save).
    Для старых saveMessagesPhoto может вернуть [200] — тогда None.
    """
    if _ensure_vk_uploader() is None:
        return None
    if owner_id is None or photo_id is None:
        return None
    try:
        api = _vk_session.get_api()
        photos = api.photos.getById(photos=f"{owner_id}_{photo_id}")
        if not photos:
            return None
        sizes = photos[0].get("sizes") or []
        if not sizes:
            return None
        max_size = max(
            sizes,
            key=lambda s: (s.get("width") or 0) * (s.get("height") or 0),
        )
        return max_size.get("url")
    except Exception as e:
        relay_logger.warning(
            "get_fresh_vk_photo_url failed (owner=%s, photo=%s): %s",
            owner_id, photo_id, str(e)[:200])
        return None


# --- УДАЛЕНИЕ VK-ФОТО / ДОКУМЕНТОВ ---

_delete_token_lock = threading.Lock()
_delete_token_index = 0


def _get_delete_tokens() -> list:
    tokens = []
    seen = set()
    for name in ("VK_DELETE_TOKEN", "VK_DELETE_TOKEN_FALLBACK", "VK_USER_TOKEN"):
        tok = (os.getenv(name) or "").strip()
        if not tok:
            continue
        if tok.startswith("fake-"):
            continue
        if tok in seen:
            continue
        tokens.append(tok)
        seen.add(tok)
    return tokens


def _current_delete_token() -> Optional[str]:
    tokens = _get_delete_tokens()
    if not tokens:
        return None
    with _delete_token_lock:
        return tokens[_delete_token_index % len(tokens)]


def _advance_delete_token() -> bool:
    global _delete_token_index
    tokens = _get_delete_tokens()
    if len(tokens) < 2:
        return False
    with _delete_token_lock:
        _delete_token_index = (_delete_token_index + 1) % len(tokens)
        new_idx = _delete_token_index
    delete_logger.warning(
        "VK delete: переключение на токен #%d (всего %d)",
        new_idx, len(tokens))
    return True


def _get_vk_delete_token(owner_id=None):
    return _current_delete_token()


def delete_vk_doc(owner_id, doc_id):
    if owner_id is None or doc_id is None:
        return False, "owner_id or doc_id is None"

    tokens = _get_delete_tokens()
    if not tokens:
        return False, (
            "skipped: no user token (set VK_DELETE_TOKEN или "
            "VK_DELETE_TOKEN_FALLBACK с правом docs)")

    last_err = None
    for _ in range(len(tokens)):
        token = _current_delete_token()
        if not token:
            break
        try:
            session = VkApi(token=token, session=_get_vk_http_session())
            session.http.timeout = 30
            try:
                session.http.trust_env = False
                session.http.proxies = {"http": None, "https": None}
            except Exception:
                pass
            session.get_api().docs.delete(owner_id=owner_id, doc_id=doc_id)
            delete_logger.info("deleted doc owner=%s doc=%s", owner_id, doc_id)
            return True, None
        except Exception as e:
            err = str(e)[:300]
            last_err = err
            if _is_permanent_auth_error(err):
                delete_logger.warning(
                    "delete doc auth error (owner=%s, doc=%s): %s — "
                    "переключаю токен", owner_id, doc_id, err)
                if not _advance_delete_token():
                    break
                continue
            delete_logger.warning("delete doc failed (%s,%s): %s",
                                  owner_id, doc_id, err)
            return False, err

    return False, last_err or "all tokens failed"


def delete_vk_photo(owner_id, photo_id):
    if owner_id is None or photo_id is None:
        return False, "owner_id or photo_id is None"

    tokens = _get_delete_tokens()
    if not tokens:
        return False, (
            "skipped: no user token (set VK_DELETE_TOKEN или "
            "VK_DELETE_TOKEN_FALLBACK с правом photos)")

    last_err = None
    for _ in range(len(tokens)):
        token = _current_delete_token()
        if not token:
            break
        try:
            session = VkApi(token=token, session=_get_vk_http_session())
            session.http.timeout = 30
            try:
                session.http.trust_env = False
                session.http.proxies = {"http": None, "https": None}
            except Exception:
                pass
            session.get_api().photos.delete(owner_id=owner_id,
                                            photo_id=photo_id)
            delete_logger.info("deleted owner=%s photo=%s", owner_id, photo_id)
            return True, None
        except Exception as e:
            err = str(e)[:300]
            last_err = err
            if "[5]" in err:
                delete_logger.warning(
                    "delete token expired (%s,%s): %s — переключаю",
                    owner_id, photo_id, err)
                if not _advance_delete_token():
                    break
                continue
            if "[15]" in err:
                delete_logger.warning(
                    "delete no scope photos (%s,%s): %s — переключаю",
                    owner_id, photo_id, err)
                if not _advance_delete_token():
                    break
                continue
            if "[27]" in err:
                delete_logger.warning(
                    "delete group auth unavailable (%s,%s): %s — переключаю",
                    owner_id, photo_id, err)
                if not _advance_delete_token():
                    break
                continue
            if "[200]" in err or "[3]" in err:
                delete_logger.warning(
                    "delete forbidden (%s,%s): %s — переключаю",
                    owner_id, photo_id, err)
                if not _advance_delete_token():
                    break
                continue
            if "[9]" in err or "flood control" in err.lower():
                delete_logger.info(
                    "delete flood control (%s,%s) — пропускаю, повтор позже",
                    owner_id, photo_id)
                return False, err
            delete_logger.warning("delete failed (%s,%s): %s",
                                  owner_id, photo_id, err)
            return False, err

    return False, last_err or "all tokens failed"


# --- ИЗВЛЕЧЕНИЕ МЕТА ИЗ ВЛОЖЕНИЙ ---

def extract_photo_meta(attachments):
    result = []
    for att in attachments or []:
        if not isinstance(att, dict) or att.get("type") != "photo":
            continue
        photo = att.get("photo") or {}
        sizes = photo.get("sizes") or []
        if not sizes:
            continue
        last = sizes[-1]
        url = last.get("url")
        if not url:
            continue
        result.append({
            "url": url,
            "owner_id": photo.get("owner_id") or last.get("_vk_owner_id"),
            "photo_id": photo.get("id") or last.get("_vk_photo_id"),
            "kind": "photo",
            "source_file_id": att.get("_tg_source_file_id"),
            "source_platform": "tg" if att.get("_tg_source_file_id") else None,
            "vk_url": last.get("_vk_url"),
            "vk_owner_id": last.get("_vk_owner_id"),
            "vk_photo_id": last.get("_vk_photo_id"),
            "vk_doc_id": None,
        })
    return result


def extract_doc_meta(attachments):
    result = []
    for att in attachments or []:
        if not isinstance(att, dict) or att.get("type") != "doc":
            continue
        doc = att.get("doc") or {}
        url = doc.get("url")
        if not url:
            continue
        result.append({
            "url": url,
            "owner_id": doc.get("owner_id") or doc.get("_vk_owner_id"),
            "doc_id": doc.get("id") or doc.get("_vk_doc_id"),
            "kind": "doc",
            "source_file_id": att.get("_tg_source_file_id"),
            "source_platform": "tg" if att.get("_tg_source_file_id") else None,
            "vk_url": doc.get("_vk_url"),
            "vk_owner_id": doc.get("_vk_owner_id"),
            "vk_photo_id": None,
            "vk_doc_id": doc.get("_vk_doc_id"),
        })
    return result


# --- ФОНОВАЯ ДОЧИСТКА VK-ФОТО ---

_warned_no_delete_token = False
_DELETE_GAP_SEC = 1.1


def _is_flood_control_error(err: str) -> bool:
    if not err:
        return False
    return "[9]" in err or "flood control" in err.lower()


def _is_permanent_auth_error(err: str) -> bool:
    if not err:
        return False
    return any(code in err for code in ("[5]", "[15]", "[27]", "[200]"))


def _has_any_delete_token() -> bool:
    return len(_get_delete_tokens()) > 0


def retry_pending_deletions(batch_size: int = 5):
    global _warned_no_delete_token
    from core.db import (
        get_media_files_pending_delete,
        mark_media_delete_success, mark_media_delete_failed,
        mark_media_delete_flood,
    )

    if not _has_any_delete_token():
        if not _warned_no_delete_token:
            retry_logger.warning(
                "VK_DELETE_TOKEN / VK_DELETE_TOKEN_FALLBACK / VK_USER_TOKEN "
                "не заданы — удаление медиа из VK отключено.")
            _warned_no_delete_token = True
        return 0, 0, 0

    rows = get_media_files_pending_delete(limit=batch_size)
    if not rows:
        return 0, 0, 0

    tried = ok = fail = 0
    for idx, row in enumerate(rows):
        if idx > 0:
            time.sleep(_DELETE_GAP_SEC)

        tried += 1

        kind = row.get("kind") or ""
        kind_parts = kind.split("_")
        is_doc = kind.startswith("doc") or "doc" in kind_parts

        if is_doc and row["doc_id"] is not None:
            success, error = delete_vk_doc(row["owner_id"], row["doc_id"])
        elif row["photo_id"] is not None:
            success, error = delete_vk_photo(row["owner_id"], row["photo_id"])
        else:
            success, error = False, f"invalid kind={kind!r} for delete"

        if success:
            mark_media_delete_success(row["id"])
            ok += 1
            continue

        if _is_flood_control_error(error):
            mark_media_delete_flood(row["id"])
            retry_logger.info(
                "flood control на id=%s — откладываю на 30 мин, прерываю",
                row["id"])
            break

        if _is_permanent_auth_error(error):
            mark_media_delete_flood(row["id"])
            retry_logger.warning(
                "auth error на id=%s (owner=%s, %s) — все токены перепробованы",
                row["id"], row.get("owner_id"), (error or "")[:80])
            fail += 1
            continue

        mark_media_delete_failed(row["id"], error or "unknown")
        fail += 1

    if tried:
        retry_logger.info("tried=%d ok=%d fail=%d", tried, ok, fail)
    return tried, ok, fail