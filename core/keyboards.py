# core/keyboards.py
"""Клавиатуры и кнопки в VK-формате (JSON-строка)."""
import json

from core.runtime import platform
from core.texts import (
    PLACEMENT_LOCATIONS, PLACEMENT_LOCATIONS_SHORT, PLACEMENT_PAGE_SIZE,
    USER_TYPE_LABELS, USER_TYPES, CATEGORY_LABELS,
)


def build_keyboard(buttons_rows, one_time=True):
    return json.dumps({"one_time": one_time, "buttons": buttons_rows}, ensure_ascii=False)


def get_empty_keyboard():
    return json.dumps({"one_time": True, "buttons": []}, ensure_ascii=False)


def build_inline_keyboard(buttons_rows):
    return json.dumps({"inline": True, "buttons": buttons_rows}, ensure_ascii=False)


def text_button(label, color="primary", payload=None):
    action = {"type": "text", "label": label[:40]}
    if payload is not None:
        action["payload"] = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return {"action": action, "color": color}


def link_button(label, link):
    return {"action": {"type": "open_link", "label": label[:40], "link": link}}


def contact_button(label="📱 Поделиться контактом", color="primary"):
    limit = 64 if platform() in ("tg", "max") else 40
    return {"action": {"type": "contact", "label": label[:limit]}, "color": color}


def get_main_keyboard():
    return build_keyboard([
        [text_button("ОФОРМИТЬ ЗАЯВКУ НА УЧАСТИЕ", "positive", {"action": "start_registration"})],
        [text_button("Задать вопрос", "primary", {"action": "ask_question"})],
    ], one_time=False)


def get_location_keyboard():
    return build_keyboard([[text_button("Краснодар", "primary", {"action": "select_city", "value": "Краснодар"})]], one_time=True)


def get_placement_location_keyboard(page=0):
    if platform() == "tg":
        rows = []
        for i in range(len(PLACEMENT_LOCATIONS)):
            rows.append([text_button(
                PLACEMENT_LOCATIONS_SHORT[i][:40],
                "primary",
                {"action": "select_placement", "value": PLACEMENT_LOCATIONS[i]}
            )])
        return build_keyboard(rows, one_time=False)

    total = len(PLACEMENT_LOCATIONS_SHORT)
    start = page * PLACEMENT_PAGE_SIZE
    end = min(start + PLACEMENT_PAGE_SIZE, total)
    rows = []
    for i in range(start, end):
        rows.append([text_button(PLACEMENT_LOCATIONS_SHORT[i][:40], "primary", {"action": "select_placement", "value": PLACEMENT_LOCATIONS[i]})])
    has_prev = page > 0
    has_next = end < total
    if has_prev or has_next:
        nav_row = []
        if has_prev:
            nav_row.append(text_button("← Назад", "secondary", {"action": "placement_page", "page": page - 1}))
        if has_next:
            nav_row.append(text_button("Далее →", "secondary", {"action": "placement_page", "page": page + 1}))
        rows.append(nav_row)
    return build_keyboard(rows, one_time=False)


def get_user_type_keyboard():
    return build_keyboard(
        [[text_button(USER_TYPE_LABELS.get(t, t[:40]), "primary", {"action": "select_user_type", "value": t})] for t in USER_TYPES],
        one_time=True,
    )


def get_phone_keyboard():
    if platform() == "tg":
        return build_keyboard([
            [contact_button("👉 Поделиться контактом 👈")],
        ], one_time=True)
    return get_empty_keyboard()


def get_phone_prompt_text():
    if platform() == "tg":
        return ("Для авторизации поделитесь своим номером телефона 👇\n\n"
                "Нажмите «📱 Поделиться контактом» внизу — "
                "номер придёт автоматически.")
    return ("Для авторизации напишите ваш номер телефона 👇\n\n"
            "Формат: +7 XXX XXX-XX-XX или 8 XXX XXX-XX-XX.")


def get_format_keyboard():
    return build_keyboard([
        [text_button("Горизонтальный или квадратный", "primary", {"action": "select_format", "value": "Горизонтальный или квадратный"})],
        [text_button("1 вертикальный", "primary", {"action": "select_format", "value": "1 вертикальный рисунок"})],
        [text_button("2 вертикальных", "primary", {"action": "select_format", "value": "2 вертикальных рисунка"})],
    ], one_time=True)


