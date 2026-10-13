"""
Frontend measurement validation test
"""
import json
import time
import subprocess
import os

os.environ["INETOMETR_ADMIN_TOKEN"] = "test_admin_token_123"

def test_full_measurement_cycle():
    """Test that the frontend measurement logic works end-to-end"""
    from inetometr.server import app
    from fastapi.testclient import TestClient
    
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

if __name__ == "__main__":
    test_full_measurement_cycle()
    print("Frontend measurement test passed!")
