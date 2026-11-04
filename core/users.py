# core/users.py
"""Админы, баны, лимиты сообщений."""
import logging
import threading
import time
import os

from core.db import (
    get_db_connection, return_db_connection,
)
from core.runtime import platform

logger = logging.getLogger("bright_city_bot")


# ============================================================
#  BOOTSTRAP-АДМИНЫ (платформо-зависимые, пункт 1)
# ============================================================
#
# Раньше был один плоский set: is_admin_user() смотрел только на
# числовой ID, игнорируя платформу. Если в TG-процессе ADMIN_TG_IDS
# случайно совпадал с VK-шным ADMIN_ID (или был пустой → "0"), то
# TG-юзер с тем же числовым ID получал админ-права.
#
# Теперь: {platform: {user_id, ...}} и авторизация строго по platform().
#
# BOOTSTRAP_ADMIN_IDS остаётся как legacy-union — используется ТОЛЬКО
# в проверках вида «нельзя разжаловать bootstrap-админа» (core.admin).
# Для авторизации НЕ использовать.

BOOTSTRAP_ADMIN_IDS_BY_PLATFORM: dict = {}

BOOTSTRAP_ADMIN_IDS: set = set()


def init_bootstrap_admins(admin_ids, platform_name=None):
    """Заполняет bootstrap-админов для ОДНОЙ платформы.

    platform_name — 'vk' | 'tg' | 'max'. Если None, берётся из
    env BOT_PLATFORM (его проставляют точки входа до этого вызова).

    Повторный вызов с той же платформой перезаписывает набор
    (идемпотентно при перезапуске).
    """
    if platform_name is None:
        platform_name = (os.getenv("BOT_PLATFORM") or "").strip().lower()
    if platform_name not in ("vk", "tg", "max"):
        raise RuntimeError(
            "init_bootstrap_admins: platform_name обязателен "
            "('vk'|'tg'|'max'), получено %r" % (platform_name,)
        )
    ids = {int(x) for x in admin_ids if int(x) > 0}
    BOOTSTRAP_ADMIN_IDS_BY_PLATFORM[platform_name] = ids

    # Legacy-union — перестраиваем на каждом вызове.
    BOOTSTRAP_ADMIN_IDS.clear()
    for s in BOOTSTRAP_ADMIN_IDS_BY_PLATFORM.values():
        BOOTSTRAP_ADMIN_IDS.update(s)

    logger.info(
        "Bootstrap admins [%s] = %s (объединение по платформам: %d)",
        platform_name, sorted(ids), len(BOOTSTRAP_ADMIN_IDS),
    )


def _notify_cache_invalidate(kind: str, user_id: int,
                             platform_name: str = None) -> None:
    """Тонкая обёртка над core.db.notify_cache_invalidate.

    platform_name — платформа, к которой относится инвалидация.
    Передавайте явно для кросс-платформенных операций (ban_user с
    target_platform='tg' из VK-процесса и т.п.), иначе payload уйдёт
    без платформы и слушатель сбросит только ТЕКУЩУЮ платформу —
    ключ чужой платформы останется stale до истечения TTL.
    """
    try:
        from core.db import notify_cache_invalidate
        notify_cache_invalidate(kind, user_id, platform_name=platform_name)
    except Exception as e:
        logger.warning(
            "_notify_cache_invalidate failed (kind=%s, user_id=%s): %s",
            kind, user_id, e)


# --- КЭШ АДМИНОВ ---

_admin_cache = {}
_admin_cache_lock = threading.Lock()
_ADMIN_CACHE_TTL = 10


