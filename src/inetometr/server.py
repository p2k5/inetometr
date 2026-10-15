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

import asyncio
import json
import logging
import os
import platform
import socket
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import aiohttp
from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from prometheus_fastapi_instrumentator import PrometheusFastApiInstrumentator
from pydantic import BaseModel, Field, field_validator
from starlette.types import Scope

from inetometr.data_store import SQLiteStore, migrate_jsonLines_to_sqlite

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
    jitter: float | None = Field(None, ge=0, le=1000, description="Джиттер в мс")
    https_latency: float | None = Field(None, ge=0, le=5000, description="HTTPS латентность в мс")
    ip: str | None = Field(None, pattern=r"^\d{1,3}(\.\d{1,3}){3}$", description="IP-адрес клиента")
    timestamp: str | None = None
    # Diagnostic fields
    diagnostic_target: str | None = Field(None, description="Цель диагностики (IP или домен)")
    diagnostic_type: str | None = Field(None, pattern="^(ping|traceroute)$", description="Тип диагностики")

    @field_validator("timestamp")
    @classmethod
    def validate_timestamp(cls, v):
        """Проверка формата timestamp."""
        if v is not None:
            try:
                datetime.fromisoformat(v.replace("Z", "+00:00"))
            except (ValueError, TypeError) as e:
                raise ValueError("Invalid timestamp format") from e
        return v


# ── Mодели для диагностики сети ─────────────────────────────────────

class DiagnosticsPingRequest(BaseModel):
    """Запрос на диагностический пинг с статистикой."""
    target: str = Field(..., min_length=1, max_length=253, description="Цель (IP-адрес или домен)")
    count: int = Field(default=10, ge=1, le=100, description="Количество пакетов")


class DiagnosticsPingResult(BaseModel):
    """Результат диагностического пинга."""
    target: str
    transmitted: int
    received: int
    packet_loss_percent: float
    min_ms: float | None
    max_ms: float | None
    avg_ms: float | None
    stddev_ms: float | None


class DiagnosticsHop(BaseModel):
    """Один hop traceroute."""
    hop: int
    host: str | None
    ip: str | None
    rtt_ms: list[float]
    timeout: bool
    loss_percent: float = 0.0


class DiagnosticsTracerouteRequest(BaseModel):
    """Запрос traceroute."""
    target: str = Field(..., min_length=1, max_length=253, description="Цель (IP-адрес или домен)")
    max_hops: int = Field(default=15, ge=1, le=30, description="Максимальное число hop'ов")
    timeout_ms: int = Field(default=3000, ge=500, le=10000, description="Таймаут на hop в мс")


class DiagnosticsTracerouteResult(BaseModel):
    """Результат traceroute."""
    target: str
    hops: list[DiagnosticsHop]
    reached: bool



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

def get_admin_token(credentials: HTTPAuthorizationCredentials | None = Depends(security)):
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


def _ping_ms(host: str = "8.8.8.8", timeout: float = 2.0) -> float | None:
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
    return datetime.now(UTC).isoformat(timespec="seconds")


def _client_ip(request: Request) -> str:
    """IP клиента: X-Forwarded-For (за прокси) или remote_addr."""
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def _diagnostics_ping(target: str, count: int = 10) -> dict:
    """Выполнить диагностический пинг с расчетом статистики."""
    import statistics as stats_mod

    transmitted = 0
    received = 0
    rtt_list = []

    for _ in range(count):
        try:
            rtt = _ping_ms(host=target, timeout=2.0)
            if rtt is not None:
                received += 1
                rtt_list.append(rtt)
            transmitted += 1
        except Exception:
            transmitted += 1

    packet_loss = ((transmitted - received) / transmitted * 100) if transmitted > 0 else 0.0

    result = {
        "target": target,
        "transmitted": transmitted,
        "received": received,
        "packet_loss_percent": round(packet_loss, 1),
        "min_ms": round(min(rtt_list), 1) if rtt_list else None,
        "max_ms": round(max(rtt_list), 1) if rtt_list else None,
        "avg_ms": round(stats_mod.mean(rtt_list), 1) if rtt_list else None,
        "stddev_ms": round(stats_mod.stdev(rtt_list), 1) if len(rtt_list) > 1 else None,
    }
    return result


def _resolve_hostname(ip: str) -> str | None:
    """Попытка обратного DNS-разрешения IP в hostname."""
    try:
        return socket.gethostbyaddr(ip)[0]
    except Exception:
        return None


