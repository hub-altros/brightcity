# core/admin.py
"""Админ-панель: заявки, статусы, sudo-подтверждения, GDPR.

Всё, что делает админ в чате, кроме команд, связанных со state machine
регистрации.
"""
import json
import logging
import time
import uuid

from psycopg2 import sql

from core.db import (
    get_db_connection, return_db_connection,
    enqueue_outbox,
    request_full_relay_for_user,
    register_media_file,
    log_gdpr_action, get_gdpr_audit,
    has_pending_gdpr_request,
    get_last_gdpr_outcome,
    get_gdpr_history_for_subject,
    log_admin_action,
    get_banned_users_list, count_banned_users,
    count_pending_gdpr_requests,
    list_pending_gdpr_requests,
    get_gdpr_request_by_id,
)
from core.gdpr import delete_user_data, anonymize_user_data, preview_user_data
from core.media import extract_photo_meta
from core.notify import (
    send_message,
    notify_admin_applications,
    admin_user_tag,
    get_user_name,
    get_admin_name,
    detect_user_platform,
    _dialog_link,
)
from core.runtime import platform, get_adapter
from core.users import (
    BOOTSTRAP_ADMIN_IDS,
    BOOTSTRAP_ADMIN_IDS_BY_PLATFORM,
    is_admin_user,
    add_dynamic_admin, remove_dynamic_admin, list_admins,
    get_admin_settings, set_admin_setting,
    ban_user, unban_user,
)
from core.storage import (
    user_state,
    save_message_to_db, get_history_for_user,
    extract_photo_urls,
)
from core.keyboards import (
    build_keyboard, build_inline_keyboard,
    text_button,
    get_empty_keyboard, get_confirm_keyboard, get_main_keyboard,
    get_question_keyboard, get_next_action_keyboard,
    get_rejection_keyboard, get_after_submit_inline_keyboard,
    get_admin_menu_keyboard, get_admin_categories_keyboard,
    get_admin_settings_keyboard, get_admin_status_keyboard,
    get_admin_apps_keyboard, get_admin_incomplete_keyboard,
    get_admin_cancel_banner_keyboard, get_admin_cancel_reply_keyboard,
    get_admin_banned_keyboard,
    get_admin_gdpr_requests_keyboard,
)
from core.texts import (
    BOT_VERSION, CHANGELOG_SHORT, CATEGORY_LABELS, STEP_LABELS,
    STATUS_LABELS, STATUS_VALUES, MAX_TEXT_LENGTH, MAX_BANNER_SIZE,
    REGISTRATION_STATES,
    STATE_START, STATE_LOCATION, STATE_PLACEMENT_LOCATION, STATE_USER_TYPE,
    STATE_ORG_PHONE, STATE_ORG_INFO, STATE_NAME_AGE, STATE_CHILD_PHOTO,
    STATE_FORMAT, STATE_DRAWINGS, STATE_TRAINING, STATE_PAYMENT,
    STATE_CONFIRM, STATE_EXTRAS, STATE_NEXT_ACTION, STATE_FINAL,
    STATE_QUESTION, STATE_ADMIN, STATE_ADMIN_SEND_BANNER,
    STATE_ADMIN_REPLY, STATE_ADMIN_REJECT_REASON,
    get_status_label, get_display_status,
)
# ФИКС отступов: убран дублирующий импорт _fire_and_forget.
from core.utils import _fire_and_forget, as_positive_int

logger = logging.getLogger("bright_city_bot")


# ============================================================
#  BOOTSTRAP-АДМИН В ТЕКУЩЕЙ ПЛАТФОРМЕ
# ============================================================

def _is_bootstrap_admin_here(admin_id) -> bool:
    """Bootstrap-админ ИМЕННО в этой платформе."""
    return admin_id in BOOTSTRAP_ADMIN_IDS_BY_PLATFORM.get(platform(), ())


def kick_out_non_admin(user_id, reason: str = "") -> None:
    """Выкидывает не-админа из админ-состояния в ПОЛЬЗОВАТЕЛЬСКОЕ меню."""
    logger.warning(
        "kick_out_non_admin: user=%s platform=%s reason=%r",
        user_id, platform(), reason)
    from core.registration import handle_start
    handle_start(user_id)


# ============================================================
#  МЕНЮ И КАТЕГОРИИ
# ============================================================

def release_current_app_if_any(admin_id):
    view, ctx = get_admin_view(admin_id)
    if view in ("app_detail", "send_banner", "reply", "reject_reason"):
        app_id = ctx.get("app_id")
        if app_id:
            clear_opened_by_if_new(app_id, admin_id)


def show_admin_menu(user_id):
    release_current_app_if_any(user_id)
    user_state.set(user_id, STATE_ADMIN)
    set_admin_view(user_id, "menu")
    has_pending = False
    try:
        has_pending = count_pending_gdpr_requests() > 0
    except Exception as e:
        logger.warning("count_pending_gdpr_requests failed: %s", e)
    send_message(user_id,
        f"🔧 Админ-панель (v{BOT_VERSION})\n\n"
        "Выберите действие кнопкой ниже или используйте текстовые команды.\n\n"

        "📋 ЗАЯВКИ\n"
        "• /заявка_5 — подробности заявки #5\n"
        "• /пользователь 123456 — все заявки пользователя (все платформы)\n"
        "• /статус 5 в обработке — сменить статус вручную\n"
        "• /поиск Иванов Иван — поиск по ФИО / телефону / локации\n"
        "• /статистика — сводная статистика по заявкам\n"
        "• /ответ 123456 текст — ответить пользователю\n"
        "   (если открыта заявка — ID обязан совпадать с её владельцем)\n"
        "• /история 123456 — переписка с пользователем\n\n"

        "👤 АДМИНЫ И ПОЛЬЗОВАТЕЛИ\n"
        "• /админы — список активных админов\n"
        "• /добавить_админа 123456 — назначить админа (bootstrap)\n"
        "• /удалить_админа 123456 — снять админа (bootstrap)\n"
        "• /бан <vk|tg|max> <id> [причина] — заблокировать\n"
        "• /разбан <vk|tg|max> <id> — разблокировать\n"
        "   ⚠ Платформа обязательна: числовые ID vk / tg / max —\n"
        "   независимые пространства, одно число ≠ один человек.\n\n"

        "🔐 GDPR / 152-ФЗ (только bootstrap-админы)\n"
        "• /gdpr_запрос <platform> <user_id> — зафиксировать запрос ПДн вручную\n"
        "• /удалить_данные <platform> <user_id> [anon] — удалить / анонимизировать\n"
        "• /gdpr_статус <platform> <user_id> [--full] — что известно о субъекте\n"
        "• /gdpr_лог [N] — журнал GDPR-операций\n"
        "   📥 Кнопка «Запросы на удаление ПДн» появляется в меню, когда\n"
        "   есть необработанные запросы пользователей. Клик по 📥 показывает\n"
        "   список с inline-кнопками 🗑 (удалить) / 🧊 (анонимизировать).\n"
        "   Уведомление о новом запросе автоматически уходит админам,\n"
        "   у которых включена настройка «Уведомления о заявках».\n\n"

        "⚠ ОПАСНЫЕ ОПЕРАЦИИ\n"
        "Требуют подтверждения через inline-кнопки:\n"
        "  /бан, /разбан, /удалить_данные, /добавить_админа,\n"
        "  /удалить_админа, отклонение заявки.\n\n"

        "ℹ️ ПРОЧЕЕ\n"
        "• /changelog — что нового в текущей версии\n"
        "• /start — выйти в пользовательское меню\n"
        "• ← Выйти из админ-режима — кнопка внизу меню",
        keyboard=get_admin_menu_keyboard(has_pending_gdpr=has_pending))


def show_admin_categories(user_id):
    release_current_app_if_any(user_id)
    user_state.set(user_id, STATE_ADMIN)
    set_admin_view(user_id, "categories")
    send_message(user_id, "📋 Все заявки\n\nВыберите категорию:",
                 keyboard=get_admin_categories_keyboard())


def show_admin_settings(user_id):
    release_current_app_if_any(user_id)
    user_state.set(user_id, STATE_ADMIN)
    set_admin_view(user_id, "settings")
    settings = get_admin_settings(user_id)
    notify_status = "✅ ВКЛ" if settings["notify_applications"] else "❌ ВЫКЛ"
    receive_status = "✅ ВКЛ" if settings["receive_user_messages"] else "❌ ВЫКЛ"
    media_status = ("✅ ВКЛ"
                    if settings.get("show_media_in_applications", True)
                    else "❌ ВЫКЛ")
    send_message(user_id,
        f"⚙️ Ваши настройки\n\n"
        f"🔔 Уведомления о заявках: {notify_status}\n"
        f"   (новые заявки, смена статусов)\n\n"
        f"📩 Приём сообщений от пользователей: {receive_status}\n"
        f"   (пересылка вопросов и сообщений)\n\n"
        f"📸 Медиа в карточках заявок: {media_status}\n"
        f"   (фото ребёнка, рисунки, квитанции, баннер)\n"
        f"   Влияет только на ваши карточки — у других админов своя настройка.\n\n"
        f"Нажмите кнопку, чтобы переключить.",
        keyboard=get_admin_settings_keyboard(user_id))


def toggle_admin_setting(user_id, setting_name):
    settings = get_admin_settings(user_id)
    set_admin_setting(user_id, setting_name, not settings.get(setting_name, True))
    show_admin_settings(user_id)


def show_admin_category_list(user_id, category, page=0):
    if category == "incomplete":
        show_admin_incomplete_list(user_id, page)
        return
    release_current_app_if_any(user_id)
    where_clause, params = get_category_filter(category, user_id)
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(sql.SQL("""SELECT id, user_id, location, user_type, name_age, phone, status, banner_url, created_at,
                assigned_admin_name, opened_by_admin_name, status_changed_by_name, status_changed_at,
                rejection_reason
                FROM bright_city_applications WHERE {} ORDER BY created_at ASC LIMIT 50""").format(where_clause), params)
            rows = cur.fetchall()
    except Exception as e:
        logger.error("DB error: %s", e)
        send_message(user_id, "Ошибка БД. Попробуйте позже.")
        return
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)
    cat_label = CATEGORY_LABELS.get(category, category)
    empty_messages = {"new": "Новых заявок нет.", "in_progress": "Заявок на обработке нет.",
        "rejected": "Отклонённых заявок нет.", "waiting_banner": "Заявок, ожидающих отправки макета, нет.",
        "completed": "Выполненных заявок нет."}
    if not rows:
        user_state.set(user_id, STATE_ADMIN)
        set_admin_view(user_id, "categories")
        send_message(user_id, empty_messages.get(category, "Заявок нет."),
                     keyboard=get_admin_categories_keyboard())
        return
    if len(rows) == 1:
        app_id = rows[0][0]
        user_state.set(user_id, STATE_ADMIN)
        set_admin_view(user_id, "app_detail", app_id=app_id,
                       from_view="category_list",
                       from_ctx={"category": category, "page": 0, "skip_list": True})
        handle_admin_application_detail(user_id, app_id)
        return
    user_state.set(user_id, STATE_ADMIN)
    set_admin_view(user_id, "category_list", category=category, page=page)
    app_entries = [{"id": r[0], "ts": (r[8].strftime("%d.%m %H:%M") if r[8] else "—")} for r in rows]
    user_state.update_temp(user_id, {_entries_key("category_list"): app_entries})
    per_page = 10
    start = page * per_page
    end = min(start + per_page, len(rows))
    lines = [f"{cat_label} (страница {page + 1}):\n"]
    for row in rows[start:end]:
        (app_id, uid, location, user_type, name_age, phone, status, banner_url, created_at,
         assigned_name, opened_name, changed_by_name, changed_at,
         rejection_reason) = row
        timestamp = created_at.strftime("%d.%m.%Y %H:%M")
        short_name = (name_age or "—")[:30]
        status_label = get_display_status(status, banner_url)
        line = f"#{app_id} | {timestamp} | {status_label}\n  {short_name} | {user_type or '—'} | {location or '—'}\n  ☎ {phone or '—'}"
        assigned_name_row = assigned_name  # уже в кортеже row
        if assigned_name_row and status == "in_progress":
            # Определяем «мой ли это» — но у нас нет admin_id в строке,
            # поэтому проще: если assigned_admin_name совпадает с
            # get_admin_name(admin_id) — без метки, иначе — 🔒.
            my_name = get_admin_name(admin_id)
            if assigned_name_row != my_name:
                line += f"\n  🔒 В работе у: {assigned_name_row}"
        if rejection_reason:
            line += f"\n  ❌ Причина: {rejection_reason}"
        if changed_by_name and changed_at:
            line += f"\n  ✏️ Статус: {changed_by_name} ({changed_at.strftime('%d.%m.%Y %H:%M')})"
        lines.append(line)
    lines.append("\n👇 Нажмите кнопку, чтобы открыть заявку:")
    send_message(user_id, "\n".join(lines),
                 keyboard=get_admin_apps_keyboard(app_entries, page=page, per_page=per_page))


def show_admin_incomplete_list(user_id, page=0):
    release_current_app_if_any(user_id)
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            incomplete_states = list(REGISTRATION_STATES) + [STATE_QUESTION]
            cur.execute("""SELECT user_id, state, temp_data, updated_at, platform
                FROM bot_user_states
                WHERE state = ANY(%s)
                ORDER BY updated_at ASC LIMIT 50""", (incomplete_states,))
            rows = cur.fetchall()
    except Exception as e:
        logger.error("DB error (incomplete): %s", e)
        send_message(user_id, "Ошибка БД. Попробуйте позже.")
        return
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)
    if not rows:
        user_state.set(user_id, STATE_ADMIN)
        set_admin_view(user_id, "categories")
        send_message(user_id, "Незавершённых заявок нет.",
                     keyboard=get_admin_categories_keyboard())
        return
    if len(rows) == 1:
        target_uid = rows[0][0]
        user_state.set(user_id, STATE_ADMIN)
        set_admin_view(user_id, "incomplete_detail", target_uid=target_uid, skip_list=True)
        handle_admin_incomplete_detail(user_id, target_uid)
        return
    user_state.set(user_id, STATE_ADMIN)
    set_admin_view(user_id, "incomplete_list", page=page)
    app_entries = []
    for row in rows:
        uid, state, _, updated_at, row_platform = row
        ts = updated_at.strftime("%d.%m %H:%M") if updated_at else "—"
        app_entries.append({"id": uid, "ts": ts, "state": state,
                            "platform": row_platform})
    user_state.update_temp(user_id, {_entries_key("incomplete_list"): app_entries})
    per_page = 10
    start = page * per_page
    end = min(start + per_page, len(app_entries))
    lines = [f"📝 Не завершённые (страница {page + 1}):\n"]
    for entry in app_entries[start:end]:
        uid = entry["id"]
        state = entry.get("state", "")
        step_label = STEP_LABELS.get(state, state)
        first, last = get_user_name(uid)
        ts = entry["ts"]
        pl_tag = ""
        if entry.get("platform") and entry["platform"] != platform():
            pl_tag = f" [{entry['platform'].upper()}]"
        lines.append(f"ID:{uid}{pl_tag} | {ts} | {step_label}\n  {first} {last}")
    send_message(user_id, "\n".join(lines),
                 keyboard=get_admin_incomplete_keyboard(app_entries, page=page, per_page=per_page))


BANNED_PAGE_SIZE = 5


def show_banned_list(admin_id, page=0):
    """Показывает список активных забаненных пользователей."""
    release_current_app_if_any(admin_id)
    user_state.set(admin_id, STATE_ADMIN)
    set_admin_view(admin_id, "banned_list", page=page)

    total = count_banned_users()
    if total == 0:
        set_admin_view(admin_id, "menu")
        send_message(admin_id, "🚫 Забаненных пользователей нет.",
                     keyboard=get_admin_menu_keyboard())
        return

    per_page = BANNED_PAGE_SIZE
    total_pages = (total + per_page - 1) // per_page
    page = max(0, min(page, total_pages - 1))

    rows = get_banned_users_list(
        platform_name=None,
        limit=per_page,
        offset=page * per_page,
    )

    lines = [
        f"🚫 Забаненные пользователи\n"
        f"Всего: {total} | страница {page + 1} из {total_pages}\n"
    ]
    entries = []
    for (platform_name, user_id, banned_by, banned_by_pl,
         banned_at, expires_at, reason, ban_count) in rows:
        try:
            first, last = get_user_name(user_id)
            name = (f"{first} {last}").strip() or f"(ID {user_id})"
        except Exception:
            name = f"(ID {user_id})"

        until = "навсегда" if expires_at is None else expires_at.strftime("%d.%m.%Y %H:%M")
        banned_ts = banned_at.strftime("%d.%m.%Y %H:%M") if banned_at else "—"
        reason_txt = f"\n     Причина: {reason}" if reason else ""
        ban_count_txt = f"\n     Банов: {ban_count}" if ban_count and ban_count > 1 else ""
        banned_by_txt = ""
        if banned_by:
            banned_by_txt = f"\n     Забанил: {banned_by}"
            if banned_by_pl and banned_by_pl != platform_name:
                banned_by_txt += f" (из {banned_by_pl.upper()})"

        lines.append(
            f"• {platform_name.upper()}:{user_id} — {name}\n"
            f"     Забанен: {banned_ts}, до: {until}"
            f"{reason_txt}{ban_count_txt}{banned_by_txt}"
        )
        entries.append({
            "user_id": user_id,
            "platform": platform_name,
        })

    lines.append("\n👇 Нажмите кнопку, чтобы разбанить:")
    keyboard = get_admin_banned_keyboard(entries, page=page, per_page=per_page)
    send_message(admin_id, "\n".join(lines), keyboard=keyboard)


def show_admin_gdpr_requests(admin_id, page=0):
    """Список необработанных GDPR-запросов с inline-кнопками."""
    if not _is_bootstrap_admin_here(admin_id):
        send_message(admin_id, "⛔ Только bootstrap-админ.")
        show_admin_menu(admin_id)
        return

    release_current_app_if_any(admin_id)
    user_state.set(admin_id, STATE_ADMIN)
    set_admin_view(admin_id, "gdpr_requests", page=page)

    rows = list_pending_gdpr_requests(limit=100)
    if not rows:
        send_message(admin_id, "✅ Необработанных запросов нет.",
                     keyboard=get_admin_menu_keyboard(has_pending_gdpr=False))
        return

    per_page = 10
    total = len(rows)
    total_pages = (total + per_page - 1) // per_page
    page = max(0, min(page, total_pages - 1))
    start = page * per_page
    end = min(start + per_page, total)

    lines = [
        f"📥 Запросы на удаление ПДн "
        f"(всего {total}, страница {page + 1} из {total_pages}):\n"
    ]
    entries = []
    for rid, pl, uid, req_at in rows[start:end]:
        ts = req_at.strftime("%d.%m.%Y %H:%M") if req_at else "—"
        lines.append(f"#{rid} | {pl.upper()}:{uid} | {ts}")
        entries.append({"id": rid, "platform": pl, "user_id": uid})

    lines.append("")
    lines.append("🗑 — удалить, 🧊 — анонимизировать. "
                 "Потребуется подтверждение через inline-кнопки.")

    send_message(
        admin_id, "\n".join(lines),
        keyboard=get_admin_gdpr_requests_keyboard(entries, page=page),
    )


