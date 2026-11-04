"""JSON API для списка/карточки заявок + мутации."""
import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from psycopg2 import sql

from core.db import get_db_connection, return_db_connection
from core.admin import _exec_reject
from core.notify import send_message
from core.storage import save_message_to_db
from core.texts import (
    STEP_LABELS, REGISTRATION_STATES, STATE_QUESTION,
)

from web_app.dependencies import (
    require_api_admin as require_admin,   # 401 вместо 303 для React
    run_blocking,
)
from web_app.db_helpers import create_banner_media_file
from web_app.services.applications import (
    first_photo_media_id as _first_photo_media_id,
    web_set_in_progress as _web_set_in_progress,
)
from web_app.api.schemas import (
    ApplicationsListResp, ApplicationListItem, ApplicationDetailResp,
    MediaItem, HistoryRow, MessageRow, OkResp,
    IncompleteListItem, IncompleteListResp, IncompleteDetailResp,
)

logger = logging.getLogger("web.api.apps")
router = APIRouter(prefix="/api/applications", tags=["applications"])
PAGE_SIZE = 20

_SORT_COLUMNS = {
    "id": "id",
    "created_at": "created_at",
    "name_age": "name_age",
    "status": "status",
    "location": "location",
    "user_type": "user_type",
    "platform": "platform",
}

_PLATFORM_ALIASES = {
    "vk": "vk", "вк": "vk", "вконтакте": "vk", "vkontakte": "vk",
    "tg": "tg", "тг": "tg", "телеграм": "tg", "telegram": "tg",
    "max": "max", "макс": "max",
}

_VALID_PLATFORMS = ("vk", "tg", "max")


# ============================================================
#  ⚠️ Статические пути — ВЫШЕ динамических.
#  GET /incomplete должен стоять раньше GET /{app_id},
#  иначе "incomplete" пытается стать int → 422.
# ============================================================

@router.get("/incomplete", response_model=IncompleteListResp)
async def list_incomplete(
    admin=Depends(require_admin),
):
    """Список незавершённых заявок (из bot_user_states).

    В каждой строке — last_remind_at: время последнего напоминания,
    отправленного через веб (или бот). Источник — admin_audit_log,
    action='remind_incomplete'.
    """
    conn = None
    items = []
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            incomplete_states = list(REGISTRATION_STATES) + [STATE_QUESTION]
            cur.execute("""
                WITH last_reminders AS (
                    SELECT DISTINCT ON (target_platform, target_id)
                           target_platform, target_id, created_at
                    FROM admin_audit_log
                    WHERE action = 'remind_incomplete'
                    ORDER BY target_platform, target_id, created_at DESC
                )
                SELECT bus.platform, bus.user_id, bus.state,
                       bus.temp_data, bus.updated_at,
                       lr.created_at AS last_remind_at
                FROM bot_user_states bus
                LEFT JOIN last_reminders lr
                       ON lr.target_platform = bus.platform
                      AND lr.target_id = bus.user_id
                WHERE bus.state = ANY(%s)
                ORDER BY bus.updated_at DESC
                LIMIT 200
            """, (incomplete_states,))
            rows = cur.fetchall()

            for pl, uid, st, temp, upd, last_remind_at in rows:
                if isinstance(temp, str):
                    try:
                        temp = json.loads(temp)
                    except Exception:
                        temp = {}
                temp = temp or {}
                items.append(IncompleteListItem(
                    platform=pl,
                    user_id=uid,
                    state=st,
                    step_label=STEP_LABELS.get(st, st),
                    updated_at=upd,
                    name_age=temp.get("name_age"),
                    phone=temp.get("phone"),
                    last_remind_at=last_remind_at,
                ))
    except Exception as e:
        logger.error("list_incomplete failed: %s", e)
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)

    return IncompleteListResp(items=items, total=len(items))


