# core/texts.py
"""Все текстовые константы, метки статусов, тексты сообщений, changelog.

Здесь нет логики — только данные, которые использует остальной код.
"""
import os

# --- ВЕРСИЯ ---
BOT_VERSION = "2.1.0"

CHANGELOG_SHORT = [
    {
        "version": "2.1.0",
        "date": "2026-10-14",
        "added": [
            "Кнопка «📥 Запросы на удаление ПДн» в админ-меню — "
            "появляется только при наличии необработанных запросов",
            "Inline-кнопки 🗑 / 🧊 в списке GDPR-запросов: "
            "удаление / анонимизация в один тап через sudo",
            "Пересылка ответа пользователя админу из свободного чата "
            "(состояние STATE_START)",
        ],
        "changed": [
            "Полностью переписан текст админ-панели: секции, примеры, "
            "явные предупреждения про обязательную платформу в /бан "
            "и /разбан",
        ],
        "fixed": [
            "SSRF: VK-адаптер проверяет URL relay-фото через allow-list "
            "хостов и запрет приватных IP",
            "GDPR-запрос от администратора больше не теряется "
            "(убрано условие not is_admin)",
            "GDPR-стёртые пользователи автоматически реактивируются "
            "при первом обращении вместо вечного сброса состояния",
            "Reply-метки админских кнопок восстанавливаются после "
            "перезапуска TG-бота",
            "Fallback в STATE_ADMIN больше не залипает без клавиатуры",
            "callback_data sudo-кнопок укладывается в 64-байтовый лимит "
            "Telegram (короткие action'ы sudo_ok / sudo_no)",
            "Медиа-очередь GDPR больше не висит вечно: "
            "permanent-auth ошибки ([200]/[15]/[27]) считаются "
            "подтверждённым удалением",
            "main.py: убран несуществующий импорт get_admins_for_notify "
            "из core.users",
        ],
        "security": [
            "Relay VK-фото: защита от SSRF через проверку allow-list "
            "и DNS-резолвинг с запретом приватных адресов",
            "GDPR-удаление: mark_media_delete_flood теперь инкрементирует "
            "vk_delete_attempts — записи с постоянными ошибками выбывают "
            "из очереди, а не висят бесконечно",
        ],
    },
    {
        "version": "2.0.0",
        "date": "2026-09-22",
        "added": [
            "Мультиплатформенное ядро: MessengerAdapter + VkAdapter + TgAdapter",
            "core/runtime, core/db, core/schema, core/media, core/gdpr",
            "core/notify, core/users, core/admin, core/registration",
            "core/state_machine — единая точка обработки событий",
            "core/texts, core/keyboards, core/storage, core/workers",
            "bots/run_vk.py, bots/run_tg.py — тонкие точки входа",
            "outbox_messages — межпроцессный обмен между VK и TG",
            "Bootstrap-админы как набор неизменяемых ID",
            "full_photo_relay_worker — запоздалый relay оригиналов TG→VK",
        ],
        "changed": [
            "Все таблицы с колонкой platform; составные PRIMARY KEY (platform, user_id)",
            "VK-логика полностью вынесена из main.py в core/*",
        ],
        "fixed": [
            "TG send_attachment URL: скачивание VK CDN самим ботом",
            "VK docs upload: peer_id = -community_id, отдельный docs.* эндпоинт",
            "get_user_name для чужой платформы → '(ID N из TG)' вместо DELETED",
            "/пользователь: показывает заявки всех платформ",
        ],
        "security": [],
    },
]


# --- ЛИМИТЫ ---
# Единый источник — core/limits.py: адаптеры импортируют его напрямую,
# чтобы проверять вложения ДО выгрузки в VK.
from core.limits import (  # noqa: F401
    MAX_PHOTOS, MAX_BANNER_SIZE, MAX_DOC_SIZE, MAX_TEXT_LENGTH,
    ALLOWED_DOC_EXT, MAX_ATTACHMENTS, MAX_PAYLOAD_LEN, EVENT_RATE_LIMIT,
)


# --- СОГЛАСИЕ НА ОБРАБОТКУ ПДн (152-ФЗ) ---
# ФИКС P1.3.
# ВАЖНО: текст согласовать с юристом перед продакшеном.
# Версия фиксируется в БД вместе с заявкой (consent_version), чтобы
# в будущем можно было доказать, на какую редакцию согласился субъект.
# ВАЖНО: при изменении текста ОБЯЗАТЕЛЬНО поднимите CONSENT_VERSION
# (ниже). В БД сохраняется SHA-256 от CONSENT_TEXT, чтобы можно было
# доказать, на какую редакцию согласился субъект (152-ФЗ, ст. 9).
CONSENT_VERSION = "1.1"