def handle_admin_gdpr_request_action(admin_id, request_id, mode):
    """Создаёт sudo-запрос на удаление/анонимизацию по конкретному
    GDPR-запросу. mode = 'delete' | 'anon'."""
    if not _is_bootstrap_admin_here(admin_id):
        send_message(admin_id, "⛔ Только bootstrap-админ.")
        return

    row = get_gdpr_request_by_id(request_id)
    if not row:
        send_message(
            admin_id,
            f"⚠ Запрос #{request_id} уже обработан или не найден.")
        view, _ = get_admin_view(admin_id)
        if view == "gdpr_requests":
            show_admin_gdpr_requests(admin_id, page=0)
        return

    target_platform, target_user_id = row
    mode_text = "УДАЛИТЬ" if mode == "delete" else "анонимизировать"
    desc = (
        f"{mode_text} данные пользователя?\n\n"
        f"Платформа: {target_platform}\n"
        f"User ID: {target_user_id}\n"
        f"Запрос #{request_id}\n\n"
        f"⚠ Действие необратимо."
    )
    create_sudo_request(
        admin_id,
        action=f"gdpr_{mode}",
        description=desc,
        target=f"{target_platform}:{target_user_id}",
        # ФИКС Баг-19: источник — клик по inline-кнопке в GDPR-списке.
        params={"source": "gdpr_list"},
    )


def handle_admin_banned_unban_prompt(admin_id, target_user_id, target_platform):
    """Запрашивает sudo-подтверждение разбана."""
    conn = None
    row = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT reason FROM banned_users
                WHERE platform = %s AND user_id = %s
                  AND (expires_at IS NULL OR expires_at > NOW())
            """, (target_platform, target_user_id))
            row = cur.fetchone()
    except Exception as e:
        logger.error("DB error (handle_admin_banned_unban_prompt): %s", e)
        send_message(admin_id, "Ошибка БД. Попробуйте позже.")
        return
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)

    if not row:
        send_message(admin_id,
            f"Пользователь {target_platform.upper()}:{target_user_id} "
            f"не забанен (уже разбанен?).")
        show_banned_list(admin_id)
        return

    reason = row[0] or "—"
    desc = (f"Разбанить пользователя?\n\n"
            f"Платформа: {target_platform.upper()}\n"
            f"User ID: {target_user_id}\n"
            f"Причина бана: {reason}")
    create_sudo_request(
        admin_id, "unban", desc,
        target=str(target_user_id),
        params={"platform": target_platform},
    )


def build_incomplete_card(target_user_id):
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT state, temp_data, updated_at, platform "
                "FROM bot_user_states "
                "WHERE user_id = %s",
                (target_user_id,))
            row = cur.fetchone()
    except Exception as e:
        logger.error("DB error (incomplete detail): %s", e)
        return None
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)
    if not row:
        return None
    state, temp_data_raw, updated_at, state_platform = row
    temp_data = {}
    if temp_data_raw:
        if isinstance(temp_data_raw, dict):
            temp_data = temp_data_raw
        elif isinstance(temp_data_raw, str):
            try:
                temp_data = json.loads(temp_data_raw)
            except Exception:
                temp_data = {}
        else:
            try:
                temp_data = json.loads(str(temp_data_raw))
            except Exception:
                temp_data = {}
    step_label = STEP_LABELS.get(state, state)
    first, last = get_user_name(target_user_id)
    lines = [
        "📝 Незавершённая заявка",
        f"Пользователь: {first} {last} (ID: {target_user_id})",
        f"Текущий шаг: {step_label}",
        f"Обновлено: {updated_at.strftime('%d.%m.%Y %H:%M') if updated_at else '—'}",
        "",
    ]
    if temp_data.get("location"):
        lines.append(f"Город: {temp_data['location']}")
    if temp_data.get("placement_location"):
        lines.append(f"Локация: {temp_data['placement_location']}")
    if temp_data.get("user_type"):
        lines.append(f"Тип: {temp_data['user_type']}")
    if temp_data.get("phone"):
        lines.append(f"Телефон: {temp_data['phone']}")
    if temp_data.get("name_age"):
        lines.append(f"Ребёнок: {temp_data['name_age']}")
    if temp_data.get("format_choice"):
        lines.append(f"Формат: {temp_data['format_choice']}")
    if temp_data.get("org_info_text"):
        lines.append(f"О себе: {temp_data['org_info_text']}")
    if temp_data.get("training_info"):
        lines.append(f"Обучение: {temp_data['training_info']}")
    if temp_data.get("child_photo_urls"):
        lines.append(f"Фото ребёнка: {len(temp_data['child_photo_urls'])} шт.")
    if temp_data.get("drawings_urls"):
        lines.append(f"Рисунков: {len(temp_data['drawings_urls'])} шт.")
    lines.append(f"\n🔗 Диалог: {_dialog_link(target_user_id)}")
    lines.append(f"Ответить: /ответ {target_user_id} ваш текст")
    msg_section = build_recent_messages_section(
        target_user_id, target_platform=state_platform)
    if msg_section:
        lines.append(msg_section)
    lines.append("👇 Доступные действия:")
    text = "\n".join(lines)
    if len(text.encode('utf-8')) > 4000:
        encoded = text.encode('utf-8')[:3990]
        text = encoded.decode('utf-8', errors='ignore') + "\n…(обрезано)"
    return (text, state)


def handle_admin_incomplete_detail(admin_id, target_user_id):
    result = build_incomplete_card(target_user_id)
    if result is None:
        send_message(admin_id, f"Пользователь {target_user_id} не найден в активных заявках.")
        return
    text, state = result
    current_view, current_ctx = get_admin_view(admin_id)
    if current_view == "incomplete_detail":
        set_admin_view(admin_id, "incomplete_detail", target_uid=target_user_id,
                       skip_list=current_ctx.get("skip_list", False),
                       page=current_ctx.get("page", 0))
    else:
        set_admin_view(admin_id, "incomplete_detail", target_uid=target_user_id)
    rows = [
        [text_button("⏰ Отправить напоминание", "primary", {"action": "admin_remind", "user_id": target_user_id}),
         text_button("💬 Написать", "primary", {"action": "admin_start_reply_incomplete", "user_id": target_user_id})],
        [text_button("← Назад", "secondary", {"action": "admin_back"})],
    ]
    send_message(admin_id, text, keyboard=build_keyboard(rows, one_time=True))


def send_reminder_to_user(admin_id, target_user_id):
    # ФИКС Баг-60: без exclude_platform — иначе MAX-админ не может
    # напомнить MAX-пользователю, TG-админ — TG, VK-админ — VK.
    # Пользователь «Незавершённых» находится в bot_user_states на
    # ТЕКУЩЕЙ платформе, значит exclude_platform=platform() всегда
    # давал None, и напоминания внутри одной платформы были сломаны.
    detected = detect_user_platform(target_user_id)
    if not detected:
        send_message(admin_id,
            f"⚠ Не удалось определить платформу получателя "
            f"{target_user_id} (нет записей или несколько платформ). "
            f"Напоминание не отправлено.")
        return
    target_platform = detected

    # ФИКС Баг-36: повторно валидируем, что пользователь всё ещё в
    # незавершённом состоянии. Карточка «Не завершённые» могла висеть
    # у админа сутки: за это время пользователь мог завершить заявку,
    # уйти в STATE_START, получить GDPR-удаление (тогда запись в
    # bot_user_states удалена, _get_user_state_for_platform вернёт
    # STATE_START) или продолжить заполнение сам. Шлём «вы не
    # завершили заполнение заявки» только тем, кто реально не завершил —
    # иначе путаем и злим.
    target_state = _get_user_state_for_platform(target_user_id, target_platform)
    # Разрешённые состояния для напоминания:
    #   - REGISTRATION_STATES (все шаги регистрации);
    #   - STATE_QUESTION (пользователь задаёт вопрос по ходу).
    # Прочее (start, next_action, extras, final, admin*) — напоминание
    # неуместно.
    if target_state not in REGISTRATION_STATES and target_state != STATE_QUESTION:
        state_label = STEP_LABELS.get(target_state, target_state or "—")
        send_message(admin_id,
            f"ℹ️ Пользователь {target_user_id} уже не в процессе "
            f"заполнения (текущее состояние: {state_label}).\n\n"
            f"Напоминание не отправлено.")
        handle_admin_incomplete_detail(admin_id, target_user_id)
        return

    reminder_text = ("Вы не завершили заполнение заявки. "
                     "Если у вас есть вопросы по заполнению данных, "
                     "то напишите ваш вопрос текстом прямо здесь в чат. "
                     "Администратор ответит в течении дня.")
    success = send_message(target_user_id, reminder_text,
                           target_platform=target_platform)
    if success:
        save_message_to_db(target_user_id, "out", reminder_text,
                           target_platform=target_platform)
        send_message(admin_id, f"⏰ Напоминание отправлено пользователю {target_user_id}.")
    else:
        send_message(admin_id, f"⚠ Не удалось отправить напоминание пользователю {target_user_id} (все попытки исчерпаны).")
    handle_admin_incomplete_detail(admin_id, target_user_id)


# ============================================================
#  ADMIN VIEW
# ============================================================

def set_admin_view(admin_id, view, **kwargs):
    """Сохраняет view + контекст для админа `admin_id`."""
    user_state.update_temp(admin_id, {"admin_view": view, "admin_view_ctx": kwargs})


def get_admin_view(admin_id):
    temp = user_state.get_temp(admin_id)
    return temp.get("admin_view", "menu"), temp.get("admin_view_ctx", {})


# ============================================================
#  СТАТУСЫ: claim, история, transitions
# ============================================================

def _log_status_change_with_cursor(cur, app_id, old_status, new_status, admin_id, admin_name):
    cur.execute("""INSERT INTO application_status_history
        (application_id, old_status, new_status, admin_id, admin_name,
         admin_platform, changed_at)
        VALUES (%s, %s, %s, %s, %s, %s, NOW())""",
        (app_id, old_status, new_status, admin_id, admin_name, platform()))
    cur.execute("UPDATE bright_city_applications SET status_changed_by_name = %s, status_changed_at = NOW() WHERE id = %s",
                (admin_name, app_id))
    if old_status == "new":
        cur.execute("""UPDATE bright_city_applications SET assigned_admin_id = %s, assigned_admin_name = %s,
            opened_by_admin_id = NULL, opened_by_admin_name = NULL, opened_at = NULL WHERE id = %s""",
            (admin_id, admin_name, app_id))
    elif old_status == "rejected" and new_status == "in_progress":
        cur.execute("""UPDATE bright_city_applications SET assigned_admin_id = %s, assigned_admin_name = %s,
            opened_by_admin_id = NULL, opened_by_admin_name = NULL, opened_at = NULL WHERE id = %s""",
            (admin_id, admin_name, app_id))


def get_status_history(app_id):
    conn = None
    rows = []
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT old_status, new_status, admin_name, changed_at FROM application_status_history WHERE application_id = %s ORDER BY changed_at ASC", (app_id,))
            rows = cur.fetchall()
    except Exception as e:
        logger.error("DB error (get status history): %s", e)
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)
    return rows


def check_and_claim_app(app_id, admin_id, admin_name):
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""SELECT status, assigned_admin_id, assigned_admin_name,
                opened_by_admin_id, opened_by_admin_name, banner_url, opened_at, created_at, status_changed_at
                FROM bright_city_applications WHERE id = %s FOR UPDATE""", (app_id,))
            row = cur.fetchone()
            if not row:
                conn.rollback()
                return None, None
            (status, assigned_id, assigned_name, opened_id, opened_name,
             banner_url, opened_at, created_at, status_changed_at) = row

            if status == "done" and banner_url:
                conn.rollback()
                return True, None

            if status == "new":
                if opened_id is not None and opened_id != admin_id:
                    if opened_at is not None:
                        cur.execute("SELECT EXTRACT(EPOCH FROM (NOW() - %s))", (opened_at,))
                        elapsed = cur.fetchone()[0]
                        if elapsed < 300:
                            conn.rollback()
                            return False, opened_name
                    else:
                        logger.warning(
                            "opened_at is NULL for app #%s (opened_by_admin_id=%s), "
                            "conservatively denying claim", app_id, opened_id)
                        conn.rollback()
                        return False, opened_name
                    cur.execute(
                        "UPDATE bright_city_applications "
                        "SET opened_by_admin_id = %s, opened_by_admin_name = %s, "
                        "    opened_at = NOW() WHERE id = %s",
                        (admin_id, admin_name, app_id))
                elif opened_id is None:
                    cur.execute(
                        "UPDATE bright_city_applications "
                        "SET opened_by_admin_id = %s, opened_by_admin_name = %s, "
                        "    opened_at = NOW() WHERE id = %s",
                        (admin_id, admin_name, app_id))
                else:
                    cur.execute(
                        "UPDATE bright_city_applications SET opened_at = NOW() "
                        "WHERE id = %s AND opened_by_admin_id = %s",
                        (app_id, admin_id))
                conn.commit()
                return True, None

            if status == "done" and not banner_url:
                if assigned_id is not None and assigned_id != admin_id:
                    timeout_ts = status_changed_at or created_at
                    if timeout_ts is not None:
                        cur.execute("SELECT EXTRACT(EPOCH FROM (NOW() - %s))", (timeout_ts,))
                        elapsed = cur.fetchone()[0]
                        if elapsed < 300:
                            conn.rollback()
                            return False, assigned_name
                    cur.execute(
                        """UPDATE bright_city_applications
                              SET assigned_admin_id = %s, assigned_admin_name = %s,
                                  opened_by_admin_id = NULL, opened_by_admin_name = NULL,
                                  opened_at = NULL
                            WHERE id = %s""",
                        (admin_id, admin_name, app_id))
                    conn.commit()
                elif assigned_id is None:
                    cur.execute(
                        """UPDATE bright_city_applications
                              SET assigned_admin_id = %s, assigned_admin_name = %s,
                                  opened_by_admin_id = NULL, opened_by_admin_name = NULL,
                                  opened_at = NULL
                            WHERE id = %s""",
                        (admin_id, admin_name, app_id))
                    conn.commit()
                else:
                    conn.rollback()
                return True, None

            # Любой другой статус (in_progress / rejected).
            if assigned_id is not None and assigned_id != admin_id:
                # ФИКС: разрешаем перехват, если заявка «зависла»
                # у другого админа дольше _IN_PROGRESS_STALE_SEC.
                timeout_ts = status_changed_at or created_at
                stale = False
                if timeout_ts is not None:
                    cur.execute("SELECT EXTRACT(EPOCH FROM (NOW() - %s))", (timeout_ts,))
                    elapsed = cur.fetchone()[0]
                    stale = elapsed >= _IN_PROGRESS_STALE_SEC
                if not stale:
                    conn.rollback()
                    return False, assigned_name
                cur.execute(
                    """UPDATE bright_city_applications
                          SET assigned_admin_id = %s, assigned_admin_name = %s,
                              opened_by_admin_id = NULL, opened_by_admin_name = NULL,
                              opened_at = NULL
                        WHERE id = %s""",
                    (admin_id, admin_name, app_id))
                conn.commit()
                return True, None

            if assigned_id is None:
                cur.execute(
                    """UPDATE bright_city_applications
                          SET assigned_admin_id = %s, assigned_admin_name = %s,
                              opened_by_admin_id = NULL, opened_by_admin_name = NULL,
                              opened_at = NULL
                        WHERE id = %s""",
                    (admin_id, admin_name, app_id))
                conn.commit()
            else:
                conn.rollback()
            return True, None
    except Exception as e:
        logger.error("DB error (check and claim): %s", e)
        return None, None
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)


def clear_opened_by_if_new(app_id, admin_id):
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT status, opened_by_admin_id FROM bright_city_applications WHERE id = %s FOR UPDATE", (app_id,))
            row = cur.fetchone()
            if row and row[0] == "new" and row[1] == admin_id:
                cur.execute("UPDATE bright_city_applications SET opened_by_admin_id = NULL, opened_by_admin_name = NULL, opened_at = NULL WHERE id = %s", (app_id,))
                conn.commit()
    except Exception as e:
        logger.error("DB error (clear opened): %s", e)
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)


def get_category_filter(category, admin_id):
    if category == "incomplete":
        return None, []
    elif category == "new":
        return sql.SQL("status = 'new'"), []
    elif category == "in_progress":
        return sql.SQL("status = 'in_progress'"), []
    elif category == "rejected":
        # ФИКС: раньше стояло "AND assigned_admin_id = %s", поэтому
        # админ видел только те отклонённые заявки, которые сам
        # обрабатывал. В веб-админке фильтра по assigned_admin_id нет,
        # и там видны все. Теперь единообразно — видим все отклонённые.
        return sql.SQL("status = 'rejected'"), []
    elif category == "waiting_banner":
        return sql.SQL("status = 'done' AND banner_url IS NULL"), []
    elif category == "completed":
        return sql.SQL("status = 'done' AND banner_url IS NOT NULL"), []
    else:
        logger.warning("get_category_filter: неизвестная категория %r", category)
        return sql.SQL("FALSE"), []


# ============================================================
#  ADMIN BACK STATE MACHINE
# ============================================================

def _back_from_menu(user_id, ctx):
    user_state.clear(user_id)
    from core.registration import handle_start
    handle_start(user_id)


def _back_to_admin_menu(user_id, ctx):
    show_admin_menu(user_id)


def _back_to_categories(user_id, ctx):
    show_admin_categories(user_id)


def _back_from_app_detail(user_id, ctx):
    app_id = ctx.get("app_id")
    if app_id:
        clear_opened_by_if_new(app_id, user_id)
    from_view = ctx.get("from_view", "categories")
    from_ctx = ctx.get("from_ctx", {})
    handler = _BACK_FROM_APP_DETAIL.get(from_view, _back_to_categories)
    handler(user_id, from_ctx)


def _back_to_category_list(user_id, ctx):
    if ctx.get("skip_list", False):
        show_admin_categories(user_id)
    else:
        show_admin_category_list(user_id, ctx.get("category", "new"), ctx.get("page", 0))