def sync_bootstrap_admins_to_db():
    conn = None
    # ФИКС Баг-31: собираем id, чтобы после commit'а разослать
    # pg_notify в чужие процессы. Определение и заполнение переменной —
    # ОБЯЗАТЕЛЬНО до основного цикла, иначе NameError на финальном
    # for (см. лог: NameError: name 'activated_ids' is not defined).
    activated_ids = []
    try:
        pl = platform()
        conn = get_db_connection()
        with conn.cursor() as cur:
            for admin_id in BOOTSTRAP_ADMIN_IDS_BY_PLATFORM.get(pl, ()):
                cur.execute("""
                    INSERT INTO admin_users (platform, admin_id, added_by, is_active)
                    VALUES (%s, %s, NULL, TRUE)
                    ON CONFLICT (platform, admin_id) DO UPDATE
                    SET is_active = TRUE
                """, (pl, admin_id))
                activated_ids.append(admin_id)
        conn.commit()
        logger.info("Bootstrap admins synced to DB (platform=%s, count=%d)",
                    pl, len(activated_ids))
    except Exception as e:
        logger.error("sync_bootstrap_admins_to_db failed: %s", e)
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)

    # ФИКС Баг-31: инвалидируем кэш ЧУЖИХ процессов. При старте мы
    # могли активировать админа, которого другой процесс считает
    # не-админом (кэш [pl, uid] = False) — до 10 секунд он будет
    # неавторизованным в чужом процессе.
    #
    # Свой кэш тоже сбросим (безвредно): activate могло не затронуть
    # строку (ON CONFLICT DO UPDATE при is_active=TRUE — no-op).
    for admin_id in activated_ids:
        try:
            _invalidate_admin_cache(admin_id, target_platform=pl)
            _notify_cache_invalidate("admin", admin_id, platform_name=pl)
        except Exception as e:
            logger.warning(
                "sync_bootstrap_admins_to_db: notify admin %s/%s "
                "failed: %s", pl, admin_id, e)


def _invalidate_admin_cache(user_id=None, target_platform=None):
    """Сбрасывает in-memory кэш админов.

    target_platform — платформа, для которой сбрасывается ключ.
    Если None, берётся текущая платформа процесса. Нужно для
    кросс-процессного сброса: cache_listener получает платформу
    из payload и явно передаёт её сюда.
    """
    with _admin_cache_lock:
        if user_id is None:
            _admin_cache.clear()
        else:
            pl = target_platform or platform()
            key = (pl, user_id)
            _admin_cache.pop(key, None)


def is_admin_user(user_id, target_platform=None):
    """Проверяет, что user_id — активный админ.

    target_platform — если задан, проверка идёт для этой платформы.
    По умолчанию — текущая. Нужно для кросс-платформенных операций:
    _exec_ban проверяет, что цель не является админом НА СВОЕЙ платформе,
    а не на платформе процесса, который выполняет команду.
    """
    pl = target_platform or platform()
    if user_id in BOOTSTRAP_ADMIN_IDS_BY_PLATFORM.get(pl, ()):
        return True
    cache_key = (pl, user_id)
    now = time.time()
    with _admin_cache_lock:
        cached = _admin_cache.get(cache_key)
    if cached is not None and now - cached[1] < _ADMIN_CACHE_TTL:
        return cached[0]
    is_admin = False
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM admin_users "
                "WHERE platform = %s AND admin_id = %s AND is_active = TRUE",
                (pl, user_id))
            is_admin = cur.fetchone() is not None
    except Exception as e:
        logger.error("DB error (is_admin_user): %s", e)
        is_admin = False
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)
    with _admin_cache_lock:
        _admin_cache[cache_key] = (is_admin, now)
    return is_admin