@router.get(
    "/incomplete/{platform}/{user_id}",
    response_model=IncompleteDetailResp,
)
async def get_incomplete(
    platform: str, user_id: int, admin=Depends(require_admin),
):
    if platform not in _VALID_PLATFORMS:
        raise HTTPException(400, "platform: vk|tg|max")

    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT state, temp_data, updated_at
                FROM bot_user_states
                WHERE platform = %s AND user_id = %s
            """, (platform, user_id))
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, "Не найдено")

            st, temp, upd = row
            if isinstance(temp, str):
                try:
                    temp = json.loads(temp)
                except Exception:
                    temp = {}
            temp = temp or {}

            return IncompleteDetailResp(
                platform=platform,
                user_id=user_id,
                state=st,
                step_label=STEP_LABELS.get(st, st),
                updated_at=upd,
                temp_data=temp,
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("get_incomplete failed: %s", e)
        raise HTTPException(500, "Ошибка сервера")
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)


@router.post(
    "/incomplete/{platform}/{user_id}/remind",
    response_model=OkResp,
)
async def remind_incomplete(
    platform: str, user_id: int, admin=Depends(require_admin),
):
    if platform not in _VALID_PLATFORMS:
        raise HTTPException(400, "platform: vk|tg|max")

    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT state FROM bot_user_states "
                "WHERE platform = %s AND user_id = %s",
                (platform, user_id))
            row = cur.fetchone()
            if not row:
                raise HTTPException(
                    404, "Пользователь не найден в незавершённых")
            state = row[0]
            if state not in REGISTRATION_STATES and state != STATE_QUESTION:
                raise HTTPException(
                    400,
                    f"Пользователь уже не в процессе заполнения "
                    f"(state={state}). Напоминание не отправлено.",
                )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("remind_incomplete: state check failed: %s", e)
        raise HTTPException(500, "Ошибка сервера")
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)

    reminder_text = (
        "Вы не завершили заполнение заявки. "
        "Если у вас есть вопросы по заполнению данных, "
        "то напишите ваш вопрос текстом прямо здесь в чат. "
        "Администратор ответит в течении дня."
    )

    ok = await run_blocking(
        send_message, user_id, reminder_text,
        target_platform=platform,
    )
    if not ok:
        raise HTTPException(500, "Не удалось отправить напоминание")

    await run_blocking(
        save_message_to_db, user_id, "out", reminder_text,
        target_platform=platform,
    )

    try:
        from core.db import log_admin_action
        await run_blocking(
            log_admin_action,
            admin[0], "remind_incomplete",
            target_platform=platform, target_id=user_id,
            params={"source": "web"},
        )
    except Exception as e:
        logger.warning(
            "remind_incomplete: log_admin_action failed: %s", e)

    return OkResp()


# ============================================================
#  Список / карточка (динамические пути — ПОСЛЕ статических)
# ============================================================

def _build_where(status: Optional[str], q: Optional[str]):
    """Хелпер для сборки WHERE-условия (общий для списка и карточки)."""
    where = [sql.SQL("TRUE")]
    params = []

    if status in ("new", "in_progress", "rejected", "done"):
        where.append(sql.SQL("status = %s"))
        params.append(status)

    if q:
        words = q.strip().lower().split()
        platform_filter = None
        search_words = []
        for w in words:
            if w in _PLATFORM_ALIASES and platform_filter is None:
                platform_filter = _PLATFORM_ALIASES[w]
            else:
                search_words.append(w)

        if platform_filter:
            where.append(sql.SQL("platform = %s"))
            params.append(platform_filter)

        if search_words:
            joined = " ".join(search_words)
            like = f"%{joined}%"
            where.append(sql.SQL(
                "(LOWER(COALESCE(name_age,'')) LIKE %s OR "
                " LOWER(COALESCE(phone,'')) LIKE %s OR "
                " LOWER(COALESCE(location,'')) LIKE %s)"))
            params.extend([like, like, like])

    return sql.SQL(" AND ").join(where), params


@router.get("", response_model=ApplicationsListResp)
async def list_apps(
    status: Optional[str] = None,
    q: Optional[str] = None,
    page: int = 1,
    sort: str = "created_at",
    dir: str = "asc",
    admin=Depends(require_admin),
):
    conn = None
    rows, total = [], 0

    sort_col = _SORT_COLUMNS.get((sort or "").lower(), "created_at")
    sort_dir = "ASC" if (dir or "").lower() == "asc" else "DESC"

    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            where_sql, params = _build_where(status, q)

            cur.execute(sql.SQL(
                "SELECT COUNT(*) FROM bright_city_applications WHERE {}"
            ).format(where_sql), params)
            total = cur.fetchone()[0]

            cur.execute(
                sql.SQL("""
                    SELECT id, user_id, location, user_type, phone, name_age,
                           status, banner_url, created_at, platform, child_photo_urls
                    FROM bright_city_applications WHERE {}
                    ORDER BY {col} {dir} NULLS LAST, id {dir}
                    LIMIT %s OFFSET %s
                """).format(
                    where_sql,
                    col=sql.Identifier(sort_col),
                    dir=sql.SQL(sort_dir),
                ),
                params + [PAGE_SIZE, (page - 1) * PAGE_SIZE])
            rows = cur.fetchall()
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)

    items = []
    for r in rows:
        (app_id, uid, loc, utype, phone, name_age, st, b_url,
         created, pl, photos_raw) = r
        items.append(ApplicationListItem(
            id=app_id, user_id=uid, location=loc, user_type=utype,
            phone=phone, name_age=name_age, status=st, banner_url=b_url,
            created_at=created, platform=pl,
            first_media_id=_first_photo_media_id(app_id, photos_raw),
        ))
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    return ApplicationsListResp(
        items=items, total=total, page=page, total_pages=total_pages)


def _media_ids_for_app(app_id: int) -> dict:
    """Возвращает {url: media_id} для всех медиа заявки.

    Два прохода:
      1) по application_id — работает для TG/VK;
      2) по url из заявки — спасает MAX, где application_id может
         остаться NULL (register_media_file до INSERT заявки).
    """
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT child_photo_urls, drawings_urls,
                       payment_doc_urls, org_info_attachments, banner_url
                FROM bright_city_applications WHERE id = %s
            """, (app_id,))
            row = cur.fetchone()
            app_urls = set()
            if row:
                for raw in (row[0], row[1], row[2], row[3]):
                    if not raw:
                        continue
                    try:
                        urls = (json.loads(raw)
                                if isinstance(raw, str)
                                else list(raw))
                        for u in urls:
                            if isinstance(u, str) and u:
                                app_urls.add(u)
                    except Exception:
                        pass
                if row[4]:
                    app_urls.add(row[4])

            result: dict = {}

            cur.execute(
                "SELECT url, id FROM media_files WHERE application_id = %s",
                (app_id,))
            for url, mid in cur.fetchall():
                result[url] = mid

            if app_urls:
                cur.execute(
                    "SELECT url, id FROM media_files WHERE url = ANY(%s) "
                    "ORDER BY (application_id = %s) DESC NULLS LAST, id ASC",
                    (list(app_urls), app_id))
                for url, mid in cur.fetchall():
                    result.setdefault(url, mid)

            return result
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)


