# core/registration.py
"""Шаги регистрации заявки: от старта до подтверждения.

Всё, что делает обычный пользователь, проходя путь заявки. Зависит от
core.notify (отправка), core.users (лимиты), core.storage (UserState,
заявки), core.keyboards (клавиатуры), core.texts (константы),
core.media (extract meta), core.db (register_media_file).
"""
import asyncio
import logging

import phonenumbers
from phonenumbers import NumberParseException, PhoneNumberFormat

from core.db import (
    get_db_connection, return_db_connection,
    register_media_file,
)
from core.notify import (
    send_message,
    notify_admin_applications,
    admin_user_tag,
    get_user_name,
    forward_to_admin,
    _dialog_link,
)
from core.runtime import platform
from core.users import check_rate_limit
from core.storage import (
    user_state,
    save_message_to_db,
    save_application_to_db,
    has_previous_applications,
    get_latest_application,
    extract_phone_from_contact,
    get_non_photo_types,
    format_wrong_types,
)
from core.media import extract_photo_meta, extract_doc_meta, is_vk_attachment_string
from core.keyboards import (
    get_main_keyboard, get_location_keyboard, get_placement_location_keyboard,
    get_user_type_keyboard, get_phone_keyboard, get_phone_prompt_text,
    get_org_info_keyboard, get_format_keyboard, get_child_photo_keyboard,
    get_drawings_keyboard, get_confirm_keyboard, get_after_submit_inline_keyboard,
    get_question_keyboard, get_extras_keyboard, get_final_inline_keyboard,
    get_empty_keyboard, get_rejection_keyboard,
    get_consent_keyboard,  # ФИКС P1.3
)
from core.texts import (
    STATE_START, STATE_LOCATION, STATE_PLACEMENT_LOCATION, STATE_USER_TYPE,
    STATE_ORG_PHONE, STATE_ORG_INFO, STATE_NAME_AGE, STATE_CHILD_PHOTO,
    STATE_FORMAT, STATE_DRAWINGS, STATE_TRAINING, STATE_PAYMENT,
    STATE_CONFIRM, STATE_EXTRAS, STATE_NEXT_ACTION, STATE_FINAL,
    STATE_QUESTION,
    MAX_TEXT_LENGTH, MAX_PHOTOS, MAX_DOC_SIZE, ALLOWED_DOC_EXT,
    PLACEMENT_LOCATIONS, PLACEMENT_LOCATIONS_SHORT,
    USER_TYPES, USER_TYPES_WITH_ORG_INFO,
    EXTRAS_TEXT, EXTRAS_PHOTO_IDS, EXTRAS_TG_FILE_IDS, EXTRAS_MAX_FILE_IDS,
    CONSENT_VERSION, CONSENT_TEXT,  # ФИКС P1.3
)


logger = logging.getLogger("bright_city_bot")


import logging as _logging
logger = _logging.getLogger("bright_city_bot")


def _send_extras_photos(user_id):
    from core.texts import EXTRAS_PHOTO_URLS
    from core.runtime import get_adapter
    from core.utils import _fire_and_forget

    if not EXTRAS_PHOTO_URLS:
        logger.warning("_send_extras_photos: EXTRAS_PHOTO_URLS пуст — пропуск")
        return

    adapter = get_adapter()

    async def _send():
        try:
            ok = await adapter.send_attachment(
                user_id, list(EXTRAS_PHOTO_URLS), kind="photo",
            )
            if ok:
                logger.info("extras photos sent to user=%s (%d)", user_id,
                            len(EXTRAS_PHOTO_URLS))
            else:
                logger.warning("extras photos rejected (user=%s)", user_id)
        except Exception as e:
            logger.warning("extras photos failed (user=%s): %s", user_id, e)

    _fire_and_forget(_send(), label=f"extras_photos user={user_id}")


# ============================================================
#  СТАРТ
# ============================================================

def handle_start(user_id):
    """Показывает главное меню. Сбрасывает состояние пользователя.

    ФИКС: TG-клиенты не всегда перерисовывают reply-клавиатуру, когда
    она приходит ОДНИМ сообщением в ответ на inline callback
    (например, «Вернуться в главное меню» из финального экрана).
    Симптом: после callback сообщение видно, а клавиатура появляется
    только когда пользователь напишет текст в чат.

    Решение: для TG текст и клавиатуру отправляем ДВУМЯ сообщениями.
    Второе короткое сообщение с клавиатурой гарантированно триггерит
    перерисовку — тот же эффект, что и в finish → extras, где перед
    клавиатурой идут фото.
    """
    user_state.set(user_id, STATE_START)
    user_state.set_temp(user_id, {})
    first_name, _ = get_user_name(user_id)
    greeting = (
        f"Привет, {first_name}! ✌\n\n"
        "Это ваш чат-бот, который поможет во всём разобраться и подать заявку на выставку.\n\n"
        "Заявки обрабатываются человеком. Если у вас что-то не получается — нажмите «Задать вопрос» "
        "и напишите в чате на любом этапе, через время вам ответят.\n\n"
        "В крайнем случае можно обратиться к родителям проекта в Чате поддержки."
    )
    if platform() == "tg":
        send_message(user_id, greeting)
        send_message(user_id, "Выберите действие 👇", keyboard=get_main_keyboard())
    else:
        send_message(user_id, greeting, keyboard=get_main_keyboard())


def start_registration(user_id):
    allowed, wait = check_rate_limit(user_id, scope="start")
    if not allowed:
        send_message(user_id, f"⏳ Вы только что начали оформление заявки. Подождите {wait} сек. перед новой.")
        return
    user_state.set(user_id, STATE_LOCATION)
    user_state.set_temp(user_id, {"user_id": user_id})
    send_message(user_id, "Выберите локацию выставки:", keyboard=get_location_keyboard())


def handle_off_script(user_id, text, state):
    """Пользователь написал вне сценария — пересылаем админам
    и возвращаем его на экран текущего шага, чтобы он не залипал.
    """
    if text and len(text) > MAX_TEXT_LENGTH:
        text = text[:MAX_TEXT_LENGTH]
    forward_to_admin(user_id, text, state)
    send_message(user_id,
        "Ваше сообщение передано администратору. Он ответит вам в ближайшее время.\n\n"
        "Вы можете продолжить заполнение заявки с того же места или дождаться ответа.")
    # Возвращаем пользователя на экран шага с подсказкой и клавиатурой.
    # Без этого он видел только «сообщение передано админам» и не понимал,
    # что бот всё ещё ждёт ввода (телефон, имя, локацию и т.д.).
    resend_step_prompt(user_id, state)


