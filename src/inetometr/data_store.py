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
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger("inetometr.data_store")

# ── константы ──────────────────────────────────────────────────────────
HISTORY_LIMIT = 500

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS speed_results (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    download    REAL NOT NULL,
    upload      REAL NOT NULL,
    ping        REAL NOT NULL,
    jitter      REAL DEFAULT NULL,
    https_latency REAL DEFAULT NULL,
    ip          TEXT NOT NULL,
    timestamp   TEXT NOT NULL
)
"""

# Индексы для ускорения сортировки и фильтрации
INDEX_SQL_DOWNLOAD = "CREATE INDEX IF NOT EXISTS idx_speed_results_download ON speed_results(download)"
INDEX_SQL_UPLOAD = "CREATE INDEX IF NOT EXISTS idx_speed_results_upload ON speed_results(upload)"
INDEX_SQL_PING = "CREATE INDEX IF NOT EXISTS idx_speed_results_ping ON speed_results(ping)"
INDEX_SQL_JITTER = "CREATE INDEX IF NOT EXISTS idx_speed_results_jitter ON speed_results(jitter)"
INDEX_SQL_HTTPS_LATENCY = "CREATE INDEX IF NOT EXISTS idx_speed_results_https_latency ON speed_results(https_latency)"
INDEX_SQL_TIMESTAMP = """
CREATE INDEX IF NOT EXISTS idx_speed_results_timestamp
ON speed_results(timestamp DESC)
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
        self._conn: sqlite3.Connection | None = None
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
        """Создаёт таблицы и индексы при первом запуске, а также мигрирует существующую схему."""
        with self._lock:
            assert self._conn is not None
            self._conn.execute(CREATE_TABLE_SQL)
            # Миграция существующей схемы ДО создания индексов
            self._migrate_schema()
            self._conn.execute(INDEX_SQL_DOWNLOAD)
            self._conn.execute(INDEX_SQL_UPLOAD)
            self._conn.execute(INDEX_SQL_PING)
            self._conn.execute(INDEX_SQL_JITTER)
            self._conn.execute(INDEX_SQL_HTTPS_LATENCY)
            self._conn.execute(INDEX_SQL_TIMESTAMP)
            self._conn.commit()

    def _migrate_schema(self) -> None:
        """Добавляет новые колонки в существующие таблицы, если их ещё нет."""
        assert self._conn is not None
        cur = self._conn.execute("PRAGMA table_info(speed_results)")
        columns = {row[1] for row in cur.fetchall()}

        migrations = [
            ("jitter", "REAL DEFAULT NULL"),
            ("https_latency", "REAL DEFAULT NULL"),
        ]

        for col_name, col_type in migrations:
            if col_name not in columns:
                try:
                    self._conn.execute(
                        f"ALTER TABLE speed_results ADD COLUMN {col_name} {col_type}"
                    )
                    logger.info("Migrated DB: added column %s %s", col_name, col_type)
                except sqlite3.Error as e:
                    logger.error("Failed to add column %s: %s", col_name, e)
        if any(col_name not in columns for col_name, _ in migrations):
            self._conn.commit()

    def append(self, entry: dict) -> int:
        """Добавляет запись в SQLite и возвращает её id."""
        with self._lock:
            assert self._conn is not None
            cur = self._conn.execute(
                "INSERT INTO speed_results (download, upload, ping, jitter, https_latency, ip, timestamp) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    entry["download"],
                    entry["upload"],
                    entry["ping"],
                    entry.get("jitter"),
                    entry.get("https_latency"),
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
                    "SELECT download, upload, ping, jitter, https_latency, ip, timestamp "
                    "FROM speed_results ORDER BY id DESC LIMIT ?",
                    (self.history_limit,),
                )
                for row in cur.fetchall():
                    h.append(
                        {
                            "download": row["download"],
                            "upload": row["upload"],
                            "ping": row["ping"],
                            "jitter": row["jitter"],
                            "https_latency": row["https_latency"],
                            "ip": row["ip"],
                            "timestamp": row["timestamp"],
                        }
                    )
                # Переворачиваем, чтобы были в хронологическом порядке
                h.reverse()
            except sqlite3.Error as e:
                logger.error("SQLite read error: %s", e)
        return h

    def load_history_paginated(
        self,
        limit: int = 20,
        offset: int = 0,
        sort_by: str = "timestamp",
        sort_dir: str = "DESC",
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> tuple[deque, int]:
        """Загружает историю с пагинацией, сортировкой и фильтрацией.

        Args:
            limit: Максимальное количество записей (1 - 500)
            offset: Смещение (сколько записей пропустить в начале)
            sort_by: Поле для сортировки ("timestamp", "download", "upload", "ping")
            sort_dir: Направление сортировки ("ASC" или "DESC")
            start_time: Начальная временная метка (ISO format, включительно)
            end_time: Конечная временная метка (ISO format, включительно)

        Returns:
            Tuple (deque of records, total count of matching records)
        """
        # Валидация лимита
        limit = max(1, min(limit, self.history_limit))

        # Базовый запрос с сортировкой
        valid_sort_fields = {"timestamp", "download", "upload", "ping"}
        if sort_by not in valid_sort_fields:
            sort_by = "timestamp"

        sort_dir = sort_dir.upper()
        if sort_dir not in ("ASC", "DESC"):
            sort_dir = "DESC"

        # Строим условия фильтрации по времени
        time_conditions = []
        params = []

        if start_time is not None:
            time_conditions.append("timestamp >= ?")
            params.append(start_time)

        if end_time is not None:
            time_conditions.append("timestamp <= ?")
            params.append(end_time)

        # Считаем общее количество записей с учетом фильтрации
        count_query = "SELECT COUNT(*) FROM speed_results"
        if time_conditions:
            count_query += " WHERE " + " AND ".join(time_conditions)

        count_cur = self._conn.execute(count_query, tuple(params) if params else ())
        total_count = count_cur.fetchone()[0]

        # Строим основной запрос с пагинацией, сортировкой и фильтрацией
        # offset применяется после сортировки (то есть мы пропускаем N записей сверху)
        select_query = '''
            SELECT download, upload, ping, jitter, https_latency, ip, timestamp
            FROM speed_results
        '''
        if time_conditions:
            select_query += " WHERE " + " AND ".join(time_conditions)

        select_query += f'''
            ORDER BY {sort_by} {sort_dir}
            LIMIT ? OFFSET ?
        '''
        final_params = tuple(params) + (limit, offset) if params else (limit, offset)

        h: deque = deque(maxlen=limit)
        with self._lock:
            assert self._conn is not None
            try:
                cur = self._conn.execute(select_query, final_params)
                for row in cur.fetchall():
                    h.append(
                        {
                            "download": row["download"],
                            "upload": row["upload"],
                            "ping": row["ping"],
                            "jitter": row["jitter"],
                            "https_latency": row["https_latency"],
                            "ip": row["ip"],
                            "timestamp": row["timestamp"],
                        }
                    )
                # SQL запрос уже возвращает результаты в правильном порядке благодаря ORDER BY
                # Никакого реверса не нужен - результаты уже отсортированы как запрошено
            except sqlite3.Error as e:
                logger.error("SQLite read error during pagination: %s", e)
                h = deque(maxlen=limit)

        return h, total_count

    def count(self) -> int:
        """Возвращает количество записей в SQLite."""
        with self._lock:
            assert self._conn is not None
            cur = self._conn.execute("SELECT COUNT(*) FROM speed_results")
            return cur.fetchone()[0]

    def ping(self) -> bool:
        """Проверяет доступность SQLite базы данных."""
        try:
            with self._lock:
                assert self._conn is not None
                self._conn.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error as e:
            logger.error("SQLite ping failed: %s", e)
            return False

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
    return datetime.now(UTC).isoformat(timespec="seconds")


def build_entry(download: float, upload: float, ping: float, ip: str,
                timestamp: str | None = None) -> dict:
    """Создаёт словарь-запись с текущей меткой времени."""
    return {
        "download": download,
        "upload": upload,
        "ping": ping,
        "ip": ip,
        "timestamp": timestamp or _now_iso(),
    }