def _resolve_media(raw, media_by_url: dict) -> list[MediaItem]:
    if not raw:
        return []
    try:
        urls = json.loads(raw) if isinstance(raw, str) else list(raw)
    except Exception:
        return []
    out = []
    for u in urls:
        if isinstance(u, str) and u:
            out.append(MediaItem(url=u, media_id=media_by_url.get(u)))
    return out


@router.get("/{app_id}", response_model=ApplicationDetailResp)
async def get_app(
    app_id: int, 
    status: Optional[str] = None, 
    q: Optional[str] = None,
    sort: str = "created_at", 
    dir: str = "asc",
    admin=Depends(require_admin)
):
    conn = None
    prev_id = None
    next_id = None
    
    sort_col = _SORT_COLUMNS.get((sort or "").lower(), "created_at")
    sort_dir = "ASC" if (dir or "").lower() == "asc" else "DESC"
    where_sql, params = _build_where(status, q)

    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, user_id, location, placement_location, user_type,
                       phone, name_age, child_photo_urls, format_choice,
                       drawings_urls, training_info, payment_doc_urls,
                       org_info_attachments, status, banner_url, created_at,
                       platform, consent_version, rejection_reason, anonymized_at
                FROM bright_city_applications WHERE id = %s
            """, (app_id,))
            r = cur.fetchone()

            if r:
                # Считаем ID соседей с учетом фильтров и сортировки
                cur.execute(
                    sql.SQL("""
                        WITH ordered AS (
                            SELECT id,
                                   LAG(id) OVER (ORDER BY {col} {dir} NULLS LAST, id {dir}) as prev_id,
                                   LEAD(id) OVER (ORDER BY {col} {dir} NULLS LAST, id {dir}) as next_id
                            FROM bright_city_applications
                            WHERE {where}
                        )
                        SELECT prev_id, next_id FROM ordered WHERE id = %s
                    """).format(
                        col=sql.Identifier(sort_col),
                        dir=sql.SQL(sort_dir),
                        where=where_sql
                    ),
                    params + [app_id]
                )
                n_row = cur.fetchone()
                if n_row:
                    prev_id, next_id = n_row[0], n_row[1]
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)

    if not r:
        raise HTTPException(404, "Заявка не найдена")

    media_by_url = _media_ids_for_app(app_id)

    hist_conn = get_db_connection()
    try:
        with hist_conn.cursor() as cur:
            cur.execute("""
                SELECT old_status, new_status, admin_name, changed_at
                FROM application_status_history WHERE application_id = %s
                ORDER BY changed_at ASC
            """, (app_id,))
            hist_rows = cur.fetchall()
            cur.execute("""
                SELECT direction, text, created_at FROM bot_messages
                WHERE platform = %s AND user_id = %s
                ORDER BY created_at DESC LIMIT 50
            """, (r[16], r[1]))
            msg_rows = cur.fetchall()
    finally:
        hist_conn.rollback()
        return_db_connection(hist_conn)

    return ApplicationDetailResp(
        id=r[0], user_id=r[1], location=r[2], placement_location=r[3],
        user_type=r[4], phone=r[5], name_age=r[6], format_choice=r[8],
        training_info=r[10], status=r[13], banner_url=r[14],
        banner_media_id=media_by_url.get(r[14]) if r[14] else None,
        created_at=r[15], platform=r[16], consent_version=r[17],
        rejection_reason=r[18], anonymized_at=r[19],
        child_photos=_resolve_media(r[7], media_by_url),
        drawings=_resolve_media(r[9], media_by_url),
        payment_docs=_resolve_media(r[11], media_by_url),
        org_attachments=_resolve_media(r[12], media_by_url),
        history=[HistoryRow(old_status=h[0], new_status=h[1],
                            admin_name=h[2], changed_at=h[3])
                 for h in hist_rows],
        messages=[MessageRow(direction=m[0], text=m[1], created_at=m[2])
                  for m in msg_rows],
        prev_id=prev_id,
        next_id=next_id,
    )


@router.post("/{app_id}/status", response_model=OkResp)
async def set_status(app_id: int, body: dict, admin=Depends(require_admin)):
    new_status = body.get("new_status")
    if new_status != "in_progress":
        raise HTTPException(400, "Используйте /reject для отклонения")
    admin_id = admin[0]
    admin_name = admin[3] or admin[1]
    try:
        await run_blocking(_web_set_in_progress, admin_id, admin_name, app_id)
    except Exception as e:
        logger.exception("set_status failed: %s", e)
        raise HTTPException(500, str(e))
    return OkResp()


@router.post("/{app_id}/reject", response_model=OkResp)
async def reject(app_id: int, body: dict, admin=Depends(require_admin)):
    reason = (body.get("reason") or "").strip()[:2000]
    if not reason:
        raise HTTPException(400, "Нужна причина")
    try:
        await run_blocking(_exec_reject, admin[0], app_id, reason)
    except Exception as e:
        logger.exception("reject failed: %s", e)
        raise HTTPException(500, str(e))
    return OkResp()


@router.post("/{app_id}/reply", response_model=OkResp)
async def reply(app_id: int, body: dict, admin=Depends(require_admin)):
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "Пустой текст")

    conn = None
    target_user_id = None
    target_platform = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT user_id, platform "
                "FROM bright_city_applications WHERE id = %s",
                (app_id,))
            r = cur.fetchone()
            if r:
                target_user_id, target_platform = r
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)

    if not target_user_id:
        raise HTTPException(404, "Заявка или пользователь не найдены")

    ok = await run_blocking(
        send_message, target_user_id, text,
        target_platform=target_platform)
    if ok:
        save_message_to_db(target_user_id, "out", text,
                           application_id=app_id,
                           target_platform=target_platform)
    else:
        raise HTTPException(500, "Не удалось отправить")
    return OkResp()


@router.post("/{app_id}/banner", response_model=OkResp)
async def upload_banner(
    app_id: int, file: UploadFile = File(...),
    admin=Depends(require_admin),
):
    admin_id = admin[0]
    admin_name = admin[3] or admin[1]

    conn = None
    target_user_id = target_platform = old_status = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT user_id, platform, status "
                "FROM bright_city_applications WHERE id = %s FOR UPDATE",
                (app_id,))
            r = cur.fetchone()
            if not r:
                raise HTTPException(404)
            target_user_id, target_platform, old_status = r
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)

    if old_status == "done":
        raise HTTPException(409, "Заявка уже завершена")

    file_bytes = await file.read()
    created = await run_blocking(
        create_banner_media_file,
        app_id, target_platform, target_user_id, file_bytes)
    if not created:
        raise HTTPException(
            400, "Не удалось обработать файл (или слишком большой)")
    media_id, public_url = created

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE bright_city_applications "
                "SET banner_url = %s, status = 'done' WHERE id = %s",
                (public_url, app_id))
            from core.admin import _log_status_change_with_cursor
            _log_status_change_with_cursor(
                cur, app_id, old_status, "done", admin_id, admin_name)
        conn.commit()
    except Exception as e:
        logger.exception("banner DB update failed: %s", e)
        raise HTTPException(500, str(e))
    finally:
        conn.rollback()
        return_db_connection(conn)

    try:
        from core.admin import send_banner_to_user
        await run_blocking(
            send_banner_to_user, target_user_id, public_url,
            app_id, admin_id=admin_id, target_platform=target_platform)
    except Exception as e:
        logger.exception("send_banner_to_user failed: %s", e)

    return OkResp()