def add_dynamic_admin(target_id, added_by):
    if target_id in BOOTSTRAP_ADMIN_IDS:
        return False
    conn = None
    try:
        pl = platform()
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO admin_users (platform, admin_id, added_by, is_active)
                VALUES (%s, %s, %s, TRUE)
                ON CONFLICT (platform, admin_id) DO UPDATE
                SET is_active = TRUE, added_by = EXCLUDED.added_by, added_at = NOW()
            """, (pl, target_id, added_by))

            # ФИКС: DEFAULT TRUE в схеме → новый админ по умолчанию
            # получает ВЕСЬ поток: сообщения пользователей, GDPR-алерты,
            # уведомления о заявках. Для только что назначенного админа
            # это приватностный и спам-риск. Явно ставим FALSE и
            # оставляем за админом решение включить.
            #
            # ON CONFLICT DO NOTHING — если строка уже есть (например,
            # админ был раньше и его сняли/назначили снова), не трогаем
            # существующие настройки: он их сознательно выставил.
            cur.execute("""
                INSERT INTO admin_settings
                    (platform, admin_id,
                     notify_applications, receive_user_messages,
                     show_media_in_applications)
                VALUES (%s, %s, FALSE, FALSE, TRUE)
                ON CONFLICT (platform, admin_id) DO NOTHING
            """, (pl, target_id))

        conn.commit()
        _invalidate_admin_cache(target_id, target_platform=pl)
        _notify_cache_invalidate("admin", target_id, platform_name=pl)
        logger.info("Dynamic admin added: %s/%s (by %s)", pl, target_id, added_by)
        return True
    except Exception as e:
        logger.error("DB error (add_dynamic_admin): %s", e)
        return False
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)


def remove_dynamic_admin(target_id):
    if target_id in BOOTSTRAP_ADMIN_IDS:
        return False
    conn = None
    try:
        pl = platform()
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE admin_users SET is_active = FALSE "
                "WHERE platform = %s AND admin_id = %s",
                (pl, target_id))
            affected = cur.rowcount
        conn.commit()
        _invalidate_admin_cache(target_id, target_platform=pl)
        _notify_cache_invalidate("admin", target_id, platform_name=pl)
        if affected > 0:
            logger.info("Dynamic admin removed: %s/%s", pl, target_id)
        return affected > 0
    except Exception as e:
        logger.error("DB error (remove_dynamic_admin): %s", e)
        return False
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)


def list_admins():
    rows = []
    pl = platform()
    for aid in BOOTSTRAP_ADMIN_IDS_BY_PLATFORM.get(pl, ()):
        rows.append({"admin_id": aid, "is_bootstrap": True,
                     "added_by": None, "added_at": None})
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT admin_id, added_by, added_at FROM admin_users
                WHERE platform = %s AND is_active = TRUE
                ORDER BY added_at
            """, (pl,))
            for row in cur.fetchall():
                rows.append({
                    "admin_id": row[0], "is_bootstrap": False,
                    "added_by": row[1], "added_at": row[2],
                })
    except Exception as e:
        logger.error("DB error (list_admins): %s", e)
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)
    seen = set()
    unique = []
    for r in rows:
        if r["admin_id"] in seen:
            continue
        seen.add(r["admin_id"])
        unique.append(r)
    return unique


# --- НАСТРОЙКИ АДМИНОВ ---

def get_admin_settings(admin_id):
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT notify_applications, receive_user_messages, "
                "show_media_in_applications "
                "FROM admin_settings "
                "WHERE platform = %s AND admin_id = %s",
                (platform(), admin_id))
            row = cur.fetchone()
            if row:
                return {
                    "notify_applications": row[0],
                    "receive_user_messages": row[1],
                    "show_media_in_applications": (
                        row[2] if row[2] is not None else True),
                }
    except Exception as e:
        logger.error("DB error (get admin settings): %s", e)
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)
    return {
        "notify_applications": True,
        "receive_user_messages": True,
        "show_media_in_applications": True,
    }


def set_admin_setting(admin_id, setting_name, value):
    conn = None
    try:
        pl = platform()
        conn = get_db_connection()
        with conn.cursor() as cur:
            if setting_name == "notify_applications":
                cur.execute("""
                    INSERT INTO admin_settings
                        (platform, admin_id, notify_applications,
                         receive_user_messages, show_media_in_applications)
                    VALUES (%s, %s, %s, TRUE, TRUE)
                    ON CONFLICT (platform, admin_id) DO UPDATE
                    SET notify_applications = EXCLUDED.notify_applications
                """, (pl, admin_id, value))
            elif setting_name == "receive_user_messages":
                cur.execute("""
                    INSERT INTO admin_settings
                        (platform, admin_id, notify_applications,
                         receive_user_messages, show_media_in_applications)
                    VALUES (%s, %s, TRUE, %s, TRUE)
                    ON CONFLICT (platform, admin_id) DO UPDATE
                    SET receive_user_messages = EXCLUDED.receive_user_messages
                """, (pl, admin_id, value))
            elif setting_name == "show_media_in_applications":
                cur.execute("""
                    INSERT INTO admin_settings
                        (platform, admin_id, notify_applications,
                         receive_user_messages, show_media_in_applications)
                    VALUES (%s, %s, TRUE, TRUE, %s)
                    ON CONFLICT (platform, admin_id) DO UPDATE
                    SET show_media_in_applications = EXCLUDED.show_media_in_applications
                """, (pl, admin_id, value))
        conn.commit()
    except Exception as e:
        logger.error("DB error (set admin setting): %s", e)
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)


