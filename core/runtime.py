# core/runtime.py
"""Глобальная ссылка на текущий MessengerAdapter.

В одном процессе активен ровно один адаптер — точка входа (bots/run_vk.py,
bots/run_tg.py, bots/run_max.py) вызывает set_adapter() до старта основного
цикла. Остальной код обращается к адаптеру через get_adapter().
"""
import logging
from typing import Optional

logger = logging.getLogger("bright_city_bot")

_adapter = None  # type: Optional[object]
_loop = None     # type: Optional[object]


def set_adapter(adapter) -> None:
    """Устанавливает активный адаптер. Вызывается один раз из точки входа."""
    global _adapter
    _adapter = adapter
    logger.info("MessengerAdapter установлен: platform=%s",
                getattr(adapter, "platform", "?"))


def get_adapter():
    """Возвращает активный адаптер. Бросает RuntimeError, если не установлен."""
    if _adapter is None:
        raise RuntimeError(
            "MessengerAdapter не установлен. Вызовите set_adapter() в точке входа."
        )
    return _adapter


def platform() -> str:
    """Короткое имя платформы: 'vk', 'tg', 'max'."""
    return get_adapter().platform


# --- EVENT LOOP ---
# Глобальная ссылка на event loop процесса. Нужна для sync-обёрток,
# которые вызываются из потоков (handle_event_sync) и хотят выполнить
# async-метод адаптера через asyncio.run_coroutine_threadsafe.

def set_event_loop(loop) -> None:
    global _loop
    _loop = loop


def get_event_loop_ref():
    return _loop


# --- Контекст события (thread-local) ---
# Используется, чтобы не пробрасывать platform_message_id и
# reply_to_platform_message_id через все сигнатуры в registration.py.
# Устанавливается в core.state_machine.handle_event_sync.

import threading as _threading

_event_context = _threading.local()


def set_event_context(platform_message_id=None,
                      reply_to_platform_message_id=None):
    """Сохраняет id текущего сообщения/ответа для этого потока.

    Все вложенные save_message_to_db / forward_to_admin читают отсюда,
    если параметры явно не переданы.
    """
    _event_context.platform_message_id = platform_message_id
    _event_context.reply_to_platform_message_id = reply_to_platform_message_id


def get_event_context():
    """Возвращает (platform_message_id, reply_to_platform_message_id)."""
    return (
        getattr(_event_context, "platform_message_id", None),
        getattr(_event_context, "reply_to_platform_message_id", None),
    )


def clear_event_context():
    _event_context.platform_message_id = None
    _event_context.reply_to_platform_message_id = None