def resend_step_prompt(user_id, state):
    """Повторно показывает подсказку для указанного шага."""
    if state == STATE_LOCATION:
        send_message(user_id, "Выберите локацию выставки:", keyboard=get_location_keyboard())
    elif state == STATE_PLACEMENT_LOCATION:
        page = user_state.get_temp(user_id).get("placement_page", 0)
        send_message(user_id, "Выберите локацию для размещения рисунка на выставке:",
                     keyboard=get_placement_location_keyboard(page))
    elif state == STATE_USER_TYPE:
        send_message(user_id, "Выберите ваш вариант:", keyboard=get_user_type_keyboard())
    elif state == STATE_ORG_PHONE:
        send_message(user_id, get_phone_prompt_text(), keyboard=get_phone_keyboard())
    elif state == STATE_ORG_INFO:
        can_skip = user_state.get_temp(user_id).get("can_skip_org_info", False)
        msg = ("Заполните информацию о себе.\n\nПришлите так, как напечатать на баннере:\n\n"
               "1. Название организации\n2. ФИО педагога\n3. Ссылку на сайт или группу (из неё сделаем QR код)\n"
               "4. Логотип, на прозрачном фоне (логотип.png)\n\n")
        if can_skip:
            msg += "Если данные изменились — пришлите новую информацию и нажмите «Подтвердить».\nЕсли всё по-прежнему — нажмите «Использовать предыдущие данные»."
        else:
            msg += "Когда пришлёте всю информацию, нажмите кнопку «Подтвердить»."
        send_message(user_id, msg, keyboard=get_org_info_keyboard(can_skip=can_skip, has_previous=can_skip))
    elif state == STATE_NAME_AGE:
        send_message(user_id, "Напишите Фамилию Имя и возраст ребёнка.\n\nПример: Иванов Иван 5лет",
                     keyboard=get_empty_keyboard())
    elif state == STATE_CHILD_PHOTO:
        _send_child_photo_example(user_id)
        send_message(user_id, "Загрузите фото вашего ребенка для публикации на баннере.\n\nИзображение чёткое, не обрезанное, портрет, должно быть видно лицо, соотношение сторон приближено к квадрату.\n\nПосле загрузки нажмите «Подтвердить».",
                     keyboard=get_child_photo_keyboard()),
    elif state == STATE_FORMAT:
        send_message(user_id, "Выберите формат рисунка:\n\n- прикрепите скрин/сертификат победы в наших конкурсах или\n- горизонтальный или квадратный рисунок (занимает весь баннер, вытянут по горизонтали).\n- 1 вертикальный рисунок (занимает пол баннера, вытянут вверх/по вертикали).\nили по предъявлению документа о наличии инвалидности у ребенка, для волонтеров проекта.\n- 2 вертикальных рисунка (занимают весь баннер).",
                     keyboard=get_format_keyboard())
    elif state == STATE_DRAWINGS:
        send_message(user_id, "Загрузите фото или сканы рисунков ребенка.\n\nНе обрезанное, без обработки, без посторонних теней и частей тела, соотношение сторон, как А3/А4.\n\nПосле загрузки нажмите «Подтвердить».\n\nЕсли нужно, прикрепите ещё рисунки.",
                     keyboard=get_drawings_keyboard())
    elif state == STATE_TRAINING:
        send_message(user_id, "Где и у кого ребенок обучается рисованию?\n\nНапример: Дома или ЦДР Радуга, педагог Петрова Анна Павловна")
    elif state == STATE_PAYMENT:
        send_message(user_id, "1. Переведите пожертвование на реализацию проекта, нажмите ЗДЕСЬ: https://www.yarkijgorod.ru/pay\n\n👇\n\n2. Пришлите в этот чат квитанцию о переводе / справку об инвалидности ребенка или скрин переписки о других договоренностях с руководителем проекта.",
                     keyboard=get_empty_keyboard())
    elif state == STATE_CONFIRM:
        # ФИКС Баг-15: явный шаг согласия перед отправкой.
        # Раньше показывалась только кнопка «Отправить заявку», а
        # consent_* в БД писались безусловно — «согласие» без
        # отдельного действия пользователя.
        #
        # ФИКС: если согласие уже дано (consent_accepted=True), не
        # переспрашиваем при off-script — иначе пользователь видит
        # полный текст согласия и кнопку «Согласен» повторно.
        if user_state.get_temp(user_id).get("consent_accepted"):
            send_message(
                user_id,
                "Если всё готово — нажмите кнопку «Отправить заявку» ниже.",
                keyboard=get_confirm_keyboard())
        else:
            send_message(user_id,
                         CONSENT_TEXT,
                         keyboard=get_consent_keyboard())
            send_message(user_id,
                         "Нажимая кнопку «Отправить заявку», Вы соглашаетесь c политикой конфиденциальности (https://yarkijgorod.ru/politika), офертой (https://yarkijgorod.ru/oferta) и даете согласие на обработку персональных данных и публикацию данных несовершеннолетнего (https://yarkijgorod.ru/consent)",
                         keyboard=get_confirm_keyboard())
    elif state == STATE_EXTRAS:
        send_extras_message(user_id)
    elif state == STATE_NEXT_ACTION:
        send_message(user_id, "Выберите следующее действие:", keyboard=get_after_submit_inline_keyboard())
    elif state == STATE_FINAL:
        handle_start(user_id)
    else:
        handle_start(user_id)


# ============================================================
#  ВЫБОР ГОРОДА И ЛОКАЦИИ
# ============================================================

def process_location(user_id, text):
    if "краснодар" not in text.strip().lower():
        handle_off_script(user_id, text, STATE_LOCATION)
        return
    user_state.update_temp(user_id, {"location": "Краснодар", "placement_page": 0})
    user_state.set(user_id, STATE_PLACEMENT_LOCATION)
    notify_admin_applications(f"📍 {admin_user_tag(user_id)} выбрал город: Краснодар")
    send_message(user_id, "Выберите локацию для размещения рисунка на выставке:",
                 keyboard=get_placement_location_keyboard(0))


def process_placement_location(user_id, text):
    text_lower = text.strip().lower()
    short_to_full = dict(zip(PLACEMENT_LOCATIONS_SHORT, PLACEMENT_LOCATIONS))
    for short, full in short_to_full.items():
        if text_lower == short.lower() or text_lower == short[:40].lower():
            user_state.update_temp(user_id, {"placement_location": full})
            user_state.set(user_id, STATE_USER_TYPE)
            notify_admin_applications(f"📍 {admin_user_tag(user_id)} выбрал локацию размещения: {full}")
            send_message(user_id, "Выберите ваш вариант:", keyboard=get_user_type_keyboard())
            return
    for loc in PLACEMENT_LOCATIONS:
        if text_lower == loc.lower():
            user_state.update_temp(user_id, {"placement_location": loc})
            user_state.set(user_id, STATE_USER_TYPE)
            notify_admin_applications(f"📍 {admin_user_tag(user_id)} выбрал локацию размещения: {loc}")
            send_message(user_id, "Выберите ваш вариант:", keyboard=get_user_type_keyboard())
            return
    locations_map = {
        "восточно-кругликовская": "ул. Восточно-Кругликовская",
        "сормовская, 179": "ул. Сормовская, 179/1, напротив храма", "сормовская 179": "ул. Сормовская, 179/1, напротив храма",
        "сормовская, 33": "Сормовская, 33", "сормовская 33": "Сормовская, 33",
        "дзержинского, 207": "ул. Дзержинского, 207, ограждение ДС 107", "дзержинского 207": "ул. Дзержинского, 207, ограждение ДС 107",
        "уральская, 35": "ул. Уральская, 35 (угол с Волжской)", "уральская 35": "ул. Уральская, 35 (угол с Волжской)",
        "кубанская набережная": "ул. Кубанская набережная, между Мира и Орджоникидзе",
        "кожевенная, 76": "ул. Кожевенная, 76", "кожевенная 76": "ул. Кожевенная, 76",
        "кореновская, 21": "Кореновская, 21 (Прикубанский округ)", "кореновская 21": "Кореновская, 21 (Прикубанский округ)",
        "гмр": "ГМР, ограждение рынка на Невкипелова", "невкипелова": "ГМР, ограждение рынка на Невкипелова",
        "чмр": "ЧМР, ул. Ставропольская между домами 105 и 97", "ставропольская": "ЧМР, ул. Ставропольская между домами 105 и 97",
        "солнечный остров": "парк Солнечный остров ул. Трамвайная 2/6", "трамвайная 2": "парк Солнечный остров ул. Трамвайная 2/6",
        "кмр": "КМР, угол ул. Уральской и Тюляева", "тюляева": "КМР, угол ул. Уральской и Тюляева",
        "тэц": "ТЭЦ (ограждение по ул. Трамвайная 4-6)", "трамвайная 4": "ТЭЦ (ограждение по ул. Трамвайная 4-6)",
    }
    placement = None
    for key, value in locations_map.items():
        if key in text_lower:
            placement = value
            break
    if not placement:
        handle_off_script(user_id, text, STATE_PLACEMENT_LOCATION)
        return
    user_state.update_temp(user_id, {"placement_location": placement})
    user_state.set(user_id, STATE_USER_TYPE)
    notify_admin_applications(f"📍 {admin_user_tag(user_id)} выбрал локацию размещения: {placement}")
    send_message(user_id, "Выберите ваш вариант:", keyboard=get_user_type_keyboard())