def get_admins_with_setting(setting_name, value=True):
    result = []
    conn = None
    pl = platform()
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            if setting_name == "notify_applications":
                cur.execute(
                    "SELECT admin_id FROM admin_settings "
                    "WHERE platform = %s AND notify_applications = %s",
                    (pl, value))
            elif setting_name == "receive_user_messages":
                cur.execute(
                    "SELECT admin_id FROM admin_settings "
                    "WHERE platform = %s AND receive_user_messages = %s",
                    (pl, value))
            else:
                return list(BOOTSTRAP_ADMIN_IDS_BY_PLATFORM.get(pl, ()))
            rows = cur.fetchall()
            result = [row[0] for row in rows]
    except Exception as e:
        logger.error("DB error (get admins with setting): %s", e)
        result = list(BOOTSTRAP_ADMIN_IDS_BY_PLATFORM.get(pl, ()))
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)
    return result


# --- БАНЛИСТ ---

_ban_cache = {}
_ban_cache_lock = threading.Lock()
_BAN_CACHE_TTL = 10


def is_banned(user_id, target_platform=None):
    """Проверяет, забанен ли user_id.

    ФИКС Bug-8: добавлен параметр target_platform. Нужен для
    кросс-платформенной проверки — когда VK-процесс проверяет,
    не забанен ли TG-получатель (перед отправкой через outbox),
    или наоборот. Раньше проверка всегда шла по текущей платформе
    процесса, поэтому кросс-платформенный бан не учитывался.
    """
    pl = target_platform or platform()
    cache_key = (pl, user_id)
    now = time.time()
    with _ban_cache_lock:
        cached = _ban_cache.get(cache_key)
    if cached is not None and now - cached[1] < _BAN_CACHE_TTL:
        return cached[0]

    is_banned_val = False
    failed = False
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM banned_users "
                "WHERE platform = %s AND user_id = %s "
                "AND (expires_at IS NULL OR expires_at > NOW())",
                (pl, user_id))
            is_banned_val = cur.fetchone() is not None
    except Exception as e:
        logger.error("DB error (is_banned): %s", e)
        failed = True
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)

    if failed:
        # Fail-closed: при ошибке БД считаем забаненным, чтобы не
        # отправить сообщение потенциальному нарушителю.
        return True
    with _ban_cache_lock:
        _ban_cache[cache_key] = (is_banned_val, now)
    return is_banned_val


def _invalidate_ban_cache(user_id=None, target_platform=None):
    """target_platform — если задан, инвалидируется кэш именно этой
    платформы. По умолчанию — текущая платформа процесса (нужно для
    cache_listener: каждый процесс инвалидирует свой кэш).
    """
    with _ban_cache_lock:
        if user_id is None:
            _ban_cache.clear()
        else:
            pl = target_platform or platform()
            _ban_cache.pop((pl, user_id), None)


