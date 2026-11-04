"""Точка входа MAX-бота.

Работает в параллельном процессе с VK- и TG-ботами, использует ту же БД.
Перед импортом main.py подставляет фиктивные VK-переменные,
чтобы VK-инициализация не запустилась в MAX-процессе.
"""
import asyncio
import logging
import os
import re
import sys

_here = os.path.dirname(os.path.abspath(__file__))
_root = os.path.dirname(_here)
if _root not in sys.path:
    sys.path.insert(0, _root)

from dotenv import load_dotenv
load_dotenv(override=True, encoding='utf-8')

# --- SSL: подсовываем certifi всем HTTPS-клиентам ДО их импорта ---
import os as _os
try:
    import certifi as _certifi
    _os.environ.setdefault("SSL_CERT_FILE", _certifi.where())
    _os.environ.setdefault("REQUESTS_CA_BUNDLE", _certifi.where())
except ImportError:
    pass

# VK-токен нужен только чтобы core.media импортировался без VkAdapter.
os.environ["VK_BOT_TOKEN"] = "fake-token-for-max-import"

# ВАЖНО: НЕ подменяем ADMIN_ID на ADMIN_MAX_IDS.
os.environ.setdefault("ADMIN_ID", "0")

from core.runtime import set_adapter, set_event_loop, get_adapter
from core.db import (
    init_db_pool, run_migrations, shutdown_db,
)
from core.users import (
    init_bootstrap_admins, sync_bootstrap_admins_to_db, is_admin_user,
    BOOTSTRAP_ADMIN_IDS_BY_PLATFORM, restore_ban_counters_on_startup,
)
from core.state_machine import handle_event_sync
from core.workers import (
    media_retry_worker, full_photo_relay_worker, outbox_worker,
)
from core.cache_listener import start_cache_listener_thread
from adapters.max_adapter import MaxAdapter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger("bright_city_bot_max")


# ============================================================
#  МАСКИРОВКА ТОКЕНОВ В ЛОГАХ
# ============================================================

class _TokenMaskFilter(logging.Filter):
    """Маскирует TG- и VK-токены в логах."""
    _TG_PATTERN = re.compile(r"bot\d+:[A-Za-z0-9_\-]+")
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
_root_lgr = logging.getLogger()
if _root_lgr.handlers:
    for _h in _root_lgr.handlers:
        _h.addFilter(_mask_filter)
else:
    _fallback = logging.StreamHandler()
    _fallback.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    _fallback.addFilter(_mask_filter)
    _root_lgr.addHandler(_fallback)


# ============================================================
#  BOOTSTRAP-АДМИНЫ MAX
# ============================================================
_MAX_ADMIN_IDS_RAW = os.getenv("ADMIN_MAX_IDS") or ""
_MAX_ADMIN_IDS = [
    int(x.strip()) for x in _MAX_ADMIN_IDS_RAW.split(",") if x.strip()
]
if not _MAX_ADMIN_IDS:
    raise RuntimeError(
        "ADMIN_MAX_IDS не задан в .env — MAX-процесс без bootstrap-админов "
        "запускать нельзя: не будут работать /добавить_админа, GDPR-команды "
        "и админ-меню."
    )
init_bootstrap_admins(_MAX_ADMIN_IDS, platform_name="max")


async def run():
    max_token = os.getenv("MAX_BOT_TOKEN")
    if not max_token:
        raise RuntimeError("Не задан MAX_BOT_TOKEN в .env")

    adapter = MaxAdapter(
        token=max_token,
        is_admin_provider=is_admin_user,
    )

    set_adapter(adapter)
    set_event_loop(asyncio.get_running_loop())

    logger.info("After set_adapter: adapter=%r platform=%s",
                get_adapter(), get_adapter().platform)

    # ── Worker tasks helper (используется и для extras-токенов ниже,
    #    и для стандартных воркеров после polling) ─────────────────
    worker_tasks: set = set()

    def _spawn(coro, name):
        t = asyncio.create_task(coro, name=name)
        worker_tasks.add(t)
        t.add_done_callback(worker_tasks.discard)
        return t

    # ── COLD-START: extras-токены грузятся СРАЗУ, параллельно с init БД,
    #    миграциями, cache_listener, warm_up_vk. К первому сообщению
    #    пользователя (обычно 5-10 сек после старта) — токены готовы,
    #    первый EXTRAS не платит 5-8 сек за upload.
    #
    #    Первая попытка — с задержкой 0. Если run_web.py ещё не поднялся
    #    или /media/raw/... недоступен — быстрые ретраи (10, 30, 60, 120
    #    сек). После успеха — раз в 12 часов (TTL кэша).
    async def _extras_tokens_refresher():
        delays = [0, 10, 30, 60, 120]
        for i, delay in enumerate(delays):
            if delay:
                await asyncio.sleep(delay)
            try:
                n = await adapter.refresh_extras_tokens_now()
                if n > 0:
                    logger.info(
                        "MAX extras tokens ready: %d "
                        "(attempt %d, +%ds from start)",
                        n, i + 1, delay,
                    )
                    break
                logger.warning(
                    "MAX extras tokens not ready yet "
                    "(attempt %d, +%ds)", i + 1, delay,
                )
            except Exception as e:
                logger.warning(
                    "extras tokens attempt %d (+%ds) failed: %s",
                    i + 1, delay, e,
                )

        while True:
            await asyncio.sleep(12 * 3600)
            try:
                n = await adapter.refresh_extras_tokens_now()
                logger.info("MAX extras tokens refreshed: %d", n)
            except Exception as e:
                logger.warning("extras tokens refresh failed: %s", e)

    _spawn(_extras_tokens_refresher(), "extras_tokens_refresher")

    # ── Дальше — стандартная инициализация, параллельно с токенами ──
    init_db_pool()
    try:
        run_migrations()
    except RuntimeError as e:
        logger.error("Не удалось применить миграции БД:\n%s", e)
        return

    start_cache_listener_thread()
    restore_ban_counters_on_startup()

    from core.media import warm_up_vk
    _spawn(asyncio.to_thread(warm_up_vk), "warm_up_vk")

    sync_bootstrap_admins_to_db()

    logger.info("MAX-бот запущен")
    logger.info("Platform: max")
    logger.info("MAX bootstrap admins: %s",
                sorted(BOOTSTRAP_ADMIN_IDS_BY_PLATFORM.get("max", ())))

    _spawn(media_retry_worker(), "media_retry_worker")
    _spawn(full_photo_relay_worker(), "full_photo_relay_worker")
    _spawn(outbox_worker(), "outbox_worker")

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

    await adapter.close()


def main():
    try:
        asyncio.run(run())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Остановка MAX-бота...")
    finally:
        shutdown_db()


if __name__ == "__main__":
    main()