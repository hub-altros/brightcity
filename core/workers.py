# core/workers.py
"""Фоновые воркеры: retry удаления VK-фото, full relay оригиналов TG→VK,
обработка outbox-очереди, алерты по застрявшим удалениям, финальные
уведомления GDPR.
"""
import asyncio
import logging

from core.runtime import platform, get_adapter
from core.db import (
    fetch_outbox_batch, mark_outbox_error, mark_outbox_sent,
    mark_outbox_skipped,
    fetch_pending_full_relay,
    mark_full_relay_success, mark_full_relay_error,
    mark_bot_message_edited,
    mark_bot_message_platform_id,
)
from core.notify import notify_admin_applications


logger = logging.getLogger("bright_city_bot")


async def media_retry_worker():
    from core.media import retry_pending_deletions
    while True:
        try:
            tried, ok, fail = await asyncio.to_thread(
                retry_pending_deletions, 20)
            if tried and fail:
                logger.warning(
                    "media retry: tried=%d ok=%d fail=%d", tried, ok, fail)
        except Exception as e:
            logger.exception("media_retry_worker loop error: %s", e)
        await asyncio.sleep(300)


async def full_photo_relay_worker():
    if platform() != "tg":
        return
    from core.media import relay_vk_upload
    adapter = get_adapter()
    while True:
        try:
            batch = await asyncio.to_thread(fetch_pending_full_relay, 5)
            for item in batch:
                try:
                    file_bytes = await adapter.get_file_bytes(item["source_file_id"])
                    if not file_bytes:
                        await asyncio.to_thread(
                            mark_full_relay_error, item["id"],
                            "get_file_bytes returned None")
                        continue
                    res = await relay_vk_upload(file_bytes)
                    if res and res.get("url"):
                        await asyncio.to_thread(
                            mark_full_relay_success, item["id"],
                            res["url"], res.get("owner_id"), res.get("photo_id"))
                    else:
                        await asyncio.to_thread(
                            mark_full_relay_error, item["id"], "vk upload failed")
                except Exception as e:
                    logger.exception("full_photo_relay_worker: id=%s: %s",
                                     item["id"], e)
                    await asyncio.to_thread(
                        mark_full_relay_error, item["id"], str(e)[:500])
        except Exception as e:
            logger.exception("full_photo_relay_worker loop error: %s", e)
        await asyncio.sleep(10)


async def _process_outbox_item(adapter, item):
    """Отправляет одну запись из outbox. Возвращает True/False."""
    try:
        mtype = item["message_type"]

        # --- EDIT: нативное редактирование ---
        if mtype == "edit":
            edit_mid = item.get("edit_platform_message_id")
            bot_msg_id = item.get("edit_of_bot_message_id")
            if not edit_mid:
                logger.warning(
                    "outbox edit without edit_platform_message_id (id=%s)",
                    item["id"])
                return False
            ok = await adapter.edit_message(
                item["target_user_id"], edit_mid, item["text"] or "")
            if ok and bot_msg_id:
                await asyncio.to_thread(
                    mark_bot_message_edited, bot_msg_id,
                    item["text"] or "")
            return ok

        # --- обычная отправка (возможно с reply) ---
        reply_mid = item.get("reply_to_platform_message_id")
        bot_msg_id = item.get("bot_message_id")

        if mtype == "banner" and item["attachment"]:
            return await adapter.send_attachment(
                item["target_user_id"],
                item["attachment"],
                caption=item["text"] or None,
                keep=True,
            )
        if mtype == "attachment" and item["attachment"]:
            return await adapter.send_attachment(
                item["target_user_id"],
                item["attachment"],
                caption=item["text"] or None,
            )

        result_mid = await adapter.send_message(
            item["target_user_id"],
            item["text"],
            keyboard=item["keyboard"],
            reply_to_platform_message_id=reply_mid,
        )
        # result_mid: str (platform id) | None | True (fallback).
        if result_mid:
            if isinstance(result_mid, str) and result_mid != "sent" \
                    and bot_msg_id:
                await asyncio.to_thread(
                    mark_bot_message_platform_id,
                    bot_msg_id, result_mid)
            return True
        return False
    except Exception as e:
        logger.exception(
            "outbox send failed (id=%s, platform=%s): %s",
            item["id"], platform(), e)
        return False


