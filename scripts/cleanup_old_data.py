#!/usr/bin/env python3
"""
Скрипт очистки старых данных Inetometr.

Управляет хранением данных с учетом политики retention:
- Удаляет старые записи из SQLite базы данных на основе возраста (дни) или количества записей
- Архивирует данные перед удалением (опционально)
- Поддерживает настройку HISTORY_LIMIT (максимум записей в памяти)
- Генерирует отчет о удаленных данных

Использование:
    python scripts/cleanup_old_data.py [--days N] [--max-records N] [--archive] [--dry-run]

Примеры:
    python scripts/cleanup_old_data.py --days 90          # Удалить данные старше 90 дней
    python scripts/cleanup_old_data.py --max-records 100  # Оставить только 100 записей
    python scripts/cleanup_old_data.py --days 30 --archive  # С архивом и удалением старше 30 дней
    python scripts/cleanup_old_data.py --dry-run          # Показать что удалится, но не удалять
"""

import argparse
import csv
import io
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Добавляем корень проекта в sys.path для импорта конфигурации
sys.path.insert(0, str(Path(__file__).parent.parent))

from inetometr.server import DB_FILE, DATA_FILE, HISTORY_LIMIT


def parse_args():
    parser = argparse.ArgumentParser(description="Очистка старых данных Inetometr")
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="Удалить записи старше N дней (по умолчанию: None)",
    )
    parser.add_argument(
        "--max-records",
        type=int,
        default=None,
        help="Оставить максимум N самых новых записей (по умолчанию: None)",
    )
    parser.add_argument(
        "--archive",
        action="store_true",
        default=False,
        help="Архивировать удаленные данные в CSV перед удалением",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Показать что будет удалено, но не удалять",
    )
    parser.add_argument(
        "--output",
        default="archive",
        help="Директория для архива CSV (по умолчанию: archive/ в корне проекта)",
    )
    return parser.parse_args()


def cleanup_by_days(days: int, archive: bool, output_dir: Path, dry_run: bool) -> dict:
    """Удаляет записи старше указанного количества дней."""
    results = {
        "deleted": 0,
        "kept": 0,
        "archived": 0,
        "bytes_freed": 0,
        "errors": [],
    }

    if not DB_FILE.exists():
        results["errors"].append("SQLite база данных не существует")
        return results

    cutoff_date = datetime.now() - timedelta(days=days)
    cutoff_str = cutoff_date.isoformat()

    try:
        conn = sqlite3.connect(str(DB_FILE))
        conn.row_factory = sqlite3.Row

        # Считаем, сколько записей удастся удалить
        cur = conn.execute(
            "SELECT COUNT(*) FROM speed_results WHERE timestamp < ?",
            (cutoff_str,),
        )
        total_to_delete = cur.fetchone()[0]

        if total_to_delete == 0:
            results["kept"] = conn.execute("SELECT COUNT(*) FROM speed_results").fetchone()[0]
            conn.close()
            return results

        results["total_to_delete"] = total_to_delete

        if not dry_run:
            # Создаем архив если нужно
            if archive:
                archive_dir = output_dir
                archive_dir.mkdir(parents=True, exist_ok=True)

                # Экспортируем удаляемые записи в CSV
                cur = conn.execute(
                    "SELECT download, upload, ping, ip, timestamp FROM speed_results WHERE timestamp < ?",
                    (cutoff_str,),
                )

                archive_file = archive_dir / f"speed_data_archive_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"

                with open(archive_file, "w", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f)
                    writer.writerow(["download", "upload", "ping", "ip", "timestamp"])
                    for row in cur:
                        writer.writerow(list(row))

                results["archived"] = 1
                results["archived_file"] = str(archive_file)
                print(f"📦 Архив сохранен: {archive_file}")

            # Удаляем старые записи
            conn.execute(
                "DELETE FROM speed_results WHERE timestamp < ?",
                (cutoff_str,),
            )
            conn.commit()

            # Считаем свободное место (оценочно)
            # В SQLite нелегко получить размер freed, но можем посчитать старую длину
            results["deleted"] = total_to_delete

            # Очищаем кэш и обновляем статистику
            conn.execute("PRAGMA incremental_vacuum")

        # Считаем оставшиеся записи
        cur = conn.execute("SELECT COUNT(*) FROM speed_results")
        results["kept"] = cur.fetchone()[0]
        conn.close()

    except sqlite3.Error as e:
        results["errors"].append(f"SQLite ошибка: {e}")

    return results