def get_confirm_keyboard():
    return build_keyboard([[text_button("Отправить заявку", "positive", {"action": "submit_application"})]], one_time=True)


def get_consent_keyboard():
    return build_keyboard([
        [text_button("✅ Согласен", "positive", {"action": "accept_consent"})],
    ], one_time=True)


def get_next_action_keyboard():
    return build_keyboard([
        [text_button("Оформить следующую заявку", "primary", {"action": "next_application"})],
        [text_button("Завершить", "negative", {"action": "finish"})],
    ], one_time=True)


def get_after_submit_inline_keyboard():
    return build_inline_keyboard([
        [text_button("Оформить следующую заявку", "positive", {"action": "next_application"})],
        [text_button("Завершить", "secondary", {"action": "finish"})],
    ])


def get_final_inline_keyboard():
    return build_inline_keyboard([
        [link_button("Перейти в группу ВК", "https://vk.com/yarkijgorod")],
        [link_button("Перейти в канал Телеграм", "https://t.me/yarkijgorod")],
        [link_button("Перейти в канал Макс", "https://max.ru/гjoin/w_jGlxi33X5yXwbXYSkucFH6RMcXlvZ7UAdnwFrn_MI")],
        [link_button("Перейти в канал ВК", "https://vk.ru/im/channels/-239465585")],
        [text_button("Вернуться в главное меню", "positive", {"action": "back_to_main"})],
    ])


def get_rejection_keyboard():
    return build_keyboard([
        [text_button("Подать новую заявку", "positive", {"action": "next_application"})],
        [text_button("Задать вопрос", "primary", {"action": "ask_question"})],
    ], one_time=True)


def get_extras_keyboard():
    return build_keyboard([[text_button("Пропустить", "primary", {"action": "skip_extras"})]], one_time=True)


def get_question_keyboard():
    return build_keyboard([[text_button("Вернуться к оформлению заявки", "secondary", {"action": "back_to_registration"})]], one_time=True)


def get_org_info_keyboard(can_skip=True, has_previous=False):
    rows = [[text_button("Подтвердить", "positive", {"action": "confirm_org_info"})]]
    if can_skip and has_previous:
        rows.append([text_button("Использовать предыдущие данные", "secondary", {"action": "skip_org_info"})])
    return build_keyboard(rows, one_time=False)


def get_child_photo_keyboard(loaded=0):
    label = "Подтвердить фото" if not loaded else f"Подтвердить фото ({loaded})"
    return build_keyboard([[text_button(label, "positive", {"action": "confirm_child_photo"})]], one_time=False)


def get_drawings_keyboard(loaded=0):
    label = "Подтвердить рисунки" if not loaded else f"Подтвердить рисунки ({loaded})"
    return build_keyboard([[text_button(label, "positive", {"action": "confirm_drawings"})]], one_time=False)


def get_admin_cancel_banner_keyboard():
    return build_keyboard([[text_button("← Назад", "negative", {"action": "admin_back"})]], one_time=False)


def get_admin_cancel_reply_keyboard():
    return build_keyboard([[text_button("← Назад", "negative", {"action": "admin_back"})]], one_time=False)


def get_admin_status_keyboard(app_id, current_status=None, banner_sent=False, is_completed=False):
    rows = []
    if is_completed:
        rows.append([text_button("💬 Написать пользователю", "primary", {"action": "admin_start_reply", "app_id": app_id})])
    elif current_status == "new":
        rows.append([text_button("🔄 В обработке", "primary", {"action": "set_status", "app_id": app_id, "status": "in_progress"})])
        rows.append([text_button("❌ Отклонена", "negative", {"action": "set_status", "app_id": app_id, "status": "rejected"})])
        rows.append([text_button("💬 Написать пользователю", "primary", {"action": "admin_start_reply", "app_id": app_id})])
    elif current_status == "in_progress":
        rows.append([text_button("📤 Отправить макет", "primary", {"action": "admin_send_banner", "app_id": app_id})])
        rows.append([text_button("❌ Отклонена", "negative", {"action": "set_status", "app_id": app_id, "status": "rejected"})])
        rows.append([text_button("💬 Написать пользователю", "primary", {"action": "admin_start_reply", "app_id": app_id})])
    elif current_status == "rejected":
        rows.append([text_button("🔄 В обработке", "primary", {"action": "set_status", "app_id": app_id, "status": "in_progress"})])
        rows.append([text_button("💬 Написать пользователю", "primary", {"action": "admin_start_reply", "app_id": app_id})])
    elif current_status == "done" and not banner_sent:
        rows.append([text_button("📤 Отправить макет", "primary", {"action": "admin_send_banner", "app_id": app_id})])
    rows.append([text_button("← Назад", "secondary", {"action": "admin_back"})])
    return build_keyboard(rows, one_time=True)


