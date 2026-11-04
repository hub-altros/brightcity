# adapters/max_keyboard_helpers.py
"""Конвертация VK-JSON клавиатуры в MAX inline_keyboard + диагностика.

Вынесено из max_adapter.py, чтобы:
  * было видно, ПОЧЕМУ клавиатура не прикрепилась (раньше функция
    молча возвращала None на 4 разных ветках);
  * разбивать широкие строки — MAX плохо рендерит >5 inline-кнопок
    в одной строке;
  * можно было переиспользовать из тестов.

ФИКС: диагностика смотрела на `markup.inline_keyboard`, но
`InlineKeyboardBuilder.as_markup()` в maxapi возвращает
`InlineKeyboardAttachment` (Pydantic-модель) со структурой
`{"type": "inline_keyboard", "payload": {"buttons": [[...]]}}`.
Поля `inline_keyboard` у неё нет, поэтому `getattr(..., "inline_keyboard",
None) or []` всегда давал пустой список, и лог показывал
`max_rows=0 buttons=0 dropped=0` даже при непустой клавиатуре.
Теперь читаем `payload.buttons` через model_dump()/dict().
"""
import json
import logging
from typing import Optional

from maxapi.types.attachments.buttons import (
    CallbackButton, LinkButton, RequestContactButton,
)
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

logger = logging.getLogger("adapters.max_adapter")

# MAX практический лимит inline-кнопок в одной строке — 5.
# Больше — attachment принимается, но клавиатура может не отрисоваться.
MAX_BUTTONS_PER_ROW = 5


def make_max_button(action: dict):
    """VK action → MAX button. Возвращает None, если кнопку нельзя создать."""
    btype = action.get("type")
    label = (action.get("label") or "")[:64]

    if btype == "open_link":
        url = action.get("link") or ""
        if not url:
            logger.warning(
                "MAX button: open_link без link, label=%r", label)
            return None
        return LinkButton(text=label, url=url)

    if btype == "text":
        payload = action.get("payload")
        if isinstance(payload, str):
            callback_data = payload
        elif payload is not None:
            callback_data = json.dumps(payload, ensure_ascii=False)
        else:
            callback_data = label
        if len(callback_data.encode("utf-8")) > 256:
            logger.error(
                "MAX callback payload >256 bytes (%d) — button dropped. "
                "label=%r payload=%r",
                len(callback_data.encode("utf-8")), label,
                callback_data[:80])
            return None
        return CallbackButton(text=label, payload=callback_data)

    if btype == "contact":
        return RequestContactButton(text=label)

    logger.warning(
        "MAX button: неизвестный тип %r (label=%r)", btype, label)
    return None


def _dump_markup(markup) -> dict:
    """model_dump/dict/dict-as-is — что доступно в текущей maxapi.

    Возвращает dict; при любой ошибке — пустой dict (диагностика
    не должна ронять отправку сообщения).
    """
    try:
        if hasattr(markup, "model_dump"):
            return markup.model_dump() or {}
    except Exception as e:
        logger.debug("MAX keyboard: model_dump() failed: %s", e)

    try:
        if hasattr(markup, "dict"):
            return markup.dict() or {}
    except Exception as e:
        logger.debug("MAX keyboard: dict() failed: %s", e)

    if isinstance(markup, dict):
        return markup
    return {}


def _count_buttons_in_payload(dumped: dict) -> tuple[int, int]:
    """Возвращает (rows, buttons) из структуры InlineKeyboardAttachment.

    Структура (maxapi):
        {"type": "inline_keyboard",
         "payload": {"buttons": [[{...}, {...}], [{...}]]}}

    Ищем payload.buttons. Если вдруг maxapi в будущем сменит формат
    и положит buttons прямо в корень — учтём и этот вариант.
    """
    payload = dumped.get("payload") or {}
    buttons = payload.get("buttons")
    if buttons is None:
        # fallback: вдруг формат плоский
        buttons = dumped.get("buttons")
    if not isinstance(buttons, list):
        return 0, 0
    rows = 0
    total = 0
    for row in buttons:
        if not isinstance(row, list):
            continue
        if not row:
            continue
        rows += 1
        total += len(row)
    return rows, total


