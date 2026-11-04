# core/gdpr.py
"""GDPR: удаление/анонимизация ПДн пользователя по 152-ФЗ.

ФИКС Bug-3: при delete/anonymize теперь чистится таблица
gdpr_completion_notified.

ФИКС Баг-16: _mark_media_for_background_delete возвращает (marked, error).
Раньше ошибка БД при пометке медиафайлов на удаление была неотличима
от «медиафайлов нет» — админ видел media_files_marked: 0 и не знал,
что фото/квитанции остались на VK CDN.

ФИКС Баг-15: delete_user_data / anonymize_user_data принимают
existing_conn. Нужно для транзакционной блокировки: core/admin._exec_gdpr
берёт SELECT ... FOR UPDATE по gdpr_requests на своём соединении и
передаёт это же соединение сюда, чтобы lock не снимался между
проверкой pending-запроса и фактическим удалением ПДн. Иначе два
админа могут параллельно взять один и тот же request_id, и один из
них запишет в аудит ссылку на уже несуществующую строку.

ФИКС 152-ФЗ (media_cache): при delete/anonymize удаляются физические
файлы из web_app/media_cache/. Раньше оставались «сироты» — фото
детей и квитанции на диске после удаления пользователя из БД.
"""
import asyncio
import logging

from core.db import (
    get_db_connection, return_db_connection,
    get_media_files_for_user, mark_media_files_deleted_by_user,
    log_gdpr_action,
    enqueue_outbox,
)
from core.media import delete_vk_photo
from core.storage import user_state
from core.runtime import platform as current_platform, get_adapter, get_event_loop_ref

logger = logging.getLogger("bright_city_bot")


def _collect_application_ids(cur, platform_name, user_id):
    cur.execute(
        "SELECT id FROM bright_city_applications "
        "WHERE platform = %s AND user_id = %s",
        (platform_name, user_id))
    return [r[0] for r in cur.fetchall()]


def _notify_user_of_deletion(target_platform, target_user_id):
    """Уведомляет пользователя об удалении его ПДн."""
    msg = ("Ваш запрос на удаление персональных данных принят и выполняется. "
           "Мы сообщим, когда удаление будет полностью завершено.")
    pl = current_platform()

    if target_platform == pl:
        try:
            adapter = get_adapter()
            loop = get_event_loop_ref()
            if loop is not None and not loop.is_closed():
                fut = asyncio.run_coroutine_threadsafe(
                    adapter.send_message(target_user_id, msg), loop)
                fut.result(timeout=15)
            else:
                asyncio.run(adapter.send_message(target_user_id, msg))
            logger.info("notify_user: sent to %s/%s",
                        target_platform, target_user_id)
            return True
        except Exception as e:
            logger.warning("notify_user: direct send failed: %s", e)
            return False

    try:
        ok = enqueue_outbox(
            target_platform=target_platform,
            target_user_id=target_user_id,
            text=msg,
        )
        if ok:
            logger.info("notify_user: queued to outbox (%s → %s, user=%s)",
                        pl, target_platform, target_user_id)
        else:
            logger.error("notify_user: enqueue_outbox failed (%s → %s, user=%s)",
                         pl, target_platform, target_user_id)
        return ok
    except Exception as e:
        logger.exception(
            "notify_user: cross-platform enqueue failed (%s → %s, user=%s): %s",
            pl, target_platform, target_user_id, e)
        return False


def _mark_media_for_background_delete(platform_name, user_id):
    """Помечает медиафайлы подлежащими удалению (deleted_at = NOW()).

    ФИКС Баг-16: возвращает (marked, error). error = None при успехе.
    """
    return mark_media_files_deleted_by_user(platform_name, user_id)


def _finalize_gdpr_media(admin_id, platform_name, user_id, action, result):
    """Помечает медиа на удаление + оповещает админа при ошибке.

    ФИКС Баг-16: вынесено из delete_user_data / anonymize_user_data,
    чтобы не дублировать блок. Вызывается ПОСЛЕ успешного commit()
    основной транзакции.

    action — 'delete' | 'anonymize' для логов.
    """
    marked, mark_err = _mark_media_for_background_delete(
        platform_name, user_id)
    result["media_files_marked"] = marked
    if mark_err:
        result["media_files_mark_error"] = mark_err
        logger.error(
            "GDPR %s(%s,%s): media_files НЕ помечены на удаление: %s",
            action, platform_name, user_id, mark_err)
        if admin_id is not None:
            try:
                from core.notify import send_message as _sm
                _sm(admin_id,
                    f"⚠ Медиафайлы {platform_name}/{user_id} НЕ помечены "
                    f"на удаление с CDN.\n\n"
                    f"ПДн в БД удалены, но фото/квитанции могли остаться "
                    f"на VK CDN. Требуется ручная проверка очереди "
                    f"media_files (deleted_at IS NULL).\n\n"
                    f"Ошибка: {mark_err}")
            except Exception as notify_err:
                logger.error(
                    "GDPR %s: не удалось уведомить админа %s об ошибке "
                    "mark media: %s", action, admin_id, notify_err)