def _back_to_search_list(user_id, ctx):
    if ctx.get("skip_list", False):
        show_admin_menu(user_id)
    else:
        handle_admin_search_text(user_id, ctx.get("query", ""), ctx.get("page", 0))


def _back_to_user_list(user_id, ctx):
    if ctx.get("skip_list", False):
        show_admin_menu(user_id)
    else:
        target_uid = ctx.get("target_uid")
        if target_uid is None:
            logger.warning("_back_to_user_list: target_uid is None, returning to admin menu")
            show_admin_menu(user_id)
        else:
            handle_admin_user_apps_text(user_id, target_uid, ctx.get("page", 0))


_BACK_FROM_APP_DETAIL = {
    "category_list": _back_to_category_list,
    "search_list":   _back_to_search_list,
    "user_list":     _back_to_user_list,
}


def _back_from_incomplete_detail(user_id, ctx):
    if ctx.get("skip_list", False):
        show_admin_categories(user_id)
    else:
        show_admin_incomplete_list(user_id, ctx.get("page", 0))


def _back_from_reject_reason(user_id, ctx):
    _cancel_all_sudo_for_admin(user_id)
    app_id = ctx.get("app_id")
    from_view = ctx.get("from_view", "categories")
    from_ctx = ctx.get("from_ctx", {})
    if app_id:
        set_admin_view(user_id, "app_detail", app_id=app_id,
                       user_id=ctx.get("user_id"),
                       from_view=from_view, from_ctx=from_ctx)
        handle_admin_application_detail(user_id, app_id)
    else:
        show_admin_menu(user_id)


def _back_from_send_banner_or_reply(user_id, ctx):
    app_id = ctx.get("app_id")
    target_user_id = ctx.get("user_id")
    from_view = ctx.get("from_view", "categories")
    from_ctx = ctx.get("from_ctx", {})
    if app_id:
        set_admin_view(user_id, "app_detail", app_id=app_id,
                       user_id=target_user_id,
                       from_view=from_view, from_ctx=from_ctx)
        handle_admin_application_detail(user_id, app_id)
    elif from_view == "incomplete_detail":
        target_uid = from_ctx.get("target_uid")
        if target_uid:
            set_admin_view(user_id, "incomplete_detail", target_uid=target_uid,
                           **{k: v for k, v in from_ctx.items() if k != "target_uid"})
            handle_admin_incomplete_detail(user_id, target_uid)
        else:
            show_admin_menu(user_id)
    else:
        show_admin_menu(user_id)


_ADMIN_BACK_SM = {
    "menu":              _back_from_menu,
    "app_detail":        _back_from_app_detail,
    "category_list":     _back_to_categories,
    "incomplete_list":   _back_to_categories,
    "incomplete_detail": _back_from_incomplete_detail,
    "categories":        _back_to_admin_menu,
    "settings":          _back_to_admin_menu,
    "search_list":       _back_to_admin_menu,
    "user_list":         _back_to_admin_menu,
    "banned_list":       _back_to_admin_menu,
    "reject_reason":     _back_from_reject_reason,
    "send_banner":       _back_from_send_banner_or_reply,
    "reply":             _back_from_send_banner_or_reply,
    "gdpr_requests":     _back_to_admin_menu,
}


def handle_admin_back(user_id):
    view, ctx = get_admin_view(user_id)
    handler = _ADMIN_BACK_SM.get(view, _back_to_admin_menu)
    if view != "menu":
        user_state.set(user_id, STATE_ADMIN)
    handler(user_id, ctx)


# ============================================================
#  КАРТОЧКА ЗАЯВКИ
# ============================================================


def build_recent_messages_section(user_id, limit=5, since=None,
                                  application_id=None, target_platform=None):
    rows = get_history_for_user(
        user_id, limit=limit, since=since,
        application_id=application_id, target_platform=target_platform)
    if not rows:
        return ""
    lines = ["💬 Последние сообщения:"]
    for direction, msg_text, created_at in rows:
        ts = created_at.strftime("%d.%m %H:%M") if created_at else ""
        if direction == "in":
            lines.append(f"  [{ts}] ← Пользователь: {msg_text[:200]}")
        else:
            lines.append(f"  [{ts}] → Админ: {msg_text[:200]}")
    lines.append("")
    return "\n".join(lines) + "\n"


_MAX_MEDIA_SOURCE_LOOKUP = 128


def _get_media_source_map(urls):
    """Возвращает map {url: {...}} по media_files.

    ФИКС Баг-33: при дубликате url среди РАЗНЫХ (user_platform, user_id)
    берём ПЕРВУЮ строку и логируем коллизию.

    ФИКС Баг-43: альтернативные строки (collision) не выбрасываем, а
    кладём в result[url]["alternatives"]. Если основная строка имеет
    kind='photo', а альтернативная — kind='doc', вызывающий может
    попробовать оба варианта. Раньше альтернативы терялись, и при
    попытке отправить документ под photo-kind мы бы получили
    send_photo на PDF.
    """
    if not urls:
        return {}
    if len(urls) > _MAX_MEDIA_SOURCE_LOOKUP:
        logger.warning(
            "_get_media_source_map: URLs больше лимита %d (получено %d) — "
            "лишние не получат метаданные, админу придёт "
            "«недоступно для пересылки»",
            _MAX_MEDIA_SOURCE_LOOKUP, len(urls))
    urls = list(urls)[:_MAX_MEDIA_SOURCE_LOOKUP]
    conn = None
    result = {}
    alternatives = {}   # url -> [entry, ...]
    collisions = []
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT url, source_file_id, source_platform, kind, "
                "       owner_id, photo_id, doc_id, user_platform, user_id, "
                "       vk_url, vk_owner_id, vk_photo_id, vk_doc_id "
                "FROM media_files WHERE url = ANY(%s) "
                "ORDER BY id ASC",
                (urls,))
            for (u, src_id, src_pl, kind, oid, pid, did,
                 row_pl, row_uid,
                 vk_url, vk_oid, vk_pid, vk_did) in cur.fetchall():
                entry = {
                    "source_file_id": src_id,
                    "source_platform": src_pl,
                    "kind": kind or "",
                    "owner_id": oid,
                    "photo_id": pid,
                    "doc_id": did,
                    "vk_url": vk_url,
                    "vk_owner_id": vk_oid,
                    "vk_photo_id": vk_pid,
                    "vk_doc_id": vk_did,
                }
                if u in result:
                    alternatives.setdefault(u, []).append(entry)
                    collisions.append((u, row_pl, row_uid))
                    continue
                result[u] = entry
    except Exception as e:
        logger.error("DB error (_get_media_source_map): %s", e)
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)

    # ФИКС Баг-43: альтернативы — атрибутом записи. Вызывающий код
    # (_send_media_items_async) может попробовать их, если основная
    # строка не дала рабочего кандидата.
    for u, alts in alternatives.items():
        if u in result:
            result[u]["alternatives"] = alts

    if collisions:
        logger.warning(
            "_get_media_source_map: %d дубликатов URL между пользователями "
            "(основная — с минимальным id, остальные в alternatives). "
            "Примеры: %s",
            len(collisions), collisions[:5])
    return result


