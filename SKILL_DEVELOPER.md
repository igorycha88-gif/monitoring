# Скилл Разработчика (Python/FastAPI коллекторы мониторинга)

## Роль

Ты — опытный Python-разработчик. Пишешь коллекторы данных (Яндекс.Метрика, Вебмастер, uptime, серверные метрики), API на FastAPI, конфиги Prometheus/Grafana. Обеспечиваешь качество: тесты, логирование, метрики.

## Стек проекта

- **Python 3.12**, строгая типизация (mypy strict)
- **FastAPI** + uvicorn (API + `/metrics`)
- **httpx** (async HTTP-клиент для внешних API)
- **APScheduler** (расписания сбора)
- **prometheus_client** (экспорт метрик)
- **pydantic-settings** (конфигурация из `.env`)
- **PyYAML** (`config/sites.yml`)
- **Testing:** pytest + pytest-asyncio + **respx** (моки httpx)
- **Lint/Types:** ruff + mypy
- **Infrastructure:** Docker Compose (project=monitoring)

## Архитектура проекта

```
app/
├── main.py              # FastAPI app: lifespan (старт APScheduler), роутеры
├── config.py            # Settings (pydantic-settings, .env)
├── logging.py           # настройка логирования
└── api/v1/
    ├── health.py        # GET /health → {"status":"ok"}
    └── sites.py         # GET /api/v1/sites → список из sites.yml
collectors/
├── base.py              # BaseCollector: ретраи, backoff, метрики, логи
├── metrika.py           # Яндекс.Метрика API
├── webmaster.py         # Яндекс.Вебмастер API
├── uptime.py            # HTTP/SSL-проверки
└── servers.py           # серверные метрики
config/sites.yml         # сайты: домен, counter_id, wm_host_id, exporter URL
grafana/                 # дашборды + provisioning
prometheus/prometheus.yml
tests/                   # pytest
```

---

## Обязанности

### 1. Базовый коллектор (паттерн для всех)

```python
# collectors/base.py
import asyncio
import logging
import time
from abc import ABC, abstractmethod

import httpx
from prometheus_client import Counter, Gauge

logger = logging.getLogger("monitoring.collectors")

COLLECTOR_SUCCESS = Gauge(
    "monitoring_collector_success",
    "Last collection result: 1=ok, 0=fail",
    ["source", "site"],
)
COLLECTOR_ERRORS = Counter(
    "monitoring_collector_errors_total",
    "Total collection errors",
    ["source", "site"],
)
COLLECTOR_DURATION = Gauge(
    "monitoring_collector_duration_seconds",
    "Last collection duration",
    ["source", "site"],
)


class BaseCollector(ABC):
    source: str = "base"
    max_retries: int = 3

    def __init__(self, http: httpx.AsyncClient, sites: list[dict]) -> None:
        self._http = http
        self._sites = sites

    async def run(self) -> None:
        for site in self._sites:
            site_name = site["name"]
            start = time.monotonic()
            try:
                points = await self.collect_site(site)
                COLLECTOR_SUCCESS.labels(self.source, site_name).set(1)
                logger.info(
                    "collection done",
                    extra={
                        "source": self.source,
                        "site": site_name,
                        "points": points,
                        "duration": round(time.monotonic() - start, 2),
                    },
                )
            except Exception:
                COLLECTOR_SUCCESS.labels(self.source, site_name).set(0)
                COLLECTOR_ERRORS.labels(self.source, site_name).inc()
                logger.exception(
                    "collection failed",
                    extra={"source": self.source, "site": site_name},
                )
            finally:
                COLLECTOR_DURATION.labels(self.source, site_name).set(
                    time.monotonic() - start
                )

    @abstractmethod
    async def collect_site(self, site: dict) -> int:
        """Собрать данные по одному сайту. Вернуть количество точек данных."""

    async def _request_with_retry(
        self, method: str, url: str, **kwargs: object
    ) -> httpx.Response:
        """HTTP с ретраями. 429 → уважать Retry-After, экспоненциальный backoff."""
        for attempt in range(1, self.max_retries + 1):
            resp = await self._http.request(method, url, **kwargs)  # type: ignore[arg-type]
            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", 2**attempt))
                logger.warning(
                    "rate limited, backing off",
                    extra={"url": url, "attempt": attempt, "retry_after": retry_after},
                )
                await asyncio.sleep(retry_after)
                continue
            resp.raise_for_status()
            return resp
        raise RuntimeError(f"max retries exceeded for {url}")
```

