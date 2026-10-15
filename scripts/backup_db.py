#!/usr/bin/env python3
"""
Скрипт резервного копирования базы данных Inetometr.

Использование:
    python scripts/backup_db.py [--output-dir BACKUP_DIR] [--max-backups N] [--compress]

Примеры:
    python scripts/backup_db.py
    python scripts/backup_db.py --output-dir /var/backups/inetometr --max-backups 30 --compress
"""

import argparse
import gzip
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

# Добавляем корень проекта в sys.path для импорта конфигурации
sys.path.insert(0, str(Path(__file__).parent.parent))

from inetometr.server import DB_FILE, DATA_FILE

def parse_args():
    parser = argparse.ArgumentParser(description="Резервное копирование БД Inetometr")
    parser.add_argument(
        "--output-dir",
        default="backups",
        help="Директория для сохранения бэкапов (по умолчанию: backups/ в корне проекта)",
    )
    parser.add_argument(
        "--max-backups",
        type=int,
        default=30,
        help="Максимальное количество хранимых бэкапов (старые удаляются). 0 = не удалять.",
    )
    parser.add_argument(
        "--compress",
        action="store_true",
        help="Сжимать бэкап с помощью gzip",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Показать что бы было сделано, но не выполнять",
    )
    return parser.parse_args()

def get_backup_filename(base_name: str, compress: bool) -> str:
    """Генерирует имя файла бэкапа с временной меткой."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if compress:
        return f"{base_name}_{timestamp}.gz"
    return f"{base_name}_{timestamp}"

def backup_file(src_path: Path, dst_dir: Path, base_name: str, compress: bool, dry_run: bool) -> Path:
    """Копирует (и при необходимости сжимает) файл в директорию бэкапов."""
    if not src_path.exists():
        print(f"⚠️  Файл не найден, пропускаем: {src_path}")
        return None

    dst_filename = get_backup_filename(base_name, compress)
    dst_path = dst_dir / dst_filename

    if dry_run:
        print(f"[DRY RUN] Бэкап {src_path.name} -> {dst_path}")
        return dst_path

    # Создаём директорию
    dst_dir.mkdir(parents=True, exist_ok=True)

    if compress:
        # Сжимаем через gzip
        with open(src_path, "rb") as f_in:
            with gzip.open(dst_path, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
        print(f"✅ Бэкап создан (сжат): {dst_path}")
    else:
        shutil.copy2(src_path, dst_path)
        print(f"✅ Бэкап создан: {dst_path}")

    return dst_path

def cleanup_old_backups(dst_dir: Path, base_name: str, max_backups: int, compress: bool):
    """Удаляет старые бэкапы, оставляя только max_backups последних."""
    if max_backups <= 0:
        return

    # Ищем все файлы бэкапов для данного базового имени
    pattern = f"{base_name}_*"
    if compress:
        pattern += ".gz"

    backups = sorted(dst_dir.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)

    if len(backups) > max_backups:
        to_delete = backups[max_backups:]
        for old_backup in to_delete:
            old_backup.unlink()
            print(f"🗑️  Удалён старый бэкап: {old_backup}")

def main():
    args = parse_args()

    project_root = Path(__file__).parent.parent
    output_dir = Path(args.output_dir)
    if not output_dir.is_absolute():
        output_dir = project_root / output_dir

    print(f"📦 Резервное копирование Inetometr")
    print(f"   Исходная БД: {DB_FILE}")
    print(f"   Директория бэкапов: {output_dir}")
    print(f"   Сжатие: {'включено' if args.compress else 'выключено'}")
    print(f"   Макс. бэкапов: {args.max_backups if args.max_backups > 0 else 'неограничено'}")
    print()

    # Бэкап SQLite базы
    sqlite_backup = backup_file(DB_FILE, output_dir, "speed_data.db", args.compress, args.dry_run)

    # Бэкап JSON Lines файла (если существует)
    json_backup = backup_file(DATA_FILE, output_dir, "speed_data.json", args.compress, args.dry_run)

    # Очистка старых бэкапов
    if not args.dry_run:
        if sqlite_backup:
            cleanup_old_backups(output_dir, "speed_data.db", args.max_backups, args.compress)
        if json_backup:
            cleanup_old_backups(output_dir, "speed_data.json", args.max_backups, args.compress)

    print()
    print("✅ Резервное копирование завершено")

    # Возвращаем код возврата
    if sqlite_backup is None and json_backup is None:
        print("⚠️  Не создано ни одного бэкапа (исходные файлы не найдены)")
        return 1
    return 0

if __name__ == "__main__":
    sys.exit(main())