def build_application_card(app_id):
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""SELECT id, user_id, location, placement_location, user_type, phone, name_age,
                child_photo_urls, format_choice, drawings_urls, training_info, payment_doc_urls,
                org_info_attachments, status, banner_url, created_at, assigned_admin_name, opened_by_admin_name,
                status_changed_by_name, status_changed_at, rejection_reason, platform
                FROM bright_city_applications WHERE id = %s""", (app_id,))
            row = cur.fetchone()
    except Exception as e:
        logger.error("DB error: %s", e)
        return None
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)
    if not row:
        return None
    (app_id, user_id, location, placement_location, user_type, phone, name_age,
     child_photo_urls, format_choice, drawings_urls, training_info, payment_doc_urls,
     org_info_attachments, status, banner_url, created_at, assigned_name, opened_name,
     changed_by_name, changed_at, rejection_reason, app_platform) = row
    status_label = get_display_status(status, banner_url)
    is_completed = (status == "done" and banner_url is not None)
    status_dt = changed_at.strftime("%d.%m.%Y %H:%M") if changed_at else ""
    status_line = f"Статус: {status_label}"
    if status_dt:
        status_line += f"  ({status_dt})"

    lines = [f"📋 Заявка #{app_id}", f"Дата: {created_at.strftime('%d.%m.%Y %H:%M')}", status_line]
    if rejection_reason:
        lines.append(f"Причина отклонения: {rejection_reason}")
        if status_dt:
            lines.append(f"  (установлено: {status_dt})")
    lines.extend([
        f"Город: {location or '—'}", f"Локация: {placement_location or '—'}", f"Тип: {user_type or '—'}",
        f"Телефон: {phone or '—'}", f"Ребёнок: {name_age or '—'}", f"Формат: {format_choice or '—'}",
        f"Обучение: {training_info or '—'}", ""])

    media_items = []

    if child_photo_urls:
        if isinstance(child_photo_urls, str):
            try:
                child_photo_urls = json.loads(child_photo_urls)
            except (json.JSONDecodeError, TypeError):
                child_photo_urls = [child_photo_urls]
        if isinstance(child_photo_urls, list) and child_photo_urls:
            total = len(child_photo_urls)
            lines.append(f"📸 Фото ребёнка ({total} шт.):")
            for i, url in enumerate(child_photo_urls, 1):
                lines.append(f"  Фото {i}/{total}")
                media_items.append({
                    "url": url,
                    "label": f"📸 Фото ребёнка {i}/{total} — заявка #{app_id}",
                })
    if drawings_urls:
        if isinstance(drawings_urls, str):
            try:
                drawings_urls = json.loads(drawings_urls)
            except (json.JSONDecodeError, TypeError):
                drawings_urls = [drawings_urls]
        if isinstance(drawings_urls, list) and drawings_urls:
            total = len(drawings_urls)
            lines.append(f"🖼 Рисунки ({total} шт.):")
            for i, url in enumerate(drawings_urls, 1):
                lines.append(f"  Рисунок {i}/{total}")
                media_items.append({
                    "url": url,
                    "label": f"🖼 Рисунок {i}/{total} — заявка #{app_id}",
                })
    if payment_doc_urls:
        if isinstance(payment_doc_urls, str):
            try:
                payment_doc_urls = json.loads(payment_doc_urls)
            except (json.JSONDecodeError, TypeError):
                payment_doc_urls = [payment_doc_urls]
        if isinstance(payment_doc_urls, list) and payment_doc_urls:
            total = len(payment_doc_urls)
            lines.append(f"💳 Квитанции / документы ({total} шт.):")
            for i, url in enumerate(payment_doc_urls, 1):
                lines.append(f"  Квитанция {i}/{total}")
                media_items.append({
                    "url": url,
                    "label": f"💳 Квитанция {i}/{total} — заявка #{app_id}",
                })
    if org_info_attachments:
        if isinstance(org_info_attachments, str):
            try:
                org_info_attachments = json.loads(org_info_attachments)
            except (json.JSONDecodeError, TypeError):
                org_info_attachments = [org_info_attachments]
        if isinstance(org_info_attachments, list) and org_info_attachments:
            total = len(org_info_attachments)
            lines.append(f"📎 Вложения — информация о себе ({total} шт.):")
            for i, url in enumerate(org_info_attachments, 1):
                lines.append(f"  Вложение {i}/{total}")
                media_items.append({
                    "url": url,
                    "label": f"📎 Вложение о себе {i}/{total} — заявка #{app_id}",
                })
    if banner_url:
        lines.append("🎨 Макет баннера: см. фото ниже")
        media_items.append({
            "url": banner_url,
            "label": f"🎨 Макет баннера — заявка #{app_id}",
        })

    lines.append(f"\n🔗 Диалог: {_dialog_link(user_id)}")
    lines.append(f"Все заявки пользователя: /пользователь {user_id}")
    lines.append(f"Ответить: /ответ {user_id} ваш текст")
    history = get_status_history(app_id)
    if history:
        if len(history) > 5:
            display_history = [history[0]] + history[-4:]
        else:
            display_history = history
        lines.append("\n📝 История статусов:")
        for old_st, new_st, adm_name, changed_dt in display_history:
            ts = changed_dt.strftime("%d.%m.%Y %H:%M") if changed_dt else ""
            lines.append(f"  {ts} | {adm_name}: {get_status_label(old_st) if old_st else '—'} → {get_status_label(new_st)}")
    if assigned_name:
        lines.append(f"\n👤 Админ: {assigned_name}")
    if opened_name and not assigned_name:
        lines.append(f"\n👁 Открыл: {opened_name}")
    msg_section = build_recent_messages_section(
        user_id, since=created_at, application_id=app_id,
        target_platform=app_platform)
    if msg_section:
        lines.append(msg_section)
    lines.append("👇 Доступные действия:")
    text = "\n".join(lines)
    if len(text.encode('utf-8')) > 4000:
        encoded = text.encode('utf-8')[:3990]
        text = encoded.decode('utf-8', errors='ignore') + "\n…(обрезано)"
    return (text, status, banner_url, is_completed, user_id, media_items,
            app_platform)


def handle_admin_application_detail(admin_id, app_id):
    admin_name = get_admin_name(admin_id)
    can_view, other_admin = check_and_claim_app(app_id, admin_id, admin_name)
    if can_view is None:
        user_state.set(admin_id, STATE_ADMIN)
        send_message(admin_id, f"❌ Заявка #{app_id} не найдена или удалена.")
        show_admin_menu(admin_id)
        return
    if not can_view:
        user_state.set(admin_id, STATE_ADMIN)
        send_message(admin_id, f"🔒 Над этой заявкой #{app_id} работает админ: {other_admin}\nЗаявка недоступна для просмотра.")
        handle_admin_back(admin_id)
        return

    prev_view, prev_ctx = get_admin_view(admin_id)
    prev_app_id = prev_ctx.get("app_id") if prev_view in (
        "app_detail", "send_banner", "reply", "reject_reason") else None
    if prev_app_id and prev_app_id != app_id:
        clear_opened_by_if_new(prev_app_id, admin_id)

    result = build_application_card(app_id)
    if result is None:
        user_state.set(admin_id, STATE_ADMIN)
        send_message(admin_id, f"❌ Заявка #{app_id} не найдена или удалена.")
        show_admin_menu(admin_id)
        return
    text, status, banner_url, is_completed, user_id, media_items, app_platform = result

    current_view_for_reset, current_ctx_for_reset = get_admin_view(admin_id)
    current_app_id = current_ctx_for_reset.get("app_id")
    if current_app_id != app_id:
        user_state.update_temp(admin_id, {
            "admin_banner_app_id": None,
            "admin_reply_user_id": None,
            "admin_reply_app_id": None,
            "admin_reject_app_id": None,
        })
    user_state.set(admin_id, STATE_ADMIN)
    current_view, ctx = get_admin_view(admin_id)
    if current_view in ("category_list", "search_list", "user_list"):
        from_view = current_view
        from_ctx = dict(ctx)
    else:
        from_view = ctx.get("from_view", "categories")
        from_ctx = ctx.get("from_ctx", {})

    set_admin_view(admin_id, "app_detail", app_id=app_id, user_id=user_id,
                   from_view=from_view, from_ctx=from_ctx)

    log_admin_action(admin_id, "view_application", target_id=app_id)

    send_message(
        admin_id, text,
        keyboard=get_admin_status_keyboard(
            app_id, current_status=status,
            banner_sent=bool(banner_url), is_completed=is_completed))

    if media_items:
        # ФИКС: уважаем пер-админ настройку «Медиа в карточках».
        # Если админ её выключил — вложения ему не отправляем,
        # вместо них приходит короткая подсказка.
        try:
            show_media = get_admin_settings(
                admin_id).get("show_media_in_applications", True)
        except Exception as e:
            logger.warning(
                "handle_admin_application_detail: get_admin_settings "
                "failed (admin=%s): %s — показываю медиа по умолчанию",
                admin_id, e)
            show_media = True

        if show_media:
            _fire_and_forget(
                _send_media_items_async(admin_id, app_id, media_items),
                label=f"send_media app={app_id} admin={admin_id}",
            )
        else:
            send_message(
                admin_id,
                f"📎 Вложения заявки #{app_id} скрыты — у вас выключена "
                f"настройка «📸 Медиа в карточках».\n"
                f"Включить: ⚙️ Настройки → 📸 Медиа в карточках: ВКЛ.")


async def _send_media_items_async(admin_id: int, app_id: int, media_items: list):
    """Отправляет вложения заявки админу в фоне."""
    import asyncio
    from core.runtime import platform as _platform, get_adapter

    if not media_items:
        return

    adapter = get_adapter()
    admin_platform = getattr(adapter, "platform", None) or _platform()

    acquired = await adapter.acquire_media_send(admin_id, app_id)
    if not acquired:
        logger.info(
            "_send_media_items_async: already sending (admin=%s, app=%s) — skip",
            admin_id, app_id)
        return

    try:
        urls_for_map = [item["url"] for item in media_items if item.get("url")]
        src_map = await asyncio.to_thread(_get_media_source_map, urls_for_map)

        # ФИКС: сообщаем адаптеру, что сейчас начнётся серия вложений.
        # Он придержит переотправку клавиатуры, пока не закончим.
        try:
            await adapter.defer_keyboard_resend(admin_id)
        except Exception as e:
            logger.debug(
                "defer_keyboard_resend failed (admin=%s, app=%s): %s",
                admin_id, app_id, e)

        for item in media_items:
            url = item.get("url")
            label = item.get("label") or f"Вложение заявки #{app_id}"
            if not url:
                continue

            info = src_map.get(url) or {}
            src_id = info.get("source_file_id")
            src_pl = info.get("source_platform")
            kind = info.get("kind") or ""

            # ФИКС Баг-13: "doc" in kind ловил любое вхождение подстроки —
            # значение kind="document" или гипотетическое "индок" дало бы
            # ложное срабатывание. Явно:
            #   - kind начинается с "doc" ("doc", "doc_scan", ...);
            #   - ИЛИ любой сегмент, разделённый "_", равен "doc"
            #     ("payment_doc", "banner_doc", ...).
            kind_parts = kind.split("_")
            is_doc = kind.startswith("doc") or "doc" in kind_parts
            tg_kind = "doc" if is_doc else "photo"

            owner_id = info.get("owner_id")
            photo_id = info.get("photo_id")
            doc_id = info.get("doc_id")

            # --- Порядок кандидатов ---
            # Для VK-админа VK-строка (photo{owner}_{id}) должна идти
            # ПЕРЕД URL: иначе VK API молча принимает URL как текст,
            # а не как вложение, и админ видит ссылку вместо фото.
            #
            # ВАЖНО: VK-строка работает только с community-фото
            # (owner_id < 0). User-фото (owner_id > 0) community-токен
            # отправить не может — VK дропает вложение.
            # --- Порядок кандидатов ---
            # Каждый кандидат — (значение, kind). kind уходит в
            # send_attachment и определяет, отправит ли адаптер
            # send_photo или send_document. У одной и той же media-строки
            # kind фиксирован, но альтернативная запись (см. ниже) может
            # иметь ДРУГОЙ kind — поэтому kind едет в паре со значением,
            # а не берётся один tg_kind на весь цикл.
            #
            # Для VK-админа VK-строка (photo{owner}_{id}) идёт ПЕРЕД URL:
            # иначе VK API молча принимает URL как текст, а не как
            # вложение, и админ видит ссылку вместо фото.
            # VK-строка работает только с community-фото (owner_id < 0):
            # user-фото (owner_id > 0) community-токен отправить не может.
            # Данные VK-релея, сохранённые в media_files адаптером
            # сразу после relay (см. update_media_file_vk_info).
            vk_url = info.get("vk_url")
            vk_owner_id = info.get("vk_owner_id")
            vk_photo_id = info.get("vk_photo_id")
            vk_doc_id = info.get("vk_doc_id")

            candidates = []

            # 1) source_file_id — самый долгоживущий вариант для админа
            #    на «родной» платформе (TG file_id для TG-админа).
            if src_id and src_pl and src_pl == admin_platform:
                candidates.append((src_id, tg_kind))

            # 2) Родная VK-строка (photo-XXX_YYY / doc-XXX_YYY) — для
            #    VK-админа, если релей уже был и owner_id < 0
            #    (community-owned фото). Это самый надёжный вариант
            #    для VK: живёт, пока сообщество не удалит.
            if (admin_platform == "vk"
                    and vk_owner_id is not None and vk_owner_id < 0):
                if is_doc and vk_doc_id is not None:
                    candidates.append(
                        (f"doc{vk_owner_id}_{vk_doc_id}", "doc"))
                elif (not is_doc) and vk_photo_id is not None:
                    candidates.append(
                        (f"photo{vk_owner_id}_{vk_photo_id}", "photo"))

            # 3) vk_url (HTTP VK CDN) — работает для MAX/TG-админа:
            #    их адаптеры скачают и зальют в свою платформу. И для
            #    VK-админа, если community-строки нет (owner_id > 0
            #    в старой колонке — user-owned, community-токен не
            #    отправит).
            if (isinstance(vk_url, str)
                    and vk_url.startswith(("http://", "https://"))):
                candidates.append((vk_url, tg_kind))

            # 4) Родное вложение VK из старых колонок (для совместимости
            #    с записями, у которых vk_owner_id ещё NULL, а
            #    owner_id/photo_id уже были заполнены).
            if (admin_platform == "vk"
                    and owner_id is not None and owner_id < 0):
                if is_doc and doc_id is not None:
                    candidates.append((f"doc{owner_id}_{doc_id}", "doc"))
                elif (not is_doc) and photo_id is not None:
                    candidates.append(
                        (f"photo{owner_id}_{photo_id}", "photo"))

            # 5) Оригинальный URL — MAX CDN / VK user CDN (может уже
            #    быть мёртвым). TG file_id тоже сюда попадает — TG-
            #    адаптер его поймёт, остальные — нет.
            if url:
                candidates.append((url, tg_kind))

            # 6) ФИКС Баг-43 (продолжение): alternatives — второй и
            # последующие строки media_files с тем же url у РАЗНЫХ
            # владельцев. _get_media_source_map сохраняет их в
            # info["alternatives"] с их собственным kind. Если основная
            # запись не даст рабочего кандидата (редкий сценарий,
            # когда kind не совпадает с реальным типом вложения) —
            # попробуем альтернативу.
            for alt in info.get("alternatives") or []:
                alt_kind = alt.get("kind") or ""
                alt_parts = alt_kind.split("_")
                alt_is_doc = (alt_kind.startswith("doc")
                              or "doc" in alt_parts)
                alt_tg_kind = "doc" if alt_is_doc else "photo"

                alt_src = alt.get("source_file_id")
                alt_src_pl = alt.get("source_platform")
                if alt_src and alt_src_pl and alt_src_pl == admin_platform:
                    candidates.append((alt_src, alt_tg_kind))

                alt_oid = alt.get("owner_id")
                if (admin_platform == "vk"
                        and alt_oid is not None and alt_oid < 0):
                    if alt_is_doc and alt.get("doc_id") is not None:
                        candidates.append((
                            f"doc{alt_oid}_{alt['doc_id']}", "doc"))
                    elif (not alt_is_doc) and alt.get("photo_id") is not None:
                        candidates.append((
                            f"photo{alt_oid}_{alt['photo_id']}", "photo"))

                if url:
                    candidates.append((url, alt_tg_kind))

            logger.info(
                "_send_media_items_async: app=%s admin=%s kind=%s "
                "owner_id=%s photo_id=%s doc_id=%s → candidates=%s",
                app_id, admin_id, tg_kind,
                owner_id, photo_id, doc_id,
                [str(c)[:40] for c in candidates])

            seen = set()
            sent_ok = False
            for cand, cand_kind in candidates:
                if not cand:
                    continue
                # dedup по паре (значение, kind): один и тот же URL
                # может прийти и из основной записи, и из alternatives,
                # но с разным kind — это разные попытки отправки.
                dedup_key = (cand, cand_kind)
                if dedup_key in seen:
                    continue
                seen.add(dedup_key)
                logger.info(
                    "_send_media_items_async: try cand=%s kind=%s "
                    "(app=%s, admin=%s)",
                    str(cand)[:64], cand_kind, app_id, admin_id)
                try:
                    ok = await adapter.send_attachment(
                        admin_id, cand, kind=cand_kind, caption=label)
                    if ok:
                        sent_ok = True
                        break
                    logger.warning(
                        "async send attempt returned False "
                        "(app #%s, admin=%s, kind=%s, cand=%s…)",
                        app_id, admin_id, cand_kind, str(cand)[:48])
                except Exception as e:
                    logger.warning(
                        "async send attempt failed "
                        "(app #%s, admin=%s, kind=%s, cand=%s…): %s",
                        app_id, admin_id, cand_kind, str(cand)[:48], e)

            if not sent_ok:
                await asyncio.to_thread(
                    send_message, admin_id,
                    f"⚠ {label} — недоступно для пересылки.")
    except Exception as e:
        logger.exception(
            "async media send group failed (app #%s, admin=%s): %s",
            app_id, admin_id, e)
    finally:
        # ФИКС: сначала отпускаем слот фоновой отправки, потом просим
        # адаптер переотправить клавиатуру — она гарантированно
        # окажется последним сообщением в серии (в MAX — внизу чата).
        await adapter.release_media_send(admin_id, app_id)
        try:
            await adapter.flush_keyboard_resend(admin_id)
        except Exception as e:
            logger.debug(
                "flush_keyboard_resend failed (admin=%s, app=%s): %s",
                admin_id, app_id, e)


def handle_admin_user_apps_text(admin_id, target_user_id, page=0):
    try:
        page = max(0, int(page))
    except (TypeError, ValueError):
        page = 0
    release_current_app_if_any(admin_id)
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, location, user_type, name_age, phone, status, banner_url, created_at, platform "
                "FROM bright_city_applications "
                "WHERE user_id = %s "
                "ORDER BY created_at ASC",
                (target_user_id,))
            rows = cur.fetchall()
    except Exception as e:
        logger.error("DB error: %s", e)
        return
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)
    if not rows:
        send_message(admin_id, f"У пользователя {target_user_id} заявок нет.")
        show_admin_menu(admin_id)
        return
    if len(rows) == 1:
        app_id = rows[0][0]
        user_state.set(admin_id, STATE_ADMIN)
        set_admin_view(admin_id, "app_detail", app_id=app_id,
                       user_id=target_user_id,
                       from_view="user_list",
                       from_ctx={"target_uid": target_user_id, "page": 0, "skip_list": True})
        handle_admin_application_detail(admin_id, app_id)
        return
    user_state.set(admin_id, STATE_ADMIN)
    app_entries = [{"id": row[0], "ts": row[7].strftime("%d.%m %H:%M")} for row in rows]
    user_state.update_temp(admin_id, {_entries_key("user_list"): app_entries})

    first_name, last_name = get_user_name(target_user_id)
    if first_name.startswith("(ID ") and not last_name:
        header = f"👤 Заявки пользователя {first_name}, страница {page + 1}:\n"
    else:
        header = f"👤 Заявки пользователя {first_name} {last_name} (ID: {target_user_id}), страница {page + 1}:\n"
    lines = [header]

    per_page = 10
    start = page * per_page
    end = min(start + per_page, len(rows))
    for app_id, location, user_type, name_age, phone, status, banner_url, created_at, row_platform in rows[start:end]:
        pl_tag = ""
        if row_platform != platform():
            pl_tag = f" [{row_platform.upper()}]"
        lines.append(f"#{app_id} | {created_at.strftime('%d.%m.%Y %H:%M')}{pl_tag} | {get_display_status(status, banner_url)}\n  {(name_age or '—')[:30]} | {user_type or '—'} | {location or '—'}\n  ☎ {phone or '—'}")
    lines.append("\n👇 Нажмите кнопку, чтобы открыть заявку:")
    set_admin_view(admin_id, "user_list", target_uid=target_user_id, page=page)
    send_message(admin_id, "\n".join(lines),
                 keyboard=get_admin_apps_keyboard(app_entries, page=page, per_page=per_page))


def handle_admin_user_apps(user_id, text, page=0):
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        send_message(user_id, "Формат: /пользователь <user_id>\nПример: /пользователь 123456")
        return
    # ФИКС отступов: as_positive_int не бросает ValueError, а возвращает
    # None. Прежний try/except его не ловил.
    target_user_id = as_positive_int(parts[1])
    if target_user_id is None:
        send_message(user_id, "user_id должен быть числом. Пример: /пользователь 123456")
        return
    handle_admin_user_apps_text(user_id, target_user_id, page)


def _entries_key(view):
    return f"admin_entries:{view}"


def handle_admin_page_navigation(user_id, page):
    try:
        page = max(0, int(page))
    except (TypeError, ValueError):
        page = 0
    view, ctx = get_admin_view(user_id)
    entries_key = _entries_key(view)
    app_entries = user_state.get_temp(user_id).get(entries_key, [])
    if not app_entries:
        send_message(user_id, "Список заявок не найден. Начните заново.")
        return
    per_page = 10
    total_pages = (len(app_entries) + per_page - 1) // per_page
    page = max(0, min(page, total_pages - 1))
    start = page * per_page
    end = min(start + per_page, len(app_entries))
    if view == "incomplete_list":
        lines = [f"📝 Не завершённые (страница {page + 1} из {total_pages}):\n"]
        for entry in app_entries[start:end]:
            uid = entry["id"]
            state = entry.get("state", "")
            step_label = STEP_LABELS.get(state, state)
            first, last = get_user_name(uid)
            ts = entry.get("ts", "—")
            lines.append(f"ID:{uid} | {ts} | {step_label}\n  {first} {last}")
        lines.append("\n👇 Нажмите кнопку, чтобы открыть:")
        set_admin_view(user_id, "incomplete_list", page=page)
        send_message(user_id, "\n".join(lines),
                     keyboard=get_admin_incomplete_keyboard(app_entries, page=page, per_page=per_page))
        return
    for attempt in range(3):
        conn = None
        try:
            conn = get_db_connection()
            with conn.cursor() as cur:
                lines = [f"📋 Страница {page + 1} из {total_pages}:\n"]
                ids = [entry["id"] for entry in app_entries[start:end]]
                if ids:
                    cur.execute("SELECT id, location, user_type, name_age, phone, status, banner_url, created_at FROM bright_city_applications WHERE id = ANY(%s) ORDER BY created_at ASC", (ids,))
                    rows_by_id = {row[0]: row for row in cur.fetchall()}
                    for entry in app_entries[start:end]:
                        row = rows_by_id.get(entry["id"])
                        if row:
                            _, location, user_type, name_age, phone, status, banner_url, created_at = row
                            lines.append(f"#{entry['id']} | {created_at.strftime('%d.%m %H:%M')} | {get_display_status(status, banner_url)}\n  {(name_age or '—')[:30]} | {user_type or '—'} | {location or '—'} | ☎ {phone or '—'}")
            conn.rollback()
            break
        except Exception as e:
            logger.error("DB error (page navigation, attempt %d/3): %s",
                         attempt + 1, e)
            if attempt < 2:
                # ФИКС Баг-23: 0.2/0.5 сек вместо 1+2+3+4. Суммарное
                # время под user_lock сокращено с ~10 до ~0.7 сек.
                time.sleep(0.2 * (attempt + 1))
            else:
                send_message(user_id, "❌ Ошибка базы данных. Попробуйте позже.")
                return
        finally:
            if conn:
                try:
                    conn.rollback()
                except Exception:
                    pass
                return_db_connection(conn)
    lines.append("\n👇 Нажмите кнопку, чтобы открыть заявку:")
    if view == "category_list":
        set_admin_view(user_id, "category_list", category=ctx.get("category", "new"), page=page)
    else:
        set_admin_view(user_id, view, **{**ctx, "page": page})
    send_message(user_id, "\n".join(lines),
                 keyboard=get_admin_apps_keyboard(app_entries, page=page, per_page=per_page))


# ============================================================
#  СТАТУСЫ
# ============================================================

def handle_admin_set_status(user_id, text):
    parts = text.split(maxsplit=2)
    if len(parts) < 3:
        send_message(user_id, "Формат: /статус <ID> <статус>\nПример: /статус 5 в обработке\n\nДоступные: в обработке, отклонена\n\nПримечание: статус «завершена» устанавливается автоматически после отправки макета.")
        return
    id_part = parts[1].strip()
    new_status_rus = parts[2].strip().lower()
    try:
        app_id = int(id_part)
    except ValueError:
        send_message(user_id, f"ID заявки должен быть числом. Вы указали: {id_part}")
        return
    new_status = STATUS_VALUES.get(new_status_rus)
    if not new_status:
        send_message(user_id, f"Неизвестный статус «{new_status_rus}».\nДоступные: в обработке, отклонена")
        return
    if new_status == "done":
        send_message(user_id, "⛔ Статус «завершена» нельзя установить вручную.\nИспользуйте кнопку «📤 Отправить макет» в карточке заявки — статус изменится автоматически после отправки макета.")
        return
    if new_status == "new":
        send_message(user_id, "⛔ Статус «новая» нельзя установить вручную.\nНовая заявка создаётся автоматически при отправке пользователем.")
        return
    handle_admin_set_status_button(user_id, app_id, new_status)


def handle_admin_set_status_button(admin_id, app_id, new_status):
    if new_status not in STATUS_LABELS:
        send_message(admin_id, f"Неизвестный статус: {new_status}")
        return
    if new_status == "done":
        send_message(admin_id, "⛔ Статус «завершена» нельзя установить вручную. Используйте кнопку «📤 Отправить макет» в карточке заявки — статус изменится автоматически после отправки макета.")
        return
    if new_status == "new":
        send_message(admin_id, "⛔ Статус «новая» нельзя установить вручную.\nНовая заявка создаётся автоматически при отправке пользователем.")
        return
    if new_status == "rejected":
        user_state.set(admin_id, STATE_ADMIN_REJECT_REASON)
        user_state.update_temp(admin_id, {"admin_reject_app_id": app_id})
        _, ctx = get_admin_view(admin_id)
        set_admin_view(admin_id, "reject_reason", app_id=app_id,
                       user_id=ctx.get("user_id"),
                       from_view=ctx.get("from_view", "categories"),
                       from_ctx=ctx.get("from_ctx", {}))
        send_message(admin_id,
            f"❌ Укажите причину отклонения заявки #{app_id}:\n\n"
            "Напишите текст причины и отправьте сообщением.\n"
            "Если передумали — нажмите «← Отмена».",
            keyboard=get_admin_cancel_reply_keyboard())
        return

    admin_name = get_admin_name(admin_id)
    conn = None
    target_platform = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, assigned_admin_id, opened_by_admin_id, user_id, "
                "       banner_url, created_at, platform, status_changed_at "
                "FROM bright_city_applications WHERE id = %s FOR UPDATE",
                (app_id,))
            row = cur.fetchone()
            if not row:
                send_message(admin_id, f"Заявка #{app_id} не найдена.")
                conn.rollback()
                return
            (old_status, assigned_id, opened_id, target_user_id,
             banner_url, created_at, target_platform, status_changed_at) = row

            standard_transitions = {
                "new": {"in_progress", "rejected"},
                "in_progress": {"done", "rejected"},
                "rejected": {"in_progress"},
                "done": set(),
            }
            allowed = standard_transitions.get(old_status, set())
            if new_status not in allowed:
                logger.warning(
                    "Нестандартный переход статуса заявки #%s: %s → %s (админ: %s)",
                    app_id, old_status, new_status, admin_name)
                send_message(admin_id,
                    f"⛔ Переход из статуса «{get_status_label(old_status)}» "
                    f"в «{get_status_label(new_status)}» недопустим "
                    f"для заявки #{app_id}.")
                conn.rollback()
                handle_admin_application_detail(admin_id, app_id)
                return

            if old_status == "done":
                send_message(admin_id,
                    f"🔒 Заявка #{app_id} уже выполнена. Смена статуса недоступна.")
                conn.rollback()
                handle_admin_application_detail(admin_id, app_id)
                return

            # Проверка «чужой» блокировки.
            if old_status == "new":
                if opened_id is not None and opened_id != admin_id:
                    cur.execute(
                        "SELECT opened_at FROM bright_city_applications WHERE id = %s",
                        (app_id,))
                    opened_at_row = cur.fetchone()
                    if opened_at_row and opened_at_row[0] is not None:
                        cur.execute("SELECT EXTRACT(EPOCH FROM (NOW() - %s))", (opened_at_row[0],))
                        elapsed = cur.fetchone()[0]
                        if elapsed < 300:
                            n = get_user_name(opened_id)
                            send_message(admin_id,
                                f"🔒 Над заявкой #{app_id} работает админ: {n[0]} {n[1]}")
                            conn.rollback()
                            handle_admin_back(admin_id)
                            return
                    else:
                        logger.warning(
                            "opened_at is NULL in set_status for app #%s, "
                            "conservatively denying", app_id)
                        n = get_user_name(opened_id)
                        send_message(admin_id,
                            f"🔒 Над заявкой #{app_id} работает админ: {n[0]} {n[1]}")
                        conn.rollback()
                        handle_admin_back(admin_id)
                        return
            else:
                if assigned_id is not None and assigned_id != admin_id:
                    timeout_ts = status_changed_at or created_at
                    stale = False
                    if timeout_ts is not None:
                        cur.execute("SELECT EXTRACT(EPOCH FROM (NOW() - %s))", (timeout_ts,))
                        elapsed = cur.fetchone()[0]
                        stale = elapsed >= _IN_PROGRESS_STALE_SEC
                    if not stale:
                        n = get_user_name(assigned_id)
                        send_message(admin_id,
                            f"🔒 Над заявкой #{app_id} работает админ: {n[0]} {n[1]}")
                        conn.rollback()
                        handle_admin_back(admin_id)
                        return
                    # Перехват зависшей заявки.
                    cur.execute(
                        """UPDATE bright_city_applications
                              SET assigned_admin_id = %s, assigned_admin_name = %s,
                                  opened_by_admin_id = NULL, opened_by_admin_name = NULL,
                                  opened_at = NULL
                            WHERE id = %s""",
                        (admin_id, admin_name, app_id))

            cur.execute(
                "UPDATE bright_city_applications "
                "SET status = %s, rejection_reason = NULL WHERE id = %s",
                (new_status, app_id))
            if old_status != new_status:
                _log_status_change_with_cursor(
                    cur, app_id, old_status, new_status, admin_id, admin_name)
        conn.commit()
    except Exception as e:
        logger.error("DB error (set status): %s", e)
        send_message(admin_id, f"❌ Ошибка при смене статуса заявки #{app_id}.")
        return
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)

    try:
        notify_admin_applications(
            f"✅ Заявка #{app_id} → {get_status_label(new_status)} "
            f"(админ: {admin_name})")
        status_messages = {
            "in_progress": "Ваша заявка принята в обработку. Мы проверяем данные и свяжемся с вами, если что-то понадобится.",
            "done": "Ваш баннер напечатан и смонтирован! 🎉\nСпасибо за участие в проекте «Яркий!Город»!\nДиплом: https://yarkijgorod.ru/diplom-zayavka",
        }
        user_msg = status_messages.get(new_status)
        if user_msg:
            target_pl = target_platform or platform()
            notified = send_message(target_user_id, user_msg,
                                    target_platform=target_pl)
            if get_admin_settings(admin_id)["notify_applications"]:
                if notified:
                    logger.info(
                        "handle_admin_set_status_button: заявка #%s → %s, "
                        "user %s уведомлён",
                        app_id, new_status, target_user_id)
                else:
                    send_message(admin_id,
                        f"⚠ Статус заявки #{app_id} изменён, но уведомление "
                        f"пользователю {target_user_id} НЕ доставлено "
                        f"(забанен, недоступен или ошибка отправки).")
            else:
                if notified:
                    send_message(admin_id,
                        f"→ Пользователь {target_user_id} уведомлён.")
                else:
                    send_message(admin_id,
                        f"⚠ Статус заявки #{app_id} изменён, но уведомление "
                        f"пользователю {target_user_id} НЕ доставлено.")
    except Exception as notify_err:
        logger.error(
            "Ошибка отправки уведомлений после смены статуса заявки #%s: %s",
            app_id, notify_err)
        send_message(admin_id,
            f"⚠ Статус заявки #{app_id} изменён, но не удалось отправить "
            f"все уведомления.")
    handle_admin_application_detail(admin_id, app_id)


def process_admin_reject_reason(admin_id, reason_text):
    if not is_admin_user(admin_id):
        kick_out_non_admin(admin_id, reason="process_admin_reject_reason")
        return
    reason_text = reason_text.strip()
    if reason_text in ("← Назад", "← Отмена", "Назад", "Отмена"):
        handle_admin_back(admin_id)
        return
    if len(reason_text) > MAX_TEXT_LENGTH:
        reason_text = reason_text[:MAX_TEXT_LENGTH]
    temp = user_state.get_temp(admin_id)
    app_id = temp.get("admin_reject_app_id")
    if not app_id:
        send_message(admin_id, "Не удалось определить заявку. Возврат в админ-меню.")
        show_admin_menu(admin_id)
        return
    conn = None
    ok = False
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM bright_city_applications WHERE id = %s", (app_id,))
            row = cur.fetchone()
            if not row:
                send_message(admin_id, f"Заявка #{app_id} не найдена.")
                show_admin_menu(admin_id)
                return
            old_status = row[0]
            if old_status not in ("new", "in_progress"):
                send_message(admin_id, f"⛔ Нельзя отклонить заявку в статусе «{get_status_label(old_status)}».")
                handle_admin_application_detail(admin_id, app_id)
                return
            ok = True
    except Exception as e:
        logger.error("DB error (reject precheck): %s", e)
        send_message(admin_id, "Ошибка БД. Попробуйте позже.")
        return
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)
    if not ok:
        return
    ok = create_sudo_request(admin_id, "reject",
                             f"Отклонить заявку #{app_id}?\n\nПричина: {reason_text[:200]}",
                             target=app_id, params={"reason": reason_text})
    if ok:
        send_message(admin_id, "Ждём подтверждения…", keyboard=get_empty_keyboard())
    user_state.set(admin_id, STATE_ADMIN)


# ============================================================
#  СТАТИСТИКА И ПОИСК
# ============================================================

def handle_admin_stats(user_id):
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM bright_city_applications")
            total = cur.fetchone()[0]
            cur.execute("SELECT status, COUNT(*) FROM bright_city_applications GROUP BY status ORDER BY COUNT(*) DESC")
            by_status = cur.fetchall()
            cur.execute("SELECT COUNT(*) FILTER (WHERE status = 'done' AND banner_url IS NOT NULL), COUNT(*) FILTER (WHERE status = 'done' AND banner_url IS NULL) FROM bright_city_applications")
            banner_stats = cur.fetchone()
            cur.execute("SELECT location, COUNT(*) FROM bright_city_applications GROUP BY location ORDER BY COUNT(*) DESC LIMIT 20")
            by_city = cur.fetchall()
            cur.execute("SELECT COUNT(DISTINCT location) FROM bright_city_applications")
            total_cities = cur.fetchone()[0]
            cur.execute("SELECT user_type, COUNT(*) FROM bright_city_applications GROUP BY user_type ORDER BY COUNT(*) DESC")
            by_type = cur.fetchall()
            cur.execute("SELECT DATE(created_at), COUNT(*) FROM bright_city_applications WHERE created_at > NOW() - INTERVAL '7 days' GROUP BY DATE(created_at) ORDER BY DATE(created_at) DESC")
            by_day = cur.fetchall()
    except Exception as e:
        logger.error("DB error: %s", e)
        send_message(user_id, "❌ Ошибка базы данных.",
                     keyboard=get_admin_menu_keyboard())
        return
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)
    lines = [f"📊 Статистика\n\nВсего заявок: {total}\n", "По статусам:"]
    lines.extend(f"  {get_status_label(s)}: {c}" for s, c in by_status)
    lines.append(f"\nМакеты отправлены: {banner_stats[0] or 0}")
    lines.append(f"Ожидает отправки макета: {banner_stats[1] or 0}")
    lines.append("\nПо городам:")
    lines.extend(f"  {c or '—'}: {n}" for c, n in by_city)
    if total_cities > len(by_city):
        lines.append(f"  ... и ещё {total_cities - len(by_city)} городов")
    lines.append("\nПо типам:")
    lines.extend(f"  {t or '—'}: {n}" for t, n in by_type)
    lines.append("\nЗа последние 7 дней:")
    lines.extend(f"  {d.strftime('%d.%m')}: {n}" for d, n in by_day)
    send_message(user_id, "\n".join(lines),
                 keyboard=get_admin_menu_keyboard())


def handle_admin_search_text(user_id, query, page=0):
    release_current_app_if_any(user_id)
    user_state.set(user_id, STATE_ADMIN)
    words = [w.strip() for w in query.split() if w.strip()]
    if not words:
        send_message(user_id, "Поисковый запрос пуст.")
        return
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            where_parts = []
            params = []
            for word in words:
                escaped_word = word.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
                like_val = f"%{escaped_word}%"
                where_parts.append(sql.SQL(
                    "(LOWER(name_age) LIKE %s ESCAPE '\\' "
                    "OR LOWER(training_info) LIKE %s ESCAPE '\\' "
                    "OR LOWER(phone) LIKE %s ESCAPE '\\' "
                    "OR LOWER(placement_location) LIKE %s ESCAPE '\\')"
                ))
                params.extend([like_val, like_val, like_val, like_val])
            where_clause = sql.SQL(" AND ").join(where_parts)
            cur.execute(sql.SQL(
                "SELECT id, user_id, name_age, user_type, phone, status, banner_url, created_at "
                "FROM bright_city_applications "
                "WHERE {} "
                "ORDER BY created_at ASC LIMIT 50"
            ).format(where_clause), params)
            rows = cur.fetchall()
    except Exception as e:
        logger.error("DB error: %s", e)
        return
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)
    if not rows:
        send_message(user_id, f"По запросу «{query}» ничего не найдено.")
        show_admin_menu(user_id)
        return
    if len(rows) == 1:
        app_id = rows[0][0]
        set_admin_view(user_id, "app_detail", app_id=app_id,
                       user_id=rows[0][1],
                       from_view="search_list",
                       from_ctx={"query": query, "page": 0, "skip_list": True})
        handle_admin_application_detail(user_id, app_id)
        return
    app_entries = [{"id": row[0], "ts": row[7].strftime("%d.%m %H:%M")} for row in rows]
    user_state.update_temp(user_id, {_entries_key("search_list"): app_entries})
    per_page = 10
    start = page * per_page
    end = min(start + per_page, len(rows))
    lines = [f"🔍 Результаты поиска «{query}» (страница {page + 1}):\n"]
    for app_id, uid, name_age, user_type, phone, status, banner_url, created_at in rows[start:end]:
        lines.append(f"#{app_id} | {created_at.strftime('%d.%m %H:%M')} | {get_display_status(status, banner_url)}\n  {name_age or '—'} | {user_type or '—'} | ☎ {phone or '—'}\n  /заявка_{app_id}  |  /пользователь {uid}")
    lines.append("\n👇 Нажмите кнопку, чтобы открыть заявку:")
    set_admin_view(user_id, "search_list", query=query, page=page)
    send_message(user_id, "\n".join(lines),
                 keyboard=get_admin_apps_keyboard(app_entries, page=page, per_page=per_page))


def handle_admin_search(user_id, text, page=0):
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        send_message(user_id, "Формат: /поиск <имя или часть имени>")
        return
    handle_admin_search_text(user_id, parts[1].strip().lower(), page)


# ============================================================
#  ОТВЕТЫ И ИСТОРИЯ
# ============================================================

def _get_user_state_for_platform(user_id, pl):
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT state FROM bot_user_states "
                "WHERE platform = %s AND user_id = %s",
                (pl, user_id))
            row = cur.fetchone()
            if row:
                return str(row[0]) if row[0] is not None else ""
    except Exception as e:
        logger.error("DB error (_get_user_state_for_platform): %s", e)
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)
    return STATE_START


# ============================================================
#  ПРОВЕРКА ВЛАДЕЛЬЦА ЗАЯВКИ
# ============================================================

def _check_app_owner(app_id, target_user_id):
    """Проверяет, что заявка app_id принадлежит target_user_id."""
    if app_id is None:
        return True, ""
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT user_id FROM bright_city_applications WHERE id = %s",
                (app_id,))
            row = cur.fetchone()
            if not row:
                return False, (
                    f"заявка #{app_id} не найдена (удалена или "
                    f"анонимизирована) — невозможно проверить владельца"
                )
            owner_id = row[0]
            if owner_id is None:
                return False, (
                    f"заявка #{app_id} анонимизирована "
                    f"(user_id удалён по 152-ФЗ)"
                )
            if owner_id != target_user_id:
                return False, (
                    f"заявка #{app_id} принадлежит пользователю {owner_id}, "
                    f"а вы адресуете ответ пользователю {target_user_id}"
                )
            return True, ""
    except Exception as e:
        logger.error("_check_app_owner: DB error (app=%s): %s", app_id, e)
        return False, "не удалось проверить принадлежность заявки"
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)


def _parse_reply_target(raw: str):
    """Разбирает <user_id> или <platform>:<user_id>.

    ФИКС Баг-59: если платформа не определяется из БД (пользователь
    ни разу не писал боту), админ может указать её явно:
        /ответ vk:11424783 текст
        /ответ tg:11424783 текст

    Возвращает (user_id: int | None, platform: str | None).
    platform = None → определять из БД как раньше.
    platform = 'vk'|'tg'|'max' → использовать явно.
    Возвращает (None, None) при ошибке разбора.
    """
    if not isinstance(raw, str):
        return None, None
    raw = raw.strip()
    if not raw:
        return None, None
    if ":" in raw:
        pl, uid_str = raw.split(":", 1)
        pl = pl.strip().lower()
        if pl not in ("vk", "tg", "max"):
            return None, None
        uid = as_positive_int(uid_str.strip())
        if uid is None:
            return None, None
        return uid, pl
    # Без префикса — только user_id, платформу определим из БД.
    uid = as_positive_int(raw)
    if uid is None:
        return None, None
    return uid, None


def _send_reply_to_user(target_user_id, text, application_id=None,
                        target_platform=None):
    """Отправляет ответ админа пользователю.

    ФИКС Баг-59: параметр target_platform позволяет явно указать
    платформу получателя. Нужно когда пользователь не писал боту
    (нет записи в bot_user_states / bright_city_applications), и
    detect_user_platform не может определить её автоматически.
    """
    # target_platform приоритетнее application_id и автоопределения.
    if application_id and not target_platform:
        conn = None
        try:
            conn = get_db_connection()
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT platform, user_id "
                    "FROM bright_city_applications WHERE id = %s",
                    (application_id,))
                row = cur.fetchone()
                if not row:
                    logger.warning(
                        "_send_reply_to_user: заявка #%s не найдена — "
                        "ответ не отправляю (target=%s)",
                        application_id, target_user_id)
                    return False
                target_platform = row[0]
                owner_id = row[1]
                if owner_id is None:
                    logger.warning(
                        "_send_reply_to_user: заявка #%s анонимизирована "
                        "— ответ не отправляю (target=%s)",
                        application_id, target_user_id)
                    return False
                if owner_id != target_user_id:
                    logger.warning(
                        "_send_reply_to_user: заявка #%s принадлежит "
                        "user %s, ответ адресован %s — отказ",
                        application_id, owner_id, target_user_id)
                    return False
        except Exception as e:
            logger.error("_send_reply_to_user: failed to detect platform: %s", e)
            return False
        finally:
            if conn:
                conn.rollback()
                return_db_connection(conn)

    if not target_platform:
        # ФИКС Баг-60: НЕ передаём exclude_platform=platform().
        # Раньше это отсекало пользователей, которые есть ТОЛЬКО на
        # текущей платформе процесса: MAX-админ не мог ответить
        # MAX-юзеру (detect_user_platform(uid, 'max') → None, потому
        # что 'max' == platform()). Аналогичная проблема у VK- и
        # TG-админов.
        #
        # Семантика exclude_platform правильная только для
        # get_user_name (не идти в чужой API за именем своего юзера).
        # Для «определить, куда отправлять» нам нужен ВСЕГДА
        # фактический носитель user_id.
        detected = detect_user_platform(target_user_id)   # без exclude
        if not detected:
            logger.error(
                "_send_reply_to_user: не могу определить платформу "
                "для %s (нет записей в bot_user_states / "
                "bright_city_applications, или пользователь встречается "
                "на нескольких) — используйте явный формат /ответ "
                "vk:%s <текст>",
                target_user_id, target_user_id)
            return False

    is_cross_platform = (target_platform != platform())

    if is_cross_platform:
        target_state = _get_user_state_for_platform(target_user_id, target_platform)
    else:
        target_state = user_state.get(target_user_id)

    if target_state == STATE_QUESTION:
        body = (f"💬 Ответ от администратора:\n\n{text}\n\n"
                "Если есть ещё вопросы — пишите прямо сюда.")
        kb = None if is_cross_platform else get_question_keyboard()
    elif target_state == STATE_START:
        body = (f"💬 Ответ от администратора:\n\n{text}\n\n"
                "Вы можете ответить прямо здесь — сообщение уйдёт "
                "администратору.")
        kb = None if is_cross_platform else get_main_keyboard()
    elif target_state in (STATE_NEXT_ACTION, STATE_EXTRAS):
        body = (f"💬 Ответ от администратора:\n\n{text}\n\n"
                "Заявка принята в обработку!\n\n"
                "Хотите оформить ещё одну заявку или завершить работу с ботом?")
        kb = None if is_cross_platform else get_next_action_keyboard()
    else:
        body = (f"💬 Ответ от администратора:\n\n{text}\n\n"
                "Вы можете продолжить заполнение заявки с того же места.")
        kb = None

    if is_cross_platform:
        send_success = send_message(
            target_user_id, body, keyboard=kb,
            target_platform=target_platform)
    else:
        # keep=True: сообщение от админа — «якорь диалога», не удаляем.
        # Особенно важно в STATE_QUESTION: пользователь может получить
        # ответ, потом задать следующий вопрос, и предыдущий ответ должен
        # остаться в чате.
        send_success = send_message(
            target_user_id, body, keyboard=kb, keep=True)

    if send_success:
        save_message_to_db(
            target_user_id, "out",
            f"💬 Ответ от администратора:\n\n{text}",
            application_id=application_id,
            target_platform=target_platform)
    return send_success


def start_admin_reply(user_id, app_id):
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT user_id, platform FROM bright_city_applications WHERE id = %s",
                (app_id,))
            row = cur.fetchone()
    except Exception as e:
        logger.error("DB error: %s", e)
        return
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)
    if not row:
        send_message(user_id, f"Заявка #{app_id} не найдена.")
        return
    target_user_id, target_platform = row
    first, last = get_user_name(target_user_id)
    _, ctx = get_admin_view(user_id)
    user_state.set(user_id, STATE_ADMIN_REPLY)
    from_view = ctx.get("from_view", "categories")
    from_ctx = ctx.get("from_ctx", {})
    user_state.update_temp(user_id, {
        "admin_reply_app_id": app_id,
        "admin_reply_user_id": target_user_id,
        "admin_reply_from_view": from_view,
        "admin_reply_from_ctx": from_ctx,
    })
    set_admin_view(user_id, "reply", app_id=app_id,
                   user_id=target_user_id,
                   from_view=from_view, from_ctx=from_ctx)
    send_message(user_id,
        f"💬 Режим ответа пользователю\n\n"
        f"Получатель: {first} {last} (ID: {target_user_id})\n"
        f"Заявка: #{app_id}\n\n"
        "Напишите сообщение — оно будет отправлено пользователю.\n"
        "Для отмены нажмите кнопку ниже.",
        keyboard=get_admin_cancel_reply_keyboard())


def process_admin_reply(user_id, text, app_id=None, target_platform=None):
    if not is_admin_user(user_id):
        kick_out_non_admin(user_id, reason="process_admin_reply")
        return
    text = text[:MAX_TEXT_LENGTH]
    temp = user_state.get_temp(user_id)
    target_user_id = temp.get("admin_reply_user_id")
    view, ctx = get_admin_view(user_id)

    ctx_app_id = ctx.get("app_id")
    ctx_user_id = ctx.get("user_id")
    temp_app_id = temp.get("admin_reply_app_id")

    if (view != "reply"
            or not target_user_id
            or ctx_user_id != target_user_id
            or (temp_app_id and ctx_app_id != temp_app_id)):
        logger.warning(
            "process_admin_reply: устаревший контекст "
            "(admin=%s, view=%s, temp_uid=%s, ctx_uid=%s, "
            "temp_app=%s, ctx_app=%s) — отклоняю",
            user_id, view, target_user_id, ctx_user_id,
            temp_app_id, ctx_app_id)
        user_state.set(user_id, STATE_ADMIN)
        show_admin_menu(user_id)
        return

    if app_id is None:
        app_id = temp_app_id
    if not target_user_id:
        send_message(user_id, "Не удалось определить получателя. Возврат в админ-меню.")
        show_admin_menu(user_id)
        return
    send_success = _send_reply_to_user(
        target_user_id, text,
        application_id=app_id,
        target_platform=target_platform,
    )
    if send_success:
        send_message(user_id, f"✅ Сообщение отправлено пользователю {target_user_id}.")
    else:
        send_message(user_id, f"⚠ Не удалось отправить сообщение пользователю {target_user_id} (все попытки исчерпаны). Сообщение не сохранено.")
    user_state.set(user_id, STATE_ADMIN)
    if app_id:
        handle_admin_application_detail(user_id, app_id)
    else:
        from_view = temp.get("admin_reply_from_view", "")
        from_ctx = temp.get("admin_reply_from_ctx", {})
        if from_view == "incomplete_detail":
            target_uid = from_ctx.get("target_uid") or target_user_id
            skip_list = from_ctx.get("skip_list", False)
            set_admin_view(user_id, "incomplete_detail", target_uid=target_uid, skip_list=skip_list)
            handle_admin_incomplete_detail(user_id, target_uid)
        else:
            show_admin_menu(user_id)


def handle_admin_reply(admin_id, text):
    parts = text.split(maxsplit=2)
    if len(parts) < 3:
        send_message(
            admin_id,
            "Формат: /ответ <user_id> <текст>\n"
            "Пример: /ответ 123456 Здравствуйте!\n\n"
            "Если бот не может определить платформу пользователя "
            "(он ни разу не писал), укажите её явно:\n"
            "   /ответ vk:123456 Здравствуйте!\n"
            "   /ответ tg:123456 Здравствуйте!")
        return
    # ФИКС Баг-59: поддержка префикса <platform>:<user_id>.
    target_user_id, explicit_platform = _parse_reply_target(parts[1])
    if target_user_id is None:
        send_message(
            admin_id,
            "Не могу разобрать user_id. Ожидается число или "
            "<platform>:<user_id>.\n"
            "Пример: /ответ 123456 текст\n"
            "        /ответ vk:123456 текст")
        return
    reply_text = parts[2][:MAX_TEXT_LENGTH]
    view, ctx = get_admin_view(admin_id)
    app_id = ctx.get("app_id") if view == "app_detail" else None

    ok, reason = _check_app_owner(app_id, target_user_id)
    if not ok:
        send_message(admin_id,
            f"⚠ Нельзя отправить ответ: {reason}.\n\n"
            f"Проверьте, что вы пишете правильному пользователю. "
            f"Открыта заявка: #{app_id}.")
        return

    send_success = _send_reply_to_user(
        target_user_id, reply_text,
        application_id=app_id,
        target_platform=explicit_platform,
    )
    if send_success:
        send_message(admin_id, f"✅ Ответ отправлен пользователю {target_user_id}.")
    else:
        send_message(admin_id, f"⚠ Не удалось отправить ответ пользователю {target_user_id} (все попытки исчерпаны). Сообщение не сохранено.")
    if view == "app_detail" and app_id:
        handle_admin_application_detail(admin_id, app_id)
        return
    show_admin_menu(admin_id)


def handle_admin_history(admin_id, text):
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        send_message(admin_id, "Формат: /история <user_id>\nПример: /история 123456",
                     keyboard=get_admin_menu_keyboard())
        return
    target_user_id = as_positive_int(parts[1])
    if target_user_id is None:
        send_message(admin_id, "user_id должен быть числом. Пример: /история 123456",
                     keyboard=get_admin_menu_keyboard())
        return

    target_platform = detect_user_platform(
        target_user_id, exclude_platform=None)
    if target_platform is None:
        target_platform = platform()
        logger.warning(
            "handle_admin_history: платформа для %s не определена "
            "однозначно, fallback=%s", target_user_id, target_platform)

    rows = get_history_for_user(
        target_user_id, target_platform=target_platform)
    if not rows:
        send_message(admin_id, f"История диалогов с пользователем {target_user_id} пуста.",
                     keyboard=get_admin_menu_keyboard())
        return
    first_name, last_name = get_user_name(target_user_id)
    lines = [f"📜 История диалогов с {first_name} {last_name} (ID: {target_user_id}):\n"]
    for direction, msg_text, created_at in rows:
        ts = created_at.strftime("%d.%m.%Y %H:%M")
        who = "Пользователь" if direction == "in" else "Администратор"
        lines.append(f"[{ts}] {who}:\n{msg_text}\n")
    full = "\n".join(lines)
    if len(full.encode('utf-8')) > 4000:
        encoded = full.encode('utf-8')[:3800]
        full = encoded.decode('utf-8', errors='ignore') + "\n...(обрезано)"
    send_message(admin_id, full,
                 keyboard=get_admin_menu_keyboard())


# ============================================================
#  МАКЕТ
# ============================================================

def start_admin_send_banner(user_id, app_id):
    conn = None
    target_user_id = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT user_id FROM bright_city_applications WHERE id = %s",
                (app_id,))
            row = cur.fetchone()
            if row:
                target_user_id = row[0]
    except Exception as e:
        logger.error("DB error (start_admin_send_banner): %s", e)
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)
    if target_user_id is None:
        send_message(user_id, f"Заявка #{app_id} не найдена.")
        return
    _, ctx = get_admin_view(user_id)
    user_state.set(user_id, STATE_ADMIN_SEND_BANNER)
    user_state.update_temp(user_id, {"admin_banner_app_id": app_id})
    set_admin_view(user_id, "send_banner", app_id=app_id,
                   user_id=target_user_id,
                   from_view=ctx.get("from_view", "categories"),
                   from_ctx=ctx.get("from_ctx", {}))
    send_message(user_id,
        f"📤 Режим отправки макета для заявки #{app_id}\n\n"
        "Загрузите фото готового макета баннера.\n"
        "Оно будет отправлено участнику и сохранено в заявке.\n\n"
        "Для отмены нажмите кнопку ниже.",
        keyboard=get_admin_cancel_banner_keyboard())


def process_admin_banner_photo(user_id, attachments, text=None):
    if not is_admin_user(user_id):
        kick_out_non_admin(user_id, reason="process_admin_banner_photo")
        return
    temp = user_state.get_temp(user_id)
    app_id = temp.get("admin_banner_app_id")
    view, ctx = get_admin_view(user_id)
    ctx_app_id = ctx.get("app_id")
    ctx_user_id = ctx.get("user_id")
    if (not app_id
            or view != "send_banner"
            or ctx_app_id != app_id
            or ctx_user_id is None):
        logger.warning(
            "process_admin_banner_photo: устаревший контекст "
            "(admin=%s, view=%s, temp_app=%s, ctx_app=%s, ctx_uid=%s) — отклоняю",
            user_id, view, app_id, ctx_app_id, ctx_user_id)
        user_state.set(user_id, STATE_ADMIN)
        show_admin_menu(user_id)
        return
    photo_urls = extract_photo_urls(attachments)
    if not photo_urls:
        send_message(user_id, "Нужно именно фото. Прикрепите изображение макета." if text and text.strip() else "Не удалось получить фото. Попробуйте ещё раз.")
        return

    banner_url = photo_urls[0]
    import requests as _requests
    try:
        head_resp = _requests.head(banner_url, timeout=10)
        content_length_str = head_resp.headers.get("Content-Length")
        if content_length_str is None:
            logger.warning("HEAD request returned no Content-Length for banner URL: %s", banner_url)
            send_message(user_id, "⚠ Не удалось определить размер макета (сервер не вернул Content-Length). Попробуйте загрузить фото заново.")
            user_state.set(user_id, STATE_ADMIN)
            handle_admin_application_detail(user_id, app_id)
            return
        content_length = int(content_length_str)
        if content_length == 0:
            logger.warning("HEAD request returned Content-Length=0 for banner URL: %s", banner_url)
            send_message(user_id, "⚠ Сервер вернул пустой ответ (Content-Length: 0). Попробуйте загрузить фото заново.")
            user_state.set(user_id, STATE_ADMIN)
            handle_admin_application_detail(user_id, app_id)
            return
        if content_length > MAX_BANNER_SIZE:
            size_mb = content_length // (1024 * 1024)
            max_mb = MAX_BANNER_SIZE // (1024 * 1024)
            send_message(user_id, f"⚠ Макет слишком большой ({size_mb} МБ). Максимум — {max_mb} МБ. Уменьшите размер файла и попробуйте снова.")
            user_state.set(user_id, STATE_ADMIN)
            handle_admin_application_detail(user_id, app_id)
            return
    except Exception as e:
        logger.warning("HEAD request failed for banner URL: %s", e)
        send_message(user_id, "⚠ Не удалось проверить размер макета. Попробуйте загрузить фото заново.")
        user_state.set(user_id, STATE_ADMIN)
        handle_admin_application_detail(user_id, app_id)
        return

    admin_name = get_admin_name(user_id)
    target_user_id = None
    target_platform_banner = None
    old_status = None
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT status, user_id, platform FROM bright_city_applications WHERE id = %s FOR UPDATE", (app_id,))
            row = cur.fetchone()
            if not row:
                send_message(user_id, f"Заявка #{app_id} не найдена.")
                conn.rollback()
                show_admin_menu(user_id)
                return
            old_status, target_user_id, target_platform_banner = row
            cur.execute("UPDATE bright_city_applications SET banner_url = %s, status = 'done' WHERE id = %s", (banner_url, app_id))
            if old_status != "done":
                _log_status_change_with_cursor(cur, app_id, old_status, "done", user_id, admin_name)
        conn.commit()
        if target_platform_banner == "tg":
            try:
                n = request_full_relay_for_user(
                    "tg", target_user_id, application_id=app_id)
                if n:
                    logger.info(
                        "full_relay scheduled: app=%s user=%s count=%s",
                        app_id, target_user_id, n)
            except Exception as e:
                logger.warning("full_relay schedule failed for app #%s: %s",
                               app_id, e)
    except Exception as e:
        logger.error("DB error (banner photo save + status done): %s", e)
        send_message(user_id, f"❌ Ошибка при сохранении макета для заявки #{app_id}.")
        user_state.set(user_id, STATE_ADMIN)
        handle_admin_application_detail(user_id, app_id)
        return
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)
    try:
        banner_meta = extract_photo_meta(attachments)
        register_media_file(
            url=banner_url,
            owner_id=banner_meta[0].get("owner_id") if banner_meta else None,
            photo_id=banner_meta[0].get("photo_id") if banner_meta else None,
            kind="banner",
            user_platform=target_platform_banner or platform(),
            user_id=target_user_id,
            application_id=app_id,
        )
    except Exception as e:
        logger.warning("media_file register (banner) failed: %s", e)

    if target_user_id:
        success = send_banner_to_user(
            target_user_id, banner_url, app_id,
            admin_id=user_id,
            target_platform=target_platform_banner)
        if not success:
            rollback_conn = None
            try:
                rollback_conn = get_db_connection()
                with rollback_conn.cursor() as cur:
                    # ФИКС Баг-25: откатываем ВСЁ, что записал
                    # _log_status_change_with_cursor, а не только
                    # banner_url и status. Раньше status_changed_by_name,
                    # status_changed_at, assigned_admin_id/name оставались
                    # от неудачной попытки, и карточка врала о статусе.
                    cur.execute(
                        "SELECT status_changed_by_name, status_changed_at, "
                        "       assigned_admin_id, assigned_admin_name "
                        "FROM bright_city_applications WHERE id = %s",
                        (app_id,))
                    prev_row = cur.fetchone()
                    prev_changed_by_name = prev_row[0] if prev_row else None
                    prev_changed_at = prev_row[1] if prev_row else None
                    prev_assigned_id = prev_row[2] if prev_row else None
                    prev_assigned_name = prev_row[3] if prev_row else None

                    # Восстанавливаем из application_status_history
                    # предыдущее состояние (запись до нашей 'done'-попытки).
                    cur.execute("""
                        SELECT old_status, admin_name, changed_at
                        FROM application_status_history
                        WHERE application_id = %s
                        ORDER BY changed_at DESC
                        LIMIT 1 OFFSET 1
                    """, (app_id,))
                    prev_hist = cur.fetchone()

                    cur.execute("""
                        UPDATE bright_city_applications
                        SET banner_url = NULL,
                            status = %s,
                            status_changed_by_name = %s,
                            status_changed_at = %s
                        WHERE id = %s
                    """, (old_status,
                          prev_hist[1] if prev_hist else prev_changed_by_name,
                          prev_hist[2] if prev_hist else prev_changed_at,
                          app_id))

                    if old_status != "done":
                        cur.execute("""DELETE FROM application_status_history
                            WHERE id IN (
                                SELECT id FROM application_status_history
                                WHERE application_id = %s AND new_status = 'done'
                                ORDER BY changed_at DESC LIMIT 1
                            )""", (app_id,))
                rollback_conn.commit()
            except Exception as rb_err:
                if rollback_conn:
                    rollback_conn.rollback()
                logger.error("DB error (rollback banner_url + status): %s", rb_err)
            finally:
                if rollback_conn:
                    rollback_conn.rollback()
                    return_db_connection(rollback_conn)
            user_state.set(user_id, STATE_ADMIN)
            handle_admin_application_detail(user_id, app_id)
            return
    if target_platform_banner and target_platform_banner != platform():
        notify_admin_applications(
            f"✅ Макет для заявки #{app_id} поставлен в очередь на доставку "
            f"в {target_platform_banner.upper()}.")
    else:
        notify_admin_applications(
            f"✅ Макет для заявки #{app_id} отправлен участнику.")
    user_state.set(user_id, STATE_ADMIN)
    handle_admin_application_detail(user_id, app_id)


def send_banner_to_user(user_id, banner_url, app_id, admin_id=None, target_platform=None):
    target_platform = target_platform or platform()

    if target_platform != platform():
        banner_msg = ("🎨 Ваш макет баннера готов!\n\n"
                      "Проверьте, пожалуйста, все данные на макете: имя, возраст, "
                      "фотографию, рисунок, информацию о педагоге/организации.\n\n"
                      "После печати внести изменения будет невозможно.\n\n"
                      "Если всё верно — просто напишите «Всё верно» в ответ.\n"
                      "Если нужно что-то исправить — опишите, что не так.")
        ok = enqueue_outbox(
            target_platform=target_platform,
            target_user_id=user_id,
            text=banner_msg,
            attachment=banner_url,
            caption=banner_msg,
            message_type="banner",
        )
        if ok:
            save_message_to_db(user_id, "out", banner_msg,
                               application_id=app_id,
                               target_platform=target_platform)
        else:
            if admin_id:
                send_message(admin_id,
                    f"⚠ Не удалось поставить макет в очередь для {target_platform}.")
            logger.error("send_banner_to_user: enqueue_outbox failed "
                         "(platform=%s, user=%s, app=%s)",
                         target_platform, user_id, app_id)
        return ok

    import requests as _requests
    from core.utils import _run_async
    response = None
    try:
        response = _requests.get(banner_url, timeout=30, stream=True)
        if response.status_code != 200:
            raise Exception(f"Не удалось скачать фото, статус {response.status_code}")
        content_type = response.headers.get("Content-Type", "")
        if not content_type.startswith("image/"):
            raise Exception(f"Недопустимый Content-Type: {content_type}. Ожидается изображение.")
        cl_header = response.headers.get('Content-Length')
        if cl_header is not None:
            content_length = int(cl_header)
            if content_length > MAX_BANNER_SIZE:
                size_mb = content_length // (1024 * 1024)
                max_mb = MAX_BANNER_SIZE // (1024 * 1024)
                msg = f"⚠ Макет слишком большой ({size_mb} МБ). Максимум — {max_mb} МБ. Уменьшите размер файла и попробуйте снова."
                if admin_id:
                    send_message(admin_id, msg)
                else:
                    logger.error(msg)
                return False
            if content_length == 0:
                raise Exception("Сервер вернул пустой ответ (Content-Length: 0)")
            chunks = []
            for chunk in response.iter_content(chunk_size=8192):
                if not chunk:
                    break
                chunks.append(chunk)
            file_data = b''.join(chunks)
        else:
            chunks = []
            total = 0
            for chunk in response.iter_content(chunk_size=8192):
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_BANNER_SIZE:
                    size_mb = total // (1024 * 1024)
                    max_mb = MAX_BANNER_SIZE // (1024 * 1024)
                    msg = f"⚠ Макет слишком большой (>{size_mb} МБ). Максимум — {max_mb} МБ. Уменьшите размер файла и попробуйте снова."
                    if admin_id:
                        send_message(admin_id, msg)
                    else:
                        logger.error(msg)
                    return False
                chunks.append(chunk)
            file_data = b''.join(chunks)
        # ФИКС Баг-2: TgAdapter.upload_photo — заглушка, для TG пропускаем
        # upload и передаём BytesIO в send_attachment.
        import io as _io
        _adapter = get_adapter()
        if getattr(_adapter, "platform", None) == "tg":
            attachment = _io.BytesIO(file_data)
            attachment.seek(0)
        else:
            try:
                attachment = _run_async(_adapter.upload_photo(file_bytes=file_data))
            except Exception as e:
                raise Exception(f"upload_photo failed: {e}")
            if not attachment:
                raise Exception("upload_photo вернул None")
        sent = False
        for photo_attempt in range(2):
            try:
                if _run_async(get_adapter().send_attachment(user_id, attachment, keep=True)):
                    sent = True
                    break
            except Exception as e:
                logger.error("Send banner photo failed (user %s, attempt %d/2): %s",
                             user_id, photo_attempt + 1, e)
            if photo_attempt < 1:
                time.sleep(1)
        if not sent:
            raise Exception("send_attachment не удалось после 2 попыток")

        # ФИКС: если баннер ушёл как community-owned VK-строка
        # (photo-XXXX_YYYY), обновляем banner_url и media_files,
        # чтобы при следующем открытии карточки VK-админом мы
        # отправляли реальное вложение, а не user-URL.
        if platform() == "vk" and isinstance(attachment, str) \
                and attachment.startswith("photo-"):
            upd_conn = None
            try:
                upd_conn = get_db_connection()
                with upd_conn.cursor() as cur:
                    cur.execute(
                        "UPDATE bright_city_applications "
                        "SET banner_url = %s WHERE id = %s",
                        (attachment, app_id))
                    parts = attachment.replace("photo", "").split("_")
                    if len(parts) == 2:
                        try:
                            new_owner = int(parts[0])
                            new_photo = int(parts[1])
                        except ValueError:
                            new_owner = new_photo = None
                        if new_owner is not None:
                            cur.execute(
                                "UPDATE media_files "
                                "SET url = %s, owner_id = %s, photo_id = %s "
                                "WHERE application_id = %s AND kind = 'banner'",
                                (attachment, new_owner, new_photo, app_id))
                upd_conn.commit()
                logger.info(
                    "banner_url обновлён на community-owned: %s (app=%s)",
                    attachment, app_id)
            except Exception as e:
                logger.warning("banner_url update failed: %s", e)
            finally:
                if upd_conn:
                    upd_conn.rollback()
                    return_db_connection(upd_conn)

        send_message(user_id,
            "🎨 Ваш макет баннера готов!\n\nПроверьте, пожалуйста, все данные на макете: имя, возраст, "
            "фотографию, рисунок, информацию о педагоге/организации.\n\n"
            "После печати внести изменения будет невозможно.\n\n"
            "Если всё верно — просто напишите «Всё верно» в ответ.\nЕсли нужно что-то исправить — опишите, что не так.")
        save_message_to_db(user_id, "out",
            "🎨 Ваш макет баннера готов!\n\n"
            "Проверьте, пожалуйста, все данные на макете: имя, возраст, "
            "фотографию, рисунок, информацию о педагоге/организации.\n\n"
            "После печати внести изменения будет невозможно.\n\n"
            "Если всё верно — просто напишите «Всё верно» в ответ.\n"
            "Если нужно что-то исправить — опишите, что не так.",
            application_id=app_id)
        return True
    except Exception as e:
        logger.error("Ошибка отправки макета: %s", e)
        if admin_id:
            send_message(admin_id, f"⚠ Не удалось отправить фото участнику. URL: {banner_url}\n\nПопробуйте загрузить макет заново — возможно, временная ошибка VK.")
        else:
            send_message(user_id, "⚠ Не удалось отправить вам фото макета. Администратор попробует отправить повторно позже.")
        return False
    finally:
        if response is not None:
            response.close()


# ============================================================
#  SUDO-ПОДТВЕРЖДЕНИЯ
# ============================================================

SUDO_TTL_SECONDS = 60


# Если in_progress-заявка висит у одного админа дольше N часов —
# разрешаем перехват. Иначе админ уволился, а заявка «умерла».
_IN_PROGRESS_STALE_SEC = 24 * 3600


def _expire_old_sudo_sessions():
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM admin_sudo_sessions WHERE expires_at < NOW()")
        conn.commit()
    except Exception as e:
        logger.error("DB error (_expire_old_sudo_sessions): %s", e)
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)


def create_sudo_request(admin_id, action, description, target=None, params=None):
    _expire_old_sudo_sessions()
    request_id = uuid.uuid4().hex[:22]
    conn = None
    try:
        pl = platform()
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO admin_sudo_sessions
                    (request_id, platform, admin_id, action, target, params, expires_at)
                VALUES (%s, %s, %s, %s, %s, %s, NOW() + make_interval(secs => %s))
            """, (request_id, pl, admin_id, action,
                  str(target) if target is not None else None,
                  _json_dumps(params or {}),
                  SUDO_TTL_SECONDS))
        conn.commit()
    except Exception as e:
        logger.error("DB error (create sudo session): %s", e)
        send_message(admin_id, "❌ Не удалось создать запрос подтверждения.")
        return False
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)

    keyboard = build_inline_keyboard([
        [text_button("✅ Подтвердить", "positive", {"action": "sudo_ok",   "rid": request_id})],
        [text_button("❌ Отмена",      "negative", {"action": "sudo_no",   "rid": request_id})],
    ])
    send_message(admin_id,
        f"⚠️ Подтвердите опасное действие\n\n"
        f"{description}\n\n"
        f"⏱ У вас {SUDO_TTL_SECONDS} секунд.",
        keyboard=keyboard)
    return True


