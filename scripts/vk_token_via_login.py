# scripts/vk_token_via_login.py
"""Получение user-token через логин/пароль (offline, бессрочный).

Запуск:
    python scripts/vk_token_via_login.py

Логин/пароль можно задать через переменные окружения:
    VK_ADMIN_LOGIN=...
    VK_ADMIN_PASSWORD=...

По умолчанию используется app_id=2685278 (Kate Mobile) — это даёт
бессрочный токен (offline). ВНИМАНИЕ: неофициальный метод, VK может
отозвать токен. Для продакшена используйте VK ID OAuth с refresh.
"""
import os
import sys

import vk_api
from vk_api.exceptions import AuthError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def auth_handler():
    """Запрашивает код 2FA, если он включён на аккаунте."""
    key = input("2FA код: ").strip()
    return key, True  # (code, remember_device)


def captcha_handler(captcha):
    """VK сам передаёт объект капчи, если она запрошена."""
    print(f"\n⚠️  Требуется капча: {captcha.get_url()}")
    key = input("Введите капчу: ").strip()
    return captcha.try_again(key)


def main():
    login = (os.getenv("VK_ADMIN_LOGIN") or "").strip()
    password = (os.getenv("VK_ADMIN_PASSWORD") or "").strip()

    if not login:
        login = input("VK login (phone/email): ").strip()
    if not password:
        password = input("VK password: ").strip()

    try:
        vk_session = vk_api.VkApi(
            login=login,
            password=password,
            auth_handler=auth_handler,
            captcha_handler=captcha_handler,
            app_id=2685278,                         # Kate Mobile
            scope="offline,photos,docs,groups",     # offline = бессрочный
        )
        vk_session.auth()
        token = vk_session.token["access_token"]

        # Проверка: кто мы и админ ли сообщества
        api = vk_session.get_api()
        me = api.users.get()[0]
        print(f"\n✅ Вход выполнен: {me['id']} ({me['first_name']} {me['last_name']})")

        # Убедимся, что аккаунт — админ -241529035
        try:
            info = api.groups.getById(
                group_id=241529035,
                fields="is_admin,is_member",
            )[0]
            is_admin = info.get("is_admin")
            print(f"   Админ сообщества -241529035: {'ДА' if is_admin else 'НЕТ'}")
            if not is_admin:
                print("\n⚠️  Этот аккаунт НЕ админ -241529035.")
                print("   Фото и документы сообщества могут быть недоступны.")
                print("   Войдите под аккаунтом настоящего администратора.")
        except Exception as e:
            print(f"   groups.getById failed: {e}")

        print("\n" + "=" * 60)
        print("ACCESS_TOKEN:")
        print(token)
        print("=" * 60)
        print("\nСкопируйте его в .env как VK_DELETE_TOKEN и VK_USER_TOKEN.")

    except AuthError as e:
        print(f"\n❌ Ошибка авторизации: {e}")
        print("   Проверьте логин/пароль. Возможно, включена 2FA или капча.")
    except Exception as e:
        print(f"\n❌ Ошибка: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()