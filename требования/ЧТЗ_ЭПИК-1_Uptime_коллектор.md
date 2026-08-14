# ЧТЗ: ЭПИК-1 — Uptime-коллектор (HTTP- и SSL-проверки)

## Версия: 1.0
## Дата: 2026-08-14
## Приоритет: High
## Статус: Согласовано (на основе ADR-002)

---

## 1. Цели и задачи

### 1.1 Бизнес-цель
Знать в реальном времени, доступны ли наши сайты, насколько быстро они отвечают
и когда истекают SSL-сертификаты — до того, как заметят пользователи/поисковики.

### 1.2 Пользовательская ценность
В Prometheus появляются ряды `monitoring_uptime_*` и `monitoring_ssl_days_left`
по каждому сайту из `config/sites.yml` — основа дашбордов ЭПИК-3 и алертов ЭПИК-7.

### 1.3 Метрики успеха
- `monitoring_uptime_status{site}` обновляется каждые ~60 сек для всех сайтов
- `monitoring_ssl_days_left{site}` обновляется раз в час
- Лежащий сайт не увеличивает `monitoring_collector_errors_total{source="uptime"}`

## 2. Функциональные требования

### 2.1 User Stories с Acceptance Criteria

**US-1: Как владелец, я хочу знать статус доступности сайта каждую минуту.**
- Given сайт отвечает 2xx/3xx, When прошёл цикл, Then `monitoring_uptime_status{site}=1`,
  `monitoring_uptime_response_code{site}=<код>`, `monitoring_uptime_response_seconds{site}>0`
- Given сайт отвечает 5xx, Then `status=0`, `response_code=<реальный код>`, collector_success=1
- Given сайт не отвечает (timeout/DNS/conn refused/SSL-провал), Then `status=0`,
  `response_code=0`, `response_seconds=<время до провала>`, collector_success=1, цикл продолжается

**US-2: Как владелец, я хочу знать, сколько дней осталось до истечения SSL.**
- Given сертификат валиден N>0 дней, Then `monitoring_ssl_days_left{site}=N` (целое)
- Given сертификат просрочен на M дней, Then `monitoring_ssl_days_left{site}=-M`
- Given хост недоступен/не TLS, Then метрика не обновляется,
  `monitoring_collector_success{source="ssl",site}=0`, `monitoring_collector_errors_total` +1,
  остальные сайты цикла обработаны

**US-3: Как оператор, я хочу, чтобы цикл не растягивался при массовых сбоях.**
- Given все 10 сайтов лежат с таймаутом 10 сек, When цикл, Then длительность цикла ≈ 10 сек
  (parallel), а не 100 сек; последующие циклы не пропускаются из-за наложения

## 3. Нефункциональные требования

### 3.1 Лимиты внешних API
Не применяются (наши сайты). Самоограничение: 1 GET/мин/сайт + 1 TLS/час/сайт;
uptime-проверка — строго 1 попытка за цикл (без ретраев — ADR-002).

### 3.2 Кардинальность
Только лейбл `site` (4–10 значений). Новых динамических лейблов нет.

### 3.3 Безопасность
Секреты не требуются. SSL-контекст с `CERT_NONE` — только в изолированном
read-only хелпере чтения `notAfter` (ADR-002, п.4).

## 4. Техническая архитектура (по ADR-002)

### 4.1 Модель метрик (точно)
```
monitoring_uptime_status{site}            Gauge  1/0
monitoring_uptime_response_seconds{site}  Gauge  сек
monitoring_uptime_response_code{site}     Gauge  HTTP-код; 0 = ответа нет
monitoring_ssl_days_left{site}            Gauge  дней (может быть < 0)
```
Регистрация в `app/metrics.py`.

### 4.2 Коллекторы (`collectors/uptime.py`)
- `UptimeCollector(BaseCollector)`: `source="uptime"`, `parallel=True`,
  `max_retries` НЕ используется (одна попытка), httpx GET `https://{domain}/`,
  `follow_redirects=True`, `verify=True`, таймаут из Settings;
  catch `httpx.HTTPError` → данные «сайт вниз» (не исключение); прочие
  исключения пробрасываются в BaseCollector
- `SSLCollector(BaseCollector)`: `source="ssl"`, `parallel=True`, ретраи
  `retry_on=(TimeoutError, OSError)`; хелпер `_get_ssl_not_after(host, timeout)`
  через `asyncio.open_connection` + `ssl` (CERT_NONE, read-only; сертификат
  читается в DER → парсинг `cryptography>=42`, см. поправку в ADR-002);
  дни = `(not_after - now_utc).days`; ошибки хелпера после ретраев → исключение
