"""CLI для веб-админки.

    python -m web_app.cli add-admin <login> <password> [display_name]
    python -m web_app.cli list-admins
    python -m web_app.cli reset-password <login> <new_password>
    python -m web_app.cli delete-admin <login>
    python -m web_app.cli reset-2fa <login>
    python -m web_app.cli gen-icons

Все команды требуют настроенный .env (DB_*). Запускать из корня проекта.
"""
import argparse
import logging
import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
_root = os.path.dirname(_here)
if _root not in sys.path:
    sys.path.insert(0, _root)

from dotenv import load_dotenv
load_dotenv(encoding="utf-8")

from core.db import init_db_pool, shutdown_db
from web_app.db_helpers import (
    ensure_web_schema,
    create_web_admin,
    list_web_admins,
    set_web_admin_password,
    delete_web_admin,
    reset_totp,
)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")


def cmd_add_admin(args):
    init_db_pool()
    ensure_web_schema()
    admin_id = create_web_admin(
        args.login, args.password, args.display_name,
        bot_platform=args.bot_platform, bot_user_id=args.bot_user_id,
    )
    if admin_id:
        print(f"✅ Веб-админ создан: id={admin_id}, login={args.login}")
        print("   Первый вход = настройка 2FA (по QR-коду).")
    else:
        print("❌ Не удалось создать. Проверьте, что login не занят.")
    shutdown_db()


def cmd_list_admins(args):
    init_db_pool()
    ensure_web_schema()
    rows = list_web_admins()
    if not rows:
        print("(пусто)")
    for r in rows:
        print(f"#{r[0]} | {r[1]} | {r[2] or '—'} | active={r[3]} | "
              f"2fa={'✅' if r[4] else '❌'} | last_login={r[7] or '—'}")
    shutdown_db()


def cmd_reset_password(args):
    init_db_pool()
    ensure_web_schema()
    if set_web_admin_password(args.login, args.password):
        print(f"✅ Пароль для {args.login} обновлён.")
        print("   2FA не тронута — при следующем входе её спрашивать как обычно.")
    else:
        print(f"❌ Логин {args.login} не найден.")
    shutdown_db()


def cmd_delete_admin(args):
    init_db_pool()
    ensure_web_schema()
    if delete_web_admin(args.login):
        print(f"✅ Веб-админ {args.login} удалён (сессии и recovery-коды тоже).")
    else:
        print(f"❌ Логин {args.login} не найден.")
    shutdown_db()


def cmd_reset_2fa(args):
    init_db_pool()
    ensure_web_schema()
    if reset_totp(args.login):
        print(f"✅ 2FA сброшена для {args.login}. "
              f"При следующем входе настройка заново.")
    else:
        print(f"❌ Логин {args.login} не найден.")
    shutdown_db()


def cmd_gen_icons(args):
    """Генерирует PNG-иконки 192 и 512 из простого шаблона."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        print("❌ Требуется Pillow: pip install Pillow")
        return
    out_dir = os.path.join(os.path.dirname(__file__), "static")
    os.makedirs(out_dir, exist_ok=True)

    for size in (192, 512):
        img = Image.new("RGBA", (size, size), (234, 88, 12, 255))
        draw = ImageDraw.Draw(img)
        text = "ЯГ"
        try:
            font = ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                int(size * 0.42))
        except Exception:
            try:
                font = ImageFont.truetype("arial.ttf", int(size * 0.42))
            except Exception:
                font = ImageFont.load_default()
        bbox = draw.textbbox((0, 0), text, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        draw.text(((size - tw) / 2, (size - th) / 2 - bbox[1]), text,
                  fill=(255, 255, 255, 255), font=font)
        path = os.path.join(out_dir, f"icon-{size}.png")
        img.save(path, "PNG")
        print(f"✅ {path}")


def main():
    parser = argparse.ArgumentParser(prog="web_app.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("add-admin", help="Создать веб-админа")
    p.add_argument("login")
    p.add_argument("password")
    p.add_argument("display_name", nargs="?", default=None)
    p.add_argument("--bot-platform", default=None,
                   help="vk / tg / max — куда слать эхо-уведомления (опционально)")
    p.add_argument("--bot-user-id", type=int, default=None)
    p.set_defaults(func=cmd_add_admin)

    p = sub.add_parser("list-admins")
    p.set_defaults(func=cmd_list_admins)

    p = sub.add_parser("reset-password", help="Сбросить пароль веб-админа")
    p.add_argument("login")
    p.add_argument("password")
    p.set_defaults(func=cmd_reset_password)

    p = sub.add_parser("delete-admin", help="Удалить веб-админа")
    p.add_argument("login")
    p.set_defaults(func=cmd_delete_admin)

    p = sub.add_parser("reset-2fa")
    p.add_argument("login")
    p.set_defaults(func=cmd_reset_2fa)

    p = sub.add_parser("gen-icons")
    p.set_defaults(func=cmd_gen_icons)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()