# ============================================================
#  ТЕЛЕФОН
# ============================================================

def process_org_phone(user_id, attachments, text=None):
    phone_raw = extract_phone_from_contact(attachments)
    if not phone_raw and text and text.strip():
        text_stripped = text.strip()
        try:
            parsed_number = phonenumbers.parse(text_stripped, "RU")
            if phonenumbers.is_valid_number(parsed_number):
                phone_raw = phonenumbers.format_number(parsed_number, PhoneNumberFormat.E164)
        except NumberParseException:
            pass
        if not phone_raw:
            digits = "".join(c for c in text_stripped if c.isdigit() or c == "+")
            total_chars = len(text_stripped)
            digit_count = sum(1 for c in text_stripped if c.isdigit())
            phone_chars = sum(1 for c in text_stripped if c.isdigit() or c in "+ ()-")
            looks_like_phone = phone_chars / max(total_chars, 1) >= 0.6 and digit_count >= 10
            if looks_like_phone and 10 <= len(digits) <= 15:
                phone_raw = digits

    if not phone_raw:
        if text and text.strip():
            text_stripped = text.strip()
            # ФИКС: если пользователь явно пытается ввести номер, но ошибся
            # форматом (например, "8474793" или "+7 999"), не пересылаем
            # это админам как вопрос. Просто просим исправить.
            digit_count = sum(1 for c in text_stripped if c.isdigit())
            is_phone_attempt = (
                digit_count >= 3
                and all(c.isdigit() or c in "+ ()-" for c in text_stripped)
            )
            if is_phone_attempt:
                send_message(
                    user_id,
                    "Не удалось распознать номер.\n\n"
                    "Пожалуйста, отправьте российский номер в формате "
                    "+7 XXX XXX-XX-XX или 8 XXX XXX-XX-XX (10–11 цифр).",
                    keyboard=get_phone_keyboard(),
                )
            else:
                # Это действительно текстовый вопрос, пересылаем админам
                handle_off_script(user_id, text, STATE_ORG_PHONE)
        else:
            send_message(user_id,
                "Не удалось получить номер.\n\n"
                "Напишите ваш номер телефона и отправьте его в чат.",
                keyboard=get_phone_keyboard())
        return

    try:
        parsed_number = phonenumbers.parse(phone_raw, "RU")
        if not phonenumbers.is_valid_number(parsed_number):
            send_message(user_id,
                "Номер некорректен.\n\n"
                "Пожалуйста, напишите правильный российский номер "
                "в формате +7 XXX XXX-XX-XX или 8 XXX XXX-XX-XX.",
                keyboard=get_phone_keyboard())
            return
        normalized_phone = phonenumbers.format_number(parsed_number, PhoneNumberFormat.E164)
        user_state.update_temp(user_id, {"phone": normalized_phone})
        notify_admin_applications(f"📞 {admin_user_tag(user_id)} прислал номер: {normalized_phone}")
        send_message(user_id, f"✅ Номер принят! Ваш номер: {normalized_phone}")
        user_type = user_state.get_temp(user_id).get("user_type", "")
        if user_type not in USER_TYPES_WITH_ORG_INFO:
            user_state.set(user_id, STATE_NAME_AGE)
            send_message(user_id,
                "Напишите Фамилию Имя и возраст ребёнка.\n\nПример: Иванов Иван 5лет",
                keyboard=get_empty_keyboard())
            return
        has_prev = has_previous_applications(user_id)
        user_state.set(user_id, STATE_ORG_INFO)
        user_state.update_temp(user_id, {"org_info_provided": False, "can_skip_org_info": has_prev})
        msg = ("❗Очень важно❗ Заполняя данные на детей у вас должно быть согласие от родителей на передачу этой информации!\n\n"
               "Заполните информацию о себе.\nПришлите так, как напечатать на баннере. Если что-то печатать не нужно, не присылайте:\n\n"
               "1. Название организации\n2. ФИО педагога\n3. Ссылку на сайт или группу (из неё сделаем QR код)\n"
               "4. Логотип, на прозрачном фоне (логотип.png)\nУсловия публикация логотипа уточняйте по телефону\n\n")
        if has_prev:
            msg += "Если данные изменились — пришлите новую информацию и нажмите «Подтвердить».\nЕсли всё по-прежнему — нажмите «Использовать предыдущие данные»."
        else:
            msg += "Когда пришлёте всю информацию, нажмите кнопку «Подтвердить»."
        send_message(user_id, msg, keyboard=get_org_info_keyboard(can_skip=has_prev, has_previous=has_prev))
    except NumberParseException:
        send_message(user_id, "Не удалось распознать номер.\n\nПожалуйста, напишите номер в формате +7 XXX XXX-XX-XX или 8 XXX XXX-XX-XX.")


# ============================================================
#  ИНФОРМАЦИЯ О СЕБЕ
# ============================================================

def process_org_info(user_id, text, attachments):
    temp = dict(user_state.get_temp(user_id))

    if text and text.strip():
        text_stripped = text.strip()[:MAX_TEXT_LENGTH]
        if text_stripped.startswith("/"):
            send_message(user_id,
                "ℹ️ Похоже, вы ввели команду.\n\n"
                "Пришлите информацию о себе текстом — так, как её нужно напечатать на баннере:\n"
                "1. Название организации\n2. ФИО педагога\n3. Ссылку на сайт или группу\n"
                "4. Логотип (логотип.png на прозрачном фоне)\n\n"
                "Когда всё готово — нажмите «Подтвердить».",
                keyboard=get_org_info_keyboard(
                    can_skip=temp.get("can_skip_org_info", False),
                    has_previous=temp.get("can_skip_org_info", False)))
            return
        save_message_to_db(user_id, "in", text_stripped)
        temp["org_info_provided"] = True
        existing = temp.get("org_info_text", "")
        if existing:
            temp["org_info_text"] = (existing + "\n" + text_stripped)[:MAX_TEXT_LENGTH]
        else:
            temp["org_info_text"] = text_stripped

    if attachments:
        temp["org_info_provided"] = True
        temp.setdefault("org_info_attachment_urls", [])
        temp["org_info_attachment_urls"] = list(temp["org_info_attachment_urls"])

        for att in attachments:
            if att.get("type") == "photo":
                sizes = att.get('photo', {}).get('sizes', [])
                if not sizes or 'url' not in sizes[-1]:
                    continue
                url = sizes[-1]['url']
                if not isinstance(url, str) or not url:
                    continue
                if not (url.startswith(("http://", "https://"))
                        or is_vk_attachment_string(url)):
                    continue
                temp["org_info_attachment_urls"].append(url)
                photo_obj = att.get("photo") or {}
                last = sizes[-1]
                try:
                    media_id = register_media_file(
                        url=url,
                        owner_id=photo_obj.get("owner_id") or last.get("_vk_owner_id"),
                        photo_id=photo_obj.get("id") or last.get("_vk_photo_id"),
                        kind="photo",
                        user_platform=platform(),
                        user_id=user_id,
                        source_file_id=att.get("_tg_source_file_id"),
                        source_platform="tg" if att.get("_tg_source_file_id") else None,
                        vk_url=last.get("_vk_url"),
                        vk_owner_id=last.get("_vk_owner_id"),
                        vk_photo_id=last.get("_vk_photo_id"),
                    )
                    if media_id and last.get("_vk_url"):
                        try:
                            from core.media import cache_media_locally
                            from core.db import update_media_file_local_url
                            local_url = cache_media_locally(
                                last["_vk_url"], media_id)
                            if local_url:
                                update_media_file_local_url(media_id, local_url)
                        except Exception as e:
                            logger.warning(
                                "process_org_info: cache media failed "
                                "(id=%s): %s", media_id, e)
                except Exception as e:
                    logger.warning(
                        "media_file register (org_info photo) failed: %s", e)

            elif att.get("type") == "doc":
                doc_info = att.get("doc", {})
                ext = (doc_info.get("ext") or "").lower().lstrip(".")
                if ext and ext not in ALLOWED_DOC_EXT:
                    send_message(user_id,
                                 f"Неподдерживаемый тип файла (.{ext}). "
                                 f"Разрешены: PDF, JPG, PNG, GIF, BMP, WEBP, TIFF, DOC, DOCX.")
                    continue
                doc_size = doc_info.get("size") or 0
                if doc_size and doc_size > MAX_DOC_SIZE:
                    max_mb = MAX_DOC_SIZE // (1024 * 1024)
                    send_message(user_id,
                                 f"Файл больше {max_mb} МБ. Перешлите файл меньшего "
                                 f"размера, пожалуйста.")
                    continue
                url = doc_info.get('url')
                if not isinstance(url, str) or not url:
                    continue
                if not (url.startswith(("http://", "https://"))
                        or is_vk_attachment_string(url)):
                    continue
                temp["org_info_attachment_urls"].append(url)
                try:
                    media_id = register_media_file(
                        url=url,
                        owner_id=doc_info.get("owner_id")
                                 or doc_info.get("_vk_owner_id"),
                        photo_id=None,
                        doc_id=doc_info.get("id") or doc_info.get("_vk_doc_id"),
                        kind="doc",
                        user_platform=platform(),
                        user_id=user_id,
                        source_file_id=att.get("_tg_source_file_id"),
                        source_platform="tg" if att.get("_tg_source_file_id") else None,
                        vk_url=doc_info.get("_vk_url"),
                        vk_owner_id=doc_info.get("_vk_owner_id"),
                        vk_doc_id=doc_info.get("_vk_doc_id"),
                    )
                    if media_id and doc_info.get("_vk_url"):
                        try:
                            from core.media import cache_media_locally
                            from core.db import update_media_file_local_url
                            local_url = cache_media_locally(
                                doc_info["_vk_url"], media_id)
                            if local_url:
                                update_media_file_local_url(media_id, local_url)
                        except Exception as e:
                            logger.warning(
                                "process_org_info: cache media failed "
                                "(id=%s): %s", media_id, e)
                except Exception as e:
                    logger.warning(
                        "media_file register (org_info doc) failed: %s", e)
        temp["org_info_attachments"] = bool(temp.get("org_info_attachment_urls", []))

    user_state.set_temp(user_id, temp)

    info_provided = temp.get("org_info_provided", False)
    if info_provided:
        reply = ("Данные приняты. Если нужно что-то добавить — присылайте.\n"
                 "Когда всё готово, нажмите «Подтвердить».")
    else:
        reply = ("Пришлите информацию о себе (название организации, ФИО педагога, ссылку, логотип).\n"
                 "Или нажмите «Использовать предыдущие данные», если ничего не изменилось.")
    send_message(user_id, reply, keyboard=get_org_info_keyboard(
        can_skip=temp.get("can_skip_org_info", False),
        has_previous=temp.get("can_skip_org_info", False)))