- Регистрация в `app/main.py` lifespan: load_sites(Settings.sites_config_path) →
  оба коллектора → `register(scheduler, интервал)`; лог `collector_registered`

### 4.3 API
Без изменений.

### 4.4 Структура файлов
```
изменить: app/metrics.py, app/config.py, app/main.py, collectors/base.py (parallel),
          .env.example, ARCHITECTURE.md
создать:  collectors/uptime.py, tests/test_uptime.py, tests/test_ssl_collector.py
```

### 4.5 Конфигурация
Settings (+ в `.env.example` с дефолтами, без секретов):
```
UPTIME_INTERVAL_SECONDS=60
UPTIME_TIMEOUT_SECONDS=10.0
UPTIME_SUCCESS_MAX_CODE=399
SSL_INTERVAL_SECONDS=3600
SSL_TIMEOUT_SECONDS=10.0
```
`collectors/base.py`: атрибут `parallel: bool = False`; при True — `run_once`
использует `asyncio.gather` с пер-сайт try/except (вынести в `_collect_site_safe`).

## 5. Дашборды Grafana
Вне эпика (ЭПИК-3). PromQL-заготовки — в ADR-002.

## 6. Декомпозиция на задачи

### Backend
**TASK-BCK-010: UptimeCollector** (collectors/uptime.py, app/metrics.py,
app/config.py) — HTTP-проверки + метрики + Settings. Зависимости: —.
**TASK-BCK-011: SSLCollector** (collectors/uptime.py) — TLS-хелпер + метрика.
Зависимости: BCK-010 (общая infra).
**TASK-BCK-013: parallel в BaseCollector + регистрация в lifespan**
(collectors/base.py, app/main.py). Зависимости: —.

### Infrastructure
**TASK-INF-010: .env.example + ARCHITECTURE.md** (уже частично сделано).

### Testing
**TASK-TST-010: tests/test_uptime.py** (respx). **TASK-TST-011:
tests/test_ssl_collector.py** (мок TLS-хелпера). **TASK-TST-013: parallel
режим в tests/test_base_collector.py.**

Порядок: BCK-013 → BCK-010 → BCK-011 → INF-010 → TST-*.

## 7. Тестирование

### 7.1 Unit-тесты
- uptime: 200 → status 1; 301→200 (redirect) → 1; 404/500 → 0 с реальным кодом;
  timeout / DNS / connection refused / SSL-ошибка → 0, code=0, collector_success=1;
  неожиданное исключение → errors_total+1, success=0; проверка лог-событий
- ssl: notAfter через 30 дней → 30; просрочен → отрицательное; хелпер бросает
  OSError после ретраев → errors_total+1, success=0, метрика не записана;
  парсинг формата `notAfter='Aug 20 12:00:00 2026 GMT'`
- base: parallel=True — оба сайта обработаны, один упал — второй в метриках ok;
  parallel=False — поведение как раньше (регрессия)
- config: новые ключи читаются, дефолты верны

### 7.2 Моки
respx для httpx; `monkeypatch` для `asyncio.open_connection`/хелпера notAfter.
Реальная сеть в тестах запрещена.

## 8. Риски и зависимости
См. ADR-002 (таблица рисков). Зависимость: ЭПИК-0 (готов).

## 9. Критерии приёмки
- [ ] Все 4 метрики присутствуют в `/metrics` после первого цикла
- [ ] US-1/US-2/US-3 покрыты автотестами (7.1), покрытие новых файлов ≥ 60%
- [ ] `pytest && ruff check . && mypy .` зелёные
- [ ] Логи: collector_cycle_start/end, collector_site_success/error, collector_registered
- [ ] Лежащий сайт не рвёт цикл и не растит errors_total (source="uptime")
- [ ] Секретов в коде/тестах нет; .env.example обновлён
- [ ] Локальный деплой: docker compose healthy, метрики видны в /metrics

## Маршрутизация

**Архитектор:** ТРЕБОВАЛСЯ (новый коллектор/источник данных) — ADR-002 создан.
**Исполнитель:** Разработчик
**Обоснование:** Новый коллектор + метрики + изменение base.py — код; инфраструктура
не меняется (compose/prometheus готовы), деплой — стандартный Этап 4.