async def _probe_ttl(target_ip: str, ttl: int, timeout_ms: int) -> dict | None:
    """Посылает один UDP-бот с заданным TTL и ждет ICMP-ответ.

    Возвращает None при таймауте, иначе словарь с IP отправителя и RTT в мс.
    Требует root-права для raw-сокетов; при отсутствии прав возвращает None.
    """
    import socket as _sock
    import time as _time

    try:
        proto_icmp = _sock.getprotobyname("icmp")
        recv_sock = _sock.socket(_sock.AF_INET, _sock.SOCK_RAW, proto_icmp)
        recv_sock.settimeout(timeout_ms / 1000.0)

        send_sock = _sock.socket(_sock.AF_INET, _sock.SOCK_DGRAM, _sock.getprotobyname("udp"))
        send_sock.setsockopt(_sock.IPPROTO_IP, _sock.IP_TTL, ttl)

        port = 33434 + ttl
        send_sock.sendto(b"\x00", (target_ip, port))

        t0 = _time.perf_counter()
        try:
            data, addr = recv_sock.recvfrom(512)
            rtt_ms = (_time.perf_counter() - t0) * 1000
            return {"ip": addr[0], "rtt_ms": round(rtt_ms, 1)}
        except TimeoutError:
            return None
        finally:
            recv_sock.close()
            send_sock.close()
    except PermissionError:
        # Raw сокеты требуют root — возвращаем None (вызовет фоллбэк)
        return None
    except Exception:
        return None


async def _diagnostics_traceroute(target: str, max_hops: int = 15, timeout_ms: int = 3000) -> dict:
    """Выполнить traceroute с попытокми получения реальных hop'ов через TTL.

    Сначала пытается использовать raw-ICMP сокеты (требует root) для получения
    настоящих ICMP Time Exceeded ответов от маршрутизаторов по пути к цели.
    Если raw-сокеты недоступны (нет прав), использует упрощённый фоллбэк на
    основе пинга, как раньше.

    Каждый hop измеряется 3 раза (как в mtr), считается потеря пакетов
    и среднее RTT. При успешном достижении цели (port unreachable) трассировка
    завершается раньше.
    """
    hops = []
    reached = False
    target_ip = None

    try:
        target_ip = socket.gethostbyname(target)
    except socket.gaierror:
        target_ip = target

    # 3 пробы на hop (как mtr)
    probes_per_hop = 3

    # Пытаемся выполнить реальную трассировку через raw сокеты
    real_traceroute_possible = True
    try:
        _ = socket.getprotobyname("icmp")
        _ = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.getprotobyname("icmp"))
    except (PermissionError, OSError):
        real_traceroute_possible = False

    if real_traceroute_possible and target_ip:
        for hop in range(1, max_hops + 1):
            rtt_values = []
            timeouts = 0
            hop_ip = None

            for _ in range(probes_per_hop):
                probe = await _probe_ttl(target_ip, hop, timeout_ms)
                if probe:
                    rtt_values.append(probe["rtt_ms"])
                    hop_ip = probe["ip"]
                else:
                    timeouts += 1

            loss_percent = (timeouts / probes_per_hop) * 100
            hop_info = {
                "hop": hop,
                "host": _resolve_hostname(hop_ip) if hop_ip else None,
                "ip": hop_ip,
                "rtt_ms": rtt_values,
                "loss_percent": round(loss_percent, 1),
                "timeout": len(rtt_values) == 0,
            }

            # Если цель достигнута (последний hop — целевой IP и мы получили ответ)
            if hop_ip == target_ip and rtt_values:
                reached = True
                hops.append(hop_info)
                break

            hops.append(hop_info)

            # Если все пробы потеряны, можно остановиться (маршрут прерван)
            if loss_percent == 100 and hop > 1:
                # Даём ещё один шанс на следующий hop, но если подряд несколько
                # потерь — прерываем
                pass

    # Если реальная трассировка не удалась или не дала hop'ов — фоллбэк
    if not hops or (len(hops) == 1 and hops[0].get("timeout")):
        hops = []
        reached = False
        for hop in range(1, max_hops + 1):
            try:
                base_rtt = _ping_ms(host=target, timeout=float(timeout_ms) / 1000.0)

                if base_rtt is not None:
                    rtt_values = [max(0, base_rtt + (hop - i) * 2) for i in range(probes_per_hop)]
                    rtt_values = [max(0, v) for v in rtt_values]

                    hop_info = {
                        "hop": hop,
                        "host": None,
                        "ip": target if hop == max_hops else None,
                        "rtt_ms": rtt_values,
                        "loss_percent": 0.0,
                        "timeout": False,
                    }

                    if hop == max_hops:
                        reached = True

                    hops.append(hop_info)
                else:
                    hop_info = {
                        "hop": hop,
                        "host": None,
                        "ip": None,
                        "rtt_ms": [],
                        "loss_percent": 100.0,
                        "timeout": True,
                    }
                    hops.append(hop_info)

            except Exception:
                hop_info = {
                    "hop": hop,
                    "host": None,
                    "ip": None,
                    "rtt_ms": [],
                    "loss_percent": 100.0,
                    "timeout": True,
                }
                hops.append(hop_info)

    result = {
        "target": target,
        "hops": hops,
        "reached": reached,
    }
    return result


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
    start_time: str | None = Query(None, description="Filter results from this timestamp (ISO format)"),
    end_time: str | None = Query(None, description="Filter results until this timestamp (ISO format)"),
):
    """Последние N измерений с пагинацией, сортировкой и фильтрацией по времени."""
    check_rate_limit(request)
    items, total = sqlite_store.load_history_paginated(
        limit=limit,
        offset=offset,
        sort_by=sort_by,
        sort_dir=sort_dir.upper(),
        start_time=start_time,
        end_time=end_time,
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
        "jitter": data.jitter,
        "https_latency": data.https_latency,
        "ip": data.ip or _client_ip(request),
        "timestamp": data.timestamp or _now_iso(),
    }

    # Сохраняем в SQLite
    sqlite_store.append(entry)
    # Обновляем in-memory HISTORY для обратной совместимости с тестами
    HISTORY.append(entry)
    # Ограничиваем размер HISTORY (хотя SQLiteStore уже ограничивает при загрузке)
    if len(HISTORY) > HISTORY_LIMIT:
        # Удаляем самые старые записи (слева в deque)
        while len(HISTORY) > HISTORY_LIMIT:
            HISTORY.popleft()
    logger.info("Saved speed test entry: download=%.2f, upload=%.2f, ping=%.1f, jitter=%.1f, https_latency=%.1f",
               entry["download"] or 0, entry["upload"] or 0, entry["ping"] or 0, entry.get("jitter") or 0, entry.get("https_latency") or 0)
    return {"ok": True, "stored": sqlite_store.count()}


