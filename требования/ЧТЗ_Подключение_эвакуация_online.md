# ЧТЗ (упрощённое): Подключение эвакуация.online к централизованному мониторингу

> Версия: 1.0 · Дата: 2026-08-19 · Эпик: расширение ЭПИК-9 (ADR-007)
> Маршрут: Аналитик → DevOps (конфигурация + пересборка; изменений кода нет)
> Парное ЧТЗ для команды сайта: `Эваукация/требования/ЧТЗ_Централизованный_мониторинг.md`

## Задача

Подключить сайт эвакуация.online (VPS 178.57.218.204, Next.js + PostgreSQL)
полным пакетом, как da-dryclean.ru:
- uptime + SSL — автоматически после добавления домена;
- Яндекс.Метрика — счётчик **111456265** (из ЧТЗ сайта);
- Яндекс.Вебмастер — топ поисковых запросов;
- site-metrics — 4 kind (`tracking/content/node/postgres`) за `X-Monitoring-Key`.

## Исходные данные

| Параметр | Значение |
|---|---|
| Домен (canonical, главное зеркало) | `эвакуация.online` (punycode: `xn--80akhbyknj4f.online`) |
| Метрика counter_id | `111456265` |
| Вебмастер host_id | получить через API (см. Шаг 2); сайт верифицирован в Вебмастере |
| Ключ site-metrics | существующий `SITE_METRICS_API_KEY` из `.env` (общий с da-dryclean; владельцем передаётся команде сайта) |
| SSH сервера сайта | не требуется для этой ЧТЗ (серверную часть делает команда сайта); отпечаток хоста от владельца: `SHA256:g3xVlty76Op1vIbTuwy+8M+0ZUXx4XgtQX2+YrTtbLY` — при любом доступе сверять, при несовпадении СТОП |

## Изменения

1. **`config/sites.yml`** — добавить сайт:
   ```yaml
   - domain: эвакуация.online
     metrika_counter_id: 111456265
     webmaster_host_id: "https:эвакуация.online:443"   # уточнить по Шагу 2
     metrics_urls:
       tracking: https://эвакуация.online/metrics/tracking
       content: https://эвакуация.online/metrics/content
       node: https://эвакуация.online/metrics/node
       postgres: https://эвакуация.online/metrics/postgres
   ```
   Код не меняется: ADR-007 — «любой следующий сайт подключается одной записью
   `metrics_urls` в sites.yml» (SD подхватит за 60 с). Коллекторы Метрики (ЭПИК-2)
   и Вебмастера (ЭПИК-5) включаются по наличию полей.

2. **Уточнить `webmaster_host_id`** — с VPS мониторинга (токен уже в `.env`):
   ```bash
   curl -s -H "Authorization: OAuth $YANDEX_WEBMASTER_OAUTH_TOKEN" \
     https://api.webmaster.yandex.net/v4/user/2298186322/hosts | jq
   ```
   Найти хост эвакуация.online, взять точный `host_id` и `verified=true`.
   Если хоста нет в этом аккаунте → СТОП: запросить у владельца доступ/токен
   (риск: Вебмастер зарегистрирован на другой аккаунт Яндекса).

3. **`.env`** — проверить доступность счётчика Метрики:
   ```bash
   curl -s -H "Authorization: OAuth $YANDEX_METRIKA_OAUTH_TOKEN" \
     "https://api-metrika.yandex.net/management/v1/counters/111456265" | jq '.counter.status'
   ```
   403/аккаунт не совпадает → владелец выпускает OAuth-токен с правом
   `metrika:read` для счётчика. Новых ключей в коде/`.env.example` не появляется.

## Порядок и зависимости

- uptime/SSL/Метрика/Вебмастер работают сразу после пересборки.
- site-* jobs: до выполнения ЧТЗ командой сайта — `up=0` (404),
  `SiteMetricsEndpointDown` в pending — ожидаемо (коммуникация с владельцем).

## Критерии приёмки

1. `pytest && ruff check . && ruff format --check . && mypy .` — зелёные
   (sites.yml проходит валидацию: IDN-домен, https-only metrics_urls).
2. После полной пересборки в `/metrics`:
   - `monitoring_uptime_status{site="эвакуация.online"}` = 1 (сайт жив);
   - `monitoring_ssl_cert_days_left{site="эвакуация.online"}` присутствует;
   - `monitoring_site_visits_total{site="эвакуация.online"}` после первого цикла
     Метрики (первое включение коллектора — ранее сайтов с counter_id не было);
   - `monitoring_search_*{site="эвакуация.online"}` после первого цикла Вебмастера.
3. `GET /api/v1/sd/site-metrics/tracking` содержит
   `"targets":["эвакуация.online:443"]` с лейблом `site`.
4. Prometheus: цели site-* для нового сайта видны в UI; `up=0` до доработки
   сайта — норма. Если юникод-хост в target не резолвится — заменить host в
   `metrics_urls` на punycode `xn--80akhbyknj4f.online` (лейбл `site` остаётся
   из поля `domain`) и пересобрать.
5. Дашборды «Обзор сайта», «Все сайты», «Бизнес сайта»: `эвакуация.online`
   появляется в переменной `$site`.
6. Алерты не срабатывают ложно: SiteDown не firing, CollectorFailing не firing.
7. Регрессия: da-dryclean.ru — метрики/коллекторы работают как раньше.
8. Секреты в файлах не появились: `git grep` по токенам/ключу — пусто.

## Маршрутизация

**Архитектор:** НЕ ТРЕБУЕТСЯ (механизм ADR-007/ADR-005 переиспользуется как есть).
**Исполнитель:** DevOps (правка sites.yml, уточнение host_id/токенов, полная
пересборка локального стека; прод-деплой мониторинга — по отдельной команде
владельца через PIPELINE_PROD после готовности сайта).
**Обоснование:** изменений кода нет — только конфигурация и env (образец:
ЧТЗ_Вебмастер_da-dryclean.md).
