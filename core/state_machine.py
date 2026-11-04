# core/state_machine.py
"""Обработка входящих событий: команды, payload, state machine.

Это финальная точка, куда стекаются все события от адаптера (VK, TG).
Отсюда идёт вызов:
  - core.registration.*   — для шагов регистрации
  - core.admin.*          — для админ-команд и payload админа
  - core.notify.send_message — для ответов пользователю
  - core.users.*          — для проверок (is_admin_user, is_banned, лимиты)
  - core.storage.*        — UserState, parse_payload

Платформо-агностичен. Один единственный публичный вход:
handle_event_sync(user_id, text, attachments, raw_payload).
"""
import hashlib
import logging
import threading
import time

from core.runtime import platform
from core.notify import (
    send_message,
    notify_admin_applications,
    forward_to_admin,
    admin_user_tag,
    get_user_name,
)
from core.users import (
    is_admin_user, is_banned, check_message_rate, auto_ban_user,
    check_step_message_limit,
)
from core.storage import user_state, parse_payload, save_message_to_db
from core.keyboards import (
    get_main_keyboard, get_admin_menu_keyboard,
    get_confirm_keyboard, get_admin_cancel_banner_keyboard,
    get_admin_cancel_reply_keyboard, get_placement_location_keyboard,
    get_user_type_keyboard, get_phone_keyboard, get_phone_prompt_text,
    get_org_info_keyboard, get_rejection_keyboard,
    get_after_submit_inline_keyboard,
    get_extras_keyboard,
)
from core.texts import (
    MAX_TEXT_LENGTH, STEP_LABELS,
    STATE_START, STATE_LOCATION, STATE_PLACEMENT_LOCATION, STATE_USER_TYPE,
    STATE_ORG_PHONE, STATE_ORG_INFO, STATE_NAME_AGE, STATE_CHILD_PHOTO,
    STATE_FORMAT, STATE_DRAWINGS, STATE_TRAINING, STATE_PAYMENT,
    STATE_CONFIRM, STATE_EXTRAS, STATE_NEXT_ACTION, STATE_FINAL,
    STATE_QUESTION, STATE_ADMIN, STATE_ADMIN_SEND_BANNER,
    STATE_ADMIN_REPLY, STATE_ADMIN_REJECT_REASON,
    PLACEMENT_LOCATIONS, PLACEMENT_LOCATIONS_SHORT, PLACEMENT_PAGE_SIZE,
    USER_TYPES,
    looks_like_gdpr_request,
)
from core.admin import (
    # меню
    show_admin_menu,
    handle_admin_application_detail,
    handle_admin_user_apps,
    handle_admin_stats,
    handle_admin_set_status,
    handle_admin_search,
    handle_admin_changelog,
    handle_admin_add_cmd,
    handle_admin_remove_cmd,
    handle_admin_list_cmd,
    handle_admin_ban,
    handle_admin_unban,
    handle_admin_delete_data,
    handle_admin_gdpr_log,
    handle_admin_gdpr_request_cmd,
    handle_admin_user_apps_text,
    handle_admin_search_text,
    # НОВОЕ: статус субъекта (delete/anon) из gdpr_audit_log
    handle_admin_gdpr_status,
    # sudo
    execute_sudo_action, cancel_sudo_request,
    handle_admin_set_status_button,
    # reply
    handle_admin_reply, handle_admin_history,
    start_admin_reply,
    # back
    handle_admin_back,
    # banner
    start_admin_send_banner,
    # payload view
    handle_admin_incomplete_detail,
    send_reminder_to_user,
    get_admin_view,
    set_admin_view,
    process_admin_reply,
    process_admin_reject_reason,
    process_admin_banner_photo,
    # page nav
    handle_admin_page_navigation,
    # show category
    show_admin_categories, show_admin_category_list,
    show_admin_incomplete_list, show_admin_settings,
    toggle_admin_setting,
    show_banned_list,
    handle_admin_banned_unban_prompt,
    # ФИКС п.7: сверка владельца заявки с адресатом ответа
    _check_app_owner,
    # НОВОЕ: единая точка выкидывания не-админа из админ-состояния
    kick_out_non_admin,
    show_admin_gdpr_requests,
    handle_admin_gdpr_request_action,
)
from core.registration import (
    handle_start, start_registration,
    handle_off_script, resend_step_prompt,
    process_location, process_placement_location,
    process_org_phone, process_org_info,
    process_org_info_confirm, process_org_info_skip,
    process_name_age,
    process_child_photo, process_child_photo_confirm,
    process_format,
    process_drawings, process_drawings_confirm,
    process_training,
    process_payment,
    process_confirm,
    start_question_mode, process_question,
    send_extras_message, skip_extras,
    send_final_message,
)

from core.utils import as_positive_int

logger = logging.getLogger("bright_city_bot")

# Админ-состояния. Раньше константа была объявлена, но НЕ использовалась:
# проверка `is_admin_user` жила только внутри process_admin_* в core.admin,
# а на ветке «не админ» вызывался show_admin_menu — то есть демотированный
# админ оставался в админ-состоянии и продолжал видеть админ-меню.
# Теперь _ADMIN_STATES используется как единый choke-point в
# _handle_state_processing: любое событие от не-админа в одном из этих
# состояний приводит к kick_out_non_admin (сброс в STATE_START +
# пользовательское меню). Дополнительные проверки в process_admin_*
# остаются как defense-in-depth для прямых вызовов.
_ADMIN_STATES = (STATE_ADMIN, STATE_ADMIN_SEND_BANNER,
                 STATE_ADMIN_REPLY, STATE_ADMIN_REJECT_REASON)

# ФИКС: набор навигационных action'ов. Их нажатие НЕ должно расходовать
# лимит сообщений на шаге — иначе пользователь, листающий страницы
# локаций, исчерпает лимит, даже не отправив ни одного текста.
_NAVIGATION_ACTIONS = frozenset({
    "placement_page", "admin_page", "admin_back",
    "admin_categories", "show_category", "admin_settings",
    "toggle_setting", "admin_stats", "admin_search_prompt",
    "admin_user_prompt", "admin_changelog", "back_to_main",
    "sudo_ok", "sudo_no",
    "sudo_confirm", "sudo_cancel",
    "accept_consent",
    "admin_banned_list", "admin_banned_page", "admin_unban_prompt",
    "admin_gdpr_requests",
    "admin_gdpr_page",
    "admin_gdpr_confirm_delete",
    "admin_gdpr_confirm_anon",
})


