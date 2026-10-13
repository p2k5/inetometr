FROM python:3.12-slim

# Устанавливаем рабочую директорию
WORKDIR /app

# Устанавливаем системные зависимости
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Копируем файл зависимостей
COPY pyproject.toml .

# Устанавливаем uv
RUN pip install --no-cache-dir uv

# Устанавливаем зависимости проекта
RUN uv pip install --system -e .

# Копируем исходный код
COPY src/ /app/src/

# Копируем данные
COPY data/ /app/data/

# Открываем порт
EXPOSE 8080

# Команда запуска
CMD ["python", "-m", "inetometr.server"]