def _cleanup_media_cache_files(platform_name: str, user_id: int) -> int:
    """Удаляет физические файлы из web_app/media_cache для медиа пользователя.

    Вызывается в delete_user_data и anonymize_user_data. Файлы в кэше
    — это фотографии детей и квитанции (ПДн). По 152-ФЗ при удалении
    или анонимизации пользователя они должны быть уничтожены вместе
    с записями в media_files.

    Возвращает количество удалённых файлов.

    ФИКС: раньше здесь стояло `_cleanup_media_cache_files(tp, tuid)` —
    таких переменных в функции нет (параметры platform_name/user_id),
    это давало NameError, пойманный except'ом выше. Файлы оставались
    на диске, то есть 152-ФЗ формально нарушался.
    """
    conn = None
    media_ids = []
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM media_files "
                "WHERE user_platform = %s AND user_id = %s",
                (platform_name, user_id))
            media_ids = [r[0] for r in cur.fetchall()]
    except Exception as e:
        logger.warning(
            "gdpr: _cleanup_media_cache_files query failed "
            "(platform=%s uid=%s): %s", platform_name, user_id, e)
        return 0
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)

    if not media_ids:
        return 0

    try:
        from core.media import delete_media_cache
    except Exception as e:
        logger.warning("gdpr: delete_media_cache import failed: %s", e)
        return 0

    deleted = 0
    for mid in media_ids:
        if delete_media_cache(mid):
            deleted += 1
    logger.info(
        "gdpr: deleted %d/%d media_cache files for %s/%s",
        deleted, len(media_ids), platform_name, user_id)
    return deleted


