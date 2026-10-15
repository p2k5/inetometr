#!/usr/bin/env python3
"""Inetometr — сервис для измерения скорости интернета.

Интерфейс в стиле Яндекс-Интернетометра: страница с тестом,
пинг, загрузка, передача, история измерений.

Эндпоинты:
  GET  /          → HTML-страница с тестом скорости
  GET  /admin     → панель с историей измерений (защищено токеном)
  GET  /api/status→ JSON: IP, пинг, загрузка сервера
  POST /api/speed → JSON: {download, upload, ping, ip} → сохранение
  GET  /api/history → последние N измерений (default 20)
  GET  /health    → проверка живости для мониторинга
"""

import os
import socket
import json
import platform
import time
import asyncio
import logging
from datetime import datetime, timezone
from collections import deque
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field, field_validator
from pathlib import Path

import aiohttp
from starlette.types import Scope
from inetometr.data_store import SQLiteStore, _now_iso, build_entry, migrate_jsonLines_to_sqlite
from fastapi import FastAPI, Request, Query, HTTPException, status, Depends, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, Response, RedirectResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

try:
    from ping3 import ping
except ImportError:  # ping3 требует root; без него пинг отдаём через TCP
    ping = None

# ── константы и конфигурация ────────────────────────────────────────
BASE_DIR = Path(__file__).parent.parent.parent  # репо
DATA_FILE = Path(os.environ.get("INETOMETR_DATA", str(BASE_DIR / "data" / "speed_data.json")))
DB_FILE = Path(os.environ.get("INETOMETR_DB", str(BASE_DIR / "data" / "speed_data.db")))
HISTORY_LIMIT = 500

# Лимиты для защиты от злоупотреблений
MAX_FILE_SIZE_MB = 50          # Макс. размер файла для /download
MAX_UPLOAD_SIZE_MB = 10        # Макс. размер загружаемого файла
RATE_LIMIT_REQUESTS = 60       # Кол-во запросов за период (увеличено для теста скорости)
RATE_LIMIT_WINDOW = 60         # Период в секундах

# CORS настройки: белый список из env, по умолчанию "*" для разработки
_CORS_ORIGINS_STR = os.environ.get("INETOMETR_CORS_ORIGINS", "*")
CORS_ORIGINS = ["*"] if _CORS_ORIGINS_STR == "*" else [o.strip() for o in _CORS_ORIGINS_STR.split(",") if o.strip()]
# При использовании wildcard-origin "*" credentials должны быть отключены (CORS-спецификация)
ALLOW_CREDENTIALS = True if CORS_ORIGINS != ["*"] else False

# Токен для доступа к админ-панели (по умолчанию из env, иначе требуется установить)
ADMIN_TOKEN = os.environ.get("INETOMETR_ADMIN_TOKEN", "")

# Путь к файлам шаблонов и статики
STATIC_DIR = Path(__file__).parent / "static"
TEMPLATES_DIR = Path(__file__).parent / "templates"

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("inetometr")

