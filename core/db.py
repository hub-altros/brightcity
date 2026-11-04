# core/db.py
"""Инфраструктура БД: пул соединений, миграции, сериализация JSON."""
import os
import time
import json
import logging
import threading

from dotenv import load_dotenv
from psycopg2.extras import Json
from psycopg2.pool import ThreadedConnectionPool
import psycopg2.errors
from core.schema import ensure_schema, check_schema_compatibility
from core.utils import safe_str  # noqa: F401

logger = logging.getLogger("bright_city_bot")

load_dotenv(encoding="utf-8")

DB_CONFIG = {
    "dbname": os.getenv("DB_NAME"),
    "user": os.getenv("DB_USER"),
    "password": os.getenv("DB_PASSWORD"),
    "host": os.getenv("DB_HOST", "localhost"),
    "port": int(os.getenv("DB_PORT") or "5432"),
    "connect_timeout": int(os.getenv("DB_CONNECT_TIMEOUT") or "5"),
    "sslmode": os.getenv("DB_SSLMODE", "require"),
    "application_name": os.getenv("DB_APP_NAME", "bright_city_bot"),
}

for _key in ["dbname", "user", "password", "host"]:
    if not DB_CONFIG[_key]:
        raise RuntimeError(f"Не задано DB_{_key.upper()} в .env")
if not isinstance(DB_CONFIG["port"], int) or DB_CONFIG["port"] <= 0:
    raise RuntimeError("DB_PORT должен быть положительным целым числом")

# --- ПУЛ СОЕДИНЕНИЙ ---

_db_pool = None
_pool_lock = threading.Lock()


def init_db_pool():
    global _db_pool
    with _pool_lock:
        if _db_pool is None:
            _db_pool = ThreadedConnectionPool(
                1, int(os.getenv("DB_POOL_MAX") or "20"), **DB_CONFIG)


def get_db_connection(timeout=10):
    if _db_pool is None:
        init_db_pool()
    deadline = time.time() + timeout
    last_err = None
    while time.time() < deadline:
        try:
            conn = _db_pool.getconn()
            if conn is not None:
                return conn
        except Exception as e:
            last_err = e
            time.sleep(0.5)
    raise ConnectionError(
        f"Не удалось получить соединение из пула за {timeout} сек: {last_err}"
    )


def return_db_connection(conn):
    if conn is None or _db_pool is None:
        return
    try:
        if conn.closed:
            _db_pool.putconn(conn, close=True)
            return
        conn.rollback()
        _db_pool.putconn(conn)
    except Exception:
        try:
            _db_pool.putconn(conn, close=True)
        except Exception:
            pass


_erased_cache = {}
_erased_lock = threading.Lock()
_ERASED_TTL = 60


def _invalidate_erased_cache(platform_name, user_id):
    """Сбрасывает локальный кэш is_user_erased для пары (platform, user_id)."""
    with _erased_lock:
        _erased_cache.pop((platform_name, user_id), None)


def clear_gdpr_erasure_log(platform_name, user_id):
    """Снимает защиту от воскрешения ПДн после GDPR-удаления."""
    key = (platform_name, user_id)
    conn = None
    cleared = False
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM gdpr_erasure_log "
                "WHERE platform = %s AND user_id = %s",
                (platform_name, user_id))
            cleared = cur.rowcount > 0
        conn.commit()
    except Exception as e:
        logger.error("DB error (clear_gdpr_erasure_log): %s", e)
        return False
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)
    _invalidate_erased_cache(platform_name, user_id)
    return cleared


def is_user_erased(platform_name, user_id):
    key = (platform_name, user_id)
    now = time.time()
    with _erased_lock:
        hit = _erased_cache.get(key)
    if hit and now - hit[1] < _ERASED_TTL:
        return hit[0]
    erased = False
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM gdpr_erasure_log "
                        "WHERE platform = %s AND user_id = %s",
                        (platform_name, user_id))
            erased = cur.fetchone() is not None
    except Exception as e:
        logger.error("is_user_erased: %s (миграция gdpr_erasure_log применена?)", e)
        erased = False
    finally:
        return_db_connection(conn)
    with _erased_lock:
        _erased_cache[key] = (erased, now)
    return erased


def log_admin_action(admin_id, action, target_platform=None, target_id=None,
                     params=None):
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO admin_audit_log
                    (admin_id, admin_platform, action, target_platform,
                     target_id, params)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (admin_id, _runtime_platform(), action, target_platform,
                  target_id, SafeJson(params or {})))
        conn.commit()
        return True
    except Exception as e:
        logger.error("DB error (log_admin_action): %s", e)
        return False
    finally:
        return_db_connection(conn)


def _runtime_platform():
    try:
        from core.runtime import platform as _p
        return _p()
    except Exception:
        return "?"


# --- ЗАПУСК МИГРАЦИЙ ---

