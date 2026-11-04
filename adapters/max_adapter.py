# adapters/max_adapter.py
"""Реализация MessengerAdapter для мессенджера MAX (библиотека maxapi)."""
import asyncio
import io
import ipaddress
import json
import logging
import re
import socket
import time
from typing import AsyncIterator, List, Optional, Tuple, Union
from urllib.parse import urlparse as _urlparse

import httpx

from maxapi import Bot, Dispatcher
from maxapi.types import BotStarted, MessageCallback, MessageCreated
from maxapi.types.attachments import (
    AttachmentUpload,
    InputMediaBuffer,
)
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

from adapters.base import MessengerAdapter
from adapters.max_keyboard_helpers import (
    vk_json_to_max_keyboard as _vk_json_to_max_keyboard_new,
    update_reply_map_from_vk_json,
)
from core.media import (
    relay_vk_upload, relay_vk_upload_doc,
    _is_trusted_web_url,
)

logger = logging.getLogger("adapters.max_adapter")

_MAX_API_BASE = "https://platform-api2.max.ru"
_UNREACHABLE_TTL = 6 * 3600
_UPLOAD_DEBOUNCE_SEC = 2.5
_PROGRESS_MIN_FILES = 1
_PROGRESS_DELAY_SEC = 1.5

_SEND_MIN_INTERVAL = 0.55
_CLEANUP_GAP_SEC = 3.0
_MAX_TRACKED_MESSAGES = 200

_LONG_TEXT_KB_THRESHOLD = 400

_KEYBOARD_RESEND_GAP_SEC = 3.0
_KEYBOARD_RESEND_MAX_AGE_SEC = 180.0

_VK_PHOTO_URL_PATTERN = re.compile(
    r"https://[a-z0-9\-]{1,63}\.(?:userapi\.com|vkuserphoto\.ru)"
    r"/[A-Za-z0-9_\-./%]+",
    re.IGNORECASE,
)

_UNSUPPORTED_WARN_INTERVAL = 60
_UNSUPPORTED_RESET_TTL = 5 * 60

_vk_json_to_max_keyboard = _vk_json_to_max_keyboard_new