def delete_user_data(platform_name, user_id, admin_id=None, admin_platform=None,
                     notify_user=False, existing_conn=None):
    """Полное удаление ПДн субъекта.

    existing_conn — если задан, все операции выполняются на этом
    соединении. Соединение НЕ возвращается в пул (владелец вернёт сам).
    commit() делается здесь — это освобождает блокировку, взятую
    вызывающим (например, SELECT ... FOR UPDATE по gdpr_requests).

    ФИКС Баг-15.
    """
    result = {"applications": 0, "status_history": 0, "bot_messages": 0,
              "bot_user_states": 0, "banned_users": 0,
              "admin_users": 0, "admin_settings": 0, "admin_sudo_sessions": 0,
              "media_files_marked": 0,
              "outbox_messages": 0, "gdpr_requests": 0,
              "gdpr_completion_notified": 0,
              "media_files_removed": 0,
              "media_cache_files_removed": 0}

    own_conn = existing_conn is None
    conn = None
    try:
        conn = get_db_connection() if own_conn else existing_conn

        with conn.cursor() as cur:
            app_ids = _collect_application_ids(cur, platform_name, user_id)
            if app_ids:
                cur.execute(
                    "DELETE FROM application_status_history "
                    "WHERE application_id = ANY(%s)", (app_ids,))
                result["status_history"] = cur.rowcount
            cur.execute(
                "DELETE FROM bright_city_applications "
                "WHERE platform = %s AND user_id = %s",
                (platform_name, user_id))
            result["applications"] = cur.rowcount
            cur.execute(
                "DELETE FROM bot_messages "
                "WHERE platform = %s AND user_id = %s",
                (platform_name, user_id))
            result["bot_messages"] = cur.rowcount
            cur.execute(
                "DELETE FROM bot_user_states "
                "WHERE platform = %s AND user_id = %s",
                (platform_name, user_id))
            result["bot_user_states"] = cur.rowcount
            cur.execute(
                "DELETE FROM banned_users "
                "WHERE platform = %s AND user_id = %s",
                (platform_name, user_id))
            result["banned_users"] = cur.rowcount
            cur.execute(
                "DELETE FROM admin_users "
                "WHERE platform = %s AND admin_id = %s",
                (platform_name, user_id))
            result["admin_users"] = cur.rowcount
            cur.execute(
                "DELETE FROM admin_settings "
                "WHERE platform = %s AND admin_id = %s",
                (platform_name, user_id))
            result["admin_settings"] = cur.rowcount
            cur.execute(
                "DELETE FROM admin_sudo_sessions "
                "WHERE platform = %s AND admin_id = %s",
                (platform_name, user_id))
            result["admin_sudo_sessions"] = cur.rowcount
            cur.execute(
                "DELETE FROM outbox_messages "
                "WHERE platform = %s AND target_user_id = %s",
                (platform_name, user_id))
            result["outbox_messages"] = cur.rowcount
            cur.execute(
                "DELETE FROM gdpr_requests "
                "WHERE platform = %s AND user_id = %s",
                (platform_name, user_id))
            result["gdpr_requests"] = cur.rowcount

            cur.execute(
                "DELETE FROM gdpr_completion_notified "
                "WHERE platform = %s AND user_id = %s",
                (platform_name, user_id))
            result["gdpr_completion_notified"] = cur.rowcount

            cur.execute("""DELETE FROM media_files
                           WHERE user_platform = %s AND user_id = %s
                             AND vk_delete_confirmed = TRUE""",
                        (platform_name, user_id))
            result["media_files_removed"] = cur.rowcount
            cur.execute("""
                INSERT INTO gdpr_erasure_log
                    (platform, user_id, erased_at, admin_id)
                VALUES (%s, %s, NOW(), %s)
                ON CONFLICT (platform, user_id) DO UPDATE SET erased_at = NOW()
            """, (platform_name, user_id, admin_id))

        # Файлы в кэше — это ПДн. Удаляем до commit: если транзакция
        # откатится, потеряем файлы (не критично — пользователь
        # перезалит при следующей заявке), но не оставим ПДн на диске.
        # ФИКС: параметры platform_name/user_id, а не tp/tuid.
        try:
            removed = _cleanup_media_cache_files(platform_name, user_id)
            result["media_cache_files_removed"] = removed
        except Exception as e:
            logger.warning(
                "delete_user_data: media_cache cleanup failed "
                "(platform=%s uid=%s): %s", platform_name, user_id, e)

        # commit освобождает lock, взятый вызывающим (FOR UPDATE).
        conn.commit()
    except Exception as e:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        logger.exception("DB error (delete_user_data): %s", e)
        return None
    finally:
        # В пул возвращаем ТОЛЬКО собственное соединение. existing_conn
        # возвращает владелец (core/admin._exec_gdpr).
        if own_conn and conn:
            return_db_connection(conn)

    # ------------------------------------------------------------------
    # Всё ниже — независимые операции на своих соединениях.
    # ------------------------------------------------------------------

    # ФИКС Баг-16: через helper, с обработкой ошибки и алертом админу.
    _finalize_gdpr_media(admin_id, platform_name, user_id,
                         action="delete", result=result)

    # ФИКС Баг-3: pg_notify, чтобы чужой процесс сбросил кэш
    # is_user_erased / UserState.
    try:
        from core.db import notify_cache_invalidate
        notify_cache_invalidate(
            "gdpr", user_id, platform_name=platform_name)
    except Exception as e:
        logger.warning(
            "notify gdpr cache invalidate failed (%s/%s): %s",
            platform_name, user_id, e)

    try:
        if platform_name == current_platform():
            user_state.invalidate(user_id, platform_name)
    except Exception as e:
        logger.warning("invalidate state cache failed (%s/%s): %s",
                       platform_name, user_id, e)

    if admin_id is not None:
        log_gdpr_action(admin_id, admin_platform or "?",
                        platform_name, user_id, "delete", result)

    if notify_user:
        _notify_user_of_deletion(platform_name, user_id)
    logger.info("GDPR delete(%s,%s): %s", platform_name, user_id, result)
    return result