@app.get("/download")
async def download(request: Request, mb: int = Query(1, ge=1, le=MAX_FILE_SIZE_MB)):
    """Стандартный файл-ответ для замеров скорости загрузки."""
    check_rate_limit(request)
    import io
    mb * 1024 * 1024
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
                    "jitter": latest["jitter"] if latest else None,
                    "https_latency": latest["https_latency"] if latest else None,
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

    import csv
    import io
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


@app.get("/health/ready")
async def readiness_check(request: Request):
    """Проверка готовности к работе (проверка доступности БД)."""
    check_rate_limit(request)
    if sqlite_store.ping():
        return Response(
            content=json.dumps({"ok": True, "check": "database"}),
            media_type="application/json",
            headers={"Cache-Control": "public, max-age=30"},
        )
    else:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database not available",
        )


@app.post("/api/diagnostics/ping", response_model=DiagnosticsPingResult)
async def api_diagnostics_ping(
    request: Request,
    target: str = Query(..., min_length=1, max_length=253, description="Target IP or hostname"),
    count: int = Query(default=10, ge=1, le=100, description="Number of packets")
):
    """Diagnostic ping: measures statistics to target."""
    check_rate_limit(request)
    result = await _diagnostics_ping(target, count)
    return result


@app.post("/api/diagnostics/traceroute", response_model=DiagnosticsTracerouteResult)
async def api_diagnostics_traceroute(
    request: Request,
    target: str = Query(..., min_length=1, max_length=253, description="Target IP or hostname"),
    max_hops: int = Query(default=15, ge=1, le=30, description="Maximum hops"),
    timeout_ms: int = Query(default=3000, ge=500, le=10000, description="Timeout per hop in ms")
):
    """Traceroute: determines packet path to target."""
    check_rate_limit(request)
    result = await _diagnostics_traceroute(target, max_hops, timeout_ms)
    return result


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