def _json_dumps(obj):
    return json.dumps(obj, ensure_ascii=False)


def _fetch_and_consume_sudo(admin_id, request_id):
    conn = None
    row = None
    try:
        pl = platform()
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT action, target, params,
                       EXTRACT(EPOCH FROM (expires_at - NOW()))
                FROM admin_sudo_sessions
                WHERE platform = %s AND request_id = %s AND admin_id = %s
                FOR UPDATE
            """, (pl, request_id, admin_id))
            row = cur.fetchone()
            if row:
                cur.execute(
                    "DELETE FROM admin_sudo_sessions "
                    "WHERE platform = %s AND request_id = %s",
                    (pl, request_id))
        conn.commit()
    except Exception as e:
        logger.error("DB error (consume sudo session): %s", e)
        return None
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)
    if not row:
        return None
    action, target, params, ttl_left = row
    if ttl_left is not None and ttl_left < 0:
        return None
    return (action, target, params or {})


def cancel_sudo_request(admin_id, request_id):
    conn = None
    action = None
    target = None
    try:
        pl = platform()
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT action, target FROM admin_sudo_sessions
                WHERE platform = %s AND request_id = %s AND admin_id = %s
            """, (pl, request_id, admin_id))
            row = cur.fetchone()
            if row:
                action, target = row
                cur.execute(
                    "DELETE FROM admin_sudo_sessions "
                    "WHERE platform = %s AND request_id = %s AND admin_id = %s",
                    (pl, request_id, admin_id))
                affected = cur.rowcount
            else:
                affected = 0
        conn.commit()
        if affected:
            send_message(admin_id, "✅ Действие отменено.")
            user_state.set(admin_id, STATE_ADMIN)
            if action == "reject" and target:
                try:
                    handle_admin_application_detail(admin_id, int(target))
                    return
                except Exception as e:
                    logger.error("Failed to return to app_detail after sudo cancel: %s", e)
            show_admin_menu(admin_id)
        else:
            send_message(admin_id,
                f"⏱ Время подтверждения истекло ({SUDO_TTL_SECONDS} секунд). Действие не выполнено.")
    except Exception as e:
        logger.error("DB error (cancel_sudo): %s", e)
        send_message(admin_id, "❌ Не удалось отменить.")
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)


