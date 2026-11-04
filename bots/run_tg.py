"""Точка входа Telegram-бота.

Работает в параллельном процессе с VK-ботом, использует ту же БД.
Перед импортом main.py подставляет фиктивные VK-переменные,
чтобы VK-инициализация не запустилась в TG-процессе.
"""
import asyncio
import logging
import os
import re
import sys

from urllib.parse import urlparse as _urlparse

_here = os.path.dirname(os.path.abspath(__file__))
_root = os.path.dirname(_here)
if _root not in sys.path:
    sys.path.insert(0, _root)

from dotenv import load_dotenv
load_dotenv(override=True, encoding='utf-8')

# VK-токен нужен только чтобы core.media импортировался без VkAdapter.
os.environ["VK_BOT_TOKEN"] = "fake-token-for-tg-import"

# ВАЖНО: НЕ подменяем ADMIN_ID на ADMIN_TG_IDS.
os.environ.setdefault("ADMIN_ID", "0")

from core.runtime import set_adapter, set_event_loop, get_adapter
from core.db import (
    init_db_pool, run_migrations, shutdown_db,
)
from core.texts import ADMIN_IDS
from core.users import (
    init_bootstrap_admins, sync_bootstrap_admins_to_db, is_admin_user,
    BOOTSTRAP_ADMIN_IDS_BY_PLATFORM,
)
from core.state_machine import handle_event_sync
from core.workers import (
    media_retry_worker, full_photo_relay_worker, outbox_worker,
    stuck_deletion_alert_worker, gdpr_completion_notify_worker,
)
from core.cache_listener import start_cache_listener_thread
from adapters.tg_adapter import TgAdapter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("telegram.ext.Updater").setLevel(logging.CRITICAL)
logging.getLogger("telegram.ext._utils.networkloop").setLevel(logging.CRITICAL)

logger = logging.getLogger("bright_city_bot_tg")


# ============================================================
#  МАСКИРОВКА ТОКЕНОВ В ЛОГАХ
# ============================================================

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
        if "bot" in masked and ":" in masked:
            masked = self._TG_PATTERN.sub("bot<TOKEN>", masked)
        if "access_token" in masked.lower():
            masked = self._VK_QUERY_PATTERN.sub(r"\1=<TOKEN>", masked)
        if masked != msg:
            record.msg = masked
            record.args = ()
        return True


_mask_filter = _TokenMaskFilter()
_root = logging.getLogger()
if _root.handlers:
    for _h in _root.handlers:
        _h.addFilter(_mask_filter)
else:
    _fallback = logging.StreamHandler()
    _fallback.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    _fallback.addFilter(_mask_filter)
    _root.addHandler(_fallback)


# ============================================================
#  BOOTSTRAP-АДМИНЫ TG
# ============================================================
_TG_ADMIN_IDS_RAW = os.getenv("ADMIN_TG_IDS") or ""
_TG_ADMIN_IDS = [
    int(x.strip()) for x in _TG_ADMIN_IDS_RAW.split(",") if x.strip()
]
if not _TG_ADMIN_IDS:
    raise RuntimeError(
        "ADMIN_TG_IDS не задан в .env — TG-процесс без bootstrap-админов "
        "запускать нельзя: не будут работать /добавить_админа, GDPR-команды "
        "и админ-меню."
    )
init_bootstrap_admins(_TG_ADMIN_IDS, platform_name="tg")


# ============================================================
#  ОТКЛЮЧЕНИЕ СИСТЕМНОГО ПРОКСИ ДЛЯ VK-ХОСТОВ (только requests)
# ============================================================

