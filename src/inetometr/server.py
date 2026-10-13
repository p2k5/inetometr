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

import aiohttp
from fastapi import FastAPI, Request, Query, HTTPException, status, Depends
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.middleware.cors import CORSMiddleware
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

try:
    from ping3 import ping
except ImportError:  # ping3 требует root; без него пинг отдаём через TCP
    ping = None

# ── константы и конфигурация ────────────────────────────────────────
BASE_DIR = Path(__file__).parent.parent.parent  # репо
DATA_FILE = Path(os.environ.get("INETOMETR_DATA", str(BASE_DIR / "data" / "speed_data.json")))
HISTORY_LIMIT = 500

# Лимиты для защиты от злоупотреблений
MAX_FILE_SIZE_MB = 50          # Макс. размер файла для /download
MAX_UPLOAD_SIZE_MB = 10        # Макс. размер загружаемого файла
RATE_LIMIT_REQUESTS = 60       # Кол-во запросов за период (увеличено для теста скорости)
RATE_LIMIT_WINDOW = 60         # Период в секундах

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

# ── FastAPI приложение ───────────────────────────────────────────────
from contextlib import asynccontextmanager

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

# ── static files & templates ─────────────────────────────────────────
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=TEMPLATES_DIR)

# CORS middleware (можно убрать в production)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # В production следует ограничить
    allow_credentials=True,
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
    """Публичный IP-адрес хоста (через несколько бесплатных API)."""
    timeout = aiohttp.ClientTimeout(total=4)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for url in ("https://api.ipify.org", "https://ifconfig.co", "https://ident.me"):
            try:
                async with session.get(url) as r:
                    ip = (await r.text()).strip()
                    if ip:
                        return ip
            except Exception:
                continue
    try:
        return socket.gethostbyname(socket.gethostname())
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


def _load_history() -> deque:
    """Загрузка истории из файла speed_data.json с обработкой ошибок."""
    h = deque(maxlen=HISTORY_LIMIT)
    if DATA_FILE.exists():
        try:
            with open(DATA_FILE) as f:
                for line_num, line in enumerate(f, 1):
                    line = line.strip()
                    if line:
                        try:
                            h.append(json.loads(line))
                        except json.JSONDecodeError as e:
                            logger.warning(
                                "Skipping invalid history line %d in %s: %s",
                                line_num, DATA_FILE, e
                            )
        except OSError as e:
            logger.error("Cannot read history file %s: %s", DATA_FILE, e)
    return h

HISTORY: deque = _load_history()


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
    """Статус сервера + IP клиента + пинг."""
    check_rate_limit(request)
    return {
        "public_ip": await _public_ip(),
        "client_ip": _client_ip(request),
        "ping_ms": _ping_ms(),
        "os": f"{platform.system()} {platform.release()}",
        "history_count": len(HISTORY),
    }


@app.get("/api/history")
async def api_history(request: Request, limit: int = Query(20, ge=1, le=HISTORY_LIMIT)):
    """Последние N измерений."""
    check_rate_limit(request)
    items = list(HISTORY)
    return items[-limit:]


@app.post("/api/speed")
async def api_speed(request: Request):
    """Сохранить замер скорости."""
    check_rate_limit(request)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad json"}, status_code=400)

    entry = {
        "download": body.get("download"),
        "upload": body.get("upload"),
        "ping": body.get("ping"),
        "ip": body.get("ip") or _client_ip(request),
        "timestamp": body.get("timestamp") or _now_iso(),
    }
    # чистим: только числа, иначе None
    for k in ("download", "upload", "ping"):
        v = entry[k]
        if isinstance(v, (int, float)) and v is not None:
            try:
                entry[k] = round(float(v), 2)
            except (ValueError, OverflowError):
                entry[k] = None
        else:
            entry[k] = None

    HISTORY.append(entry)
    try:
        DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(DATA_FILE, "a") as f:
            f.write(json.dumps(entry) + "\n")
        logger.info("Saved speed test entry: download=%.2f, upload=%.2f, ping=%.1f",
                   entry["download"] or 0, entry["upload"] or 0, entry["ping"] or 0)
    except Exception as e:
        logger.error("Failed to save speed test entry: %s", e)
        pass
    return {"ok": True, "stored": len(HISTORY)}


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


@app.get("/health")
async def health():
    """Проверка живости для мониторинга."""
    return {"ok": True, "time": _now_iso()}


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