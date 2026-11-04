# core/cache_listener.py
"""ФИКС P2.7: кросс-процессный сброс кэшей прав и банов.

Каждый процесс (VK и TG) держит ОДНО долгоживущее соединение к БД,
на котором сделан LISTEN bot_cache_invalidate. При любом изменении
прав/банов процесс-источник вызывает core.db.notify_cache_invalidate
(она делает pg_notify). Все слушатели в ОБОИХ процессах получают
уведомление и немедленно сбрасывают свой in-memory кэш для
указанного (platform, user_id), не дожидаясь истечения TTL (10с).

ФИКС (security): payload теперь включает ПЛАТФОРМУ: "kind:platform:user_id".
Раньше платформа не передавалась, и слушатель инвалидировал кэш только
для СВОЕЙ платформы. Это оставляло устаревшее значение в кэше ключа
ЧУЖОЙ платформы на срок до _ADMIN_CACHE_TTL / _BAN_CACHE_TTL (10 с).
Практическое следствие: VK-процесс, кэшировавший is_admin_user(uid,
target_platform='tg') = False до того, как TG-процесс выдал uid админку,
в течение 10 с продолжал считать uid не-админом и мог забанить его
через /бан tg <uid>.

Почему отдельное соединение, а не из пула:
  - LISTEN привязан к конкретному backend-сеансу PG. Если соединение
    вернуть в пул (putconn), backend останется тем же, но другой код
    может его переиспользовать и случайно снять LISTEN.
  - psycopg2 ждёт уведомлений блокирующим select(). Если делать это
    на соединении из пула, оно будет занято вечно — пул «съест» слот.

Поэтому здесь создаётся отдельное соединение, живущее ровно столько,
сколько живёт слушатель.

Формат payload: "<kind>:<platform>:<user_id>",
  kind ∈ {"admin", "ban"}, platform ∈ {"vk", "tg", "max"}.
Legacy-формат "<kind>:<user_id>" (без платформы) поддерживается на
время плавного деплоя: сбрасываем кэш только для ТЕКУЩЕЙ платформы
процесса (прежнее поведение).

ВАЖНО: этот модуль НЕ импортирует core.users на уровне модуля —
циклический импорт. Импорт core.users делается лениво внутри callback.
"""
import logging
import select
import threading
import time

import psycopg2
from psycopg2 import extras as _pg_extras

from core.db import DB_CONFIG

logger = logging.getLogger("bright_city_bot.cache_listener")


_CHANNEL = "bot_cache_invalidate"

# Backoff при обрыве соединения: 1 → 2 → 4 → ... → 30 сек.
_RECONNECT_BACKOFF_INITIAL = 1.0
_RECONNECT_BACKOFF_MAX = 30.0

# select() ждёт уведомлений не дольше этого таймаута. Нужен, чтобы
# периодически проверять stop_event и не висеть вечно в select().
_SELECT_TIMEOUT_SEC = 5.0

# Допустимые платформы. Значение вне этого набора → инвалидируем
# только текущую платформу процесса (безопасный fallback).
_VALID_PLATFORMS = ("vk", "tg", "max")


def _handle_notification(payload: str) -> None:
    """Парсит payload от pg_notify и вызывает нужный сброс кэша.

    Поддерживает:
      - новый формат:  "kind:platform:user_id"  (приоритетный);
      - legacy-формат: "kind:user_id"           (fallback без платформы).

    Для legacy-формата и для неизвестной платформы инвалидируем
    только ТЕКУЩУЮ платформу процесса — то есть ведём себя как раньше,
    не расширяя область сброса по ошибке.
    """
    if not payload or ":" not in payload:
        logger.warning("cache_listener: bad payload %r", payload)
        return

    parts = payload.split(":", 2)
    if len(parts) == 3:
        kind, pl, uid_str = parts
        if pl not in _VALID_PLATFORMS:
            logger.warning(
                "cache_listener: bad platform %r in payload %r — "
                "сброшу только текущую платформу", pl, payload)
            pl = None
    elif len(parts) == 2:
        kind, uid_str = parts
        pl = None
        logger.warning(
            "cache_listener: payload без платформы %r — "
            "сброшу только текущую платформу", payload)
    else:
        logger.warning("cache_listener: bad payload %r", payload)
        return

    try:
        user_id = int(uid_str)
    except ValueError:
        logger.warning("cache_listener: bad user_id in payload %r", payload)
        return

    # Ленивый импорт — избегаем циклов и лишних зависимостей на этапе старта.
    try:
        from core.users import _invalidate_admin_cache, _invalidate_ban_cache
    except Exception as e:
        logger.error("cache_listener: cannot import core.users: %s", e)
        return

    if kind == "admin":
        try:
            _invalidate_admin_cache(user_id, target_platform=pl)
            logger.info(
                "cache_listener: invalidated admin cache for %s/%s",
                pl or "current", user_id)
        except Exception as e:
            logger.error("cache_listener: invalidate admin cache failed: %s", e)
    elif kind == "ban":
        try:
            _invalidate_ban_cache(user_id, target_platform=pl)
            logger.info(
                "cache_listener: invalidated ban cache for %s/%s",
                pl or "current", user_id)
        except Exception as e:
            logger.error("cache_listener: invalidate ban cache failed: %s", e)
    elif kind == "gdpr":
        # ФИКС Баг-3: pg_notify "gdpr:<platform>:<user_id>" приходит от
        # core.gdpr.delete_user_data / anonymize_user_data. Сбрасываем
        # ДВА кэша:
        #   1) core.db._erased_cache (60 с TTL) — is_user_erased в этом
        #      процессе;
        #   2) UserState._erased_local (300 с TTL) + state/temp-кэш через
        #      user_state.invalidate — иначе _save пропустит запись
        #      по устаревшему _erased_local = False.
        #
        # pl here всегда задан: notify_cache_invalidate валидирует
        # platform_name перед отправкой.
        if pl is None:
            logger.warning(
                "cache_listener: gdpr payload без платформы %r — skip",
                payload)
            return

        try:
            from core.db import _invalidate_erased_cache
            _invalidate_erased_cache(pl, user_id)
            logger.info(
                "cache_listener: invalidated erased cache for %s/%s",
                pl, user_id)
        except Exception as e:
            logger.error(
                "cache_listener: invalidate erased cache failed: %s", e)

        try:
            from core.storage import user_state as _us
            _us.invalidate(user_id, platform_name=pl)
            logger.info(
                "cache_listener: invalidated UserState for %s/%s",
                pl, user_id)
        except Exception as e:
            logger.error(
                "cache_listener: invalidate UserState failed: %s", e)
    else:
        logger.warning("cache_listener: unknown kind %r", kind)


