"""Создаёт обычный фотоальбом в VK-сообществе и печатает его id.

Запуск:
    python scripts/create_vk_album.py

Требует в .env:
    VK_RELAY_TOKEN — user-token админа сообщества -241529035
    VK_GROUP_ID    — 241529035 (или свой)
"""
import os
import sys
from dotenv import load_dotenv
from vk_api import VkApi

load_dotenv(encoding="utf-8")

TOKEN = os.getenv("VK_RELAY_TOKEN") or os.getenv("VK_BOT_TOKEN")
GROUP_ID = os.getenv("VK_GROUP_ID") or "241529035"

if not TOKEN:
    print("❌ Нужен VK_RELAY_TOKEN в .env")
    sys.exit(1)

try:
    api = VkApi(token=TOKEN).get_api()

    # Проверка, что токен — user (не community). Community token
    # на users.get даст [27].
    try:
        me = api.users.get()[0]
        print(f"✅ Токен: {me['first_name']} {me['last_name']} (id={me['id']})")
    except Exception as e:
        print(f"❌ Токен не user (community?). users.get: {str(e)[:120]}")
        print("   Для photos.getUploadServer/photos.save нужен user-token админа.")
        sys.exit(1)

    # Проверка, что аккаунт — админ сообщества
    try:
        info = api.groups.getById(
            group_id=GROUP_ID, fields="is_admin"
        )[0]
        is_admin = info.get("is_admin")
        print(f"   Админ {GROUP_ID}: {'ДА' if is_admin else 'НЕТ'}")
        if not is_admin:
            print("⚠️  Не админ — возможно [15] при создании альбома.")
    except Exception as e:
        print(f"   groups.getById: {str(e)[:120]}")

    # Создать альбом
    print(f"\n📷 Создаю альбом «Заявки» в сообществе {GROUP_ID}…")
    album = api.photos.createAlbum(
        group_id=int(GROUP_ID),
        title="Заявки",
        description="Фото и рисунки заявок «Яркий!Город»",
    )
    album_id = album["id"]
    print(f"\n✅ Альбом создан: album_id={album_id}")
    print(f"\n📝 Добавь в .env:")
    print(f"   VK_PHOTOS_ALBUM_ID={album_id}")
    print(f"\nЗатем перезапусти MAX-бота (Ctrl+C → python -m bots.run_max)")

except Exception as e:
    print(f"\n❌ Ошибка: {type(e).__name__}: {e}")
    sys.exit(1)