def vk_json_to_max_keyboard(vk_json: Optional[str]):
    """VK-JSON строка клавиатуры → MAX markup (или None с явным WARNING).

    Диагностирует молчаливые ветки возврата None:
      1) пустой ввод;
      2) битый JSON;
      3) пустой "buttons";
      4) все кнопки отфильтрованы (нет валидных type/label/link);
      5) markup собран, но payload.buttons пуст (редкий случай —
         builder.row() мог не добавить ничего из-за ошибки maxapi).
    """
    if not vk_json:
        logger.debug("MAX keyboard: пустой ввод — клавиатура не нужна")
        return None

    try:
        data = json.loads(vk_json)
    except (json.JSONDecodeError, TypeError) as e:
        logger.warning(
            "MAX keyboard: невалидный JSON (%s): %r",
            e, str(vk_json)[:200])
        return None

    rows = data.get("buttons") or []
    if not rows:
        # Не WARNING: пустые buttons — это норма (get_empty_keyboard).
        # Вызывающий код и так увидит None и не прикрепит клавиатуру.
        logger.debug("MAX keyboard: пустой buttons — клавиатура не нужна")
        return None

    builder = InlineKeyboardBuilder()
    added_rows = 0
    dropped = 0

    for ri, row in enumerate(rows):
        row_buttons = []
        for bi, b in enumerate(row or []):
            if not isinstance(b, dict):
                logger.warning(
                    "MAX keyboard: элемент row=%d col=%d не dict: %r",
                    ri, bi, type(b).__name__)
                dropped += 1
                continue
            action = b.get("action") or {}
            btn = make_max_button(action)
            if btn is None:
                dropped += 1
                continue
            row_buttons.append(btn)

        if not row_buttons:
            logger.warning(
                "MAX keyboard: строка %d пуста после фильтра", ri)
            continue

        # Разбиваем широкие строки — MAX плохо рендерит >5 кнопок подряд.
        for start in range(0, len(row_buttons), MAX_BUTTONS_PER_ROW):
            builder.row(*row_buttons[start:start + MAX_BUTTONS_PER_ROW])
            added_rows += 1

    if not added_rows:
        logger.warning(
            "MAX keyboard: НИ ОДНОЙ кнопки не добавлено "
            "(rows=%d, dropped=%d). Полный JSON: %r",
            len(rows), dropped, str(vk_json)[:300])
        return None

    try:
        markup = builder.as_markup()
    except Exception as e:
        logger.error("MAX builder.as_markup() failed: %s", e, exc_info=True)
        return None

    # ФИКС: у InlineKeyboardAttachment кнопки в payload.buttons,
    # а НЕ в inline_keyboard. Без этого диагностика показывала
    # max_rows=0/buttons=0 при непустой клавиатуре.
    dumped = _dump_markup(markup)
    max_rows, n_buttons = _count_buttons_in_payload(dumped)

    if n_buttons == 0:
        logger.warning(
            "MAX keyboard: markup собран, но payload.buttons пуст. "
            "repr(markup)=%r, dump=%r",
            markup, str(dumped)[:500])
        return None

    logger.info(
        "MAX keyboard OK: vk_rows=%d -> max_rows=%d buttons=%d dropped=%d",
        len(rows), max_rows, n_buttons, dropped)
    return markup


def update_reply_map_from_vk_json(
    vk_json: Optional[str], reply_map: dict, limit: int = 300,
) -> None:
    """Обновляет reply_map (label -> payload) для text-кнопок.

    Раньше это делалось ТОЛЬКО если клавиатура успешно сконвертирована.
    Из-за этого при сбое конвертации клик по reply-кнопке (пользователь
    набирает текст) не находил payload. Теперь обновляем НЕЗАВИСИМО.

    Для inline-клавиатур (data["inline"]=True) reply_map не трогаем —
    там пользователь жмёт кнопку, а не набирает текст.
    """
    if not vk_json or not isinstance(vk_json, str):
        return
    try:
        data = json.loads(vk_json)
    except (json.JSONDecodeError, TypeError):
        return
    if data.get("inline"):
        return
    for row in data.get("buttons") or []:
        for b in row or []:
            action = b.get("action") or {}
            if action.get("type") != "text":
                continue
            label = (action.get("label") or "")[:64]
            if not label:
                continue
            payload_raw = action.get("payload")
            if isinstance(payload_raw, str):
                try:
                    payload = json.loads(payload_raw)
                except (json.JSONDecodeError, TypeError):
                    payload = {"action": "raw", "value": payload_raw}
            else:
                payload = payload_raw
            reply_map[label] = payload
    if len(reply_map) > limit:
        keys = list(reply_map.keys())
        for k in keys[:-limit]:
            reply_map.pop(k, None)