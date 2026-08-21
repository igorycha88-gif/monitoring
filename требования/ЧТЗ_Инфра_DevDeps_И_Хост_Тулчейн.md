# ЧТЗ — Инфра-пробел: dev-зависимости в образе + tулчейн на хосте

**Маршрут:** 3 (Аналитик → DevOps, прямая инфраструктурная задача)
**Тип:** упрощённое ЧТЗ (раздел 6 AGENTS.md)
**Дата:** 2026-08-20

---

## 1. Постановка проблемы

Зафиксированы два инфра-пробела, мешающих выполнению Правила 3/8 (`pytest && ruff && mypy`):

### 1.1 В Docker-образе нет dev-зависимостей
- `Dockerfile` выполняет только `pip install .` → ставит runtime-зависимости (`fastapi`, `apscheduler`, `httpx`, …).
- `pytest`, `mypy`, `respx`, `pytest-asyncio`, `pytest-cov`, `types-PyYAML` в образ НЕ попадают.
- `.dockerignore` дополнительно исключает директорию `tests/` — даже при установке dev-зависимостей тесты в образ не попадают.
- Следствие: внутри контейнера невозможно запустить `pytest`/`mypy` — нарушается воспроизводимость проверок Тестировщика в контейнерной среде.

### 1.2 На хосте system-python не видит apscheduler
- Системный `python3` (3.14, `/Library/Frameworks/Python.framework/...`) имеет глобально установленные `pytest`/`mypy`/`ruff`, но НЕ имеет `apscheduler` (и остальных runtime-зависимостей проекта).
- `.venv` (тоже 3.14) — корректна и содержит ВСЕ deps (runtime + dev, editable install `monitoring`).
- При запуске `pytest` (без активации venv) системным интерпретатором → `ModuleNotFoundError: No module named 'apscheduler'` на `tests/conftest.py` (import `app.main` → import `apscheduler`).
- Следствие: пользователь не может запустить проверки «просто командой» из AGENTS.md §8.

---

## 2. Решение

### 2.1 Dockerfile → multi-stage с dev-таргетом
- `base` — runtime-зависимости (`pip install .`).
- `dev` — наследуется от `base`, доустанавливает `.[dev]`, копирует `tests/`, `pyproject.toml`. Цель: запуск `pytest && ruff && mypy` внутри контейнера.
- `prod` (финальный, по умолчанию) — только runtime, БЕЗ `tests/`, БЕЗ dev-deps. Прод-образ остаётся lean (без регресса по размеру/поверхности атаки).

### 2.2 .dockerignore
- Убрать строку `tests` из `.dockerignore` (чтобы dev-stage мог скопировать тесты).
- Prod-образ не копирует `tests/` (Dockerfile-уровень), поэтому lean-цель сохраняется.

### 2.3 docker-compose.yml → сервис `app-dev` под profile `dev`
- Новый сервис `app-dev` с `profiles: ["dev"]` (не стартует в обычном `up`).
- `build: { context: ., target: dev }`.
- Команда по умолчанию: `pytest`.
- Volume-маунт `./tests:/srv/tests:ro` для локального прогона без пересборки.
- Портов не открывает, network — `monitoring-net` (необязателен, но единообразно).

### 2.4 Хост: scripts/check.sh — канонический wrapper
- Новый `scripts/check.sh` (chmod +x) запускает три проверки через `.venv/bin/`:
  ```
  .venv/bin/ruff check . && .venv/bin/mypy . && .venv/bin/pytest
  ```
- Не загрязняет system-python (apscheduler и прочие runtime-deps остаются только в `.venv`).
- Делает `.venv` каноническим тулчейном на хосте — явно, без необходимости помнить `source activate`.
- AGENTS.md §8 НЕ меняется (там остаются «эталонные» команды); wrapper — операционное удобство.

---

## 3. Файлы для изменения

| Файл | Действие |
|------|----------|
| `Dockerfile` | Переписать в multi-stage (`base`/`dev`/`prod`) |
| `.dockerignore` | Убрать `tests` |
| `docker-compose.yml` | Добавить сервис `app-dev` (profile `dev`) |
| `scripts/check.sh` | Новый wrapper-скрипт, chmod +x |

---

## 4. Критерии приёмки

1. `docker build --target dev -t monitoring-dev .` успешно собирается.
2. `docker run --rm monitoring-dev pytest --co -q` собирает тесты (нет ImportError по apscheduler/pytest).
3. `docker run --rm monitoring-dev mypy --version` и `ruff --version` работают внутри контейнера.
4. `docker build -t monitoring-app .` (default target = prod) собирается и НЕ содержит `tests/`, `pytest`, `mypy` (lean образ сохранён).
5. `docker compose -p monitoring up -d` НЕ запускает `app-dev` (он под profile `dev`).
6. `docker compose -p monitoring --profile dev run --rm app-dev pytest --co -q` собирает тесты.
7. На хосте: `./scripts/check.sh` запускается и доходит до выполнения pytest (нет ImportError по apscheduler) — использует `.venv/bin/`.
8. Существующее приложение НЕ затронуто (раздел 0.1) — задача локальная, на хосте/в образе monitoring-стека.
9. Секреты не попадают в образ: `.env` остаётся в `.dockerignore`.

---

## 5. Проверка регресса

- `docker compose -p monitoring config` — валидный compose.
- `docker compose -p monitoring up -d` поднимает ТОЛЬКО `app`, `prometheus`, `alertmanager`, `grafana` (не `app-dev`).
- Prod-образ `monitoring-app` после сборки: `docker run --rm monitoring-app python -c "import pytest" → ImportError` (pytest НЕ в prod — корректно).
- `curl http://127.0.0.1:8088/health` → 200 (после пересборки).

---

## 6. Маршрутизация

- **Исполнитель:** 🚀 DevOps (прямая инфраструктурная задача).
- **После выполнения:** полная пересборка monitoring-стека (Правило 6) + проверка по критериям приёмки.