# ============================================================
#  ADMIN PAYLOAD ACTIONS (пункт 2)
# ============================================================
# Action'ы из callback_data / reply-payload, которые ТРЕБУЮТ прав
# администратора. Проверяются ОДНИМ guard'ом в начале _handle_payload,
# а не is_admin-обёртками в каждой ветке.
#
# ВАЖНО: любое новое действие, доступное только админу, ОБЯЗАНО быть
# добавлено сюда. Иначе оно станет публичным.
#
# НЕ путать с _NAVIGATION_ACTIONS: тот список про то, что не считается
# в per-step лимит сообщений, а этот — про авторизацию. Множества
# пересекаются (например, admin_back), но это разные задачи.
_ADMIN_PAYLOAD_ACTIONS = frozenset({
    # sudo
    "sudo_ok", "sudo_no",
    "sudo_confirm", "sudo_cancel",
    # заявки
    "set_status", "view_app", "view_incomplete",
    # напоминания и ответы по незавершённой
    "admin_remind", "admin_start_reply_incomplete",
    # меню и навигация админки
    "admin_categories", "show_category",
    "admin_settings", "toggle_setting",
    "admin_stats", "admin_changelog",
    "admin_search_prompt", "admin_user_prompt",
    "admin_back", "admin_page",
    # бан-лист
    "admin_banned_list", "admin_banned_page", "admin_unban_prompt",
    # макет и ответ по заявке
    "admin_send_banner", "admin_start_reply",
    "admin_gdpr_requests",
    "admin_gdpr_page",
    "admin_gdpr_confirm_delete",
    "admin_gdpr_confirm_anon",
})


# ============================================================
#  БЛОКИРОВКИ ПОЛЬЗОВАТЕЛЯ
# ============================================================

_event_user_locks = {}
_event_user_locks_guard = threading.Lock()
_EVENT_USER_LOCKS_MAX = 5000


def _get_event_user_lock(user_id):
    key = (platform(), user_id)
    with _event_user_locks_guard:
        if len(_event_user_locks) > _EVENT_USER_LOCKS_MAX:
            for k in list(_event_user_locks.keys())[:1000]:
                if not _event_user_locks[k].locked():
                    _event_user_locks.pop(k, None)
        lock = _event_user_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _event_user_locks[key] = lock
        return lock


# ФИКС Баг-35: раньше при превышении лимита событий (60/мин)
# пользователь получал полное молчание — событие возвращалось из
# handle_event_sync без ответа, и пользователь не понимал, что
# произошло. Теперь шлём одно уведомление, но не чаще раза в 30 сек,
# чтобы сотня подряд заблокированных событий не превратилась в спам.
_ev_rate_warn_last = {}
_ev_rate_warn_lock = threading.Lock()


def _maybe_warn_event_rate(user_id):
    """Сообщает пользователю о срабатывании event-rate лимита.

    Троттлинг: не чаще раза в 30 сек на пользователя. Чистит dict
    при переполнении (записи старше 10 минут удаляются).
    """
    now = time.time()
    with _ev_rate_warn_lock:
        last = _ev_rate_warn_last.get(user_id, 0.0)
        if now - last < 30:
            return
        _ev_rate_warn_last[user_id] = now
        # Чистим при переполнении — иначе dict растёт на потоке
        # уникальных пользователей, как _event_rate_tracker в users.py.
        if len(_ev_rate_warn_last) > 10000:
            cutoff = now - 600
            stale = [k for k, t in _ev_rate_warn_last.items() if t < cutoff]
            for k in stale:
                _ev_rate_warn_last.pop(k, None)
    try:
        send_message(user_id,
            "⏳ Слишком много действий за минуту.\n\n"
            "Подождите, пожалуйста, немного и повторите.")
    except Exception as e:
        logger.debug("_maybe_warn_event_rate: send_message failed: %s", e)


# ============================================================
#  ГЛАВНЫЙ ВХОД
# ============================================================

def handle_event_sync(user_id, text, attachments, raw_payload,
                      platform_message_id=None,
                      reply_to_platform_message_id=None):
    """Синхронная обработка одного события.

    platform_message_id / reply_to_platform_message_id сохраняются в
    thread-local контекст, чтобы вложенные save_message_to_db /
    forward_to_admin могли их прочитать без явной передачи через все
    сигнатуры.
    """
    from core.runtime import set_event_context, clear_event_context
    set_event_context(platform_message_id, reply_to_platform_message_id)

    t_start = time.time()
    try:
        # ПДн (ФИО, телефон, адрес) не пишем в логи: только длина и хеш
        logger.info("handle_event_sync START: user=%s platform=%s text_len=%d text_sha=%s",
                    user_id, platform(), len(text or ""),
                    hashlib.sha256((text or "").encode()).hexdigest()[:12])

        # === is_admin_user ===
        t0 = time.time()
        is_admin = is_admin_user(user_id)
        dt = time.time() - t0
        if dt > 1.0:
            logger.warning("handle_event_sync: is_admin_user took %.2fs (user=%s)",
                           dt, user_id)

        # === is_banned ===
        if not is_admin:
            t0 = time.time()
            banned = is_banned(user_id)
            dt = time.time() - t0
            if dt > 1.0:
                logger.warning("handle_event_sync: is_banned took %.2fs (user=%s)",
                               dt, user_id)
            if banned:
                logger.info("handle_event_sync: user %s banned — exit", user_id)
                return

        # === check_message_rate ===
        # Лимит на ЛЮБЫЕ события, включая payload и вложения: иначе
        # события с payload не считались вообще и автобан не срабатывал
        if not is_admin:
            from core.users import check_event_rate
            allowed_ev, _ = check_event_rate(user_id)
            if not allowed_ev:
                logger.warning("event rate limited user=%s", user_id)
                # ФИКС Баг-35: сообщаем пользователю (троттлинг 30 сек).
                _maybe_warn_event_rate(user_id)
                return

        _is_user_message = (
            isinstance(text, str) and len(text.strip()) > 0
            and not (raw_payload is not None)
        )
        if not is_admin and _is_user_message:
            t0 = time.time()
            allowed, ban_flag = check_message_rate(user_id)
            dt = time.time() - t0
            if dt > 1.0:
                logger.warning("handle_event_sync: check_message_rate took %.2fs (user=%s)",
                               dt, user_id)
            if not allowed:
                logger.warning(
                    "handle_event_sync: rate limited user=%s flag=%s — exit",
                    user_id, ban_flag)
                if ban_flag == 0:
                    try:
                        send_message(
                            user_id,
                            "⏳ Слишком много сообщений за минуту. "
                            "Подождите, пожалуйста, немного и повторите.",
                            target_platform=platform(),
                        )
                    except Exception:
                        pass
                if ban_flag == 1:
                    ban_duration = auto_ban_user(user_id)
                    notify_admin_applications(
                        f"🚫 Автобан: пользователь {user_id} заблокирован на "
                        f"{ban_duration or ''} (превышение лимита сообщений)"
                    )
                return

        # === user lock ===
        t0 = time.time()
        lock = _get_event_user_lock(user_id)
        waited = time.time() - t0
        if waited > 0.5:
            logger.warning("handle_event_sync: waited for lock %.2fs (user=%s)",
                           waited, user_id)

        with lock:
            logger.info("handle_event_sync: lock acquired, calling state machine (user=%s)",
                        user_id)
            t0 = time.time()
            try:
                result = _handle_state_processing(
                    user_id, text, attachments, raw_payload, is_admin,
                    platform_message_id=platform_message_id,
                    reply_to_platform_message_id=reply_to_platform_message_id)
                dt = time.time() - t0
                if dt > 2.0:
                    logger.warning("handle_event_sync: state machine took %.2fs (user=%s)",
                                   dt, user_id)
                return result
            finally:
                logger.info("handle_event_sync: done in %.2fs (user=%s)",
                            time.time() - t_start, user_id)
    except Exception as e:
        logger.exception("handle_event_sync: Ошибка при обработке события: %s", e)
    finally:
        clear_event_context()