def run_migrations(max_attempts=6, delay_sec=5):
    last_err = None
    for attempt in range(1, max_attempts + 1):
        conn = None
        try:
            conn = get_db_connection()

            with conn.cursor() as cur:
                ensure_schema(cur)
            conn.commit()

            _ensure_media_files_columns(conn)
            conn.commit()

            _ensure_outbox_columns(conn)
            conn.commit()

            _ensure_banned_users_columns(conn)
            conn.commit()

            _ensure_admin_settings_columns(conn)
            conn.commit()

            _ensure_bot_messages_columns(conn)
            conn.commit()

            with conn.cursor() as cur:
                check_schema_compatibility(cur)
            conn.commit()

            logger.info("Миграции БД применены (attempt %d).", attempt)
            return
        except psycopg2.errors.LockNotAvailable as e:
            if conn:
                try:
                    conn.rollback()
                except Exception:
                    pass
            last_err = e
            if attempt < max_attempts:
                logger.warning(
                    "run_migrations: lock timeout (attempt %d/%d), "
                    "retry через %d сек",
                    attempt, max_attempts, delay_sec)
                time.sleep(delay_sec)
                continue
        except (psycopg2.errors.DuplicateColumn,
                psycopg2.errors.UniqueViolation) as e:
            if conn:
                try:
                    conn.rollback()
                except Exception:
                    pass
            logger.warning("run_migrations: параллельная миграция, продолжаю: %s", e)
            return
        except RuntimeError:
            if conn:
                try:
                    conn.rollback()
                except Exception:
                    pass
            raise
        except Exception:
            if conn:
                try:
                    conn.rollback()
                except Exception:
                    pass
            raise
        finally:
            if conn:
                return_db_connection(conn)

    raise RuntimeError(
        f"Не удалось применить миграции после {max_attempts} попыток: {last_err}"
    )


def shutdown_db():
    global _db_pool
    if _db_pool is not None:
        _db_pool.closeall()
        _db_pool = None
        logger.info("Database pool closed.")


# --- JSON ---

class SafeJson(Json):
    def dumps(self, obj):
        return json.dumps(obj, ensure_ascii=False)


def _ensure_bot_messages_columns(conn):
    """Добавляет поля для нативного reply/edit в bot_messages."""
    try:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL lock_timeout = '30s'")
            for col, ddl in [
                ("platform_message_id",
                 "ALTER TABLE bot_messages ADD COLUMN platform_message_id TEXT"),
                ("edited_at",
                 "ALTER TABLE bot_messages ADD COLUMN edited_at TIMESTAMPTZ"),
                ("reply_to_id",
                 "ALTER TABLE bot_messages ADD COLUMN reply_to_id BIGINT"),
                ("reply_to_text",
                 "ALTER TABLE bot_messages ADD COLUMN reply_to_text TEXT"),
                ("reply_to_direction",
                 "ALTER TABLE bot_messages ADD COLUMN reply_to_direction TEXT"),
            ]:
                cur.execute("""SELECT 1 FROM information_schema.columns
                    WHERE table_schema = current_schema()
                      AND table_name = 'bot_messages' AND column_name = %s""",
                            (col,))
                if cur.fetchone() is None:
                    cur.execute(ddl)
                    logger.info("Column %s added to bot_messages", col)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_bot_messages_platform_msg_id
                ON bot_messages (platform_message_id)
                WHERE platform_message_id IS NOT NULL
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_bot_messages_reply_to_id
                ON bot_messages (reply_to_id)
                WHERE reply_to_id IS NOT NULL
            """)
    except Exception as e:
        logger.error("DB error (_ensure_bot_messages_columns): %s", e)
        raise


def _ensure_media_files_columns(conn):
    try:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL lock_timeout = '30s'")
            for col, ddl in [
                ("vk_delete_confirmed",
                 "ALTER TABLE media_files ADD COLUMN vk_delete_confirmed BOOLEAN DEFAULT FALSE"),
                ("vk_delete_attempts",
                 "ALTER TABLE media_files ADD COLUMN vk_delete_attempts INTEGER DEFAULT 0"),
                ("vk_delete_last_at",
                 "ALTER TABLE media_files ADD COLUMN vk_delete_last_at TIMESTAMP"),
                ("vk_delete_last_error",
                 "ALTER TABLE media_files ADD COLUMN vk_delete_last_error TEXT"),
                ("source_file_id",
                 "ALTER TABLE media_files ADD COLUMN source_file_id TEXT"),
                ("source_platform",
                 "ALTER TABLE media_files ADD COLUMN source_platform TEXT"),
                ("full_relay_requested_at",
                 "ALTER TABLE media_files ADD COLUMN full_relay_requested_at TIMESTAMP"),
                ("full_relay_confirmed",
                 "ALTER TABLE media_files ADD COLUMN full_relay_confirmed BOOLEAN DEFAULT FALSE"),
                ("full_relay_url",
                 "ALTER TABLE media_files ADD COLUMN full_relay_url TEXT"),
                ("full_relay_owner_id",
                 "ALTER TABLE media_files ADD COLUMN full_relay_owner_id BIGINT"),
                ("full_relay_photo_id",
                 "ALTER TABLE media_files ADD COLUMN full_relay_photo_id BIGINT"),
                ("full_relay_attempts",
                 "ALTER TABLE media_files ADD COLUMN full_relay_attempts INTEGER DEFAULT 0"),
                ("full_relay_last_error",
                 "ALTER TABLE media_files ADD COLUMN full_relay_last_error TEXT"),
                ("doc_id",
                 "ALTER TABLE media_files ADD COLUMN doc_id BIGINT"),
                ("full_relay_last_attempt_at",
                 "ALTER TABLE media_files ADD COLUMN full_relay_last_attempt_at TIMESTAMP"),
                ("vk_url",
                 "ALTER TABLE media_files ADD COLUMN vk_url TEXT"),
                ("vk_owner_id",
                 "ALTER TABLE media_files ADD COLUMN vk_owner_id BIGINT"),
                ("vk_photo_id",
                 "ALTER TABLE media_files ADD COLUMN vk_photo_id BIGINT"),
                ("vk_doc_id",
                 "ALTER TABLE media_files ADD COLUMN vk_doc_id BIGINT"),
            ]:
                cur.execute("""SELECT 1 FROM information_schema.columns
                    WHERE table_schema = current_schema()
                      AND table_name = 'media_files' AND column_name = %s""", (col,))
                if cur.fetchone() is None:
                    cur.execute(ddl)
                    logger.info("Column %s added to media_files", col)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_media_files_pending_delete
                ON media_files(deleted_at, vk_delete_confirmed, vk_delete_last_at)
                WHERE deleted_at IS NOT NULL AND vk_delete_confirmed = FALSE
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_media_files_full_relay_pending
                ON media_files(full_relay_requested_at)
                WHERE full_relay_requested_at IS NOT NULL
                  AND full_relay_confirmed = FALSE
                  AND source_file_id IS NOT NULL
            """)
    except Exception as e:
        logger.error("DB error (_ensure_media_files_columns): %s", e)
        raise


