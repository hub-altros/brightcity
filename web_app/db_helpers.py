"""Хелперы БД для веб-админки. Отдельный модуль, чтобы не трогать core.db."""
import logging
import secrets

from core.db import get_db_connection, return_db_connection

logger = logging.getLogger("web.db")


# ============================================================
#  СХЕМА
# ============================================================

def ensure_web_schema():
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SET LOCAL lock_timeout = '30s'")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS web_admins (
                    id              BIGSERIAL PRIMARY KEY,
                    login           TEXT UNIQUE NOT NULL,
                    password_hash   TEXT NOT NULL,
                    display_name    TEXT,
                    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
                    totp_secret     TEXT,
                    totp_confirmed_at TIMESTAMP,
                    totp_last_used_step BIGINT,
                    bot_platform    TEXT,
                    bot_user_id     BIGINT,
                    created_at      TIMESTAMP NOT NULL DEFAULT NOW(),
                    last_login_at   TIMESTAMP
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS web_sessions (
                    id          TEXT PRIMARY KEY,
                    admin_id    BIGINT NOT NULL
                                REFERENCES web_admins(id) ON DELETE CASCADE,
                    created_at  TIMESTAMP NOT NULL DEFAULT NOW(),
                    expires_at  TIMESTAMP NOT NULL,
                    user_agent  TEXT,
                    ip          TEXT
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_web_sessions_admin
                ON web_sessions(admin_id)
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_web_sessions_expires
                ON web_sessions(expires_at)
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS web_recovery_codes (
                    id          BIGSERIAL PRIMARY KEY,
                    admin_id    BIGINT NOT NULL
                                REFERENCES web_admins(id) ON DELETE CASCADE,
                    code_hash   TEXT NOT NULL,
                    used_at     TIMESTAMP,
                    created_at  TIMESTAMP NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_web_recovery_admin
                ON web_recovery_codes(admin_id)
                WHERE used_at IS NULL
            """)
        conn.commit()
        logger.info("web schema ensured")
    except Exception as e:
        logger.exception("ensure_web_schema failed: %s", e)
        raise
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)


# ============================================================
#  ВЕБ-АДМИНЫ
# ============================================================

def get_web_admin_by_login(login: str):
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, login, password_hash, display_name, is_active,
                       totp_secret, totp_confirmed_at, bot_platform, bot_user_id
                FROM web_admins WHERE login = %s
            """, (login,))
            return cur.fetchone()
    except Exception as e:
        logger.error("get_web_admin_by_login failed: %s", e)
        return None
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)


def get_web_admin_by_id(admin_id: int):
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, login, password_hash, display_name, is_active,
                       totp_secret, totp_confirmed_at, bot_platform, bot_user_id
                FROM web_admins WHERE id = %s
            """, (admin_id,))
            return cur.fetchone()
    except Exception as e:
        logger.error("get_web_admin_by_id failed: %s", e)
        return None
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)


def list_web_admins():
    conn = None
    rows = []
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, login, display_name, is_active,
                       totp_confirmed_at, bot_platform, bot_user_id,
                       last_login_at
                FROM web_admins ORDER BY id
            """)
            rows = cur.fetchall()
    except Exception as e:
        logger.error("list_web_admins failed: %s", e)
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)
    return rows


def create_web_admin(login: str, password: str, display_name: str = None,
                     bot_platform: str = None,
                     bot_user_id: int = None) -> int | None:
    from web_app.auth import hash_password
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO web_admins
                    (login, password_hash, display_name, bot_platform, bot_user_id)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
            """, (login, hash_password(password), display_name,
                  bot_platform, bot_user_id))
            row = cur.fetchone()
        conn.commit()
        return row[0] if row else None
    except Exception as e:
        logger.error("create_web_admin failed: %s", e)
        return None
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)


def set_web_admin_password(login: str, new_password: str) -> bool:
    from web_app.auth import hash_password
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE web_admins SET password_hash = %s WHERE login = %s "
                "RETURNING id",
                (hash_password(new_password), login))
            row = cur.fetchone()
        conn.commit()
        return row is not None
    except Exception as e:
        logger.error("set_web_admin_password failed: %s", e)
        return False
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)


def delete_web_admin(login: str) -> bool:
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM web_admins WHERE login = %s RETURNING id",
                (login,))
            row = cur.fetchone()
        conn.commit()
        return row is not None
    except Exception as e:
        logger.error("delete_web_admin failed: %s", e)
        return False
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)


# ============================================================
#  2FA
# ============================================================

def update_totp_secret(admin_id: int, secret: str):
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE web_admins SET totp_secret = %s "
                "WHERE id = %s AND totp_confirmed_at IS NULL",
                (secret, admin_id))
        conn.commit()
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)


def confirm_totp(admin_id: int):
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE web_admins SET totp_confirmed_at = NOW() WHERE id = %s",
                (admin_id,))
        conn.commit()
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)


def set_totp_last_step(admin_id: int, step: int):
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE web_admins SET totp_last_used_step = %s WHERE id = %s",
                (step, admin_id))
        conn.commit()
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)


def get_totp_last_step(admin_id: int) -> int | None:
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT totp_last_used_step FROM web_admins WHERE id = %s",
                (admin_id,))
            r = cur.fetchone()
            return r[0] if r else None
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)


def reset_totp(login: str) -> bool:
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE web_admins
                SET totp_secret = NULL,
                    totp_confirmed_at = NULL,
                    totp_last_used_step = NULL
                WHERE login = %s RETURNING id
            """, (login,))
            r = cur.fetchone()
        conn.commit()
        return r is not None
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)