def _run_once(stop_event: threading.Event) -> None:
    """Одно подключение: LISTEN, select-цикл до stop_event или обрыва."""
    # ФИКС: DB_CONFIG уже содержит ключ application_name, нельзя
    # передавать его второй раз через **kwargs — Python бросит
    # TypeError: got multiple values for keyword argument.
    # Делаем копию и переопределяем только в ней.
    cfg = dict(DB_CONFIG)
    cfg["application_name"] = (
        cfg.get("application_name", "bright_city_bot") + "_cache_listener"
    )
    conn = psycopg2.connect(**cfg)
    try:
        # autocommit обязателен: LISTEN внутри транзакции не生效 до COMMIT,
        # и уведомления не будут доставляться, пока транзакция открыта.
        conn.autocommit = True

        with conn.cursor() as cur:
            cur.execute(f"LISTEN {_CHANNEL}")
        logger.info("cache_listener: LISTEN %s OK (db=%s)",
                    _CHANNEL, DB_CONFIG.get("dbname"))

        while not stop_event.is_set():
            # select() с таймаутом — не блокируемся навсегда, чтобы
            # иметь возможность отреагировать на stop_event.
            try:
                rlist, _, _ = select.select([conn], [], [], _SELECT_TIMEOUT_SEC)
            except (OSError, ValueError) as e:
                # conn закрыт извне — выходим.
                logger.warning("cache_listener: select failed: %s", e)
                break

            if not rlist:
                continue

            # Забираем все накопившиеся NOTIFY одним заходом.
            try:
                conn.poll()
            except Exception as e:
                logger.warning("cache_listener: poll failed: %s", e)
                break

            while conn.notifies:
                notify = conn.notifies.pop(0)
                try:
                    _handle_notification(notify.payload)
                except Exception as e:
                    logger.error("cache_listener: handler failed: %s", e)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def cache_listener_worker(stop_event: threading.Event = None) -> None:
    """Блокирующий цикл LISTEN с автопереподключением.

    Запускается в отдельном потоке (daemon=True) из точки входа.
    Завершается при установке stop_event.

    Не падает при обрыве соединения к БД: логирует, ждёт backoff и
    подключается заново.
    """
    if stop_event is None:
        stop_event = threading.Event()

    backoff = _RECONNECT_BACKOFF_INITIAL
    while not stop_event.is_set():
        try:
            _run_once(stop_event)
            # Если _run_once вернулась без исключения — либо stop_event,
            # либо conn закрыт. В обоих случаях выходим из цикла
            # (stop_event проверяется в начале).
            if stop_event.is_set():
                break
            # conn закрылся штатно, но stop_event не выставлен —
            # переподключаемся с backoff.
            logger.info("cache_listener: connection closed, reconnect in %.1fs",
                        backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, _RECONNECT_BACKOFF_MAX)
        except Exception as e:
            if stop_event.is_set():
                break
            logger.warning(
                "cache_listener: connection failed (%s), reconnect in %.1fs",
                e, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, _RECONNECT_BACKOFF_MAX)

        # Удачное подключение и работа без обрыва — сбрасываем backoff.
        # Сюда попадаем только после исключения; backoff уже увеличен.
        # Дополнительная логика не нужна: если соединение стабильно —
        # цикл уходит в _run_once и вернётся только при обрыве.


def start_cache_listener_thread() -> threading.Event:
    """Запускает слушатель в daemon-потоке. Возвращает stop_event.

    Вызывается из run_vk.py / run_tg.py. Поток daemon=True — умрёт
    вместе с процессом, отдельная остановка не обязательна, но
    stop_event можно выставить для чистого shutdown.
    """
    stop_event = threading.Event()
    thread = threading.Thread(
        target=cache_listener_worker,
        args=(stop_event,),
        name="cache-listener",
        daemon=True,
    )
    thread.start()
    logger.info("cache_listener: thread started (name=%s)", thread.name)
    return stop_event
