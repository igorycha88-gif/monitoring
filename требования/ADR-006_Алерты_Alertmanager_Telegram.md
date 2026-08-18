# ADR-006: Контур алертов — правила Prometheus + Alertmanager + Telegram

- Статус: Принято
- Дата: 2026-08-18
- Эпик: ЭПИК-7 (TASK-070, TASK-071)
- Предыдущие решения: ADR-001 (каркас), ADR-002 (uptime/SSL), ADR-003 (Метрика), ADR-005 (node_exporter)

## Контекст

Мониторинг уже собирает метрики (uptime, SSL, трафик Метрики, поиск Вебмастера,
серверные метрики node_exporter), но реакция на инциденты — только ручной
просмотр дашбордов. Нужны автоматические алерты и доставка уведомлений
в Telegram (выбор пользователя).

## Решение

Классический конур Prometheus → Alertmanager → Telegram Bot API:

```
monitoring-app (метрики) ──▶ Prometheus (правила alerts.yml, for-окна)
                              │ firing alerts
                              ▼
                          Alertmanager (группировка, inhibit, repeat)
                              │ sendMessage
                              ▼
                          Telegram Bot API → чат владельца
```

### 1. Правила алертов — `prometheus/alerts.yml`

Загружаются Prometheus'ом (`rule_files`), fire виден в UI Prometheus
(`127.0.0.1:9091/alerts`) — соответствует DoD TASK-070.

| Алерт | Выражение (суть) | Окно `for` | Severity |
|-------|------------------|-----------|----------|
| `SiteDown` | `monitoring_uptime_status == 0` | 3m | critical |
| `SiteSslExpiringSoon` | `monitoring_ssl_days_left < 14` | 1h | warning |
| `SiteSslExpiringCritical` | `monitoring_ssl_days_left < 3` | 1h | critical |
| `SiteTrafficDrop` | визиты < 50% от `offset 24h`, при вчерашних > 10 | 2h | warning |
| `CollectorFailing` | `monitoring_collector_success == 0` | 15m | warning |
| `NodeExporterDown` | `up{job="node-exporter"} == 0` | 5m | warning |
| `MonitoringAppDown` | `absent(monitoring_uptime_status)` | 5m | critical |

Обоснование окон: uptime-цикл 60 с → SiteDown требует 3 подряд неудачных
проверки (защита от флапа); SSL/трафик — медленные процессы, окна длинные;
`CollectorFailing` 15м — collector_success 0 держится дольше ретраев (backoff
3 попытки × 1-2-4 с в пределах цикла).

`SiteTrafficDrop` — сравнение нарастающего итога дня с тем же временем суток
вчера (`offset 24h`, обе стороны gauge без rate()) — корректно, т.к. метрика
обнуляется в полночь UTC и сравниваются одинаковые доли суток. Guard
«вчера > 10 визитов» отсекает шум ранним утром и на молодых сайтах.

Аннотации (`summary`, `description`) — на русском, содержат `{{ $labels.site }}`
и `{{ $value }}`.

### 2. Alertmanager — новый сервис `monitoring-alertmanager`

- Образ `prom/alertmanager:v0.27.0` (пин версии; `bot_token_file`/конфиг
  стабильны). Контейнер `monitoring-alertmanager`, сеть `monitoring-net`.
- Порт: **127.0.0.1:9093** — НОВЫЙ порт нашего проекта. Расширяет набор
  «своих» портов (8088/9091/3300 + 9093), наружу НЕ открывается (раздел 0.2
  AGENTS.md). Перед любым деплоем — проверка `ss -tlnp | grep 9093`.
- Подключение: `prometheus.yml → alerting.alertmanagers → alertmanager:9093`.
- Конфиг `prometheus/alertmanager.yml`:
  - `route`: `group_by: [alertname, site]`, `group_wait: 30s`,
    `group_interval: 5m`, `repeat_interval: 12h` (анти-спам);
  - `receiver: telegram` → `telegram_configs` (`send_resolved: true`);
  - `inhibit_rules`: critical (SSL critical / SiteDown) глушит warning
    по тому же `site` и `alertname`-группе.

### 3. Секреты Telegram

- `TELEGRAM_BOT_TOKEN` и `TELEGRAM_CHAT_ID` — только в `.env` (chmod 600),
  как и `GRAFANA_ADMIN_PASSWORD`. В репозитории — только `.env.example`
  с пустыми значениями.
- Alertmanager не умеет env-подстановку в конфиге → в compose у сервиса
  переопределяется `entrypoint`: shell-скрипт подставляет значения env в
  шаблон `alertmanager.yml.tmpl` и стартует alertmanager с готовым конфигом.
  Токен не попадает в файлы репозитория; видимость через `docker inspect`
  эквивалентна чтению `.env` на хосте (тот же уровень доверия).
- Если токен/chat_id не заданы — alertmanager стартует с конфигом, где
  токен-заглушка: контур алертов живёт (fire видно в Prometheus UI),
  доставка в Telegram активируется после заполнения `.env` + `docker compose
  up -d alertmanager`. Ошибка отправки логируется alertmanager'ом.

### 4. Что НЕ делаем (отклонённые альтернативы)

- **Grafana Alerting** — правила вне кода (UI), хуже ревью/тестов; отклонено.
- **Свой Python-алертер** (poll `/api/v1/alerts`) — дублирует Alertmanager,
  свой код = свои баги; отклонено.
- **E-mail** — пользователь выбрал Telegram; при необходимости добавляется
  одним receiver'ом в alertmanager.yml.
- Алерты на `monitoring_search_*` (Вебмастер) — за рамками ЭПИК-7 (нет
  в DoD TASK-070); при необходимости — отдельная задача.

### 5. Тестируемость (Правило 8)

- `tests/test_alerts.py`: валидация YAML правил/alertmanager-конфига,
  наличие всех алертов DoD, severity/labels/annotations, корректность
  выражений (структурные проверки), wiring compose/prometheus.yml
  (rule_files, volume, alertmanagers, env-передача).
- Autotest `pytest` + `promtool check rules` — шаг DevOps (в образе
  prom/prometheus) — верификация выражений на реальном парсере.
- Метрики/логи: нового Python-кода нет (инфраструктурный контур);
  логирование — средствами Prometheus/Alertmanager, проверяется на этапе
  DevOps (`docker compose logs`).

## Последствия

- +1 контейнер и +1 порт (9093, только 127.0.0.1) — обновить PIPELINE_PROD
  pre-flight при прод-деплое.
- Простой контур: без Python-кода, правила декларативны и версионируются.
- Изменение порогов = правка `prometheus/alerts.yml` + рестарт Prometheus.
- Telegram-доставка требует одноразовых действий пользователя (создать бота,
  получить chat_id — инструкция в ЧТЗ и `.env.example`).
