# core/utils.py
"""Мелкие утилиты, используемые во всех модулях бота.

- safe_str: безопасное приведение к строке.
- _run_async: запуск корутины в текущем event loop (sync-обёртка).
- _fire_and_forget: запуск корутины без ожидания результата.
"""
import asyncio
import logging
import re
import threading

from core.runtime import get_event_loop_ref

logger = logging.getLogger("bright_city_bot")


def safe_str(s):
    """Приводит значение к строке безопасно: None → '',
    bytes → utf-8, остальное → str()."""
    if s is None:
        return ""
    if isinstance(s, bytes):
        return s.decode("utf-8", errors="replace")
    return str(s)


def as_positive_int(value):
    """Безопасно приводит значение из payload к положительному int.

    str.isdigit() недостаточен: '²'.isdigit() == True, но int('²')
    бросает ValueError, а '٣'.isdigit() == True и int('٣') == 3.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and re.fullmatch(r"\d{1,18}", value):
        parsed = int(value)
        return parsed if parsed > 0 else None
    return None


def _run_async(coro):
    """Запускает корутину в event loop процесса, ожидая результат.

    ФИКС: явная защита от дедлока. Если эта функция вызывается из
    потока, который сам владеет running loop (то есть из корутины),
    run_coroutine_threadsafe + future.result() заблокирует loop
    навсегда. В этом случае используем asyncio.run_coroutine_threadsafe
    в отдельном loop или падаем с понятной ошибкой.

    Реально _run_async вызывается из sync-потоков (asyncio.to_thread
    в точках входа) — там loop не running в этом потоке, и всё работает.
    """
    loop = get_event_loop_ref()
    if loop is None or loop.is_closed():
        return asyncio.run(coro)

    # ФИКС: если мы уже внутри running loop в этом потоке — прямой
    # вызов run_coroutine_threadsafe + result() даст дедлок. Определяем
    # это по наличию активного running loop в текущем потоке.
    try:
        running_here = asyncio.get_running_loop()
    except RuntimeError:
        running_here = None

    if running_here is loop:
        # Мы в самом loop-потоке. Нельзя блокироваться на result().
        # Возвращаем Task, чтобы caller мог его дождаться через await.
        # Но caller sync — значит это ошибка проектирования.
        logger.error(
            "_run_async: вызов из event loop потока — "
            "используйте await вместо _run_async")
        raise RuntimeError(
            "_run_async нельзя вызывать из event loop; используйте await")

    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=60)


def _fire_and_forget(coro, label: str = "task"):
    """Запускает корутину в event loop, НЕ ожидая результата.

    ФИКС Баг-22: если loop не установлен, НЕ запускаем корутину
    синхронно (это противоречило бы названию функции и блокировало
    вызывающего). Логируем ошибку и закрываем корутину, чтобы не
    было RuntimeWarning «coroutine was never awaited».
    """
    loop = get_event_loop_ref()
    if loop is None or loop.is_closed():
        logger.error(
            "_fire_and_forget(%s): event loop не установлен — "
            "задача пропущена", label)
        try:
            coro.close()
        except Exception:
            pass
        return

    future = asyncio.run_coroutine_threadsafe(coro, loop)

    def _on_done(f):
        try:
            f.result(timeout=120)
        except Exception as e:
            logger.error("_fire_and_forget(%s): %s", label, e)

    future.add_done_callback(_on_done)