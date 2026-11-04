# adapters/tg_adapter.py
"""Реализация MessengerAdapter для Telegram (python-telegram-bot 22.8+)."""
import asyncio
import json
import logging
import time
import re
import io
import requests
from core.media import relay_vk_upload, relay_vk_upload_doc

_VK_PHOTO_URL_PATTERN = re.compile(
    r"https://[a-z0-9\-]+\.(?:userapi\.com|vkuserphoto\.ru)/[^\s]+",
    re.IGNORECASE,
)
from typing import AsyncIterator, Optional, Tuple, List

from telegram import (
    Update, Bot,
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardRemove,
    InputMediaPhoto,
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application, ApplicationBuilder,
    MessageHandler, CallbackQueryHandler,
    ContextTypes, filters,
)
from telegram.error import TelegramError, BadRequest
from adapters.base import MessengerAdapter

logger = logging.getLogger("adapters.tg_adapter")

_PROGRESS_MIN_FILES = 3
_UPLOAD_DEBOUNCE_SEC = 2.5
_UNREACHABLE_TTL = 6 * 3600
_UNSUPPORTED_WARN_INTERVAL = 60
_UNSUPPORTED_RESET_TTL = 5 * 60

# Telegram: править можно только свои сообщения и только за 48 часов.
_TG_EDIT_MAX_AGE_SEC = 48 * 3600


def _vk_button_to_reply(b: dict):
    action = b.get("action") or {}
    btype = action.get("type")
    label = (action.get("label") or "")[:64]
    if btype == "text":
        return KeyboardButton(text=label)
    return None


def _vk_button_to_inline(b: dict):
    action = b.get("action") or {}
    btype = action.get("type")
    label = (action.get("label") or "")[:64]
    if btype == "open_link":
        url = action.get("link") or ""
        return InlineKeyboardButton(text=label, url=url)
    if btype == "text":
        payload = action.get("payload")
        if isinstance(payload, str):
            callback_data = payload
        elif payload is not None:
            callback_data = json.dumps(payload, ensure_ascii=False)
        else:
            callback_data = label
        raw = callback_data.encode("utf-8")
        if len(raw) > 64:
            logger.warning("callback_data too long, truncating: %s", callback_data[:40])
            callback_data = raw[:64].decode("utf-8", errors="ignore")
        return InlineKeyboardButton(text=label, callback_data=callback_data)
    return None


def _vk_json_to_reply_markup(vk_json: Optional[str]):
    if not vk_json:
        return None, {}
    try:
        data = json.loads(vk_json)
    except (json.JSONDecodeError, TypeError):
        return None, {}
    if data.get("inline"):
        return None, {}
    rows = data.get("buttons") or []
    one_time = bool(data.get("one_time", False))

    if not rows:
        return ReplyKeyboardRemove(), {}

    has_contact_button = False
    for row in rows:
        for b in row or []:
            if ((b.get("action") or {}).get("type") == "contact"):
                has_contact_button = True
                break
        if has_contact_button:
            break

    tg_rows = []
    label_map = {}
    for row in rows:
        tg_row = []
        for b in row or []:
            action = b.get("action") or {}
            btype = action.get("type")
            label = (action.get("label") or "")[:64]

            if btype == "contact":
                tg_row.append(KeyboardButton(text=label, request_contact=True))
                continue
            if btype != "text":
                continue

            tg_row.append(KeyboardButton(text=label))
            payload = action.get("payload")
            if payload is None:
                payload = {"action": "text_button", "label": label}
            elif isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except (json.JSONDecodeError, TypeError):
                    payload = {"action": "raw", "value": payload}
            label_map[label] = payload
        if tg_row:
            tg_rows.append(tg_row)
    if not tg_rows:
        return None, {}
    return ReplyKeyboardMarkup(
        keyboard=tg_rows,
        resize_keyboard=not has_contact_button,
        one_time_keyboard=one_time,
    ), label_map