def process_org_info_confirm(user_id):
    temp = user_state.get_temp(user_id)
    has_prev = temp.get("can_skip_org_info", False)
    info_provided = temp.get("org_info_provided", False)
    info_text = temp.get("org_info_text", "")
    attachment_urls = temp.get("org_info_attachment_urls", [])
    has_meaningful_content = bool(info_text and len(info_text) >= 3) or bool(attachment_urls)
    if not has_prev and not info_provided:
        send_message(user_id, "Пожалуйста, заполните информацию о себе перед подтверждением.\n\nПришлите название организации, ФИО педагога, ссылку на сайт или группу, логотип — всё, что нужно напечатать на баннере.\n\nПосле этого нажмите «Подтвердить».",
                     keyboard=get_org_info_keyboard(can_skip=False, has_previous=False))
        return
    if not has_prev and info_provided and not has_meaningful_content:
        send_message(user_id, "Данные слишком короткие. Пожалуйста, заполните информацию о себе (минимум 3 символа) и нажмите «Подтвердить».",
                     keyboard=get_org_info_keyboard(can_skip=False, has_previous=False))
        return
    if has_prev and not info_provided:
        send_message(user_id, "Пожалуйста, заполните информацию о себе перед подтверждением.\n\nЕсли данные не изменились — нажмите «Использовать предыдущие данные».",
                     keyboard=get_org_info_keyboard(can_skip=True, has_previous=True))
        return
    if has_prev and info_provided and not has_meaningful_content:
        send_message(user_id, "Данные слишком короткие. Пришлите информацию заново или нажмите «Использовать предыдущие данные».",
                     keyboard=get_org_info_keyboard(can_skip=True, has_previous=True))
        return
    admin_parts = [f"📋 {admin_user_tag(user_id)} завершил заполнение информации о себе:"]
    if info_text:
        admin_parts.append(f"\nТекст:\n{info_text}")
    if attachment_urls:
        n = len(attachment_urls)
        if n <= 10:
            labels = ", ".join(f"Вложение {i}" for i in range(1, n + 1))
        else:
            labels = f"Вложение 1 … Вложение {n}"
        admin_parts.append(f"\nВложения: {n} шт. — придут отдельно: {labels}")
    admin_parts.append(f"\n🔗 Диалог: {_dialog_link(user_id)}")
    admin_parts.append(f"Ответить: /ответ {user_id} ваш текст")
    notify_admin_applications("\n".join(admin_parts))
    user_state.set(user_id, STATE_NAME_AGE)
    send_message(user_id,
        "✅ Информация о себе принята.\n\n"
        "Переходим к следующему шагу — данным ребёнка.\n\n"
        "Напишите Фамилию Имя и возраст ребёнка.\n\n"
        "Пример: Иванов Иван 5лет",
        keyboard=get_empty_keyboard())


def process_org_info_skip(user_id):
    prev_app = get_latest_application(user_id)
    prev_attachments = prev_app.get("org_info_attachments", []) if prev_app else []
    user_state.update_temp(user_id, {"org_info_text": "", "org_info_attachment_urls": prev_attachments,
                                     "org_info_attachments": bool(prev_attachments)})
    if prev_app:
        notify_admin_applications(f"⏭ {admin_user_tag(user_id)} использовал предыдущие данные (тип: {prev_app.get('user_type', 'н/д')})")
    else:
        notify_admin_applications(f"⏭ {admin_user_tag(user_id)} пропустил заполнение информации")
    user_state.set(user_id, STATE_NAME_AGE)
    send_message(user_id,
        "✅ Использованы предыдущие данные.\n\n"
        "Переходим к следующему шагу — данным ребёнка.\n\n"
        "Напишите Фамилию Имя и возраст ребёнка.\n\n"
        "Пример: Иванов Иван 5лет",
        keyboard=get_empty_keyboard())


# ============================================================
#  ИМЯ И ВОЗРАСТ РЕБЁНКА
# ============================================================

def process_name_age(user_id, text):
    text_stripped = text.strip()
    if not text_stripped or len(text_stripped) < 2:
        handle_off_script(user_id, text, STATE_NAME_AGE)
        return
    if text_stripped.startswith("/"):
        send_message(user_id,
            "ℹ️ Похоже, вы ввели команду.\n\n"
            "Напишите Фамилию Имя и возраст ребёнка.\n\n"
            "Пример: Иванов Иван 5лет",
            keyboard=get_empty_keyboard())
        return
    if len(text) > MAX_TEXT_LENGTH:
        text = text[:MAX_TEXT_LENGTH]
    text_stripped = text.strip()
    save_message_to_db(user_id, "in", text_stripped)
    user_state.update_temp(user_id, {"name_age": text_stripped})
    user_state.set(user_id, STATE_CHILD_PHOTO)
    notify_admin_applications(f"📝 {admin_user_tag(user_id)} указал: {text.strip()[:500]}")
    _send_child_photo_example(user_id)
    send_message(user_id, "Загрузите фото вашего ребенка для публикации на баннере.\n\nИзображение чёткое, не обрезанное, портрет, должно быть видно лицо, соотношение сторон приближено к квадрату.\n\nПосле загрузки нажмите «Подтвердить».",
                 keyboard=get_child_photo_keyboard())