# ── Pydantic модели для валидации ───────────────────────────────────
class SpeedResult(BaseModel):
    """Модель для валидации данных о замере скорости."""
    download: float = Field(..., ge=0, le=10000, description="Скорость загрузки в Мбит/с")
    upload: float = Field(..., ge=0, le=10000, description="Скорость передачи в Мбит/с")
    ping: float = Field(..., ge=0, le=1000, description="Пинг в мс")
    ip: Optional[str] = Field(None, pattern=r"^\d{1,3}(\.\d{1,3}){3}$", description="IP-адрес клиента")
    timestamp: Optional[str] = None

    @field_validator("timestamp")
    @classmethod
    def validate_timestamp(cls, v):
        """Проверка формата timestamp."""
        if v is not None:
            try:
                datetime.fromisoformat(v.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                raise ValueError("Invalid timestamp format")
        return v


# ── FastAPI приложение ───────────────────────────────────────────────
from contextlib import asynccontextmanager
from prometheus_fastapi_instrumentator import PrometheusFastApiInstrumentator

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Управление жизненным циклом приложения."""
    # Startup
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    logger.info("Приложение запущено, директория данных: %s", DATA_FILE.parent)
    if not ADMIN_TOKEN:
        logger.warning("INETOMETR_ADMIN_TOKEN не установлен. Админ-панель будет недоступна (503).")

    yield
    # Shutdown
    logger.info("Приложение остановлено")

app = FastAPI(
    title="Inetometr",
    description="Измерение скорости интернета",
    lifespan=lifespan,
)

# Instrument Prometheus metrics (must be done at module level, before middleware)
PrometheusFastApiInstrumentator().instrument(app).expose(app, endpoint="/metrics")

# HTTPS redirect middleware for production
@app.middleware("http")
async def https_redirect(request: Request, call_next):
    if os.environ.get("INETOMETR_FORCE_HTTPS", "").lower() in ("true", "1", "yes"):
        if request.headers.get("x-forwarded-proto", "http") != "https":
            url = request.url.replace(scheme="https")
            return RedirectResponse(url=url, status_code=301)
    response = await call_next(request)
    return response

# Trusted host middleware for security
if os.environ.get("INETOMETR_ALLOWED_HOSTS"):
    allowed_hosts = [h.strip() for h in os.environ["INETOMETR_ALLOWED_HOSTS"].split(",")]
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)

# ── статические файлы с кешированием ─────────────────────────────────────
class CacheControlStaticFiles(StaticFiles):
    """Статические файлы с различными политиками кеширования."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        """Переопределяем метод для добавления заголовков кеширования."""
        response = await super().get_response(path, scope)
        # Добавляем заголовки кеширования в зависимости от типа файла
        path_str = str(path).lower()
        if path_str.endswith('.css'):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        elif path_str.endswith('.js'):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        elif path_str.endswith(('.png', '.jpg', '.jpeg', '.gif', '.ico', '.svg')):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            response.headers["Cache-Control"] = "public, max-age=86400"
        return response

# ── static files & templates ─────────────────────────────────────────
app.mount("/static", CacheControlStaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=TEMPLATES_DIR)

# CORS middleware (можно убрать в production)
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,  # В production следует ограничить через env
    allow_credentials=ALLOW_CREDENTIALS,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── простая in-memory rate limiting ─────────────────────────────────
class RateLimiter:
    def __init__(self, requests: int, window: int):
        self.requests = requests
        self.window = window
        self.history = {}

    def is_allowed(self, key: str) -> bool:
        now = time.time()
        if key not in self.history:
            self.history[key] = []
        # Удаляем старые записи
        self.history[key] = [t for t in self.history[key] if now - t < self.window]
        # Проверяем лимит
        if len(self.history[key]) >= self.requests:
            return False
        self.history[key].append(now)
        return True

rate_limiter = RateLimiter(RATE_LIMIT_REQUESTS, RATE_LIMIT_WINDOW)

def check_rate_limit(request: Request):
    """Проверка rate limiting по IP клиента."""
    client_ip = request.client.host if request.client else "unknown"
    if not rate_limiter.is_allowed(client_ip):
        logger.warning("Rate limit exceeded for IP: %s", client_ip)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many requests. Please try again later."
        )

# ── security ─────────────────────────────────────────────────────────
security = HTTPBearer(auto_error=False)

def get_admin_token(credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)):
    """Извлекает токен из заголовка авторизации."""
    if not credentials or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing admin token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return credentials.credentials

# ── утилиты ─────────────────────────────────────────────────────────
async def _public_ip() -> str:
    """Публичный IP-адрес хоста (через несколько бесплатных API) с кэшированием."""
    # Кэш на 5 минут (300 секунд)
    cache_key = "public_ip"
    now = time.time()
    if hasattr(_public_ip, "cache") and _public_ip.cache:
        cached_time, cached_ip = _public_ip.cache
        if now - cached_time < 300:  # 5 минут
            return cached_ip

    timeout = aiohttp.ClientTimeout(total=4)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for url in ("https://api.ipify.org", "https://ifconfig.co", "https://ident.me"):
            try:
                async with session.get(url) as r:
                    ip = (await r.text()).strip()
                    if ip:
                        # Сохраняем в кэш
                        _public_ip.cache = (now, ip)
                        return ip
            except Exception:
                continue

    try:
        ip = socket.gethostbyname(socket.gethostname())
        # Сохраняем в кэш даже если fallback
        _public_ip.cache = (now, ip)
        return ip
    except Exception:
        return "unknown"


def _ping_ms(host: str = "8.8.8.8", timeout: float = 2.0) -> Optional[float]:
    """Пинг в мс: ping3 (нужен root) или fallback через TCP-соединение."""
    if ping:
        try:
            r = ping(host, timeout=timeout)
            if r:
                return round(r * 1000, 1)
        except Exception:
            pass
    # fallback: время TCP-подключения
    try:
        t0 = time.perf_counter()
        s = socket.create_connection((host, 53), timeout=timeout)
        s.close()
        return round((time.perf_counter() - t0) * 1000, 1)
    except Exception:
        return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _client_ip(request: Request) -> str:
    """IP клиента: X-Forwarded-For (за прокси) или remote_addr."""
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# ── SQLite storage ─────────────────────────────────────────────────────
sqlite_store = SQLiteStore(DB_FILE, HISTORY_LIMIT)
# One-time migration from JSON Lines to SQLite
if DATA_FILE.exists():
    migrated = migrate_jsonLines_to_sqlite(DATA_FILE, sqlite_store)
    if migrated:
        logger.info("Migrated %d entries from JSON Lines to SQLite", migrated)
    # Переименовываем старый файл, чтобы не мигрировать повторно
    try:
        DATA_FILE.rename(DATA_FILE.with_suffix(".json.bak"))
    except OSError:
        pass

# Инициализируем HISTORY для обратной совместимости (используем SQLite)
HISTORY: deque = sqlite_store.load_history()


# ── HTML-страницы ──────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Главная страница — тест скорости в стиле Яндекс-Интернетометра."""
    return templates.TemplateResponse(
        request,
        "index.html",
        {"title": "Inetometr"}
    )


def verify_admin_token(token: str = Depends(get_admin_token)):
    """Проверка токена для доступа к админ-панели."""
    if not ADMIN_TOKEN:
        # Если токен не установлен, запрещаем доступ к админ-панели
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Admin panel not configured. Set INETOMETR_ADMIN_TOKEN environment variable."
        )
    if token != ADMIN_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid admin token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return token


