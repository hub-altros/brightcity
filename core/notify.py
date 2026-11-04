# core/notify.py
"""Отправка сообщений и уведомлений админам.

Платформо-агностичный модуль: работает через core.runtime.get_adapter(),
не знает про VK/TG напрямую. Кросс-платформенные сообщения уходят через
outbox_messages (см. core.db.enqueue_outbox).
"""
import logging

from psycopg2 import sql

from core.runtime import platform, get_adapter
from core.db import (
    get_db_connection, return_db_connection,
    enqueue_outbox,
)
from core.utils import safe_str, _run_async, _fire_and_forget

logger = logging.getLogger("bright_city_bot")


# --- Ссылка на диалог ---

def _dialog_link(user_id) -> str:
    pl = platform()
    if pl == "tg":
        return f"tg://user?id={user_id}"
    if pl == "vk":
        return f"https://vk.com/im?sel={user_id}"
    return ""


# --- Имена пользователей ---

def get_user_name(user_id):
    """Возвращает (first_name, last_name) пользователя."""
    try:
        other_pl = detect_user_platform(user_id, exclude_platform=platform())
        if other_pl:
            return f"(ID {user_id} из {other_pl.upper()})", ""
        return _run_async(get_adapter().get_user_name(user_id))
    except Exception as e:
        logger.error("get_user_name wrapper failed (user %s): %s", user_id, e)
        return "друг", ""


def get_admin_name(admin_id):
    first, last = get_user_name(admin_id)
    return f"{first} {last}"


def admin_user_tag(user_id):
    first, last = get_user_name(user_id)
    if last:
        return f"{first} {last} (ID: {user_id})"
    return f"{first} (ID: {user_id})"


# --- Определение платформы получателя ---

def detect_user_platform(target_user_id, exclude_platform=None):
    if not target_user_id:
        return None

    conn = None
    platforms = set()
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT platform FROM bot_user_states WHERE user_id = %s",
                (target_user_id,))
            platforms = {row[0] for row in cur.fetchall()}
            if not platforms:
                cur.execute(
                    "SELECT DISTINCT platform FROM bright_city_applications "
                    "WHERE user_id = %s",
                    (target_user_id,))
                platforms = {row[0] for row in cur.fetchall()}
            if not platforms:
                # Fallback: platform_users (заполняется при входящих)
                try:
                    cur.execute(
                        "SELECT DISTINCT platform FROM platform_users "
                        "WHERE user_id = %s",
                        (target_user_id,))
                    platforms = {row[0] for row in cur.fetchall()}
                except Exception:
                    pass
    except Exception as e:
        logger.error("DB error (detect_user_platform): %s", e)
        return None
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)

    if not platforms:
        return None
    if len(platforms) > 1:
        logger.warning(
            "detect_user_platform: user_id=%s найден на нескольких "
            "платформах: %s — не угадываем",
            target_user_id, platforms)
        return None
    pl = platforms.pop()
    if exclude_platform and pl == exclude_platform:
        return None
    return pl


# --- Список админов для уведомлений ---

def get_admins_for_notify(setting_name):
    if setting_name not in ("notify_applications", "receive_user_messages"):
        logger.error("get_admins_for_notify: invalid setting %r", setting_name)
        return []

    col_sql = sql.SQL("s.notify_applications") if setting_name == "notify_applications" \
        else sql.SQL("s.receive_user_messages")

    result = []
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(sql.SQL("""
                SELECT au.platform, au.admin_id
                FROM admin_users au
                LEFT JOIN admin_settings s
                  ON s.platform = au.platform AND s.admin_id = au.admin_id
                WHERE au.is_active = TRUE
                  AND COALESCE({}, TRUE) = TRUE
            """).format(col_sql))
            result = [(row[0], row[1]) for row in cur.fetchall()]
    except Exception as e:
        logger.error("DB error (get_admins_for_notify): %s", e)
    finally:
        if conn:
            conn.rollback()
            return_db_connection(conn)
    return result


# --- Отправка сообщений ---