import logging
from core.runtime import platform, get_adapter

logger = logging.getLogger("bright_city_bot")


# ============================================================
#  ФОТО РЕБЁНКА
# ============================================================

def process_child_photo(user_id, attachments, text=None):
    current_state = user_state.get(user_id)
    if current_state != STATE_CHILD_PHOTO:
        logger.warning(
            "process_child_photo: state mismatch (user=%s, state=%s) — "
            "игнорирую %d вложений",
            user_id, current_state, len(attachments or []))
        return

    photo_meta = extract_photo_meta(attachments)
    photo_urls = [meta["url"] for meta in photo_meta]
    if photo_urls:
        temp = dict(user_state.get_temp(user_id))
        temp.setdefault("child_photo_urls", [])
        temp["child_photo_urls"] = list(temp["child_photo_urls"])

        room = MAX_PHOTOS - len(temp["child_photo_urls"])

        if room <= 0:
            send_message(user_id,
                f"У вас уже {len(temp['child_photo_urls'])} фото "
                f"(лимит {MAX_PHOTOS}). Переходим к следующему шагу.",
                keyboard=get_empty_keyboard())
            process_child_photo_confirm(user_id)
            return

        truncated = len(photo_meta) > room
        skipped = max(0, len(photo_meta) - room)
        if truncated:
            photo_meta = photo_meta[:room]
            photo_urls = [meta["url"] for meta in photo_meta]

        for meta in photo_meta:
            url = meta["url"]
            temp["child_photo_urls"].append(url)
            try:
                media_id = register_media_file(
                    url=url,
                    owner_id=meta.get("owner_id"),
                    photo_id=meta.get("photo_id"),
                    kind="photo",
                    user_platform=platform(),
                    user_id=user_id,
                    source_file_id=meta.get("source_file_id"),
                    source_platform=meta.get("source_platform"),
                    vk_url=meta.get("vk_url"),
                    vk_owner_id=meta.get("vk_owner_id"),
                    vk_photo_id=meta.get("vk_photo_id"),
                    vk_doc_id=meta.get("vk_doc_id"),
                )
                if media_id and meta.get("vk_url"):
                    try:
                        from core.media import cache_media_locally
                        from core.db import update_media_file_local_url
                        local_url = cache_media_locally(
                            meta["vk_url"], media_id)
                        if local_url:
                            update_media_file_local_url(media_id, local_url)
                    except Exception as e:
                        logger.warning(
                            "process_child_photo: cache media failed "
                            "(id=%s): %s", media_id, e)
            except Exception as e:
                logger.warning("media_file register failed: %s", e)

        if photo_meta:
            loaded_total = len(temp["child_photo_urls"])
            n = len(photo_meta)
            if n <= 10:
                labels = ", ".join(f"Фото {i}" for i in range(1, n + 1))
            else:
                labels = f"Фото 1 … Фото {n}"
            notify_admin_applications(
                f"📸 {admin_user_tag(user_id)} загрузил "
                f"{n} фото ребёнка (всего: {loaded_total}).\n\n"
                f"Вложения: {labels}\n"
                f"Фото — в карточке заявки (открыть заявку)"
            )

        user_state.set_temp(user_id, temp)
        loaded = len(temp["child_photo_urls"])

        if loaded >= MAX_PHOTOS:
            if truncated:
                send_message(user_id,
                    f"Принято {room} фото из {room + skipped} — "
                    f"лимит {MAX_PHOTOS} достигнут.\n\n"
                    "Переходим к следующему шагу.",
                    keyboard=get_empty_keyboard())
            else:
                send_message(user_id,
                    f"Загружено {loaded} фото — лимит {MAX_PHOTOS} достигнут.\n\n"
                    "Переходим к следующему шагу.",
                    keyboard=get_empty_keyboard())
            user_state.set(user_id, STATE_FORMAT)
            send_message(user_id,
                "Выберите формат рисунка:\n\n- прикрепите скрин/сертификат победы в наших конкурсах или\n- горизонтальный или квадратный рисунок (занимает весь баннер, вытянут по горизонтали).\n- 1 вертикальный рисунок (занимает пол баннера, вытянут вверх/по вертикали).\nили по предъявлению документа о наличии инвалидности у ребенка, для волонтеров проекта.\n- 2 вертикальных рисунка (занимают весь баннер).",
                keyboard=get_format_keyboard())
            return

        if truncated:
            send_message(user_id,
                f"Принято {room} фото из {room + skipped} — лимит {MAX_PHOTOS}. "
                f"Всего загружено: {loaded}.\n\n"
                "Когда все готовы, нажмите кнопку «Подтвердить».",
                keyboard=get_child_photo_keyboard(loaded=loaded))
        else:
            send_message(user_id,
                "Фото принято. Если нужно, прикрепите ещё.\n"
                "Когда все готовы, нажмите кнопку «Подтвердить».",
                keyboard=get_child_photo_keyboard(loaded=loaded))
        return

    if attachments:
        wrong = get_non_photo_types(attachments)
        if wrong:
            send_message(user_id,
                f"Прикрепите Фото, а не {format_wrong_types(wrong)}.\n\n"
                "Нужно именно фото ребёнка для баннера.",
                keyboard=get_child_photo_keyboard())
            return
    if text and text.strip():
        handle_off_script(user_id, text, STATE_CHILD_PHOTO)
        return

    logger.debug(
        "process_child_photo: пустое событие (user=%s, attachments=%d) — игнорирую",
        user_id, len(attachments or []),
    )


def process_child_photo_confirm(user_id):
    urls_count = len(user_state.get_temp(user_id).get("child_photo_urls") or [])
    logger.info(
        "process_child_photo_confirm: user=%s child_photos=%d state_before=%s",
        user_id, urls_count, user_state.get(user_id))
    if not urls_count:
        send_message(user_id, "Необходимо прикрепить хотя бы одно фото!\n\nПожалуйста, прикрепите изображение ребёнка, а затем нажмите «Подтвердить».",
                     keyboard=get_child_photo_keyboard())
        return
    user_state.set(user_id, STATE_FORMAT)
    logger.info(
        "process_child_photo_confirm: user=%s → STATE_FORMAT", user_id)
    send_message(user_id, "Выберите формат рисунка:\n\n- прикрепите скрин/сертификат победы в наших конкурсах или\n- горизонтальный или квадратный рисунок (занимает весь баннер, вытянут по горизонтали).\n- 1 вертикальный рисунок (занимает пол баннера, вытянут вверх/по вертикали).\nили по предъявлению документа о наличии инвалидности у ребенка, для волонтеров проекта.\n- 2 вертикальных рисунка (занимают весь баннер).",
                 keyboard=get_format_keyboard())


# ============================================================
#  ФОРМАТ РИСУНКА
# ============================================================

def process_format(user_id, format_value):
    logger.info(
        "process_format: user=%s value=%r state_before=%s",
        user_id, format_value, user_state.get(user_id))
    user_state.update_temp(user_id, {"format_choice": format_value})
    user_state.set(user_id, STATE_DRAWINGS)
    logger.info(
        "process_format: user=%s → STATE_DRAWINGS", user_id)
    notify_admin_applications(f"📐 {admin_user_tag(user_id)} выбрал формат: {format_value}")
    send_message(user_id, "Загрузите фото или сканы рисунков ребенка.\n\nНе обрезанное, без обработки, без посторонних теней и частей тела, соотношение сторон, как А3/А4.\n\nПосле загрузки нажмите «Подтвердить».\n\nЕсли нужно, прикрепите ещё рисунки.",
                 keyboard=get_drawings_keyboard())


# ============================================================
#  РИСУНКИ
# ============================================================

