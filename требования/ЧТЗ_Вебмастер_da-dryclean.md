# ЧТЗ (упрощённое): Включение сбора данных Яндекс.Вебмастера для da-dryclean.ru

## Задача
Включить сбор топа поисковых запросов (клики/показы/позиция) из Яндекс.Вебмастера для сайта da-dryclean.ru.

## Изменения
1. `.env`: `YANDEX_WEBMASTER_OAUTH_TOKEN` — OAuth-токен с правом api-webmaster (получен, chmod 600)
2. `config/sites.yml`: добавлен `webmaster_host_id: "https:da-dryclean.ru:443"` сайту da-dryclean.ru

## Реализация (уже существует, без изменений кода)
- Коллектор `collectors/webmaster.py` — топ-50 запросов за неделю, интервал 3600с (WEBMASTER_INTERVAL_SECONDS)
- Метрики: `monitoring_search_clicks_total`, `monitoring_search_shows_total`, `monitoring_search_position{site, query}`

## Критерии приёмки
1. Коллектор webmaster регистрируется при старте (в логах `webmaster_collector_init`)
2. GET https://api.webmaster.yandex.net/v4/user — 200, user_id=2298186322
3. GET /hosts — host_id найден, verified=true
4. После деплоя в /metrics появляются `monitoring_search_*` метрики
5. Сбор по расписанию раз в час без ошибок 401/403/404 в логах

## Маршрутизация
Аналитик → DevOps (конфигурация + пересборка, изменений кода нет)
