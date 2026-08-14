# ADR-002: Uptime- и SSL-коллекторы (ЭПИК-1)

**Дата:** 2026-08-14
**Статус:** Accepted
**Контекст:** ЭПИК-1 бэклога: HTTP/SSL-проверки сайтов из `config/sites.yml`.
Каркас ЭПИК-0 готов: `BaseCollector` (метрики `monitoring_collector_*`, логи,
ретраи, APScheduler), Settings, sites.yml. Нужны первый «боевой» коллектор и
параллельный обход сайтов, чтобы уложиться в интервал 60 сек.

## Решение

1. **Два коллектора в одном файле `collectors/uptime.py`:**
   - `UptimeCollector` (`source="uptime"`) — HTTP GET `https://{domain}/`
     каждые 60 сек, httpx async, `follow_redirects=True`, SSL-верификация
     включена (сайт с невалидным сертификатом = недоступен для пользователя).
   - `SSLCollector` (`source="ssl"`) — TLS-хендшейк раз в час, чтение
     `notAfter` сертификата через `asyncio.open_connection` + stdlib `ssl`.
2. **Семантика разделения здоровья сайта и здоровья коллектора:**
   - Сайт недоступен (timeout/DNS/SSL/conn refused, HTTP ≥ 400) — это **данные**:
     `monitoring_uptime_status{site}=0`, `monitoring_uptime_response_code{site}=0`
     (или реальный код), collector_success=1.
   - `monitoring_collector_*{source,site}` меняется только при **неожиданном**
     исключении (баг), не при лежащем сайте. Алерт «сайт лежит» — по
     `monitoring_uptime_status`, не по collector-метрикам.
3. **Uptime-проверка — одна попытка за цикл** (без ретраев, как blackbox_exporter):
   цикл 60 сек сам является «ретраем»; 3 попытки раз в минуту = DOS своего сайта.
   **SSL-проверка — с ретраями** (`retry_on=(TimeoutError, OSError)`): цикл
   редкий (раз в час), транзиентный сбой не должен стоить часа данных.
4. **SSL-сертификат читается БЕЗ верификации** (`CERT_NONE` в отдельном
   контексте, только для чтения): просроченный сертификат должен давать
   `monitoring_ssl_days_left{site}` с **отрицательным** значением (дней
   просрочки), а не «проверка не удалась». Хост без TLS/недоступный →
   `collector_success{source="ssl",site}=0`, метрика не трогается (сталeness
   Prometheus + алерт по collector_success).
   *Реализационная поправка:* при `CERT_NONE` stdlib `getpeercert()` возвращает
   пустой dict, поэтому сертификат читается как DER (`binary_form=True`) и
   парсится зависимостью `cryptography>=42` (`not_valid_after_utc`).
5. **Параллельный обход сайтов** — opt-in в `BaseCollector`: атрибут
   `parallel: bool = False`; при `True` `run_once` исполняет `collect_site`
   через `asyncio.gather` (пер-сайт try/except сохранён). Worst-case цикла =
   таймаут одного сайта (~10 сек), а не 10×10 сек. Метрика/Вебмастер останутся
   последовательными (`parallel=False`) из-за лимитов API.
6. Метрики регистрируются централизованно в `app/metrics.py`; коллекторы только
   импортируют (защита от дубликатов при пересоздании коллекторов в тестах).

## Альтернативы

### Вариант А: Один коллектор Uptime с SSL внутри, один источник `source="uptime"`
- Плюсы: меньше классов, один файл расписания
- Минусы: разные интервалы (60 сек vs 1 час) через один job невозможны;
  пер-сайт метрики collector_* двух циклов перезаписывают друг друга
  (duration/success скачут между HTTP- и SSL-фазами). Оценка: 4/10

### Вариант Б: SSL через метаданные httpx-ответа (без отдельного хендшейка)
- Плюсы: один клиент
- Минусы: httpx НЕ экспонирует сертификат пира; нужен workaround с
  transport-хуками или сторонняя библиотека. Оценка: 3/10

### Вариант В (выбран): Два коллектора в `collectors/uptime.py`, параллельный обход
- Плюсы: независимые расписания и здоровье; чистая семантика метрик;
  worst-case цикла не зависит от числа сайтов; stdlib-only для SSL
- Минусы: +1 класс; SSL-хендшейк отдельным соединением (дешёво, раз в час).
  Оценка: 9/10

## Влияние на архитектуру