# ============================================================
#  STATE MACHINE
# ============================================================

def _handle_state_processing(user_id, text, attachments, raw_payload,
                             is_admin, platform_message_id=None,
                             reply_to_platform_message_id=None):
    try:
        # ФИКС: пользователь возвращается после GDPR-удаления.
        #
        # Раньше gdpr_erasure_log блокировал UserState._save НАВСЕГДА:
        # start_registration не сохранял STATE_LOCATION, следующий клик
        # по «Краснодар» читал из БД STATE_START и игнорировался —
        # пользователь застревал в цикле «клик → ничего».
        #
        # Здесь: при первом событии от стёртого пользователя снимаем
        # маркер, чистим возможный устаревший кэш состояния, шлём
        # главное меню и выходим. Следующее событие пойдёт обычным
        # путём — state будет сохраняться, бот заработает.
        from core.db import is_user_erased, clear_gdpr_erasure_log
        if is_user_erased(platform(), user_id):
            logger.info(
                "reactivating GDPR-erased user %s/%s", platform(), user_id)
            clear_gdpr_erasure_log(platform(), user_id)
            # На случай, если в этом процессе остался устаревший
            # кэш состояния (cache_listener не доставил notify).
            user_state.clear(user_id)
            send_message(user_id,
                "ℹ️ Ваши предыдущие данные были удалены по вашему "
                "запросу (152-ФЗ). Начинаем заново.",
                keyboard=get_main_keyboard())
            return

        # --- /start ---
        if text == "/start":
            handle_start(user_id)
            return

        # --- GDPR-запрос от пользователя вручную ---
        if not is_admin and text == "/удалить_мои_данные":
            from core.gdpr_user import handle_user_delete_request
            handle_user_delete_request(user_id)
            return

        # --- неизвестная команда от не-админа ---
        if not is_admin and text.startswith("/"):
            send_message(user_id,
                "ℹ️ Я не понимаю эту команду. Используйте кнопки ниже "
                "или напишите вопрос текстом.",
                keyboard=get_main_keyboard())
            return

        # --- GDPR-запрос по фразе ---
        if not is_admin and looks_like_gdpr_request(text):
            from core.gdpr_user import handle_user_delete_request
            handle_user_delete_request(user_id)
            return

        # --- админ-команды ---
        if is_admin and text == "/админ":
            show_admin_menu(user_id)
            return
        elif is_admin and (text.startswith("/ответ ") or text == "/ответ"):
            user_state.set(user_id, STATE_ADMIN)
            parts = text.split(maxsplit=2)
            if len(parts) >= 3:
                # ФИКС Баг-59: поддержка префикса <platform>:<user_id>
                # наряду с обычным <user_id>.
                from core.admin import _parse_reply_target
                target_uid, explicit_pl = _parse_reply_target(parts[1])
                if target_uid is None:
                    handle_admin_reply(user_id, text)
                else:
                    reply_text = parts[2][:MAX_TEXT_LENGTH]
                    view, ctx = get_admin_view(user_id)
                    app_id = ctx.get("app_id") if view == "app_detail" else None

                    ok, reason = _check_app_owner(app_id, target_uid)
                    if not ok:
                        send_message(user_id,
                                     f"⚠ Нельзя ответить: {reason}.\n\n"
                                     f"Проверьте user_id. Открыта заявка: #{app_id}.")
                        return

                    if app_id:
                        rv_from_view = ctx.get("from_view", "categories")
                        rv_from_ctx = ctx.get("from_ctx", {})
                    else:
                        rv_from_view = "menu"
                        rv_from_ctx = {}
                    user_state.set(user_id, STATE_ADMIN_REPLY)
                    user_state.update_temp(user_id, {
                        "admin_reply_app_id": app_id,
                        "admin_reply_user_id": target_uid,
                        "admin_reply_from_view": rv_from_view,
                        "admin_reply_from_ctx": rv_from_ctx,
                    })
                    if app_id:
                        set_admin_view(user_id, "reply", app_id=app_id, user_id=target_uid,
                                       from_view=rv_from_view, from_ctx=rv_from_ctx)
                    else:
                        set_admin_view(user_id, "reply", app_id=None, user_id=target_uid,
                                       from_view=rv_from_view, from_ctx=rv_from_ctx)
                    process_admin_reply(
                        user_id, reply_text,
                        app_id=app_id,
                        target_platform=explicit_pl,
                    )
            else:
                handle_admin_reply(user_id, text)
            return
        elif is_admin and (text.startswith("/история ") or text == "/история"):
            user_state.set(user_id, STATE_ADMIN)
            handle_admin_history(user_id, text)
            return
        elif is_admin and text.startswith("/заявка_"):
            user_state.set(user_id, STATE_ADMIN)
            try:
                app_id = int(text.split("_")[1].strip())
                set_admin_view(user_id, "app_detail", app_id=app_id,
                               from_view="categories", from_ctx={})
                handle_admin_application_detail(user_id, app_id)
            except (ValueError, IndexError):
                send_message(user_id, "Формат: /заявка_<номер>")
            return
        elif is_admin and (text.startswith("/пользователь ") or text == "/пользователь"):
            user_state.set(user_id, STATE_ADMIN)
            handle_admin_user_apps(user_id, text)
            return
        elif is_admin and text.startswith("/статистика"):
            user_state.set(user_id, STATE_ADMIN)
            handle_admin_stats(user_id)
            return
        elif is_admin and (text.startswith("/статус ") or text == "/статус"):
            user_state.set(user_id, STATE_ADMIN)
            handle_admin_set_status(user_id, text)
            return
        elif is_admin and (text.startswith("/поиск ") or text == "/поиск"):
            user_state.set(user_id, STATE_ADMIN)
            handle_admin_search(user_id, text)
            return
        elif is_admin and (text == "/changelog" or text.startswith("/changelog ")):
            user_state.set(user_id, STATE_ADMIN)
            handle_admin_changelog(user_id, text)
            return
        elif is_admin and (text == "/changelog_full" or text.startswith("/changelog_full ")):
            user_state.set(user_id, STATE_ADMIN)
            handle_admin_changelog(user_id, text)
            return
        elif is_admin and (text.startswith("/добавить_админа") or text == "/добавить_админа"):
            user_state.set(user_id, STATE_ADMIN)
            handle_admin_add_cmd(user_id, text)
            return
        elif is_admin and (text.startswith("/удалить_админа") or text == "/удалить_админа"):
            user_state.set(user_id, STATE_ADMIN)
            handle_admin_remove_cmd(user_id, text)
            return
        elif is_admin and text in ("/админы", "/список_админов"):
            user_state.set(user_id, STATE_ADMIN)
            handle_admin_list_cmd(user_id, text)
            return
        elif is_admin and (text.startswith("/бан ") or text == "/бан"):
            user_state.set(user_id, STATE_ADMIN)
            handle_admin_ban(user_id, text)
            return
        elif is_admin and (text.startswith("/разбан ") or text == "/разбан"):
            user_state.set(user_id, STATE_ADMIN)
            handle_admin_unban(user_id, text)
            return
        elif is_admin and (text.startswith("/удалить_данные") or text == "/удалить_данные"):
            user_state.set(user_id, STATE_ADMIN)
            handle_admin_delete_data(user_id, text)
            return
        elif is_admin and (text.startswith("/gdpr_лог") or text == "/gdpr_лог"):
            user_state.set(user_id, STATE_ADMIN)
            handle_admin_gdpr_log(user_id, text)
            return
        elif is_admin and (text.startswith("/gdpr_запрос") or text == "/gdpr_запрос"):
            user_state.set(user_id, STATE_ADMIN)
            handle_admin_gdpr_request_cmd(user_id, text)
            return
        elif is_admin and (text.startswith("/gdpr_статус") or text == "/gdpr_статус"):
            user_state.set(user_id, STATE_ADMIN)
            handle_admin_gdpr_status(user_id, text)
            return

        # --- разбор payload (если он есть) ДО лимита шага ---
        payload_data = None
        payload_from_fallback = False
        if raw_payload is not None:
            try:
                payload_data = parse_payload(raw_payload)
            except Exception as e:
                logger.error("Payload parse error: %s", e)
                payload_data = None
            logger.info(
                "_handle_state_processing: payload=%s action=%r",
                bool(raw_payload),
                payload_data.get("action") if isinstance(payload_data, dict) else None,
            )

        # --- fallback для reply-кнопок TG после перезапуска ---
        # ФИКС (баг №3): разрешаем метку reply-кнопки ДО проверки лимита
        # шага. Раньше fallback стоял ПОСЛЕ check_step_message_limit, из-за
        # чего нажатие reply-кнопки после перезапуска TG-бота (когда
        # _user_reply_map адаптера пуст и payload не приходит) считалось
        # обычным текстовым сообщением и расходовало лимит шага. Пользователь
        # упирался в «Лимит сообщений на этом шаге исчерпан» на навигационных
        # кнопках («← Назад», «Далее →», «Подтвердить»), хотя никакого
        # осмысленного ввода не делал.
        if payload_data is None and raw_payload is None and text:
            resolved = _resolve_reply_label(user_id, text)
            if resolved is not None:
                logger.info(
                    "reply-label fallback: user=%s text=%r → action=%r",
                    user_id, text[:40], resolved.get("action"))
                payload_data = resolved
                payload_from_fallback = True

        action = payload_data.get("action") if isinstance(payload_data, dict) else None
        is_navigation = action in _NAVIGATION_ACTIONS

        state = user_state.get(user_id)

        # --- НОВЫЙ GUARD: не-админ в админ-состоянии ---
        if state in _ADMIN_STATES and not is_admin:
            kick_out_non_admin(user_id, reason=f"state={state}")
            return

        if not is_admin and not is_navigation:
            allowed_step, warn_step = check_step_message_limit(user_id, state)
            if not allowed_step:
                if warn_step:
                    send_message(user_id,
                                 "⚠ Лимит сообщений на этом шаге исчерпан.\n\n"
                                 "Завершите текущий шаг или напишите позже. "
                                 "Если нужна помощь — напишите «Задать вопрос» "
                                 "или дождитесь ответа администратора.")
                return

        # --- inline/reply payload ---
        if payload_data:
            # Для fallback-ответов text обнуляем — как и в прежнем коде,
            # чтобы state machine не обработала метку кнопки как обычный
            # ввод (команду, офф-скрипт и т.п.).
            effective_text = "" if payload_from_fallback else text
            if handle_payload(user_id, payload_data, effective_text, attachments):
                return

        # --- state-машина ---
        if text and len(text) > MAX_TEXT_LENGTH:
            text = text[:MAX_TEXT_LENGTH]
        state = user_state.get(user_id)
        logger.info(
            "_handle_state_processing: fallthrough to state machine — "
            "user=%s state=%s text=%r att=%d",
            user_id, state, text[:40] if text else "", len(attachments),
        )

        if state == STATE_START:
            # ФИКС: раньше ЛЮБОЙ текст в STATE_START уходил в handle_start —
            # главное меню — и молча терялся. Админ отправлял ответ из
            # карточки заявки, пользователь отвечал, но его сообщение до
            # админа не доходило (и сам пользователь видел только меню).
            # STATE_START — это состояние «свободного чата», как
            # STATE_QUESTION/STATE_NEXT_ACTION: непустой текст пересылаем
            # админам через forward_to_admin, пустой — только меню.
            if text and text.strip():
                forward_to_admin(user_id, text, STATE_START,
                                 platform_message_id=platform_message_id,
                                 reply_to_platform_message_id=reply_to_platform_message_id)
                send_message(user_id,
                    "Ваше сообщение передано администратору. "
                    "Ответ поступит в этот чат.",
                    keyboard=get_main_keyboard())
            else:
                handle_start(user_id)
        elif state == STATE_LOCATION:
            process_location(user_id, text)
        elif state == STATE_PLACEMENT_LOCATION:
            process_placement_location(user_id, text)
        elif state == STATE_USER_TYPE:
            handle_off_script(user_id, text, STATE_USER_TYPE)
        elif state == STATE_ORG_PHONE:
            process_org_phone(user_id, attachments, text)
        elif state == STATE_ORG_INFO:
            process_org_info(user_id, text, attachments)
        elif state == STATE_NAME_AGE:
            process_name_age(user_id, text)
        elif state == STATE_CHILD_PHOTO:
            process_child_photo(user_id, attachments, text)
        elif state == STATE_FORMAT:
            handle_off_script(user_id, text, STATE_FORMAT)
        elif state == STATE_DRAWINGS:
            process_drawings(user_id, attachments, text)
        elif state == STATE_TRAINING:
            process_training(user_id, text)
        elif state == STATE_PAYMENT:
            process_payment(user_id, attachments, text)
        elif state == STATE_CONFIRM:
            # handle_off_script теперь сам вызывает resend_step_prompt,
            # который показывает текст согласия + кнопку «Отправить заявку».
            # Второй send_message не нужен — иначе будет дубль.
            handle_off_script(user_id, text, STATE_CONFIRM)
        elif state == STATE_EXTRAS:
            if text and text.strip():
                # 1. Сохраняем сообщение в историю.
                save_message_to_db(
                    user_id, "in", text,
                    platform_message_id=platform_message_id,
                    reply_to_platform_message_id=reply_to_platform_message_id,
                )

                # 2. Уведомляем админов. forward_to_admin ждёт state
                # (строку), а не attachments (list) — передаём
                # STATE_EXTRAS, чтобы в карточке админа было видно,
                # с какого шага пользователь написал.
                forward_to_admin(user_id, text, STATE_EXTRAS,
                                 platform_message_id=platform_message_id,
                                 reply_to_platform_message_id=reply_to_platform_message_id)

                # 3. Короткий ответ с кнопкой «Пропустить»,
                # без дублирования EXTRAS_TEXT.
                send_message(
                    user_id,
                    "✅ Ваше сообщение передано администратору.\n\n"
                    "Если хотите завершить оформление, нажмите «Пропустить».",
                    keyboard=get_extras_keyboard()
                )
            else:
                # Пустое событие (или кнопка «Пропустить» через payload).
                skip_extras(user_id)
        elif state == STATE_NEXT_ACTION:
            handle_off_script(user_id, text, STATE_NEXT_ACTION)
        elif state == STATE_QUESTION:
            process_question(user_id, text)
        elif state == STATE_FINAL:
        # STATE_FINAL — переходное состояние (send_final_message сразу
        # ставит STATE_START). Если пользователь всё же успел что-то
        # написать — пересылаем, как в STATE_START.
            if text and text.strip():
                forward_to_admin(user_id, text, STATE_FINAL,
                                 platform_message_id=platform_message_id,
                                 reply_to_platform_message_id=reply_to_platform_message_id)
            handle_start(user_id)
        elif state == STATE_ADMIN:
        # ФИКС: раньше во всех view кроме 'menu' сообщение уходило
        # БЕЗ клавиатуры, и админ залипал: reply-кнопки карточки не
        # восстанавливались (см. _resolve_reply_label), а показать
        # меню заново было нечем. Теперь всегда возвращаем админа
        # на валидный экран с клавиатурой.
            view, vctx = get_admin_view(user_id)
            if view == "menu":
                show_admin_menu(user_id)
            elif view in ("app_detail", "send_banner", "reply",
                          "reject_reason"):
                app_id = vctx.get("app_id")
                if app_id:
                    handle_admin_application_detail(user_id, app_id)
                else:
                    show_admin_menu(user_id)
            elif view == "category_list":
                show_admin_category_list(
                    user_id, vctx.get("category", "new"), vctx.get("page", 0))
            elif view == "incomplete_list":
                show_admin_incomplete_list(user_id, vctx.get("page", 0))
            elif view == "incomplete_detail":
                tuid = vctx.get("target_uid")
                if tuid:
                    handle_admin_incomplete_detail(user_id, tuid)
                else:
                    show_admin_menu(user_id)
            elif view == "search_list":
                handle_admin_search_text(
                    user_id, vctx.get("query", ""), vctx.get("page", 0))
            elif view == "user_list":
                tuid = vctx.get("target_uid")
                if tuid is not None:
                    handle_admin_user_apps_text(
                        user_id, tuid, vctx.get("page", 0))
                else:
                    show_admin_menu(user_id)
            elif view == "banned_list":
                show_banned_list(user_id, vctx.get("page", 0))
            elif view == "gdpr_requests":
                show_admin_gdpr_requests(user_id, vctx.get("page", 0))
            else:
                show_admin_menu(user_id)
        elif state == STATE_ADMIN_SEND_BANNER:
            if attachments:
                process_admin_banner_photo(user_id, attachments, text)
            elif text and text.strip():
                send_message(user_id, "Нужно прикрепить фото макета, а не текст.")
            else:
                send_message(user_id, "Загрузите фото макета или нажмите «Назад».",
                             keyboard=get_admin_cancel_banner_keyboard())
        elif state == STATE_ADMIN_REPLY:
            if text and text.strip():
                process_admin_reply(user_id, text.strip())
            else:
                send_message(user_id, "Напишите текст сообщения для пользователя.\n"
                                      "Для отмены нажмите кнопку ниже.",
                             keyboard=get_admin_cancel_reply_keyboard())
        elif state == STATE_ADMIN_REJECT_REASON:
            if text and text.strip():
                process_admin_reject_reason(user_id, text.strip())
            else:
                send_message(user_id, "Напишите текст причины отклонения.\n"
                                      "Для отмены нажмите кнопку ниже.",
                             keyboard=get_admin_cancel_reply_keyboard())
        else:
            # Неизвестное состояние — сбрасываем в START, но текст
            # не теряем.
            if text and text.strip():
                forward_to_admin(user_id, text, "unknown",
                                 platform_message_id=platform_message_id,
                                 reply_to_platform_message_id=reply_to_platform_message_id)
            handle_start(user_id)
    except Exception as e:
        logger.error("Ошибка при обработке события: %s", e)