@app.get("/admin", response_class=HTMLResponse)
async def admin(request: Request, token: str = Depends(verify_admin_token)):
    """Админ-панель: история, статистика, настройки (защищено токеном)."""
    return templates.TemplateResponse(
        request,
        "admin.html",
        {"title": "Inetometr — панель"}
    )


# ── API ─────────────────────────────────────────────────────────────
@app.get("/api/status")
async def api_status(request: Request):
    """Статус сервера + IP клиента + пинг с заголовками кеширования."""
    check_rate_limit(request)
    return Response(
        content=json.dumps({
            "public_ip": await _public_ip(),
            "client_ip": _client_ip(request),
            "ping_ms": _ping_ms(),
            "os": f"{platform.system()} {platform.release()}",
            "history_count": len(HISTORY),
        }),
        media_type="application/json",
        headers={
            "Cache-Control": "public, max-age=30",  # Кешировать на 30 секунд
            "Content-Type": "application/json"
        }
    )


@app.get("/api/history")
async def api_history(
    request: Request,
    limit: int = Query(20, ge=1, le=HISTORY_LIMIT),
    offset: int = Query(0, ge=0),
    sort_by: str = Query("timestamp", pattern="^(timestamp|download|upload|ping)$"),
    sort_dir: str = Query("desc", pattern="^(asc|desc)$"),
):
    """Последние N измерений с пагинацией и сортировкой."""
    check_rate_limit(request)
    items, total = sqlite_store.load_history_paginated(
        limit=limit, offset=offset, sort_by=sort_by, sort_dir=sort_dir.upper()
    )
    response = JSONResponse(list(items))
    response.headers["x-total-count"] = str(total)
    response.headers["x-limit"] = str(limit)
    response.headers["x-offset"] = str(offset)
    response.headers["cache-control"] = "no-store"
    return response