CONSENT_TEXT = (
    "📋 Согласие на обработку персональных данных\n\n"
    "Нажимая кнопку «Согласен», я подтверждаю, что:\n\n"
    "1. Даю согласие на обработку персональных данных (моих и моего "
    "несовершеннолетнего ребёнка) в целях участия в проекте "
    "«Яркий!Город».\n\n"
    "2. Согласен на публикацию данных несовершеннолетнего "
    "(имя, возраст, фотография, рисунок) на баннере и в "
    "информационных материалах проекта.\n\n"
    "3. Подтверждаю, что имею право давать такое согласие "
    "(как законный представитель).\n\n"
    "4. Согласен с политикой конфиденциальности: "
    "https://yarkijgorod.ru/politika "
    "и офертой: https://yarkijgorod.ru/oferta\n\n"
    "5. Понимаю, что мои данные, отправленные через чат-бот, "
    "становятся доступны операторам проекта (администраторам) "
    "в их личных чатах в мессенджерах VK / Telegram. Эти копии "
    "используются исключительно для обработки заявки и не "
    "передаются третьим лицам.\n\n"
    "Согласие можно отозвать в любой момент, отправив команду "
    "«удали мои данные» в чат бота."
)


# --- STATES ---
STATE_START = "start"
STATE_LOCATION = "location"
STATE_PLACEMENT_LOCATION = "placement_location"
STATE_USER_TYPE = "user_type"
STATE_ORG_PHONE = "org_phone"
STATE_ORG_INFO = "org_info"
STATE_NAME_AGE = "name_age"
STATE_CHILD_PHOTO = "child_photo"
STATE_FORMAT = "format"
STATE_DRAWINGS = "drawings"
STATE_TRAINING = "training"
STATE_PAYMENT = "payment"
STATE_CONFIRM = "confirm"
STATE_EXTRAS = "extras"
STATE_NEXT_ACTION = "next_action"
STATE_FINAL = "final"
STATE_QUESTION = "question"
STATE_ADMIN = "admin"
STATE_ADMIN_SEND_BANNER = "admin_send_banner"
STATE_ADMIN_REPLY = "admin_reply"
STATE_ADMIN_REJECT_REASON = "admin_reject_reason"

REGISTRATION_STATES = {
    STATE_LOCATION, STATE_PLACEMENT_LOCATION, STATE_USER_TYPE,
    STATE_ORG_PHONE, STATE_ORG_INFO, STATE_NAME_AGE,
    STATE_CHILD_PHOTO, STATE_FORMAT, STATE_DRAWINGS,
    STATE_TRAINING, STATE_PAYMENT, STATE_CONFIRM,
}

STEP_LABELS = {
    STATE_START: "Стартовый экран",
    STATE_LOCATION: "Выбор города",
    STATE_PLACEMENT_LOCATION: "Выбор локации размещения",
    STATE_USER_TYPE: "Выбор категории пользователя",
    STATE_ORG_PHONE: "Авторизация номером телефона",
    STATE_ORG_INFO: "Информация о себе",
    STATE_NAME_AGE: "ФИО и возраст ребёнка",
    STATE_CHILD_PHOTO: "Фото ребёнка для баннера",
    STATE_FORMAT: "Выбор формата рисунка",
    STATE_DRAWINGS: "Загрузка фото рисунков",
    STATE_TRAINING: "Где обучается рисованию",
    STATE_PAYMENT: "Перевод и квитанция",
    STATE_CONFIRM: "Подтверждение заявки",
    STATE_EXTRAS: "Дополнительные возможности",
    STATE_NEXT_ACTION: "Следующее действие",
    STATE_FINAL: "Финальное сообщение",
    STATE_QUESTION: "Режим вопросов",
    STATE_ADMIN: "Админ-панель",
    STATE_ADMIN_SEND_BANNER: "Админ: отправка макета",
    STATE_ADMIN_REPLY: "Админ: ответ пользователю",
    STATE_ADMIN_REJECT_REASON: "Админ: причина отклонения",
}

STATUS_LABELS = {
    "new": "🆕 Новая",
    "in_progress": "🔄 В обработке",
    "rejected": "❌ Отклонена",
    "done": "🏁 Завершена",
}
STATUS_VALUES = {
    "новая": "new", "в обработке": "in_progress",
    "отклонена": "rejected", "завершена": "done",
}
CATEGORY_LABELS = {
    "incomplete": "📝 Не завершённые",
    "new": "🆕 Новые",
    "in_progress": "🔄 На обработке",
    "rejected": "❌ Отклонены",
    "waiting_banner": "⏳ Ждут макет",
    "completed": "✅ Выполненные",
}


