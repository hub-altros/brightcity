"""Общая бизнес-логика для заявок.

Используется и в HTML-роутах (web_app/routes/applications.py),
и в JSON API (web_app/api/applications.py), чтобы код не расходился.
"""
import json
import logging
from typing import Optional

from core.db import get_db_connection, return_db_connection
from core.notify import enqueue_outbox, notify_admin_applications

logger = logging.getLogger("web.services.applications")


def first_photo_media_id(app_id: int, photos_raw) -> Optional[int]:
    """Возвращает id первого медиафайла (фото) для заявки.

    Bug 4: у MAX media_files.application_id может оставаться NULL —
        register_media_file вызывался без application_id до INSERT
        заявки. Поиск по url БЕЗ жёсткого WHERE application_id = %s
        работает и для TG, и для MAX.
    """
    if not app_id:
        return None

    first_url = None
    if photos_raw:
        try:
            urls = (json.loads(photos_raw)
                    if isinstance(photos_raw, str)
                    else list(photos_raw))
            if urls and isinstance(urls[0], str) and urls[0]:
                first_url = urls[0]
        except Exception:
            pass

    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            if first_url:
                cur.execute(
                    "SELECT id FROM media_files "
                    "WHERE url = %s "
                    "ORDER BY (application_id = %s) DESC NULLS LAST, "
                    "         id ASC LIMIT 1",
                    (first_url, app_id))
                r = cur.fetchone()
                if r:
                    return r[0]

            cur.execute(
                "SELECT id FROM media_files "
                "WHERE application_id = %s "
                "  AND kind NOT IN ('doc', 'payment_doc', 'org_info') "
                "ORDER BY id ASC LIMIT 1",
                (app_id,))
            r = cur.fetchone()
            return r[0] if r else None
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)


def web_set_in_progress(admin_id: int, admin_name: str, app_id: int):
    """Переводит заявку в in_progress из веб-админки.

    Уведомляет пользователя через outbox (кросс-платформенно)
    и админов через notify_admin_applications.
    """
    conn = None
    target_user_id = None
    target_platform = None
    old_status = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, user_id, platform "
                "FROM bright_city_applications WHERE id = %s FOR UPDATE",
                (app_id,))
            row = cur.fetchone()
            if not row:
                return
            old_status, target_user_id, target_platform = row

            if old_status == "in_progress":
                conn.rollback()
                return
            if old_status not in ("new", "rejected"):
                conn.rollback()
                logger.warning(
                    "web_set_in_progress: %s → in_progress запрещён "
                    "(app=%s)", old_status, app_id)
                return

            cur.execute(
                """UPDATE bright_city_applications
                      SET status = 'in_progress',
                          rejection_reason = NULL,
                          assigned_admin_id = %s,
                          assigned_admin_name = %s,
                          opened_by_admin_id = NULL,
                          opened_by_admin_name = NULL,
                          opened_at = NULL
                    WHERE id = %s""",
                (admin_id, admin_name, app_id))
            cur.execute(
                """INSERT INTO application_status_history
                       (application_id, old_status, new_status,
                        admin_id, admin_name, admin_platform, changed_at)
                   VALUES (%s, %s, 'in_progress', %s, %s, 'web', NOW())""",
                (app_id, old_status, admin_id, admin_name))
            cur.execute(
                "UPDATE bright_city_applications "
                "SET status_changed_by_name = %s, "
                "    status_changed_at = NOW() "
                "WHERE id = %s",
                (admin_name, app_id))
        conn.commit()
    except Exception as e:
        logger.error("web_set_in_progress DB error: %s", e)
        return
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)

    if target_user_id and target_platform:
        try:
            enqueue_outbox(
                target_platform=target_platform,
                target_user_id=target_user_id,
                text=("Ваша заявка принята в обработку. "
                      "Мы проверяем данные и свяжемся с вами, "
                      "если что-то понадобится."),
                message_type="text",
            )
        except Exception as e:
            logger.warning(
                "web_set_in_progress: notify user failed (app=%s): %s",
                app_id, e)

    try:
        notify_admin_applications(
            f"✅ Заявка #{app_id} → В работе (админ: {admin_name}, web)")
    except Exception as e:
        logger.warning(
            "web_set_in_progress: admin notify failed (app=%s): %s",
            app_id, e)


# Алиасы для обратной совместимости, если где-то остались
# старые вызовы с подчёркиванием. Можно удалить после полной чистки.
_first_photo_media_id = first_photo_media_id