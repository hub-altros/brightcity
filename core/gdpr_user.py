# core/gdpr_user.py
"""GDPR-запрос от обычного пользователя (не админа).

Пользователь пишет «удали мои данные» или /удалить_мои_данные —
мы регистрируем запрос в БД и уведомляем админов, чтобы они
выполнили удаление вручную (по 152-ФЗ).
"""
import logging

from core.db import log_gdpr_request, has_pending_gdpr_request
from core.notify import (
    send_message, notify_admin_applications, admin_user_tag,
)
from core.runtime import platform
from core.storage import save_message_to_db

logger = logging.getLogger("bright_city_bot")


def handle_user_delete_request(user_id):
    pl = platform()
    # Дедупликация: без неё цикл фразы плодил дубли в gdpr_requests
    # и шторм уведомлений всем админам
    if has_pending_gdpr_request(pl, user_id):
        send_message(user_id, "Ваш запрос уже принят и находится в обработке.")
        return
    save_message_to_db(user_id, "in", "/удалить_мои_данные")
    log_gdpr_request(pl, user_id)
    admin_msg = (
        f"🗑 Запрос на удаление ПДн\n\n"
        f"Пользователь: {admin_user_tag(user_id)}\n"
        f"Платформа: {pl}\nUser ID: {user_id}\n\n"
        f"Удалить: /удалить_данные {pl} {user_id}\n"
        f"Анонимизировать: /удалить_данные {pl} {user_id} anon\n\n"
        f"Уведомление пользователю будет отправлено автоматически (152-ФЗ)."
    )
    notify_admin_applications(admin_msg)
    send_message(user_id,
        "Ваш запрос на удаление персональных данных передан администратору.\n\n"
        "Удаление будет выполнено в ближайшее время, о результате вас уведомят.")