def get_status_label(status):
    return STATUS_LABELS.get(status, status or "—")


def get_display_status(status, banner_url=None):
    if status == "done":
        return "🏁 Завершена, макет отправлен" if banner_url else "⏳ Ожидает отправки макета"
    return get_status_label(status)


# --- EXTRAS ---
EXTRAS_PHOTO_IDS = [
    "photo-241529035_457239027",
    "photo-241529035_457239029",
    "photo-241529035_457239028",
]

EXTRAS_TG_FILE_IDS = [
    "AgACAgIAAxkBAAIJ22q2hn-G8zbLJNnvmjLXZtmLAAGi-QACgSVrG_gssUlwAsy9jNLSTAEAAwIAA3kAAz0E",
    "AgACAgIAAxkBAAIDKGqz7-QufoM35tOJwUgrbWtlvHRDAALHI2sbNuWgSar4qSSDPdcGAQADAgADeAADPQQ",
    "AgACAgIAAxkBAAIDNWq0Hf_tG9aT9BoKXVw6DUjHDb-dAAIQJWsbNuWgSSZcrMOdbYuKAQADAgADeAADPQQ",
]

EXTRAS_MAX_FILE_IDS = [
    "https://sun9-38.vkuserphoto.ru/s/v1/ig2/yfnH495mpSpgmjrtynJs61ce-pVGnqF3L8Qo4XvPrxUjKIExv8g3Rvd8DnPTJIITxLMwSwbgfZcvX9pDAG3h4U59.jpg?quality=95&as=32x32,48x48,72x72,108x108,160x160,240x240,360x360,480x480,540x540,640x640,720x720,1080x1080,1280x1280&from=bu&cs=1280x0",
    "https://sun9-46.vkuserphoto.ru/s/v1/ig2/GYPm7m_z_GmoVHEQTGfzlUBbmH9XoJSYJVsIkStstWSLdDajVIwd9RQtAvSKARG_hGr98VUYWp1pngTzw-BpC3NT.jpg?quality=95&as=32x56,48x85,72x127,108x191,160x282,240x424,360x635,480x847,510x900&from=bu&cs=510x0",
    "https://sun9-51.vkuserphoto.ru/s/v1/ig2/S0WVHsukdxASkOTy0T3Qs30uVqc20hvu8D9PqG5i9TMYCxQrY_NJXSDMTN0c1-w1ExgDFeteoM3lvR7WcI5E5P-O.jpg?quality=95&as=32x29,48x44,72x66,108x99,160x146,240x220,360x330,480x439,540x494,580x531&from=bu&cs=580x0",
]

EXTRAS_TEXT = """Дополнительные возможности:

6 - Авторская выставка вашего ребенка (индивидуальные условия)

5 - Монтаж к определенной дате (индивидуальные условия)

4 - Заказать индивидуальное или групповое награждение на локации с вашими рисунками (индивидуальные условия)

3 - принять участие в торжественном вручении дипломов участников (когда известна дата праздника, публикуем информацию в соц сетях)

2 - получить значок с рисунком вашего ребёнка
В подарок, при публикации поста-отзыва с активной ссылкой на проект, ссылку пришлите сюда. Или пожертвование от 550Р.
Доставка за счет получателя. Если вы хотите отправку доставкой за счет получателя на пишите адрес, способ доставки, в других случаях следите самостоятельно за информацией в телеграм и забирайте значки самостоятельно на наших мероприятиях. Значки изготавливаются при наборе определенного количества заявок.

1. Если вы приходите на монтаж и помогаете монтировать баннера, то забираете свой баннер с собой.
Или делаете пожертвование от 300р, мы снимаем баннер и передаем его добровольцам, у которых вы можете его забрать или отправляем доставкой за счет получателя

Напишите словами или цифрами, что вас заинтересовало и способ получения, если у нас будет возможность это предоставить, то мы учтем ваши пожелания.
Эти пункты не являются обязательными."""