def _is_private_ip(ip) -> bool:
    return (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


def _peer_ip_from_response(resp) -> Optional[str]:
    stream = None
    try:
        stream = resp.extensions.get("network_stream")
    except Exception:
        pass
    if stream is None:
        return None
    try:
        peer = stream.get_extra_info("server_addr")
        if peer and isinstance(peer, (tuple, list)) and len(peer) >= 1:
            return str(peer[0])
    except Exception:
        pass
    return None


def _is_allowed_max_url(url: str) -> bool:
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
        if _is_private_ip(ip):
            return False

    return True


def _preflight_attachments(attachments):
    from core.limits import (ALLOWED_DOC_EXT, MAX_DOC_SIZE,
                             MAX_PHOTOS, MAX_ATTACHMENTS)
    kept, dropped = [], []
    photos = 0
    for att in attachments or []:
        if not isinstance(att, dict):
            continue
        t = att.get("type")
        if t == "photo":
            if photos >= MAX_PHOTOS:
                dropped.append(f"фото (превышен лимит {MAX_PHOTOS})")
                continue
            photos += 1
        elif t == "doc":
            doc = att.get("doc") or {}
            ext = (doc.get("ext") or "").lower().lstrip(".")
            size = doc.get("size") or doc.get("file_size") or 0
            if ext and ext not in ALLOWED_DOC_EXT:
                dropped.append(f"файл .{ext} — неподдерживаемый тип")
                continue
            if size and size > MAX_DOC_SIZE:
                mb = MAX_DOC_SIZE // (1024 * 1024)
                dropped.append(f"файл — размер больше {mb} МБ")
                continue
        kept.append(att)
    if len(kept) > MAX_ATTACHMENTS:
        extra = len(kept) - MAX_ATTACHMENTS
        dropped.append(f"{extra} вложений сверх лимита {MAX_ATTACHMENTS}")
        kept = kept[:MAX_ATTACHMENTS]
    return kept, dropped


def _extract_mid_from_message(msg) -> Optional[str]:
    """Достаёт mid (message id) из объекта MAX-сообщения.

    Структура maxapi: event.message.body.mid — основной путь.
    Fallback на .mid и .id на самом message.
    """
    if msg is None:
        return None
    try:
        body = getattr(msg, "body", None)
        if body is not None:
            mid = getattr(body, "mid", None)
            if mid is not None:
                return str(mid)
        for attr in ("mid", "id", "message_id"):
            v = getattr(msg, attr, None)
            if v is not None:
                return str(v)
    except Exception:
        pass
    return None


class MaxAdapter(MessengerAdapter):
    platform = "max"

    def __init__(self, token: str, is_admin_provider=None):
        self._token = token
        self._is_admin_provider = is_admin_provider or (lambda uid: False)

        self._bot = Bot(token=token)
        self._bot.api_url = "https://platform-api2.max.ru"
        self._dp = Dispatcher()

        self._event_queue: asyncio.Queue = asyncio.Queue()

        self._user_name_cache = {}
        self._user_reply_map = {}
        self._user_sent = {}
        self._user_keep = {}
        self._user_last_sent_ts = {}
        self._cache_lock = asyncio.Lock()

        self._unreachable = {}
        self._unreachable_lock = asyncio.Lock()

        self._active_media_sends = set()
        self._active_media_sends_lock = asyncio.Lock()

        self._last_admin_keyboard = {}
        self._keyboard_resend_tasks = {}
        self._keyboard_resend_deferred = set()

        self._pending_uploads = {}
        self._pending_uploads_lock = asyncio.Lock()

        self._max_token_cache = {}
        self._max_token_cache_lock = asyncio.Lock()
        self._max_token_cache_ttl = 3600.0
        self._max_token_cache_max = 500

        self._bytes_cache = {}
        self._bytes_cache_ttl = 3600.0

        self._send_throttle_lock = asyncio.Lock()
        self._send_last_ts = 0.0

        self._http: Optional[httpx.AsyncClient] = None
        self._polling_started = False

        self._unsupported_state = {}
        self._unsupported_lock = asyncio.Lock()

        self._extras_tokens: list[str] = []
        self._extras_tokens_updated_at: float = 0.0
        self._extras_tokens_lock = asyncio.Lock()
        self._extras_tokens_ttl = 12 * 3600

        self._register_handlers()

    def _register_handlers(self):
        @self._dp.bot_started()
        async def _on_bot_started(event: BotStarted):
            user = getattr(event, "user", None)
            user_id = getattr(user, "user_id", None) if user else None
            if user_id is None:
                logger.error("MAX bot_started: нет user_id")
                return
            try:
                first_name = (getattr(user, "first_name", None)
                              or getattr(user, "name", None))
                last_name = getattr(user, "last_name", None) or ""
                if first_name:
                    async with self._cache_lock:
                        self._user_name_cache[user_id] = (
                            (first_name, last_name), time.time(),
                        )
            except Exception as e:
                logger.debug("MAX bot_started: cache name failed: %s", e)

            # Сохраняем профиль в platform_users
            try:
                from core.users import upsert_platform_user
                await asyncio.to_thread(
                    upsert_platform_user, "max", user_id,
                    first_name=getattr(user, "first_name", None)
                                or getattr(user, "name", None),
                    last_name=getattr(user, "last_name", None),
                )
            except Exception as e:
                logger.debug("upsert_platform_user (max bot_started) failed: %s", e)

            logger.info("MAX bot_started: user=%s — эмулирую /start", user_id)
            await self._event_queue.put({
                "user_id": user_id,
                "text": "/start",
                "attachments": [],
                "payload": None,
                "raw": event,
                "platform_message_id": None,
            })

        @self._dp.message_created()
        async def _on_message(event: MessageCreated):
            await self._handle_message(event)

        @self._dp.message_callback()
        async def _on_callback(event: MessageCallback):
            await self._handle_callback(event)

    async def _get_http(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            import os as _os
            _verify = True
            _env_ca = _os.getenv("MAX_CA_BUNDLE")
            if _env_ca and _os.path.isfile(_env_ca):
                _verify = _env_ca
                logger.info(
                    "MAX httpx: SSL verify using MAX_CA_BUNDLE=%s", _env_ca)
            else:
                try:
                    import ssl
                    import truststore
                    _verify = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                    logger.info(
                        "MAX httpx: SSL verify using truststore "
                        "(system CA store)")
                except ImportError:
                    logger.info(
                        "MAX httpx: truststore не установлен — "
                        "использую системный SSLContext (verify=True).")
                    _verify = True

            self._http = httpx.AsyncClient(
                timeout=httpx.Timeout(60.0, connect=30.0, read=120.0),
                follow_redirects=True,
                verify=_verify,
                trust_env=False,
            )
        return self._http

    async def close(self):
        if self._http is not None and not self._http.is_closed:
            await self._http.aclose()

    async def _throttle_send(self):
        async with self._send_throttle_lock:
            now = time.monotonic()
            wait = _SEND_MIN_INTERVAL - (now - self._send_last_ts)
            if wait > 0:
                await asyncio.sleep(wait)
            self._send_last_ts = time.monotonic()

    def _track_sent_message(self, user_id: int, message_id):
        if message_id is None:
            return
        lst = self._user_sent.setdefault(user_id, [])
        lst.append(message_id)
        if len(lst) > _MAX_TRACKED_MESSAGES:
            del lst[:-_MAX_TRACKED_MESSAGES]
        self._user_last_sent_ts[user_id] = time.time()

    def _track_keep_message(self, user_id: int, message_id):
        if message_id is None:
            return
        lst = self._user_sent.setdefault(user_id, [])
        lst.append(message_id)
        if len(lst) > _MAX_TRACKED_MESSAGES:
            del lst[:-_MAX_TRACKED_MESSAGES]
        self._user_keep.setdefault(user_id, set()).add(message_id)
        self._user_last_sent_ts[user_id] = time.time()

    async def _delete_message(self, message_id) -> bool:
        if not message_id:
            return False
        try:
            http = await self._get_http()
            resp = await http.delete(
                f"{_MAX_API_BASE}/messages",
                params={"message_id": message_id},
                headers={"Authorization": self._token},
            )
            if resp.status_code in (200, 204):
                logger.info("MAX delete_message OK: mid=%s", message_id)
                return True
            logger.warning(
                "MAX delete_message FAILED: status=%s mid=%s body=%s",
                resp.status_code, message_id, resp.text[:200],
            )
            return False
        except Exception as e:
            logger.warning(
                "MAX delete_message exception (mid=%s): %s",
                message_id, e,
            )
            return False

    async def _cleanup_previous_step(self, user_id: int,
                                     gap_sec: float = _CLEANUP_GAP_SEC):
        now = time.time()
        last_ts = self._user_last_sent_ts.get(user_id, 0.0)
        if now - last_ts < gap_sec:
            logger.debug(
                "MAX cleanup skipped: gap=%.2fs < %.2fs (user=%s)",
                now - last_ts, gap_sec, user_id,
            )
            return

        ids = self._user_sent.get(user_id) or []
        keep_ids = set(self._user_keep.get(user_id) or set())
        if not ids:
            return

        still_alive = []
        for mid in ids:
            if mid in keep_ids:
                still_alive.append(mid)
                continue
            ok = await self._delete_message(mid)
            if not ok:
                still_alive.append(mid)
        self._user_sent[user_id] = still_alive
        logger.info(
            "MAX cleanup: user=%s removed=%d kept_for_retry=%d",
            user_id,
            len(ids) - len(still_alive),
            len(still_alive),
        )

    @staticmethod
    def _extract_message_id(result):
        if result is None:
            return None
        for attr in ("message_id", "mid", "id"):
            v = getattr(result, attr, None)
            if v:
                return v
        msg = getattr(result, "message", None)
        if msg is not None:
            for attr in ("message_id", "mid", "id"):
                v = getattr(msg, attr, None)
                if v:
                    return v
            body = getattr(msg, "body", None)
            if body is not None:
                for attr in ("mid", "message_id", "id"):
                    v = getattr(body, attr, None)
                    if v:
                        return v
        return None

    async def _handle_message(self, event: MessageCreated):
        msg = event.message
        if not msg or not msg.sender:
            return
        sender = msg.sender
        if getattr(sender, "is_bot", False):
            return

        user_id = sender.user_id

        try:
            first_name = (getattr(sender, "first_name", None)
                          or getattr(sender, "name", None))
            last_name = getattr(sender, "last_name", None) or ""
            if first_name:
                async with self._cache_lock:
                    self._user_name_cache[user_id] = (
                        (first_name, last_name), time.time(),
                    )
        except Exception as e:
            logger.debug("MAX _on_message: cache name failed: %s", e)

        # Сохраняем профиль в platform_users
        try:
            from core.users import upsert_platform_user
            await asyncio.to_thread(
                upsert_platform_user, "max", user_id,
                first_name=(getattr(sender, "first_name", None)
                            or getattr(sender, "name", None)),
                last_name=getattr(sender, "last_name", None),
            )
        except Exception as e:
            logger.debug("upsert_platform_user (max) failed: %s", e)

        text = ""
        if msg.body and getattr(msg.body, "text", None):
            text = msg.body.text

        platform_message_id = _extract_mid_from_message(msg)

        # reply на сообщение админа? В MAX реплай лежит в msg.link
        reply_to_platform_message_id = None
        try:
            link = getattr(msg, "link", None)
            if link:
                link_type = (
                    link.get("type") if isinstance(link, dict)
                    else getattr(link, "type", None)
                )
                if link_type == "reply":
                    inner = (
                        link.get("message") if isinstance(link, dict)
                        else getattr(link, "message", None)
                    )
                    if inner is not None:
                        if isinstance(inner, dict):
                            body = inner.get("body") or {}
                            mid = body.get("mid") or inner.get("mid")
                        else:
                            mid = _extract_mid_from_message(inner)
                        if mid:
                            reply_to_platform_message_id = str(mid)
        except Exception as e:
            logger.debug("MAX reply extraction failed: %s", e)

        logger.info(
            "MAX _on_message: user=%s text_len=%d mid=%s",
            user_id, len(text or ""), platform_message_id)

        payload = None
        if text:
            label_map = self._user_reply_map.get(user_id) or {}
            if text in label_map:
                payload = label_map[text]
                text = ""

        attachments = self.normalize_attachments(event)

        if attachments:
            await self._reset_unsupported_max(user_id)
            await self._buffer_upload_and_maybe_flush(
                user_id=user_id, text=text, payload=payload,
                attachments=attachments, raw_event=event,
                platform_message_id=platform_message_id,
                reply_to_platform_message_id=reply_to_platform_message_id,
            )
            return

        if not text and payload is None:
            await self._warn_unsupported_max(user_id, "этот тип вложения")
            return

        await self._event_queue.put({
            "user_id": user_id,
            "text": text,
            "attachments": [],
            "payload": payload,
            "raw": event,
            "platform_message_id": platform_message_id,
            "reply_to_platform_message_id": reply_to_platform_message_id,
        })

    async def _handle_callback(self, event: MessageCallback):
        callback = event.callback
        if not callback:
            return

        try:
            await self._bot.send_callback(
                callback.callback_id, notification="")
        except Exception as e:
            logger.debug("MAX send_callback failed: %s", e)

        user_obj = None
        user_id = getattr(callback, "user_id", None)

        if user_id is None:
            user_obj = getattr(callback, "user", None)
            if user_obj is not None:
                user_id = getattr(user_obj, "user_id", None)

        if user_id is None:
            sender = getattr(callback, "sender", None)
            if sender is not None:
                user_obj = sender
                user_id = getattr(sender, "user_id", None)

        if user_id is None:
            msg = getattr(event, "message", None)
            if msg is not None:
                sender = getattr(msg, "sender", None)
                if sender is not None:
                    user_obj = sender
                    user_id = getattr(sender, "user_id", None)

        if user_id is None:
            logger.error(
                "MAX _on_callback: не удалось извлечь user_id. "
                "callback_fields=%s, event_fields=%s",
                [a for a in dir(callback) if not a.startswith("_")],
                [a for a in dir(event) if not a.startswith("_")],
            )
            return

        if user_obj is not None:
            try:
                first_name = (getattr(user_obj, "first_name", None)
                              or getattr(user_obj, "name", None))
                last_name = getattr(user_obj, "last_name", None) or ""
                if first_name:
                    async with self._cache_lock:
                        self._user_name_cache[user_id] = (
                            (first_name, last_name), time.time(),
                        )
            except Exception as e:
                logger.debug("MAX _on_callback: cache name failed: %s", e)

        # Сохраняем профиль
        try:
            from core.users import upsert_platform_user
            await asyncio.to_thread(
                upsert_platform_user, "max", user_id,
                first_name=(getattr(user_obj, "first_name", None)
                            or getattr(user_obj, "name", None)),
                last_name=getattr(user_obj, "last_name", None),
            )
        except Exception as e:
            logger.debug("upsert_platform_user (max callback) failed: %s", e)

        payload = None
        if callback.payload:
            try:
                payload = json.loads(callback.payload) \
                    if isinstance(callback.payload, str) else callback.payload
            except (json.JSONDecodeError, TypeError):
                payload = {"action": "raw", "value": callback.payload}

        logger.info(
            "MAX _on_callback: user=%s action=%r",
            user_id,
            (payload or {}).get("action")
            if isinstance(payload, dict) else payload)

        await self._event_queue.put({
            "user_id": user_id,
            "text": "",
            "attachments": [],
            "payload": payload,
            "raw": event,
            "platform_message_id": None,
        })

    async def listen(self) -> AsyncIterator[dict]:
        if not self._polling_started:
            try:
                await self._bot.delete_webhook()
            except Exception as e:
                logger.debug("MAX delete_webhook (may be absent): %s", e)

            try:
                await self._get_http()
            except Exception as e:
                logger.debug("MAX httpx warmup failed: %s", e)

            self._polling_started = True
            logger.info("MAX polling started")

            async def _run_polling():
                try:
                    await self._dp.start_polling(self._bot)
                except Exception as e:
                    logger.exception("MAX polling died: %s", e)

            asyncio.create_task(_run_polling())

        while True:
            event = await self._event_queue.get()
            if event is None:
                return
            yield event

    async def _buffer_upload_and_maybe_flush(
            self, user_id, text, payload, attachments, raw_event,
            platform_message_id=None,
            reply_to_platform_message_id=None,
            delay: float = _UPLOAD_DEBOUNCE_SEC,
    ):
        async with self._pending_uploads_lock:
            buf = self._pending_uploads.get(user_id)
            if buf is None:
                buf = {
                    "text": "", "payload": None, "attachments": [],
                    "raw_events": [], "task": None,
                    "platform_message_id": None,
                    "reply_to_platform_message_id": None,
                }
                self._pending_uploads[user_id] = buf

            if text and not buf["text"]:
                buf["text"] = text
            if payload is not None:
                buf["payload"] = payload
            if platform_message_id and not buf["platform_message_id"]:
                buf["platform_message_id"] = platform_message_id
            if reply_to_platform_message_id and not buf["reply_to_platform_message_id"]:
                buf["reply_to_platform_message_id"] = reply_to_platform_message_id
            buf["attachments"].extend(attachments)
            buf["raw_events"].append(raw_event)

            if buf["task"] is not None and not buf["task"].done():
                buf["task"].cancel()
            effective_delay = 1.0 if len(buf["attachments"]) == 1 else delay
            buf["task"] = asyncio.create_task(
                self._flush_upload_buffer(user_id, effective_delay)
            )

    async def _flush_upload_buffer(self, user_id: int, delay: float):
        my_task = asyncio.current_task()

        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return

        async with self._pending_uploads_lock:
            buf = self._pending_uploads.get(user_id)
            if buf is None:
                return
            if buf.get("task") is not my_task:
                return
            self._pending_uploads.pop(user_id, None)

        text = buf["text"]
        payload = buf["payload"]
        attachments = buf["attachments"]
        platform_message_id = buf.get("platform_message_id")

        logger.info(
            "MAX upload flush: user=%s attachments=%d mid=%s",
            user_id, len(attachments), platform_message_id)

        attachments, dropped = _preflight_attachments(attachments)
        if dropped:
            try:
                await self._throttle_send()
                await self._bot.send_message(
                    user_id=user_id,
                    text=(
                            "⚠ Часть вложений не принята:\n"
                            + "\n".join(f"  • {d}" for d in dropped)
                            + "\n\nРазрешены: PDF, JPG, PNG, GIF, BMP, WEBP, "
                              "TIFF, DOC, DOCX (до 10 МБ)."
                    ),
                )
            except Exception as e:
                logger.debug("MAX preflight-drop notify failed: %s", e)

        relayable = [a for a in attachments
                     if a.get("type") in ("photo", "doc")]
        relayable_count = len(relayable)

        progress_msg_id = None
        progress_delay_task = None

        if relayable_count >= _PROGRESS_MIN_FILES:
            progress_wait = (0.3 if relayable_count == 1
                             else _PROGRESS_DELAY_SEC)

            async def _show_progress_later():
                try:
                    await asyncio.sleep(progress_wait)
                    nonlocal progress_msg_id
                    progress_msg_id = await self._send_progress_message(
                        user_id,
                        f"📥 Обрабатываю файлы (0/{relayable_count})…",
                    )
                except (asyncio.CancelledError, Exception) as e:
                    logger.debug("progress message failed: %s", e)

            progress_delay_task = asyncio.create_task(_show_progress_later())

        async def on_progress(done, total):
            if progress_msg_id is None:
                return
            await self._edit_progress_message(
                progress_msg_id,
                f"📥 Обрабатываю файлы ({done}/{total})…",
            )

        if attachments:
            try:
                await self._relay_attachments_to_vk(
                    attachments, user_id, on_progress=on_progress)
            except Exception as e:
                logger.exception("MAX relay to VK failed: %s", e)
            finally:
                if (progress_delay_task is not None
                        and not progress_delay_task.done()):
                    progress_delay_task.cancel()
                    try:
                        await progress_delay_task
                    except (asyncio.CancelledError, Exception):
                        pass
                if progress_msg_id is not None:
                    await self._delete_progress_message(progress_msg_id)

        await self._event_queue.put({
            "user_id": user_id,
            "text": text,
            "attachments": attachments,
            "payload": payload,
            "raw": buf["raw_events"][-1] if buf["raw_events"] else None,
            "platform_message_id": platform_message_id,
            "reply_to_platform_message_id": buf.get("reply_to_platform_message_id"),
        })

    async def _send_progress_message(self, user_id: int,
                                     text: str) -> Optional[str]:
        try:
            http = await self._get_http()
            resp = await http.post(
                f"{_MAX_API_BASE}/messages",
                params={"user_id": user_id},
                headers={"Authorization": self._token},
                json={"text": text},
            )
            if resp.status_code != 200:
                logger.debug(
                    "MAX progress send: status=%s", resp.status_code)
                return None
            data = resp.json()
            mid = (((data.get("message") or {}).get("body") or {})
                   .get("mid"))
            return mid
        except Exception as e:
            logger.debug("MAX progress send failed: %s", e)
            return None

    async def _edit_progress_message(self, message_id, text: str):
        if not message_id:
            return
        try:
            http = await self._get_http()
            resp = await http.put(
                f"{_MAX_API_BASE}/messages",
                params={"message_id": message_id},
                headers={"Authorization": self._token},
                json={"text": text},
            )
            if resp.status_code != 200:
                logger.debug(
                    "MAX progress edit: status=%s", resp.status_code)
        except Exception as e:
            logger.debug("MAX progress edit failed: %s", e)

    async def _delete_progress_message(self, message_id):
        await self._delete_message(message_id)

    async def _is_unreachable(self, user_id: int) -> bool:
        async with self._unreachable_lock:
            until = self._unreachable.get(user_id, 0.0)
            if until > time.time():
                return True
            if user_id in self._unreachable:
                del self._unreachable[user_id]
            return False

    async def _mark_unreachable(self, user_id: int, reason: str = ""):
        async with self._unreachable_lock:
            was_known = user_id in self._unreachable
            self._unreachable[user_id] = time.time() + _UNREACHABLE_TTL
        if not was_known:
            logger.warning(
                "MAX user %s недоступен (%s): помечен на %d ч",
                user_id, reason or "chat not found",
                _UNREACHABLE_TTL // 3600)

    async def _warn_unsupported_max(self, user_id: int, kind: str):
        now = time.time()
        should_warn = False
        count_snapshot = 0

        async with self._unsupported_lock:
            if len(self._unsupported_state) > 1000:
                stale = [
                    uid for uid, s in self._unsupported_state.items()
                    if now - s.get("last_seen_ts", 0.0) > 3600
                ]
                for uid in stale:
                    self._unsupported_state.pop(uid, None)

            st = self._unsupported_state.get(user_id)
            if st is None:
                st = {"count": 0, "last_warn_ts": 0.0, "last_seen_ts": 0.0}
                self._unsupported_state[user_id] = st

            if now - st["last_seen_ts"] > _UNSUPPORTED_RESET_TTL:
                st["count"] = 0
                st["last_warn_ts"] = 0.0

            st["count"] += 1
            st["last_seen_ts"] = now

            if (st["last_warn_ts"] == 0.0
                    or now - st["last_warn_ts"] >= _UNSUPPORTED_WARN_INTERVAL):
                st["last_warn_ts"] = now
                should_warn = True
                count_snapshot = st["count"]

        if not should_warn:
            return

        if count_snapshot <= 1:
            text = (f"Файл не принят: {kind} не подходит. "
                    "Пришлите, пожалуйста, фото (JPG/PNG) "
                    "или документ (PDF/DOCX).")
        else:
            text = (f"⚠ Уже {count_snapshot} неподдерживаемых файлов "
                    f"подряд ({kind} и т.п.).\n\n"
                    "Пришлите, пожалуйста, фото (JPG/PNG) "
                    "или документ (PDF/DOCX).")

        try:
            await self._throttle_send()
            await self._bot.send_message(user_id=user_id, text=text)
        except Exception as e:
            logger.debug("MAX unsupported-warn failed: %s", e)

    async def _reset_unsupported_max(self, user_id: int):
        async with self._unsupported_lock:
            self._unsupported_state.pop(user_id, None)

    async def acquire_media_send(self, admin_id: int, app_id: int) -> bool:
        key = (admin_id, app_id)
        async with self._active_media_sends_lock:
            if key in self._active_media_sends:
                return False
            self._active_media_sends.add(key)
            return True

    async def release_media_send(self, admin_id: int, app_id: int) -> None:
        key = (admin_id, app_id)
        async with self._active_media_sends_lock:
            self._active_media_sends.discard(key)

    def is_media_send_active(self, admin_id: int, app_id: int) -> bool:
        return (admin_id, app_id) in self._active_media_sends

    def _track_keyboard_for_resend(self, user_id: int, text: str,
                                   keyboard_attachment) -> None:
        self._last_admin_keyboard[user_id] = {
            "text": text or "",
            "kb": keyboard_attachment,
            "ts": time.time(),
            "att_count": 0,
        }
        old_task = self._keyboard_resend_tasks.pop(user_id, None)
        if old_task and not old_task.done():
            old_task.cancel()

    async def send_message(self, user_id: int, text: str,
                           keyboard=None, max_attempts: int = 2,
                           parse_mode: Optional[str] = None,
                           keep: bool = False,
                           reply_to_platform_message_id: Optional[str] = None,
                           ) -> Union[str, None]:
        logger.info(
            "MaxAdapter.send_message START: user_id=%s text_len=%d "
            "has_keyboard=%s keep=%s reply_to=%s",
            user_id, len(text or ""), bool(keyboard), keep,
            reply_to_platform_message_id)

        max_attempts = max(max_attempts, 1)

        if await self._is_unreachable(user_id):
            logger.debug(
                "MAX send_message skipped (user %s unreachable)", user_id)
            return None

        if keyboard and isinstance(keyboard, str):
            try:
                update_reply_map_from_vk_json(
                    keyboard,
                    self._user_reply_map.setdefault(user_id, {}))
            except Exception as e:
                logger.debug("MAX: reply_map update failed: %s", e)

        keyboard_attachment = None
        if keyboard and isinstance(keyboard, str):
            keyboard_attachment = _vk_json_to_max_keyboard(keyboard)
            if keyboard_attachment is None:
                try:
                    import json as _json
                    _has_buttons = bool(
                        (_json.loads(keyboard) or {}).get("buttons"))
                except Exception:
                    _has_buttons = True
                if _has_buttons:
                    logger.warning(
                        "MaxAdapter.send_message: keyboard передан, но "
                        "НЕ сконвертирован — сообщение уйдёт БЕЗ клавиатуры "
                        "(user=%s, keyboard_len=%d)",
                        user_id, len(keyboard))

        if (keyboard_attachment is not None and text
                and len(text) > _LONG_TEXT_KB_THRESHOLD):
            ok = await self._send_text_and_keyboard_separately(
                user_id, text, keyboard_attachment, keep)
            if ok:
                self._track_keyboard_for_resend(
                    user_id, text, keyboard_attachment)
                return "sent"

        attachments = []
        if keyboard_attachment is not None:
            attachments.append(keyboard_attachment)

        # Reply-вложение: {type: "reply", payload: {mid}}
        if reply_to_platform_message_id:
            try:
                attachments.append({
                    "type": "reply",
                    "payload": {"mid": str(reply_to_platform_message_id)},
                })
            except Exception as e:
                logger.warning(
                    "MAX send_message: bad reply_to_platform_message_id "
                    "%r: %s", reply_to_platform_message_id, e)

        for attempt in range(max_attempts):
            try:
                await self._throttle_send()
                result = await self._bot.send_message(
                    text=text or "…",
                    user_id=user_id,
                    attachments=attachments if attachments else None,
                )
                mid = self._extract_message_id(result)
                if mid is not None:
                    if keep:
                        self._track_keep_message(user_id, mid)
                    else:
                        self._track_sent_message(user_id, mid)
                if keyboard_attachment is not None:
                    self._track_keyboard_for_resend(
                        user_id, text, keyboard_attachment)
                logger.info(
                    "MaxAdapter.send_message OK: user_id=%s mid=%s",
                    user_id, mid)
                return str(mid) if mid is not None else "sent"
            except Exception as e:
                err_text = str(e).lower()
                if "not found" in err_text or "blocked" in err_text:
                    await self._mark_unreachable(user_id, str(e))
                    return None
                # Reply-target удалён — пробуем без reply.
                if (reply_to_platform_message_id
                        and "reply" in err_text
                        and ("not found" in err_text
                             or "deleted" in err_text)):
                    logger.warning(
                        "MAX reply target %s not found (user=%s) — "
                        "sending without reply",
                        reply_to_platform_message_id, user_id)
                    attachments = [a for a in attachments
                                   if not (isinstance(a, dict)
                                           and a.get("type") == "reply")]
                    if attempt < max_attempts - 1:
                        continue
                logger.error(
                    "MAX send_message failed (user %s, attempt %d/%d): %s",
                    user_id, attempt + 1, max_attempts, e,
                )
                if attempt < max_attempts - 1:
                    await asyncio.sleep(1)
                else:
                    return None
        return None

    async def edit_message(self, user_id: int,
                           platform_message_id: str,
                           new_text: str) -> bool:
        """Нативное редактирование сообщения через PUT /messages."""
        if not platform_message_id:
            return False
        if await self._is_unreachable(user_id):
            return False
        try:
            http = await self._get_http()
            resp = await http.put(
                f"{_MAX_API_BASE}/messages",
                params={"message_id": platform_message_id},
                headers={"Authorization": self._token},
                json={"text": new_text or "…"},
            )
            if resp.status_code in (200, 204):
                logger.info(
                    "MAX edit_message OK: user=%s mid=%s",
                    user_id, platform_message_id)
                return True
            logger.warning(
                "MAX edit_message failed: status=%s body=%s",
                resp.status_code, resp.text[:200])
            return False
        except Exception as e:
            logger.error(
                "MAX edit_message exception (user=%s, mid=%s): %s",
                user_id, platform_message_id, e)
            return False

    async def _send_text_and_keyboard_separately(
            self, user_id: int, text: str, keyboard_attachment, keep: bool,
    ) -> bool:
        try:
            await self._throttle_send()
            sent_text = await self._bot.send_message(
                text=text, user_id=user_id)
            mid1 = self._extract_message_id(sent_text)
            if mid1 is not None:
                if keep:
                    self._track_keep_message(user_id, mid1)
                else:
                    self._track_sent_message(user_id, mid1)
        except Exception as e:
            logger.warning(
                "MAX split: text send failed (user=%s): %s — "
                "пробую одним сообщением", user_id, e)
            return False

        try:
            await self._throttle_send()
            result = await self._bot.send_message(
                text="Выберите действие:",
                user_id=user_id,
                attachments=[keyboard_attachment],
            )
            mid2 = self._extract_message_id(result)
            if mid2 is not None:
                if keep:
                    self._track_keep_message(user_id, mid2)
                else:
                    self._track_sent_message(user_id, mid2)
        except Exception as e:
            logger.error(
                "MAX split: keyboard send failed (user=%s): %s",
                user_id, e)
            return False

        logger.info(
            "MaxAdapter.send_message OK (split): user_id=%s "
            "text_mid=%s kb_mid=%s text_len=%d",
            user_id, mid1, mid2, len(text))
        return True

    @staticmethod
    def _guess_file_meta(file_bytes: bytes, default_ext: str = ".bin") -> tuple[str, str]:
        if file_bytes.startswith(b'%PDF'):
            return "document.pdf", "application/pdf"
        if file_bytes.startswith(b'\xff\xd8\xff'):
            return "photo.jpg", "image/jpeg"
        if file_bytes.startswith(b'\x89PNG\r\n\x1a\n'):
            return "photo.png", "image/png"
        return f"file{default_ext}", "application/octet-stream"

    async def _prepare_one_attachment(self, att, kind: str = "photo"):
        if isinstance(att, str):
            if att.startswith(("http://", "https://")):
                file_bytes = await self._get_bytes_for_url(att)
                if not file_bytes:
                    logger.error(
                        "MAX prepare: не удалось скачать %s", att[:80])
                    return None

                filename, mime = self._guess_file_meta(
                    file_bytes, ".jpg" if kind == "photo" else ".bin"
                )

                if mime == "application/pdf":
                    logger.info(
                        "MAX prepare: PDF detected, sending as file (%d байт)",
                        len(file_bytes)
                    )
                    return InputMediaBuffer(buffer=file_bytes, filename=filename)

                token = await self._get_max_token_for_url(att, kind)
                if token:
                    obj = self._make_token_attachment(token, kind)
                    if obj is not None:
                        logger.info(
                            "MAX prepare: token OK for %s", att[:60])
                        return obj

                logger.info(
                    "MAX prepare: bytes fallback (%d байт, %s)",
                    len(file_bytes), filename)
                return InputMediaBuffer(buffer=file_bytes,
                                        filename=filename)
            else:
                return self._make_token_attachment(att, kind)

        elif isinstance(att, (bytes, io.BytesIO)):
            file_bytes = att.read() if isinstance(att, io.BytesIO) else att
            filename, _ = self._guess_file_meta(
                file_bytes, ".jpg" if kind == "photo" else ".bin"
            )
            return InputMediaBuffer(buffer=file_bytes, filename=filename)

        return None

    async def defer_keyboard_resend(self, user_id: int) -> None:
        self._keyboard_resend_deferred.add(user_id)
        old_task = self._keyboard_resend_tasks.pop(user_id, None)
        if old_task and not old_task.done():
            old_task.cancel()
        logger.debug(
            "MAX: keyboard resend deferred (user=%s)", user_id)

    async def flush_keyboard_resend(self, user_id: int) -> None:
        self._keyboard_resend_deferred.discard(user_id)
        st = self._last_admin_keyboard.get(user_id)
        if not st or not st.get("kb"):
            logger.debug(
                "MAX: flush keyboard — nothing to resend (user=%s)", user_id)
            return
        old_task = self._keyboard_resend_tasks.pop(user_id, None)
        if old_task and not old_task.done():
            old_task.cancel()
        await self._resend_keyboard_after_attachments(user_id, force=True)

    def _maybe_schedule_keyboard_resend(self, user_id: int) -> None:
        st = self._last_admin_keyboard.get(user_id)
        if not st:
            return
        if time.time() - st["ts"] > _KEYBOARD_RESEND_MAX_AGE_SEC:
            self._last_admin_keyboard.pop(user_id, None)
            return

        st["att_count"] += 1
        if user_id in self._keyboard_resend_deferred:
            logger.debug(
                "MAX: keyboard resend deferred (user=%s, att_count=%d)",
                user_id, st["att_count"])
            return
        old_task = self._keyboard_resend_tasks.pop(user_id, None)
        if old_task and not old_task.done():
            old_task.cancel()
        self._keyboard_resend_tasks[user_id] = asyncio.create_task(
            self._resend_keyboard_after_attachments(user_id)
        )

    async def _resend_keyboard_after_attachments(
            self, user_id: int, force: bool = False) -> None:
        if not force:
            try:
                await asyncio.sleep(_KEYBOARD_RESEND_GAP_SEC)
            except asyncio.CancelledError:
                return
            if user_id in self._keyboard_resend_deferred:
                return

        st = self._last_admin_keyboard.get(user_id)
        if not st or st["att_count"] == 0:
            return

        kb = st["kb"]
        if kb is None:
            return

        try:
            await self._throttle_send()
            result = await self._bot.send_message(
                text="Выберите действие:",
                user_id=user_id,
                attachments=[kb],
            )
            mid = self._extract_message_id(result)
            if mid is not None:
                self._track_sent_message(user_id, mid)
            logger.info(
                "MAX: resend keyboard after %d attachment(s) "
                "(user=%s, force=%s)",
                st["att_count"], user_id, force,
            )
        except Exception as e:
            logger.debug("MAX resend keyboard failed: %s", e)
        finally:
            self._keyboard_resend_tasks.pop(user_id, None)
            self._last_admin_keyboard.pop(user_id, None)

    async def send_attachment(self, user_id: int, attachment,
                              caption: Optional[str] = None,
                              max_attempts: int = 2,
                              kind: str = "photo",
                              keep: bool = False) -> bool:
        max_attempts = max(max_attempts, 1)

        if await self._is_unreachable(user_id):
            logger.debug(
                "MAX send_attachment skipped (user %s unreachable)", user_id)
            return False

        if isinstance(attachment, (list, tuple)) and len(attachment) >= 2:
            prepared = []
            for a in attachment:
                try:
                    obj = await self._prepare_one_attachment(a, kind)
                    if obj is not None:
                        prepared.append(obj)
                except Exception as e:
                    logger.error(
                        "MAX send_attachment(list): prepare failed: %s", e)

            if not prepared:
                logger.error(
                    "MAX send_attachment(list): ни одного вложения "
                    "(user=%s)", user_id)
                return False

            if len(prepared) >= 2:
                try:
                    await self._throttle_send()
                    result = await self._bot.send_message(
                        text=caption or "",
                        user_id=user_id,
                        attachments=prepared,
                    )
                    mid = self._extract_message_id(result)
                    if mid is not None:
                        if keep:
                            self._track_keep_message(user_id, mid)
                        else:
                            self._track_sent_message(user_id, mid)
                    logger.info(
                        "MaxAdapter.send_attachment(list) OK: "
                        "user_id=%s count=%d mid=%s",
                        user_id, len(prepared), mid,
                    )
                    self._maybe_schedule_keyboard_resend(user_id)
                    return True
                except Exception as e:
                    logger.warning(
                        "MAX send_attachment(list) single message failed: "
                        "%s — fallback to individual sends", e)

            sem = asyncio.Semaphore(3)

            async def _one(idx, obj):
                async with sem:
                    cap = caption if idx == 0 else None
                    for attempt in range(max_attempts):
                        try:
                            await self._throttle_send()
                            res = await self._bot.send_message(
                                text=cap or "",
                                user_id=user_id,
                                attachments=[obj],
                            )
                            mid = self._extract_message_id(res)
                            if mid is not None:
                                if keep:
                                    self._track_keep_message(user_id, mid)
                                else:
                                    self._track_sent_message(user_id, mid)
                            return True
                        except Exception as e:
                            logger.error(
                                "MAX send_attachment fallback idx=%d "
                                "attempt %d/%d: %s",
                                idx, attempt + 1, max_attempts, e)
                            if attempt < max_attempts - 1:
                                await asyncio.sleep(1)
                    return False

            results = await asyncio.gather(
                *(_one(i, o) for i, o in enumerate(prepared))
            )
            sent = sum(1 for r in results if r is True)
            total = len(prepared)
            logger.info(
                "MaxAdapter.send_attachment(list) fallback: %d/%d ok "
                "(user=%s)", sent, total, user_id,
            )
            if sent > 0:
                self._maybe_schedule_keyboard_resend(user_id)
            return sent > 0

        media_obj = await self._prepare_one_attachment(attachment, kind)
        if media_obj is None:
            logger.error(
                "MAX send_attachment: не удалось подготовить вложение "
                "типа %r", type(attachment).__name__)
            return False

        for attempt in range(max_attempts):
            try:
                await self._throttle_send()
                result = await self._bot.send_message(
                    text=caption or "",
                    user_id=user_id,
                    attachments=[media_obj],
                )
                mid = self._extract_message_id(result)
                if mid is not None:
                    if keep:
                        self._track_keep_message(user_id, mid)
                    else:
                        self._track_sent_message(user_id, mid)
                logger.info(
                    "MaxAdapter.send_attachment OK: user_id=%s kind=%s mid=%s",
                    user_id, kind, mid)
                self._maybe_schedule_keyboard_resend(user_id)
                return True
            except Exception as e:
                err_text = str(e).lower()
                if "not found" in err_text or "blocked" in err_text:
                    await self._mark_unreachable(user_id, str(e))
                    return False
                logger.error(
                    "MAX send_attachment failed (user %s, kind=%s, "
                    "attempt %d/%d): %s",
                    user_id, kind, attempt + 1, max_attempts, e,
                )
                if attempt < max_attempts - 1:
                    await asyncio.sleep(1)
                else:
                    return False
        return False

    async def _download_file(self, url: str) -> Optional[bytes]:
        if not _is_allowed_max_url(url):
            logger.warning(
                "MAX download: URL вне allow-list — пропускаю: %s",
                url[:80])
            return None

        from core.limits import MAX_DOC_SIZE, MAX_BANNER_SIZE
        max_size = max(MAX_DOC_SIZE, MAX_BANNER_SIZE)

        try:
            http = await self._get_http()

            async with http.stream(
                    "GET", url,
                    follow_redirects=False,
                    timeout=httpx.Timeout(60.0, connect=30.0, read=120.0),
            ) as resp:
                if not _is_trusted_web_url(url):
                    peer_ip = _peer_ip_from_response(resp)
                    if peer_ip:
                        try:
                            ip = ipaddress.ip_address(peer_ip)
                        except ValueError:
                            logger.warning(
                                "MAX download: не разобрал peer IP %r для %s",
                                peer_ip, url[:80])
                            return None
                        if _is_private_ip(ip):
                            logger.error(
                                "SSRF-ALERT: peer %s для URL %s — "
                                "соединение отклонено",
                                peer_ip, url[:80])
                            return None

                if resp.status_code != 200:
                    logger.warning(
                        "MAX download: status=%s for %s",
                        resp.status_code, url[:80])
                    return None

                cl_header = resp.headers.get("Content-Length")
                if cl_header:
                    try:
                        cl = int(cl_header)
                    except (TypeError, ValueError):
                        cl = 0
                    if cl > max_size:
                        logger.warning(
                            "MAX download: Content-Length %d > %d для %s",
                            cl, max_size, url[:80])
                        return None

                buf = bytearray()
                async for chunk in resp.aiter_bytes(chunk_size=65536):
                    if not chunk:
                        continue
                    buf.extend(chunk)
                    if len(buf) > max_size:
                        logger.warning(
                            "MAX download: тело превысило %d байт "
                            "(URL=%s) — прерываю", max_size, url[:80])
                        return None

                return bytes(buf) if buf else None
        except Exception as e:
            logger.error("MAX download failed (%s): %s", url[:80], e)
            return None

    async def _get_bytes_for_url(self, url: str) -> Optional[bytes]:
        now = time.time()
        async with self._max_token_cache_lock:
            entry = self._bytes_cache.get(url)
            if entry is not None:
                data, ts = entry
                if now - ts < self._bytes_cache_ttl:
                    return data
                self._bytes_cache.pop(url, None)

        data = await self._download_file(url)
        if data:
            async with self._max_token_cache_lock:
                self._bytes_cache[url] = (data, now)
                if len(self._bytes_cache) > self._max_token_cache_max:
                    items = sorted(self._bytes_cache.items(),
                                   key=lambda kv: kv[1][1])
                    drop = max(1, len(items) // 5)
                    for k, _ in items[:drop]:
                        self._bytes_cache.pop(k, None)
        return data

    async def _get_max_token_for_url(self, url: str,
                                     kind: str = "photo") -> Optional[str]:
        now = time.time()
        async with self._max_token_cache_lock:
            entry = self._max_token_cache.get(url)
            if entry is not None:
                token, ts = entry
                if now - ts < self._max_token_cache_ttl:
                    logger.info("MAX token cache HIT for %s", url[:60])
                    return token
                self._max_token_cache.pop(url, None)

        file_bytes = await self._get_bytes_for_url(url)
        if not file_bytes:
            return None
        token = await self._upload_file(file_bytes, kind)
        if not token:
            return None

        async with self._max_token_cache_lock:
            self._max_token_cache[url] = (token, now)
            if len(self._max_token_cache) > self._max_token_cache_max:
                items = sorted(self._max_token_cache.items(),
                               key=lambda kv: kv[1][1])
                drop = max(1, len(items) // 5)
                for k, _ in items[:drop]:
                    self._max_token_cache.pop(k, None)
        return token

    async def _ensure_extras_tokens(self) -> list[str]:
        from core.texts import EXTRAS_PHOTO_URLS

        if not EXTRAS_PHOTO_URLS:
            return []

        async with self._extras_tokens_lock:
            now = time.time()
            age = now - self._extras_tokens_updated_at
            tokens = self._extras_tokens

            if (tokens and age < self._extras_tokens_ttl
                    and len(tokens) == len(EXTRAS_PHOTO_URLS)):
                return list(tokens)

            logger.info(
                "MAX extras tokens: refreshing (age=%.1fs, count=%d/%d)",
                age, len(tokens), len(EXTRAS_PHOTO_URLS),
            )

            new_tokens: list[str] = []
            for i, url in enumerate(EXTRAS_PHOTO_URLS, 1):
                try:
                    tok = await self._get_max_token_for_url(url, "photo")
                    if tok:
                        new_tokens.append(tok)
                        logger.info(
                            "MAX extras token %d/%d OK (url=%s)",
                            i, len(EXTRAS_PHOTO_URLS), url[:50],
                        )
                    else:
                        logger.warning(
                            "MAX extras token %d/%d failed (url=%s)",
                            i, len(EXTRAS_PHOTO_URLS), url[:50],
                        )
                except Exception as e:
                    logger.warning(
                        "MAX extras token %d/%d exception: %s",
                        i, len(EXTRAS_PHOTO_URLS), e,
                    )

            if new_tokens:
                self._extras_tokens = new_tokens
                self._extras_tokens_updated_at = now
            return list(new_tokens)

    async def refresh_extras_tokens_now(self) -> int:
        self._extras_tokens_updated_at = 0.0
        return len(await self._ensure_extras_tokens())

    async def _upload_file(self, file_bytes: bytes,
                           kind: str = "photo") -> Optional[str]:
        if not file_bytes:
            return None

        filename, mime = self._guess_file_meta(
            file_bytes, ".jpg" if kind == "photo" else ".bin"
        )

        att_type = "image" if kind == "photo" else "file"
        if mime == "application/pdf" or filename.endswith(".pdf"):
            att_type = "file"

        http = await self._get_http()
        headers = {"Authorization": self._token}

        try:
            resp = await http.post(
                f"{_MAX_API_BASE}/uploads",
                params={"type": att_type},
                headers=headers,
            )
            if resp.status_code != 200:
                logger.error("MAX /uploads returned %s", resp.status_code)
                return None
            data = resp.json()

            upload_url = data.get("url")
            if not upload_url:
                logger.error(
                    "MAX /uploads: no 'url' in response. keys=%s",
                    list(data.keys())
                    if isinstance(data, dict) else type(data).__name__)
                return None

            files = {"data": (filename, file_bytes, mime)}
            resp2 = await http.post(upload_url, files=files)
            if resp2.status_code not in (200, 201):
                logger.error(
                    "MAX upload POST returned %s", resp2.status_code)
                return None
            upload_result = resp2.json()

            token = upload_result.get("token")
            if not token:
                token = self._extract_token_from_upload_result(upload_result)
            if not token:
                logger.error(
                    "MAX upload: no token in response. "
                    "keys=%s, photos_type=%s",
                    list(upload_result.keys())
                    if isinstance(upload_result, dict)
                    else type(upload_result).__name__,
                    type(upload_result.get("photos")).__name__,
                )
                return None

            logger.info(
                "MAX upload OK: token_len=%d, file=%s",
                len(str(token)), filename)
            return token

        except Exception as e:
            logger.exception("MAX _upload_file failed: %s", e)
            return None

    @staticmethod
    def _extract_token_from_upload_result(upload_result) -> Optional[str]:
        if not isinstance(upload_result, dict):
            return None

        photos = upload_result.get("photos")
        if photos is None:
            return None

        if isinstance(photos, list):
            for item in photos:
                if isinstance(item, dict):
                    t = item.get("token")
                    if t:
                        return t
                else:
                    t = getattr(item, "token", None)
                    if t:
                        return t
            return None

        if isinstance(photos, dict):
            if "token" in photos:
                t = photos.get("token")
                if t:
                    return t
            for v in photos.values():
                if isinstance(v, dict):
                    t = v.get("token")
                    if t:
                        return t
                elif hasattr(v, "token"):
                    t = getattr(v, "token", None)
                    if t:
                        return t
            return None

        t = getattr(photos, "token", None)
        if t:
            return t
        return None

    def _make_token_attachment(self, token: str, kind: str = "photo"):
        type_value = "image" if kind == "photo" else "file"
        try:
            return AttachmentUpload(
                type=type_value, payload={"token": token})
        except Exception as e:
            logger.error("AttachmentUpload init failed: %s", e)
            return None

    async def upload_photo(self, file_bytes: bytes) -> Optional[str]:
        return await self._upload_file(file_bytes, "photo")

    async def get_user_name(self, user_id: int) -> Tuple[str, str]:
        async with self._cache_lock:
            cached = self._user_name_cache.get(user_id)
            if cached and time.time() - cached[1] < 3600:
                return cached[0]

        try:
            get_user = getattr(self._bot, "get_user", None)
            if get_user is not None:
                user = await get_user(user_id=user_id)
                if user is not None:
                    first_name = (getattr(user, "first_name", None)
                                  or getattr(user, "name", None)
                                  or "друг")
                    last_name = getattr(user, "last_name", None) or ""
                    async with self._cache_lock:
                        self._user_name_cache[user_id] = (
                            (first_name, last_name), time.time(),
                        )
                    return first_name, last_name
        except Exception as e:
            logger.debug(
                "MAX get_user_name: fetch from API failed for %s: %s",
                user_id, e)

        logger.debug(
            "MAX get_user_name: cache miss for %s, API fetch not "
            "available — returning 'друг'", user_id)
        return "друг", ""

    def normalize_attachments(self, event) -> List[dict]:
        result = []
        msg = getattr(event, "message", None)
        if msg is None:
            return result

        body = getattr(msg, "body", None)
        attachments = getattr(body, "attachments", None) if body else None
        if not attachments:
            return result

        def _payload_to_dict(payload) -> dict:
            if payload is None:
                return {}
            if isinstance(payload, dict):
                return payload
            if hasattr(payload, "model_dump"):
                try:
                    return payload.model_dump()
                except Exception:
                    pass
            if hasattr(payload, "dict"):
                try:
                    return payload.dict()
                except Exception:
                    pass
            out = {}
            for field in ("url", "token", "photo_id", "file_id",
                          "filename", "file_name", "name", "title",
                          "size", "file_size",
                          "mime", "mime_type", "content_type",
                          "phone", "user_id",
                          "first_name", "last_name"):
                v = getattr(payload, field, None)
                if v is not None:
                    out[field] = v
            return out

        for att in attachments:
            att_type = getattr(att, "type", None)
            if isinstance(att_type, str):
                att_type = att_type.lower()
            elif att_type is not None:
                att_type = str(getattr(att_type, "value", att_type)).lower()

            payload = _payload_to_dict(getattr(att, "payload", None))

            if att_type in ("image", "photo"):
                _mime = (
                        payload.get("mime")
                        or payload.get("mime_type")
                        or payload.get("content_type")
                        or ""
                ).lower()
                _fname = (
                        payload.get("filename")
                        or payload.get("file_name")
                        or payload.get("name")
                        or payload.get("title")
                        or ""
                ).lower()
                if _mime == "application/pdf" or _fname.endswith(".pdf"):
                    logger.info(
                        "MAX normalize: PDF-as-image coerced to doc "
                        "(token=%s…)",
                        str(payload.get("token") or "")[:16])
                    att_type = "file"

            if att_type in ("image", "photo"):
                http_url = payload.get("url")
                token = payload.get("token")
                if (isinstance(http_url, str)
                        and http_url.startswith(("http://", "https://"))):
                    result.append({
                        "type": "photo",
                        "photo": {"sizes": [{"url": http_url}]},
                        "_max_source_token": token,
                    })
                elif token:
                    logger.warning(
                        "MAX normalize: photo без HTTP URL (только "
                        "token=%s…) — relay в VK не сработает",
                        str(token)[:16])
                    result.append({
                        "type": "photo",
                        "photo": {"sizes": [{"url": ""}]},
                        "_max_source_token": token,
                    })

            elif att_type == "file":
                http_url = payload.get("url")
                token = payload.get("token")

                if (isinstance(http_url, str)
                        and http_url.startswith(("http://", "https://"))):
                    doc_url = http_url
                elif (isinstance(token, str)
                      and token.startswith(("http://", "https://"))):
                    doc_url = token
                elif token:
                    logger.warning(
                        "MAX normalize: file без HTTP URL "
                        "(token=%s…) — relay в VK не сработает",
                        str(token)[:16])
                    doc_url = ""
                else:
                    doc_url = None

                if doc_url:
                    filename = (
                            payload.get("filename")
                            or payload.get("file_name")
                            or payload.get("name")
                            or payload.get("title")
                            or ""
                    )
                    ext = ""
                    if filename and "." in filename:
                        ext = filename.rsplit(".", 1)[-1].lower()

                    if not ext:
                        mime = (
                                payload.get("mime")
                                or payload.get("mime_type")
                                or payload.get("content_type")
                                or ""
                        ).lower()
                        _MIME_TO_EXT = {
                            "application/pdf": "pdf",
                            "application/msword": "doc",
                            "application/vnd.openxmlformats-officedocument."
                            "wordprocessingml.document": "docx",
                            "image/jpeg": "jpg", "image/jpg": "jpg",
                            "image/png": "png", "image/gif": "gif",
                            "image/webp": "webp", "image/bmp": "bmp",
                            "image/tiff": "tiff",
                        }
                        ext = _MIME_TO_EXT.get(mime, "")

                    result.append({
                        "type": "doc",
                        "doc": {
                            "url": doc_url,
                            "ext": ext,
                            "size": (payload.get("size")
                                     or payload.get("file_size") or 0),
                        },
                        "_max_source_token": token,
                    })

            elif att_type == "contact":
                result.append({
                    "type": "contact",
                    "contact": {
                        "phone": payload.get("phone") or "",
                        "user_id": payload.get("user_id") or 0,
                    },
                })
            else:
                logger.debug(
                    "MAX normalize_attachments: неизвестный тип вложения %r, "
                    "payload=%r", att_type, payload)

        return result

    async def _relay_attachments_to_vk(self, attachments, user_id,
                                       on_progress=None):
        to_relay = attachments
        try:
            from core.storage import user_state
            from core.texts import STATE_CHILD_PHOTO, STATE_DRAWINGS
            _state = user_state.get(user_id)
            if _state in (STATE_CHILD_PHOTO, STATE_DRAWINGS):
                to_relay = [a for a in attachments
                            if a.get("type") in ("photo", "contact")]
                if len(to_relay) < len(attachments):
                    logger.info(
                        "_relay_attachments_to_vk: state=%s — "
                        "пропускаю релей %d не-фото вложений",
                        _state, len(attachments) - len(to_relay))
        except Exception as e:
            logger.debug(
                "_relay_attachments_to_vk: state check failed: %s", e)

        relayable = [a for a in to_relay
                     if a.get("type") in ("photo", "doc")]
        total = len(relayable)
        done = 0
        counter_lock = asyncio.Lock()

        failed_labels = []
        failed_lock = asyncio.Lock()

        async def _mark_failed(label):
            async with failed_lock:
                failed_labels.append(label)

        async def _tick():
            nonlocal done
            async with counter_lock:
                done += 1
                current = done
            if on_progress is not None:
                try:
                    await on_progress(current, total)
                except Exception as e:
                    logger.debug("on_progress callback failed: %s", e)

        async def _save_vk_info(original_url, vk_url, owner_id,
                                photo_id, doc_id):
            try:
                from core.db import update_media_file_vk_info
                await asyncio.to_thread(
                    update_media_file_vk_info,
                    self.platform, user_id, original_url,
                    vk_url, owner_id, photo_id, doc_id,
                )
            except Exception as e:
                logger.debug(
                    "_relay_attachments_to_vk: save vk info failed: %s", e)

        async def _relay_one(att):
            t = att.get("type")

            if t == "photo":
                sizes = att.get("photo", {}).get("sizes") or []
                if not sizes:
                    await _mark_failed("фото")
                    await _tick()
                    return
                url = sizes[-1].get("url")
                if not isinstance(url, str) or not url.startswith(
                        ("http://", "https://")):
                    await _mark_failed("фото (нет HTTP URL)")
                    await _tick()
                    return
                file_bytes = await self._download_file(url)
                if not file_bytes:
                    await _mark_failed("фото (скачивание)")
                    await _tick()
                    return
                try:
                    res = await relay_vk_upload(file_bytes)
                except Exception as e:
                    logger.exception("relay_vk_upload (photo) failed: %s", e)
                    await _mark_failed("фото (загрузка в VK)")
                    await _tick()
                    return
                if res and res.get("url"):
                    owner_id = res.get("owner_id")
                    photo_id = res.get("photo_id")
                    if owner_id is not None and photo_id is not None:
                        sizes[-1]["url"] = f"photo{owner_id}_{photo_id}"
                    sizes[-1]["_vk_url"] = res["url"]
                    sizes[-1]["_vk_owner_id"] = owner_id
                    sizes[-1]["_vk_photo_id"] = photo_id
                    logger.info(
                        "MAX relay photo OK: vk_owner=%s vk_photo=%s",
                        res.get("owner_id"), res.get("photo_id"))
                    await _save_vk_info(
                        url, res["url"], res.get("owner_id"),
                        res.get("photo_id"), None,
                    )
                else:
                    await _mark_failed("фото (VK вернул пусто)")
                await _tick()

            elif t == "doc":
                doc = att.get("doc") or {}
                url = doc.get("url")
                if not isinstance(url, str) or not url.startswith(
                        ("http://", "https://")):
                    await _mark_failed("документ (нет HTTP URL)")
                    await _tick()
                    return
                file_bytes = await self._download_file(url)
                if not file_bytes:
                    await _mark_failed("документ (скачивание)")
                    await _tick()
                    return
                ext = (doc.get("ext") or "").lower().lstrip(".")
                filename = f"document.{ext}" if ext else "document.pdf"
                try:
                    res = await relay_vk_upload_doc(file_bytes,
                                                    filename=filename)
                except Exception as e:
                    logger.exception("relay_vk_upload_doc failed: %s", e)
                    await _mark_failed("документ (загрузка в VK)")
                    await _tick()
                    return
                if res and res.get("url"):
                    doc["_vk_url"] = res["url"]
                    doc["_vk_owner_id"] = res.get("owner_id")
                    doc["_vk_doc_id"] = res.get("doc_id")
                    logger.info(
                        "MAX relay doc OK: vk_owner=%s vk_doc=%s",
                        res.get("owner_id"), res.get("doc_id"))
                    await _save_vk_info(
                        url, res["url"], res.get("owner_id"),
                        None, res.get("doc_id"),
                    )
                else:
                    await _mark_failed("документ (VK вернул пусто)")
                await _tick()

        sem = asyncio.Semaphore(4)

        async def _bounded(att):
            async with sem:
                await _relay_one(att)

        if relayable:
            await asyncio.gather(
                *(_bounded(a) for a in relayable),
                return_exceptions=True,
            )

        try:
            from core.storage import user_state as _us
            from core.texts import STATE_PAYMENT as _SP
            _is_payment = (_us.get(user_id) == _SP)
        except Exception:
            _is_payment = False

        if failed_labels and not _is_payment:
            n = len(failed_labels)
            try:
                await self._throttle_send()
                await self._bot.send_message(
                    user_id=user_id,
                    text=(
                        f"⚠ Не удалось обработать {n} "
                        f"{'вложение' if n == 1 else 'вложений'}. "
                        f"Попробуйте, пожалуйста, отправить ещё раз."
                    ),
                )
            except Exception as e:
                logger.debug("MAX failed-relay notify failed: %s", e)
        elif failed_labels:
            logger.info(
                "relay: state=payment — не отправляю ложный toast "
                "'Не удалось обработать' (user=%s, failed=%d)",
                user_id, len(failed_labels))