def ban_user(user_id, admin_id, reason="", target_platform=None):
    """Банит пользователя на указанной платформе.

    target_platform — 'vk' | 'tg' | 'max'. Если None, используется
    текущая платформа процесса. Нужно для кросс-платформенного бана:
    VK-админ может забанить TG-юзера через /бан tg <id>.
    """
    conn = None
    try:
        pl = target_platform or platform()
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO banned_users
                    (platform, user_id, banned_by, banned_by_platform,
                     banned_at, expires_at, reason)
                VALUES (%s, %s, %s, %s, NOW(), NULL, %s)
                ON CONFLICT (platform, user_id) DO UPDATE
                SET banned_by = EXCLUDED.banned_by,
                    banned_by_platform = EXCLUDED.banned_by_platform,
                    banned_at = NOW(),
                    expires_at = NULL,
                    reason = EXCLUDED.reason
            """, (pl, user_id, admin_id, platform(), reason))
        conn.commit()
        _invalidate_ban_cache(user_id, target_platform=pl)
        _notify_cache_invalidate("ban", user_id, platform_name=pl)
        logger.info(
            "User %s/%s banned by admin %s/%s, reason: %s",
            pl, user_id, platform(), admin_id, reason)
        return True
    except Exception as e:
        logger.error("DB error (ban_user): %s", e)
        return False
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)


def unban_user(user_id, target_platform=None):
    """Снимает бан с пользователя.

    target_platform — если задан, разбан применяется к этой платформе.
    По умолчанию — platform() текущего процесса.
    Нужно для кросс-платформенного разбана: TG-админ разбанивает VK-юзера.
    """
    conn = None
    try:
        pl = target_platform or platform()
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE banned_users SET expires_at = NOW() "
                "WHERE platform = %s AND user_id = %s",
                (pl, user_id))
        conn.commit()
        _invalidate_ban_cache(user_id, target_platform=pl)
        _notify_cache_invalidate("ban", user_id, platform_name=pl)
        logger.info("User %s/%s unbanned", pl, user_id)
        return True
    except Exception as e:
        logger.error("DB error (unban_user): %s", e)
        return False
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)


_auto_ban_count = {}
_auto_ban_lock = threading.Lock()


def auto_ban_user(user_id):
    global _auto_ban_count
    with _auto_ban_lock:
        prev = _auto_ban_count.get(user_id, (0, 0))
        count = prev[0] + 1
        _auto_ban_count[user_id] = (count, time.time())

    if count >= 3:
        ban_reason = "Автобан: повторное превышение лимита (перманентная блокировка)"
        expires_val = None
        duration_text = "навсегда"
    elif count == 2:
        ban_reason = "Автобан: повторное превышение лимита сообщений"
        expires_val = 7200
        duration_text = "2 часа"
    else:
        ban_reason = "Автобан: превышение лимита сообщений"
        expires_val = 3600
        duration_text = "1 час"

    conn = None
    try:
        pl = platform()
        conn = get_db_connection()
        with conn.cursor() as cur:
            if expires_val is None:
                cur.execute("""
                    INSERT INTO banned_users
                        (platform, user_id, banned_by, banned_by_platform,
                         banned_at, expires_at, reason, ban_count)
                    VALUES (%s, %s, 0, 'system', NOW(), NULL, %s, %s)
                    ON CONFLICT (platform, user_id) DO UPDATE
                    SET expires_at = NULL,
                        banned_by_platform = 'system',
                        reason = EXCLUDED.reason,
                        banned_at = NOW(),
                        ban_count = EXCLUDED.ban_count
                """, (pl, user_id, ban_reason, count))
            else:
                cur.execute("""
                    INSERT INTO banned_users
                        (platform, user_id, banned_by, banned_by_platform,
                         banned_at, expires_at, reason, ban_count)
                    VALUES (%s, %s, 0, 'system', NOW(),
                            NOW() + make_interval(secs => %s), %s, %s)
                    ON CONFLICT (platform, user_id) DO UPDATE
                    SET expires_at = NOW() + make_interval(secs => %s),
                        banned_by_platform = 'system',
                        reason = EXCLUDED.reason,
                        ban_count = EXCLUDED.ban_count
                """, (pl, user_id, expires_val, ban_reason, count, expires_val))
        conn.commit()
        _invalidate_ban_cache(user_id, target_platform=pl)
        _notify_cache_invalidate("ban", user_id, platform_name=pl)
        logger.warning("Auto-ban: user %s/%s banned for %s (count=%d)",
                       pl, user_id, duration_text, count)
        return duration_text
    except Exception as e:
        logger.error("DB error (auto_ban_user): %s", e)
        return None
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)


def _restore_ban_counts_from_db():
    conn = None
    try:
        pl = platform()
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT user_id, ban_count FROM banned_users
                WHERE platform = %s AND (expires_at IS NULL OR expires_at > NOW())
                ORDER BY banned_at DESC LIMIT 1000
            """, (pl,))
            results = cur.fetchall()
    except Exception as e:
        logger.error("DB error (restore ban_count): %s", e)
        results = []
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)
    now = time.time()
    with _auto_ban_lock:
        for row in results:
            uid, bc = row
            if bc and bc > 0:
                _auto_ban_count[uid] = (bc, now)


def restore_ban_counters_on_startup():
    """ФИКС Баг-26: явное восстановление счётчиков автобана из БД.

    Раньше _auto_ban_count восстанавливался только при cleanup
    check_message_rate (раз в 300 сек). После рестарта процесса первые
    5 минут счётчик был пуст, и повторные нарушения карались как
    «первые»: 1 час вместо 2 часов, 2 часа вместо «навсегда».

    Вызывать из точки входа после init_db_pool() и run_migrations().
    """
    try:
        _restore_ban_counts_from_db()
        logger.info(
            "Автобан-счётчики восстановлены из БД: %d записей",
            len(_auto_ban_count))
    except Exception as e:
        logger.error("restore_ban_counters_on_startup failed: %s", e)


# --- RATE LIMIT (сообщений в минуту) ---

_message_rate_tracker = {}
_message_rate_lock = threading.Lock()
_message_rate_cleanup_last = 0


