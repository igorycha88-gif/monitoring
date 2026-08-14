# ЧТЗ: ЭПИК-3 — Grafana-дашборды v1

- Версия: 1.0
- Дата: 2026-08-14
- Приоритет: High
- Статус: Согласовано
- Основание: BACKLOG.md ЭПИК-3 (TASK-030, TASK-031); ARCHITECTURE.md; app/metrics.py

---

## 1. Цели и задачи

### 1.1 Бизнес-цель
Первая визуализация MVP-данных: uptime/SSL/трафик по каждому сайту и обзор
всех сайтов парка (4–10 сайтов).

### 1.2 Пользовательская ценность
Открыл Grafana → выбрал сайт → видишь доступность, время ответа, SSL,
трафик; либо открыл «Все сайты» → таблица статусов + спарклайны времени
ответа по каждому сайту.

### 1.3 Метрики успеха
- 2 дашборда загружаются через provisioning (без ручного импорта)
- Переменная `site` работает (выпадающий список сайтов)
- Данные отображаются при работающих коллекторах ЭПИК-1/2

## 2. Функциональные требования

### 2.1 User Stories

**US-1 (TASK-030):** Как владелец, я выбираю сайт в переменной `site`
и вижу его состояние.
- Given: коллекторы uptime/ssl/metrika работают
  When: открываю дашборд «Обзор сайта»
  Then: все панели фильтруются по `$site`, данные видны

**US-2 (TASK-031):** Как владелец, я открываю «Все сайты» и вижу парк целиком.
- Given: в sites.yml 4–10 сайтов
  When: открываю обзорный дашборд
  Then: таблица показывает все сайты; под ней — спарклайны времени ответа
  (одна маленькая панель на сайт, повторение по переменной)

## 3. Нефункциональные требования

- Только уже существующие метрики (никаких новых collector_* запросов сверх
  re-export); кардинальность не меняется
- Никаких внешних плагинов Grafana (только встроенные панели: timeseries,
  stat, table)
- Секретов в дашбордах нет; datasource по uid `prometheus` (provisioning)
- Datasource hardcoded по uid: `{"type": "prometheus", "uid": "prometheus"}`

## 4. Техническая архитектура

### 4.1 Используемые метрики (все уже существуют, app/metrics.py)

| Метрика | Лейблы | Тип |
|---------|--------|-----|
| monitoring_uptime_status | site | gauge |
| monitoring_uptime_response_seconds | site | gauge |
| monitoring_uptime_response_code | site | gauge |
| monitoring_ssl_days_left | site | gauge |
| monitoring_site_visits_total | site, source | gauge (без rate()!) |
| monitoring_site_visitors | site, source | gauge |
| monitoring_collector_success | source, site | gauge |
| monitoring_collector_duration_seconds | source, site | gauge |
| monitoring_collector_errors_total | source, site | counter |

Важно: `monitoring_site_visits_total` — gauge (нарастающий итог дня),
запрашивать напрямую, БЕЗ `rate()`/`increase()`.

### 4.2 Файлы

```
grafana/dashboards/site-overview.json   # TASK-030 «Обзор сайта»
grafana/dashboards/all-sites.json       # TASK-031 «Все сайты»
tests/test_dashboards.py                # валидация JSON-дашбордов
ARCHITECTURE.md                         # обновить раздел grafana/
требования/BACKLOG.md                   # статус ЭПИК-3 после деплоя
```

Provisioning уже настроен (provider `default`, path
`/var/lib/grafana/dashboards`, volume смонтирован в docker-compose).

### 4.3 Интервалы (для refresh панелей)

uptime — 60 с, metrika — 300 с, ssl — 3600 с → refresh дашборда 1m,
диапазон по умолчанию 24h.

## 5. Дашборды Grafana

### 5.1 «Обзор сайта» (site-overview.json, uid `site-overview`)

Переменные:
- `site` (type=query): `label_values(monitoring_uptime_status, site)`,
  single-value, без All

Панели (сверху вниз):
1. **Доступность** (stat): `monitoring_uptime_status{site="$site"}`;
   mappings: 1→"UP"(green)/0→"DOWN"(red); thresholds 0/1
2. **HTTP-код** (stat): `monitoring_uptime_response_code{site="$site"}`
3. **Дней до истечения SSL** (stat): `monitoring_ssl_days_left{site="$site"}`;
   thresholds: red<7, yellow<30, green≥30; unit `d` — значение в сутках
