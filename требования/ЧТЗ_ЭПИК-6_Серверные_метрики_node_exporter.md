# ЧТЗ: Серверные метрики node_exporter (ЭПИК-6)

- Дата: 2026-08-14
- Маршрут: Аналитик → Архитектор ✅ → Аналитик → Разработчик → Тестировщик → DevOps
- Основание: BACKLOG.md ЭПИК-6 (TASK-060/061), ADR-005

## 1. Цель

CPU/RAM/disk серверов сайтов видны в Prometheus и на дашборде «Обзор сайта».
Цели node_exporter берутся из `config/sites.yml` (поле `node_exporter_url`)
без дублирования конфигурации и без рестарта Prometheus при изменении парка
сайтов.

## 2. Требования

### Функциональные

1. `GET /api/v1/sd/node-exporter` отдаёт HTTP SD-ответ: список групп
   `[{"targets": ["<host>:<port>"], "labels": {"site": "<домен>"}}]`
   только для сайтов с заполненным `node_exporter_url` (ADR-005 §1–2).
2. Нормализация URL → target/labels: порт по умолчанию 9100; path ≠ `/` →
   лейбл `__metrics_path__`; scheme `https` → лейбл `__scheme__="https"`.
3. Валидатор `node_exporter_url` в `SiteConfig` (для непустых значений):
   схема `http`/`https`, непустой host, без query/fragment.
4. `prometheus/prometheus.yml`: job `node-exporter` с `http_sd_configs`
   (`http://app:8088/api/v1/sd/node-exporter`, `refresh_interval: 60s`).
5. Дашборд «Обзор сайта»: +4 панели (новый ряд) — CPU %, RAM %, Диск `/` %,
   Load average 1m (PromQL зафиксирован ADR-005 §4); фильтр `site="$site"`,
   datasource uid `prometheus`.
6. Ни одного сайта с URL → SD-ответ `[]` (job без таргетов, не ошибка).
7. Ошибка конфигурации sites.yml на SD-эндпоинте → 500 (как в /api/v1/sites).

### Технические

- Новый `app/api/v1/sd.py` (роутер, логирование количества таргетов);
  подключение в `app/main.py` рядом с `sites`.
- `app/sites.py`: валидатор `node_exporter_url` (urlsplit).
- `prometheus/prometheus.yml`, `config/sites.yml` (пример), дашборд
  `site-overview.json` (+4 панели, ids 9–12, ряд y=22).
- Тесты: новые `tests/test_sd_api.py`, `tests/test_prometheus_config.py`;
  обновление `tests/test_sites_config.py` (валидатор) и
  `tests/test_dashboards.py` (12 панелей; rate() разрешён только для
  `node_*`, для `monitoring_*` — по-прежнему запрет).
- Новых секретов/настроек `.env` нет.

## 3. Декомпозиция

| ID | Задача | Тип |
|----|--------|-----|
| TASK-060-B1 | Валидатор `node_exporter_url` в `SiteConfig` + тесты | BCK |
| TASK-060-B2 | SD-эндпоинт `/api/v1/sd/node-exporter` (нормализация URL, логи) + тесты | BCK |
| TASK-061-F1 | 4 панели серверных метрик в site-overview.json + обновление test_dashboards.py | FRT |
| TASK-060-INF | prometheus.yml (http_sd job), sites.yml (пример), test_prometheus_config.py | INF |

## 4. Критерии приёмки

1. `pytest && ruff check . && ruff format --check . && mypy .` — зелёные.
2. SD-эндпоинт корректен: пусто без URL; target/лейблы (порт 9100 по
   умолчанию, `__metrics_path__`, `__scheme__`); 500 при битом конфиге;
   покрытие новых файлов ≥ 60%.
3. Валидатор: `ftp://`/без схемы/пустой host/query → понятные ошибки.
4. prometheus.yml содержит job `node-exporter` с http_sd на
   `app:8088` (проверяется тестом).
5. Дашборд: 12 панелей, 4 новых с `node_*`-метриками и фильтром
   `site="$site"`; rate() только у `node_*`-expr.
6. Существующие панели/коллекторы/дашборд «Все сайты» не изменили
   поведение (старые тесты зелёные, кроме осознанных обновлений структуры).
7. `docker compose -p monitoring` полная пересборка — все healthy (DevOps).

## 5. Ограничения и риски

- Достижимость node_exporter с VPS — вне скоупа (наблюдаемо `up=0`).
- Самоподписанный https на node_exporter — job-level TLS не настраиваем
  (задокументировано в ADR-005).
- Сайты в sites.yml сейчас без `node_exporter_url` → после деплоя job
  существует, но без таргетов (активация — добавлением поля).

## 6. Вопросы

Открытых нет: схема discovery и PromQL зафиксированы ADR-005.