def cleanup_by_max_records(max_records: int, archive: bool, output_dir: Path, dry_run: bool) -> dict:
    """Оставляет только N самых новых записей, удаляя старые."""
    results = {
        "deleted": 0,
        "kept": 0,
        "archived": 0,
        "errors": [],
    }

    if not DB_FILE.exists():
        results["errors"].append("SQLite база данных не существует")
        return results

    if max_records >= HISTORY_LIMIT:
        results["errors"].f"-max-records {max_records} exceeds HISTORY_LIMIT {HISTORY_LIMIT}")
        return results

    try:
        conn = sqlite3.connect(str(DB_FILE))

        # Считаем текущее количество записей
        current_count = conn.execute("SELECT COUNT(*) FROM speed_results").fetchone()[0]

        if current_count <= max_records:
            results["kept"] = current_count
            conn.close()
            print(f"ℹ️  Записей всего {current_count}, оставить {max_records} не требуется")
            return results

        results["current_count"] = current_count
        results["to_delete"] = current_count - max_records

        if not dry_run:
            # Создаем архив если нужно
            if archive:
                archive_dir = output_dir
                archive_dir.mkdir(parents=True, exist_ok=True)

                # Экспортируем удаляемые записи (самые старые)
                cur = conn.execute(
                    "SELECT download, upload, ping, ip, timestamp FROM speed_results ORDER BY id ASC LIMIT ?",
                    (results["to_delete"],),
                )

                archive_file = archive_dir / f"speed_data_archive_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"

                with open(archive_file, "w", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f)
                    writer.writerow(["download", "upload", "ping", "ip", "timestamp"])
                    for row in cur:
                        writer.writerow(list(row))

                results["archived"] = 1
                results["archived_file"] = str(archive_file)
                print(f"📦 Архив сохранен: {archive_file}")

            # Удаляем старые записи (самые старые с низким id)
            conn.execute(
                f"DELETE FROM speed_results WHERE id IN (SELECT id FROM speed_results ORDER BY id ASC LIMIT ?)",
                (results["to_delete"],),
            )
            conn.commit()

            results["deleted"] = results["to_delete"]

        # Считаем оставшиеся записи
        cur = conn.execute("SELECT COUNT(*) FROM speed_results")
        results["kept"] = cur.fetchone()[0]
        conn.close()

    except sqlite3.Error as e:
        results["errors"].append(f"SQLite ошибка: {e}")

    return results


def main():
    args = parse_args()

    project_root = Path(__file__).parent.parent
    output_dir = project_root / args.output

    print(f"🧹 Очистка данных Inetometr")
    print(f"   SQLite: {DB_FILE}")
    print()

    total_results = {
        "deleted": 0,
        "kept": 0,
        "archived": 0,
    }

    # Очистка по дням
    if args.days is not None:
        print(f"🗓️  Удаление данных старше {args.days} дней")
        day_results = cleanup_by_days(args.days, args.archive, output_dir, args.dry_run)
        total_results["deleted"] += day_results["deleted"]
        total_results["kept"] += day_results["kept"]
        total_results["archived"] += day_results["archived"]

        if day_results["errors"]:
            print("⚠️  Ошибки:")
            for error in day_results["errors"]:
                print(f"  ❌ {error}")

    # Очистка по максимальному числу записей
    if args.max_records is not None:
        print(f"🔢 Оставление максимум {args.max_records} записей")
        max_results = cleanup_by_max_records(args.max_records, args.archive, output_dir, args.dry_run)
        total_results["deleted"] += max_results["deleted"]
        total_results["kept"] += max_results["kept"]
        total_results["archived"] += max_results["archived"]

        if max_results["errors"]:
            print("⚠️  Ошибки:")
            for error in max_results["errors"]:
                print(f"  ❌ {error}")

    # Итоговый отчет
    print()
    print("--- Итоговый отчет ---")
    print(f"Удалено записей: {total_results['deleted']}")
    print(f"Оставлено записей: {total_results['kept']}")
    print(f"Архивов создано: {total_results['archived']}")

    if args.dry_run:
        print()
        print("🔍 Режим dry-run: никакие данные не были удалены")
        print("   Запустите без --dry-run для реального удаления")

    if total_results["errors"]:
        print()
        print("⚠️  Во время обработки были ошибки (см. выше)")
        return 1

    if not args.dry_run and (total_results["deleted"] > 0 or total_results["archived"] > 0):
        print()
        print("✅ Очистка данных завершена")

    return 0


if __name__ == "__main__":
    sys.exit(main())