def send_message(user_id, text, keyboard=None, max_attempts=2,
                 target_platform=None, parse_mode=None, keep=False,
                 reply_to_platform_message_id=None,
                 bot_message_id=None):
    """Отправляет сообщение пользователю.

    reply_to_platform_message_id — id сообщения в TG/VK/MAX, на которое
    нужно ответить нативно. Работает только при прямой отправке
    (pl == platform()). При cross-platform уходит в outbox, там worker
    сам передаст в adapter.

    bot_message_id — id записи в bot_messages, к которой относится это
    сообщение. Пробрасывается в outbox, чтобы worker после реальной
    отправки сохранил platform_message_id (для будущего edit).

    keep=True — запретить cleanup предыдущих сообщений в адаптере
    (актуально для TG). VK/MAX игнорируют.
    """
    try:
        pl = target_platform or platform()

        # --- Cross-platform: через outbox ---
        if pl != platform():
            from core.users import is_banned_checked

            banned, check_failed = is_banned_checked(
                user_id, target_platform=pl)

            if banned and not check_failed:
                logger.info(
                    "send_message: %s/%s banned — cross-platform send skipped",
                    pl, user_id)
                return False
            if check_failed:
                logger.warning(
                    "send_message: is_banned check failed for %s/%s — "
                    "queuing anyway", pl, user_id)

            ok = enqueue_outbox(
                target_platform=pl,
                target_user_id=user_id,
                text=safe_str(text),
                keyboard=keyboard,
                reply_to_platform_message_id=reply_to_platform_message_id,
                bot_message_id=bot_message_id,
            )
            if not ok:
                logger.error(
                    "send_message: enqueue_outbox failed (%s → %s)",
                    platform(), pl)
            return bool(ok)

        # --- Same-platform: прямой вызов adapter'а ---
        result = _run_async(
            get_adapter().send_message(
                user_id, safe_str(text), keyboard=keyboard,
                max_attempts=max_attempts, parse_mode=parse_mode,
                keep=keep,
                reply_to_platform_message_id=reply_to_platform_message_id,
            )
        )

        # Адаптеры возвращают:
        #   TG: True/False
        #   VK: str(platform_message_id) | None
        #   MAX: str("sent"/id) | None
        #   WebBridge: True/False
        if result and bot_message_id:
            if isinstance(result, str) and result != "sent":
                try:
                    from core.db import mark_bot_message_platform_id
                    mark_bot_message_platform_id(bot_message_id, result)
                except Exception as e:
                    logger.debug(
                        "send_message: mark_bot_message_platform_id "
                        "failed (bot_msg_id=%s, platform_msg_id=%s): %s",
                        bot_message_id, result, e)

        return bool(result)

    except Exception as e:
        logger.error(
            "send_message wrapper failed (user %s): %s", user_id, e)
        return False


def notify_admin_applications(text, keyboard=None):
    any_sent = False
    my_platform = platform()
    for target_pl, admin_id in get_admins_for_notify("notify_applications"):
        try:
            if target_pl == my_platform:
                _fire_and_forget(
                    get_adapter().send_message(
                        admin_id, safe_str(text),
                        keyboard=keyboard, max_attempts=2),
                    label=f"notify_app admin={admin_id}",
                )
                any_sent = True
            else:
                if send_message(admin_id, text, keyboard=keyboard,
                                target_platform=target_pl):
                    any_sent = True
        except Exception as e:
            logger.error("Failed to notify admin %s/%s (applications): %s",
                         target_pl, admin_id, e)
    return any_sent


def notify_admin_user_messages(text):
    any_sent = False
    my_platform = platform()
    for target_pl, admin_id in get_admins_for_notify("receive_user_messages"):
        try:
            if target_pl == my_platform:
                _fire_and_forget(
                    get_adapter().send_message(
                        admin_id, safe_str(text),
                        keyboard=None, max_attempts=2),
                    label=f"notify_admin_user_msg admin={admin_id}",
                )
                any_sent = True
            else:
                if send_message(admin_id, text, target_platform=target_pl):
                    any_sent = True
        except Exception as e:
            logger.error("Failed to notify admin %s/%s (user_messages): %s",
                         target_pl, admin_id, e)
    return any_sent


def notify_admin_urls_as_photos(urls, label: str = "Фото"):
    if not urls:
        return
    my_platform = platform()
    for target_pl, admin_id in get_admins_for_notify("notify_applications"):
        for idx, url in enumerate(urls, 1):
            if not url:
                continue
            try:
                if target_pl == my_platform:
                    _fire_and_forget(
                        get_adapter().send_message(
                            admin_id, safe_str(url),
                            keyboard=None, max_attempts=2),
                        label=f"notify_{label}_admin={admin_id}_idx={idx}",
                    )
                else:
                    if not enqueue_outbox(
                        target_platform=target_pl,
                        target_user_id=admin_id,
                        text=safe_str(url),
                    ):
                        logger.error(
                            "notify_admin_urls_as_photos: enqueue failed "
                            "(%s/%s idx=%d)", target_pl, admin_id, idx)
            except Exception as e:
                logger.error(
                    "notify_admin_urls_as_photos: %s/%s idx=%d failed: %s",
                    target_pl, admin_id, idx, e)


def forward_to_admin(user_id, text, state, platform_message_id=None,
                     reply_to_platform_message_id=None):
    """Пересылает сообщение пользователя всем админам, кто подписан на
    приём сообщений. Сохраняет сообщение в bot_messages (in).

    Если platform_message_id / reply_to_platform_message_id не заданы
    явно, читаются из thread-local контекста (см. core.runtime).
    """
    from core.texts import MAX_TEXT_LENGTH, STEP_LABELS
    from core.storage import save_message_to_db
    from core.runtime import get_event_context

    ctx_pmi, ctx_rpm = get_event_context()
    if platform_message_id is None:
        platform_message_id = ctx_pmi
    if reply_to_platform_message_id is None:
        reply_to_platform_message_id = ctx_rpm

    if text and len(text) > MAX_TEXT_LENGTH:
        text = text[:MAX_TEXT_LENGTH]

    save_message_to_db(
        user_id, "in", text,
        platform_message_id=platform_message_id,
        reply_to_platform_message_id=reply_to_platform_message_id,
    )

    step_label = STEP_LABELS.get(state, "Неизвестный шаг")
    admin_msg = (f"💬 Сообщение от {admin_user_tag(user_id)}\n"
                 f"📍 Шаг: {step_label}\n\n"
                 f"{safe_str(text)}\n\n🔗 Диалог: {_dialog_link(user_id)}\n\n"
                 f"Чтобы ответить, отправьте: /ответ {user_id} ваш текст ответа")
    notify_admin_user_messages(admin_msg)