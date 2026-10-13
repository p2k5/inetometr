# Repository Guidelines

## Проектная структура и организация модулей

The repository consists of the following main components:

- **src/inetometr/** – основной код приложения (server.py, __init__.py).
- **tests/** – тесты (test_api.py, test_frontend_measurement.py, conftest.py).
- **data/** – файлы для хранения данных (speed_data.json).
- **pyproject.toml** – конфигурация проекта (FastAPI + aiohttp).
- **Dockerfile** – инструкции для сборки контейнера.
- **README.md** – общий описание проекта.

## Build, Test, и разработка

### Сборка
```bash
uv build
```
Использует `uv_build` для создания зависимостей.

### Запуск сервиса
```bash
uv run inetometr
```
или
```bash
uv run uvicorn inetometr:main --host 0.0.0.0 --port 8000
```

### Тестирование
```bash
pytest tests/ -v
```
Все тесты находятся в папке `tests/`. Используйте `pytest-asyncio` для асинхронных тестов.

## Кодинг-стиль и соглашения о именовании

- **Отступы**: 4 пробела (PEP 8).
- **Язык**: Python 3.13+.
- **Имена переменных/функций**: snake_case (например, `calculate_speed`, `test_endpoint`).
- **Файлы**: PascalCase для классов, `__init__.py` для пакетов.
- **Линтинг**: `ruff` для статического анализа кода.

## Проверка тестов

Тесты написаны с использованием **pytest**. Названия тестов следует начинать с `test_` или `integration_`. Пример:

```python
def test_api_response():
    """Проверка ответа API по адресу /health."""
    ...
```

Для запуска всех тестов используйте:
```bash
pytest tests/ -v
```

## Коммиты и pull request

- **Коммиты**: используйте conventional commits (например, `feat: добавить новый эндпоинт`, `fix: исправить баг в логике измерения`).
- **Pull requests**: обязательно привязывайте к issue в GitHub, предоставляйте краткое описание изменений и комментарии к коду.
- Каждый PR должен содержать хотя бы один тест, подтверждающий правильность изменений.

## Дополнительные рекомендации

- Не изменяйте `src/inetometr/server.py` напрямую – предпочтительно добавлять новые функции в `tests/`.
- Для добавления новых эндпоинтов создавайте соответствующие файлы в `tests/` и обновляйте документацию в `README.md`.
- Регулярно обновляйте зависимости через `uv sync --upgrade`.

