"""Точка входа VK-бота.

Поднимает VkAdapter, event loop, воркеры, и запускает общий обработчик
событий из core.state_machine.
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
load_dotenv(encoding='utf-8')

from core.runtime import set_adapter, set_event_loop, get_adapter
from core.db import (
    init_db_pool, run_migrations, shutdown_db, DB_CONFIG,
)
from core.media import warm_up_vk
from core.state_machine import handle_event_sync
from core.texts import ADMIN_IDS
from core.users import (
    init_bootstrap_admins, sync_bootstrap_admins_to_db,
    BOOTSTRAP_ADMIN_IDS_BY_PLATFORM,
)
from core.workers import (
    media_retry_worker, outbox_worker, full_photo_relay_worker,
)
from core.cache_listener import start_cache_listener_thread
from adapters.vk_adapter import VkAdapter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("bright_city_bot_vk")


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


init_bootstrap_admins(ADMIN_IDS, platform_name="vk")


def _disable_system_proxies():
    """Отключает системный прокси для текущего процесса (VK)."""
    import urllib.request as _urllib_request

    def _no_proxies():
        return {}

    if getattr(_urllib_request.getproxies, "__name__", "") != "_no_proxies":
        _urllib_request.getproxies = _no_proxies

    for _pk in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
                "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy"):
        os.environ.pop(_pk, None)

    import requests as _requests
    if not getattr(_requests.Session, "_vk_no_proxy_patched", False):
        _orig_request = _requests.Session.request
        # ФИКС Баг-65: MAX-CDN добавлен — иначе качание i.oneme.ru из
        # VK-процесса (в _resolve_url_attachment_sync) пойдёт через
        # системный прокси, если он прописан в реестре.
        _VK_HOSTS = ("vk.com", "userapi.com", "vk.ru", "vk.me",
                     "i.oneme.ru")

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


async def main_async():
    """Асинхронный главный цикл VK-бота."""
    _disable_system_proxies()

    _group_id_str = os.getenv("VK_GROUP_ID")
    _group_id = int(_group_id_str) if _group_id_str else None
    token = os.getenv("VK_BOT_TOKEN")
    if not token:
        raise RuntimeError("Не задан VK_BOT_TOKEN в .env")

    set_adapter(VkAdapter(token=token, group_id=_group_id))
    set_event_loop(asyncio.get_running_loop())

    init_db_pool()
    try:
        run_migrations()
    except RuntimeError as e:
        logger.error("Не удалось применить миграции БД:\n%s", e)
        return

    start_cache_listener_thread()

    sync_bootstrap_admins_to_db()

    from core.users import restore_ban_counters_on_startup
    restore_ban_counters_on_startup()

    logger.info("Бот запущен...")
    logger.info("Python: %s", sys.version.split()[0])
    logger.info("DB: host=%s, dbname=%s, user=%s",
                DB_CONFIG["host"], DB_CONFIG["dbname"], DB_CONFIG["user"])
    logger.info("VK bootstrap admins: %s",
                sorted(BOOTSTRAP_ADMIN_IDS_BY_PLATFORM.get("vk", ())))

    adapter = get_adapter()

    # ФИКС Баг-44: держим ссылки на воркеры и активные таски.
    worker_tasks: set = set()

    def _spawn(coro, name):
        t = asyncio.create_task(coro, name=name)
        worker_tasks.add(t)
        t.add_done_callback(worker_tasks.discard)
        return t

    _spawn(asyncio.to_thread(warm_up_vk), "warm_up_vk")
    _spawn(outbox_worker(), "outbox_worker")

    _vk_media_worker_enabled = (
        os.getenv("VK_MEDIA_WORKER", "").strip().lower()
        in ("1", "true", "yes", "on")
    )
    if _vk_media_worker_enabled:
        # ФИКС Баг-41: media_retry_worker может работать параллельно в
        # VK- и TG-процессах — get_media_files_pending_delete берёт
        # строки через FOR UPDATE SKIP LOCKED, дубли не удаляются.
        # Но VK_USER_TOKEN должен быть один: если в обоих процессах
        # он разный, каждый удалит свою пачку. Убедись, что .env
        # согласован, или оставь VK_MEDIA_WORKER выключенным.
        logger.warning(
            "media_retry_worker запущен в VK-процессе (VK_MEDIA_WORKER=1). "
            "Убедись, что TG-процесс не работает с тем же VK_USER_TOKEN.")
        _spawn(media_retry_worker(), "media_retry_worker")

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
        asyncio.run(main_async())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Остановка VK-бота...")
    finally:
        shutdown_db()


if __name__ == "__main__":
    main()