def get_admin_apps_keyboard(app_entries, page=0, per_page=10):
    rows = []
    total = len(app_entries)
    start = page * per_page
    end = min(start + per_page, total)
    has_prev = page > 0
    has_next = end < total
    for i in range(start, end, 2):
        row = []
        entry = app_entries[i]
        row.append(text_button(f"#{entry['id']} ({entry['ts']})", "primary", {"action": "view_app", "app_id": entry["id"]}))
        if i + 1 < end:
            entry2 = app_entries[i + 1]
            row.append(text_button(f"#{entry2['id']} ({entry2['ts']})", "primary", {"action": "view_app", "app_id": entry2["id"]}))
        rows.append(row)
    if has_prev or has_next:
        nav_row = []
        if has_prev:
            nav_row.append(text_button("← Стр.", "secondary", {"action": "admin_page", "page": page - 1}))
        if has_next:
            nav_row.append(text_button("Стр. →", "secondary", {"action": "admin_page", "page": page + 1}))
        rows.append(nav_row)
    rows.append([text_button("← Назад", "secondary", {"action": "admin_back"})])
    return build_keyboard(rows, one_time=True)


def get_admin_incomplete_keyboard(app_entries, page=0, per_page=10):
    rows = []
    total = len(app_entries)
    start = page * per_page
    end = min(start + per_page, total)
    has_prev = page > 0
    has_next = end < total
    for i in range(start, end, 2):
        row = []
        entry = app_entries[i]
        row.append(text_button(f"ID:{entry['id']} ({entry['ts']})", "primary", {"action": "view_incomplete", "user_id": entry["id"]}))
        if i + 1 < end:
            entry2 = app_entries[i + 1]
            row.append(text_button(f"ID:{entry2['id']} ({entry2['ts']})", "primary", {"action": "view_incomplete", "user_id": entry2["id"]}))
        rows.append(row)
    if has_prev or has_next:
        nav_row = []
        if has_prev:
            nav_row.append(text_button("← Стр.", "secondary", {"action": "admin_page", "page": page - 1}))
        if has_next:
            nav_row.append(text_button("Стр. →", "secondary", {"action": "admin_page", "page": page + 1}))
        rows.append(nav_row)
    rows.append([text_button("← Назад", "secondary", {"action": "admin_back"})])
    return build_keyboard(rows, one_time=True)


def get_admin_categories_keyboard():
    cat_items = list(CATEGORY_LABELS.items())
    rows = []
    for i in range(0, len(cat_items), 2):
        row = []
        cat_key_1, cat_label_1 = cat_items[i]
        row.append(text_button(cat_label_1, "primary", {"action": "show_category", "category": cat_key_1}))
        if i + 1 < len(cat_items):
            cat_key_2, cat_label_2 = cat_items[i + 1]
            row.append(text_button(cat_label_2, "primary", {"action": "show_category", "category": cat_key_2}))
        rows.append(row)
    rows.append([text_button("← Назад", "secondary", {"action": "admin_back"})])
    return build_keyboard(rows, one_time=False)


