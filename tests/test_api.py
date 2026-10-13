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
    """Тест получения истории"""
    client = TestClient(app)

    # Добавляем несколько записей
    for i in range(3):
        test_data = {
            "download": float(100 + i*10),
            "upload": float(50 + i*5),
            "ping": float(10 + i),
            "ip": f"192.168.1.{i+1}"
        }
        client.post("/api/speed", json=test_data)

    response = client.get("/api/history?limit=2")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 2
    # Должны быть последние 2 записи: 110.0 и 120.0
    assert data[0]["download"] == 110.0  # вторая запись (одна из последних двух)
    assert data[1]["download"] == 120.0  # третья запись (последняя)

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
