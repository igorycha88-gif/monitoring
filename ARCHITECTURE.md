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
│  │  ├── /api/v1/sd/node-exporter — HTTP SD [ЭПИК-6]          │
│  │  ├── /api/v1/sd/site-metrics/{kind} — HTTP SD (relay-таргеты) [ЭПИК-9, ADR-010] │
│  │  ├── /api/v1/relay/site-metrics/{kind}/{site} — proxy с персайтным ключом [ADR-010] │
│  │  └── APScheduler    — расписание коллекторов  │          │
    │  │   ├── collectors/uptime.py    [ЭПИК-1]    │──▶ HTTP/SSL сайтов
    │  │   ├── collectors/metrika.py   [ЭПИК-2]    │──▶ Яндекс.Метрика API
    │  │   ├── collectors/webmaster.py [ЭПИК-5]    │──▶ SQLite [ADR-011]
    │  │   ├── webmaster_export.py     [ADR-011]   │──▶ /metrics из SQLite
    │  │   ├── collectors/sitemetrics.py [ЭПИК-9]  │──▶ health эндпоинтов метрик
│  └──────────────────────┬────────────────────────┘          │
│                         │ /metrics (pull, 15s)              │
│  ┌──────────────────────▼────────────────────────┐          │
│  │ Prometheus (127.0.0.1:9091)                   │          │
│  │   ├── http_sd: /api/v1/sd/node-exporter [ЭПИК-6]
│  │   ├── http_sd: /api/v1/sd/site-metrics/{kind} → relay app [ЭПИК-9, ADR-010]
│  │   ├── rules: /etc/prometheus/alerts.yml [ЭПИК-7]
│  │   └──▶ node_exporter целей (лейбл site из SD) [ЭПИК-6]
│  │   └──▶ jobs site-*: relay app:8088 (без секретов; ключ — relay) [ЭПИК-9]
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
│   ├── metrics.py           # реестр метрик monitoring_* (поиск — рендер из БД, ADR-011)
│   ├── storage.py           # WebmasterStorage: SQLite (WAL) данных Вебмастера [ADR-009]
│   ├── webmaster_export.py  # рендер поисковых метрик из SQLite → /metrics [ADR-011]
│   └── api/v1/
│       ├── health.py        # GET /health
│       ├── sites.py         # GET /api/v1/sites
│       ├── relay.py         # GET /api/v1/relay/site-metrics/{kind}/{site} [ADR-010]
│       └── sd.py            # GET /api/v1/sd/node-exporter [ЭПИК-6], /sd/site-metrics/{kind} [ЭПИК-9]
├── collectors/
│   ├── base.py              # BaseCollector: метрики, логи, ретраи, расписание, parallel
│   ├── uptime.py            # UptimeCollector (HTTP) + SSLCollector (сертификаты) [ЭПИК-1]
│   ├── metrika.py           # MetrikaCollector (визиты/посетители) [ЭПИК-2]
│   ├── webmaster.py         # WebmasterCollector (поисковые запросы) [ЭПИК-5]
│   └── sitemetrics.py       # SiteMetricsCollector: health эндпоинтов метрик [ЭПИК-9]
├── config/
│   └── sites.yml            # сайты: domain + опц. counter_id / host_id / exporter / metrics_urls
├── grafana/
│   ├── dashboards/          # JSON-дашборды [ЭПИК-3]:
│   │   ├── site-overview.json  # «Обзор сайта» (переменная site, 8 панелей)
│   │   ├── all-sites.json      # «Все сайты» (таблица статусов + спарклайны)
│   │   ├── site-business.json  # «Бизнес сайта» (14 панелей, ЭПИК-9)
│   │   └── webmaster.json      # «Вебмастер: поиск по сайту» — единая страница (5 панелей: динамика + топы) [ADR-008, ЧТЗ_Дашборд_Вебмастер_Единый]
│   └── provisioning/        # datasources (uid: prometheus) + dashboards
├── prometheus/
│   ├── prometheus.yml       # scrape (app, node-exporter SD, site-* SD через relay) + rules + alerting
│   ├── prometheus-entrypoint.sh # копия шаблона конфига + retention 400d (секретов нет, ADR-010)
│   ├── alerts.yml           # правила алертов [ЭПИК-7, ADR-006; ЭПИК-9]
│   ├── alertmanager.yml     # конфиг Alertmanager (шаблон с подстановкой) [ЭПИК-7]
│   └── alertmanager-entrypoint.sh
├── scripts/
│   ├── backup.sh            # бэкап конфигов+дашбордов+.env (tar.gz+sha256, ротация 14) [ЭПИК-8]
│   ├── restore.sh           # восстановление из архива (sha256-проверка, .env только с --with-env) [ЭПИК-8]
│   ├── backfill_webmaster_daily.py   # разовый backfill истории Вебмастера в SQLite [ADR-009]
│   └── export_webmaster_history.py   # экспорт истории в OpenMetrics для promtool-импорта [ADR-011]
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
  окно «последняя неделя» — ADR-004); дневная динамика — per-query
  `monitoring_search_daily_clicks` / `monitoring_search_daily_shows`
  `{site, query}` (gauge, 1 точка/день из per-query history,
  `/search-queries/{query_id}/history`, дата = метка времени — ADR-008;
  сайта-уровень = `sum()` по отслеживаемым запросам, лейбл даты НЕ вводится);
  источник метрик поиска — SQLite, рендер `/metrics` кастомным collector'ом
  `WebmasterExporter` без timestamp'ов, окно рендера `WEBMASTER_RENDER_DAYS`
  (35д); прямые записи gauge коллектором удалены — БД единственный источник
  (ADR-011); история в TSDB — разовым promtool-импортом (ADR-011 D4)
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
- Дашборды Вебмастера [ADR-008]: динамика по дням — range-запросы
  `monitoring_search_daily_*`; топы со срезами — переменная `period`
  (7d/30d/365d) + `avg_over_time(...)[$period]`; для топов по позиции —
  `bottomk` (меньшая позиция = лучше); `sum_over_time` по `monitoring_*`
  gauge запрещён (суммирует семплы скрейпов — завышение)

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