def _ensure_banned_users_columns(conn):
    try:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL lock_timeout = '30s'")
            cur.execute("""
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = 'banned_users'
                  AND column_name = 'banned_by_platform'
            """)
            if cur.fetchone() is None:
                cur.execute("""
                    ALTER TABLE banned_users
                    ADD COLUMN banned_by_platform TEXT
                """)
                logger.info("Column banned_by_platform added to banned_users")
    except Exception as e:
        logger.error("DB error (_ensure_banned_users_columns): %s", e)
        raise


def _ensure_admin_settings_columns(conn):
    try:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL lock_timeout = '30s'")
            cur.execute("""
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = 'admin_settings'
                  AND column_name = 'show_media_in_applications'
            """)
            if cur.fetchone() is None:
                cur.execute("""
                    ALTER TABLE admin_settings
                    ADD COLUMN show_media_in_applications BOOLEAN DEFAULT TRUE
                """)
                logger.info(
                    "Column show_media_in_applications added to admin_settings")
    except Exception as e:
        logger.error("DB error (_ensure_admin_settings_columns): %s", e)
        raise


def _ensure_outbox_columns(conn):
    try:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL lock_timeout = '30s'")
            for col, ddl in [
                ("status",
                 "ALTER TABLE outbox_messages "
                 "ADD COLUMN status TEXT NOT NULL DEFAULT 'pending'"),
                ("claimed_at",
                 "ALTER TABLE outbox_messages "
                 "ADD COLUMN claimed_at TIMESTAMP"),
                ("attempts",
                 "ALTER TABLE outbox_messages "
                 "ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0"),
                ("reply_to_platform_message_id",
                 "ALTER TABLE outbox_messages "
                 "ADD COLUMN reply_to_platform_message_id TEXT"),
                ("edit_platform_message_id",
                 "ALTER TABLE outbox_messages "
                 "ADD COLUMN edit_platform_message_id TEXT"),
                ("edit_of_bot_message_id",
                 "ALTER TABLE outbox_messages "
                 "ADD COLUMN edit_of_bot_message_id BIGINT"),
                ("bot_message_id",
                 "ALTER TABLE outbox_messages "
                 "ADD COLUMN bot_message_id BIGINT"),
            ]:
                cur.execute("""SELECT 1 FROM information_schema.columns
                    WHERE table_schema = current_schema()
                      AND table_name = 'outbox_messages'
                      AND column_name = %s""", (col,))
                if cur.fetchone() is None:
                    cur.execute(ddl)
                    logger.info("Column %s added to outbox_messages", col)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_om_pending_status
                ON outbox_messages(platform, created_at)
                WHERE status = 'pending'
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_om_pending
                ON outbox_messages(platform, created_at)
                WHERE sent_at IS NULL
            """)
    except Exception as e:
        logger.error("DB error (_ensure_outbox_columns): %s", e)
        raise


# --- МЕДИАФАЙЛЫ ---


def update_media_file_local_url(media_id: int, local_url: str) -> bool:
    """Заменяет vk_url на локальный подписанный URL."""
    if not media_id or not local_url:
        return False
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE media_files SET vk_url = %s WHERE id = %s",
                (local_url, media_id))
        conn.commit()
        return True
    except Exception as e:
        logger.error(
            "DB error (update_media_file_local_url): %s", e)
        return False
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)


def register_media_file(url, owner_id=None, photo_id=None, kind="photo",
                        user_platform=None, user_id=None, application_id=None,
                        source_file_id=None, source_platform=None, doc_id=None,
                        vk_url=None, vk_owner_id=None,
                        vk_photo_id=None, vk_doc_id=None):
    """Регистрирует файл в media_files. Возвращает media_id или None."""
    if not url or not user_platform or user_id is None:
        return None
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO media_files
                    (url, owner_id, photo_id, doc_id, kind, user_platform,
                     user_id, application_id, source_file_id, source_platform,
                     vk_url, vk_owner_id, vk_photo_id, vk_doc_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s)
                ON CONFLICT (user_platform, user_id, url) DO UPDATE
                SET application_id = COALESCE(EXCLUDED.application_id,
                                              media_files.application_id),
                    kind           = EXCLUDED.kind,
                    owner_id       = COALESCE(EXCLUDED.owner_id,
                                              media_files.owner_id),
                    photo_id       = COALESCE(EXCLUDED.photo_id,
                                              media_files.photo_id),
                    doc_id         = COALESCE(EXCLUDED.doc_id,
                                              media_files.doc_id),
                    source_file_id = COALESCE(EXCLUDED.source_file_id,
                                              media_files.source_file_id),
                    source_platform= COALESCE(EXCLUDED.source_platform,
                                              media_files.source_platform),
                    vk_url         = COALESCE(EXCLUDED.vk_url,
                                              media_files.vk_url),
                    vk_owner_id    = COALESCE(EXCLUDED.vk_owner_id,
                                              media_files.vk_owner_id),
                    vk_photo_id    = COALESCE(EXCLUDED.vk_photo_id,
                                              media_files.vk_photo_id),
                    vk_doc_id      = COALESCE(EXCLUDED.vk_doc_id,
                                              media_files.vk_doc_id)
                RETURNING id
            """, (url, owner_id, photo_id, doc_id, kind, user_platform,
                  user_id, application_id, source_file_id, source_platform,
                  vk_url, vk_owner_id, vk_photo_id, vk_doc_id))
            row = cur.fetchone()
        conn.commit()
        return row[0] if row else None
    except Exception as e:
        logger.error("DB error (register_media_file): %s", e)
        return None
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)