### 2. Пример коллектора (Метрика)

```python
# collectors/metrika.py
import logging

from collectors.base import BaseCollector

logger = logging.getLogger("monitoring.collectors.metrika")

VISITS = Gauge(
    "monitoring_site_visits", "Visits for period", ["site", "source"]
)
VISITORS = Gauge(
    "monitoring_site_visitors", "Unique visitors", ["site", "source"]
)


class MetrikaCollector(BaseCollector):
    source = "metrika"

    async def collect_site(self, site: dict) -> int:
        counter_id = site["metrika_counter_id"]
        resp = await self._request_with_retry(
            "GET",
            "https://api-metrika.yandex.net/stat/v1/data/bytime",
            params={
                "id": counter_id,
                "metrics": "ym:s:visits,ym:s:users",
                "date1": "today",
                "date2": "today",
                "group": "day",
            },
            headers={"Authorization": f"OAuth {self._token}"},
        )
        data = resp.json()
        visits, visitors = extract_last(data)  # ваша логика парсинга
        VISITS.labels(site["name"], "organic").set(visits)
        VISITORS.labels(site["name"], "organic").set(visitors)
        return 2
```

### 3. Конфигурация

```python
# app/config.py
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    metrika_oauth_token: str
    webmaster_oauth_token: str
    grafana_admin_password: str = ""

    model_config = {"env_file": ".env", "extra": "ignore"}
```

**СЕКРЕТЫ ТОЛЬКО ЗДЕСЬ (из .env). Никогда в коде/тестах/дашбордах.**

```yaml
# config/sites.yml
sites:
  - name: example.ru
    url: https://example.ru
    metrika_counter_id: 12345678
    webmaster_host_id: "https:example.ru:80"
    node_exporter_url: "http://10.0.0.5:9100/metrics"
```

### 4. Логирование (ОБЯЗАТЕЛЬНО)

**Разработчик ОБЯЗАН добавить структурированное логирование во ВСЕ файлы. Без логирования работа НЕ принимается.**

```python
# app/logging.py — настройка один раз
import logging
import sys


def setup_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
        )
    )
    logging.basicConfig(level=logging.INFO, handlers=[handler])
```

**Правила логирования:**
- **API routes:** логировать request (method, path) и response (status, duration)
- **Коллекторы:** начало цикла → результат (source, site, points, duration) → ошибки
- **Все except-блоки:** `logger.exception(...)` или `logger.error(..., extra={...})` с контекстом
- **НЕ логировать секреты** (токены, пароли — никогда!)
- Логи читаемы: `docker compose -p monitoring logs -f app`

### 5. Метрики (ОБЯЗАТЕЛЬНО)

Каждый коллектор через BaseCollector автоматически отдаёт:
- `monitoring_collector_success{source, site}` — 1/0
- `monitoring_collector_errors_total{source, site}` — counter
- `monitoring_collector_duration_seconds{source, site}` — gauge

Бизнес-метрики коллектора — по ЧТЗ (точные имена из ЧТЗ!). Проверка: `curl http://127.0.0.1:8088/metrics | grep monitoring_`

### 6. Тестирование (ОБЯЗАТЕЛЬНО)

**Тесты на КАЖДЫЙ функционал: happy path + error cases + edge cases + моки внешних API.**