### Метрики сайтов за X-Monitoring-Key (ЭПИК-9, ADR-007/ADR-010)
- Сайты отдают метрики через свой nginx (443) по путям
  `/metrics/{tracking,content,node,postgres}` с заголовком
  `X-Monitoring-Key`; конфиг сайта — поле `metrics_urls` в sites.yml
  (только https, kinds из whitelist)
- Ключи — ПЕРСАЙТНЫЕ (ADR-010): `.env` → `SITE_METRICS_API_KEYS`
  (JSON `{"<домен>": "<ключ>"}`); `SITE_METRICS_API_KEY` — fallback.
  Резолв: `Settings.site_metrics_key(domain)`
- Скрейп: Prometheus jobs `site-{kind}` через HTTP SD
  `GET /api/v1/sd/site-metrics/{kind}` → relay-таргеты `app:8088`
  (`__metrics_path__=/api/v1/relay/site-metrics/{kind}/{домен}`); relay
  приложения проксирует запрос к сайту с ключом сайта — Prometheus
  скрейпит БЕЗ секретов; не-2xx/сбой → 502 → up=0
- Health-контроль эндпоинтов: `SiteMetricsCollector` (source=`site-metrics`,
  gauge `monitoring_site_metrics_up/response_code/latency_seconds`
  `{site,kind}`, ключ — персайтный); философия ADR-002: 403/404/5xx/сеть —
  данные (up=0), не ошибка коллектора; цикл 60 с — сам ретрай
- Бизнес-метрики сайта (`business_*`) — gauge, считаются на стороне сайта
  из БД (окна 24ч/1ч), пересчёт раз в 60 с; алерт SiteNoTraffic —
  только `== 0` и `offset`-сравнения, без rate()

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

### Эксплуатация (ЭПИК-8)
- fail2ban на VPS: jail `sshd` (backend systemd, 5 попыток/10м → бан 1ч,
  инкремент ×2 до 1нед), `ignoreip 127.0.0.1/8`; iptables-цепочка `f2b-sshd`
  вешается ТОЛЬКО на порт 22 — чужие порты не затрагиваются.
  SSH-конфиг (PasswordAuthentication) и ufw — не трогаем (решения владельца).
- Бэкапы (VPS): cron root `0 3 * * *` → `scripts/backup.sh` — tar.gz
  (config/ + prometheus/ + grafana/ + docker-compose.yml + Dockerfile + .env)
  в `/root/backups/monitoring/` (0700/0600) + `.sha256`; ротация — 14 копий.
  Лог: `/var/log/monitoring-backup.log` + syslog (`monitoring-backup`).
- Восстановление: `scripts/restore.sh <архив> [dest] [--with-env]` —
  sha256-проверка; `.env` восстанавливается только с `--with-env`
  (без флага существующий `.env` не перезаписывается).
  Volumes (Grafana sqlite, TSDB) НЕ бэкапятся (решение владельца).