def _cancel_all_sudo_for_admin(admin_id):
    conn = None
    try:
        pl = platform()
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM admin_sudo_sessions "
                "WHERE platform = %s AND admin_id = %s",
                (pl, admin_id))
        conn.commit()
    except Exception as e:
        logger.error("DB error (_cancel_all_sudo_for_admin): %s", e)
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)


def execute_sudo_action(admin_id, request_id):
    payload = _fetch_and_consume_sudo(admin_id, request_id)
    if not payload:
        send_message(admin_id,
            f"⏱ Время подтверждения истекло ({SUDO_TTL_SECONDS} секунд). Действие не выполнено.")
        user_state.set(admin_id, STATE_ADMIN)
        view, ctx = get_admin_view(admin_id)
        if view in ("reject_reason", "send_banner", "reply"):
            handle_admin_back(admin_id)
        else:
            show_admin_menu(admin_id)
        return
    action, target, params = payload
    try:
        if action == "ban":
            _exec_ban(admin_id, target, params.get("reason", ""),
                      target_platform=(params or {}).get("platform"))
        elif action == "unban":
            target_platform = (params or {}).get("platform")
            _exec_unban(admin_id, target, target_platform=target_platform)
        elif action == "reject":
            _exec_reject(admin_id, target, params.get("reason", ""))
        elif action == "add_admin":
            _exec_add_admin(admin_id, target)
        elif action == "remove_admin":
            _exec_remove_admin(admin_id, target)
        elif action == "gdpr_delete":
            _exec_gdpr(admin_id, target, mode="delete", params=params)
            _refresh_gdpr_view_if_open(admin_id)
        elif action == "gdpr_anon":
            _exec_gdpr(admin_id, target, mode="anon", params=params)
            _refresh_gdpr_view_if_open(admin_id)
        else:
            send_message(admin_id, f"❌ Неизвестное действие: {action}")
            show_admin_menu(admin_id)
    except Exception as e:
        logger.error("Sudo action '%s' failed: %s", action, e)
        send_message(admin_id, f"❌ Ошибка выполнения: {e}")
        user_state.set(admin_id, STATE_ADMIN)
        if action == "reject" and target:
            try:
                handle_admin_application_detail(admin_id, int(target))
                return
            except Exception as e2:
                logger.error("Failed to return to app_detail: %s", e2)
        show_admin_menu(admin_id)