@app.post("/api/speed")
async def api_speed(request: Request):
    """Сохранить замер скорости с валидацией Pydantic."""
    check_rate_limit(request)
    try:
        raw_body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad json"}, status_code=400)

    # Валидация через Pydantic
    try:
        data = SpeedResult(**raw_body)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=422)

    entry = {
        "download": data.download,
        "upload": data.upload,
        "ping": data.ping,
        "ip": data.ip or _client_ip(request),
        "timestamp": data.timestamp or _now_iso(),
    }

    # Сохраняем в SQLite
    rowid = sqlite_store.append(entry)
    # Обновляем in-memory HISTORY для обратной совместимости с тестами
    HISTORY.append(entry)
    # Ограничиваем размер HISTORY (хотя SQLiteStore уже ограничивает при загрузке)
    if len(HISTORY) > HISTORY_LIMIT:
        # Удаляем самые старые записи (слева в deque)
        while len(HISTORY) > HISTORY_LIMIT:
            HISTORY.popleft()
    logger.info("Saved speed test entry: download=%.2f, upload=%.2f, ping=%.1f",
               entry["download"] or 0, entry["upload"] or 0, entry["ping"] or 0)
    return {"ok": True, "stored": sqlite_store.count()}


@app.get("/download")
async def download(request: Request, mb: int = Query(1, ge=1, le=MAX_FILE_SIZE_MB)):
    """Стандартный файл-ответ для замеров скорости загрузки."""
    check_rate_limit(request)
    import io
    size = mb * 1024 * 1024
    chunk = b"\x00" * 1024 * 1024
    buf = io.BytesIO()
    for _ in range(mb):
        buf.write(chunk)
    # Небольшая задержка для корректного замера скорости (минимум 10ms)
    # Иначе локальные загрузки проходят за доли секунды и замер не работает
    await asyncio.sleep(0.01)
    return Response(content=buf.getvalue(), media_type="application/octet-stream",
                    headers={"Cache-Control": "no-store"})


@app.post("/upload")
async def upload(request: Request):
    """Принимает POST-тело и сообщает, сколько байт получено."""
    check_rate_limit(request)
    # Ограничиваем размер загружаемого файла
    body = await request.body()
    if len(body) > MAX_UPLOAD_SIZE_MB * 1024 * 1024:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File too large. Maximum size is {MAX_UPLOAD_SIZE_MB} MB"
        )
    # Небольшая задержка для корректного замера скорости
    await asyncio.sleep(0.01)
    return {"ok": True, "bytes": len(body)}


@app.websocket("/ws/metrics")
async def websocket_metrics(websocket: WebSocket):
    """WebSocket endpoint для реального времени метрик."""
    await websocket.accept()
    try:
        while True:
            # Получаем последнее измерение из истории
            items = list(sqlite_store.load_history())
            latest = items[-1] if items else None
            data = {
                "latest": {
                    "download": latest["download"] if latest else 0,
                    "upload": latest["upload"] if latest else 0,
                    "ping": latest["ping"] if latest else 0,
                },
                "history_count": sqlite_store.count(),
                "timestamp": _now_iso(),
            }
            await websocket.send_json(data)
            await asyncio.sleep(5)
    except Exception:
        pass
    finally:
        await websocket.close()


@app.get("/api/export/csv")
async def export_csv(request: Request, limit: int = Query(1000, ge=1, le=10000)):
    """Экспорт истории замеров в CSV формате."""
    check_rate_limit(request)
    items = sqlite_store.load_history()
    items = list(items)[-limit:]

    import io
    import csv
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["download", "upload", "ping", "ip", "timestamp"])
    for item in items:
        writer.writerow([item["download"], item["upload"], item["ping"], item["ip"], item["timestamp"]])

    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=speed_history.csv", "Cache-Control": "no-store"}
    )


@app.get("/health")
async def health():
    """Проверка живости для мониторинга."""
    return Response(
        content=json.dumps({"ok": True, "time": _now_iso()}),
        media_type="application/json",
        headers={"Cache-Control": "public, max-age=5"}
    )


# ── запуск ─────────────────────────────────────────────────────────
def main() -> None:
    """Консольная точка входа inetometr и python -m inetometr.server."""
    import uvicorn
    uvicorn.run(
        "inetometr.server:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8080")),
        log_level="info",
    )


if __name__ == "__main__":
    main()