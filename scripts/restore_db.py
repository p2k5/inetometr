#!/usr/bin/env python3
"""
Скрипт восстановления базы данных Inetometr из резервной копии.

Использование:
    python scripts/restore_db.py --backup-file BACKUP [--db-file DB] [--json-file JSON] [--force]

Примеры:
    python scripts/restore_db.py --backup-file backups/speed_data.db_20260115_120000.gz
    python scripts/restore_db.py --backup-file backups/speed_data.db_20260115_120000 --db-file data/speed_data.db

Параметры:
    --backup-file       Обратная копия базы данных (обязательно)
    --db-file           Путь к восстановляемой SQLite БД (по умолчанию: data/speed_data.db)
    --json-file         Путь к восстановляемому JSON Lines файлу (по умолчанию: data/speed_data.json)
    --force             Перезапись существующих файлов без подтверждения
    --dry-run           Показать что будет восстановлено, но не выполнять

Форматы backup файлов:
    - speed_data.db_YYYYMMDD_HHMMSS (несжатый SQLite)
    - speed_data.db_YYYYMMDD_HHMMSS.gz (сжатый gzip SQLite)
    - speed_data.json_YYYYMMDD_HHMMSS (JSON Lines)
    - speed_data.json_YYYYMMDD_HHMMSS.gz (сжатый JSON Lines)
"""

import argparse
import gzip
import os
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

# Добавляем корень проекта в sys.path для импорта конфигурации
sys.path.insert(0, str(Path(__file__).parent.parent))

from inetometr.server import DB_FILE, DATA_FILE


def parse_args():
    parser = argparse.ArgumentParser(description="Восстановление БД Inetometr из резервной копии")
    parser.add_argument(
        "--backup-file",
        required=True,
        help="Путь к файлу резервной копии (обязательно)",
    )
    parser.add_argument(
        "--db-file",
        default=str(DB_FILE),
        help="Путь к SQLite базе данных для восстановления (по умолчанию: data/speed_data.db)",
    )
    parser.add_argument(
        "--json-file",
        default=str(DATA_FILE),
        help="Путь к JSON Lines файлу для восстановления (по умолчанию: data/speed_data.json)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Перезапись существующих файлов без запроса подтверждения",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Показать что будет восстановлено, но не выполнять",
    )
    return parser.parse_args()


def decompress_if_needed(backup_path: Path) -> bytes:
    """Декомпрессовет файл если он сжат gzip, иначе читает как есть."""
    if backup_path.suffix == ".gz":
        with gzip.open(backup_path, "rb") as f:
            return f.read()
    else:
        with open(backup_path, "rb") as f:
            return f.read()


def restore_sqlite(backup_path: Path, target_path: Path, dry_run: bool) -> bool:
    """Восстанавливает SQLite базу данных из бэкапа."""
    data = decompress_if_needed(backup_path)

    if dry_run:
        print(f"[DRY RUN] Восстановить SQLite: {target_path}")
        print(f"   Размер бэкапа: {len(data)} байт")
        return True

    # Создаем резервную копию текущей БД перед перезаписью
    if target_path.exists() and not args_force_was_set:
        backup_current = target_path.with_suffix(".db.bak")
        shutil.copy2(target_path, backup_current)
        print(f"📦 Текущая БД сохранена в: {backup_current}")

    # Удаляем старую БД и создаем новую
    if target_path.exists():
        target_path.unlink()

    # Пишем SQLite данные
    with open(target_path, "wb") as f:
        f.write(data)

    # Проверяем, что это валидная SQLite база
    try:
        conn = sqlite3.connect(str(target_path))
        conn.execute("SELECT COUNT(*) FROM speed_results")
        count = conn.fetchone()[0]
        conn.close()
        print(f"✅ SQLite БД восстановлена: {target_path} (записей: {count})")
        return True
    except sqlite3.Error as e:
        # Если restore failed, try to restore from backup
        print(f"❌ Ошибка валидации SQLite: {e}")
        if target_path.exists():
            target_path.unlink()
        return False


def restore_jsonl(backup_path: Path, target_path: Path, dry_run: bool) -> bool:
    """Восстанавливает JSON Lines файл из бэкапа."""
    data = decompress_if_needed(backup_path)

    if dry_run:
        print(f"[DRY RUN] Восстановить JSON Lines: {target_path}")
        print(f"   Размер бэкапа: {len(data)} байт")
        return True

    # Создаем резервную копию текущего JSON файла перед перезаписью
    if target_path.exists() and not args_force_was_set:
        backup_current = target_path.with_suffix(".json.bak")
        shutil.copy2(target_path, backup_current)
        print(f"📦 Текущий JSON файл сохранен в: {backup_current}")

    # Удаляем старый файл и создаем новый
    if target_path.exists():
        target_path.unlink()

    # Пишем JSON данные
    with open(target_path, "wb") as f:
        f.write(data)

    # Проверяем, что файл валиден
    try:
        # Проверяем, что это корректные JSON Lines
        valid_lines = 0
        with open(target_path, "r") as f:
            for line in f:
                line = line.strip()
                if line:
                    import json
                    json.loads(line)  # Will raise if invalid
                    valid_lines += 1
        print(f"✅ JSON Lines файл восстановлен: {target_path} (строк: {valid_lines})")
        return True
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        print(f"❌ Ошибка валидации JSON Lines: {e}")
        if target_path.exists():
            target_path.unlink()
        return False


def main():
    global args_force_was_set
    args_force_was_set = False

    args = parse_args()
    args_force_was_set = args.force

    backup_path = Path(args.backup_file)

    if not backup_path.exists():
        print(f"❌ Файл бэкапа не найден: {backup_path}")
        return 1

    print(f"🔄 Восстановление Inetometr из: {backup_path}")
    print()

    # Определяем, какой тип бэкапа (SQLite, JSONL, или оба)
    # Проверяем расширение
    is_sqlite_backup = backup_path.name.startswith("speed_data.db")
    is_jsonl_backup = backup_path.name.startswith("speed_data.json")

    success = True

    # Восстанавливаем SQLite если это SQLite бэкап или общий
    if is_sqlite_backup or not (is_jsonl_backup and not is_sqlite_backup):
        success = restore_sqlite(backup_path, Path(args.db_file), args.dry_run) and success

    # Восстанавливаем JSONL если это JSONL бэкап или общий
    if is_jsonl_backup or not (is_sqlite_backup and not is_jsonl_backup):
        success = restore_jsonl(backup_path, Path(args.json_file), args.dry_run) and success

    print()
    if success:
        print("✅ Восстановление завершено успешно")
        return 0
    else:
        print("❌ Восстановление завершено с ошибками")
        return 1


if __name__ == "__main__":
    sys.exit(main())