# --- ЛОКАЦИИ ---
PLACEMENT_LOCATIONS = [
    "ул. Восточно-Кругликовская", "ул. Сормовская, 179/1, напротив храма", "Сормовская, 33",
    "ул. Дзержинского, 207, ограждение ДС 107", "ул. Уральская, 35 (угол с Волжской)",
    "ул. Кубанская набережная, между Мира и Орджоникидзе", "ул. Кожевенная, 76",
    "Кореновская, 21 (Прикубанский округ)", "ГМР, ограждение рынка на Невкипелова",
    "ЧМР, ул. Ставропольская между домами 105 и 97", "парк Солнечный остров ул. Трамвайная 2/6",
    "КМР, угол ул. Уральской и Тюляева", "ТЭЦ (ограждение по ул. Трамвайная 4-6)",
]
PLACEMENT_LOCATIONS_SHORT = [
    "ул. Восточно-Кругликовская", "ул. Сормовская, 179/1, напротив храма", "Сормовская, 33",
    "ул. Дзержинского, 207, ограждение ДС 107", "ул. Уральская, 35 (угол с Волжской)",
    "Кубанская набережная (Мира/Орджоникидзе)", "ул. Кожевенная, 76",
    "Кореновская, 21 (Прикубанский округ)", "ГМР, ограждение рынка на Невкипелова",
    "ЧМР, Ставропольская м/у домами 105 и 97", "парк Солнечный остров ул. Трамвайная 2/6",
    "КМР, угол ул. Уральской и Тюляева", "ТЭЦ (ограждение по ул. Трамвайная 4-6)",
]
PLACEMENT_PAGE_SIZE = 8

USER_TYPES = [
    "Я родитель",
    "Я представитель организации (школа, детский центр и т.д.)",
    "Я педагог (без организации)",
]
USER_TYPES_WITH_ORG_INFO = [
    "Я представитель организации (школа, детский центр и т.д.)",
    "Я педагог (без организации)",
]
USER_TYPE_LABELS = {
    "Я родитель": "Я родитель",
    "Я представитель организации (школа, детский центр и т.д.)": "Я представитель организации",
    "Я педагог (без организации)": "Я педагог (без организации)",
}

# Значения кнопок select_format (core/keyboards.py:114-116) — нужны для
# валидации payload по белому списку.
FORMAT_LABELS = {
    "Горизонтальный или квадратный": "Горизонтальный или квадратный",
    "1 вертикальный рисунок": "1 вертикальный рисунок",
    "2 вертикальных рисунка": "2 вертикальных рисунка",
}


# --- ATTACHMENT LABELS ---
ATTACHMENT_LABELS = {
    "video": "Видео", "audio": "Музыку", "doc": "Файл", "link": "Ссылку",
    "graffiti": "Граффити", "sticker": "Стикер", "wall": "Запись со стены",
    "market": "Товар", "poll": "Опрос", "map": "Карту",
    "contact": "Контакт", "story": "Историю",
}


# --- GDPR ---
_GDPR_KEYWORDS_DELETE = (
    "удали мои данные", "удалить мои данные", "удалите мои данные",
    "удали моих данных", "удалить моих данных", "удалите моих данных",
    "удали персональные данные", "удалить персональные данные",
    "удалите персональные данные",
    "стереть мои данные", "стереть моих данных",
    "стереть персональные данные",
    "отозвать согласие", "отзываю согласие", "отозвал согласие",
)


def looks_like_gdpr_request(text: str) -> bool:
    t = (text or "").lower()
    if not t:
        return False
    return any(k in t for k in _GDPR_KEYWORDS_DELETE)


# --- ADMIN IDs ---
_ADMIN_IDS_STR = os.getenv("ADMIN_ID", "")
ADMIN_IDS = [int(x.strip()) for x in _ADMIN_IDS_STR.split(",") if x.strip()]
if not ADMIN_IDS:
    raise RuntimeError("Не задан ADMIN_ID в .env")


# ── Пример фото ребёнка (STATE_CHILD_PHOTO) ─────────────────────────
# Один URL для всех платформ — адаптеры скачивают и пересылают.
# Если пусто — пример не отправляется (fallback: только текст).
CHILD_PHOTO_PROMPT_TEXT = (
    "Загрузите фото вашего ребенка для публикации на баннере.\n\n"
    "Изображение чёткое, не обрезанное, портрет, должно быть видно лицо, "
    "соотношение сторон приближено к квадрату.\n\n"
    "После загрузки нажмите «Подтвердить»."
)

CHILD_PHOTO_EXAMPLE_CAPTION = "📸 Пример правильного фото"

CHILD_PHOTO_EXAMPLE_URL = os.getenv("CHILD_PHOTO_EXAMPLE_URL", "").strip()

# ── EXTRAS: локальные URL (приоритет над платформенными ID) ────────
# Файлы лежат в web_app/media_cache/, отдаются через /media/raw/{id}?sig=.
# Эти URL стабильны (HMAC, без TTL) — не зависят от VK/MAX CDN.
# Используются во всех платформах как первичный источник; если пусто —
# fallback на EXTRAS_*_IDS ниже (старое поведение).

import os as _os

EXTRAS_PHOTO_URLS = [
    _os.getenv(f"EXTRAS_PHOTO_URL_{i}", "").strip()
    for i in (1, 2, 3)
]
EXTRAS_PHOTO_URLS = [u for u in EXTRAS_PHOTO_URLS if u]