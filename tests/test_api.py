"""
Тесты для API сервиса inetometr
"""
import json
import os
from pathlib import Path
from fastapi.testclient import TestClient

# Устанавливаем временные переменные окружения для тестов
os.environ["INETOMETR_ADMIN_TOKEN"] = "test_admin_token_123"

from inetometr.server import app, HISTORY, DATA_FILE, DB_FILE, sqlite_store

import pytest

@pytest.fixture(autouse=True)
def setup_and_cleanup():
    """Очистка перед каждым тестом."""
    HISTORY.clear()
    if DATA_FILE.exists():
        DATA_FILE.unlink()
    # Очистка SQLite базы данных - удаляем все записи
    with sqlite_store._lock:
        sqlite_store._conn.execute("DELETE FROM speed_results")
        sqlite_store._conn.commit()

def test_health_endpoint():
    """Тест health endpoint"""
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert "time" in data

def test_api_status_endpoint():
    """Тест API status endpoint"""
    client = TestClient(app)
    response = client.get("/api/status")
    assert response.status_code == 200
    data = response.json()
    assert "public_ip" in data
    assert "client_ip" in data
    assert "ping_ms" in data
    assert "os" in data
    assert "history_count" in data

def test_save_speed_data():
    """Тест сохранения данных о скорости"""
    client = TestClient(app)
    test_data = {
        "download": 100.5,
        "upload": 50.2,
        "ping": 10.0,
        "ip": "192.168.1.1"
    }

    response = client.post("/api/speed", json=test_data)
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["stored"] == 1

    # Проверяем, что данные сохранились в history
    assert len(HISTORY) == 1
    entry = HISTORY[0]
    assert entry["download"] == 100.5
    assert entry["upload"] == 50.2
    assert entry["ping"] == 10.0
    assert entry["ip"] == "192.168.1.1"

def test_get_history():
    """Тест получения истории с пагинацией и сортировкой"""
    client = TestClient(app)

    # Добавляем несколько записей с явными временными метками
    # Добавляем с задержкой, чтобы timestamps были разными
    for i in range(3):
        test_data = {
            "download": float(100 + i*10),
            "upload": float(50 + i*5),
            "ping": float(10 + i),
            "ip": f"192.168.1.{i+1}",
            "timestamp": f"2026-01-0{i+1}T12:00:00"
        }
        client.post("/api/speed", json=test_data)

    # По умолчанию сортировка: timestamp desc (новые первыми)
    # Сортировка по timestamp descending: последние добавленные (с большим индексом i)
    # будут первыми, если timestamps разные
    response = client.get("/api/history?limit=2")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 2
    # Сортировка timestamp DESC: последние записи сначала
    # Записи добавлены с timestamps 2026-01-01, 2026-01-02, 2026-01-03
    # DESC порядок: 01-03, 01-02, 01-01, то есть download 120.0, 110.0
    assert data[0]["download"] == 120.0  # последняя добавленная (самая новая)
    assert data[1]["download"] == 110.0  # вторая добавленная

    # Проверка пагинации: offset=1, limit=2 - пропускаем самую новую, берем следующие две
    response = client.get("/api/history?limit=2&offset=1")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 2
    # Пропускаем самую новую (120.0), берем следующие две: [110.0, 100.0]
    assert data[0]["download"] == 110.0
    assert data[1]["download"] == 100.0

    # Проверка сортировки по download ASC (от меньшего к большему)
    response = client.get("/api/history?sort_by=download&sort_dir=asc&limit=3")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 3
    assert data[0]["download"] == 100.0
    assert data[1]["download"] == 110.0
    assert data[2]["download"] == 120.0

    # Проверка сортировки по download DESC (от большего к меньшему)
    response = client.get("/api/history?sort_by=download&sort_dir=desc&limit=3")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 3
    assert data[0]["download"] == 120.0
    assert data[1]["download"] == 110.0
    assert data[2]["download"] == 100.0

    # Проверка заголовков пагинации
    response = client.get("/api/history?limit=2")
    assert response.status_code == 200
    assert "x-total-count" in response.headers
    assert int(response.headers["x-total-count"]) == 3
    assert response.headers["x-limit"] == "2"
    assert response.headers["x-offset"] == "0"

def test_admin_endpoint_without_token():
    """Тест админ-панели без токена"""
    client = TestClient(app)
    # Без заголовка Authorization - 401 (Invalid or missing admin token)
    response = client.get("/admin")
    assert response.status_code == 401

def test_admin_endpoint_with_wrong_token():
    """Тест админ-панели с неправильным токеном"""
    client = TestClient(app)
    response = client.get("/admin", headers={"Authorization": "Bearer wrong_token"})
    assert response.status_code == 401

def test_admin_endpoint_with_correct_token():
    """Тест админ-панели с правильным токеном"""
    client = TestClient(app)
    response = client.get("/admin", headers={"Authorization": "Bearer test_admin_token_123"})
    assert response.status_code == 200
    assert "Inetometr. Панель" in response.text

def test_rate_limiting():
    """Тест rate limiting (базовый)"""
    client = TestClient(app)
    # Делаем несколько запросов подряд - они должны пройти, так как лимит высокий
    for i in range(5):
        response = client.get("/health")
        assert response.status_code == 200

def test_download_endpoint():
    """Тест endpoint для скачивания файла"""
    client = TestClient(app)
    response = client.get("/download?mb=1")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/octet-stream"
    # Проверяем, что мы получили 1 МБ данных
    assert len(response.content) == 1024 * 1024