def process_drawings(user_id, attachments, text=None):
    current_state = user_state.get(user_id)
    if current_state != STATE_DRAWINGS:
        logger.warning(
            "process_drawings: state mismatch (user=%s, state=%s) — "
            "игнорирую %d вложений",
            user_id, current_state, len(attachments or []))
        return

    photo_meta = extract_photo_meta(attachments)
    photo_urls = [meta["url"] for meta in photo_meta]
    if photo_urls:
        temp = dict(user_state.get_temp(user_id))
        temp.setdefault("drawings_urls", [])
        temp["drawings_urls"] = list(temp["drawings_urls"])

        room = MAX_PHOTOS - len(temp["drawings_urls"])

        if room <= 0:
            send_message(user_id,
                f"У вас уже {len(temp['drawings_urls'])} рисунков "
                f"(лимит {MAX_PHOTOS}). Переходим к следующему шагу.",
                keyboard=get_empty_keyboard())
            process_drawings_confirm(user_id)
            return

        truncated = len(photo_meta) > room
        skipped = max(0, len(photo_meta) - room)
        if truncated:
            photo_meta = photo_meta[:room]
            photo_urls = [meta["url"] for meta in photo_meta]

        for meta in photo_meta:
            url = meta["url"]
            temp["drawings_urls"].append(url)
            try:
                media_id = register_media_file(
                    url=url,
                    owner_id=meta.get("owner_id"),
                    photo_id=meta.get("photo_id"),
                    kind="photo",
                    user_platform=platform(),
                    user_id=user_id,
                    source_file_id=meta.get("source_file_id"),
                    source_platform=meta.get("source_platform"),
                    vk_url=meta.get("vk_url"),
                    vk_owner_id=meta.get("vk_owner_id"),
                    vk_photo_id=meta.get("vk_photo_id"),
                    vk_doc_id=meta.get("vk_doc_id"),
                )
                if media_id and meta.get("vk_url"):
                    try:
                        from core.media import cache_media_locally
                        from core.db import update_media_file_local_url
                        local_url = cache_media_locally(
                            meta["vk_url"], media_id)
                        if local_url:
                            update_media_file_local_url(media_id, local_url)
                    except Exception as e:
                        logger.warning(
                            "process_drawings: cache media failed "
                            "(id=%s): %s", media_id, e)
            except Exception as e:
                logger.warning("media_file register failed: %s", e)

        if photo_meta:
            loaded_total = len(temp["drawings_urls"])
            n = len(photo_meta)
            if n <= 10:
                labels = ", ".join(f"Рисунок {i}" for i in range(1, n + 1))
            else:
                labels = f"Рисунок 1 … Рисунок {n}"
            notify_admin_applications(
                f"🖼 {admin_user_tag(user_id)} загрузил "
                f"{n} рисунков (всего: {loaded_total}).\n\n"
                f"Вложения: {labels}\n"
                f"Рисунки — в карточке заявки (открыть заявку)"
            )

        user_state.set_temp(user_id, temp)
        loaded = len(temp["drawings_urls"])

        if loaded >= MAX_PHOTOS:
            if truncated:
                send_message(user_id,
                    f"Принято {room} рисунков из {room + skipped} — "
                    f"лимит {MAX_PHOTOS} достигнут.\n\n"
                    "Переходим к следующему шагу.",
                    keyboard=get_empty_keyboard())
            else:
                send_message(user_id,
                    f"Загружено {loaded} рисунков — лимит {MAX_PHOTOS} достигнут.\n\n"
                    "Переходим к следующему шагу.",
                    keyboard=get_empty_keyboard())
            user_state.set(user_id, STATE_TRAINING)
            send_message(user_id,
                "Где и у кого ребенок обучается рисованию?\n\nНапример: Дома или ЦДР Радуга, педагог Петрова Анна Павловна",
                keyboard=get_empty_keyboard())
            return

        if truncated:
            send_message(user_id,
                f"Принято {room} рисунков из {room + skipped} — лимит {MAX_PHOTOS}. "
                f"Всего загружено: {loaded}.\n\n"
                "Когда все готовы, нажмите кнопку «Подтвердить».",
                keyboard=get_drawings_keyboard(loaded=loaded))
        else:
            send_message(user_id,
                "Фото принято. Если нужно, прикрепите ещё рисунки.\n"
                "Когда все готовы, нажмите кнопку «Подтвердить».",
                keyboard=get_drawings_keyboard(loaded=loaded))
        return

    if attachments:
        wrong = get_non_photo_types(attachments)
        if wrong:
            send_message(user_id,
                f"Прикрепите Фото, а не {format_wrong_types(wrong)}.\n\n"
                "Нужны фото или сканы рисунков ребёнка.",
                keyboard=get_drawings_keyboard())
            return
    if text and text.strip():
        handle_off_script(user_id, text, STATE_DRAWINGS)
        return

    logger.debug(
        "process_drawings: пустое событие (user=%s, attachments=%d) — игнорирую",
        user_id, len(attachments or []),
    )


def process_drawings_confirm(user_id):
    if not user_state.get_temp(user_id).get("drawings_urls"):
        send_message(user_id, "Необходимо прикрепить хотя бы один рисунок!\n\nПожалуйста, прикрепите фото или скан рисунка, а затем нажмите «Подтвердить».",
                     keyboard=get_drawings_keyboard())
        return
    user_state.set(user_id, STATE_TRAINING)
    send_message(user_id, "Где и у кого ребенок обучается рисованию?\n\nНапример: Дома или ЦДР Радуга, педагог Петрова Анна Павловна",
                 keyboard=get_empty_keyboard())


# ============================================================
#  ОБУЧЕНИЕ
# ============================================================

def process_training(user_id, text):
    text_stripped = text.strip()
    if not text_stripped or len(text_stripped) < 2:
        handle_off_script(user_id, text, STATE_TRAINING)
        return
    if text_stripped.startswith("/"):
        send_message(user_id,
            "ℹ️ Похоже, вы ввели команду.\n\n"
            "Напишите, где и у кого ребёнок обучается рисованию.\n\n"
            "Например: Дома или ЦДР Радуга, педагог Петрова Анна Павловна")
        return
    if len(text) > MAX_TEXT_LENGTH:
        text = text[:MAX_TEXT_LENGTH]
    text_stripped = text.strip()
    save_message_to_db(user_id, "in", text_stripped)
    user_state.update_temp(user_id, {"training_info": text_stripped})
    user_state.set(user_id, STATE_PAYMENT)
    notify_admin_applications(f"🎓 {admin_user_tag(user_id)} указал, где обучается: {text.strip()[:500]}")
    send_message(user_id, "1. Переведите пожертвование на реализацию проекта, нажмите ЗДЕСЬ: https://www.yarkijgorod.ru/pay\n\n👇\n\n2. Пришлите в этот чат квитанцию о переводе / справку об инвалидности ребенка или скрин переписки о других договоренностях с руководителем проекта.")


# ============================================================
#  КВИТАНЦИЯ
# ============================================================

def _register_payment_media(user_id, attachments):
    """Синхронно регистрирует медиа в media_files. Быстро, без IO в сеть.

    Возвращает список (media_id, vk_url) для фонового кэширования.
    Регистрация в БД выполняется всегда, даже если VK-релей не удался —
    в этом случае vk_url будет пустым и в кэш не попадёт.
    """
    media_entries = []
    try:
        for meta in extract_photo_meta(attachments):
            media_id = register_media_file(
                url=meta["url"],
                owner_id=meta.get("owner_id"),
                photo_id=meta.get("photo_id"),
                kind="payment_photo",
                user_platform=platform(),
                user_id=user_id,
                source_file_id=meta.get("source_file_id"),
                source_platform=meta.get("source_platform"),
                vk_url=meta.get("vk_url"),
                vk_owner_id=meta.get("vk_owner_id"),
                vk_photo_id=meta.get("vk_photo_id"),
                vk_doc_id=meta.get("vk_doc_id"),
            )
            if media_id and meta.get("vk_url"):
                media_entries.append((media_id, meta["vk_url"]))

        for meta in extract_doc_meta(attachments):
            media_id = register_media_file(
                url=meta["url"],
                owner_id=meta.get("owner_id"),
                photo_id=None,
                doc_id=meta.get("doc_id"),
                kind="payment_doc",
                user_platform=platform(),
                user_id=user_id,
                source_file_id=meta.get("source_file_id"),
                source_platform=meta.get("source_platform"),
                vk_url=meta.get("vk_url"),
                vk_owner_id=meta.get("vk_owner_id"),
                vk_photo_id=None,
                vk_doc_id=meta.get("vk_doc_id"),
            )
            if media_id and meta.get("vk_url"):
                media_entries.append((media_id, meta["vk_url"]))
    except Exception as e:
        logger.warning("media_file register (payment) failed: %s", e)
    return media_entries