def update_media_file_vk_info(user_platform, user_id, url, vk_url,
                              vk_owner_id=None, vk_photo_id=None,
                              vk_doc_id=None):
    """Сохраняет VK-информацию о релайнутом медиа."""
    if not user_platform or not user_id or not url:
        return False
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE media_files
                SET vk_url      = %s,
                    vk_owner_id = COALESCE(%s, vk_owner_id),
                    vk_photo_id = COALESCE(%s, vk_photo_id),
                    vk_doc_id   = COALESCE(%s, vk_doc_id)
                WHERE user_platform = %s AND user_id = %s AND url = %s
            """, (vk_url, vk_owner_id, vk_photo_id, vk_doc_id,
                  user_platform, user_id, url))
        conn.commit()
        return True
    except Exception as e:
        logger.error("DB error (update_media_file_vk_info): %s", e)
        return False
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)


def get_media_files_for_user(user_platform, user_id):
    conn = None
    rows = []
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, url, owner_id, photo_id, kind, application_id
                FROM media_files
                WHERE user_platform = %s AND user_id = %s AND deleted_at IS NULL
            """, (user_platform, user_id))
            for r in cur.fetchall():
                rows.append({"id": r[0], "url": r[1], "owner_id": r[2],
                             "photo_id": r[3], "kind": r[4], "application_id": r[5]})
    except Exception as e:
        logger.error("DB error (get_media_files_for_user): %s", e)
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)
    return rows


def mark_media_files_deleted_by_user(user_platform, user_id):
    """Помечает медиафайлы на удаление. Возвращает (marked, error)."""
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE media_files SET deleted_at = NOW()
                WHERE user_platform = %s AND user_id = %s AND deleted_at IS NULL
            """, (user_platform, user_id))
            affected = cur.rowcount
        conn.commit()
        return affected, None
    except Exception as e:
        logger.error("DB error (mark_media_files_deleted): %s", e)
        return 0, str(e)[:300]
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)


def get_media_files_pending_delete(limit=20):
    """Забирает медиафайлы для удаления с VK CDN."""
    conn = None
    rows = []
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE media_files
                SET vk_delete_last_at = NOW(),
                    vk_delete_attempts = vk_delete_attempts + 1
                WHERE id IN (
                    SELECT id FROM media_files
                    WHERE deleted_at IS NOT NULL
                      AND vk_delete_confirmed = FALSE
                      AND vk_delete_attempts < 10
                      AND (
                            (kind IN ('photo', 'payment_photo', 'banner')
                             AND owner_id IS NOT NULL AND photo_id IS NOT NULL)
                         OR (kind IN ('doc', 'payment_doc')
                             AND owner_id IS NOT NULL AND doc_id IS NOT NULL)
                      )
                      AND (vk_delete_last_at IS NULL
                           OR vk_delete_last_at < NOW() - INTERVAL '30 minutes')
                    ORDER BY vk_delete_last_at NULLS FIRST, id
                    LIMIT %s
                    FOR UPDATE SKIP LOCKED
                )
                RETURNING id, url, owner_id, photo_id, doc_id, kind,
                          vk_delete_attempts
            """, (limit,))
            for r in cur.fetchall():
                rows.append({"id": r[0], "url": r[1], "owner_id": r[2],
                             "photo_id": r[3], "doc_id": r[4], "kind": r[5],
                             "attempts": r[6]})
        conn.commit()
    except Exception as e:
        logger.error("DB error (get_media_files_pending_delete): %s", e)
        return []
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)
    return rows


def mark_media_delete_success(file_id):
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""UPDATE media_files
                SET vk_delete_confirmed = TRUE, vk_delete_last_at = NOW(),
                    vk_delete_last_error = NULL WHERE id = %s""", (file_id,))
        conn.commit()
        return True
    except Exception as e:
        logger.error("DB error (mark_media_delete_success): %s", e)
        return False
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)


def mark_media_delete_failed(file_id, error_text):
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""UPDATE media_files
                SET vk_delete_last_at = NOW(),
                    vk_delete_last_error = %s
                WHERE id = %s""", (safe_str(error_text)[:500], file_id))
        conn.commit()
        return True
    except Exception as e:
        logger.error("DB error (mark_media_delete_failed): %s", e)
        return False
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)


def mark_media_delete_flood(file_id):
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""UPDATE media_files
                SET vk_delete_last_at = NOW(),
                    vk_delete_last_error = 'flood control'
                WHERE id = %s""", (file_id,))
        conn.commit()
        return True
    except Exception as e:
        logger.error("DB error (mark_media_delete_flood): %s", e)
        return False
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)


def is_user_media_fully_deleted(platform_name, user_id):
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) FROM media_files
                WHERE user_platform = %s AND user_id = %s
                  AND deleted_at IS NOT NULL
                  AND vk_delete_confirmed = FALSE
            """, (platform_name, user_id))
            return cur.fetchone()[0] == 0
    except Exception as e:
        logger.error("DB error (is_user_media_fully_deleted): %s", e)
        return False
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)