def _refresh_gdpr_view_if_open(admin_id):
    """Если админ находился на списке GDPR-запросов — обновляем его."""
    try:
        view, _ = get_admin_view(admin_id)
        if view == "gdpr_requests":
            show_admin_gdpr_requests(admin_id, page=0)
    except Exception as e:
        logger.warning("refresh gdpr_requests view failed: %s", e)


def _exec_ban(admin_id, target, reason="", target_platform=None):
    """Банит пользователя на указанной платформе."""
    # ФИКС отступов: as_positive_int не бросает исключений.
    target_id = as_positive_int(target)
    if target_id is None:
        send_message(admin_id, "❌ Некорректный user_id.")
        return
    pl = target_platform or platform()
    if is_admin_user(target_id, target_platform=pl):
        send_message(admin_id, "Нельзя заблокировать администратора.")
        return
    if ban_user(target_id, admin_id, reason, target_platform=pl):
        log_admin_action(admin_id, "ban", target_id=target_id,
                         params={"reason": reason, "platform": pl})
        conn = None
        try:
            conn = get_db_connection()
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE banned_users SET ban_count = 0 "
                    "WHERE platform = %s AND user_id = %s",
                    (pl, target_id))
            conn.commit()
        except Exception:
            if conn:
                conn.rollback()
        finally:
            if conn:
                return_db_connection(conn)
        try:
            send_message(target_id, "🚫 Вы заблокированы администратором.",
                         target_platform=pl)
        except Exception:
            pass
        pl_txt = f" ({pl.upper()})"
        send_message(admin_id,
            f"🚫 Пользователь {target_id}{pl_txt} заблокирован."
            + (f" Причина: {reason}" if reason else ""))
    else:
        send_message(admin_id,
            f"❌ Не удалось заблокировать пользователя {target_id} ({pl.upper()}).")


def _exec_unban(admin_id, target, target_platform=None):
    # ФИКС отступов: as_positive_int не бросает исключений.
    target_id = as_positive_int(target)
    if target_id is None:
        send_message(admin_id, "❌ Некорректный user_id.")
        return
    actual_pl = target_platform or platform()
    if unban_user(target_id, target_platform=actual_pl):
        log_admin_action(admin_id, "unban", target_id=target_id,
                         params={"platform": actual_pl})
        send_message(admin_id,
            f"✅ Пользователь {target_id} ({actual_pl.upper()}) разблокирован.")
    else:
        send_message(admin_id,
            f"❌ Не удалось разблокировать пользователя {target_id} ({actual_pl.upper()}).")


def _exec_add_admin(admin_id, target):
    # ФИКС отступов: as_positive_int не бросает исключений.
    target_id = as_positive_int(target)
    if target_id is None:
        send_message(admin_id, "❌ Некорректный user_id.")
        return
    if target_id in BOOTSTRAP_ADMIN_IDS:
        send_message(admin_id, f"Пользователь {target_id} уже bootstrap-админ.")
        return
    if add_dynamic_admin(target_id, admin_id):
        log_admin_action(admin_id, "add_admin", target_id=target_id)
        send_message(admin_id, f"✅ Пользователь {target_id} назначен админом.")
        try:
            send_message(target_id,
                "🔑 Вам выданы права администратора.\nДля входа в панель: /админ")
        except Exception:
            pass
    else:
        send_message(admin_id, f"❌ Не удалось назначить админа {target_id}.")


def _exec_remove_admin(admin_id, target):
    # ФИКС отступов: as_positive_int не бросает исключений.
    target_id = as_positive_int(target)
    if target_id is None:
        send_message(admin_id, "❌ Некорректный user_id.")
        return
    if target_id in BOOTSTRAP_ADMIN_IDS:
        send_message(admin_id, "⛔ Нельзя снять bootstrap-админа (.env).")
        return
    if remove_dynamic_admin(target_id):
        log_admin_action(admin_id, "remove_admin", target_id=target_id)
        send_message(admin_id, f"✅ Права админа у {target_id} сняты.")
        try:
            send_message(target_id, "🔒 Ваши права администратора отозваны.")
        except Exception:
            pass
    else:
        send_message(admin_id, f"❌ Не удалось снять права с {target_id}.")


def _exec_reject(admin_id, app_id, reason_text):
    try:
        app_id = int(app_id)
    except (TypeError, ValueError):
        send_message(admin_id, "❌ Некорректный app_id.")
        return
    if len(reason_text) > MAX_TEXT_LENGTH:
        reason_text = reason_text[:MAX_TEXT_LENGTH]

    admin_name = get_admin_name(admin_id)
    conn = None
    target_platform = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, assigned_admin_id, opened_by_admin_id, user_id, "
                "       created_at, platform, status_changed_at "
                "FROM bright_city_applications WHERE id = %s FOR UPDATE",
                (app_id,))
            row = cur.fetchone()
            if not row:
                send_message(admin_id, f"Заявка #{app_id} не найдена.")
                conn.rollback()
                show_admin_menu(admin_id)
                return
            (old_status, assigned_id, opened_id, target_user_id,
             created_at, target_platform, status_changed_at) = row

            if old_status == "new":
                if opened_id is not None and opened_id != admin_id:
                    cur.execute(
                        "SELECT opened_at FROM bright_city_applications WHERE id = %s",
                        (app_id,))
                    opened_at_row = cur.fetchone()
                    if opened_at_row and opened_at_row[0] is not None:
                        cur.execute("SELECT EXTRACT(EPOCH FROM (NOW() - %s))", (opened_at_row[0],))
                        elapsed = cur.fetchone()[0]
                        if elapsed < 300:
                            n = get_user_name(opened_id)
                            send_message(admin_id,
                                f"🔒 Над заявкой #{app_id} работает админ: {n[0]} {n[1]}")
                            conn.rollback()
                            handle_admin_back(admin_id)
                            return
                    else:
                        n = get_user_name(opened_id)
                        send_message(admin_id,
                            f"🔒 Над заявкой #{app_id} работает админ: {n[0]} {n[1]}")
                        conn.rollback()
                        handle_admin_back(admin_id)
                        return
            else:
                if assigned_id is not None and assigned_id != admin_id:
                    timeout_ts = status_changed_at or created_at
                    stale = False
                    if timeout_ts is not None:
                        cur.execute("SELECT EXTRACT(EPOCH FROM (NOW() - %s))", (timeout_ts,))
                        elapsed = cur.fetchone()[0]
                        stale = elapsed >= _IN_PROGRESS_STALE_SEC
                    if not stale:
                        n = get_user_name(assigned_id)
                        send_message(admin_id,
                            f"🔒 Над заявкой #{app_id} работает админ: {n[0]} {n[1]}")
                        conn.rollback()
                        handle_admin_back(admin_id)
                        return
                    # Перехват зависшей заявки.
                    cur.execute(
                        """UPDATE bright_city_applications
                              SET assigned_admin_id = %s, assigned_admin_name = %s,
                                  opened_by_admin_id = NULL, opened_by_admin_name = NULL,
                                  opened_at = NULL
                            WHERE id = %s""",
                        (admin_id, admin_name, app_id))

            standard_transitions = {
                "new": {"in_progress", "rejected"},
                "in_progress": {"done", "rejected"},
                "rejected": {"in_progress"},
                "done": set(),
            }
            if "rejected" not in standard_transitions.get(old_status, set()):
                send_message(admin_id,
                    f"⛔ Переход из статуса «{get_status_label(old_status)}» "
                    f"в «{get_status_label('rejected')}» недопустим.")
                conn.rollback()
                handle_admin_application_detail(admin_id, app_id)
                return

            cur.execute(
                "UPDATE bright_city_applications "
                "SET status = 'rejected', rejection_reason = %s WHERE id = %s",
                (reason_text, app_id))
            _log_status_change_with_cursor(
                cur, app_id, old_status, "rejected", admin_id, admin_name)
        conn.commit()
    except Exception as e:
        logger.error("DB error (exec reject): %s", e)
        send_message(admin_id, f"❌ Ошибка при отклонении заявки #{app_id}.")
        user_state.set(admin_id, STATE_ADMIN)
        try:
            handle_admin_application_detail(admin_id, app_id)
        except Exception as e2:
            logger.error("Failed to return to app_detail: %s", e2)
        return
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)

    log_admin_action(admin_id, "reject", target_id=app_id,
                     params={"reason": reason_text})

    try:
        notify_admin_applications(
            f"❌ Заявка #{app_id} → {get_status_label('rejected')} "
            f"(админ: {admin_name})\nПричина: {reason_text}")

        target_pl = target_platform or platform()
        is_cross = (target_pl != platform())
        if is_cross:
            target_state = _get_user_state_for_platform(target_user_id, target_pl)
        else:
            target_state = user_state.get(target_user_id)

        if target_state in (STATE_NEXT_ACTION, STATE_EXTRAS):
            user_msg = (f"К сожалению, по вашей заявке есть замечания.\n\n"
                        f"Причина: {reason_text}\n\n"
                        f"Вы можете подать новую заявку или задать вопрос администратору.")
            kb = None if is_cross else get_rejection_keyboard()
        else:
            user_msg = (f"К сожалению, по вашей заявке есть замечания.\n\n"
                        f"Причина: {reason_text}")
            kb = None

        notified = send_message(target_user_id, user_msg, keyboard=kb,
                                target_platform=target_pl)
        if get_admin_settings(admin_id)["notify_applications"]:
            if notified:
                logger.info(
                    "_exec_reject: заявка #%s отклонена, user %s уведомлён",
                    app_id, target_user_id)
            else:
                send_message(admin_id,
                    f"⚠ Заявка #{app_id} отклонена, но уведомление "
                    f"пользователю {target_user_id} НЕ доставлено.")
        else:
            if notified:
                send_message(admin_id,
                    f"→ Пользователь {target_user_id} уведомлён. "
                    f"Причина отклонения сохранена.")
            else:
                send_message(admin_id,
                    f"⚠ Заявка #{app_id} отклонена, но уведомление "
                    f"пользователю {target_user_id} НЕ доставлено.")
    except Exception as notify_err:
        logger.error(
            "Ошибка отправки уведомлений после отклонения заявки #%s: %s",
            app_id, notify_err)
    user_state.set(admin_id, STATE_ADMIN)
    handle_admin_application_detail(admin_id, app_id)