def _fire_and_forget_payment_cache(user_id, media_entries):
    """Асинхронное кэширование квитанций.

    Раньше cache_media_locally вызывался СИНХРОННО и при 404 от VK CDN
    блокировал state machine на 3-12 секунд. Теперь кэш уходит в фон,
    а пользователь сразу видит «Квитанция принята».
    """
    if not media_entries:
        return
    from core.utils import _fire_and_forget

    async def _cache_all():
        for media_id, vk_url in media_entries:
            try:
                from core.media import cache_media_locally
                from core.db import update_media_file_local_url
                local_url = await asyncio.to_thread(
                    cache_media_locally, vk_url, media_id, 2)
                if local_url:
                    await asyncio.to_thread(
                        update_media_file_local_url, media_id, local_url)
                    logger.info(
                        "payment cache OK: media_id=%s url=%s",
                        media_id, local_url)
            except Exception as e:
                logger.warning(
                    "payment cache failed (id=%s): %s", media_id, e)

    _fire_and_forget(_cache_all(), label=f"payment_cache user={user_id}")


def process_payment(user_id, attachments, text=None):
    """Приём квитанции/справки/скрина переписки.

    ФИКС: раньше cache_media_locally вызывался синхронно и при 404 от
    VK CDN висел 3-12 секунд. Теперь:
      * регистрируем медиа в БД сразу (быстро, ~1 ms);
      * кэширование уходит в фон;
      * state machine завершается за <1 сек;
      * PDF (doc) принимается независимо от того, пришёл ли он как
        type=doc или type=image (см. normalize_attachments в max_adapter).
    """
    if attachments:
        docs = [a for a in attachments if a.get("type") in ("photo", "doc")]
        if docs:
            payment_doc_urls = []

            for doc in docs:
                if doc.get("type") == "photo":
                    photo_info = doc.get("photo", {})
                    photo_size = 0
                    for s in photo_info.get("sizes", []):
                        w, h = s.get("width", 0), s.get("height", 0)
                        if w * h > 0:
                            photo_size = max(photo_size, w * h)
                    if photo_size > 4000 * 4000:
                        send_message(user_id,
                                     "⚠️ Фото очень большого размера. "
                                     "Рекомендуем отправить квитанцию файлом "
                                     "меньшего размера.")
                    sizes = photo_info.get("sizes", [])
                    if not sizes or "url" not in sizes[-1]:
                        continue
                    url = sizes[-1]["url"]
                else:
                    doc_info = doc.get("doc", {})
                    ext = (doc_info.get("ext") or "").lower().lstrip(".")
                    if ext and ext not in ALLOWED_DOC_EXT:
                        send_message(user_id,
                                     f"Неподдерживаемый тип файла (.{ext}). "
                                     f"Разрешены: PDF, JPG, PNG, GIF, BMP, WEBP, "
                                     f"TIFF, DOC, DOCX.")
                        continue
                    doc_size = doc_info.get("size") or 0
                    if doc_size and doc_size > MAX_DOC_SIZE:
                        max_mb = MAX_DOC_SIZE // (1024 * 1024)
                        send_message(user_id,
                                     f"Файл больше {max_mb} МБ. Перешлите файл "
                                     f"меньшего размера, пожалуйста.")
                        continue
                    url = doc_info.get("url", "")

                if url and isinstance(url, str) and url.strip():
                    payment_doc_urls.append(url.strip())

            if payment_doc_urls:
                if text and text.strip():
                    save_message_to_db(user_id, "in", text)
                save_message_to_db(user_id, "in", "[квитанция/документ]")
                user_state.update_temp(user_id, {"payment_doc_urls": payment_doc_urls})
                user_state.set(user_id, STATE_CONFIRM)

                # 1) Регистрация в media_files — синхронно, но быстро.
                media_entries = _register_payment_media(user_id, attachments)

                # 2) Кэширование — в фон, не блокирует state machine.
                if media_entries:
                    _fire_and_forget_payment_cache(user_id, media_entries)

                # 3) Уведомление админам + пользователю.
                n = len(payment_doc_urls)
                labels = (", ".join(f"Квитанция {i}" for i in range(1, n + 1))
                          if n <= 10
                          else f"Квитанция 1 … Квитанция {n}")
                notify_admin_applications(
                    f"💳 {admin_user_tag(user_id)} прислал "
                    f"{n} документ(ов)/квитанций.\n\n"
                    f"Вложения: {labels}\n"
                    f"Документы — в карточке заявки (открыть заявку)"
                )
                send_message(
                    user_id,
                    "✅ Квитанция принята!\n\n"
                    "Нажмите кнопку «Отправить заявку» ниже, чтобы завершить оформление.\n\n"
                    "Нажимая кнопку «Отправить заявку», Вы соглашаетесь c политикой "
                    "конфиденциальности (https://yarkijgorod.ru/politika), офертой "
                    "(https://yarkijgorod.ru/oferta) и даете согласие на обработку "
                    "персональных данных и публикацию данных несовершеннолетнего "
                    "(https://yarkijgorod.ru/consent).\n\n"
                    "Ознакомьтесь с текстом согласия и нажмите «✅ Согласен».",
                    keyboard=get_consent_keyboard(),
                )
                return

    if text and text.strip():
        save_message_to_db(user_id, "in", text)
        handle_off_script(user_id, text, STATE_PAYMENT)
        return
    send_message(user_id,
                 "Пожалуйста, пришлите квитанцию о переводе, справку об "
                 "инвалидности или скрин переписки о других договоренностях.")


# ============================================================
#  ПОДТВЕРЖДЕНИЕ ЗАЯВКИ
# ============================================================

def process_confirm(user_id):
    data = dict(user_state.get_temp(user_id))
    if not data.get("consent_accepted"):
        send_message(user_id,
            "Пожалуйста, сначала ознакомьтесь с текстом согласия и "
            "нажмите «✅ Согласен».",
            keyboard=get_consent_keyboard())
        return
    data["user_id"] = user_id
    if not data.get("client_request_id"):
        import uuid
        data["client_request_id"] = uuid.uuid4().hex
        user_state.update_temp(user_id, {"client_request_id": data["client_request_id"]})

    if not data.get("client_request_id"):
        import uuid
        data["client_request_id"] = uuid.uuid4().hex

    allowed, wait = check_rate_limit(user_id, scope="confirm")
    if not allowed:
        send_message(user_id, f"⏳ Подождите {wait} сек. перед отправкой заявки.")
        return

    import hashlib
    from datetime import datetime, timezone
    data["consent_version"] = CONSENT_VERSION
    data["consent_text_hash"] = hashlib.sha256(
        CONSENT_TEXT.encode("utf-8")).hexdigest()[:64]
    data["consent_at"] = datetime.now(timezone.utc)
    data["consent_platform"] = platform()

    org_info = data.get("org_info_text", "")
    training_info = data.get("training_info", "")
    combined_training_info = training_info
    if org_info and org_info.strip():
        if training_info and training_info.strip():
            combined_training_info = f"О себе: {org_info.strip()}\nОбучение: {training_info.strip()}"
        else:
            combined_training_info = f"О себе: {org_info.strip()}"
    save_data = dict(data)
    save_data["training_info"] = combined_training_info
    save_data["org_info_attachment_urls"] = data.get("org_info_attachment_urls", [])
    success, app_id = save_application_to_db(save_data)
    if not success:
        send_message(user_id, "❌ Произошла ошибка при сохранении заявки.\n\nНажмите кнопку «Отправить заявку» ещё раз, чтобы повторить попытку.\nЕсли ошибка повторяется — обратитесь к администратору.",
                     keyboard=get_confirm_keyboard())
        return
    admin_msg = ("🆕 Новая заявка в «Яркий!Город»!\n\n"
        f"Город: {data.get('location', 'Не указано')}\n"
        f"Локация размещения: {data.get('placement_location', 'Не указано')}\n"
        f"Тип пользователя: {data.get('user_type', 'Не указано')}\n"
        f"Телефон: {data.get('phone', 'Не указано')}\n"
        f"ФИО и возраст ребёнка: {data.get('name_age', 'Не указано')}\n"
        f"Формат: {data.get('format_choice', 'Не указано')}\n"
        f"О себе: {org_info or 'Не указано'}\n"
        f"Обучение: {training_info or 'Не указано'}\n"
        f"🔗 Диалог: {_dialog_link(user_id)}")
    notify_admin_applications(admin_msg)
    user_state.set_temp(user_id, {})
    user_state.set(user_id, STATE_NEXT_ACTION)
    send_message(user_id,
        "Заявка принята в обработку!\n\nХотите оформить ещё одну заявку или завершить работу с ботом?",
        keyboard=get_after_submit_inline_keyboard())