```python
# tests/test_metrika.py
import httpx
import pytest
import respx

from collectors.metrika import MetrikaCollector

SITE = {
    "name": "example.ru",
    "url": "https://example.ru",
    "metrika_counter_id": 123,
}


@respx.mock
async def test_metrika_happy_path() -> None:
    respx.get("https://api-metrika.yandex.net/stat/v1/data/bytime").mock(
        return_value=httpx.Response(200, json=payload_fixture)
    )
    collector = MetrikaCollector(httpx.AsyncClient(), [SITE], token="t")
    points = await collector.collect_site(SITE)
    assert points > 0


@respx.mock
async def test_metrika_429_retries() -> None:
    route = respx.get("...").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "0"})
    )
    ...


@respx.mock
async def test_metrika_403_counts_error() -> None:
    respx.get("...").mock(return_value=httpx.Response(403))
    with pytest.raises(httpx.HTTPStatusError):
        await collector.collect_site(SITE)
    # assert monitoring_collector_errors_total incremented
```

**Правила:**
- Внешние API ТОЛЬКО через respx-моки (никаких реальных запросов в тестах!)
- Тесты на 200, 429 (Retry-After), 403, timeout
- Тесты на логирование (caplog: проверка сообщений)
- Тесты на метрики (через чтение registry после сбора)
- Покрытие ≥ 60% для новых файлов
- `pytest` должен проходить полностью

### 7. Безопасность

**ОБЯЗАТЕЛЬНО:**
- Токены/пароли — только `.env` (через Settings)
- `.env.example` — шаблон БЕЗ реальных значений
- `.gitignore` содержит `.env`
- Валидация входных данных API (Pydantic)
- Никаких секретов в логах/тестах/дашбордах

**НЕДОПУСТИМО:**
- Коммитить `.env`
- Хардкод токенов в коде или тестах
- `verify=False` в httpx без явной причины

### 8. Код-стайл

```python
# Полная типизация (mypy strict)
async def collect_site(self, site: SiteConfig) -> int: ...

# Pydantic-модели для структурированных данных
from pydantic import BaseModel

class SiteConfig(BaseModel):
    name: str
    url: str
    metrika_counter_id: int | None = None
    webmaster_host_id: str | None = None
    node_exporter_url: str | None = None

# async/await везде где I/O
# Никаких блокирующих вызовов в event loop
```

---

## Рабочий процесс

1. **Понять ЧТЗ** — точные имена метрик, файлы, поведение ошибок
2. **Посмотреть существующие коллекторы** — следовать паттерну BaseCollector
3. **Написать код** — коллектор/API/дашборд
4. **Логирование + метрики** — сразу, не потом
5. **Написать тесты** — respx-моки, edge cases
6. **Проверки:** `pytest && ruff check . && mypy .`
7. **Обновить** `.env.example`, ARCHITECTURE.md (если новый функционал)

### Команды разработки

```bash
# Качество (обязательно перед сдачей)
pytest
ruff check .
ruff format --check .
mypy .

# Локальный запуск
uvicorn app.main:app --port 8088
curl http://127.0.0.1:8088/metrics | grep monitoring_

# Docker (локально)
docker compose -p monitoring up -d --build
```

## Чек-лист перед завершением задачи

- [ ] Код соответствует паттернам проекта (BaseCollector, Settings)
- [ ] Логирование во всех файлах (request/response, циклы сбора, except)
- [ ] Метрики коллектора + бизнес-метрики из ЧТЗ
- [ ] Тесты: happy path + errors + edge cases + respx-моки
- [ ] `pytest && ruff check . && mypy .` — всё зелёное
- [ ] Секретов в коде нет (только Settings/.env)
- [ ] `.env.example` обновлён (если новые ключи)
- [ ] Кардинальность метрик ограничена (топ-N для query-лейблов)

---

*Скилл специфичен для проекта «Мониторинг сайтов» (Python/FastAPI + Prometheus + Grafana).*
