# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Quick commands

| Task | Command |
|------|---------|
| Install dependencies (once) | `uv pip install -e ".[dev]"` |
| Run the development server | `INETOMETR_ADMIN_TOKEN=mysecrettoken uv run python -m inetometr.server` |
| Run all tests | `pytest tests/ -v` |
| Run a single test file | `pytest tests/test_api.py -v` |
| Lint source code | `ruff check src/` |
| Build the package | `uv build` |
| Start with Docker Compose | `INETOMETR_ADMIN_TOKEN=mysecrettoken docker-compose up --build -d` |
| View Docker logs | `docker-compose logs -f` |
| Stop Docker Compose | `docker-compose down` |

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `8080` | HTTP server port |
| `HOST` | `0.0.0.0` | Listen address |
| `INETOMETR_DATA` | `data/speed_data.json` | Path to the JSON history file |
| `INETOMETR_ADMIN_TOKEN` | _(required)_ | Bearer token for the `/admin` endpoint (must be set; otherwise `/admin` returns 503) |

## Architecture overview

This is a minimalist speed‑test service in the style of Yandex‑Internetometr, built with **FastAPI** and **aiohttp**. Key points:

- **FastAPI app** (`src/inetometr/server.py`) defines all routes and a lifespan manager that ensures the data directory exists at startup.
- **Data store**: measurements are appended as JSON lines to `data/speed_data.json`. A `deque` (max 500 entries) keeps the in‑memory history for the `/api/history` endpoint.
- **Endpoints**:
  - `GET /` – HTML test page (Jinja2 template `src/inetometr/templates/index.html`).
  - `GET /admin` – Admin panel (protected by `INETOMETR_ADMIN_TOKEN`; template `admin.html`).
  - `GET /api/status` – Server IP, ping, OS, and history count.
  - `POST /api/speed` – Accepts `{download, upload, ping, ip?}` and appends the entry to the history file.
  - `GET /api/history` – Returns the last N measurements (default 20, max 500).
  - `GET /download?mb=N` – Serves an N‑MB zero‑filled blob (used for speed‑test upload/download measurements).
  - `POST /upload` – Accepts raw body, returns `{ok, bytes}`; limited to 10 MB.
  - `GET /health` – Simple `{"ok": true, "time": "…"}` for monitoring.
- **Rate limiting**: in‑memory rate limiter (60 requests per 60 s per IP), configurable via `RATE_LIMIT_REQUESTS` and `RATE_LIMIT_WINDOW`.
- **Admin security**: the `/admin` and `/api/status` endpoints require a Bearer token passed as `Authorization: Bearer <token>`. The token is read from the `INETOMETR_ADMIN_TOKEN` environment variable.
- **Templates and static assets**: Jinja2 templates live under `src/inetometr/templates/`; static CSS/JS under `src/inetometr/static/`.

## Development workflow

1. **Install dev dependencies** (includes pytest, httpx, etc.): `uv pip install -e ".[dev]"`
2. **Start the server** with an admin token, e.g. `INETOMETR_ADMIN_TOKEN=test_token uv run python -m inetometr.server`
3. **Run the test suite**: `pytest tests/ -v`
   - Tests use a `TestClient` from `fastapi.testclient` and automatically set `INETOMETR_ADMIN_TOKEN=test_admin_token_123`.
   - Each test auto‑clears the in‑memory history via the `setup_and_cleanup` fixture.
4. **Lint the source**: `ruff check src/`
5. **Add a new endpoint or feature** in `src/inetometr/server.py` and add corresponding tests in `tests/`. Prefer adding tests over modifying `server.py` directly.
6. **Update `README.md`** if you change public API surface or configuration.

## Common pitfalls

- Forgetting to set `INETOMETR_ADMIN_TOKEN` will cause `/admin` to return **503** and the admin panel to be inaccessible.
- The rate‑limiter is per‑IP; repeated rapid requests from the same client will receive **429** responses.
- `/download?mb=N` caps at `MAX_FILE_SIZE_MB=50`; requests with `mb>50` will be rejected by the query‑parameter validation.
- Uploads larger than `MAX_UPLOAD_SIZE_MB=10` return **413** Request Entity Too Large.

## Where to look for …

| Topic | File(s) |
|-------|---------|
| Main FastAPI application, all routes | `src/inetometr/server.py` |
| Test configuration and admin token | `tests/conftest.py`, `tests/test_api.py` |
| Data file path and history handling | `src/inetometr/server.py` (constants `DATA_FILE`, `HISTORY`, `HISTORY_LIMIT`) |
| Templates (HTML pages) | `src/inetometr/templates/index.html`, `src/inetometr/templates/admin.html` |
| Static assets (CSS/JS) | `src/inetometr/static/css/main.css`, `src/inetometr/static/js/app.js` |
| Docker / Compose setup | `Dockerfile`, `docker-compose.yml` |
| Project metadata & dependencies | `pyproject.toml` |