# ============================================================
#  ВОПРОСЫ
# ============================================================

def start_question_mode(user_id):
    current_state = user_state.get(user_id)
    if current_state != STATE_QUESTION:
        user_state.update_temp(user_id, {"return_to_state": current_state})
    user_state.set(user_id, STATE_QUESTION)
    send_message(user_id,
        "Вы перешли в режим вопросов. Напишите всё, что хотите узнать — администратор ответит вам в этом чате.\n\n"
        "Чтобы оформить заявку, нажмите кнопку ниже.",
        get_question_keyboard())


def process_question(user_id, text):
    if not text or not text.strip():
        send_message(user_id, "Напишите ваш вопрос текстом.")
        return
    if len(text) > MAX_TEXT_LENGTH:
        text = text[:MAX_TEXT_LENGTH]
    forward_to_admin(user_id, text, STATE_QUESTION)
    send_message(user_id,
        "Ваше сообщение передано администратору. Ответ поступит в этот чат.",
        get_question_keyboard())


# ============================================================
#  EXTRAS / ФИНАЛ
# ============================================================

def send_extras_message(user_id):
    pl = platform()
    from core.runtime import get_adapter
    from core.utils import _run_async
    from core.texts import EXTRAS_PHOTO_URLS

    adapter = get_adapter()
    sent = False

    def _norm_ids(raw):
        if not raw:
            return []
        if isinstance(raw, str):
            raw = [s.strip() for s in raw.split(",") if s.strip()]
        return [
            x for x in raw
            if isinstance(x, str) and x
            and not x.startswith(("http://", "https://"))
        ]

    def _try_send(label, items):
        nonlocal sent
        if not items or sent:
            return
        try:
            ok = _run_async(adapter.send_attachment(
                user_id, list(items), kind="photo",
            ))
            if ok:
                sent = True
                logger.info(
                    "extras: %s sent (user=%s, count=%d)",
                    label, user_id, len(items))
            else:
                logger.info(
                    "extras: %s not delivered "
                    "(user=%s, count=%d) — fallback",
                    label, user_id, len(items))
        except Exception as e:
            logger.warning(
                "send_extras_message: %s failed (user=%s): %s — fallback",
                label, user_id, e)

    if pl == "tg":
        _try_send("tg file_id", _norm_ids(EXTRAS_TG_FILE_IDS))
    elif pl == "vk":
        _try_send("vk photo", _norm_ids(EXTRAS_PHOTO_IDS))

    if pl == "max" and not sent:
        try:
            tokens = _run_async(adapter._ensure_extras_tokens())
        except Exception as e:
            logger.warning(
                "send_extras_message: extras tokens failed: %s", e)
            tokens = []
        _try_send("max tokens", tokens)

    if not sent and EXTRAS_PHOTO_URLS:
        _try_send("urls", list(EXTRAS_PHOTO_URLS))

    if not sent and not EXTRAS_PHOTO_URLS:
        if pl == "tg":
            _try_send("legacy tg", _norm_ids(EXTRAS_TG_FILE_IDS))
        elif pl == "max":
            legacy = EXTRAS_MAX_FILE_IDS
            if isinstance(legacy, str):
                legacy = [s.strip() for s in legacy.split(",") if s.strip()]
            _try_send("legacy max", legacy)
        else:
            _try_send("legacy vk", _norm_ids(EXTRAS_PHOTO_IDS))

    send_message(user_id, EXTRAS_TEXT, keyboard=get_extras_keyboard())


def skip_extras(user_id):
    send_final_message(user_id)


def send_final_message(user_id):
    user_state.set(user_id, STATE_START)
    send_message(user_id,
        "Поздравляем!\n\nЕсли вы на этом экране, значит данные точно приняты, всё в порядке, "
        "мы обработаем их в ближайшее время.\n\nЕСЛИ по ним будут вопросы, неподходящее качество рисунка, "
        "размер, тематика или что-то ещё — мы свяжемся с вами через этот чат.\n\n"
        "После обработки заявки (в течение 3-х недель) пришлём вам сюда изображение баннера с рисунком "
        "перед отправкой на печать. Проверьте информацию, так как после печати её изменить будет невозможно.\n\n"
        "Отчёты о монтажах и вся организационная информация, а также информация о конкурсах и отзывы "
        "мы публикуем в наших группах, поэтому важно подписаться на:\n"
        "- группу ВКонтакте: https://vk.com/yarkijgorod\n"
        "- Телеграм-канал: https://t.me/yarkijgorod\n"
        "- канал в Макс: https://max.ru/гjoin/w_jGlxi33X5yXwbXYSkucFH6RMcXlvZ7UAdnwFrn_MI\n"
        "- канал ВК: https://vk.ru/im/channels/-239465585\n\nПодписывайтесь.\n\n"
        "Дата монтажа может измениться на несколько дней, так как монтируем сразу в нескольких городах "
        "и всё очень сильно зависит от погодных условий и занятости добровольцев. "
        "Монтажи производим в течение месяца после завершения приёма рисунков.\n\n"
        "Будем рады, если вы поможете с монтажом на локации — нам всегда нужны добровольцы!\n\n"
        "Диплом об участии вы можете скачать по ссылке: https://yarkijgorod.ru/diplom-zayavka",
        keyboard=get_final_inline_keyboard())


# ============================================================
#  ПРИМЕР ФОТО РЕБЁНКА (STATE_CHILD_PHOTO)
# ============================================================

import logging as _logging


def _send_child_photo_example(user_id: int) -> None:
    """Отправляет пример фото ребёнка (если настроен URL).

    Fire-and-forget: не блокирует обработку события, ошибки только
    логируются. Если URL не задан — тихо выходим.
    """
    from core.texts import (
        CHILD_PHOTO_EXAMPLE_URL,
        CHILD_PHOTO_EXAMPLE_CAPTION,
    )
    from core.runtime import get_adapter
    from core.utils import _fire_and_forget

    if not CHILD_PHOTO_EXAMPLE_URL:
        return

    adapter = get_adapter()

    async def _send():
        try:
            ok = await adapter.send_attachment(
                user_id,
                CHILD_PHOTO_EXAMPLE_URL,
                kind="photo",
                caption=CHILD_PHOTO_EXAMPLE_CAPTION,
                keep=True,
            )
            if ok:
                logger.info("child photo example sent to user=%s", user_id)
            else:
                logger.warning(
                    "child photo example rejected (user=%s)", user_id)
        except Exception as e:
            logger.warning(
                "child photo example failed (user=%s): %s", user_id, e)

    _fire_and_forget(_send(), label=f"child_photo_example user={user_id}")