4. **Здоровье коллекторов** (stat): `min(monitoring_collector_success{site="$site"})`;
   1→OK/0→ERR
5. **Время ответа** (timeseries): `monitoring_uptime_response_seconds{site="$site"}`;
   unit seconds; legend {{site}}
6. **Визиты, нарастающий итог дня** (timeseries):
   `monitoring_site_visits_total{site="$site", source="metrika"}` (без rate!)
7. **Посетители, нарастающий итог дня** (timeseries):
   `monitoring_site_visitors{site="$site", source="metrika"}`
8. **Длительность сбора коллекторов** (timeseries):
   `monitoring_collector_duration_seconds{site="$site"}`; legend {{source}}

### 5.2 «Все сайты» (all-sites.json, uid `all-sites`)

Переменные:
- `site` (type=query): `label_values(monitoring_uptime_status, site)`,
  multi + includeAll, default `all` — для фильтра таблицы и repeat

Панели:
1. **Статусы сайтов** (table, instant-запросы):
   - `monitoring_uptime_status` — колонка «Доступен» (value mappings UP/DOWN)
   - `monitoring_uptime_response_code` — «HTTP-код»
   - `monitoring_uptime_response_seconds` — «Время ответа, с»
   - `monitoring_ssl_days_left` — «SSL, дней»
   - `monitoring_site_visits_total{source="metrika"}` — «Визиты (сегодня)»
   - `min by (site) (monitoring_collector_success)` — «Коллекторы»
   Все targets: фильтр `site=~"$site"`, format table, merge в одну таблицу
   по колонке site (join by field site)
2. **Время ответа — спарклайны** (timeseries, repeat by `site`):
   `monitoring_uptime_response_seconds{site="$site"}`, legend {{site}},
   small grid (2 колонки), отключены оси/легенда — компактные мини-графики
   по одному на сайт (repeat по multi-переменной site)

## 6. Декомпозиция

### Frontend
- TASK-FRT-001: site-overview.json (панели 1–8, переменная site)
- TASK-FRT-002: all-sites.json (таблица + repeat-спарклайны)

### Testing
- TASK-TST-001: tests/test_dashboards.py — см. раздел 7

### Documentation
- TASK-DOC-001: обновить ARCHITECTURE.md (раздел grafana), BACKLOG-статус
  после DevOps

### Infrastructure
- Не требуется (provisioning/volumes уже настроены в ЭПИК-0)

## 7. Тестирование

Дашборды — декларативные JSON; автотесты = валидация структуры:

1. Оба файла — валидный JSON, обязательные поля: uid (уникальные), title,
   schemaVersion, datasource uid `prometheus` во всех targets/панелях
2. Каждый expr содержит ТОЛЬКО метрики из белого списка (приложение
   знает их все — матчинг по именам из app/metrics.py + агрегатам
   `min by`) — защита от опечаток в PromQL
3. Все expr с фильтром сайта используют `site=~"$site"` или `site="$site"`
4. В «Обзоре сайта» ровно 8 панелей, есть переменная site типа query
5. В «Все сайты» есть панель-таблица и повторяемая панель (repeat: "site")
6. Ни в одном expr нет `rate(`/`increase(` по gauge-метрикам Метрики

## 8. Риски и зависимости

- До первого сбора данных переменная `site` пуста → дашборды без данных,
  это ожидаемо (после старта коллекторов данные появятся)
- Спарклайны через repeat по переменной — штатный механизм, при 10 сайтах
  сетка 2×5 остаётся читаемой

## 9. Критерии приёмки

1. `pytest && ruff check . && ruff format --check . && mypy .` — зелёные
2. `grafana/dashboards/site-overview.json` и `all-sites.json` существуют
   и проходят валидацию из раздела 7
3. После полной пересборки: Grafana healthy, оба дашборда доступны через
   API `GET /api/search?query=` (проверка DevOps), provisioning без ошибок
   в логах grafana
4. Существующее приложение и порты не затронуты (8088/9091/3300 — наши)

## Маршрутизация

**Архитектор:** НЕ ТРЕБУЕТСЯ (новых источников данных нет, метрики
существуют; по правилам BACKLOG.md Архитектор обязателен только для
ЭПИК-0/1/2/5)
**Исполнитель:** Разработчик (дашборды Grafana с новыми данными → FRT)
**Обоснование:** SKILL_ANALYST.md Маршрутизация Шаг 2: «Дашборды Grafana
(новые данные) → Разработчик»
