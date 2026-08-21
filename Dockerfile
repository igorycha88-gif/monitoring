# syntax=docker/dockerfile:1.7

# === base: runtime-зависимости проекта =========================================
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

COPY pyproject.toml ./
COPY app ./app
COPY collectors ./collectors
COPY config ./config
COPY scripts ./scripts

RUN pip install .

# === dev: tулчейн для тестов/линта/тайпчекка (pytest, mypy, ruff, respx) ========
# Использование:
#   docker build --target dev -t monitoring-dev .
#   docker run --rm monitoring-dev pytest
#   docker compose -p monitoring --profile dev run --rm app-dev pytest
FROM base AS dev

# dev-зависимости из [project.optional-dependencies].dev
RUN pip install '.[dev]'

# Тесты нужны только в dev-образе (в prod не копируются).
COPY tests ./tests

# По умолчанию dev-контейнер запускает pytest.
CMD ["pytest"]

# === prod: финальный lean-образ (по умолчанию) ==================================
# Только runtime-deps, БЕЗ tests/, БЕЗ pytest/mypy/ruff.
FROM base AS prod

EXPOSE 8088

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8088/health', timeout=3)"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8088"]
