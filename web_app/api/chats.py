"""JSON API для страницы «Переписка»: список диалогов, архив, ответы, правка."""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from psycopg2 import sql

from core.db import get_db_connection, return_db_connection, enqueue_outbox
from core.notify import send_message
from core.storage import save_message_to_db

from web_app.dependencies import require_api_admin as require_admin, run_blocking
from web_app.api.schemas import (
    ChatListResp, ChatListItem, ChatDetailResp, ChatMessage, QuoteRef,
    OkResp, ReplyResp, EditResp,
)

logger = logging.getLogger("web.api.chats")
router = APIRouter(prefix="/api/chats", tags=["chats"])
PAGE_SIZE = 50
_VALID_PLATFORMS = ("vk", "tg", "max")

_PLATFORM_ALIASES = {
    "vk": "vk", "вк": "vk", "вконтакте": "vk", "vkontakte": "vk",
    "tg": "tg", "тг": "tg", "телеграм": "tg", "telegram": "tg",
    "max": "max", "макс": "max",
}


@router.get("", response_model=ChatListResp)
async def list_chats(
    tab: str = "active",
    page: int = 1,
    q: Optional[str] = None,
    admin=Depends(require_admin),
):
    if tab not in ("active", "archive"):
        raise HTTPException(400, "tab: active|archive")

    conn = None
    items, total = [], 0
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            platforms = list(_VALID_PLATFORMS)
            where = [sql.SQL("cl.platform = ANY(%s)")]
            params: list = [platforms]

            if q and q.strip():
                words = q.strip().lower().split()
                platform_filter = None
                search_words = []
                for w in words:
                    if w in _PLATFORM_ALIASES and platform_filter is None:
                        platform_filter = _PLATFORM_ALIASES[w]
                    else:
                        search_words.append(w)

                if platform_filter:
                    where.append(sql.SQL("cl.platform = %s"))
                    params.append(platform_filter)

                if search_words:
                    like = f"%{' '.join(search_words)}%"
                    where.append(sql.SQL("""
                        (LOWER(COALESCE(cm.name_age,'')) LIKE %s
                         OR LOWER(COALESCE(cm.phone,''))   LIKE %s
                         OR CAST(cl.user_id AS TEXT)       LIKE %s
                         OR LOWER(COALESCE(cu.display_name,'')) LIKE %s)
                    """))
                    params.extend([like, like, like, like])

            if tab == "archive":
                where.append(sql.SQL(
                    "ca.archived_at IS NOT NULL AND cl.last_message_at <= ca.archived_at"
                ))
            else:
                where.append(sql.SQL(
                    "(ca.archived_at IS NULL OR cl.last_message_at > ca.archived_at)"
                ))

            where_sql = sql.SQL(" AND ").join(where)

            base_cte = sql.SQL("""
                WITH cl AS (
                    SELECT DISTINCT ON (platform, user_id)
                        platform, user_id,
                        created_at AS last_message_at,
                        direction, text
                    FROM bot_messages
                    WHERE platform = ANY(%s)
                    ORDER BY platform, user_id, created_at DESC
                ),
                cm AS (
                    SELECT DISTINCT ON (platform, user_id)
                        platform, user_id,
                        id AS application_id,
                        name_age, phone, status
                    FROM bright_city_applications
                    WHERE platform = ANY(%s)
                    ORDER BY platform, user_id, created_at DESC
                ),
                cu AS (
                    SELECT platform, user_id, display_name, username
                    FROM platform_users
                )
            """)

            cur.execute(
                base_cte + sql.SQL("""
                    SELECT COUNT(*)
                    FROM cl
                    LEFT JOIN cm USING (platform, user_id)
                    LEFT JOIN cu USING (platform, user_id)
                    LEFT JOIN chat_archives ca USING (platform, user_id)
                    WHERE {}
                """).format(where_sql),
                [platforms, platforms] + params,
            )
            total = cur.fetchone()[0]

            cur.execute(
                base_cte + sql.SQL("""
                    SELECT
                        cl.platform, cl.user_id,
                        cu.display_name, cu.username,
                        cm.application_id, cm.name_age, cm.phone, cm.status,
                        cl.last_message_at, cl.direction, cl.text,
                        ca.archived_at
                    FROM cl
                    LEFT JOIN cm USING (platform, user_id)
                    LEFT JOIN cu USING (platform, user_id)
                    LEFT JOIN chat_archives ca USING (platform, user_id)
                    WHERE {}
                    ORDER BY cl.last_message_at DESC
                    LIMIT %s OFFSET %s
                """).format(where_sql),
                [platforms, platforms] + params + [PAGE_SIZE, (page - 1) * PAGE_SIZE],
            )
            for r in cur.fetchall():
                (pl, uid, disp_name, username, app_id, name_age, phone,
                 app_status, last_at, last_dir, last_text, archived_at) = r
                items.append(ChatListItem(
                    platform=pl,
                    user_id=uid,
                    sender_name=disp_name,
                    sender_username=username,
                    application_id=app_id,
                    name_age=name_age,
                    phone=phone,
                    application_status=app_status,
                    last_message_at=last_at,
                    last_message_text=last_text,
                    last_message_direction=last_dir,
                    is_unanswered=(last_dir == "in"),
                    archived_at=archived_at,
                ))
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)

    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    return ChatListResp(
        items=items, total=total, page=page, total_pages=total_pages)


