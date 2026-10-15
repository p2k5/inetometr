# Inetometr

Минималистичный сервис для измерения скорости интернета в стиле [Яндекс-Интернетометр](https://yandex.ru/internet/).

- HTML-интерфейс с тестом (пинг, загрузка, передача)
- REST API для замеров — `POST /api/speed`
- Панель администратора — `/admin` (защищено Bearer-токеном)
- Данные хранятся в `data/speed_data.json`
- Rate limiting для защиты от злоупотреблений
- Логирование через стандартный модуль `logging`
- Docker-ready

## Быстрый старт (локально)

```bash
# Клонирование
git clone https://github.com/<user>/inetometr.git
cd inetometr

# Установка зависимостей (с помощью uv)
uv pip install -e .

# Запуск
INETOMETR_ADMIN_TOKEN=mysecrettoken PORT=8080 python -m inetometr.server
```

Откройте в браузере: http://localhost:8080

## Быстрый старт (Docker)

```bash
# Сборка и запуск
INETOMETR_ADMIN_TOKEN=mysecrettoken docker-compose up --build -d

# Просмотр логов
docker-compose logs -f

# Остановка
docker-compose down
```

Откройте в браузере: http://localhost:8080

## Переменные среды

| Переменная           | По умолчанию               | Описание                                    |
|----------------------|---------------------------|---------------------------------------------|
| `PORT`               | `8080`                    | Порт HTTP-сервера                           |
| `HOST`               | `0.0.0.0`                 | Адрес для прослушивания                     |
| `INETOMETR_DATA`     | `data/speed_data.json`    | Файл с историей замеров                     |
| `INETOMETR_ADMIN_TOKEN` | *(пусто)*             | Токен для доступа к `/admin` (обязательно!) |

⚠️ **Важно**: Админ-панель `/admin` доступна только с правильным Bearer-токеном. Без установки `INETOMETR_ADMIN_TOKEN` доступ будет запрещен (503).

## Эндпоинты

| Метод | Путь         | Описание                                      |
|------|--------------|-----------------------------------------------|
| GET  | `/`          | Страница теста скорости                       |
| GET  | `/admin`     | Панель с историей (требует Bearer-токен)      |
| GET  | `/api/status`| Статус сервера + IP + пинг                    |
| POST | `/api/speed` | Сохранить замер: `{download, upload, ping, ip?}` |
| GET  | `/api/history`| Последние N измерений (default 20, max 500)   |
| GET  | `/download?mb=N`| Файл N МБ для замеров (1–50)                 |
| POST | `/upload`    | Принимает POST-тело, возвращает размер (max 10 МБ) |
| GET  | `/health`    | Проверка живости (`{"ok": true, "time": ...}`) |

## Пример сохранения замеров

```bash
curl -X POST http://localhost:8080/api/speed \
  -H "Content-Type: application/json" \
  -d '{"download": 123.4, "upload": 56.7, "ping": 12.5}'
```

## Пример получения истории

```bash
curl "http://localhost:8080/api/history?limit=10"
```

## Доступ к админ-панели

```bash
# В браузере откройте http://localhost:8080/admin
# Браузер запросит логин/пароль (или используйте Bearer токен в заголовке)

# Через curl с токеном:
curl -H "Authorization: Bearer mysecrettoken" http://localhost:8080/admin
```

## Запуск тестов

```bash
uv pip install -e ".[dev]"
pytest tests/ -v
```

## Структура проекта

```
inetometr/
├── src/
│   └── inetometr/
│       ├── __init__.py
│       └── server.py        # Основной код приложения
├── data/
│   └── speed_data.json      # История замеров (JSON Lines)
├── tests/
│   ├── conftest.py          # Конфигурация pytest
│   └── test_api.py          # Тесты API
├── Dockerfile
├── docker-compose.yml
├── prometheus.yml
├── pyproject.toml
└── README.md
```

## Разработка

```bash
# Установка dev-зависимостей
uv pip install -e ".[dev]"

# Запуск сервера с автозагрузкой
uv run python -m inetometr.server

# Запуск тестов
pytest tests/ -v

# Линтинг (при наличии ruff)
ruff check src/
```

## Резервное копирование и восстановление

Скрипты управления данными находятся в `scripts/`:

| Команда | Описание |
|---------|----------|
| `python scripts/backup_db.py` | Создать бэкап SQLite БД и JSON Lines |
| `python scripts/backup_db.py --compress --max-backups 30` | Бэкап со сжатием и ротацией |
| `python scripts/restore_db.py --backup-file backups/speed_data.db_...` | Восстановить БД из бэкапа |
| `python scripts/validate_db.py` | Проверить целостность данных |
| `python scripts/validate_db.py --check-health` | Быстрая проверка здоровья |
| `python scripts/cleanup_old_data.py --days 90` | Удалить данные старше 90 дней |
| `python scripts/cleanup_old_data.py --max-records 100 --archive` | Оставить 100 новых записей + архив |

## Лицензия

MIT