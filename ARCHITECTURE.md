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
│  │  ├── /api/v1/sd/node-exporter — HTTP SD [ЭПИК-6]
│  │  └── APScheduler    — расписание коллекторов  │          │
│  │       ├── collectors/uptime.py    [ЭПИК-1]    │──▶ HTTP/SSL сайтов
│  │       ├── collectors/metrika.py   [ЭПИК-2]    │──▶ Яндекс.Метрика API
│  │       ├── collectors/webmaster.py [ЭПИК-5]    │──▶ Яндекс.Вебмастер API
│  └──────────────────────┬────────────────────────┘          │
│                         │ /metrics (pull, 15s)              │
│  ┌──────────────────────▼────────────────────────┐          │
│  │ Prometheus (127.0.0.1:9091)                   │          │
│  │   ├── http_sd: /api/v1/sd/node-exporter [ЭПИК-6]
│  │   ├── rules: /etc/prometheus/alerts.yml [ЭПИК-7]
│  │   └──▶ node_exporter целей (лейбл site из SD) [ЭПИК-6]
│  │   │ firing alerts [ЭПИК-7]                    │          │
│  └───┼──────────────────┬────────────────────────┘          │
│      ▼                  │ datasource                        │
│  ┌────────────────┐     │                                    │
│  │ Alertmanager   │     │                                    │
│  │ (127.0.0.1:    │     │                                    │
│  │  9093) [ЭПИК-7]│     │                                    │
│  │ └─▶ Telegram   │     │                                    │
│  └────────────────┘     │                                    │
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
│       ├── sites.py         # GET /api/v1/sites
│       └── sd.py            # GET /api/v1/sd/node-exporter — HTTP SD [ЭПИК-6]
├── collectors/
│   ├── base.py              # BaseCollector: метрики, логи, ретраи, расписание, parallel
│   ├── uptime.py            # UptimeCollector (HTTP) + SSLCollector (сертификаты) [ЭПИК-1]
│   ├── metrika.py           # MetrikaCollector (визиты/посетители) [ЭПИК-2]
│   └── webmaster.py         # WebmasterCollector (поисковые запросы) [ЭПИК-5]
├── config/
│   └── sites.yml            # сайты: domain + опц. counter_id / host_id / exporter
├── grafana/
│   ├── dashboards/          # JSON-дашборды [ЭПИК-3]:
│   │   ├── site-overview.json  # «Обзор сайта» (переменная site, 8 панелей)
│   │   └── all-sites.json      # «Все сайты» (таблица статусов + спарклайны)
│   └── provisioning/        # datasources (uid: prometheus) + dashboards
├── prometheus/
│   ├── prometheus.yml       # scrape + rule_files + alerting→alertmanager [ЭПИК-7]
│   ├── alerts.yml           # правила алертов [ЭПИК-7, ADR-006]
│   ├── alertmanager.yml     # конфиг Alertmanager (шаблон с подстановкой) [ЭПИК-7]
│   └── alertmanager-entrypoint.sh
├── tests/
├── docker-compose.yml       # app 8088 + prometheus 9091 + alertmanager 9093 + grafana 3300
├── Dockerfile
└── .env.example             # шаблон секретов (.env НЕ в git, chmod 600)
```

## Конвенции

### Метрики
- Имя: `monitoring_<сущность>_<показатель>_<единицы>`
- `site` (домен из sites.yml) — обязателен в метриках сайтов; `source` — в метриках
  коллекторов (`uptime`, `ssl`, `metrika`, `webmaster`, `servers`)
- Counter — только монотонные величины; состояния — gauge
- Кардинальность: лейбл `query` (ЭПИК-5) — только топ-N (≤ 100);
  текст запроса нормализуется (пробелы + обрезка 100 символов), метрики
  поиска — `monitoring_search_clicks_total` / `monitoring_search_shows_total` /
  `monitoring_search_position` с лейблами `{site, query}` (DoD, gauge,
  окно «последняя неделя» — ADR-004)
- Здоровье САЙТА (`monitoring_uptime_status`) ≠ здоровье КОЛЛЕКТОРА
  (`monitoring_collector_success`): лежащий сайт — это данные (status=0),
  а не ошибка коллектора (см. ADR-002)

### Коллекторы
- Наследование от `BaseCollector`, реализация `collect_site(site) -> int`
- Метрики/логи цикла пишет базовый класс; ошибка одного сайта не рвёт цикл
- Ретраи: `self.retry(...)` — backoff, `retry_on` настраивается; при
  `RateLimitError` (HTTP 429) уважается `Retry-After` (ADR-003);
  uptime-проверка — без ретраев (цикл 60 сек = ретрай), SSL — с ретраями (ADR-002)
- API-коллекторы Яндекса (Метрика ADR-003, Вебмастер ЭПИК-5): ошибки
  401/403/404 не ретраятся; пустой ответ = данные (нули), не ошибка
- `parallel = True` — параллельный обход сайтов (uptime/ssl); API-коллекторы
  (Метрика/Вебмастер) — последовательные (`parallel=False`, лимиты API)
- Токены только из Settings (.env); никогда в коде/коммитах

### Дашборды Grafana (ЭПИК-3)
- Datasource только по uid `prometheus` (provisioning), без `${DS_...}` переменных
- Переменная `site` — query `label_values(monitoring_uptime_status, site)`
- `monitoring_site_visits_total` / `monitoring_site_visitors` — gauge:
  в панелях напрямую, БЕЗ `rate()`/`increase()`
- Валидация JSON-дашбордов — `tests/test_dashboards.py`
  (белый список метрик синхронизирован с `app/metrics.py`)

### Серверные метрики node_exporter (ЭПИК-6, ADR-005)
- Python-коллектора НЕТ: Prometheus скрейпит node_exporter напрямую через
  `http_sd_configs` → `GET /api/v1/sd/node-exporter` (единый источник
  целей — `config/sites.yml`, поле `node_exporter_url`; refresh 60s)
- SD-ответ: `[{"targets": ["host:port"], "labels": {"site": "<домен>"}}]`;
  лейбл `site` из SD прикрепляется ко всем `node_*`-метрикам таргета
- Порт по умолчанию 9100; path из URL → `__metrics_path__`,
  https → `__scheme__`
- Здоровье цели — встроенная метрика `up{job="node-exporter"}`;
  `monitoring_collector_*` для серверных метрик не заводятся
- В дашбордах `rate()` разрешён ТОЛЬКО для `node_*` (counter);
  метрики проекта `monitoring_*` — gauge, без rate()/increase()

### Алерты (ЭПИК-7, ADR-006)
- Правила — `prometheus/alerts.yml` (rules-as-code): SiteDown (3m, critical),
  SiteSslExpiringSoon (<14d, warning) / SiteSslExpiringCritical (<3d,
  critical), SiteTrafficDrop (<50% к `offset 24h` при вчерашних >10, 2h),
  CollectorFailing (15m), NodeExporterDown (5m), MonitoringAppDown
  (absent, 5m, critical)
- Доставка — Alertmanager (`monitoring-alertmanager`, 127.0.0.1:9093) →
  Telegram Bot API; группировка `[alertname, site]`, repeat 12h,
  `send_resolved: true`; critical глушит warning того же сайта (inhibit)
- Секреты `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` — только `.env`;
  подстановка в конфиг — entrypoint-скриптом при старте контейнера
- `monitoring_*`-метрики в выражениях — gauge, БЕЗ rate()/increase();
  сравнение дней — только `offset`

### Логирование
- structlog, JSON; события: `http_request`, `collector_cycle_start/end`,
  `collector_site_success/error`, `collector_retry`, `collector_registered`
- Каждый except: `logger.error(..., error=..., operation=...)`

## Порты (все 127.0.0.1)

| Сервис | Порт | Назначение |
|--------|------|-----------|
| monitoring-app | 8088 | API + /metrics |
| prometheus | 9091 | TSDB (retention 90d) + правила алертов |
| alertmanager | 9093 | маршрутизация алертов → Telegram [ЭПИК-7] |
| grafana | 3300 | визуализация (admin, anonymous off) |

## Запуск и проверки

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest && .venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy .

cp .env.example .env && chmod 600 .env
docker compose -p monitoring build --no-cache
docker compose -p monitoring up -d --force-recreate
```