def anonymize_user_data(platform_name, user_id, admin_id=None, admin_platform=None,
                        notify_user=False, existing_conn=None):
    """Анонимизация: ПДн затираются, но заявки/аудит остаются.

    existing_conn — см. delete_user_data. ФИКС Баг-15.
    """
    result = {"applications_anonymized": 0, "bot_messages_anonymized": 0,
              "bot_user_states": 0, "banned_users": 0,
              "admin_users": 0, "admin_settings": 0, "admin_sudo_sessions": 0,
              "media_files_marked": 0, "outbox_messages": 0,
              "gdpr_requests": 0,
              "gdpr_completion_notified": 0,
              "media_cache_files_removed": 0}

    own_conn = existing_conn is None
    conn = None
    try:
        conn = get_db_connection() if own_conn else existing_conn

        with conn.cursor() as cur:
            cur.execute("""
                UPDATE bright_city_applications
                SET name_age = '[УДАЛЕНО]', phone = '[УДАЛЕНО]',
                    training_info = '[УДАЛЕНО]',
                    child_photo_urls = '[]'::jsonb,
                    drawings_urls = '[]'::jsonb,
                    payment_doc_urls = '[]'::jsonb,
                    org_info_attachments = '[]'::jsonb,
                    banner_url = NULL,
                    location = NULL,
                    placement_location = NULL,
                    rejection_reason = NULL,
                    user_id = NULL,
                    anonymized_at = NOW()
                WHERE platform = %s AND user_id = %s
            """, (platform_name, user_id))
            result["applications_anonymized"] = cur.rowcount
            cur.execute(
                "UPDATE bot_messages SET text = '[УДАЛЕНО]' "
                "WHERE platform = %s AND user_id = %s",
                (platform_name, user_id))
            result["bot_messages_anonymized"] = cur.rowcount
            cur.execute(
                "DELETE FROM bot_user_states "
                "WHERE platform = %s AND user_id = %s",
                (platform_name, user_id))
            result["bot_user_states"] = cur.rowcount
            cur.execute(
                "DELETE FROM banned_users "
                "WHERE platform = %s AND user_id = %s",
                (platform_name, user_id))
            result["banned_users"] = cur.rowcount
            cur.execute(
                "DELETE FROM admin_users "
                "WHERE platform = %s AND admin_id = %s",
                (platform_name, user_id))
            result["admin_users"] = cur.rowcount
            cur.execute(
                "DELETE FROM admin_settings "
                "WHERE platform = %s AND admin_id = %s",
                (platform_name, user_id))
            result["admin_settings"] = cur.rowcount
            cur.execute(
                "DELETE FROM admin_sudo_sessions "
                "WHERE platform = %s AND admin_id = %s",
                (platform_name, user_id))
            result["admin_sudo_sessions"] = cur.rowcount
            cur.execute(
                "DELETE FROM outbox_messages "
                "WHERE platform = %s AND target_user_id = %s",
                (platform_name, user_id))
            result["outbox_messages"] = cur.rowcount
            cur.execute(
                "DELETE FROM gdpr_requests "
                "WHERE platform = %s AND user_id = %s",
                (platform_name, user_id))
            result["gdpr_requests"] = cur.rowcount
            cur.execute(
                "DELETE FROM gdpr_completion_notified "
                "WHERE platform = %s AND user_id = %s",
                (platform_name, user_id))
            result["gdpr_completion_notified"] = cur.rowcount
            cur.execute("""
                INSERT INTO gdpr_erasure_log
                    (platform, user_id, erased_at, admin_id)
                VALUES (%s, %s, NOW(), %s)
                ON CONFLICT (platform, user_id) DO UPDATE SET erased_at = NOW()
            """, (platform_name, user_id, admin_id))

        # Файлы в кэше — это ПДн. При анонимизации фото/квитанции
        # тоже удаляются: изображение нельзя «анонимизировать»,
        # это либо реальный файл, либо его нет.
        # ФИКС: параметры platform_name/user_id, а не tp/tuid.
        # ФИКС: один вызов, а не два подряд (было дублирование).
        try:
            removed = _cleanup_media_cache_files(platform_name, user_id)
            result["media_cache_files_removed"] = removed
        except Exception as e:
            logger.warning(
                "anonymize_user_data: media_cache cleanup failed "
                "(platform=%s uid=%s): %s", platform_name, user_id, e)

        conn.commit()
    except Exception as e:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        logger.exception("DB error (anonymize_user_data): %s", e)
        return None
    finally:
        if own_conn and conn:
            return_db_connection(conn)

    # ФИКС Баг-16: через helper.
    _finalize_gdpr_media(admin_id, platform_name, user_id,
                         action="anonymize", result=result)

    # ФИКС Баг-3: pg_notify.
    try:
        from core.db import notify_cache_invalidate
        notify_cache_invalidate(
            "gdpr", user_id, platform_name=platform_name)
    except Exception as e:
        logger.warning(
            "notify gdpr cache invalidate failed (%s/%s): %s",
            platform_name, user_id, e)

    try:
        if platform_name == current_platform():
            user_state.invalidate(user_id, platform_name)
    except Exception as e:
        logger.warning("invalidate state cache failed (%s/%s): %s",
                       platform_name, user_id, e)

    if admin_id is not None:
        log_gdpr_action(admin_id, admin_platform or "?",
                        platform_name, user_id, "anonymize", result)
    if notify_user:
        _notify_user_of_deletion(platform_name, user_id)
    logger.info("GDPR anonymize(%s,%s): %s", platform_name, user_id, result)
    return result


def preview_user_data(platform_name, user_id):
    conn = None
    result = {}
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            for name, sql in [
                ("applications",
                 "SELECT COUNT(*) FROM bright_city_applications "
                 "WHERE platform=%s AND user_id=%s"),
                ("bot_messages",
                 "SELECT COUNT(*) FROM bot_messages "
                 "WHERE platform=%s AND user_id=%s"),
                ("bot_user_states",
                 "SELECT COUNT(*) FROM bot_user_states "
                 "WHERE platform=%s AND user_id=%s"),
                ("banned_users",
                 "SELECT COUNT(*) FROM banned_users "
                 "WHERE platform=%s AND user_id=%s"),
            ]:
                cur.execute(sql, (platform_name, user_id))
                result[name] = cur.fetchone()[0]
    except Exception as e:
        logger.error("DB error (preview_user_data): %s", e)
        return None
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)
    return result