### Модель метрик (новые)
```
monitoring_uptime_status{site}            # gauge: 1/0; 1 = HTTP-ответ < 400
monitoring_uptime_response_seconds{site}  # gauge: время ответа (сек), при
                                          # сетевой ошибке — фактическое время до провала
monitoring_uptime_response_code{site}     # gauge: итоговый HTTP-код; 0 = ответа нет
monitoring_ssl_days_left{site}            # gauge: дней до истечения; < 0 = просрочен
```
Кардинальность: только лейбл `site` (4–10 значений) — взрыва нет.

### Коллекторы
| Параметр | UptimeCollector | SSLCollector |
|----------|-----------------|--------------|
| source | `uptime` | `ssl` |
| Интервал | 60 сек (`UPTIME_INTERVAL_SECONDS`) | 3600 сек (`SSL_INTERVAL_SECONDS`) |
| Таймаут | 10 сек (`UPTIME_TIMEOUT_SECONDS`) | 10 сек (`SSL_TIMEOUT_SECONDS`) |
| Ретраи | нет (1 попытка) | 3 попытки, backoff, `retry_on=(TimeoutError, OSError)` |
| Параллельность | да | да |
| Точки за сайт | 3 (status, seconds, code) | 1 (days_left) |

### Конфигурация (Settings, `.env`)
```
UPTIME_INTERVAL_SECONDS=60      # интервал HTTP-проверок
UPTIME_TIMEOUT_SECONDS=10.0     # таймаут HTTP-запроса
UPTIME_SUCCESS_MAX_CODE=399     # код <= порога => сайт "up"
SSL_INTERVAL_SECONDS=3600       # интервал SSL-проверок
SSL_TIMEOUT_SECONDS=10.0        # таймаут TLS-хендшейка
```
Секреты не требуются. Новых зависимостей нет (httpx + stdlib ssl/asyncio).

### Файловая структура (изменения)
```
collectors/uptime.py        # НОВОЕ: UptimeCollector, SSLCollector, хелпер TLS
collectors/base.py          # opt-in parallel (по умолчанию False — поведение ЭПИК-0 не меняется)
app/metrics.py              # +4 gauge
app/config.py               # +5 настроек
app/main.py                 # регистрация коллекторов в lifespan
tests/test_uptime.py        # НОВОЕ (respx: 200/301/500/timeout/DNS/SSL-провал)
tests/test_ssl_collector.py # НОВОЕ (мок TLS-хелпера: валидный/просроченный/отсутствующий notAfter)
```

### Дашборды
Не в этом эпике (ЭПИК-3). PromQL для будущего дашборда/алертов:
- uptime %: `avg_over_time(monitoring_uptime_status{site}[1h])`
- сайт лежит: `monitoring_uptime_status{site} == 0`
- SSL: `monitoring_ssl_days_left{site} < 14`

### API
Без изменений (`/health`, `/metrics`, `/api/v1/sites` — достаточно).

### Нагрузка
1 GET/мин/сайт + 1 TLS/час/сайт на СВОИ сайты — пренебрежимо. Внешние API
Яндекса не затрагиваются.

## Риски и митигация

| Риск | Вероятность | Влияние | Митигация |
|------|-------------|---------|-----------|
| Цикл дольше интервала при лежащих сайтах | Средняя | Среднее | parallel-обход: worst-case = 1 таймаут (~10 сек) |
| Сайт без 443 порта (только HTTP) | Низкая | Низкое | uptime проверяет https; SSL-цикл зафиксирует collector_success=0, не роняя остальные |
| Рассинхрон часов (notAfter локальное время) | Низкая | Низкое | `notAfter` в формате GMT, парсим в UTC-aware datetime |
| Изменение поведения base.run_once ломает ЭПИК-0 | Низкая | Среднее | `parallel=False` по умолчанию; существующие тесты BaseCollector остаются зелёными |
| Верификация CERT_NONE принята за уязвимость | Низкая | Низкое | Контекст изолирован в read-only хелпере, никуда не передаётся; задокументировано в ADR |

## Критерии успеха
- [ ] `monitoring_uptime_status/response_seconds/response_code` и `monitoring_ssl_days_left` в `/metrics`
- [ ] Лежащий сайт: status=0, code=0, collector_success=1, цикл продолжается
- [ ] 5xx-ответ: status=0, code=<реальный код>, collector_success=1
- [ ] Просроченный сертификат: `monitoring_ssl_days_left < 0`
- [ ] Параллельный цикл укладывается в интервал при всех лежащих сайтах
- [ ] pytest + ruff + mypy strict зелёные, покрытие новых файлов ≥ 60%