def get_admin_menu_keyboard(has_pending_gdpr: bool = False):
    rows = [
        [text_button("📋 Все заявки", "primary", {"action": "admin_categories"})],
        [text_button("📊 Статистика", "primary", {"action": "admin_stats"}),
         text_button("⚙️ Настройки", "primary", {"action": "admin_settings"})],
        [text_button("🔍 Поиск по ФИО", "primary", {"action": "admin_search_prompt"})],
        [text_button("👤 Заявки пользователя", "primary", {"action": "admin_user_prompt"})],
        [text_button("🚫 Забаненные", "negative", {"action": "admin_banned_list"})],
    ]
    # ФИКС: кнопка появляется только когда есть необработанные запросы.
    if has_pending_gdpr:
        rows.append([
            text_button("📥 Запросы на удаление ПДн",
                        "negative",
                        {"action": "admin_gdpr_requests"}),
        ])
    rows.append([text_button("📜 История изменений", "primary", {"action": "admin_changelog"})])
    rows.append([text_button("← Выйти из админ-режима", "negative", {"action": "admin_back"})])
    return build_keyboard(rows, one_time=False)


def get_admin_settings_keyboard(admin_id):
    from core.users import get_admin_settings
    settings = get_admin_settings(admin_id)
    notify_label = "🔔 Уведомления: ВКЛ" if settings["notify_applications"] else "🔕 Уведомления: ВЫКЛ"
    receive_label = "📩 Приём сообщений: ВКЛ" if settings["receive_user_messages"] else "📩 Приём сообщений: ВЫКЛ"
    media_label = ("📸 Медиа в карточках: ВКЛ"
                   if settings.get("show_media_in_applications", True)
                   else "🚫 Медиа в карточках: ВЫКЛ")
    rows = [
        [text_button(notify_label, "primary", {"action": "toggle_setting", "setting": "notify_applications"})],
        [text_button(receive_label, "primary", {"action": "toggle_setting", "setting": "receive_user_messages"})],
        [text_button(media_label, "primary", {"action": "toggle_setting", "setting": "show_media_in_applications"})],
        [text_button("← Назад", "secondary", {"action": "admin_back"})],
    ]
    return build_keyboard(rows, one_time=False)


def get_admin_banned_keyboard(banned_entries, page=0, per_page=5):
    """Клавиатура для списка забаненных.

    banned_entries: [{"user_id": int, "platform": str}, ...]
    """
    rows = []
    total = len(banned_entries)
    start = page * per_page
    end = min(start + per_page, total)
    for i in range(start, end):
        e = banned_entries[i]
        uid = e["user_id"]
        pl = e["platform"]
        rows.append([
            text_button(
                f"✅ Разбанить {pl.upper()}:{uid}",
                "positive",
                {"action": "admin_unban_prompt", "user_id": uid, "platform": pl},
            )
        ])
    has_prev = page > 0
    has_next = end < total
    if has_prev or has_next:
        nav_row = []
        if has_prev:
            nav_row.append(text_button("← Стр.", "secondary",
                                       {"action": "admin_banned_page", "page": page - 1}))
        if has_next:
            nav_row.append(text_button("Стр. →", "secondary",
                                       {"action": "admin_banned_page", "page": page + 1}))
        rows.append(nav_row)
    rows.append([text_button("← Назад", "secondary", {"action": "admin_back"})])
    return build_keyboard(rows, one_time=True)


def get_admin_gdpr_requests_keyboard(entries, page=0, per_page=10):
    """entries: [{"id": int, "platform": str, "user_id": int}, ...]

    Inline-кнопки: на каждый запрос пара «🗑 Удалить» / «🧊 Анонимизировать».
    Payload умещается в 64-байтовый лимит callback_data Telegram.
    """
    rows = []
    total = len(entries)
    start = page * per_page
    end = min(start + per_page, total)

    for e in entries[start:end]:
        rid = e["id"]
        rows.append([
            text_button(
                f"🗑 #{rid}",
                "negative",
                {"action": "admin_gdpr_confirm_delete", "request_id": rid},
            ),
            text_button(
                f"🧊 #{rid}",
                "primary",
                {"action": "admin_gdpr_confirm_anon", "request_id": rid},
            ),
        ])

    has_prev = page > 0
    has_next = end < total
    if has_prev or has_next:
        nav_row = []
        if has_prev:
            nav_row.append(text_button(
                "← Стр.", "secondary",
                {"action": "admin_gdpr_page", "page": page - 1}))
        if has_next:
            nav_row.append(text_button(
                "Стр. →", "secondary",
                {"action": "admin_gdpr_page", "page": page + 1}))
        rows.append(nav_row)

    rows.append([text_button("← Назад", "secondary",
                             {"action": "admin_back"})])
    return build_inline_keyboard(rows)