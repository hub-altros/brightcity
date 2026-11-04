"""Получить прямые URL VK-фото (максимальный размер) по VK-строкам вложений.

Использование:
    python scripts/get_vk_photo_urls.py

Скрипт читает VK-строки из EXTRAS_PHOTO_IDS (core/texts.py) и токен из
VK_BOT_TOKEN / VK_RELAY_TOKEN (.env), затем печатает готовый список URL,
который можно вставить в EXTRAS_MAX_FILE_IDS.
"""
import os
import re
import sys

_here = os.path.dirname(os.path.abspath(__file__))
_root = os.path.dirname(_here)
if _root not in sys.path:
    sys.path.insert(0, _root)

from dotenv import load_dotenv
load_dotenv(encoding="utf-8")

import requests
from vk_api import VkApi

from core.texts import EXTRAS_PHOTO_IDS


def _parse_attachments(items):
    """photo-241529035_457239027 → [(-241529035, 457239027), ...]"""
    out = []
    for s in items:
        m = re.match(r"^photo(-?\d+)_(\d+)$", s)
        if not m:
            print(f"⚠ Пропускаю, не photo-строка: {s!r}", file=sys.stderr)
            continue
        out.append((int(m.group(1)), int(m.group(2))))
    return out


def main():
    token = os.getenv("VK_USER_TOKEN") or os.getenv("VK_RELAY_TOKEN") or os.getenv("VK_BOT_TOKEN")
    if not token or token.startswith("fake-"):
        print("❌ Нужен VK_RELAY_TOKEN или VK_BOT_TOKEN в .env", file=sys.stderr)
        sys.exit(1)

    pairs = _parse_attachments(EXTRAS_PHOTO_IDS)
    if not pairs:
        print("❌ EXTRAS_PHOTO_IDS пуст или не содержит photo-строк", file=sys.stderr)
        sys.exit(1)

    # ФИКС: VK API и longpoll ходят НАПРЯМУЮ, без прокси.
    session = requests.Session()
    session.trust_env = False
    session.proxies = {"http": None, "https": None}

    vk = VkApi(token=token, session=session).get_api()

    attachments_arg = ",".join(f"photo{p[0]}_{p[1]}" for p in pairs)
    try:
        photos = vk.photos.getById(
            photos=attachments_arg,
            extended=0,
            photo_sizes=1,
        )
    except Exception as e:
        print(f"❌ VK API photos.getById failed: {e}", file=sys.stderr)
        sys.exit(1)

    urls = []
    for photo in photos:
        sizes = photo.get("sizes") or []
        if not sizes:
            print(f"⚠ У фото {photo.get('owner_id')}_{photo.get('id')} нет sizes",
                  file=sys.stderr)
            continue
        # Размеры упорядочены от мелкого к крупному, берём максимальный.
        max_size = max(sizes, key=lambda s: (s.get("width") or 0) * (s.get("height") or 0))
        url = max_size.get("url")
        if url:
            urls.append(url)
            print(f"  photo{photo.get('owner_id')}_{photo.get('id')} → {max_size.get('type')} ({max_size.get('width')}x{max_size.get('height')})")

    print()
    print("=" * 70)
    print("Вставь это в core/texts.py как EXTRAS_MAX_FILE_IDS:")
    print("=" * 70)
    print()
    print("EXTRAS_MAX_FILE_IDS = [")
    for u in urls:
        print(f'    "{u}",')
    print("]")
    print()


if __name__ == "__main__":
    main()