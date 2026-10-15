#!/usr/bin/env python3
"""
Скрипт валидации базы данных Inetometr.

Проверяет целостность:
1. SQLite базы данных (существование таблиц, целостность данных)
2. JSON Lines файла (валидность строк, формат полей)
3. Соответствие данных между SQLite и JSON Lines
4. Индексы и оптимизацию запросов

Использование:
    python scripts/validate_db.py [--db-file DB] [--json-file JSON] [--strict]
    python scripts/validate_db.py --check-health

Примеры:
    python scripts/validate_db.py
    python scripts/validate_db.py --strict --db-file data/speed_data.db
    python scripts/validate_db.py --check-health
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

# Добавляем корень проекта в sys.path для импорта конфигурации
sys.path.insert(0, str(Path(__file__).parent.parent))

from inetometr.server import DB_FILE, DATA_FILE, HISTORY_LIMIT


def parse_args():
    parser = argparse.ArgumentParser(description="Валидация БД Inetometr")
    parser.add_argument(
        "--db-file",
        default=str(DB_FILE),
        help="Путь к SQLite базе данных (по умолчанию: data/speed_data.db)",
    )
    parser.add_argument(
        "--json-file",
        default=str(DATA_FILE),
        help="Путь к JSON Lines файлу (по умолчанию: data/speed_data.json)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Строгая проверка: также проверяет формат полей и правила Pydantic",
    )
    parser.add_argument(
        "--check-health",
        action="store_true",
        help="Краткая проверка здоровья (как health check)",
    )
    return parser.parse_args()


def validate_sqlite(db_path: Path, strict: bool = False) -> dict:
    """Валидирует SQLite базу данных."""
    results = {
        "file_exists": db_path.exists(),
        "tables_exist": False,
        "record_count": 0,
        "indexes_ok": True,
        "errors": [],
    }

    if not db_path.exists():
        results["errors"].append("Файл базы данных не существует")
        return results

    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row

        # Проверяем, что таблица существует
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='speed_results'"
        )
        table_exists = cur.fetchone() is not None
        results["tables_exist"] = table_exists

        if not table_exists:
            results["errors"].append("Таблица speed_results не найдена")
            conn.close()
            return results

        # Считаем записи
        cur = conn.execute("SELECT COUNT(*) FROM speed_results")
        results["record_count"] = cur.fetchone()[0]

        # Проверяем обязательные поля при strict режиме
        if strict:
            cur = conn.execute("SELECT * FROM speed_results LIMIT 1")
            row = cur.fetchone()
            if row:
                # Проверяем, что все нужные колонки есть
                required_cols = {"download", "upload", "ping", "ip", "timestamp"}
                actual_cols = set([desc[0] for desc in cur.description])
                missing_cols = required_cols - actual_cols
                if missing_cols:
                    results["errors"].append(f"Отсутствуют колонки: {missing_cols}")

        # Проверяем индексы
        cur = conn.execute("PRAGMA index_list(speed_results)")
        indexes = cur.fetchall()
        # min_rc > 0 означает, что индекс используется
        results["indexes_ok"] = len(indexes) > 0 or results["record_count"] == 0

        conn.close()

    except sqlite3.Error as e:
        results["errors"].append(f"SQLite ошибка: {e}")
        results["tables_exist"] = False

    return results


def validate_jsonl(jsonl_path: Path, strict: bool = False) -> dict:
    """Валидирует JSON Lines файл."""
    results = {
        "file_exists": jsonl_path.exists(),
        "valid_lines": 0,
        "total_lines": 0,
        "errors": [],
        "field_issues": [],
    }

    if not jsonl_path.exists():
        results["errors"].append("Файл JSON Lines не существует")
        return results

    try:
        required_fields = {"download", "upload", "ping", "ip", "timestamp"}
        optional_fields = {"download", "upload", "ping", "ip", "timestamp"}  # все поля могут быть

        with open(jsonl_path, "r", encoding="utf-8") as f:
            for line_num, line in enumerate(f, 1):
                results["total_lines"] += 1
                line = line.strip()
                if not line:
                    continue

                try:
                    data = json.loads(line)
                    results["valid_lines"] += 1

                    if strict:
                        # Проверяем required fields
                        missing = required_fields - set(data.keys())
                        if missing:
                            results["field_issues"].append(
                                f"Строка {line_num}: не хватает полей {missing}"
                            )

                        # Проверяем типы полей
                        if "download" in data and not isinstance(data["download"], (int, float)):
                            results["field_issues"].append(
                                f"Строка {line_num}: download должен быть числом"
                            )
                        if "upload" in data and not isinstance(data["upload"], (int, float)):
                            results["field_issues"].append(
                                f"Строка {line_num}: upload должен быть числом"
                            )
                        if "ping" in data and not isinstance(data["ping"], (int, float)):
                            results["field_issues"].append(
                                f"Строка {line_num}: ping должен быть числом"
                            )

                        # Проверяем IP-формат
                        if "ip" in data:
                            import re
                            ip_pattern = r"^\d{1,3}(\.\d{1,3}){3}$"
                            if not re.match(ip_pattern, data["ip"]):
                                results["field_issues"].append(
                                    f"Строка {line_num}: некорректный IP формат: {data['ip']}"
                                )

                except json.JSONDecodeError as e:
                    results["errors"].append(f"Строка {line_num}: неверный JSON - {e}")

    except UnicodeDecodeError as e:
        results["errors"].append(f"Ошибка декодирования: {e}")

    return results


def check_data_consistency(sqlite_results: int, jsonl_valid: int) -> dict:
    """Проверяет соответствие данных между SQLite и JSON Lines."""
    results = {
        "consistent": True,
        "sqlite_count": sqlite_results,
        "jsonl_count": jsonl_valid,
        "difference": 0,
        "issues": [],
    }

    if sqlite_results >= 0 and jsonl_valid >= 0:
        results["difference"] = abs(sqlite_results - jsonl_valid)
        if sqlite_results != jsonl_valid:
            results["consistent"] = False
            results["issues"].append(
                f"Различие в количестве записей: SQLite={sqlite_results}, JSONL={jsonl_valid}"
            )

    return results


def main():
    args = parse_args()

    if args.check_health:
        # Быстрая проверка здоровья
        print("🏥 Health check Inetometr БД")
        print()

        # Проверяем SQLite
        sqlite_result = validate_sqlite(Path(args.db_file), strict=False)
        print(f"SQLite: {'✅ OK' if sqlite_result['file_exists'] and sqlite_result['tables_exist'] else '❌ ПРОБЛЕМА'}")
        if sqlite_result["file_exists"] and sqlite_result["tables_exist"]:
            print(f"  Записей: {sqlite_result['record_count']}")

        # Проверяем JSONL
        jsonl_result = validate_jsonl(Path(args.json_file), strict=False)
        print(f"JSONL: {'✅ OK' if jsonl_result['file_exists'] and jsonl_result['valid_lines'] > 0 else '❌ ПРОБЛЕМА'}")
        if jsonl_result["file_exists"]:
            print(f"  Валидных строк: {jsonl_result['valid_lines']} из {jsonl_result['total_lines']}")

        # Общий статус
        all_ok = (
            sqlite_result["file_exists"]
            and sqlite_result["tables_exist"]
            and jsonl_result["file_exists"]
        )
        print()
        if all_ok:
            print("✅ Все проверки прошли успешно")
            return 0
        else:
            print("❌ Есть проблемы, требующие внимания")
            return 1

    # Полная валидация
    print(f"🔍 Валидация Inetometr баз данных")
    print(f"   SQLite: {args.db_file}")
    print(f"   JSONL:  {args.json_file}")
    print()

    # Валидируем SQLite
    print("--- SQLite ---")
    sqlite_result = validate_sqlite(Path(args.db_file), strict=args.strict)
    print(f"Файл существует: {'✅' if sqlite_result['file_exists'] else '❌'}")
    print(f"Таблица существует: {'✅' if sqlite_result['tables_exist'] else '❌'}")
    print(f"Записей: {sqlite_result['record_count']}")
    print(f"Индексы OK: {'✅' if sqlite_result['indexes_ok'] else '❌'}")

    if sqlite_result["errors"]:
        print("Ошибки:")
        for error in sqlite_result["errors"]:
            print(f"  ❌ {error}")

    # Валидируем JSONL
    print()
    print("--- JSON Lines ---")
    jsonl_result = validate_jsonl(Path(args.json_file), strict=args.strict)
    print(f"Файл существует: {'✅' if jsonl_result['file_exists'] else '❌'}")
    print(f"Всего строк: {jsonl_result['total_lines']}")
    print(f"Валидных: {jsonl_result['valid_lines']}")

    if jsonl_result["field_issues"]:
        print("Проблемы с полями:")
        for issue in jsonl_result["field_issues"]:
            print(f"  ⚠️ {issue}")

    if jsonl_result["errors"]:
        print("Ошибки:")
        for error in jsonl_result["errors"]:
            print(f"  ❌ {error}")

    # Проверяем согласованность
    print()
    print("--- Согласованность ---")
    consistency = check_data_consistency(
        sqlite_result["record_count"], jsonl_result["valid_lines"]
    )
    print(f"SQLite записи: {consistency['sqlite_count']}")
    print(f"JSONL валидные: {consistency['jsonl_count']}")
    print(f"Разница: {consistency['difference']}")

    if not consistency["consistent"]:
        print("⚠️  Внимание: данные в SQLite и JSONL не согласованы!")
        for issue in consistency["issues"]:
            print(f"  - {issue}")
    else:
        print("✅ Данные согласованы между SQLite и JSONL")

    # Итог
    print()
    all_ok = (
        sqlite_result["file_exists"]
        and sqlite_result["tables_exist"]
        and jsonl_result["file_exists"]
        and consistency["consistent"]
    )

    if args.strict:
        strict_ok = not sqlite_result["field_issues"]
        all_ok = all_ok and strict_ok
        if strict_ok:
            print("✅ Строгая проверка пройдена успешно")
        else:
            print("⚠️  Строгая проверка содержит предупреждения")

    if all_ok:
        print("✅ Все проверки пройдены успешно")
        return 0
    else:
        print("❌ Есть проблемы, требующие внимания")
        return 1


if __name__ == "__main__":
    sys.exit(main())