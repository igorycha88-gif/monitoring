# ARCHITECTURE.md — Мониторинг сайтов

> Живой документ. Обновляется Архитектором при архитектурных изменениях.
> Решение фундамента: `требования/ADR-001_Каркас_и_модель_метрик.md`.

## Обзор

```
┌─────────────────────────────────────────────────────────────┐
│  VPS 130.49.129.241 (/root/monitoring/)  [локально: compose] │
│                                                             │
│  ┌───────────────────────────────────────────────┐          │
│  │ monitoring-app (FastAPI, 127.0.0.1:8088)      │          │
│  │  ├── /health        — healthcheck             │          │
│  │  ├── /metrics       — prometheus_client       │          │
│  │  ├── /api/v1/sites  — список сайтов           │          │
│  │  └── APScheduler    — расписание коллекторов  │          │
│  │       ├── collectors/uptime.py    [ЭПИК-1]    │──▶ HTTP/SSL сайтов
│  │       ├── collectors/metrika.py   [ЭПИК-2]    │──▶ Яндекс.Метрика API
│  │       ├── collectors/webmaster.py [ЭПИК-5]    │──▶ Яндекс.Вебмастер API
│  │       └── collectors/servers.py   [ЭПИК-6]    │──▶ node_exporter
│  └──────────────────────┬────────────────────────┘          │
│                         │ /metrics (pull, 15s)              │
│  ┌──────────────────────▼────────────────────────┐          │
│  │ Prometheus (127.0.0.1:9091)                   │          │
│  └──────────────────────┬────────────────────────┘          │
│                         │ datasource                        │
│  ┌──────────────────────▼────────────────────────┐          │
│  │ Grafana (127.0.0.1:3300) — provisioning       │          │
│  └───────────────────────────────────────────────┘          │
│                                                             │
│  РЯДОМ: СУЩЕСТВУЮЩЕЕ приложение — НЕ ТРОГАТЬ                │
└─────────────────────────────────────────────────────────────┘
```

## Стек

- Python ≥ 3.12 (типизация: mypy strict), FastAPI + uvicorn
- httpx (async), APScheduler 3.x, prometheus_client, structlog, PyYAML,
  cryptography (парсинг TLS-сертификатов)
- Тесты: pytest + pytest-asyncio + respx; lint: ruff; типы: mypy (pydantic plugin)
- Docker Compose (project `monitoring`), все порты на 127.0.0.1

## Файловая структура

```
monitoring/
├── app/
│   ├── main.py              # FastAPI app, lifespan (планировщик), middleware логов
│   ├── config.py            # Settings (pydantic-settings, .env)
│   ├── logging.py           # structlog (JSON по умолчанию)
│   ├── sites.py             # загрузка/валидация config/sites.yml
│   ├── metrics.py           # реестр метрик monitoring_*
│   └── api/v1/
│       ├── health.py        # GET /health
│       └── sites.py         # GET /api/v1/sites
├── collectors/
│   ├── base.py              # BaseCollector: метрики, логи, ретраи, расписание, parallel
│   └── uptime.py            # UptimeCollector (HTTP) + SSLCollector (сертификаты) [ЭПИК-1]
├── config/
│   └── sites.yml            # сайты: domain + опц. counter_id / host_id / exporter
├── grafana/
│   ├── dashboards/          # JSON-дашборды (ЭПИК-3)
│   └── provisioning/        # datasources + dashboards
├── prometheus/prometheus.yml
├── tests/
├── docker-compose.yml       # app 8088 + prometheus 9091 + grafana 3300
├── Dockerfile
└── .env.example             # шаблон секретов (.env НЕ в git, chmod 600)
```

## Конвенции

### Метрики
- Имя: `monitoring_<сущность>_<показатель>_<единицы>`
- `site` (домен из sites.yml) — обязателен в метриках сайтов; `source` — в метриках
  коллекторов (`uptime`, `ssl`, `metrika`, `webmaster`, `servers`)
- Counter — только монотонные величины; состояния — gauge
- Кардинальность: лейбл `query` (ЭПИК-5) — только топ-N (≤ 100)
- Здоровье САЙТА (`monitoring_uptime_status`) ≠ здоровье КОЛЛЕКТОРА
  (`monitoring_collector_success`): лежащий сайт — это данные (status=0),
  а не ошибка коллектора (см. ADR-002)

### Коллекторы
- Наследование от `BaseCollector`, реализация `collect_site(site) -> int`
- Метрики/логи цикла пишет базовый класс; ошибка одного сайта не рвёт цикл
- Ретраи: `self.retry(...)` — backoff, `retry_on` настраивается (429 — ЭПИК-2);
  uptime-проверка — без ретраев (цикл 60 сек = ретрай), SSL — с ретраями (ADR-002)
- `parallel = True` — параллельный обход сайтов (uptime/ssl); API-коллекторы
  (Метрика/Вебмастер) — последовательные (`parallel=False`, лимиты API)
- Токены только из Settings (.env); никогда в коде/коммитах

### Логирование
- structlog, JSON; события: `http_request`, `collector_cycle_start/end`,
  `collector_site_success/error`, `collector_retry`, `collector_registered`
- Каждый except: `logger.error(..., error=..., operation=...)`

## Порты (все 127.0.0.1)

| Сервис | Порт | Назначение |
|--------|------|-----------|
| monitoring-app | 8088 | API + /metrics |
| prometheus | 9091 | TSDB (retention 90d) |
| grafana | 3300 | визуализация (admin, anonymous off) |

## Запуск и проверки

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest && .venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy .

cp .env.example .env && chmod 600 .env
docker compose -p monitoring build --no-cache
docker compose -p monitoring up -d --force-recreate
```