async def outbox_worker():
    from core.users import is_banned_checked

    adapter = get_adapter()
    my_platform = platform()
    while True:
        try:
            batch = await asyncio.to_thread(
                fetch_outbox_batch, my_platform, 10)
            for item in batch:
                try:
                    banned, check_failed = await asyncio.to_thread(
                        is_banned_checked,
                        item["target_user_id"],
                        my_platform,
                    )
                except Exception as e:
                    logger.warning(
                        "outbox_worker: is_banned_checked raised "
                        "(id=%s, user=%s): %s — пропускаю для retry",
                        item["id"], item["target_user_id"], e)
                    continue

                if check_failed:
                    continue
                if banned:
                    await asyncio.to_thread(
                        mark_outbox_skipped, item["id"],
                        "target user is banned on %s" % my_platform)
                    continue

                ok = await _process_outbox_item(adapter, item)
                if ok:
                    await asyncio.to_thread(mark_outbox_sent, item["id"])
                else:
                    await asyncio.to_thread(
                        mark_outbox_error, item["id"],
                        "send failed (all retries)")
                    try:
                        msg_preview = (item["text"] or "")[:200]
                        await asyncio.to_thread(
                            notify_admin_applications,
                            f"⚠ Не удалось доставить сообщение "
                            f"пользователю {item['target_user_id']} "
                            f"(id={item['id']}).\n\n"
                            f"Текст: {msg_preview}"
                        )
                    except Exception as notify_err:
                        logger.error(
                            "outbox_worker: не удалось уведомить "
                            "админов об ошибке: %s", notify_err)
        except Exception as e:
            logger.exception("outbox_worker (%s) loop error: %s",
                             my_platform, e)
        await asyncio.sleep(2)


async def stuck_deletion_alert_worker(interval_sec=6 * 3600):
    from core.db import get_stuck_deletions
    while True:
        try:
            stuck = await asyncio.to_thread(get_stuck_deletions, 60)
            if stuck:
                lines = [
                    f"⚠ GDPR: {len(stuck)} медиафайлов не удалены с VK CDN:\n"
                ]
                for m in stuck[:20]:
                    err = (m['last_error'] or '—')[:60]
                    lines.append(
                        f"  id={m['id']} | {m['user_platform']}/{m['user_id']}"
                        f" | kind={m['kind']} | attempts={m['attempts']}"
                        f" | err={err}"
                    )
                if len(stuck) > 20:
                    lines.append(f"  … и ещё {len(stuck) - 20}")
                await asyncio.to_thread(
                    notify_admin_applications, "\n".join(lines))
        except Exception as e:
            logger.exception("stuck_deletion_alert_worker: %s", e)
        await asyncio.sleep(interval_sec)


async def gdpr_completion_notify_worker(interval_sec=60):
    # (без изменений от оригинала — оставил как было)
    from core.db import get_db_connection, return_db_connection
    while True:
        try:
            conn = None
            rows = []
            try:
                conn = get_db_connection()
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT e.platform, e.user_id
                        FROM gdpr_erasure_log e
                        LEFT JOIN gdpr_completion_notified n
                          ON n.platform = e.platform AND n.user_id = e.user_id
                        WHERE e.erased_at > NOW() - INTERVAL '30 days'
                          AND n.platform IS NULL
                        LIMIT 200
                    """)
                    rows = cur.fetchall()
            finally:
                if conn:
                    conn.rollback()
                    return_db_connection(conn)

            for pl, uid in rows:
                from core.db import is_user_media_fully_deleted, enqueue_outbox
                if not await asyncio.to_thread(
                        is_user_media_fully_deleted, pl, uid):
                    continue
                ok = await asyncio.to_thread(
                    enqueue_outbox,
                    target_platform=pl,
                    target_user_id=uid,
                    text=("✅ Удаление персональных данных завершено. "
                          "Все ваши данные и медиафайлы удалены."),
                    message_type="gdpr_completion",
                    idempotency_key=f"gdpr_completion:{pl}:{uid}",
                )
                if ok:
                    conn = None
                    try:
                        conn = get_db_connection()
                        with conn.cursor() as cur:
                            cur.execute("""
                                INSERT INTO gdpr_completion_notified
                                    (platform, user_id, notified_at)
                                VALUES (%s, %s, NOW())
                                ON CONFLICT (platform, user_id) DO NOTHING
                            """, (pl, uid))
                        conn.commit()
                    finally:
                        if conn:
                            conn.rollback()
                            return_db_connection(conn)
        except Exception as e:
            logger.exception("gdpr_completion_notify_worker: %s", e)
        await asyncio.sleep(interval_sec)