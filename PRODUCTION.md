# PRODUCTION.md — Runbook прода «Мониторинг сайтов»

> Единый документ «как всё устроено на проде». Актуализован: 2026-08-22.
> Архитектура: `ARCHITECTURE.md` (детали решений — в `требования/ADR-*`).
> Деплой: `SKILL_DEVOPS_PROD.md` + `PIPELINE_PROD.js`.

## 1. Где и как живёт

| Параметр | Значение |
|---|---|
| VPS | 130.49.129.241 (SSH root по ключу; хост-ключ `SHA256:g3xVlty…tbLY`) |
| Директория | `/root/monitoring/` |
| Compose project | `monitoring` (контейнеры `monitoring-*`, сеть `monitoring-net`) |
| Доступ извне | **только SSH-туннель** (см. §2) |
| Сосед | ЧУЖОЕ приложение (smarttraffic-*) — **НЕ ТРОГАТЬ** (AGENTS.md 0.1) |
| Бэкапы | cron root `0 3 * * *` → `scripts/backup.sh` → `/root/backups/monitoring/` (tar.gz+sha256, ротация 14) |
| fail2ban | jail sshd, только порт 22 |

## 2. Порты и доступ (ВСЕ на 127.0.0.1!)

| Сервис | Порт | Проверка |
|---|---|---|
| monitoring-app (API + /metrics) | 8088 | `curl -sf http://127.0.0.1:8088/health` |
| Prometheus | 9091 | `curl -sf http://127.0.0.1:9091/-/healthy` |
| Grafana | 3300 | `curl -sf http://127.0.0.1:3300/api/health` |
| Alertmanager | 9093 | `curl -sf http://127.0.0.1:9093/-/healthy` |

SSH-туннель (основной способ доступа):

```bash
ssh -L 3300:127.0.0.1:3300 -L 8088:127.0.0.1:8088 \
    -L 9091:127.0.0.1:9091 -L 9093:127.0.0.1:9093 root@130.49.129.241
# Grafana http://localhost:3300 (admin, anonymous off)
```

Контроль: `ss -tlnp | grep -E ':(8088|9091|3300|9093) '` — только 127.0.0.1.

## 3. Сайты и источники данных (факт: 4 сайта)

| Сайт | uptime/ssl | Метрика | Вебмастер | site-metrics (business_*) |
|---|---|---|---|---|
| example.com | ✅ | — | — | — (health только) |
| example.org | ✅ | — | — | — (health только) |
| da-dryclean.ru | ✅ | — | ✅ | ✅ 4 kind |
| эвакуация.online | ✅ | ✅ counter 111456265 | ✅ | ✅ 4 kind |

Конфиг: `config/sites.yml` (в контейнере `/srv/config`, ro). Источники
включаются по наличию полей (`metrika_counter_id`, `webmaster_host_id`,
`metrics_urls`, `node_exporter_url`).

## 4. Поток данных

```
Сайты/Яндекс API ─▶ collectors/ (APScheduler) ─▶ /metrics (app)
                                                    │ scrape 15s
Вебмастер API ─▶ SQLite (volume monitoring-db) ─▶ рендер search_* в /metrics
                                                    ▼
node_exporter сайтов ─(http_sd)─▶ Prometheus 9091 (retention 400d)
Сайты /metrics/* ─(relay app + персайтный ключ)─▶ jobs site-*
                                                    │
                                    rules/alerts ─▶ Alertmanager 9093 ─▶ Telegram
                                                    ▼
                                              Grafana 3300 (4 дашборда)
```

Расписания: uptime 60с; ssl 1ч; metrika 300с (`METRIKA_INTERVAL_SECONDS`);
webmaster **07:00/19:00 МСК** (`WEBMASTER_CRON_HOUR=7,19` — данные Яндекса
суточные, ADR-008); site-metrics health 60с; рендер search_* из SQLite —
фоновый поток 60с (`WEBMASTER_RENDER_REFRESH_SECONDS`).

## 5. Volumes

| Volume | Что внутри | Бэкапится? |
|---|---|---|
| `monitoring_monitoring-db` | SQLite Вебмастера (история поиска) | нет (решение владельца) — восстановим backfill'ом |
| `monitoring_prometheus-data` | TSDB (retention 400d) | нет |
| `monitoring_grafana-data` | БД Grafana | нет (дашборды в git + provisioning) |
| `monitoring_alertmanager-data` | состояние Alertmanager | нет |

⚠️ **Свежий деплой с новым volume `monitoring-db` = пустая история Вебмастера**
(дашборды «без динамики», хотя сбор работает) → сразу делать backfill (§7.2).

## 6. Секреты (.env на VPS, chmod 600; НИКОГДА не в git/логи)

