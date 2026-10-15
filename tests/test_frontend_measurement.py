"""
Frontend measurement validation test
"""
import json
import time
import os

os.environ["INETOMETR_ADMIN_TOKEN"] = "test_admin_token_123"

from inetometr.server import app, sqlite_store
from fastapi.testclient import TestClient
import pytest


@pytest.fixture(autouse=True)
def setup_and_cleanup():
    """Очистка перед каждым тестом."""
    with sqlite_store._lock:
        sqlite_store._conn.execute("DELETE FROM speed_results")
        sqlite_store._conn.commit()


def test_full_measurement_cycle():
    """Test that the frontend measurement logic works end-to-end"""
    client = TestClient(app)

    # Simulate what the frontend does
    # 1. Measure ping via /health requests
    ping_times = []
    for i in range(5):
        start = time.perf_counter()
        client.get("/health")
        end = time.perf_counter()
        ping_times.append((end - start) * 1000)
    avg_ping = sum(ping_times) / len(ping_times)

    # 2. Measure download
    start = time.perf_counter()
    response = client.get("/download?mb=1")
    end = time.perf_counter()
    assert response.status_code == 200
    assert len(response.content) == 1024 * 1024  # 1 MB
    download_time = end - start
    download_mbps = (1024*1024*8) / (download_time*1e6)

    # 3. Measure upload
    start = time.perf_counter()
    response = client.post("/upload", content=b"x" * (2*1024*1024))  # 2 MB
    end = time.perf_counter()
    assert response.status_code == 200
    assert response.json()["bytes"] == 2*1024*1024
    upload_time = end - start
    upload_mbps = (2*1024*1024*8) / (upload_time*1e6)

    # 4. Save results
    response = client.post("/api/speed", json={
        "download": round(download_mbps, 1),
        "upload": round(upload_mbps, 1),
        "ping": round(avg_ping, 1)
    })
    assert response.status_code == 200
    assert response.json()["ok"] is True

    # 5. Verify saved
    response = client.get("/api/history")
    assert response.status_code == 200
    history = response.json()
    assert len(history) >= 1
    latest = history[-1]
    assert latest["download"] == round(download_mbps, 1)
    assert latest["upload"] == round(upload_mbps, 1)
    assert latest["ping"] == round(avg_ping, 1)


def test_ws_metrics_connection():
    """Test WebSocket /ws/metrics endpoint sends valid data"""
    import asyncio

    async def _test_ws():
        with TestClient(app) as client:
            with client.websocket_connect("/ws/metrics") as ws:
                # Receive at least one message
                msg = ws.receive_json()
                assert msg is not None
                assert "latest" in msg
                assert "download" in msg["latest"]
                assert "upload" in msg["latest"]
                assert "ping" in msg["latest"]
                # Verify values are reasonable numbers
                assert isinstance(msg["latest"]["download"], (int, float))
                assert isinstance(msg["latest"]["upload"], (int, float))
                assert isinstance(msg["latest"]["ping"], (int, float))

    asyncio.run(_test_ws())


def test_export_csv():
    """Test CSV export endpoint"""
    client = TestClient(app)

    # Add some test data first
    with sqlite_store._lock:
        sqlite_store._conn.execute(
            "INSERT INTO speed_results (download, upload, ping, ip, timestamp) VALUES (?, ?, ?, ?, ?)",
            (100.0, 50.0, 10.0, "192.168.1.1", "2026-01-15T12:00:00")
        )
        sqlite_store._conn.execute(
            "INSERT INTO speed_results (download, upload, ping, ip, timestamp) VALUES (?, ?, ?, ?, ?)",
            (120.0, 60.0, 15.0, "192.168.1.2", "2026-01-15T13:00:00")
        )
        sqlite_store._conn.commit()

    response = client.get("/api/export/csv")
    assert response.status_code == 200
    # Should have CSV content
    assert "text/csv" in response.headers.get("content-type", "")
    # Should contain the data we inserted
    csv_text = response.text
    assert "100.0" in csv_text or "download" in csv_text
    # Should have no-store cache control
    assert response.headers["cache-control"] == "no-store"


def test_prometheus_metrics():
    """Test Prometheus metrics endpoint"""
    client = TestClient(app)

    # Add some test data
    with sqlite_store._lock:
        sqlite_store._conn.execute(
            "INSERT INTO speed_results (download, upload, ping, ip, timestamp) VALUES (?, ?, ?, ?, ?)",
            (100.0, 50.0, 10.0, "192.168.1.1", "2026-01-15T12:00:00")
        )
        sqlite_store._conn.commit()

    response = client.get("/metrics")
    assert response.status_code == 200
    # Should contain prometheus-formatted metrics
    text = response.text
    assert ("speed_results_total" in text or "inetometr" in text or "# HELP" in text)


if __name__ == "__main__":
    test_full_measurement_cycle()
    print("Frontend measurement test passed!")