@router.get("/{platform}/{user_id}", response_model=ChatDetailResp)
async def get_chat(platform: str, user_id: int, admin=Depends(require_admin)):
    if platform not in _VALID_PLATFORMS:
        raise HTTPException(400, "platform: vk|tg|max")

    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, direction, text, created_at,
                       reply_to_id, reply_to_text, reply_to_direction,
                       edited_at
                FROM bot_messages
                WHERE platform = %s AND user_id = %s
                ORDER BY created_at ASC
                LIMIT 500
            """, (platform, user_id))
            messages = []
            for m in cur.fetchall():
                (mid, direction, text, created_at,
                 r_to_id, r_to_text, r_to_dir, edited_at) = m
                reply_to = None
                if r_to_id is not None or r_to_text is not None:
                    reply_to = QuoteRef(
                        id=int(r_to_id) if r_to_id else 0,
                        text=r_to_text or "(сообщение удалено)",
                        direction=r_to_dir or "in",
                    )
                messages.append(ChatMessage(
                    id=mid,
                    direction=direction,
                    text=text,
                    created_at=created_at,
                    reply_to=reply_to,
                    edited_at=edited_at,
                ))

            cur.execute("""
                SELECT id, name_age, phone, status
                FROM bright_city_applications
                WHERE platform = %s AND user_id = %s
                ORDER BY created_at DESC LIMIT 1
            """, (platform, user_id))
            app_row = cur.fetchone()

            cur.execute(
                "SELECT archived_at FROM chat_archives "
                "WHERE platform = %s AND user_id = %s",
                (platform, user_id))
            arch_row = cur.fetchone()
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)

    return ChatDetailResp(
        platform=platform,
        user_id=user_id,
        messages=messages,
        application_id=app_row[0] if app_row else None,
        name_age=app_row[1] if app_row else None,
        phone=app_row[2] if app_row else None,
        application_status=app_row[3] if app_row else None,
        archived_at=arch_row[0] if arch_row else None,
    )


@router.post("/{platform}/{user_id}/reply", response_model=ReplyResp)
async def reply(platform: str, user_id: int, body: dict,
                admin=Depends(require_admin)):
    if platform not in _VALID_PLATFORMS:
        raise HTTPException(400, "platform: vk|tg|max")

    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "Пустой текст")

    reply_to_id = body.get("reply_to_id")
    try:
        reply_to_id = int(reply_to_id) if reply_to_id else None
    except (TypeError, ValueError):
        reply_to_id = None

    reply_to_text = None
    reply_to_direction = None
    reply_to_platform_message_id = None

    conn = get_db_connection()
    app_id = None
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM bright_city_applications "
                "WHERE platform = %s AND user_id = %s "
                "ORDER BY created_at DESC LIMIT 1",
                (platform, user_id))
            r = cur.fetchone()
            if r:
                app_id = r[0]

            if reply_to_id:
                cur.execute(
                    "SELECT text, direction, platform_message_id "
                    "FROM bot_messages "
                    "WHERE id = %s AND platform = %s AND user_id = %s",
                    (reply_to_id, platform, user_id))
                q = cur.fetchone()
                if q:
                    reply_to_text = (q[0] or "")[:500]
                    reply_to_direction = q[1]
                    reply_to_platform_message_id = q[2]
                else:
                    logger.warning(
                        "reply: message id=%s не найдено для %s/%s — "
                        "отправляю без цитаты",
                        reply_to_id, platform, user_id)
                    reply_to_id = None
    finally:
        conn.rollback()
        return_db_connection(conn)

    # Fallback: если у оригинала нет platform_message_id, добавляем
    # текстовую цитату в тело (в TG/VK/MAX нативный reply невозможен).
    send_text = text
    if reply_to_text and not reply_to_platform_message_id:
        prefix_direction = "Вы" if reply_to_direction == "out" else "Пользователь"
        short = reply_to_text if len(reply_to_text) <= 120 else reply_to_text[:117] + "…"
        send_text = f"↩ {prefix_direction}: «{short}»\n\n{text}"

    bot_message_id = save_message_to_db(
        user_id, "out", text,
        application_id=app_id, target_platform=platform,
        platform_message_id=None,
    )

    if reply_to_id and bot_message_id:
        c2 = None
        try:
            c2 = get_db_connection()
            with c2.cursor() as cur:
                cur.execute(
                    "UPDATE bot_messages "
                    "SET reply_to_id = %s, reply_to_text = %s, "
                    "    reply_to_direction = %s "
                    "WHERE id = %s",
                    (reply_to_id, reply_to_text, reply_to_direction,
                     bot_message_id))
            c2.commit()
        except Exception as e:
            logger.error("reply: update reply_* failed: %s", e)
        finally:
            if c2:
                c2.rollback()
                return_db_connection(c2)

    ok = await run_blocking(
        send_message, user_id, send_text,
        target_platform=platform,
        reply_to_platform_message_id=reply_to_platform_message_id,
        bot_message_id=bot_message_id,
    )

    if not ok:
        # Откатываем bot_messages, чтобы не висело в UI «как будто отправлено».
        if bot_message_id:
            c3 = None
            try:
                c3 = get_db_connection()
                with c3.cursor() as cur:
                    cur.execute(
                        "DELETE FROM bot_messages WHERE id = %s",
                        (bot_message_id,))
                c3.commit()
            except Exception as e:
                logger.error("reply: cleanup bot_messages failed: %s", e)
            finally:
                if c3:
                    c3.rollback()
                    return_db_connection(c3)
        raise HTTPException(500, "Не удалось отправить")

    return ReplyResp(ok=True, bot_message_id=bot_message_id)


@router.patch("/{platform}/{user_id}/messages/{message_id}",
              response_model=EditResp)
async def edit_message(platform: str, user_id: int, message_id: int,
                       body: dict, admin=Depends(require_admin)):
    """Редактирует исходящее сообщение.

    Если у сообщения есть platform_message_id и воркер умеет edit —
    редактирует нативно в TG/VK/MAX (mode='native').
    Если нет (старые сообщения или воркер пока не научился) —
    обновляет БД и отправляет пользователю «Уточнение: <новый текст>»
    отдельным сообщением (mode='correction').
    """
    if platform not in _VALID_PLATFORMS:
        raise HTTPException(400, "platform: vk|tg|max")

    new_text = (body.get("text") or "").strip()
    if not new_text:
        raise HTTPException(400, "Пустой текст")

    conn = get_db_connection()
    row = None
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT direction, platform_message_id, text "
                "FROM bot_messages "
                "WHERE id = %s AND platform = %s AND user_id = %s",
                (message_id, platform, user_id))
            row = cur.fetchone()
    finally:
        conn.rollback()
        return_db_connection(conn)

    if not row:
        raise HTTPException(404, "Сообщение не найдено")
    direction, platform_message_id, old_text = row
    if direction != "out":
        raise HTTPException(400, "Можно редактировать только свои сообщения")
    if old_text and old_text == new_text:
        return EditResp(ok=True, mode="native")  # no-op

    app_id = None
    c3 = get_db_connection()
    try:
        with c3.cursor() as cur:
            cur.execute(
                "SELECT id FROM bright_city_applications "
                "WHERE platform = %s AND user_id = %s "
                "ORDER BY created_at DESC LIMIT 1",
                (platform, user_id))
            r = cur.fetchone()
            if r:
                app_id = r[0]
    finally:
        c3.rollback()
        return_db_connection(c3)

    # 1) Пытаемся нативно, если есть platform_message_id.
    native_queued = False
    if platform_message_id:
        try:
            native_queued = enqueue_outbox(
                target_platform=platform,
                target_user_id=user_id,
                text=new_text,
                message_type="edit",
                edit_platform_message_id=platform_message_id,
                edit_of_bot_message_id=message_id,
            )
        except Exception as e:
            logger.error("edit_message: enqueue edit failed: %s", e)
            native_queued = False

    if native_queued:
        return EditResp(ok=True, mode="native")

    # 2) Fallback: обновляем БД + отправляем «Уточнение» отдельным сообщением.
    conn2 = get_db_connection()
    try:
        with conn2.cursor() as cur:
            cur.execute(
                "UPDATE bot_messages "
                "SET text = %s, edited_at = NOW() "
                "WHERE id = %s",
                (new_text, message_id))
        conn2.commit()
    finally:
        conn2.rollback()
        return_db_connection(conn2)

    correction_text = f"✏️ Уточнение: {new_text}"
    ok = await run_blocking(
        send_message, user_id, correction_text,
        target_platform=platform,
    )
    if not ok:
        logger.warning(
            "edit_message: correction send failed (user=%s)", user_id)

    save_message_to_db(
        user_id, "out", correction_text,
        application_id=app_id, target_platform=platform,
    )

    return EditResp(ok=True, mode="correction")


@router.post("/{platform}/{user_id}/archive", response_model=OkResp)
async def archive(platform: str, user_id: int, admin=Depends(require_admin)):
    if platform not in _VALID_PLATFORMS:
        raise HTTPException(400, "platform: vk|tg|max")

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM bot_messages "
                "WHERE platform = %s AND user_id = %s AND direction = 'out' "
                "LIMIT 1",
                (platform, user_id))
            if not cur.fetchone():
                raise HTTPException(
                    400,
                    "Сначала ответьте пользователю — без ответа нельзя "
                    "переместить диалог в архив")
            cur.execute("""
                INSERT INTO chat_archives (platform, user_id, archived_at, archived_by)
                VALUES (%s, %s, NOW(), %s)
                ON CONFLICT (platform, user_id)
                DO UPDATE SET archived_at = NOW(),
                              archived_by = EXCLUDED.archived_by
            """, (platform, user_id, admin[0]))
        conn.commit()
    except HTTPException:
        raise
    finally:
        conn.rollback()
        return_db_connection(conn)

    return OkResp()


@router.post("/{platform}/{user_id}/unarchive", response_model=OkResp)
async def unarchive(platform: str, user_id: int, admin=Depends(require_admin)):
    if platform not in _VALID_PLATFORMS:
        raise HTTPException(400, "platform: vk|tg|max")

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM chat_archives "
                "WHERE platform = %s AND user_id = %s",
                (platform, user_id))
        conn.commit()
    finally:
        conn.rollback()
        return_db_connection(conn)

    return OkResp()