def get_stuck_deletions(stuck_minutes=60):
    conn = None
    rows = []
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, user_platform, user_id, url, kind,
                       vk_delete_attempts, vk_delete_last_at,
                       vk_delete_last_error
                FROM media_files
                WHERE deleted_at IS NOT NULL
                  AND vk_delete_confirmed = FALSE
                  AND (
                        vk_delete_attempts >= 10
                        OR (vk_delete_last_at IS NOT NULL
                            AND vk_delete_last_at
                                < NOW() - make_interval(mins => %s))
                      )
                ORDER BY vk_delete_last_at NULLS FIRST
                LIMIT 100
            """, (stuck_minutes,))
            for r in cur.fetchall():
                rows.append({
                    "id": r[0], "user_platform": r[1], "user_id": r[2],
                    "url": r[3], "kind": r[4],
                    "attempts": r[5], "last_at": r[6], "last_error": r[7],
                })
    except Exception as e:
        logger.error("DB error (get_stuck_deletions): %s", e)
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)
    return rows


# --- СПИСОК ЗАБАНЕННЫХ ---

def get_banned_users_list(platform_name=None, limit=100, offset=0):
    conn = None
    rows = []
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            if platform_name:
                cur.execute("""
                    SELECT platform, user_id, banned_by, banned_by_platform,
                           banned_at, expires_at, reason, ban_count
                    FROM banned_users
                    WHERE platform = %s
                      AND (expires_at IS NULL OR expires_at > NOW())
                    ORDER BY banned_at DESC
                    LIMIT %s OFFSET %s
                """, (platform_name, limit, offset))
            else:
                cur.execute("""
                    SELECT platform, user_id, banned_by, banned_by_platform,
                           banned_at, expires_at, reason, ban_count
                    FROM banned_users
                    WHERE (expires_at IS NULL OR expires_at > NOW())
                    ORDER BY banned_at DESC
                    LIMIT %s OFFSET %s
                """, (limit, offset))
            rows = cur.fetchall()
    except Exception as e:
        logger.error("DB error (get_banned_users_list): %s", e)
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)
    return rows


def count_banned_users(platform_name=None):
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            if platform_name:
                cur.execute("""
                    SELECT COUNT(*) FROM banned_users
                    WHERE platform = %s
                      AND (expires_at IS NULL OR expires_at > NOW())
                """, (platform_name,))
            else:
                cur.execute("""
                    SELECT COUNT(*) FROM banned_users
                    WHERE (expires_at IS NULL OR expires_at > NOW())
                """)
            return cur.fetchone()[0]
    except Exception as e:
        logger.error("DB error (count_banned_users): %s", e)
        return 0
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)


# --- GDPR АУДИТ ---

def log_gdpr_action(admin_id, admin_platform, target_platform, target_user_id,
                    action, details=None):
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO gdpr_audit_log
                (admin_id, admin_platform, target_platform, target_user_id, action, details)
                VALUES (%s, %s, %s, %s, %s, %s)""",
                        (admin_id, admin_platform, target_platform,
                         target_user_id, action, SafeJson(details or {})))
        conn.commit()
    except Exception as e:
        logger.error("DB error (log_gdpr_action): %s", e)
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)


def get_gdpr_audit(limit=20):
    conn = None
    rows = []
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""SELECT admin_id, admin_platform, target_platform,
                       target_user_id, action, details, created_at
                FROM gdpr_audit_log ORDER BY created_at DESC LIMIT %s""", (limit,))
            rows = cur.fetchall()
    except Exception as e:
        logger.error("DB error (get_gdpr_audit): %s", e)
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)
    return rows


# --- GDPR-ЗАПРОСЫ ПОЛЬЗОВАТЕЛЕЙ ---

def log_gdpr_request(platform_name, user_id):
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO gdpr_requests (platform, user_id)
                VALUES (%s, %s) RETURNING id""", (platform_name, user_id))
            row = cur.fetchone()
        conn.commit()
        logger.info("gdpr_request: logged %s/%s", platform_name, user_id)
        return row[0] if row else None
    except Exception as e:
        logger.error("DB error (log_gdpr_request): %s", e)
        return None
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)


def has_pending_gdpr_request(platform_name, user_id):
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""SELECT 1 FROM gdpr_requests
                WHERE platform = %s AND user_id = %s AND fulfilled = FALSE
                LIMIT 1""", (platform_name, user_id))
            return cur.fetchone() is not None
    except Exception as e:
        logger.error("DB error (has_pending_gdpr_request): %s", e)
        return False
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)


def count_pending_gdpr_requests() -> int:
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM gdpr_requests WHERE fulfilled = FALSE")
            return cur.fetchone()[0]
    except Exception as e:
        logger.error("DB error (count_pending_gdpr_requests): %s", e)
        return 0
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)


def get_gdpr_request_by_id(request_id: int):
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT platform, user_id FROM gdpr_requests "
                "WHERE id = %s AND fulfilled = FALSE",
                (request_id,))
            return cur.fetchone()
    except Exception as e:
        logger.error("DB error (get_gdpr_request_by_id): %s", e)
        return None
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)


def list_pending_gdpr_requests(limit=100):
    conn = None
    rows = []
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, platform, user_id, requested_at
                FROM gdpr_requests
                WHERE fulfilled = FALSE
                ORDER BY requested_at ASC
                LIMIT %s
            """, (limit,))
            rows = cur.fetchall()
    except Exception as e:
        logger.error("DB error (list_pending_gdpr_requests): %s", e)
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)
    return rows