# ============================================================
#  FALLBACK ДЛЯ REPLY-КНОПОК TG
# ============================================================

def _resolve_reply_label(user_id, text):
    """Fallback для reply-кнопок TG после перезапуска бота.

    В TG reply-клавиатуры живут на стороне клиента, payload не приходит —
    только текст. Адаптер хранит label→payload в _user_reply_map, но
    после перезапуска карта пуста. Восстанавливаем payload по метке
    и текущему state пользователя.
    """
    if not text:
        return None
    t = text.strip()
    state = user_state.get(user_id)

    # --- Универсальные кнопки главного меню ---
    if t == "ОФОРМИТЬ ЗАЯВКУ НА УЧАСТИЕ":
        return {"action": "start_registration"}
    if t == "Задать вопрос":
        return {"action": "ask_question"}

    # --- Выбор города ---
    if state == STATE_LOCATION and t == "Краснодар":
        return {"action": "select_city", "value": "Краснодар"}

    # --- Выбор локации размещения ---
    if state == STATE_PLACEMENT_LOCATION:
        for i, full in enumerate(PLACEMENT_LOCATIONS):
            short = PLACEMENT_LOCATIONS_SHORT[i]
            if t == short[:40] or t == full:
                return {"action": "select_placement", "value": full}
        if t == "← Назад":
            page = user_state.get_temp(user_id).get("placement_page", 0)
            return {"action": "placement_page", "page": max(0, page - 1)}
        if t == "Далее →":
            page = user_state.get_temp(user_id).get("placement_page", 0)
            return {"action": "placement_page", "page": page + 1}

    # --- Тип пользователя ---
    if state == STATE_USER_TYPE:
        user_types_map = {
            "Я родитель": "Я родитель",
            "Я представитель организации": "Я представитель организации (школа, детский центр и т.д.)",
            "Я педагог (без организации)": "Я педагог (без организации)",
        }
        if t in user_types_map:
            return {"action": "select_user_type", "value": user_types_map[t]}

    # --- Информация о себе ---
    if state == STATE_ORG_INFO:
        if t == "Подтвердить":
            return {"action": "confirm_org_info"}
        if t == "Использовать предыдущие данные":
            return {"action": "skip_org_info"}

    # --- Фото ребёнка ---
    if state == STATE_CHILD_PHOTO:
        if t.startswith("Подтвердить фото") or t == "Подтвердить":
            return {"action": "confirm_child_photo"}

    # --- Формат рисунка ---
    if state == STATE_FORMAT:
        format_map = {
            "Горизонтальный или квадратный": "Горизонтальный или квадратный",
            "1 вертикальный": "1 вертикальный рисунок",
            "2 вертикальных": "2 вертикальных рисунка",
        }
        if t in format_map:
            return {"action": "select_format", "value": format_map[t]}

    # --- Рисунки ---
    if state == STATE_DRAWINGS:
        if t.startswith("Подтвердить рисунки") or t == "Подтвердить":
            return {"action": "confirm_drawings"}

    # --- Подтверждение заявки ---
    if state == STATE_CONFIRM and t == "Отправить заявку":
        return {"action": "submit_application"}

    # --- Следующее действие ---
    if state == STATE_NEXT_ACTION:
        if t == "Оформить следующую заявку":
            return {"action": "next_application"}
        if t == "Завершить":
            return {"action": "finish"}

    # --- Дополнительные возможности ---
    if state == STATE_EXTRAS and t == "Пропустить":
        return {"action": "skip_extras"}

    # --- Режим вопросов ---
    if state == STATE_QUESTION and t == "Вернуться к оформлению заявки":
        return {"action": "back_to_registration"}

    # --- Админские reply-кнопки ---
    # ФИКС: раньше fallback не знал про админ-метки. После перезапуска
    # TG-бота (или при потере _user_reply_map) любое нажатие админской
    # reply-кнопки приходило как текст → в STATE_ADMIN fallback без
    # клавиатуры → админ залипал на «Используйте кнопки или команды»
    # и не мог вернуться, пока вручную не наберёт /админ.
    if state in _ADMIN_STATES:
        _, actx = get_admin_view(user_id)
        app_id = actx.get("app_id")

        if t == "← Назад":
            return {"action": "admin_back"}
        if t == "📋 Все заявки":
            return {"action": "admin_categories"}
        if t == "📊 Статистика":
            return {"action": "admin_stats"}
        if t == "⚙️ Настройки":
            return {"action": "admin_settings"}
        if t == "🔍 Поиск по ФИО":
            return {"action": "admin_search_prompt"}
        if t == "👤 Заявки пользователя":
            return {"action": "admin_user_prompt"}
        if t == "🚫 Забаненные":
            return {"action": "admin_banned_list"}
        if t == "📥 Запросы на удаление ПДн":
            return {"action": "admin_gdpr_requests"}
        if t == "📜 История изменений":
            return {"action": "admin_changelog"}
        if t == "← Выйти из админ-режима":
            return {"action": "admin_back"}
        if app_id:
            # Кнопки карточки заявки (view='app_detail').
            if t == "🔄 В обработке":
                return {"action": "set_status", "app_id": app_id,
                        "status": "in_progress"}
            if t == "❌ Отклонена":
                return {"action": "set_status", "app_id": app_id,
                        "status": "rejected"}
            if t == "📤 Отправить макет":
                return {"action": "admin_send_banner", "app_id": app_id}
            if t == "💬 Написать пользователю":
                return {"action": "admin_start_reply", "app_id": app_id}

    return None


