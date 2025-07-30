FROM python:3.12-slim

WORKDIR /app/backend

# Установка системных зависимостей
RUN apt-get update && apt-get install -y \
    gcc \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Установка uv
RUN pip install uv

# Копирование pyproject.toml и uv.lock
COPY pyproject.toml uv.lock ./

# Установка зависимостей через uv
RUN uv sync

# Копирование кода приложения
COPY . .

# Открытие порта
EXPOSE 8000

# Запуск приложения
CMD ["uv", "run", "python", "-m", "hypercorn", "main:app", "--bind", "0.0.0.0:8000"] 