- Скачивание бэкапа локально:
  `scp 'root@130.49.129.241:/root/backups/monitoring/monitoring-*.tar.gz' ./backups/`

### Логирование
- structlog, JSON; события: `http_request`, `collector_cycle_start/end`,
  `collector_site_success/error`, `collector_retry`, `collector_registered`
- Каждый except: `logger.error(..., error=..., operation=...)`

## Порты (все 127.0.0.1)

| Сервис | Порт | Назначение |
|--------|------|-----------|
| monitoring-app | 8088 | API + /metrics |
| prometheus | 9091 | TSDB (retention 400d — годовые срезы ADR-008) + правила алертов |
| alertmanager | 9093 | маршрутизация алертов → Telegram [ЭПИК-7] |
| grafana | 3300 | визуализация (admin, anonymous off) |

## Прод-справка (актуализовано 2026-08-22)

> Полный runbook: `PRODUCTION.md`. Ниже — архитектурно значимые факты прода.

### Volumes (docker, project=monitoring)

| Volume | Назначение | Последствие потери |
|---|---|---|
| `monitoring_monitoring-db` | SQLite Вебмастера (`webmaster.db`, ADR-009/011) | теряется долговременная история поиска |
| `monitoring_prometheus-data` | TSDB (retention 400d) | теряется вся история метрик |
| `monitoring_grafana-data` | sqlite Grafana (дашборды также в git, provisioning) | минимально |
| `monitoring_alertmanager-data` | состояние Alertmanager (nflog/silences) | минимально |

**Важно:** при деплое на чистый сервер volume `monitoring-db` создаётся пустым →
дашборды Вебмастера «без динамики», хотя сбор работает. Лечение — разовый
backfill (см. ниже). Именно это дало ложный инцидент «метрики не собираются»
2026-08-22.

### Расписания сбора (факт прода)

| Коллектор | Расписание | Env |
|---|---|---|
| uptime | каждые 60 с | `UPTIME_INTERVAL_SECONDS` |
| ssl | каждый час | `SSL_INTERVAL_SECONDS` |
| metrika | каждые 300 с | `METRIKA_INTERVAL_SECONDS` |
| webmaster | **07:00 и 19:00 МСК** (данные Яндекса обновляются раз в сутки; вечерний прогон дозаполняет лаг, ADR-008) | `WEBMASTER_CRON_HOUR=7,19` |
| site-metrics (health) | каждые 60 с | — |
| Prometheus scrape app | 15 с | `prometheus/prometheus.yml` |
| Рендер search-* из SQLite | фон. поток, 60 с | `WEBMASTER_RENDER_REFRESH_SECONDS` |

### Backfill Вебмастера после свежего деплоя

```bash
docker exec -e WEBMASTER_HISTORY_DAYS=31 monitoring-app \
    python scripts/backfill_webmaster_daily.py   # окно 31 день (max API), upsert
```

### Диагностика типовых «нет данных» (уроки 2026-08-22)

1. **Метрика = 0 визитов, коллектор success=1** — токен валиден, API отдаёт
   пустой отчёт как данные. Проверить, установлен ли тег счётчика на САЙТЕ
   (`curl -s https://<домен> | grep -c mc.yandex`). Тест доступа токена:
   чужой счётчик → 404, свой → 200 (Management API `/counters` может отдавать
   пусто даже при валидном токене — 403-особенность прав, не признак отказа).
2. **Дашборды Вебмастера без истории** → `SELECT MIN(date) FROM webmaster_daily`
   в контейнере; мало дней → backfill (выше).
3. **`monitoring_search_daily_*` только один день** — так задумано (ADR-011):
   gauge рендерит последний завершённый день; многодневная динамика —
   range-история в Prometheus + разовый promtool-импорт
   (`scripts/export_webmaster_history.py`).
4. **Скрипт полной верификации** (коллекторы → Prometheus → Grafana):
   `python3 scripts/verify_prod.py` на VPS.

### Errata имён метрик

Исторические ЧТЗ могут содержать `monitoring_ssl_cert_days_left` —
реальное имя: **`monitoring_ssl_days_left`** (`app/metrics.py`, дашборды,
alerts.yml). Живой источник имён — `app/metrics.py` + `tests/test_dashboards.py`.

## Запуск и проверки

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest && .venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy .

cp .env.example .env && chmod 600 .env
docker compose -p monitoring build --no-cache
docker compose -p monitoring up -d --force-recreate
```
