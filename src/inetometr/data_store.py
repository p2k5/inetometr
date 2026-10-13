"""Хранение данных: SQLite как основной, JSON Lines как резервный.

Модуль предоставляет единый интерфейс для сохранения и чтения
истории замеров скорости. SQLite используется по умолчанию,
JSON Lines — для миграции и обратной совместимости.
"""

import json
import logging
import sqlite3
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("inetometr.data_store")

# ── константы ──────────────────────────────────────────────────────────
HISTORY_LIMIT = 500

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS speed_results (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    download    REAL NOT NULL,
    upload      REAL NOT NULL,
    ping        REAL NOT NULL,
    ip          TEXT NOT NULL,
    timestamp   TEXT NOT NULL
)
"""

CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_speed_results_timestamp
ON speed_results(timestamp DESC)
"""


class SQLiteStore:
    """Потокобезопасное SQLite-хранилище истории замеров."""

    def __init__(self, db_path: Path, history_limit: int = HISTORY_LIMIT):
        self.db_path = Path(db_path)
        self.history_limit = history_limit
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None
        self._connect()
        self._init_db()

    def _connect(self) -> None:
        """Устанавливает соединение с SQLite."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            timeout=30.0,
        )
        # Включаем WAL для лучшей параллельной работы
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.row_factory = sqlite3.Row

    def _init_db(self) -> None:
        """Создаёт таблицы и индексы при первом запуске."""
        with self._lock:
            assert self._conn is not None
            self._conn.execute(CREATE_TABLE_SQL)
            self._conn.execute(CREATE_INDEX_SQL)
            self._conn.commit()

    def append(self, entry: dict) -> int:
        """Добавляет запись в SQLite и возвращает её id."""
        with self._lock:
            assert self._conn is not None
            cur = self._conn.execute(
                "INSERT INTO speed_results (download, upload, ping, ip, timestamp) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    entry["download"],
                    entry["upload"],
                    entry["ping"],
                    entry["ip"],
                    entry["timestamp"],
                ),
            )
            self._conn.commit()
            return cur.lastrowid or 0

    def load_history(self) -> deque:
        """Загружает историю из SQLite (последние history_limit записей)."""
        h: deque = deque(maxlen=self.history_limit)
        with self._lock:
            assert self._conn is not None
            try:
                cur = self._conn.execute(
                    "SELECT download, upload, ping, ip, timestamp "
                    "FROM speed_results ORDER BY id DESC LIMIT ?",
                    (self.history_limit,),
                )
                for row in cur.fetchall():
                    h.append(
                        {
                            "download": row["download"],
                            "upload": row["upload"],
                            "ping": row["ping"],
                            "ip": row["ip"],
                            "timestamp": row["timestamp"],
                        }
                    )
                # Переворачиваем, чтобы были в хронологическом порядке
                h.reverse()
            except sqlite3.Error as e:
                logger.error("SQLite read error: %s", e)
        return h

    def count(self) -> int:
        """Возвращает количество записей в SQLite."""
        with self._lock:
            assert self._conn is not None
            cur = self._conn.execute("SELECT COUNT(*) FROM speed_results")
            return cur.fetchone()[0]

    def close(self) -> None:
        """Закрывает соединение с SQLite."""
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None


def migrate_jsonLines_to_sqlite(json_path: Path, store: SQLiteStore) -> int:
    """Переносит данные из JSON Lines в SQLite (однократная миграция)."""
    migrated = 0
    if not json_path.exists():
        return migrated
    try:
        with open(json_path) as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    store.append(entry)
                    migrated += 1
                except json.JSONDecodeError as e:
                    logger.warning(
                        "Skipping invalid JSON line %d during migration: %s",
                        line_num, e,
                    )
    except OSError as e:
        logger.error("Cannot read JSON file for migration: %s", e)
    return migrated


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def build_entry(download: float, upload: float, ping: float, ip: str,
                timestamp: Optional[str] = None) -> dict:
    """Создаёт словарь-запись с текущей меткой времени."""
    return {
        "download": download,
        "upload": upload,
        "ping": ping,
        "ip": ip,
        "timestamp": timestamp or _now_iso(),
    }