# ============================================================
#  PAYLOAD (INLINE/REPLY-КНОПКИ)
# ============================================================

def handle_payload(user_id, payload_data, text, attachments):
    """Обёртка: защищает от неожиданных типов внутри payload
    (раньше исключение теряло событие целиком)."""
    if not isinstance(payload_data, dict):
        logger.warning("handle_payload: payload_data не dict: %r",
                       type(payload_data).__name__)
        return False
    try:
        return _handle_payload(user_id, payload_data, text, attachments)
    except (AttributeError, TypeError, ValueError, KeyError) as e:
        logger.warning("handle_payload: некорректный payload %r: %s",
                       payload_data, e)
        return True


def _handle_payload(user_id, payload_data, text, attachments):
    # Импорты ОБЯЗАНЫ быть в начале функции: as_positive_int вызывается
    # ниже (set_status, admin_send_banner, admin_start_reply), а импорт
    # в середине тела сделал бы имя локальным и дал UnboundLocalError.
    from core.texts import PLACEMENT_LOCATIONS, FORMAT_LABELS

    action = payload_data.get("action")
    logger.info(
        "handle_payload: user=%s action=%r state=%s",
        user_id, action, user_state.get(user_id),
    )
    is_admin = is_admin_user(user_id)

    # --- Централизованный guard (пункт 2): админ-действия только для админов ---
    # Проверка ДО всех веток. Возвращаем True, чтобы event считался
    # «обработанным, но отклонённым» и не ушёл в state machine с пустым
    # text (для callback-событий text обычно "").
    if action in _ADMIN_PAYLOAD_ACTIONS and not is_admin:
        logger.warning(
            "handle_payload: non-admin tried admin action %r "
            "(user=%s, state=%s)",
            action, user_id, user_state.get(user_id),
        )
        return True

    # --- Sudo ---
    # ФИКС: поддержка старых inline-кнопок (sudo_confirm/sudo_cancel +
    # request_id) на время плавного деплоя, пока в чатах висят кнопки,
    # отправленные до правки create_sudo_request. Новый формат —
    # sudo_ok/sudo_no + rid — укладывается в 64-байтовый лимит Telegram.
    if action in ("sudo_confirm", "sudo_ok"):
        rid = payload_data.get("rid") or payload_data.get("request_id")
        if rid:
            execute_sudo_action(user_id, rid)
        return True
    if action in ("sudo_cancel", "sudo_no"):
        rid = payload_data.get("rid") or payload_data.get("request_id")
        if rid:
            cancel_sudo_request(user_id, rid)
        return True

    if not action:
        return False

    # --- Админ: смена статуса ---
    if action == "set_status":
        app_id = as_positive_int(payload_data.get("app_id"))
        new_status = payload_data.get("status")
        if app_id and new_status:
            handle_admin_set_status_button(user_id, app_id, new_status)
        return True

    # --- Админ: просмотр заявки ---
    if action == "view_app":
        # ФИКС P2.12: as_positive_int защищает от не-ASCII цифр ('٣'),
        # отрицательных и слишком длинных значений. str.isdigit() их
        # пропускал, а int() иногда падал или давал неожиданный результат.
        app_id = as_positive_int(payload_data.get("app_id"))
        if app_id:
            handle_admin_application_detail(user_id, app_id)
        return True

    # --- Админ: просмотр незавершённой ---
    if action == "view_incomplete":
        target_uid = as_positive_int(payload_data.get("user_id"))
        if target_uid:
            handle_admin_incomplete_detail(user_id, target_uid)
        return True

    # --- Админ: напомнить ---
    if action == "admin_remind":
        target_uid = as_positive_int(payload_data.get("user_id"))
        if target_uid:
            send_reminder_to_user(user_id, target_uid)
        return True

    # --- Админ: ответить в незавершённую ---
    if action == "admin_start_reply_incomplete":
        target_uid = as_positive_int(payload_data.get("user_id"))
        if target_uid:
            first, last = get_user_name(target_uid)
            _, ctx = get_admin_view(user_id)
            skip_list = ctx.get("skip_list", False)
            user_state.set(user_id, STATE_ADMIN_REPLY)
            user_state.update_temp(user_id, {
                "admin_reply_app_id": None,
                "admin_reply_user_id": target_uid,
                "admin_reply_from_view": "incomplete_detail",
                "admin_reply_from_ctx": {"target_uid": target_uid, "skip_list": skip_list},
            })
            set_admin_view(user_id, "reply", app_id=None, user_id=target_uid,
                           from_view="incomplete_detail",
                           from_ctx={"target_uid": target_uid, "skip_list": skip_list})
            send_message(user_id,
                         f"💬 Режим ответа пользователю\n\nПолучатель: {first} {last} (ID: {target_uid})\n\n"
                         "Напишите сообщение — оно будет отправлено пользователю.\nДля отмены нажмите кнопку ниже.",
                         keyboard=get_admin_cancel_reply_keyboard())
        return True

    # --- Админ: меню и категории ---
    if action == "admin_categories":
        show_admin_categories(user_id)
        return True
    if action == "show_category":
        category = payload_data.get("category", "new")
        if category == "incomplete":
            show_admin_incomplete_list(user_id, 0)
        else:
            show_admin_category_list(user_id, category, 0)
        return True
    if action == "admin_settings":
        show_admin_settings(user_id)
        return True
    if action == "toggle_setting":
        setting_name = payload_data.get("setting", "")
        if setting_name:
            toggle_admin_setting(user_id, setting_name)
        return True
    if action == "admin_stats":
        handle_admin_stats(user_id)
        return True
    if action == "admin_banned_list":
        show_banned_list(user_id, 0)
        return True
    if action == "admin_gdpr_requests":
        show_admin_gdpr_requests(user_id)
        return True
    if action == "admin_gdpr_page":
        try:
            page = int(payload_data.get("page", 0))
        except (TypeError, ValueError):
            page = 0
        show_admin_gdpr_requests(user_id, page=page)
        return True
    if action == "admin_gdpr_confirm_delete":
        rid = as_positive_int(payload_data.get("request_id"))
        if rid:
            handle_admin_gdpr_request_action(user_id, rid, mode="delete")
        return True
    if action == "admin_gdpr_confirm_anon":
        rid = as_positive_int(payload_data.get("request_id"))
        if rid:
            handle_admin_gdpr_request_action(user_id, rid, mode="anon")
        return True
    if action == "admin_banned_page":
        try:
            page = int(payload_data.get("page", 0))
        except (TypeError, ValueError):
            page = 0
        show_banned_list(user_id, page)
        return True
    if action == "admin_unban_prompt":
        target_uid = as_positive_int(payload_data.get("user_id"))
        target_pl = payload_data.get("platform", "")
        if target_uid and target_pl in ("vk", "tg", "max"):
            handle_admin_banned_unban_prompt(user_id, target_uid, target_pl)
        return True
    if action == "admin_changelog":
        handle_admin_changelog(user_id, "")
        return True
    if action == "admin_search_prompt":
        send_message(user_id, "Введите: /поиск <Фамилия, Имя или часть>\nПример: /поиск Иванов Иван")
        return True
    if action == "admin_user_prompt":
        send_message(user_id, "Введите: /пользователь <VK ID пользователя>\nПример: /пользователь 123456")
        return True
    if action == "admin_back":
        handle_admin_back(user_id)
        return True
    if action == "admin_page":
        try:
            page = int(payload_data.get("page", 0))
        except (TypeError, ValueError):
            page = 0
        handle_admin_page_navigation(user_id, page)
        return True
    if action == "admin_send_banner":
        app_id = as_positive_int(payload_data.get("app_id"))
        if app_id:
            start_admin_send_banner(user_id, app_id)
        return True
    if action == "admin_start_reply":
        app_id = as_positive_int(payload_data.get("app_id"))
        if app_id:
            start_admin_reply(user_id, app_id)
        return True

    # --- Пользователь ---

    # ФИКС P1.3: обработчик согласия на ПДн.
    if action == "accept_consent":
        cur = user_state.get(user_id)
        if cur != STATE_CONFIRM:
            logger.warning(
                "handle_payload: accept_consent в неверном state "
                "(user=%s, state=%s) — игнорирую", user_id, cur)
            return True
        user_state.update_temp(user_id, {"consent_accepted": True})
        # ФИКС Баг-15: только после явного согласия показываем
        # кнопку отправки заявки.
        send_message(user_id,
            "✅ Согласие принято.\n\n"
            "Теперь нажмите «Отправить заявку».",
            keyboard=get_confirm_keyboard())
        return True

    if action == "start_registration":
        start_registration(user_id)
        return True
    if action == "ask_question":
        start_question_mode(user_id)
        return True
    if action == "back_to_main":
        user_state.clear(user_id)
        handle_start(user_id)
        return True
    if action == "back_to_registration":
        return_state = user_state.get_temp(user_id).get("return_to_state", STATE_START)
        user_state.set(user_id, return_state)
        send_message(user_id, "Вы вернулись к оформлению заявки. Продолжим с того места, где остановились.")
        resend_step_prompt(user_id, return_state)
        return True

    def _take_choice(payload, allowed_values=None, max_len=200):
        """Достаёт и валидирует value из payload кнопки.

        Без белого списка и лимита длины в заявку, в админ-чат и на
        печатный баннер попадал произвольный текст из payload.

        ФИКС: защита от payload=None — раньше .get() падал с
        AttributeError, если _take_choice вызывалась с None.
        """
        if not isinstance(payload, dict):
            return None
        value = payload.get("value")
        if not isinstance(value, str):
            return None
        value = value.strip()
        if not value or len(value) > max_len:
            return None
        if allowed_values is not None and value not in allowed_values:
            return None
        return value

    if action == "select_city":
        cur = user_state.get(user_id)
        if cur != STATE_LOCATION:
            logger.warning(
                "handle_payload: select_city в неверном state "
                "(user=%s, state=%s) — игнорирую",
                user_id, cur)
            return True
        city = _take_choice(payload_data, {"Краснодар"})
        if city is None:
            logger.warning("select_city: недопустимое value (user=%s)", user_id)
            return True
        user_state.update_temp(user_id, {"location": city, "placement_page": 0})
        user_state.set(user_id, STATE_PLACEMENT_LOCATION)
        notify_admin_applications(f"📍 {admin_user_tag(user_id)} выбрал город: {city}")
        send_message(user_id, "Выберите локацию для размещения рисунка на выставке:",
                     keyboard=get_placement_location_keyboard(0))
        return True

    if action == "placement_page":
        cur = user_state.get(user_id)
        if cur != STATE_PLACEMENT_LOCATION:
            logger.warning(
                "handle_payload: placement_page в неверном state "
                "(user=%s, state=%s) — игнорирую",
                user_id, cur)
            return True
        try:
            page = int(payload_data.get("page", 0))
        except (TypeError, ValueError):
            page = 0
        total_pages = (len(PLACEMENT_LOCATIONS_SHORT) + PLACEMENT_PAGE_SIZE - 1) // PLACEMENT_PAGE_SIZE
        page = max(0, min(page, total_pages - 1))
        user_state.update_temp(user_id, {"placement_page": page})
        send_message(user_id, "📍 Локации:", keyboard=get_placement_location_keyboard(page))
        return True

    if action == "select_placement":
        cur = user_state.get(user_id)
        if cur != STATE_PLACEMENT_LOCATION:
            logger.warning(
                "handle_payload: select_placement в неверном state "
                "(user=%s, state=%s) — игнорирую",
                user_id, cur)
            return True
        location = _take_choice(payload_data, set(PLACEMENT_LOCATIONS))
        if location is None:
            logger.warning("select_placement: недопустимое value (user=%s)",
                           user_id)
            return True
        user_state.update_temp(user_id, {"placement_location": location})
        user_state.set(user_id, STATE_USER_TYPE)
        notify_admin_applications(f"📍 {admin_user_tag(user_id)} выбрал локацию размещения: {location}")
        send_message(user_id, "Выберите ваш вариант:", keyboard=get_user_type_keyboard())
        return True

    if action == "select_user_type":
        cur = user_state.get(user_id)
        if cur != STATE_USER_TYPE:
            logger.warning(
                "handle_payload: select_user_type в неверном state "
                "(user=%s, state=%s) — игнорирую",
                user_id, cur)
            return True
        user_type = payload_data.get("value", "")
        if not user_type or user_type not in USER_TYPES:
            return True
        user_state.update_temp(user_id, {"user_type": user_type})
        notify_admin_applications(f"👤 {admin_user_tag(user_id)} выбрал: {user_type}")
        user_state.set(user_id, STATE_ORG_PHONE)
        send_message(user_id, get_phone_prompt_text(), keyboard=get_phone_keyboard())
        return True

    if action == "confirm_org_info":
        cur = user_state.get(user_id)
        if cur != STATE_ORG_INFO:
            logger.warning(
                "handle_payload: confirm_org_info в неверном state "
                "(user=%s, state=%s) — игнорирую",
                user_id, cur)
            return True
        process_org_info_confirm(user_id)
        return True

    if action == "select_format":
        cur = user_state.get(user_id)
        if cur != STATE_FORMAT:
            logger.warning(
                "handle_payload: select_format в неверном state "
                "(user=%s, state=%s) — игнорирую",
                user_id, cur)
            return True
        format_value = _take_choice(payload_data, set(FORMAT_LABELS))
        if format_value:
            logger.info(
                "handle_payload: process_format user=%s value=%r",
                user_id, format_value)
            process_format(user_id, format_value)
        else:
            logger.warning(
                "handle_payload: select_format без value (user=%s) — игнорирую",
                user_id)
        return True

    if action == "skip_org_info":
        cur = user_state.get(user_id)
        if cur != STATE_ORG_INFO:
            logger.warning(
                "handle_payload: skip_org_info в неверном state "
                "(user=%s, state=%s) — игнорирую",
                user_id, cur)
            return True
        if not user_state.get_temp(user_id).get("can_skip_org_info", False):
            send_message(user_id,
                "Пропустить заполнение информации нельзя — это ваша первая заявка.\n\n"
                "Пожалуйста, заполните информацию о себе и нажмите «Подтвердить».",
                keyboard=get_org_info_keyboard(can_skip=False, has_previous=False))
            return True
        process_org_info_skip(user_id)
        return True

    if action == "confirm_child_photo":
        cur = user_state.get(user_id)
        if cur != STATE_CHILD_PHOTO:
            logger.warning(
                "handle_payload: confirm_child_photo в неверном state "
                "(user=%s, state=%s) — игнорирую",
                user_id, cur)
            return True
        process_child_photo_confirm(user_id)
        return True

    if action == "confirm_drawings":
        cur = user_state.get(user_id)
        if cur != STATE_DRAWINGS:
            logger.warning(
                "handle_payload: confirm_drawings в неверном state "
                "(user=%s, state=%s) — игнорирую",
                user_id, cur)
            return True
        process_drawings_confirm(user_id)
        return True

    if action == "submit_application":
        cur = user_state.get(user_id)
        if cur != STATE_CONFIRM:
            logger.warning(
                "handle_payload: submit_application в неверном state "
                "(user=%s, state=%s) — игнорирую",
                user_id, cur)
            return True
        process_confirm(user_id)
        return True

    if action == "skip_extras":
        cur = user_state.get(user_id)
        if cur != STATE_EXTRAS:
            logger.warning(
                "handle_payload: skip_extras в неверном state "
                "(user=%s, state=%s) — игнорирую",
                user_id, cur)
            return True
        skip_extras(user_id)
        return True

    if action == "next_application":
        cur = user_state.get(user_id)
        if cur != STATE_NEXT_ACTION:
            logger.warning(
                "handle_payload: next_application в неверном state "
                "(user=%s, state=%s) — игнорирую",
                user_id, cur)
            return True
        if platform() == "tg":
            send_message(user_id,
                "Благодарю за внимательность при заполнении!\n\n"
                "<b>Данные для размещения информации о проекте ребенка "
                "будут взяты из заполненных форм.</b>",
                parse_mode="HTML")
        else:
            send_message(user_id,
                "Благодарю за внимательность при заполнении!\n\n"
                "‼️ Данные для размещения информации о проекте ребенка "
                "будут взяты из заполненных форм. ‼️")
        start_registration(user_id)
        return True

    if action == "finish":
        cur = user_state.get(user_id)
        if cur != STATE_NEXT_ACTION:
            logger.warning(
                "handle_payload: finish в неверном state "
                "(user=%s, state=%s) — игнорирую",
                user_id, cur)
            return True
        user_state.set(user_id, STATE_EXTRAS)
        send_extras_message(user_id)
        return True

    return False