def _exec_gdpr(admin_id, target, mode="delete", params=None):
    """Удаление/анонимизация ПДн."""
    if not target or ":" not in target:
        send_message(admin_id, "❌ Некорректный target GDPR.")
        return
    tp, tuid_str = target.split(":", 1)
    try:
        tuid = int(tuid_str)
    except ValueError:
        send_message(admin_id, "❌ Некорректный user_id.")
        return

    pending_request_id = None
    notify_user = False
    ap = platform()

    conn = get_db_connection()
    try:
        # Блокировка НЕ снимается до конца транзакции — мы передадим
        # это же соединение в delete_user_data/anonymize_user_data,
        # чтобы параллельный админ не смог выполнить своё удаление,
        # пока наше ещё не зафиксировано.
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM gdpr_requests "
                "WHERE platform = %s AND user_id = %s AND fulfilled = FALSE "
                "ORDER BY requested_at ASC LIMIT 1 "
                "FOR UPDATE",
                (tp, tuid))
            row = cur.fetchone()
            pending_request_id = row[0] if row else None

        notify_user = pending_request_id is not None
        if notify_user:
            send_message(admin_id,
                "ℹ️ У пользователя есть открытый запрос — уведомление "
                "будет отправлено автоматически (152-ФЗ).")

        log_admin_action(
            admin_id,
            "gdpr_delete" if mode == "delete" else "gdpr_anonymize",
            target_platform=tp,
            target_id=tuid,
            params={
                "request_id": pending_request_id,
                "source": (params or {}).get("source", "sudo"),
            },
        )

        if mode == "anon":
            result = anonymize_user_data(
                tp, tuid, admin_id=admin_id, admin_platform=ap,
                notify_user=notify_user, existing_conn=conn)
            action_label = "анонимизированы"
        else:
            result = delete_user_data(
                tp, tuid, admin_id=admin_id, admin_platform=ap,
                notify_user=notify_user, existing_conn=conn)
            action_label = "удалены"

        if result is None:
            send_message(
                admin_id,
                f"❌ Ошибка удаления {tp}/{tuid}. "
                f"Запрос пользователя оставлен открытым для повторной попытки.")
            return

        lines = [f"✅ Данные {tp}/{tuid} {action_label}:", ""]
        for key, val in result.items():
            if val:
                lines.append(f"  {key}: {val}")
        if all(v == 0 for v in result.values()):
            lines.append("  (ничего не найдено)")
        send_message(admin_id, "\n".join(lines))
    finally:
        # delete_user_data сделал COMMIT → блокировка снята. Rollback
        # здесь безвреден (no-op после commit).
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
            return_db_connection(conn)

# ============================================================
#  КОМАНДЫ УПРАВЛЕНИЯ АДМИНАМИ
# ============================================================

def handle_admin_add_cmd(admin_id, text):
    if not _is_bootstrap_admin_here(admin_id):
        send_message(admin_id, "⛔ Только bootstrap-админ (из .env) может назначать админов.")
        return
    parts = text.split()
    if len(parts) < 2:
        send_message(admin_id, "Формат: /добавить_админа <user_id>")
        return
    target = as_positive_int(parts[1])
    if target is None:
        send_message(admin_id, "user_id должен быть числом.")
        return
    if target == admin_id:
        send_message(admin_id, "Вы уже админ.")
        return
    if is_admin_user(target):
        send_message(admin_id, f"Пользователь {target} уже админ.")
        return
    create_sudo_request(admin_id, "add_admin",
                        f"Назначить пользователя {target} админом?", target=target)


def handle_admin_remove_cmd(admin_id, text):
    if not _is_bootstrap_admin_here(admin_id):
        send_message(admin_id, "⛔ Только bootstrap-админ (из .env) может снимать админов.")
        return
    parts = text.split()
    if len(parts) < 2:
        send_message(admin_id, "Формат: /удалить_админа <user_id>")
        return
    target = as_positive_int(parts[1])
    if target is None:
        send_message(admin_id, "user_id должен быть числом.")
        return
    if target == admin_id:
        send_message(admin_id, "Нельзя снять права с самого себя.")
        return
    if target in BOOTSTRAP_ADMIN_IDS:
        send_message(admin_id, "⛔ Нельзя снять bootstrap-админа (.env).")
        return
    if not is_admin_user(target):
        send_message(admin_id, f"Пользователь {target} не является админом.")
        return
    create_sudo_request(admin_id, "remove_admin",
                        f"Снять права админа у {target}?", target=target)


def handle_admin_list_cmd(admin_id, text):
    if not is_admin_user(admin_id):
        return
    rows = list_admins()
    if not rows:
        send_message(admin_id, "Список админов пуст.",
                     keyboard=get_admin_menu_keyboard())
        return
    lines = ["🔑 Активные админы:\n"]
    for r in rows:
        first, last = get_user_name(r["admin_id"])
        tag = "🛡 bootstrap" if r["is_bootstrap"] else "👤 dynamic"
        lines.append(f"{tag} | {first} {last} (ID: {r['admin_id']})")
        if not r["is_bootstrap"] and r["added_at"]:
            lines.append(f"     добавлен: {r['added_at'].strftime('%d.%m.%Y %H:%M')}")
    send_message(admin_id, "\n".join(lines),
                 keyboard=get_admin_menu_keyboard())


# ============================================================
#  БАН / РАЗБАН (команды)
# ============================================================

def handle_admin_ban(admin_id, text):
    """Формат: /бан <vk|tg|max> <user_id> [причина]"""
    parts = text.split(maxsplit=3)
    if len(parts) < 3:
        send_message(admin_id,
            "Формат: /бан <vk|tg|max> <user_id> [причина]\n"
            "Пример: /бан vk 123456 спам\n"
            "        /бан tg 999999991 мошенничество")
        return

    target_platform = parts[1].strip().lower()
    if target_platform not in ("vk", "tg", "max"):
        send_message(admin_id,
            f"Платформа должна быть vk, tg или max (получено: {parts[1]!r}).\n"
            "Пример: /бан vk 123456 спам")
        return

    # ФИКС отступов: as_positive_int не бросает ValueError.
    target = as_positive_int(parts[2])
    if target is None:
        send_message(admin_id, "user_id должен быть числом. Пример: /бан vk 123456 спам")
        return

    if is_admin_user(target, target_platform=target_platform):
        send_message(admin_id, "Нельзя заблокировать администратора.")
        return

    reason = parts[3] if len(parts) > 3 else ""
    pl_line = f"Платформа: {target_platform.upper()}"
    if target_platform != platform():
        pl_line += f" (кросс-платформенный бан из {platform().upper()})"
    desc = f"Заблокировать пользователя {target}?\n\n{pl_line}"
    if reason:
        desc += f"\nПричина: {reason}"

    create_sudo_request(
        admin_id, "ban", desc, target=target,
        params={"reason": reason, "platform": target_platform},
    )


def handle_admin_unban(admin_id, text):
    """Формат: /разбан <vk|tg|max> <user_id>"""
    parts = text.split(maxsplit=2)
    if len(parts) < 3:
        send_message(admin_id,
            "Формат: /разбан <vk|tg|max> <user_id>\n"
            "Пример: /разбан vk 123456\n"
            "        /разбан tg 999999991")
        return

    target_platform = parts[1].strip().lower()
    if target_platform not in ("vk", "tg", "max"):
        send_message(admin_id,
            f"Платформа должна быть vk, tg или max (получено: {parts[1]!r}).\n"
            "Пример: /разбан vk 123456")
        return

    # ФИКС отступов: as_positive_int не бросает ValueError.
    target = as_positive_int(parts[2])
    if target is None:
        send_message(admin_id, "user_id должен быть числом. Пример: /разбан vk 123456")
        return

    pl_line = f"Платформа: {target_platform.upper()}"
    if target_platform != platform():
        pl_line += f" (кросс-платформенный разбан из {platform().upper()})"
    create_sudo_request(
        admin_id, "unban",
        f"Разблокировать пользователя {target}?\n\n{pl_line}",
        target=target,
        params={"platform": target_platform},
    )


# ============================================================
#  GDPR-КОМАНДЫ
# ============================================================

def handle_admin_delete_data(admin_id, text):
    if not _is_bootstrap_admin_here(admin_id):
        send_message(admin_id, "⛔ Только bootstrap-админ.")
        return
    parts = text.split()
    if len(parts) < 3:
        send_message(admin_id,
            "Формат: /удалить_данные <platform> <user_id> [anon]\n"
            "  anon — анонимизировать вместо удаления\n\n"
            "Уведомление отправляется автоматически, если у пользователя есть "
            "открытый запрос на удаление ПДн.")
        return
    target_platform = parts[1].strip().lower()
    if target_platform not in ("vk", "tg", "max"):
        send_message(admin_id, f"Неизвестная платформа: {target_platform}.")
        return
    target_user_id = as_positive_int(parts[2])
    if target_user_id is None:
        send_message(admin_id, "user_id должен быть числом.")
        return
    flags = set(p.lower() for p in parts[3:])
    mode = "anon" if "anon" in flags else "delete"
    preview = preview_user_data(target_platform, target_user_id)
    if preview is None:
        send_message(admin_id, "Ошибка БД.")
        return
    if sum(preview.values()) == 0:
        send_message(admin_id, f"У пользователя {target_platform}/{target_user_id} нет данных.")
        return
    has_req = has_pending_gdpr_request(target_platform, target_user_id)
    mode_text = "анонимизировать" if mode == "anon" else "УДАЛИТЬ"
    req_text = ("\n\n🔔 У пользователя есть открытый запрос — уведомление "
                "будет отправлено автоматически.") if has_req else ""
    last = get_last_gdpr_outcome(target_platform, target_user_id)
    if last:
        when = (last["created_at"].strftime("%d.%m.%Y %H:%M")
                if last["created_at"] else "—")
        outcome_ru = _OUTCOME_LABELS_RU.get(last["outcome"], last["outcome"])
        history_text = (f"\n\nℹ️ Ранее данные уже были {outcome_ru} "
                        f"({when}, админ {last['admin_id']}/"
                        f"{last['admin_platform']}).")
    else:
        history_text = ""

    desc = (f"{mode_text} данные пользователя?\n\n"
            f"Платформа: {target_platform}\nUser ID: {target_user_id}\n\n"
            f"Найдено:\n"
            f"  Заявок: {preview['applications']}\n"
            f"  Сообщений: {preview['bot_messages']}\n"
            f"  Состояний: {preview['bot_user_states']}\n"
            f"  Записей в банлисте: {preview['banned_users']}"
            f"{req_text}{history_text}\n\n⚠ Действие необратимо.")
    create_sudo_request(admin_id, f"gdpr_{mode}", desc,
                        target=f"{target_platform}:{target_user_id}",
                        # ФИКС Баг-19: источник — текстовая команда /удалить_данные.
                        params={"source": "command"})


def handle_admin_gdpr_log(admin_id, text):
    if not _is_bootstrap_admin_here(admin_id):
        send_message(admin_id, "⛔ Только bootstrap-админ.",
                     keyboard=get_admin_menu_keyboard())
        return
    parts = text.split()
    limit = 20
    if len(parts) > 1:
        try:
            limit = max(1, min(int(parts[1]), 100))
        except ValueError:
            pass
    rows = get_gdpr_audit(limit)
    if not rows:
        send_message(admin_id, "📜 Журнал GDPR пуст.",
                     keyboard=get_admin_menu_keyboard())
        return
    lines = [f"📜 GDPR аудит (последние {len(rows)}):\n"]
    for r in rows:
        aid, apl, tpl, tuid, act, det, ts = r
        act_rus = "удаление" if act == "delete" else "аноним." if act == "anonymize" else act
        lines.append(f"[{ts.strftime('%d.%m %H:%M')}] admin {aid}/{apl} → {act_rus} {tpl}/{tuid}")
    send_message(admin_id, "\n".join(lines),
                 keyboard=get_admin_menu_keyboard())


_OUTCOME_LABELS_RU = {
    "deleted": "удалены",
    "anonymized": "анонимизированы",
}


_GDPR_HISTORY_FULL_LIMIT = 50
_GDPR_STATUS_HISTORY_LIMIT = 5


def handle_admin_gdpr_status(admin_id, text):
    """Формат: /gdpr_статус <platform> <user_id> [--full]"""
    if not _is_bootstrap_admin_here(admin_id):
        send_message(admin_id, "⛔ Только bootstrap-админ.",
                     keyboard=get_admin_menu_keyboard())
        return

    parts = text.split()
    if len(parts) < 3:
        send_message(admin_id,
            "Формат: /gdpr_статус <platform> <user_id> [--full]\n"
            "Пример: /gdpr_статус tg 1352827465",
            keyboard=get_admin_menu_keyboard())
        return

    tp = parts[1].strip().lower()
    if tp not in ("vk", "tg", "max"):
        send_message(admin_id, f"Неизвестная платформа: {tp}.",
                     keyboard=get_admin_menu_keyboard())
        return

    tuid = as_positive_int(parts[2])
    if tuid is None:
        send_message(admin_id, "user_id должен быть числом.",
                     keyboard=get_admin_menu_keyboard())
        return

    show_full = any(p.lower() == "--full" for p in parts[3:])
    hist_limit = _GDPR_HISTORY_FULL_LIMIT if show_full else _GDPR_STATUS_HISTORY_LIMIT

    pending = has_pending_gdpr_request(tp, tuid)
    history = get_gdpr_history_for_subject(tp, tuid, limit=hist_limit)

    lines = [f"🔐 GDPR-статус {tp}/{tuid}", ""]
    lines.append(f"Открытый запрос: {'да' if pending else 'нет'}")

    if not history:
        lines.append("Операций над субъектом не найдено.")
        send_message(admin_id, "\n".join(lines),
                     keyboard=get_admin_menu_keyboard())
        return

    last = history[0]
    when = (last["created_at"].strftime("%d.%m.%Y %H:%M")
            if last["created_at"] else "—")
    outcome_ru = _OUTCOME_LABELS_RU.get(last["outcome"], last["outcome"])
    lines.append(f"Последняя операция: {outcome_ru}")
    lines.append(f"  Когда: {when}")
    lines.append(
        f"  Админ: {last['admin_id']} ({last['admin_platform']})")
    if last["action"] != last["outcome"]:
        lines.append(f"  Источник: {last['action']}")

    if len(history) > 1:
        shown = len(history) - 1
        lines.append("")
        lines.append(f"История ({shown}{'+' if show_full and shown >= hist_limit else ''}):")
        for rec in history[1:]:
            ts = (rec["created_at"].strftime("%d.%m.%Y %H:%M")
                  if rec["created_at"] else "—")
            o_ru = _OUTCOME_LABELS_RU.get(rec["outcome"], rec["outcome"])
            lines.append(
                f"  [{ts}] {o_ru} — админ {rec['admin_id']}/"
                f"{rec['admin_platform']}")

    if not show_full and len(history) >= _GDPR_STATUS_HISTORY_LIMIT:
        lines.append("")
        lines.append("Показаны не все записи. Полная история: "
                     "/gdpr_статус <platform> <user_id> --full")

    send_message(admin_id, "\n".join(lines),
                 keyboard=get_admin_menu_keyboard())


def handle_admin_gdpr_request_cmd(admin_id, text):
    if not _is_bootstrap_admin_here(admin_id):
        send_message(admin_id, "⛔ Только bootstrap-админ.")
        return
    parts = text.split()
    if len(parts) < 3:
        send_message(admin_id,
            "Формат: /gdpr_запрос <platform> <user_id>\n"
            "Пример: /gdpr_запрос tg 1352827465")
        return
    tp = parts[1].strip().lower()
    if tp not in ("vk", "tg", "max"):
        send_message(admin_id, f"Неизвестная платформа: {tp}.")
        return
    tuid = as_positive_int(parts[2])
    if tuid is None:
        send_message(admin_id, "user_id должен быть числом.")
        return
    from core.db import log_gdpr_request
    if log_gdpr_request(tp, tuid):
        send_message(admin_id,
            f"✅ Запрос пользователя {tp}/{tuid} зафиксирован.\n\n"
            f"Теперь при удалении уведомление уйдёт автоматически.\n"
            f"Удалить: /удалить_данные {tp} {tuid}")
    else:
        send_message(admin_id, "❌ Не удалось зафиксировать запрос.")


# ============================================================
#  CHANGELOG
# ============================================================

def _format_changelog_entry(entry):
    lines = [f"━━━ v{entry['version']} ({entry['date']}) ━━━"]
    for section, title in [
        ("added", "➕ Добавлено"),
        ("changed", "✏️ Изменено"),
        ("fixed", "🐛 Исправлено"),
        ("security", "🔒 Безопасность"),
    ]:
        items = entry.get(section) or []
        if items:
            lines.append(f"\n{title}:")
            lines.extend(f"  • {it}" for it in items)
    return "\n".join(lines)


def handle_admin_changelog(user_id, text=""):
    is_full = "full" in (text or "").lower()
    header = f"📜 История изменений бота (текущая версия: {BOT_VERSION})\n"
    if is_full:
        header += ("\n📖 Полная история — в файле CHANGELOG.md в репозитории проекта.\n"
                   "Ниже — краткая сводка последних версий.\n")
    blocks = [_format_changelog_entry(e) for e in CHANGELOG_SHORT]
    body = "\n\n".join(blocks)
    full_text = header + "\n" + body
    if len(full_text.encode("utf-8")) > 4000:
        encoded = full_text.encode("utf-8")[:3900]
        full_text = encoded.decode("utf-8", errors="ignore") + "\n…(обрезано)"
    send_message(user_id, full_text, keyboard=get_admin_menu_keyboard())