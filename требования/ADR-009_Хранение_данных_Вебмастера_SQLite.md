# ADR-009: Долговременное хранение данных Вебмастера в SQLite с привязкой к сайту

- Дата: 2026-08-19
- Статус: принят
- Контекст: ЧТЗ «Хранение данных Вебмастера в БД» (запрос пользователя)

## Контекст

Все данные Вебмастера сейчас живут только в Prometheus TSDB (gauge-снапшоты):
- недельный срез `/popular` перезаписывается каждым прогоном;
- дневная история накапливается точками gauge, но теряется безвозвратно при
  потере TSDB, а сырые ответы API не сохраняются нигде.

Нужно: надёжное долговременное хранение всех получаемых данных Вебмастера
с привязкой к сайту (domain) — источник правды для аналитики/экспорта.

## Решения

### D1: SQLite (WAL), файл — не отдельный СУБД-контейнер

Объём данных крошечный: ~2 сайта × ≤100 запросов × 2 прогона/день ≈
десятки тысяч строк в год. Отдельный Postgres = новый сервис, порт, память
и точка отказа на VPS рядом с чужим приложением (Правило изоляции 0.1).
SQLite-файл в docker volume `monitoring-db` (аналогично prometheus-data),
резервное копирование — файл целиком существующими rsync-скриптами.

- Формат: `/var/lib/monitoring/webmaster.db` (в контейнере),
  локально — `data/webmaster.db`; режим WAL + busy_timeout.
- Стандартная библиотека `sqlite3` — без новых зависимостей.

### D2: Слой хранения `app/storage.py`, вызовы через asyncio.to_thread

Класс `WebmasterStorage(path)`: `init()` (каталог + DDL + WAL),
`save_weekly(site, rows, fetched_at)`, `save_daily(site, rows)`, `close()`.
sqlite3 — синхронный: коллектор (async) оборачивает каждый вызов в
`asyncio.to_thread`, чтобы не блокировать event loop uvicorn.
Подключение — на вызов (объём мал, упрощает конкурентный доступ);
транзакция — на пакет (один прогон сайта = один commit).

### D3: Схема — две таблицы, привязка к сайту в каждой строке

```sql
-- Недельные снапшоты /popular (история прогона сохраняется)
CREATE TABLE IF NOT EXISTS webmaster_weekly (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    site TEXT NOT NULL,              -- domain из config/sites.yml
    query_id TEXT NOT NULL,
    query TEXT NOT NULL,
    shows REAL, clicks REAL, position REAL,   -- NULL = индикатор не определён
    fetched_at TEXT NOT NULL,        -- ISO-8601 UTC момента запроса
    UNIQUE(site, query_id, fetched_at)
);

-- Дневная история per-query (новейшая завершённая точка каждого прогона)
CREATE TABLE IF NOT EXISTS webmaster_daily (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    site TEXT NOT NULL,
    query_id TEXT NOT NULL,
    query TEXT NOT NULL,
    date TEXT NOT NULL,              -- день Яндекса (Europe/Moscow), YYYY-MM-DD
    clicks REAL, shows REAL,
    fetched_at TEXT NOT NULL,
    UNIQUE(site, query_id, date)     -- UPSERT: ретро-корректировки Яндекса
);                                   -- перезаписывают значение того же дня
```

`NULL` для отсутствующих индикаторов — семантика «не определён», не 0
(согласовано с поведением метрик, ADR-004/008). Индексы: `(site, date)` на
daily, `(site, fetched_at)` на weekly.

### D4: Отказ БД не роняет сбор метрик

Prometheus — основной канал мониторинга; БД — долговременное сырьё.
Исключение при записи: `logger.error("webmaster_db_write_failed", ...)` +
счётчик `monitoring_storage_errors_total{source="webmaster"}` (app/metrics.py),
сбор метрик по сайту продолжается. Обратный порядок (метрики упали — БД не
пишется) сохраняется естественным путём выполнения.

### D5: Конфигурация и Docker

- `app/config.py`: `webmaster_db_path: str = "data/webmaster.db"` (env
  `WEBMASTER_DB_PATH`); пустая строка — хранение отключено (обратная
  совместимость тестов/окружений без БД).
- `app/main.py` (lifespan): создать `WebmasterStorage`, `init()`, передать в
  `WebmasterCollector(storage=...)`; в finally — `close()`.
- `docker-compose.yml`: volume `monitoring-db:/var/lib/monitoring` +
  `WEBMASTER_DB_PATH=/var/lib/monitoring/webmaster.db` для app.
- `.dockerignore`: `data/` (локальная БД не попадает в образ).
- `.env.example`: `WEBMASTER_DB_PATH` с комментарием.

## Последствия

- Сырая история Вебмастера переживает пересборку/потерю TSDB; выборки по
  сайту — `WHERE site = ?`.
- Данные в БД и метрики Prometheus пишутся из одного прогона — согласованы
  по значениям на момент `fetched_at`.
- Рост БД линейный и медленный; архивация — копия файла (WAL checkpoint
  перед копией не требуется для rsync-ежедневника в пределах одного хоста).
- Чтение из БД (API/экспорт) — вне скоупа, отдельная задача при 필요ности.

## Тестирование

- Unit-тесты `tests/test_storage.py`: DDL, привязка к сайту, идемпотентность
  UPSERT daily, накопление weekly-снапшотов, NULL-индикаторы.
- Тесты коллектора: подмена storage (fake) — проверка вызовов записи;
  ошибка storage не валит `collect_site`.