_GDPR_DELETE_ACTIONS = frozenset({"delete", "delete_sql"})
_GDPR_ANON_ACTIONS = frozenset({"anonymize"})


def get_last_gdpr_outcome(target_platform, target_user_id):
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT action, admin_id, admin_platform, created_at "
                "FROM gdpr_audit_log "
                "WHERE target_platform = %s AND target_user_id = %s "
                "ORDER BY created_at DESC LIMIT 1",
                (target_platform, target_user_id))
            row = cur.fetchone()
    except Exception as e:
        logger.error("DB error (get_last_gdpr_outcome): %s", e)
        return None
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)

    if not row:
        return None
    action, admin_id, admin_platform, created_at = row
    if action in _GDPR_DELETE_ACTIONS:
        outcome = "deleted"
    elif action in _GDPR_ANON_ACTIONS:
        outcome = "anonymized"
    else:
        logger.warning("get_last_gdpr_outcome: unknown action %r "
                       "(%s/%s)", action, target_platform, target_user_id)
        outcome = action

    return {
        "action": action,
        "outcome": outcome,
        "created_at": created_at,
        "admin_id": admin_id,
        "admin_platform": admin_platform,
    }


def get_gdpr_history_for_subject(target_platform, target_user_id, limit=50):
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 50
    limit = max(1, min(limit, 500))

    conn = None
    rows = []
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, action, admin_id, admin_platform, "
                "       details, created_at "
                "FROM gdpr_audit_log "
                "WHERE target_platform = %s AND target_user_id = %s "
                "ORDER BY created_at DESC, id DESC "
                "LIMIT %s",
                (target_platform, target_user_id, limit))
            rows = cur.fetchall()
    except Exception as e:
        logger.error("DB error (get_gdpr_history_for_subject): %s", e)
        return []
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)

    result = []
    for r in rows:
        rec_id, action, admin_id, admin_platform, details, created_at = r
        if action in _GDPR_DELETE_ACTIONS:
            outcome = "deleted"
        elif action in _GDPR_ANON_ACTIONS:
            outcome = "anonymized"
        else:
            logger.warning(
                "get_gdpr_history_for_subject: unknown action %r "
                "(id=%s, %s/%s)",
                action, rec_id, target_platform, target_user_id)
            outcome = action

        if details is None:
            details_norm = {}
        elif isinstance(details, dict):
            details_norm = details
        else:
            try:
                details_norm = json.loads(details)
            except (TypeError, ValueError):
                details_norm = {"raw": str(details)[:500]}

        result.append({
            "id": rec_id,
            "action": action,
            "outcome": outcome,
            "admin_id": admin_id,
            "admin_platform": admin_platform,
            "details": details_norm,
            "created_at": created_at,
        })
    return result


# --- OUTBOX: межпроцессный обмен сообщениями ---

_VALID_OUTBOX_TYPES = frozenset({
    "text", "attachment", "banner", "gdpr_completion", "edit",
})


def enqueue_outbox(target_platform: str, target_user_id: int,
                   text: str = "", keyboard=None,
                   attachment=None, caption: str = "",
                   message_type: str = None,
                   idempotency_key: str = None,
                   reply_to_platform_message_id: str = None,
                   edit_platform_message_id: str = None,
                   edit_of_bot_message_id: int = None,
                   bot_message_id: int = None) -> bool:
    if not target_platform or not isinstance(target_user_id, int) \
            or target_user_id <= 0:
        logger.error("enqueue_outbox: некорректные аргументы "
                     "(platform=%r, user=%r)", target_platform, target_user_id)
        return False

    if idempotency_key is not None and not isinstance(idempotency_key, str):
        logger.error("enqueue_outbox: idempotency_key не строка: %r",
                     type(idempotency_key).__name__)
        idempotency_key = None
    if idempotency_key is not None and len(idempotency_key) > 200:
        logger.error("enqueue_outbox: idempotency_key слишком длинный "
                     "(%d > 200)", len(idempotency_key))
        idempotency_key = None

    mtype = message_type or ("attachment" if attachment else "text")
    if mtype not in _VALID_OUTBOX_TYPES:
        logger.error("enqueue_outbox: неизвестный message_type=%r, "
                     "использую 'text'", mtype)
        mtype = "text"

    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO outbox_messages
                    (platform, target_user_id, message_type, text, keyboard,
                     attachment, created_at, idempotency_key,
                     reply_to_platform_message_id,
                     edit_platform_message_id,
                     edit_of_bot_message_id,
                     bot_message_id)
                VALUES (%s, %s, %s, %s, %s, %s, NOW(), %s, %s, %s, %s, %s)
                ON CONFLICT (idempotency_key)
                    WHERE idempotency_key IS NOT NULL
                DO NOTHING
                RETURNING id
            """, (
                target_platform,
                target_user_id,
                mtype,
                safe_str(text) if text else None,
                SafeJson(keyboard) if keyboard else None,
                safe_str(attachment) if attachment else None,
                idempotency_key,
                reply_to_platform_message_id,
                edit_platform_message_id,
                edit_of_bot_message_id,
                bot_message_id,
            ))
            inserted = cur.fetchone() is not None
        conn.commit()

        if inserted:
            logger.info("Outbox queued: platform=%s user=%s type=%s%s",
                        target_platform, target_user_id, mtype,
                        f" idem={idempotency_key}" if idempotency_key else "")
        else:
            logger.info(
                "Outbox idempotent skip: platform=%s user=%s type=%s idem=%s",
                target_platform, target_user_id, mtype, idempotency_key)
        return True
    except Exception as e:
        logger.error("DB error (enqueue_outbox): %s", e)
        return False
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)


def fetch_outbox_batch(platform_name: str, limit: int = 10):
    conn = None
    rows = []
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE outbox_messages
                SET status = 'pending',
                    claimed_at = NULL,
                    error = COALESCE(error, 'reset: worker died')
                WHERE platform = %s
                  AND status = 'in_progress'
                  AND claimed_at < NOW() - INTERVAL '5 minutes'
                  AND attempts < 5
            """, (platform_name,))
            cur.execute("""
                UPDATE outbox_messages
                SET status = 'failed',
                    error = COALESCE(error, 'expired (>24h in queue)')
                WHERE platform = %s AND status = 'pending'
                  AND created_at <= NOW() - INTERVAL '24 hours'
            """, (platform_name,))
            cur.execute("""
                UPDATE outbox_messages SET status = 'in_progress',
                       claimed_at = NOW(), attempts = attempts + 1
                WHERE id IN (
                    SELECT id FROM outbox_messages
                    WHERE platform = %s AND status = 'pending' AND attempts < 5
                      AND created_at > NOW() - INTERVAL '24 hours'
                    ORDER BY created_at ASC
                    LIMIT %s
                    FOR UPDATE SKIP LOCKED
                )
                RETURNING id, target_user_id, message_type, text, keyboard,
                          attachment,
                          reply_to_platform_message_id,
                          edit_platform_message_id,
                          edit_of_bot_message_id,
                          bot_message_id
            """, (platform_name, limit))
            raw = cur.fetchall()
            rows = [
                {
                    "id": r[0],
                    "target_user_id": r[1],
                    "message_type": r[2],
                    "text": r[3] or "",
                    "keyboard": r[4],
                    "attachment": r[5],
                    "reply_to_platform_message_id": r[6],
                    "edit_platform_message_id": r[7],
                    "edit_of_bot_message_id": r[8],
                    "bot_message_id": r[9],
                }
                for r in raw
            ]
        conn.commit()
    except Exception as e:
        logger.error("DB error (fetch_outbox_batch): %s", e)
        return []
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)
    return rows


