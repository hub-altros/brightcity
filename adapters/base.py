# adapters/base.py
"""Абстрактный интерфейс мессенджер-адаптера.

Каждый мессенджер (VK, Telegram, MAX) реализует этот интерфейс.
Бизнес-логика бота работает только через адаптер и не зависит от платформы.

Все сетевые методы — async. Синхронный только normalize_attachments()
(чистая функция над структурой вложения).
"""
import logging
from abc import ABC, abstractmethod
from typing import AsyncIterator, Optional, Tuple, List


logger = logging.getLogger("adapters.base")


class MessengerAdapter(ABC):
    platform: str = "unknown"

    # --- входящие события ---

    @abstractmethod
    async def listen(self) -> AsyncIterator[dict]:
        """Асинхронный генератор входящих событий. Каждое событие — dict:

        {
          "user_id":     int,
          "text":        str,
          "attachments": list[dict],
          "payload":     dict | None,
          "raw":         Any,
          "platform_message_id": str | None,   # id сообщения в платформе
        }
        """
        raise NotImplementedError

    # --- отправка ---

    @abstractmethod
    async def send_message(self, user_id: int, text: str,
                           keyboard=None, max_attempts: int = 2,
                           parse_mode: Optional[str] = None,
                           keep: bool = False,
                           reply_to_platform_message_id: Optional[str] = None) -> bool:
        """Отправить текстовое сообщение.

        reply_to_platform_message_id — id сообщения в платформе, на
        которое нужно ответить нативно (TG reply / VK reply_to / MAX
        attachment "reply"). None — обычная отправка.
        """
        raise NotImplementedError

    @abstractmethod
    async def send_attachment(self, user_id: int, attachment,
                              caption: Optional[str] = None,
                              max_attempts: int = 2,
                              kind: str = "photo",
                              keep: bool = False) -> bool:
        """Отправить вложение по платформенному идентификатору."""
        raise NotImplementedError

    @abstractmethod
    async def upload_photo(self, file_bytes: bytes) -> Optional[str]:
        """Загрузить фото на сервер платформы."""
        raise NotImplementedError

    # --- редактирование ---

    async def edit_message(self, user_id: int,
                           platform_message_id: str,
                           new_text: str) -> bool:
        """Отредактировать ранее отправленное сообщение.

        Базовая реализация возвращает False — адаптер не поддерживает
        редактирование. Платформы, которые умеют (TG/VK/MAX),
        переопределяют этот метод.
        """
        return False

    # --- утилиты ---

    @abstractmethod
    async def get_user_name(self, user_id: int) -> Tuple[str, str]:
        """Вернуть (first_name, last_name)."""
        raise NotImplementedError

    @abstractmethod
    def normalize_attachments(self, raw) -> List[dict]:
        raise NotImplementedError

    # --- медиа-дедупликация ---

    async def acquire_media_send(self, admin_id: int, app_id: int) -> bool:
        return True

    async def release_media_send(self, admin_id: int, app_id: int) -> None:
        return None

    def is_media_send_active(self, admin_id: int, app_id: int) -> bool:
        return False