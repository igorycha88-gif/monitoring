# ADR-005: Серверные метрики node_exporter через http_sd (ЭПИК-6)

- Статус: принято
- Дата: 2026-08-14
- Связанные: ADR-001 (модель метрик), ARCHITECTURE.md, BACKLOG.md ЭПИК-6 (TASK-060/061)

## Контекст

TASK-060: Prometheus должен скрейпить node_exporter целей, указанных в
`config/sites.yml` (поле `node_exporter_url: str | None` уже есть в
`SiteConfig`), чтобы CPU/RAM/disk были видны в Prometheus. TASK-061: панели
серверных метрик в дашборде «Обзор сайта».

Ограничения:

- sites.yml — единственный источник правды о парке сайтов (4–10 шт.);
- метрики node_exporter (`node_*`) не содержат лейбла `site`, а весь
  дашборд-слой проекта фильтрует по `site="$site"`;
- Prometheus и app уже в одной docker-сети `monitoring-net`.

## Решения

### 1. Discovery: Prometheus http_sd → endpoint приложения

Добавить в `prometheus/prometheus.yml` job:

```yaml
- job_name: node-exporter
  http_sd_configs:
    - url: http://app:8088/api/v1/sd/node-exporter
      refresh_interval: 60s
```

Приложение отдаёт стандартный HTTP SD-ответ (`app/api/v1/sd.py`,
`GET /api/v1/sd/node-exporter`):

```json
[
  {"targets": ["203.0.113.10:9100"], "labels": {"site": "example.com"}}
]
```

- Один сайт без `node_exporter_url` → не попадает в ответ.
- Ни одного сайта с URL → `[]` (job просто без таргетов).
- SD-лейблы Prometheus прикрепляет ко **всем** метрикам таргета →
  `node_*` получают лейбл `site` — единый фильтр дашбордов работает.
- Смена парка сайтов = правка sites.yml, Prometheus подхватит сам
  (refresh 60s), без рестарта и правки prometheus.yml.

### 2. Нормализация `node_exporter_url` → target + labels

Валидатор в `SiteConfig` (только для непустых значений): схема `http`/`https`,
непустой host. Таргет и лейблы выводятся из URL:

| Часть URL | Правило |
|-----------|---------|
| host, port | `host:port`; порт по умолчанию — **9100** |
| path | непустой и ≠ `/` → лейбл `__metrics_path__` (иначе дефолт `/metrics`) |
| scheme https | лейбл `__scheme__="https"` (иначе дефолт http) |

Запрос/фрагмент в URL запрещены валидатором.

### 3. Без Python-коллектора

Prometheus скрейпит node_exporter напрямую (pull). Приложение лишь раздаёт
discovery. Здоровье таргета наблюдаемо встроенной метрикой
`up{job="node-exporter", site=...}`; `monitoring_collector_*` для серверных
метрик не заводим (нет цикла сбора). Схема в ARCHITECTURE.md
(упоминание `collectors/servers.py`) корректируется на http_sd.

### 4. Дашборд (TASK-061): 4 панели в «Обзор сайта» (новый ряд)

| Панель | PromQL |
|--------|--------|
| CPU, % | `100 * (1 - avg by (site) (rate(node_cpu_seconds_total{mode="idle",site="$site"}[5m])))` |
| RAM, % | `100 * (1 - node_memory_MemAvailable_bytes{site="$site"} / node_memory_MemTotal_bytes{site="$site"})` |
| Диск `/`, % | `100 * (1 - node_filesystem_avail_bytes{site="$site",mountpoint="/",fstype!~"tmpfs|overlay|squashfs"} / node_filesystem_size_bytes{site="$site",mountpoint="/"})` |
| Load average 1m | `node_load1{site="$site"}` |

Правило валидации дашбордов «без rate()/increase()» уточняется: запрет
действует на метрики проекта `monitoring_*` (все gauge); для `node_*`
counter `rate()` корректен и разрешён.

## Альтернативы (отклонены)

- **Статический prometheus.yml** — дублирование парка сайтов в двух файлах,
  синхронизация руками, рестарт Prometheus при каждом изменении.
- **file_sd + генерация файла** — нужен шаг генерации, рассинхрон при ручной
  правке, volume между app и Prometheus.
- **Python-коллектор `collectors/servers.py`** (упомянут в раннем наброске
  схемы) — двойной перенос данных (node_exporter → app → Prometheus),
  лишний хоп и код; pull-модель Prometheus проще.
- **Relabel по имени хоста вместо SD-лейбла** — требует маппинга host→site
  в prometheus.yml, снова дублирование конфигурации.

## Риски

- node_exporter недостижим с VPS (файрвол цели) → таргет `up=0`, наблюдаемо
  в Prometheus Targets; настройка целей — вне скоупа эпика.
- Самоподписанный https на node_exporter → нужен job-level TLS-конфиг;
  сейчас дефолт (обычный http в приватной сети), задокументировано.
- Если у цели в metrics уже есть лейбл `site` — конфликт resolved стандартно
  (`instance`-префикс), на практике node_exporter такой лейбл не отдаёт.

## Последствия

- Новые файлы: `app/api/v1/sd.py`, `tests/test_sd_api.py`,
  `tests/test_prometheus_config.py`.
- Затрагиваются: `app/sites.py` (валидатор URL), `app/main.py` (роутер),
  `prometheus/prometheus.yml` (job + http_sd), `grafana/dashboards/
  site-overview.json` (+4 панели), `tests/test_dashboards.py` (структура +
  правило rate), `tests/test_sites_config.py` (валидатор),
  `config/sites.yml` (пример), `ARCHITECTURE.md`.
- Новых секретов и настроек `.env` нет; поведение существующих коллекторов
  и дашборда «Все сайты» не меняется.
