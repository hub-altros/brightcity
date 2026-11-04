"""Диагностика: какие токены видят фото -241529035_457239410."""
import os
from dotenv import load_dotenv
from vk_api import VkApi

load_dotenv(encoding="utf-8")


def try_token(name: str, token: str):
    if not token or token.startswith("fake-"):
        print(f"{name}: —")
        return

    print(f"\n=== {name} (len={len(token)}) ===")
    try:
        api = VkApi(token=token).get_api()

        # Кто владелец токена? Community token тут упадёт с [27] — это норма.
        try:
            me = api.users.get()[0]
            print(f"  user: {me['id']} ({me['first_name']} {me['last_name']})")
        except Exception as e:
            print(f"  users.get: {str(e)[:120]} (может быть community token)")

        # Основное: видит ли токен наши фото?
        for pid in (457239410, 457239411):
            try:
                res = api.photos.getById(photos=f"-241529035_{pid}")
                print(f"  photo {pid}: {'OK' if res else 'EMPTY'}")
            except Exception as e:
                print(f"  photo {pid}: ERROR {str(e)[:120]}")
    except Exception as e:
        print(f"  init failed: {str(e)[:200]}")


for env_name in (
    "VK_USER_TOKEN",
    "VK_RELAY_TOKEN",
    "VK_BOT_TOKEN",
    "VK_DELETE_TOKEN",
    "VK_DELETE_TOKEN_FALLBACK",
):
    try_token(env_name, (os.getenv(env_name) or "").strip())