def check_message_rate(user_id):
    global _message_rate_tracker, _message_rate_cleanup_last
    now = time.time()
    need_cleanup = False
    with _message_rate_lock:
        need_cleanup = (now - _message_rate_cleanup_last > 300)
    if need_cleanup:
        cutoff = now - 300
        with _message_rate_lock:
            stale = [k for k, v in _message_rate_tracker.items() if not v or v[-1] < cutoff]
            for k in stale:
                del _message_rate_tracker[k]
            _message_rate_cleanup_last = now
        with _auto_ban_lock:
            ab_cutoff = now - 3600
            stale_ab = [k for k, v in _auto_ban_count.items() if v[1] < ab_cutoff]
            for k in stale_ab:
                del _auto_ban_count[k]
            need_restore = not _auto_ban_count
        if need_restore:
            _restore_ban_counts_from_db()

    with _message_rate_lock:
        timestamps = _message_rate_tracker.get(user_id, [])
        timestamps = [t for t in timestamps if now - t < 60]
        count = len(timestamps)

        if count >= 30:
            _message_rate_tracker[user_id] = timestamps
            return False, 1
        if count >= 10:
            _message_rate_tracker[user_id] = timestamps
            return False, 0

        timestamps.append(now)
        _message_rate_tracker[user_id] = timestamps

    return True, 0


# --- RATE LIMIT (любые события) ---

_event_rate_tracker = {}
_event_rate_lock = threading.Lock()
EVENT_RATE_LIMIT = 60

# ФИКС Баг-28: параметры очистки устаревших записей.
# Раньше _event_rate_tracker рос линейно за всё время жизни процесса:
# обновлялась только запись текущего user_id, старые никогда не
# удалялись. При потоке в 1M пользователей — сотни МБ RSS.
_EVENT_RATE_CLEANUP_INTERVAL = 300.0   # не чаще, чем раз в 5 минут
_event_rate_cleanup_last = 0.0


def _cleanup_event_rate_tracker(now):
    """Удаляет записи пользователей, не подававших событий > 5 мин.

    Вызывается из check_event_rate под _event_rate_lock, но не чаще,
    чем раз в _EVENT_RATE_CLEANUP_INTERVAL. Без интервала мы бы
    проходили весь dict на каждом событии — O(N) на горячем пути.
    """
    global _event_rate_cleanup_last
    if now - _event_rate_cleanup_last < _EVENT_RATE_CLEANUP_INTERVAL:
        return
    cutoff = now - 300  # 5 минут: окно rate-limit = 60 сек, запас x5
    stale = [
        uid for uid, stamps in _event_rate_tracker.items()
        if not stamps or stamps[-1] < cutoff
    ]
    for uid in stale:
        del _event_rate_tracker[uid]
    _event_rate_cleanup_last = now
    if stale:
        logger.debug(
            "_cleanup_event_rate_tracker: удалено %d устаревших записей "
            "(осталось %d)", len(stale), len(_event_rate_tracker))


def check_event_rate(user_id, limit=EVENT_RATE_LIMIT):
    now = time.time()
    with _event_rate_lock:
        # ФИКС Баг-28: чистим dict, чтобы он не рос линейно.
        _cleanup_event_rate_tracker(now)

        stamps = [t for t in _event_rate_tracker.get(user_id, []) if now - t < 60]
        count = len(stamps)
        if count >= limit:
            _event_rate_tracker[user_id] = stamps
            return False, count
        stamps.append(now)
        _event_rate_tracker[user_id] = stamps
    return True, count + 1


# --- RATE LIMIT (start_registration / confirm) ---

_application_rate_limit = {}
_rate_limit_lock = threading.Lock()
_rate_limit_cleanup_last = 0


def check_rate_limit(user_id, scope="default"):
    global _rate_limit_cleanup_last
    global _application_rate_limit
    now = time.time()
    key = (user_id, scope)
    with _rate_limit_lock:
        if now - _rate_limit_cleanup_last > 120:
            cutoff = now - 120
            stale = [k for k, v in _application_rate_limit.items() if v <= cutoff]
            for k in stale:
                del _application_rate_limit[k]
            _rate_limit_cleanup_last = now
        last = _application_rate_limit.get(key, 0)
        elapsed = now - last
        if elapsed < 60:
            return False, int(60 - elapsed)
        _application_rate_limit[key] = now
        return True, 0


# --- ЛИМИТ СООБЩЕНИЙ НА ОДИН ШАГ ---

STEP_MESSAGE_LIMIT_DEFAULT = 8