def test_download_endpoint_size_limit():
    """Тест лимита размера для скачивания"""
    client = TestClient(app)
    # Пытаемся запросить файл больше максимального размера
    response = client.get("/download?mb=100")  # MAX_FILE_SIZE_MB = 50
    assert response.status_code == 422  # Validation error от Query

def test_upload_endpoint():
    """Тест endpoint для загрузки файла"""
    client = TestClient(app)
    test_data = b"x" * 1000  # 1000 байт
    response = client.post("/upload", content=test_data)
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["bytes"] == 1000

def test_upload_endpoint_size_limit():
    """Тест лимита размера для загрузки"""
    client = TestClient(app)
    # Пытаемся загрузить файл больше максимального размера
    test_data = b"x" * (11 * 1024 * 1024)  # 11 МБ > MAX_UPLOAD_SIZE_MB (10)
    response = client.post("/upload", content=test_data)
    assert response.status_code == 413  # Request Entity Too Large


# ── HTTP кеширование тесты (фаза 2.4) ────────────────────────────────────

def test_api_status_cache_headers():
    """Тест кеширования заголовков для /api/status"""
    client = TestClient(app)
    response = client.get("/api/status")
    assert response.status_code == 200
    # Должен иметь заголовок кеширования
    assert "cache-control" in response.headers
    cache_control = response.headers["cache-control"]
    # Должен быть public с max-age=30
    assert "public" in cache_control
    assert "max-age=30" in cache_control


def test_api_history_no_cache():
    """Тест отсутствия кеширования для /api/history (персональные данные)"""
    client = TestClient(app)
    response = client.get("/api/history")
    assert response.status_code == 200
    # Должен иметь no-store для персональных данных
    assert "cache-control" in response.headers
    assert response.headers["cache-control"] == "no-store"


def test_download_no_cache():
    """Тест отсутствия кеширования для /download"""
    client = TestClient(app)
    response = client.get("/download?mb=1")
    assert response.status_code == 200
    # Должен иметь no-store
    assert "cache-control" in response.headers
    assert response.headers["cache-control"] == "no-store"


def test_upload_no_cache():
    """Тест отсутствия кеширования для /upload"""
    client = TestClient(app)
    response = client.post("/upload", content=b"test")
    assert response.status_code == 200
    # Должен иметь no-store (или отсутствие кеширования)
    # POST обычно не кешируется, но проверяем заголовок если есть
    if "cache-control" in response.headers:
        assert "no-store" in response.headers["cache-control"]


def test_static_files_caching():
    """Тест кеширования статических файлов"""
    client = TestClient(app)
    # Проверяем CSS файл (должен иметь длительное кеширование)
    response = client.get("/static/css/main.css")
    # Файл может не существовать в тестах, но если существует -
    if response.status_code == 200:
        assert "cache-control" in response.headers
        cache_control = response.headers["cache-control"]
        # CSS должен иметь max-age=31536000 (1 год) и immutable
        assert "max-age=31536000" in cache_control
        assert "immutable" in cache_control


def test_api_export_csv_no_cache():
    """Тест отсутствия кеширования для CSV экспорта"""
    client = TestClient(app)
    response = client.get("/api/export/csv")
    # Может быть 200 (если есть данные) или 200 с пустым результатом
    assert response.status_code == 200
    # Должен иметь no-store
    assert "cache-control" in response.headers
    assert response.headers["cache-control"] == "no-store"


def test_health_endpoint_cache():
    """Тест кеширования для /health endpoint"""
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    # Должен иметь заголовок кеширования
    assert "cache-control" in response.headers
    cache_control = response.headers["cache-control"]
    # Должен быть public с небольшим max-age
    assert "public" in cache_control
    assert "max-age=" in cache_control


# ── CORS тесты (фаза 1.1) ────────────────────────────────────────────

def test_cors_allowed_origin():
    """Тест CORS для разрешённого Origin (wildcard по умолчанию)."""
    client = TestClient(app)
    response = client.get(
        "/health",
        headers={
            "Origin": "https://inetometr.ru",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert response.status_code == 200
    # CORS-заголовки должны быть присутствовать
    assert "access-control-allow-origin" in response.headers
    # При wildcard-origin "*" credentials не включаются (CORS-спецификация)
    # Это корректное поведение: credentials с wildcard запрещены
    acao = response.headers.get("access-control-allow-origin")
    assert acao in ("*", "https://inetometr.ru")


def test_cors_denied_origin():
    """Тест CORS для запрещённого Origin (не в белом списке)."""
    client = TestClient(app)
    response = client.get(
        "/health",
        headers={
            "Origin": "https://evil.example.com",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert response.status_code == 200
    # Запрещённый origin не должен получить разрешение
    # (заголовок либо отсутствует, либо не совпадает с Origin)
    acao = response.headers.get("access-control-allow-origin")
    assert acao != "https://evil.example.com"


def test_cors_preflight():
    """Тест CORS preflight-запроса (OPTIONS)."""
    client = TestClient(app)
    response = client.options(
        "/api/speed",
        headers={
            "Origin": "https://inetometr.ru",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Content-Type",
        },
    )
    # OPTIONS может быть обработан middleware и вернуть 200 или 405,
    # но CORS-заголовки должны присутствовать при разрешённом origin
    assert "access-control-allow-origin" in response.headers
    assert "access-control-allow-methods" in response.headers


def test_cors_no_origin():
    """Тест CORS без Origin заголовка (запрос с того же сервера)."""
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    # При отсутствии Origin CORS-заголовки могут отсутствовать
    # или быть пустыми — это нормально