# ============================================================
#  RECOVERY-КОДЫ
# ============================================================

def add_recovery_codes(admin_id: int, hashes: list[str]):
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            for h in hashes:
                cur.execute(
                    "INSERT INTO web_recovery_codes (admin_id, code_hash) "
                    "VALUES (%s, %s)", (admin_id, h))
        conn.commit()
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)


def consume_recovery_code(admin_id: int, code_hash: str) -> bool:
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE web_recovery_codes SET used_at = NOW() "
                "WHERE admin_id = %s AND code_hash = %s AND used_at IS NULL "
                "RETURNING id", (admin_id, code_hash))
            ok = cur.fetchone() is not None
        conn.commit()
        return ok
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)


def touch_last_login(admin_id: int):
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE web_admins SET last_login_at = NOW() WHERE id = %s",
                (admin_id,))
        conn.commit()
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)


# ============================================================
#  СЕССИИ
# ============================================================

def create_session(admin_id: int, ttl_seconds: int = 30 * 24 * 3600,
                   user_agent: str = "", ip: str = "") -> str:
    session_id = secrets.token_urlsafe(32)
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO web_sessions
                    (id, admin_id, expires_at, user_agent, ip)
                VALUES (%s, %s, NOW() + make_interval(secs => %s), %s, %s)
            """, (session_id, admin_id, ttl_seconds, user_agent[:300], ip[:64]))
        conn.commit()
        return session_id
    except Exception as e:
        logger.error("create_session failed: %s", e)
        return ""
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)


def get_session(session_id: str):
    if not session_id:
        return None
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT admin_id FROM web_sessions
                WHERE id = %s AND expires_at > NOW()
            """, (session_id,))
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)


def delete_session(session_id: str):
    if not session_id:
        return
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM web_sessions WHERE id = %s", (session_id,))
        conn.commit()
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)


# ============================================================
#  БАННЕР
# ============================================================

def create_banner_media_file(app_id: int, target_platform: str,
                             target_user_id: int,
                             file_bytes: bytes) -> tuple[int, str] | None:
    """Сохраняет макет баннера и регистрирует его в media_files."""
    import io as _io
    import uuid

    from core.db import get_db_connection as _gdbc, return_db_connection as _rdbc
    from core.limits import MAX_BANNER_SIZE

    try:
        from PIL import Image
        Image.MAX_IMAGE_PIXELS = 40_000_000
        img = Image.open(_io.BytesIO(file_bytes))
        img.verify()
        img = Image.open(_io.BytesIO(file_bytes))
        if img.mode != "RGB":
            img = img.convert("RGB")
        out = _io.BytesIO()
        img.save(out, format="JPEG", quality=92)
        file_bytes = out.getvalue()
    except Exception as e:
        logger.warning("banner reencode failed: %s", e)
        return None

    if not file_bytes or len(file_bytes) > MAX_BANNER_SIZE:
        logger.warning("banner too big after reencode: %s байт",
                       len(file_bytes) if file_bytes else 0)
        return None

    media_id = None
    conn = None
    try:
        conn = _gdbc()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO media_files
                    (url, kind, user_platform, user_id, application_id)
                VALUES (%s, 'banner', %s, %s, %s)
                RETURNING id
            """, (f"web://banner/{uuid.uuid4().hex}",
                  target_platform, target_user_id, app_id))
            row = cur.fetchone()
            media_id = row[0] if row else None
        conn.commit()
    except Exception as e:
        logger.error("create_banner_media_file INSERT failed: %s", e)
        return None
    finally:
        if conn:
            conn.rollback()
            _rdbc(conn)

    if not media_id:
        return None

    try:
        from web_app.media import save_media_bytes_to_cache
        save_media_bytes_to_cache(media_id, file_bytes)
    except Exception as e:
        logger.error("save banner to cache failed: %s", e)
        return None

    from web_app.media import sign_media_url
    public_url = sign_media_url(media_id)
    return media_id, public_url