# scripts/migrate_to_platform.py
"""Одноразовая миграция БД: DROP всех таблиц бота и создание новой схемы
с поддержкой нескольких платформ (platform).

Запуск:
    python scripts/migrate_to_platform.py --force

Флаг --force обязателен. Без него скрипт отказывается работать.
Скрипт удаляет ВСЕ таблицы бота вместе с данными — используйте только
если боевых заявок нет или есть свежий бэкап.
"""
import os
import sys
import time
import argparse
import logging
import subprocess

# Чтобы можно было запускать как python scripts/migrate_to_platform.py
# из корня проекта.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.db import get_db_connection, return_db_connection
from core.schema import drop_all_tables, ensure_schema

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("bright_city_migrate")


def main():
    parser = argparse.ArgumentParser(
        description="Миграция БД бота на мультиплатформенную схему.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Обязательный флаг подтверждения. Без него скрипт откажется работать.",
    )
    args = parser.parse_args()

    if not args.force:
        print("ОШИБКА: миграция удалит все таблицы бота вместе с данными.")
        print("Если вы уверены, запустите с флагом --force:")
        print("    python scripts/migrate_to_platform.py --force")
        sys.exit(1)

    # ИСПРАВЛЕНО: единственной защитой был флаг --force, а сам DROP необратим.
    # Теперь без успешного pg_dump миграция не запускается вообще.
    backup_dir = os.getenv("GDPR_BACKUP_DIR")
    if not backup_dir:
        print("ОШИБКА: задайте GDPR_BACKUP_DIR — без дампа миграция запрещена.")
        print("Скрипт выполняет DROP всех таблиц бота вместе с данными.")
        print('    $env:GDPR_BACKUP_DIR = "D:\\backups"')
        sys.exit(1)
    os.makedirs(backup_dir, exist_ok=True)
    dump_path = os.path.join(
        backup_dir, "pre_migration_%d.sql" % int(time.time()))
    try:
        subprocess.run(
            ["pg_dump", "-f", dump_path, os.environ.get("DB_NAME", "")],
            check=True,
        )
    except Exception as e:
        print("ОШИБКА: pg_dump не выполнен (%s). Миграция прервана." % e)
        sys.exit(1)
    print("Дамп создан: %s" % dump_path)

    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            logger.warning("Удаляем все таблицы бота…")
            drop_all_tables(cur)

            logger.warning("Создаём новую схему с platform…")
            ensure_schema(cur)

        conn.commit()
        logger.info("Миграция завершена успешно.")
        logger.info("Теперь можно запустить бота: python main.py")
    except Exception as e:
        if conn:
            conn.rollback()
        logger.exception("Ошибка миграции: %s", e)
        sys.exit(1)
    finally:
        if conn:
            return_db_connection(conn)


if __name__ == "__main__":
    main()