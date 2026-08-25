# ЧТЗ: Фикс бизнес-метрик zabor-i-naves.ru (несовпадение ключа site-metrics)

> Маршрут 3 (инфраструктурная): Аналитик → DevOps. Упрощённая форма (малая задача).
> Дата: 2026-08-24. Постдеплойный рестарт после деплоя v1.4.

## 1. Проблема

Дашборд «Бизнес сайта» по zabor-i-naves.ru пуст; Prometheus targets `site-tracking`
(и др.) для сайта down; алерты `SiteMetricsEndpointDown` firing (4 шт).

## 2. Диагностика (проверено на проде 2026-08-24)

| Проверка | Результат | Вывод |
|---|---|---|
| `GET https://zabor-i-naves.ru/metrics/tracking` с ключом из `.env` (X-Monitoring-Key / Bearer / query) | 403 при любой схеме | наш ключ сайтом отклоняется |
| Тот же URL с фактическим ключом сайта (предоставлен владельцем) | **200**, полный набор `business_*` (sessions_24h, unique_visitors_24h, bounce_rate_24h, leads_24h/1h, conversion_rate_24h, events_24h, service_clicks_24h, avg_session_duration) | путь в `sites.yml` ВЕРЕН, данные ЕСТЬ |
| `/metrics/{content,node,postgres}` с валидным ключом | 404 | сайтом не реализованы (остаётся за командой сайта; `up=0` — норма по ЧТЗ подключения) |
| `/api/metrics` с валидным ключом | 200: `analytics_events_total`, `conversion_funnel_total`, `page_views_total`, `calculator_events_total` и др. | аналитика дашборда «User Analytics» — вне текущих 4 kinds (отдельная задача, см. §7) |
| Счётчик Метрики 108488683 (наш токен) | 403 | доступ аккаунту мониторинга так и не выдан (за владельцем) |

**Корневая причина:** в `SITE_METRICS_API_KEYS` (локально и на VPS) для
zabor-i-naves.ru записан ключ, сгенерированный при подключении, а сайт-команда
установила собственное значение ключа.

## 3. Изменения

1. **`.env` на VPS** (`/root/monitoring/.env`, chmod 600): в JSON
   `SITE_METRICS_API_KEYS` для `zabor-i-naves.ru` заменить значение на
   фактический ключ сайта (источник: владелец, передан в сессии 2026-08-24).
2. **Локальный `.env`**: то же изменение (консистентность сред).
3. Пересоздать стек (`docker compose -p monitoring up -d --force-recreate`)
   для подхвата окружения.

Код, `config/sites.yml`, дашборды, правила алертов — БЕЗ изменений.

## 4. Критерии приёмки

- [ ] Prometheus: target `site-tracking` для zabor-i-naves.ru → `up`
- [ ] `monitoring_site_metrics_up{site="zabor-i-naves.ru",kind="tracking"}` = 1
- [ ] `business_sessions_24h{site="zabor-i-naves.ru"}` присутствует в Prometheus
- [ ] Алерты `SiteMetricsEndpointDown` для kind=tracking погасли
      (content/node/postgres продолжают firing — ожидаемо, §2)
- [ ] Дашборд «Бизнес сайта» (site=zabor-i-naves.ru) показывает значения
- [ ] `verify_prod.py` → ИТОГО: N OK, 0 FAIL
- [ ] Секрет только в `.env` (600); в git/логах отсутствует
- [ ] Изоляция: smarttraffic-* не затронуты

## 5. Риски и откаты

- Ключ передан в чате: не записывать в файлы репозитория/логи (только .env).
- Откат: вернуть прежнее значение ключа в `.env` + force-recreate.

## 6. Маршрутизация

Исполнитель: **DevOps** (секрет + пересоздание стека). Тестировщик: приёмка по §4
(проверка на проде после деплоя).

## 7. Открытые пункты (вне этого ЧТЗ)

1. `content`/`node`/`postgres` kinds: ждут реализации сайтом (алерты —
   штатный трекер готовности).
2. Аналитика `/api/metrics` (events/funnel/calculator) не входит в 4 kinds —
   при необходимости подсветить дашборд «User Analytics — zabor-i-naves.ru»
   отдельным ЧТЗ (новый kind `analytics` + job Prometheus).
3. Метрика 108488683: владелец должен выдать доступ аккаунту мониторинга
   (представитель) или выпустить токен с `metrika:read`; тег счётчика уже
   установлен на сайте.