def _vk_json_to_inline_markup(vk_json: Optional[str]):
    if not vk_json:
        return None
    try:
        data = json.loads(vk_json)
    except (json.JSONDecodeError, TypeError):
        return None
    rows = data.get("buttons") or []
    tg_rows = []
    for row in rows:
        tg_row = []
        for b in row or []:
            btn = _vk_button_to_inline(b)
            if btn is not None:
                tg_row.append(btn)
        if tg_row:
            tg_rows.append(tg_row)
    if not tg_rows:
        return None
    return InlineKeyboardMarkup(inline_keyboard=tg_rows)


class TgAdapter(MessengerAdapter):
    platform = "tg"

    def __init__(self, token: str, proxy_url: Optional[str] = None,
                 is_admin_provider=None):
        self._token = token
        self._is_admin_provider = is_admin_provider or (lambda uid: False)

        import httpx
        request_kwargs = {
            "connect_timeout": 30.0,
            "read_timeout": 60.0,
            "write_timeout": 30.0,
            "pool_timeout": 30.0,
            "httpx_kwargs": {
                "limits": httpx.Limits(
                    max_keepalive_connections=5,
                    max_connections=20,
                    keepalive_expiry=30.0,
                ),
            },
        }
        if proxy_url:
            request_kwargs["proxy"] = proxy_url

        from telegram.request import HTTPXRequest
        request = HTTPXRequest(**request_kwargs)
        get_updates_request = HTTPXRequest(**request_kwargs)

        self._application: Application = (
            ApplicationBuilder()
            .token(token)
            .request(request)
            .get_updates_request(get_updates_request)
            .build()
        )

        self._event_queue: asyncio.Queue = asyncio.Queue()
        self._user_name_cache = {}
        self._user_reply_map = {}
        self._user_sent = {}
        self._user_last_sent_ts = {}
        self._cache_lock = asyncio.Lock()
        self._polling_started = False

        self._pending_uploads = {}
        self._pending_uploads_lock = asyncio.Lock()

        self._unreachable = {}
        self._unreachable_lock = asyncio.Lock()

        self._unsupported_state = {}
        self._unsupported_lock = asyncio.Lock()

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
                "TG user %s недоступен (%s): помечен на %d ч",
                user_id, reason or "chat not found", _UNREACHABLE_TTL // 3600,
            )

    async def _clear_unreachable(self, user_id: int):
        async with self._unreachable_lock:
            if user_id in self._unreachable:
                del self._unreachable[user_id]
                logger.info("TG user %s снова доступен (написал боту)", user_id)

    async def _warn_unsupported(self, user_id: int, kind: str):
        now = time.time()
        should_warn = False
        count_snapshot = 0

        async with self._unsupported_lock:
            if len(self._unsupported_state) > 1000:
                now_cleanup = time.time()
                stale = [uid for uid, s in self._unsupported_state.items()
                         if now_cleanup - s.get("last_seen_ts", 0.0) > 3600]
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

            since_warn = now - st["last_warn_ts"]
            if st["last_warn_ts"] == 0.0 or since_warn >= _UNSUPPORTED_WARN_INTERVAL:
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
            await self._application.bot.send_message(
                chat_id=user_id, text=text,
            )
        except TelegramError as e:
            logger.debug("unsupported-warning send failed: %s", e)

    async def _reset_unsupported(self, user_id: int):
        async with self._unsupported_lock:
            if user_id in self._unsupported_state:
                self._unsupported_state.pop(user_id, None)

    async def _cleanup_previous_step(self, user_id: int, gap_sec: float = 3.0):
        try:
            if self._is_admin_provider(user_id):
                return
        except Exception as e:
            logger.warning("_is_admin_provider failed for user %s: %s",
                           user_id, e)

        now = time.time()
        last_ts = self._user_last_sent_ts.get(user_id, 0.0)
        if now - last_ts < gap_sec:
            return
        ids = self._user_sent.get(user_id) or []
        for mid in ids:
            try:
                await self._application.bot.delete_message(
                    chat_id=user_id, message_id=mid,
                )
            except TelegramError as e:
                logger.debug("cleanup prev msg %s failed: %s", mid, e)
        self._user_sent[user_id] = []

    def _track_sent_message(self, user_id: int, message_id: int):
        self._user_sent.setdefault(user_id, []).append(message_id)
        self._user_last_sent_ts[user_id] = time.time()

    async def _safe_chat_action(self, user_id: int, action: str):
        try:
            await self._application.bot.send_chat_action(
                chat_id=user_id, action=action,
            )
        except TelegramError as e:
            logger.debug("chat_action failed for %s: %s", user_id, e)

    async def _buffer_upload_and_maybe_flush(
        self, user_id, text, payload, attachments, raw_update,
        platform_message_id=None,
        reply_to_platform_message_id=None,
        delay: float = _UPLOAD_DEBOUNCE_SEC,
    ):
        async with self._pending_uploads_lock:
            buf = self._pending_uploads.get(user_id)
            if buf is None:
                buf = {
                    "text": "",
                    "payload": None,
                    "attachments": [],
                    "raw_updates": [],
                    "task": None,
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
            buf["raw_updates"].append(raw_update)

            if buf["task"] is not None and not buf["task"].done():
                buf["task"].cancel()
            effective_delay = 1.0 if len(buf["attachments"]) == 1 else delay
            buf["task"] = asyncio.create_task(
                self._flush_upload_buffer(user_id, effective_delay)
            )

    async def _flush_upload_buffer(self, user_id: int, delay: float):
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return

        async with self._pending_uploads_lock:
            buf = self._pending_uploads.pop(user_id, None)
        if buf is None:
            return

        text = buf["text"]
        payload = buf["payload"]
        attachments = buf["attachments"]
        platform_message_id = buf.get("platform_message_id")

        relayable = [a for a in attachments if a.get("type") in ("photo", "doc")]

        progress_msg_id: Optional[int] = None
        progress_delay_task: Optional[asyncio.Task] = None

        if relayable:
            await self._safe_chat_action(user_id, ChatAction.UPLOAD_PHOTO)

        if len(relayable) >= _PROGRESS_MIN_FILES:
            async def _show_progress_later():
                try:
                    await asyncio.sleep(1.5)
                    nonlocal progress_msg_id
                    progress = await self._application.bot.send_message(
                        chat_id=user_id,
                        text=f"📥 Обрабатываю файлы (0/{len(relayable)})…",
                    )
                    progress_msg_id = progress.message_id
                except (TelegramError, asyncio.CancelledError) as e:
                    logger.debug("progress message failed: %s", e)

            progress_delay_task = asyncio.create_task(_show_progress_later())

        async def on_progress(done: int, total: int):
            if progress_msg_id is None:
                return
            try:
                await self._application.bot.edit_message_text(
                    chat_id=user_id,
                    message_id=progress_msg_id,
                    text=f"📥 Обрабатываю файлы ({done}/{total})…",
                )
            except TelegramError as e:
                logger.debug("progress edit failed: %s", e)

        try:
            attachments = await self._relay_attachments_to_vk(
                attachments, user_id, on_progress=on_progress,
            )
        finally:
            if progress_delay_task is not None and not progress_delay_task.done():
                progress_delay_task.cancel()
                try:
                    await progress_delay_task
                except (asyncio.CancelledError, Exception):
                    pass
            if progress_msg_id is not None:
                try:
                    await self._application.bot.delete_message(
                        chat_id=user_id, message_id=progress_msg_id,
                    )
                except TelegramError as e:
                    logger.debug("progress cleanup failed: %s", e)

        await self._event_queue.put({
            "user_id": user_id,
            "text": text,
            "attachments": attachments,
            "payload": payload,
            "raw": buf["raw_updates"][-1] if buf["raw_updates"] else None,
            "platform_message_id": platform_message_id,
            "reply_to_platform_message_id": buf.get("reply_to_platform_message_id"),
        })

    async def _on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.message or not update.effective_user:
            return
        user = update.effective_user

        await self._clear_unreachable(user.id)

        # Сохраняем профиль TG-пользователя (имя + username) в platform_users
        try:
            from core.users import upsert_platform_user
            await asyncio.to_thread(
                upsert_platform_user, "tg", user.id,
                first_name=user.first_name,
                last_name=user.last_name,
                username=user.username,
            )
        except Exception as e:
            logger.debug("upsert_platform_user (tg) failed: %s", e)

        text = update.message.text or update.message.caption or ""
        message_id = update.message.message_id
        platform_message_id = str(message_id)

        # reply на сообщение админа? (юзер ответил на конкретное сообщение)
        reply_to_platform_message_id = None
        if update.message.reply_to_message:
            reply_to_platform_message_id = str(
                update.message.reply_to_message.message_id
            )

        is_reply_button = False
        payload = None
        if text:
            label_map = self._user_reply_map.get(user.id) or {}
            if text in label_map:
                payload = label_map[text]
                is_reply_button = True
                text = ""

        if is_reply_button:
            try:
                await self._application.bot.delete_message(
                    chat_id=user.id, message_id=message_id,
                )
            except TelegramError as e:
                logger.warning("Failed to delete user message %s: %s", message_id, e)

        if is_reply_button:
            try:
                is_admin_reply = False
                if payload and isinstance(payload, dict):
                    act = payload.get("action", "")
                    if act in (
                            "admin_back", "view_app", "set_status", "show_category",
                            "admin_page", "admin_start_reply", "admin_send_banner",
                            "admin_categories", "admin_settings", "toggle_setting",
                            "admin_stats", "admin_search_prompt", "admin_user_prompt",
                            "admin_changelog", "sudo_confirm", "sudo_cancel",
                            "view_incomplete", "admin_remind",
                            "admin_start_reply_incomplete",
                    ):
                        is_admin_reply = True
                if not is_admin_reply:
                    sticker_msg = await self._application.bot.send_sticker(
                        chat_id=user.id,
                        sticker="CAACAgIAAxkBAAOXarJimTukS1Fs0WNp1xFPgDVuGTsAAlGhAAI8NJhJW9ftVETzd_s9BA",
                    )
                    asyncio.create_task(self._delete_after(sticker_msg.message_id, user.id, 1))
            except TelegramError as e:
                logger.debug("Sticker send failed: %s", e)

        attachments = self.normalize_attachments(update)
        relayable = [a for a in attachments if a.get("type") in ("photo", "doc")]

        if relayable:
            await self._reset_unsupported(user.id)
            await self._buffer_upload_and_maybe_flush(
                user_id=user.id,
                text=text,
                payload=payload,
                attachments=attachments,
                raw_update=update,
                platform_message_id=platform_message_id,
                reply_to_platform_message_id=reply_to_platform_message_id,
            )
            return

        if not text and not attachments and payload is None:
            m = update.message
            unsupported = None
            if m.video:
                unsupported = "видео"
            elif m.video_note:
                unsupported = "видео-кружок"
            elif m.voice:
                unsupported = "голосовое"
            if unsupported:
                await self._warn_unsupported(user.id, unsupported)
            return

        await self._event_queue.put({
            "user_id": user.id,
            "text": text,
            "attachments": attachments,
            "payload": payload,
            "raw": update,
            "platform_message_id": platform_message_id,
            "reply_to_platform_message_id": reply_to_platform_message_id,
        })

    async def _delete_after(self, message_id: int, chat_id: int, delay: float):
        await asyncio.sleep(delay)
        try:
            await self._application.bot.delete_message(chat_id=chat_id, message_id=message_id)
        except TelegramError as e:
            logger.debug("Failed to delete temp message %s: %s", message_id, e)

    async def _on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        if not query or not update.effective_user:
            return
        try:
            await query.answer()
        except TelegramError:
            pass

        # Сохраняем профиль
        try:
            from core.users import upsert_platform_user
            u = update.effective_user
            await asyncio.to_thread(
                upsert_platform_user, "tg", u.id,
                first_name=u.first_name,
                last_name=u.last_name,
                username=u.username,
            )
        except Exception as e:
            logger.debug("upsert_platform_user (tg callback) failed: %s", e)

        payload = None
        if query.data:
            try:
                payload = json.loads(query.data)
            except (json.JSONDecodeError, TypeError):
                payload = {"action": "raw", "value": query.data}

        # platform_message_id из сообщения, к которому привязана callback-кнопка.
        pmi = None
        try:
            if query.message:
                pmi = str(query.message.message_id)
        except Exception:
            pass

        await self._event_queue.put({
            "user_id": update.effective_user.id,
            "text": "",
            "attachments": [],
            "payload": payload,
            "raw": update,
            "platform_message_id": pmi,
            "reply_to_platform_message_id": None,
        })

    async def listen(self) -> AsyncIterator[dict]:
        if not self._polling_started:
            self._application.add_handler(MessageHandler(filters.ALL, self._on_message))
            self._application.add_handler(CallbackQueryHandler(self._on_callback))

            backoff = 5
            max_backoff = 300
            attempt = 0
            while True:
                attempt += 1
                try:
                    await self._application.initialize()
                    await self._application.start()
                    if self._application.updater is None:
                        raise RuntimeError(
                            "Telegram updater is None — polling не запустится.")
                    await self._application.updater.start_polling(
                        allowed_updates=Update.ALL_TYPES,
                        timeout=5,
                        poll_interval=1.0,
                    )
                    break
                except (KeyboardInterrupt, SystemExit):
                    raise
                except Exception as e:
                    logger.warning(
                        "TG init attempt %d failed: %s — retry через %d сек",
                        attempt, e, backoff)
                    try:
                        await self._application.shutdown()
                    except Exception:
                        pass
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, max_backoff)

            self._polling_started = True
            logger.info("Telegram polling started")

        while True:
            event = await self._event_queue.get()
            yield event

    async def send_message(self, user_id: int, text: str,
                           keyboard=None, max_attempts: int = 2,
                           parse_mode: Optional[str] = None,
                           keep: bool = False,
                           reply_to_platform_message_id: Optional[str] = None) -> bool:
        max_attempts = max(max_attempts, 1)

        if await self._is_unreachable(user_id):
            logger.debug("TG send_message skipped (user %s unreachable)", user_id)
            return False

        await self._cleanup_previous_step(user_id)

        if text:
            sent_photos = await self._extract_and_send_vk_photos(user_id, text)
            if sent_photos:
                text_wo_urls = _VK_PHOTO_URL_PATTERN.sub("", text).strip()
                if not text_wo_urls:
                    return True

        inline_markup = None
        reply_markup = None
        if keyboard and isinstance(keyboard, str):
            if '"inline"' in keyboard:
                inline_markup = _vk_json_to_inline_markup(keyboard)
            else:
                reply_markup, label_map = _vk_json_to_reply_markup(keyboard)
                if label_map:
                    existing = self._user_reply_map.setdefault(user_id, {})
                    existing.update(label_map)
                    if len(existing) > 300:
                        keys = list(existing.keys())
                        for k in keys[:-300]:
                            existing.pop(k, None)
                else:
                    self._user_reply_map.pop(user_id, None)

        # reply_to_platform_message_id — нативный reply
        reply_to_message_id = None
        if reply_to_platform_message_id:
            try:
                reply_to_message_id = int(reply_to_platform_message_id)
            except (TypeError, ValueError):
                logger.warning(
                    "TG send_message: invalid reply_to_platform_message_id %r",
                    reply_to_platform_message_id,
                )

        for attempt in range(max_attempts):
            try:
                sent = await self._application.bot.send_message(
                    chat_id=user_id,
                    text=text or "…",
                    reply_markup=inline_markup or reply_markup,
                    parse_mode=parse_mode,
                    reply_to_message_id=reply_to_message_id,
                )
                self._track_sent_message(user_id, sent.message_id)
                return str(sent.message_id)
            except TelegramError as e:
                err_text = str(e).lower()
                if "chat not found" in err_text or "bot was blocked" in err_text:
                    await self._mark_unreachable(user_id, str(e))
                    return False
                # «message to be replied not found» — оригинал удалён.
                # Пробуем без reply.
                if reply_to_message_id is not None and (
                    "replied" in err_text and "not found" in err_text
                ):
                    logger.warning(
                        "TG reply target %s not found (user=%s) — "
                        "sending without reply",
                        reply_to_message_id, user_id,
                    )
                    reply_to_message_id = None
                    if attempt < max_attempts - 1:
                        continue
                logger.error(
                    "TG send_message failed (user %s, attempt %d/%d): %s",
                    user_id, attempt + 1, max_attempts, e,
                )
                if attempt < max_attempts - 1:
                    await asyncio.sleep(1)
                else:
                    return False
            except Exception as e:
                logger.error("TG send_message unexpected error (user %s): %s", user_id, e)
                return False
        return False

    async def edit_message(self, user_id: int,
                           platform_message_id: str,
                           new_text: str) -> bool:
        """Нативно редактирует отправленное сообщение.

        Ограничения Telegram: только свои сообщения и только в течение
        48 часов. При ошибке возвращаем False — вызывающий код может
        откатиться на «Уточнение».
        """
        if await self._is_unreachable(user_id):
            return False
        try:
            message_id = int(platform_message_id)
        except (TypeError, ValueError):
            logger.warning(
                "TG edit_message: invalid platform_message_id %r",
                platform_message_id,
            )
            return False

        try:
            await self._application.bot.edit_message_text(
                chat_id=user_id,
                message_id=message_id,
                text=new_text,
            )
            logger.info(
                "TG edit_message OK: user=%s message_id=%s",
                user_id, message_id,
            )
            return True
        except BadRequest as e:
            err_text = str(e).lower()
            if "message is not modified" in err_text:
                # Тот же текст — считаем успехом, не плодим «Уточнение».
                return True
            logger.warning(
                "TG edit_message failed (user=%s, message_id=%s): %s",
                user_id, message_id, e,
            )
            return False
        except TelegramError as e:
            logger.warning(
                "TG edit_message failed (user=%s, message_id=%s): %s",
                user_id, message_id, e,
            )
            return False

    async def send_attachment(self, user_id: int, attachment,
                              caption: Optional[str] = None,
                              max_attempts: int = 2,
                              kind: str = "photo",
                              keep: bool = False) -> bool:
        max_attempts = max(max_attempts, 1)

        if await self._is_unreachable(user_id):
            logger.debug("TG send_attachment skipped (user %s unreachable)", user_id)
            return False

        await self._cleanup_previous_step(user_id)

        if isinstance(attachment, (list, tuple)) and len(attachment) >= 2:
            if kind == "doc":
                all_ok = True
                for idx, att in enumerate(attachment):
                    cap = caption if idx == 0 else None
                    ok = await self.send_attachment(
                        user_id, att, caption=cap,
                        max_attempts=max_attempts, kind="doc",
                    )
                    if not ok:
                        all_ok = False
                return all_ok
            return await self._send_media_group(
                user_id, list(attachment), caption, max_attempts,
            )

        single = attachment[0] if isinstance(attachment, (list, tuple)) else attachment

        media_for_send = single
        if isinstance(single, str) and single.startswith(("http://", "https://")):
            try:
                resp = await asyncio.to_thread(
                    lambda: requests.get(single, timeout=30, stream=True)
                )
                resp.raise_for_status()
                buf = io.BytesIO()
                for chunk in resp.iter_content(chunk_size=65536):
                    if chunk:
                        buf.write(chunk)
                buf.seek(0)
                media_for_send = buf
            except Exception as e:
                logger.warning(
                    "TG send_attachment: URL download failed "
                    "(user=%s, url=%s…): %s — пробуем отдать URL как есть",
                    user_id, str(single)[:60], e)
                media_for_send = single

        for attempt in range(max_attempts):
            try:
                if isinstance(media_for_send, io.BytesIO):
                    media_for_send.seek(0)
                if kind == "doc":
                    sent = await self._application.bot.send_document(
                        chat_id=user_id,
                        document=media_for_send,
                        caption=caption or None,
                    )
                else:
                    sent = await self._application.bot.send_photo(
                        chat_id=user_id,
                        photo=media_for_send,
                        caption=caption or None,
                    )
                self._track_sent_message(user_id, sent.message_id)
                return True
            except TelegramError as e:
                err_text = str(e).lower()
                if "chat not found" in err_text or "bot was blocked" in err_text:
                    await self._mark_unreachable(user_id, str(e))
                    return False
                logger.error(
                    "TG send_attachment failed (user %s, kind=%s, "
                    "attempt %d/%d): %s",
                    user_id, kind, attempt + 1, max_attempts, e,
                )
                if attempt < max_attempts - 1:
                    await asyncio.sleep(1)
                else:
                    return False
        return False

    async def _send_media_group(self, user_id: int, attachments: list,
                                caption: Optional[str],
                                max_attempts: int) -> bool:
        MAX_PER_GROUP = 10
        chunks = [attachments[i:i + MAX_PER_GROUP]
                  for i in range(0, len(attachments), MAX_PER_GROUP)]

        all_ok = True
        for chunk_idx, chunk in enumerate(chunks):
            media = []
            for i, att in enumerate(chunk):
                item = InputMediaPhoto(media=att)
                if i == 0 and chunk_idx == 0 and caption:
                    item.caption = caption
                media.append(item)

            sent_ok = False
            for attempt in range(max_attempts):
                try:
                    messages = await self._application.bot.send_media_group(
                        chat_id=user_id, media=media,
                    )
                    for m in messages:
                        self._track_sent_message(user_id, m.message_id)
                    sent_ok = True
                    break
                except TelegramError as e:
                    err_text = str(e).lower()
                    if "chat not found" in err_text or "bot was blocked" in err_text:
                        await self._mark_unreachable(user_id, str(e))
                        return False
                    logger.error(
                        "TG send_media_group failed (user %s, chunk %d/%d, "
                        "attempt %d/%d): %s",
                        user_id, chunk_idx + 1, len(chunks),
                        attempt + 1, max_attempts, e,
                    )
                    if attempt < max_attempts - 1:
                        await asyncio.sleep(1)

            if not sent_ok:
                all_ok = False

        return all_ok

    async def upload_photo(self, file_bytes: bytes) -> Optional[str]:
        return None

    async def get_user_name(self, user_id: int) -> Tuple[str, str]:
        async with self._cache_lock:
            cached = self._user_name_cache.get(user_id)
            if cached and time.time() - cached[1] < 300:
                return cached[0]
        try:
            chat = await self._application.bot.get_chat(chat_id=user_id)
            first = chat.first_name or "друг"
            last = chat.last_name or ""
            name = (first, last)
            async with self._cache_lock:
                self._user_name_cache[user_id] = (name, time.time())
            return name
        except Exception as e:
            logger.error("TG get_user_name failed (user %s): %s", user_id, e)
            return "друг", ""

    def normalize_attachments(self, update: Update) -> List[dict]:
        if not update.message:
            return []
        result = []
        if update.message.photo:
            sizes = update.message.photo
            preview_idx = min(2, len(sizes) - 1)
            preview = sizes[preview_idx]
            original = sizes[-1]
            result.append({
                "type": "photo",
                "photo": {
                    "sizes": [{
                        "url": preview.file_id,
                        "width": preview.width,
                        "height": preview.height,
                    }],
                },
                "_tg_source_file_id": original.file_id,
                "_tg_source_width": original.width,
                "_tg_source_height": original.height,
            })
        if update.message.document:
            doc = update.message.document
            result.append({
                "type": "doc",
                "doc": {
                    "url": doc.file_id,
                    "ext": (doc.file_name or "").rsplit(".", 1)[-1].lower() if doc.file_name else "",
                    "size": doc.file_size or 0,
                },
                "_tg_source_file_id": doc.file_id,
                "_tg_source_width": None,
                "_tg_source_height": None,
            })
        if update.message.contact:
            c = update.message.contact
            result.append({
                "type": "contact",
                "contact": {"phone": c.phone_number or "", "user_id": c.user_id or 0},
            })
        return result

    async def _get_tg_file_bytes(self, file_id: str) -> Optional[bytes]:
        try:
            f = await self._application.bot.get_file(file_id=file_id)
            buf = io.BytesIO()
            await f.download_to_memory(buf)
            return buf.getvalue()
        except TelegramError as e:
            logger.error("get_file bytes failed (%s…): %s", file_id[:24], e)
            return None

    async def get_file_bytes(self, file_id: str) -> Optional[bytes]:
        return await self._get_tg_file_bytes(file_id)

    async def _relay_attachments_to_vk(self, attachments, user_id,
                                       on_progress=None):
        if not attachments:
            return attachments

        relayable_count = sum(
            1 for a in attachments if a.get("type") in ("photo", "doc")
        )
        done_count = 0
        counter_lock = asyncio.Lock()

        async def _tick_progress():
            nonlocal done_count
            async with counter_lock:
                done_count += 1
                current = done_count
            if on_progress is not None:
                try:
                    await on_progress(current, relayable_count)
                except Exception as e:
                    logger.debug("on_progress callback failed: %s", e)

        async def _relay_one(att):
            t = att.get("type")
            if t == "photo":
                sizes = att.get("photo", {}).get("sizes") or []
                if not sizes:
                    await _tick_progress()
                    return
                fid = sizes[-1].get("url")
                if not isinstance(fid, str) or fid.startswith(("http://", "https://")):
                    await _tick_progress()
                    return
                file_bytes = await self._get_tg_file_bytes(fid)
                if not file_bytes:
                    await _tick_progress()
                    return
                try:
                    res = await relay_vk_upload(file_bytes)
                except Exception as e:
                    logger.exception("relay_vk_upload (photo) failed: %s", e)
                    await _tick_progress()
                    return
                if res and res.get("url"):
                    sizes[-1]["url"] = res["url"]
                    sizes[-1]["_vk_owner_id"] = res.get("owner_id")
                    sizes[-1]["_vk_photo_id"] = res.get("photo_id")
                    sizes[-1]["_relayed_to_vk"] = True
                await _tick_progress()
            elif t == "doc":
                doc = att.get("doc") or {}
                fid = doc.get("url")
                if not isinstance(fid, str) or fid.startswith(("http://", "https://")):
                    await _tick_progress()
                    return
                file_bytes = await self._get_tg_file_bytes(fid)
                if not file_bytes:
                    await _tick_progress()
                    return
                ext = (doc.get("ext") or "pdf").lower()
                filename = f"document.{ext}"
                try:
                    res = await relay_vk_upload_doc(
                        file_bytes, filename=filename,
                    )
                except Exception as e:
                    logger.exception("relay_vk_upload_doc failed: %s", e)
                    await _tick_progress()
                    return
                if res and res.get("url"):
                    doc["url"] = res["url"]
                    doc["_vk_owner_id"] = res.get("owner_id")
                    doc["_vk_doc_id"] = res.get("doc_id")
                    doc["_relayed_to_vk"] = True
                await _tick_progress()

        sem = asyncio.Semaphore(4)

        async def _bounded(att):
            async with sem:
                await _relay_one(att)

        await asyncio.gather(
            *(_bounded(a) for a in attachments),
            return_exceptions=True,
        )
        return attachments

    async def _extract_and_send_vk_photos(self, user_id, text):
        if not text:
            return 0
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
        unique = unique[:2]
        sent = 0
        for url in unique:
            try:
                await self._application.bot.send_photo(chat_id=user_id, photo=url)
                sent += 1
            except TelegramError as e:
                logger.debug("send_photo by URL failed (%s): %s", url[:60], e)
        if sent:
            logger.info("TgAdapter: relayed %d VK photo(s)", sent)
        return sent