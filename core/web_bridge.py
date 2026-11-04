"""WebBridgeAdapter — адаптер веб-процесса.

Все исходящие core.* в веб-процессе проходят здесь:
- сообщения текущему веб-админу (эхо от core.admin) складываются в его
  flash-очередь — роут покажет их как баннеры в UI;
- сообщения пользователям ставятся в outbox с правильной target_platform —
  бот-процесс доставит.
"""
import contextvars
import logging
from collections import defaultdict

from adapters.base import MessengerAdapter
from core.db import enqueue_outbox
from core.notify import detect_user_platform

logger = logging.getLogger("web.bridge")


_current_web_admin: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "current_web_admin", default=None
)

# Маркер конца карточки заявки в build_application_card. Если строка
# его содержит — это точно карточка, а не короткое эхо вроде
# «✅ Статус изменён».
_APPLICATION_CARD_MARKER = "👇 Доступные действия:"

# Служебные сообщения длиннее этого в flash не пускаем.
_FLASH_MAX_LEN = 800


class WebBridgeAdapter(MessengerAdapter):
    platform = "web"

    def __init__(self, admin_ids_provider=None):
        self._admin_ids_provider = admin_ids_provider or (lambda: set())
        self._flash: dict[int, list[str]] = defaultdict(list)

    # --- Контекст ---

    def set_current_admin(self, admin_id: int | None):
        _current_web_admin.set(admin_id)

    def _is_current_admin(self, user_id: int) -> bool:
        return user_id == _current_web_admin.get()

    def pop_flash(self, admin_id: int) -> list[str]:
        items = list(self._flash.get(admin_id, []))
        self._flash[admin_id] = []
        return items

    # --- Listen ---

    async def listen(self):
        return
        yield  # pragma: no cover

    # --- Отправка ---

    @staticmethod
    def _should_skip_flash(text: str) -> bool:
        if not text:
            return True
        if _APPLICATION_CARD_MARKER in text:
            return True
        if len(text) > _FLASH_MAX_LEN:
            return True
        return False

    async def send_message(self, user_id, text, keyboard=None,
                           max_attempts=2, parse_mode=None, keep=False,
                           reply_to_platform_message_id=None,
                           bot_message_id=None):
        # Эхо текущему веб-админу — во flash, не в outbox.
        if self._is_current_admin(user_id):
            if self._should_skip_flash(text or ""):
                logger.debug(
                    "web bridge: skip flash (len=%d) для admin=%s",
                    len(text or ""), user_id,
                )
                return True
            self._flash[user_id].append(text or "")
            return True

        # Всем остальным — через outbox.
        target_pl = detect_user_platform(user_id)
        if target_pl is None:
            logger.warning(
                "web bridge: cannot detect platform for user=%s; "
                "message NOT queued: %s…", user_id, (text or "")[:60])
            return False
        return enqueue_outbox(
            target_platform=target_pl,
            target_user_id=user_id,
            text=text,
            keyboard=keyboard,
            reply_to_platform_message_id=reply_to_platform_message_id,
            bot_message_id=bot_message_id,
        )

    async def send_attachment(self, user_id, attachment, caption=None,
                              max_attempts=2, kind="photo", keep=False):
        if self._is_current_admin(user_id):
            return True
        target_pl = detect_user_platform(user_id)
        if target_pl is None:
            logger.warning(
                "web bridge: cannot detect platform for user=%s; "
                "attachment NOT queued", user_id)
            return False
        return enqueue_outbox(
            target_platform=target_pl,
            target_user_id=user_id,
            attachment=str(attachment),
            caption=caption or "",
            message_type="banner" if keep else "attachment",
        )

    async def upload_photo(self, file_bytes):
        return None

    async def get_user_name(self, user_id):
        return "друг", ""

    def normalize_attachments(self, raw):
        return []