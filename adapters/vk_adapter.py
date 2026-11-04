# adapters/vk_adapter.py
"""Реализация MessengerAdapter для ВКонтакте (async-обёртка над vk_api)."""
import asyncio
import io
import json
import logging
import time
import threading
import uuid
import re
import requests
from requests.adapters import HTTPAdapter

try:
    import pypdfium2 as pdfium
except ImportError:
    pdfium = None

try:
    from PIL import Image
except ImportError:
    Image = None

MAX_RELAY_SIZE = 20 * 1024 * 1024

_VK_PHOTO_URL_PATTERN = re.compile(
    r"https://[a-z0-9\-]{1,63}\.(?:userapi\.com|vkuserphoto\.ru)"
    r"/[A-Za-z0-9_\-./%]+",
    re.IGNORECASE,
)

from core.limits import MAX_RELAY_URLS_PER_MSG
from typing import AsyncIterator, Optional, Tuple, List
from typing import Union

from vk_api import VkApi
from vk_api.bot_longpoll import VkBotLongPoll, VkBotEventType
from vk_api.upload import VkUpload

from adapters.base import MessengerAdapter

from core.media import (
    relay_vk_upload, relay_vk_upload_doc,
    is_allowed_media_url as _is_allowed_media_url,
)

logger = logging.getLogger("bright_city_bot")


def _safe_str(s) -> str:
    if s is None:
        return ""
    if isinstance(s, bytes):
        return s.decode("utf-8", errors="replace")
    return str(s)


def _next_random_id() -> int:
    return uuid.uuid4().int % (2 ** 31)


def _make_vk_session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False
    s.proxies = {"http": None, "https": None}
    adapter = HTTPAdapter(
        pool_connections=10, pool_maxsize=20, max_retries=1,
    )
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