| Переменная | Назначение |
|---|---|
| `YANDEX_METRIKA_OAUTH_TOKEN` | Метрика stat/v1 (срок ~1 год; клиентские креды `YANDEX_OAUTH_CLIENT_ID/SECRET`) |
| `YANDEX_WEBMASTER_OAUTH_TOKEN` | Вебмастер — общий fallback |
| `YANDEX_WEBMASTER_OAUTH_TOKENS` | JSON `{домен: токен}` — персайтные токены Вебмастера |
| `SITE_METRICS_API_KEY(S)` | X-Monitoring-Key: общий (персайтные JSON) |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | алерты (подставляются entrypoint'ом Alertmanager) |
| `GRAFANA_ADMIN_PASSWORD` | пароль Grafana |
| `WEBMASTER_DB_PATH` | подменяется compose на `/var/lib/monitoring/webmaster.db` |

Проверка без раскрытия секрета: `docker exec monitoring-app printenv <ИМЯ> | wc -c`.

## 7. Процедуры

### 7.1. Полная верификация стека (одной командой)

```bash
cd /root/monitoring && python3 scripts/verify_prod.py
```

Проверяет: коллекторы всех сайтов (success/errors) → значения в Prometheus
(uptime, ssl_days, search_*, business_*) → свежесть scrape → Grafana
(health, datasource, все 4 дашборда через datasource proxy). Ожидание:
`ИТОГО: N OK, 0 FAIL`, exit 0.

### 7.2. Backfill истории Вебмастера (после свежего деплоя / потери БД)

```bash
docker exec -e WEBMASTER_HISTORY_DAYS=31 monitoring-app \
    python scripts/backfill_webmaster_daily.py
# успех: backfill_finished, rows_written > 0; рендер подхватит за 60 с
# проверка диапазона:
docker exec monitoring-app python -c \
  "import sqlite3; print(list(sqlite3.connect('/var/lib/monitoring/webmaster.db').execute('SELECT site, MIN(date), MAX(date), COUNT(*) FROM webmaster_daily GROUP BY site')))"
```

### 7.3. Ручная проверка диапазона/токенов

```bash
# токен Метрики валиден? (наш счётчик → 200; чужой → 404; это норма)
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: OAuth $(docker exec monitoring-app printenv YANDEX_METRIKA_OAUTH_TOKEN)" \
  'https://api-metrika.yandex.net/stat/v1/data?ids=111456265&metrics=ym:s:visits&date1=today&date2=today'
# ⚠️ Management API /counters может отдавать пусто при валидном токене (403-особенность прав) — НЕ признак отказа
```

### 7.4. Восстановление из бэкапа

`scripts/restore.sh <архив> [dest] [--with-env]` — sha256-проверка; `.env`
только с `--with-env`. Скачивание локально:
`scp 'root@130.49.129.241:/root/backups/monitoring/monitoring-*.tar.gz' ./backups/`

### 7.5. Откат деплоя

См. `SKILL_DEVOPS_PROD.md → Откат (Rollback)`.

## 8. Диагностика «нет данных» (уроки инцидента 2026-08-22)

| Симптом | Причина | Действие |
|---|---|---|
| Метрика: visits=0, коллектор success=1, сайт живой | тег счётчика НЕ установлен на сайте | задача команды сайта; проверка тега: `curl -s https://<домен> \| grep -c mc.yandex` (0 = нет) |
| Дашборды Вебмастера без динамики | пустой volume monitoring-db (свежий деплой) | §7.2 backfill |
| `monitoring_search_daily_*` только 1 день | так задумано (ADR-011: gauge последнего завершённого дня) | не баг; многодневная история — range-запрос + promtool-импорт |
| Вебмастер «молчит» весь день | расписание 07:00/19:00 МСК — суточные данные Яндекса | проверить `docker logs monitoring-app \| grep webmaster` |
| job site-* up=0 | эндпоинт сайта недоступен/403 | данные (не авария коллектора); проверить relay: `curl -sf http://127.0.0.1:8088/api/v1/relay/site-metrics/<kind>/<домен> -H 'X-...'` |

## 9. Errata (имена метрик)

В исторических ЧТЗ встречается `monitoring_ssl_cert_days_left` — реальное
имя: **`monitoring_ssl_days_left`**. Живой источник имён: `app/metrics.py`,
белый список `tests/test_dashboards.py`.

## 10. Контроль изоляции (перед ЛЮБЫМ действием)

```bash
docker ps --format '{{.Names}} {{.Status}}'   # smarttraffic-* не меняются
ss -tlnp | grep -E ':(8088|9091|3300|9093) '   # только 127.0.0.1
crontab -l                                     # только наш backup-cron
```