def mark_outbox_error(message_id: int, error_text: str) -> None:
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE outbox_messages SET error = %s, "
                "status = CASE WHEN attempts < 5 THEN 'pending' ELSE 'failed' END "
                "WHERE id = %s",
                (safe_str(error_text)[:500], message_id))
        conn.commit()
    except Exception as e:
        logger.error("DB error (mark_outbox_error): %s", e)
    finally:
        return_db_connection(conn)


def mark_outbox_skipped(message_id: int, reason: str) -> bool:
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE outbox_messages "
                "SET status = 'failed', error = %s, claimed_at = NULL "
                "WHERE id = %s",
                (safe_str(reason)[:500], message_id))
        conn.commit()
        return True
    except Exception as e:
        logger.error("DB error (mark_outbox_skipped): %s", e)
        return False
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)


def mark_outbox_sent(message_id: int) -> bool:
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE outbox_messages "
                "SET status = 'sent', sent_at = NOW(), error = NULL "
                "WHERE id = %s "
                "RETURNING message_type, platform, target_user_id",
                (message_id,))
            row = cur.fetchone()
            if row and row[0] == "gdpr_completion":
                _, pl, uid = row
                cur.execute("""
                    INSERT INTO gdpr_completion_notified
                        (platform, user_id, notified_at)
                    VALUES (%s, %s, NOW())
                    ON CONFLICT (platform, user_id) DO NOTHING
                """, (pl, uid))
        conn.commit()
        return True
    except Exception as e:
        logger.error("DB error (mark_outbox_sent): %s", e)
        return False
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)


def mark_bot_message_platform_id(bot_message_id: int,
                                 platform_message_id: str) -> bool:
    """Сохраняет platform_message_id после успешной отправки.

    Вызывается из core.workers.outbox_worker после того, как адаптер
    вернул id сообщения от платформы.
    """
    if not bot_message_id or not platform_message_id:
        return False
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE bot_messages "
                "SET platform_message_id = %s "
                "WHERE id = %s AND platform_message_id IS NULL",
                (safe_str(platform_message_id), bot_message_id))
        conn.commit()
        return True
    except Exception as e:
        logger.error("DB error (mark_bot_message_platform_id): %s", e)
        return False
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)


def mark_bot_message_edited(bot_message_id: int, new_text: str) -> bool:
    """Обновляет текст bot_messages после успешного редактирования.

    Вызывается из core.workers.outbox_worker после adapter.edit_message().
    """
    if not bot_message_id:
        return False
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE bot_messages "
                "SET text = %s, edited_at = NOW() "
                "WHERE id = %s",
                (safe_str(new_text), bot_message_id))
        conn.commit()
        return True
    except Exception as e:
        logger.error("DB error (mark_bot_message_edited): %s", e)
        return False
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)