def _disable_vk_proxies_for_requests():
    """Не даёт requests/urllib3 ходить через системный прокси на VK-хосты.

    TG-адаптер использует httpx и сам читает HTTPS_PROXY/TG_PROXY —
    его мы НЕ трогаем. Патчим только requests.Session.request:
    для VK-хостов принудительно ставим proxies={"http": None, "https": None}.

    Без этого warm_up_vk и relay MAX/TG→VK уходят в системный прокси
    и падают с ReadTimeoutError на pu.vk.ru (см. лог 30 сек × 3 ретрая).
    """
    import requests as _requests

    if getattr(_requests.Session, "_vk_no_proxy_patched", False):
        return

    _orig_request = _requests.Session.request
    _VK_HOSTS = ("vk.com", "userapi.com", "vk.ru", "vk.me",
                 "pu.vk.ru", "i.oneme.ru")

    def _patched_request(self, method, url, **kwargs):
        try:
            url_str = url if isinstance(url, str) else str(url)
            host = (_urlparse(url_str).hostname or "").lower()
        except Exception:
            host = ""
        if host and any(host == h or host.endswith("." + h)
                        for h in _VK_HOSTS):
            kwargs["proxies"] = {"http": None, "https": None}
        return _orig_request(self, method, url, **kwargs)

    _requests.Session.request = _patched_request
    _requests.Session._vk_no_proxy_patched = True
    logger.info("TG-процесс: VK-хосты отключены от системного прокси "
                "(requests.Session.request patched)")


async def run():
    tg_token = os.getenv("TG_BOT_TOKEN")
    if not tg_token:
        raise RuntimeError("Не задан TG_BOT_TOKEN в .env")

    adapter = TgAdapter(
        token=tg_token,
        proxy_url=os.getenv("HTTPS_PROXY") or os.getenv("TG_PROXY"),
        is_admin_provider=is_admin_user,
    )

    set_adapter(adapter)
    set_event_loop(asyncio.get_running_loop())

    logger.info("After set_adapter: adapter=%r platform=%s",
                get_adapter(), get_adapter().platform)

    _disable_vk_proxies_for_requests()

    init_db_pool()
    try:
        run_migrations()
    except RuntimeError as e:
        logger.error("Не удалось применить миграции БД:\n%s", e)
        return

    start_cache_listener_thread()

    from core.media import warm_up_vk
    asyncio.create_task(asyncio.to_thread(warm_up_vk))

    # ФИКС Баг-31: sync_bootstrap_admins_to_db инвалидирует кэш
    # админов в чужих процессах через pg_notify. Если у тебя функция
    # ещё без этой правки — она вызовет NameError (см. логи).
    sync_bootstrap_admins_to_db()

    from core.users import restore_ban_counters_on_startup
    restore_ban_counters_on_startup()

    logger.info("TG-бот запущен")
    logger.info("Platform: tg")
    logger.info("TG bootstrap admins: %s",
                sorted(BOOTSTRAP_ADMIN_IDS_BY_PLATFORM.get("tg", ())))

    # ФИКС Баг-44: держим ссылки на задачи. Раньше create_task без
    # сохранения ссылки позволял GC'у удалить их до завершения — на
    # shutdown незавершённые операции терялись (в частности,
    # воркеры не успевали отправить последнюю пачку из outbox).
    worker_tasks: set = set()

    def _spawn(coro, name):
        t = asyncio.create_task(coro, name=name)
        worker_tasks.add(t)
        t.add_done_callback(worker_tasks.discard)
        return t

    _spawn(media_retry_worker(), "media_retry_worker")
    _spawn(full_photo_relay_worker(), "full_photo_relay_worker")
    _spawn(outbox_worker(), "outbox_worker")
    _spawn(stuck_deletion_alert_worker(), "stuck_deletion_alert_worker")
    _spawn(gdpr_completion_notify_worker(), "gdpr_completion_notify_worker")

    # ФИКС Баг-44: держим и ссылки на активные handle_event_sync-таски.
    active_tasks = set()

    async for event in adapter.listen():
        task = asyncio.create_task(
            asyncio.to_thread(
                handle_event_sync,
                event["user_id"],
                event["text"],
                event["attachments"],
                event["payload"],
                event.get("platform_message_id"),
                event.get("reply_to_platform_message_id"),
            )
        )
        active_tasks.add(task)
        task.add_done_callback(active_tasks.discard)
        if len(active_tasks) >= 20:
            await asyncio.gather(*list(active_tasks), return_exceptions=True)
            active_tasks.clear()


def main():
    try:
        asyncio.run(run())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Остановка TG-бота...")
    finally:
        shutdown_db()


if __name__ == "__main__":
    main()