class VkAdapter(MessengerAdapter):
    platform = "vk"

    def __init__(self, token: str, group_id: Optional[int] = None):
        self._http_session = _make_vk_session()
        self._session = VkApi(token=token, session=self._http_session)
        self._session.http.timeout = 30
        try:
            self._session.http.trust_env = False
            self._session.http.proxies = {"http": None, "https": None}
        except Exception:
            pass

        self._vk = self._session.get_api()

        if group_id is not None:
            self._group_id = group_id
        else:
            self._group_id = self._vk.groups.getById()[0]["id"]

        self._longpoll = VkBotLongPoll(
            self._session, group_id=self._group_id, wait=90)
        self._upload = VkUpload(self._session)

        self._name_cache = {}
        self._name_cache_lock = threading.Lock()
        self._name_cache_cleanup_last = 0.0

        self._active_media_sends = set()
        self._active_media_sends_lock = threading.Lock()

        self._unreachable = {}
        self._unreachable_lock = threading.Lock()
        self._UNREACHABLE_TTL = 6 * 3600

    async def listen(self) -> AsyncIterator[dict]:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()

        def producer():
            backoff = 5
            max_backoff = 60
            while True:
                try:
                    for event in self._longpoll.listen():
                        if event.type != VkBotEventType.MESSAGE_NEW:
                            continue
                        msg = event.obj.message
                        # VK-сообщение: message id (глобальный)
                        pmi = msg.get("id") or msg.get("conversation_message_id")

                        reply_to_platform_message_id = None
                        reply_msg = msg.get("reply_message")
                        if reply_msg:
                            rid = reply_msg.get("id") or reply_msg.get(
                                "conversation_message_id"
                            )
                            if rid:
                                reply_to_platform_message_id = str(rid)

                        normalized = {
                            "user_id": msg["from_id"],
                            "text": _safe_str(msg.get("text") or ""),
                            "attachments": self.normalize_attachments(
                                msg.get("attachments", [])),
                            "payload": msg.get("payload"),
                            "raw": msg,
                            "platform_message_id": str(pmi) if pmi else None,
                            "reply_to_platform_message_id": reply_to_platform_message_id,
                        }
                        loop.call_soon_threadsafe(queue.put_nowait, normalized)
                        backoff = 5
                except Exception as e:
                    logger.warning(
                        "VK longpoll died, reconnecting in %ds: %s",
                        backoff, e)
                    time.sleep(backoff)
                    backoff = min(backoff * 2, max_backoff)
                    try:
                        self._longpoll = VkBotLongPoll(
                            self._session,
                            group_id=self._group_id,
                            wait=90,
                        )
                    except Exception as e2:
                        logger.error("longpoll recreate failed: %s", e2)
                        time.sleep(min(backoff, max_backoff))

        thread = threading.Thread(
            target=producer, daemon=True, name="vk-longpoll")
        thread.start()

        while True:
            event = await queue.get()
            if event is None:
                return
            yield event

    def _is_unreachable(self, user_id: int) -> bool:
        with self._unreachable_lock:
            until = self._unreachable.get(user_id, 0.0)
            if until > time.time():
                return True
            if user_id in self._unreachable:
                del self._unreachable[user_id]
            return False

    def _mark_unreachable(self, user_id: int):
        with self._unreachable_lock:
            was_known = user_id in self._unreachable
            self._unreachable[user_id] = time.time() + self._UNREACHABLE_TTL
        if not was_known:
            logger.warning(
                "VK user %s недоступен для сообщений ([901]) — помечен на %d ч",
                user_id, self._UNREACHABLE_TTL // 3600,
            )

    def _send_message_sync(self, user_id: int, text: str,
                           keyboard=None, max_attempts: int = 2,
                           reply_to_platform_message_id: Optional[str] = None,
                           ) -> Optional[str]:
        """Возвращает platform_message_id отправленного сообщения (str)
        или None при ошибке.
        """
        max_attempts = max(max_attempts, 1)

        if self._is_unreachable(user_id):
            logger.debug("VK send_message skipped (user %s unreachable)", user_id)
            return None

        if text:
            try:
                self._extract_and_send_vk_photos_sync(user_id, text)
            except Exception as e:
                logger.warning("relay photos in VK send_message failed: %s", e)

        reply_to_int = None
        if reply_to_platform_message_id:
            try:
                reply_to_int = int(reply_to_platform_message_id)
            except (TypeError, ValueError):
                logger.warning(
                    "VK send_message: invalid reply_to_platform_message_id %r",
                    reply_to_platform_message_id)

        for attempt in range(max_attempts):
            try:
                params = {
                    "user_id": user_id,
                    "message": _safe_str(text),
                    "random_id": _next_random_id(),
                }
                if keyboard is not None:
                    params["keyboard"] = keyboard
                if reply_to_int is not None:
                    params["reply_to"] = reply_to_int
                resp = self._vk.messages.send(**params)
                # VK возвращает message_id (int) или список (для мультичата).
                if isinstance(resp, int):
                    return str(resp)
                if isinstance(resp, list) and resp:
                    return str(resp[0])
                return "sent"  # фолбэк: считаем успехом, но без id
            except Exception as e:
                err = str(e)
                if "901" in err or "without permission" in err.lower():
                    self._mark_unreachable(user_id)
                    return None
                if any(code in err for code in ("[902]", "[903]", "[904]")):
                    self._mark_unreachable(user_id)
                    return None
                # Оригинал для reply удалён — пробуем без reply.
                if reply_to_int is not None and "reply" in err.lower() and (
                    "not found" in err.lower() or "deleted" in err.lower()
                ):
                    logger.warning(
                        "VK reply target %s not found (user=%s) — "
                        "sending without reply",
                        reply_to_int, user_id)
                    reply_to_int = None
                    if attempt < max_attempts - 1:
                        continue
                logger.error(
                    "VK send_message failed (user %s, attempt %d/%d): %s",
                    user_id, attempt + 1, max_attempts, e,
                )
                if attempt < max_attempts - 1:
                    time.sleep(1)
                else:
                    return None
        return None

    async def send_message(self, user_id: int, text: str,
                           keyboard=None, max_attempts: int = 2,
                           parse_mode: Optional[str] = None,
                           keep: bool = False,
                           reply_to_platform_message_id: Optional[str] = None,
                           ) -> Union[str, None]:
        return await asyncio.to_thread(
            self._send_message_sync, user_id, text, keyboard, max_attempts,
            reply_to_platform_message_id,
        )

    def _edit_message_sync(self, user_id: int,
                           platform_message_id: str,
                           new_text: str) -> bool:
        """Редактирует своё сообщение через messages.edit.

        VK: message_id в messages.edit — это id сообщения в беседе
        (conversation_message_id, он же cmid). Мы сохраняем cmid во
        входящих событиях, но для исходящих берём то, что вернул
        messages.send — обычно это тот же id. Если VK не примет —
        возвращаем False, фронт откатится на «Уточнение».
        """
        try:
            mid = int(platform_message_id)
        except (TypeError, ValueError):
            return False

        try:
            self._vk.messages.edit(
                peer_id=user_id,
                message_id=mid,
                message=_safe_str(new_text),
            )
            logger.info(
                "VK edit_message OK: user=%s message_id=%s", user_id, mid)
            return True
        except Exception as e:
            logger.warning(
                "VK edit_message failed (user=%s, message_id=%s): %s",
                user_id, mid, e)
            return False

    async def edit_message(self, user_id: int,
                           platform_message_id: str,
                           new_text: str) -> bool:
        return await asyncio.to_thread(
            self._edit_message_sync, user_id, platform_message_id, new_text)

    def _send_attachment_sync(self, user_id: int, attachment,
                              caption: Optional[str] = None,
                              max_attempts: int = 2) -> bool:
        if isinstance(attachment, (list, tuple)):
            attachment = ",".join(attachment)
        max_attempts = max(max_attempts, 1)

        if self._is_unreachable(user_id):
            return False

        if isinstance(attachment, str) and attachment.startswith(
                ("http://", "https://")):
            try:
                resolved = self._resolve_url_attachment_sync(
                    attachment, user_id)
            except Exception as e:
                logger.warning(
                    "VK send_attachment: URL → VK upload failed (%s): %s",
                    attachment[:80], e)
                resolved = None
            if not resolved:
                try:
                    msg = _safe_str(caption or "")
                    if msg:
                        msg = f"{msg}\n\n{attachment}"
                    else:
                        msg = attachment
                    self._vk.messages.send(
                        user_id=user_id, message=msg,
                        random_id=_next_random_id(),
                    )
                    return True
                except Exception as e:
                    logger.error(
                        "VK: send URL as text fallback failed: %s", e)
                    return False
            attachment = resolved

        for attempt in range(max_attempts):
            try:
                self._vk.messages.send(
                    user_id=user_id,
                    attachment=str(attachment),
                    message=_safe_str(caption or ""),
                    random_id=_next_random_id(),
                )
                return True
            except Exception as e:
                err = str(e)
                if "901" in err or "without permission" in err.lower():
                    self._mark_unreachable(user_id)
                    return False
                if any(code in err for code in ("[902]", "[903]", "[904]")):
                    self._mark_unreachable(user_id)
                    return False
                logger.error(
                    "VK send_attachment failed (user %s, attempt %d/%d): %s",
                    user_id, attempt + 1, max_attempts, e,
                )
                if attempt < max_attempts - 1:
                    time.sleep(1)
                else:
                    return False
        return False

    def _resolve_url_attachment_sync(self, url: str,
                                     user_id: int) -> Optional[str]:
        # (без изменений от оригинала)
        try:
            from urllib.parse import urlparse as _urlparse
            try:
                _host = (_urlparse(url).hostname or "").lower()
            except Exception:
                _host = ""
            is_vk_cdn = bool(_host) and any(
                _host == h or _host.endswith("." + h)
                for h in ("userapi.com", "vkuserphoto.ru", "vk.com", "vk.ru"))
            if not is_vk_cdn:
                if not _is_allowed_media_url(url):
                    logger.warning(
                        "VK _resolve_url_attachment_sync: URL вне allow-list: "
                        "%s", url[:80])
                    return None

            from core.limits import MAX_DOC_SIZE, MAX_BANNER_SIZE
            MAX_SIZE = max(MAX_DOC_SIZE, MAX_BANNER_SIZE)

            resp = self._http_session.get(
                url, timeout=30, stream=True, allow_redirects=True)
            if resp.status_code != 200:
                logger.warning(
                    "VK _resolve_url_attachment_sync: status=%s для %s",
                    resp.status_code, url[:80])
                resp.close()
                return None

            content_type = (resp.headers.get("Content-Type") or "").lower()
            chunks = []
            total = 0
            for chunk in resp.iter_content(chunk_size=8192):
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_SIZE:
                    resp.close()
                    return None
                chunks.append(chunk)
            resp.close()
            file_data = b"".join(chunks)
            if not file_data:
                return None

            try:
                _path = (_urlparse(url).path or "").lower()
            except Exception:
                _path = url.lower()
            is_image = (
                content_type.startswith("image/")
                or any(_path.endswith(ext)
                       for ext in (".jpg", ".jpeg", ".png", ".gif",
                                   ".webp", ".bmp"))
            )
            if file_data.startswith(b'%PDF'):
                is_image = False
            elif file_data.startswith((b'\xff\xd8\xff',
                                       b'\x89PNG\r\n\x1a\n',
                                       b'GIF8', b'BM')):
                is_image = True

            if is_image:
                try:
                    photo = self._upload.photo_messages(
                        [io.BytesIO(file_data)])[0]
                    return f"photo{photo['owner_id']}_{photo['id']}"
                except Exception as e:
                    logger.warning(
                        "VK _resolve_url_attachment_sync: photo upload "
                        "failed: %s", e)
                    return None
            else:
                if file_data.startswith(b'%PDF'):
                    png_bytes = self._convert_pdf_to_png_bytes(file_data)
                    if png_bytes:
                        try:
                            photo = self._upload.photo_messages(
                                [io.BytesIO(png_bytes)])[0]
                            return f"photo{photo['owner_id']}_{photo['id']}"
                        except Exception as e:
                            logger.warning(
                                "VK PDF->PNG: photo upload failed: %s", e)
                doc_attachment = self._upload_doc_sync(
                    file_data, user_id, "document.pdf")
                if doc_attachment:
                    return doc_attachment
                return None
        except Exception as e:
            logger.exception(
                "VK _resolve_url_attachment_sync failed for %s: %s",
                url[:80], e)
            return None

    @staticmethod
    def _convert_pdf_to_png_bytes(pdf_bytes: bytes,
                                  dpi: int = 150) -> Optional[bytes]:
        if pdfium is None or Image is None:
            return None
        try:
            pdf = pdfium.PdfDocument(pdf_bytes)
            if len(pdf) == 0:
                return None
            page = pdf[0]
            scale = dpi / 72.0
            bitmap = page.render(scale=scale)
            img = bitmap.to_pil()
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue()
        except Exception as e:
            logger.exception("VK PDF->PNG conversion failed: %s", e)
            return None

    def _upload_doc_sync(self, file_data: bytes, user_id: int,
                         filename: str) -> Optional[str]:
        # (без изменений от оригинала — длинный блок кода)
        try:
            server = self._vk.docs.getMessagesUploadServer(
                type='doc', peer_id=user_id)
        except Exception as e:
            logger.exception(
                "VK docs.getMessagesUploadServer(peer_id=%s) failed: %s",
                user_id, e)
            return None

        if not isinstance(server, dict):
            return None
        upload_url = server.get('upload_url')
        if not upload_url:
            return None

        base = filename or "document.pdf"
        if file_data.startswith(b'%PDF'):
            base = "document.pdf"
        elif file_data.startswith(b'\xff\xd8\xff'):
            base = "photo.jpg"
        elif file_data.startswith(b'\x89PNG\r\n\x1a\n'):
            base = "photo.png"
        safe_filename = re.sub(r'[^A-Za-z0-9._-]', '_', base) or "document.pdf"

        try:
            resp = self._http_session.post(
                upload_url,
                files={'file': (safe_filename, file_data,
                                'application/octet-stream')},
                timeout=60,
            )
        except Exception as e:
            logger.exception("VK doc upload POST failed: %s", e)
            return None

        if resp.status_code != 200 or not resp.content:
            return None

        try:
            upload_result = resp.json()
        except ValueError:
            return None

        file_token = upload_result.get('file')
        if not file_token:
            return None

        try:
            saved = self._vk.docs.save(
                file=file_token,
                title=(safe_filename[:-4]
                       if safe_filename.lower().endswith(".pdf")
                       else safe_filename),
            )
        except Exception as e:
            logger.exception("VK docs.save failed: %s", e)
            return None

        doc_obj = None
        if isinstance(saved, list) and saved:
            first = saved[0]
            if isinstance(first, dict):
                doc_obj = first.get('doc') or first
        elif isinstance(saved, dict):
            doc_obj = saved.get('doc') or saved

        if not doc_obj:
            return None
        owner_id = doc_obj.get('owner_id')
        doc_id = doc_obj.get('id')
        if owner_id is None or doc_id is None:
            return None
        return f"doc{owner_id}_{doc_id}"

    async def send_attachment(self, user_id: int, attachment,
                              caption: Optional[str] = None,
                              max_attempts: int = 2,
                              kind: str = "photo",
                              keep: bool = False) -> bool:
        return await asyncio.to_thread(
            self._send_attachment_sync, user_id, attachment, caption,
            max_attempts
        )

    def _upload_photo_sync(self, file_bytes: bytes) -> Optional[str]:
        try:
            photo = self._upload.photo_messages([io.BytesIO(file_bytes)])[0]
            return f"photo{photo['owner_id']}_{photo['id']}"
        except Exception as e:
            logger.error("VK upload_photo failed: %s", e)
            return None

    async def upload_photo(self, file_bytes: bytes) -> Optional[str]:
        return await asyncio.to_thread(self._upload_photo_sync, file_bytes)

    def _get_user_name_sync(self, user_id: int) -> Tuple[str, str]:
        now = time.time()
        with self._name_cache_lock:
            if now - self._name_cache_cleanup_last > 300:
                cutoff = now - 300
                stale = [k for k, v in self._name_cache.items()
                         if v[1] <= cutoff]
                for k in stale:
                    del self._name_cache[k]
                self._name_cache_cleanup_last = now
            cached = self._name_cache.get(user_id)
            if cached is not None and now - cached[1] < 300:
                return cached[0]
        try:
            user = self._vk.users.get(user_ids=str(user_id))[0]
            name = (_safe_str(user.get("first_name", "друг")),
                    _safe_str(user.get("last_name", "")))
            with self._name_cache_lock:
                self._name_cache[user_id] = (name, time.time())
            return name
        except Exception:
            return "друг", ""

    async def get_user_name(self, user_id: int) -> Tuple[str, str]:
        return await asyncio.to_thread(self._get_user_name_sync, user_id)

    async def acquire_media_send(self, admin_id: int, app_id: int) -> bool:
        key = (admin_id, app_id)
        with self._active_media_sends_lock:
            if key in self._active_media_sends:
                return False
            self._active_media_sends.add(key)
            return True

    async def release_media_send(self, admin_id: int, app_id: int) -> None:
        key = (admin_id, app_id)
        with self._active_media_sends_lock:
            self._active_media_sends.discard(key)

    def is_media_send_active(self, admin_id: int, app_id: int) -> bool:
        with self._active_media_sends_lock:
            return (admin_id, app_id) in self._active_media_sends

    def normalize_attachments(self, raw) -> List[dict]:
        if not raw:
            return []
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                return []
        if not isinstance(raw, list):
            return []
        result = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            if "type" not in item:
                continue
            result.append(item)
        return result

    def _extract_and_send_vk_photos_sync(self, user_id, text):
        # (без изменений от оригинала)
        if not text:
            return 0
        try:
            from core.users import is_admin_user
            if is_admin_user(user_id):
                return 0
        except Exception:
            pass
        urls = _VK_PHOTO_URL_PATTERN.findall(text)
        if not urls:
            return 0
        seen = set()
        unique = []
        for u in urls:
            if u in seen:
                continue
            seen.add(u)
            unique.append(u)
        unique = unique[:MAX_RELAY_URLS_PER_MSG]
        sent = 0
        for url in unique:
            if not _is_allowed_media_url(url):
                continue
            try:
                resp = self._http_session.get(
                    url, timeout=30, stream=True, allow_redirects=False)
                if resp.status_code != 200:
                    resp.close()
                    continue
                chunks = []
                total = 0
                for chunk in resp.iter_content(chunk_size=8192):
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_RELAY_SIZE:
                        break
                    chunks.append(chunk)
                resp.close()
                file_data = b"".join(chunks)
                if not file_data:
                    continue
                photo = self._upload.photo_messages(
                    [io.BytesIO(file_data)])[0]
                attachment = f"photo{photo['owner_id']}_{photo['id']}"
                self._vk.messages.send(
                    user_id=user_id, attachment=attachment,
                    random_id=_next_random_id())
                sent += 1
            except Exception as e:
                logger.debug("VK relay-photo failed (%s): %s", url[:60], e)
        if sent:
            logger.info("VkAdapter: relayed %d VK photo(s)", sent)
        return sent