def fetch_pending_full_relay(limit=5):
    conn = None
    rows = []
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, source_file_id, url, user_platform, user_id,
                       application_id, kind
                FROM media_files
                WHERE full_relay_requested_at IS NOT NULL
                  AND full_relay_confirmed = FALSE
                  AND source_file_id IS NOT NULL
                  AND full_relay_attempts < 5
                  AND (full_relay_last_error IS NULL
                       OR full_relay_last_attempt_at IS NULL
                       OR full_relay_last_attempt_at
                          < NOW() - INTERVAL '5 minutes')
                ORDER BY full_relay_requested_at
                LIMIT %s
                FOR UPDATE SKIP LOCKED
            """, (limit,))
            for r in cur.fetchall():
                rows.append({
                    "id": r[0], "source_file_id": r[1], "preview_url": r[2],
                    "user_platform": r[3], "user_id": r[4],
                    "application_id": r[5], "kind": r[6],
                })
            ids = [r["id"] for r in rows]
            if ids:
                cur.execute(
                    "UPDATE media_files SET full_relay_attempts = full_relay_attempts + 1, "
                    "full_relay_last_attempt_at = NOW() WHERE id = ANY(%s)", (ids,))
        conn.commit()
    except Exception as e:
        logger.error("DB error (fetch_pending_full_relay): %s", e)
        return []
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)
    return rows


def mark_full_relay_success(media_id, full_url, owner_id=None, photo_id=None):
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT url, kind FROM media_files WHERE id = %s",
                        (media_id,))
            row = cur.fetchone()
            if not row:
                return False
            preview_url, kind = row

            cur.execute("""
                UPDATE media_files
                SET full_relay_confirmed = TRUE,
                    full_relay_url = %s,
                    full_relay_owner_id = %s,
                    full_relay_photo_id = %s,
                    full_relay_last_error = NULL
                WHERE id = %s
            """, (full_url, owner_id, photo_id, media_id))

            if kind in ("photo",):
                cur.execute("""
                    UPDATE bright_city_applications
                    SET child_photo_urls = (
                        SELECT jsonb_agg(
                            CASE WHEN elem::text = to_jsonb(%s::text)::text
                                 THEN to_jsonb(%s::text)
                                 ELSE elem END
                        )
                        FROM jsonb_array_elements(child_photo_urls) elem
                    )
                    WHERE child_photo_urls @> to_jsonb(ARRAY[%s::text])
                """, (preview_url, full_url, preview_url))

                cur.execute("""
                    UPDATE bright_city_applications
                    SET drawings_urls = (
                        SELECT jsonb_agg(
                            CASE WHEN elem::text = to_jsonb(%s::text)::text
                                 THEN to_jsonb(%s::text)
                                 ELSE elem END
                        )
                        FROM jsonb_array_elements(drawings_urls) elem
                    )
                    WHERE drawings_urls @> to_jsonb(ARRAY[%s::text])
                """, (preview_url, full_url, preview_url))
        conn.commit()
        return True
    except Exception as e:
        logger.error("DB error (mark_full_relay_success): %s", e)
        return False
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)


def mark_full_relay_error(media_id, error_text):
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE media_files
                SET full_relay_last_error = %s
                WHERE id = %s
            """, (str(error_text)[:500], media_id))
        conn.commit()
        return True
    except Exception as e:
        logger.error("DB error (mark_full_relay_error): %s", e)
        return False
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)


def request_full_relay_for_user(user_platform, user_id, application_id=None):
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            if application_id is not None:
                cur.execute("""
                    UPDATE media_files
                    SET full_relay_requested_at = NOW(),
                        full_relay_attempts = 0,
                        full_relay_last_error = NULL
                    WHERE user_platform = %s AND user_id = %s
                      AND application_id = %s
                      AND source_file_id IS NOT NULL
                      AND full_relay_confirmed = FALSE
                """, (user_platform, user_id, application_id))
            else:
                cur.execute("""
                    UPDATE media_files
                    SET full_relay_requested_at = NOW(),
                        full_relay_attempts = 0,
                        full_relay_last_error = NULL
                    WHERE user_platform = %s AND user_id = %s
                      AND source_file_id IS NOT NULL
                      AND full_relay_confirmed = FALSE
                """, (user_platform, user_id))
            affected = cur.rowcount
        conn.commit()
        return affected
    except Exception as e:
        logger.error("DB error (request_full_relay_for_user): %s", e)
        return 0
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)


# --- КРОСС-ПРОЦЕССНЫЙ СБРОС КЭШЕЙ ---

_CACHE_INVALIDATE_CHANNEL = "bot_cache_invalidate"
_VALID_PLATFORMS = frozenset({"vk", "tg", "max"})
_VALID_CACHE_KINDS = frozenset({"admin", "ban", "gdpr"})


def notify_cache_invalidate(kind: str, user_id: int,
                            platform_name: str = None) -> None:
    if kind not in _VALID_CACHE_KINDS:
        logger.warning("notify_cache_invalidate: bad kind %r", kind)
        return
    if not isinstance(user_id, int) or user_id <= 0:
        logger.warning("notify_cache_invalidate: bad user_id %r", user_id)
        return

    if platform_name is None:
        pl = _runtime_platform()
        if pl == "?":
            logger.error(
                "notify_cache_invalidate: runtime platform не инициализирован "
                "(kind=%s, user_id=%s) — payload НЕ отправлен",
                kind, user_id)
            return
    else:
        pl = platform_name

    if pl not in _VALID_PLATFORMS:
        logger.warning(
            "notify_cache_invalidate: bad platform %r "
            "(kind=%s, user_id=%s) — payload уйдёт без платформы",
            pl, kind, user_id)
        pl = ""

    payload = f"{kind}:{pl}:{user_id}"

    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_notify(%s, %s)",
                (_CACHE_INVALIDATE_CHANNEL, payload)
            )
        conn.commit()
    except Exception as e:
        logger.warning("notify_cache_invalidate failed (%s): %s", payload, e)
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)