STEP_MESSAGE_LIMITS = {
    # STATE_START — «свободный чат»: пользователь отвечает админу в
    # произвольной форме. Лимит шага тут бессмыслен и мешает диалогу
    # (после 8 сообщений бот отвечал «Лимит сообщений исчерпан»).
    # Общие rate-limit'ы (check_message_rate 30/мин и check_event_rate
    # 60/мин) продолжают работать — от флуда защищены.
    "start": 999,
    "child_photo": 10,
    "drawings": 10,
    "org_info": 10,
    "payment": 5,
    "question": 20,
}

_step_message_lock = threading.Lock()
_step_message_counters = {}
_STEP_COUNTER_TTL = 3600


def _get_step_limit(state):
    return STEP_MESSAGE_LIMITS.get(state, STEP_MESSAGE_LIMIT_DEFAULT)


def _cleanup_step_counters(now):
    stale = [uid for uid, e in _step_message_counters.items()
             if now - e.get("last_ts", 0) > _STEP_COUNTER_TTL]
    for uid in stale:
        del _step_message_counters[uid]


def check_step_message_limit(user_id, state):
    now = time.time()
    limit = _get_step_limit(state)
    with _step_message_lock:
        _cleanup_step_counters(now)
        entry = _step_message_counters.get(user_id)
        if entry is None or entry.get("state") != state:
            _step_message_counters[user_id] = {
                "state": state, "count": 1, "warned": False, "last_ts": now,
            }
            return True, False
        entry["count"] += 1
        entry["last_ts"] = now
        if entry["count"] <= limit:
            return True, False
        if not entry["warned"]:
            entry["warned"] = True
            return False, True
        return False, False


def reset_step_message_limit(user_id):
    with _step_message_lock:
        _step_message_counters.pop(user_id, None)


def is_banned_checked(user_id, target_platform=None):
    """Возвращает (is_banned, check_failed).

    ФИКС Баг-27: в отличие от is_banned, различает «точно забанен» и
    «не удалось проверить». is_banned оставлен как fail-closed для
    read-path (handle_event_sync, где важно не пустить нарушителя).

    Здесь — для write-path (outbox, отправка), где потеря сообщения
    хуже, чем отправка забаненному в случае сбоя БД: забаненный и так
    не прочитает сообщение через бота (state забаненных игнорируется),
    а легитимный пользователь при ложном True теряет ответ навсегда.
    """
    pl = target_platform or platform()
    cache_key = (pl, user_id)
    now = time.time()
    with _ban_cache_lock:
        cached = _ban_cache.get(cache_key)
    if cached is not None and now - cached[1] < _BAN_CACHE_TTL:
        return cached[0], False

    is_banned_val = False
    failed = False
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM banned_users "
                "WHERE platform = %s AND user_id = %s "
                "AND (expires_at IS NULL OR expires_at > NOW())",
                (pl, user_id))
            is_banned_val = cur.fetchone() is not None
    except Exception as e:
        logger.error("DB error (is_banned_checked): %s", e)
        failed = True
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)

    if not failed:
        with _ban_cache_lock:
            _ban_cache[cache_key] = (is_banned_val, now)
    return is_banned_val, failed


def upsert_platform_user(platform_name: str, user_id: int,
                         first_name: str = None, last_name: str = None,
                         username: str = None) -> None:
    """Сохраняет/обновляет профиль пользователя соцсети."""
    if platform_name not in ("vk", "tg", "max"):
        return
    first_name = (first_name or "").strip() or None
    last_name = (last_name or "").strip() or None
    username = (username or "").strip().lstrip("@") or None

    parts = [p for p in (first_name, last_name) if p]
    display_name = " ".join(parts) if parts else None
    if not display_name and username:
        display_name = f"@{username}"

    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO platform_users
                    (platform, user_id, first_name, last_name, username,
                     display_name, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (platform, user_id) DO UPDATE SET
                    first_name   = COALESCE(EXCLUDED.first_name,
                                            platform_users.first_name),
                    last_name    = COALESCE(EXCLUDED.last_name,
                                            platform_users.last_name),
                    username     = COALESCE(EXCLUDED.username,
                                            platform_users.username),
                    display_name = COALESCE(EXCLUDED.display_name,
                                            platform_users.display_name),
                    updated_at   = NOW()
            """, (platform_name, user_id, first_name, last_name, username,
                  display_name))
        conn.commit()
    except Exception as e:
        logger.error("DB error (upsert_platform_user): %s", e)
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)