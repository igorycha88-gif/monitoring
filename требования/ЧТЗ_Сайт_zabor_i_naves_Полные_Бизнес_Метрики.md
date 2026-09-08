# ЧТЗ (для репо сайта zabor-i-naves.ru): Клики по телефону + полнота бизнес-метрик

**Версия:** 1.0
**Дата:** 2026-08-29
**Связанные:** ADR-012 (клики по телефону), ADR-007/ADR-010 (конвейер site-metrics)
**Заказчик:** владелец. Исполнитель — команд(ы) сайтовой части.

## 1. Анализ: какие данные для дашбордов НЕ поступают (факт 2026-08-29, прод)

Дашборд «Бизнес сайта» (site-business.json) — пустые панели у этого сайта:

| Метрика | Панель | Состояние |
|---|---|---|
| `business_phone_clicks_12h` / `business_phone_clicks_event` | «Клики по телефону (12ч)», «(время клика)» | ❌ метрики сайт не отдаёт |
| `business_geo_visitors_24h{city}` | «Гео посетителей (топ-10, 24ч)» | ❌ не отдаёт |

Дашборд «Обзор сайта»:

| Данные | Панели | Состояние |
|---|---|---|
| Визиты/посетители Яндекс.Метрики (счётчик 108488683) | «Визиты», «Посетители» | ❌ **коллектор падает: 403** — токен истёк или нет доступа к счётчику (`monitoring_collector_success{source="metrika",site="zabor-i-naves.ru"}=0`) |
| Серверные метрики | CPU/RAM/Диск/Load | ❌ `node_exporter_url` не настроен |

Работает: sessions/page_views/visitors/leads/conversion/bounce/events/
referral_sources/service_clicks (окна 24ч/1ч/30мин), Вебмастер,
`/metrics/content|node|postgres`, персайтный дашборд
site-zabor-analytics (`analytics_events_total` и др.).

## 2. Требования

### 2.1 Клики по телефону (ADR-012, приоритет 1)

1. Делегированный JS-обработчик `click` на `a[href^="tel:"]`
   (номер 74993901595 и новые при добавлении) → существующий канал
   событий сайта (`analytics_events`), `event_type="click_phone"`,
   точный `created_at` в БД.
2. Debounce: не чаще 1 события на ссылку в 5 секунд.
3. Экспорт в `/metrics/tracking`:

```
# HELP business_phone_clicks_12h Phone number (tel:) clicks in the last 12 hours
# TYPE business_phone_clicks_12h gauge
business_phone_clicks_12h 3

# HELP business_phone_clicks_event Phone click events (last 24h), one sample per click with exact click timestamp
# TYPE business_phone_clicks_event gauge
business_phone_clicks_event 1 1753940520000
business_phone_clicks_event 1 1753969080000
```

Правила: `12h` — gauge-окно, пересчёт в 60с-цикле; `event` — один семпл
`1` на клик за 24ч, третий токен — unix-мс момента клика, по возрастанию,
без лейблов, значение строго `1` (дедупликация, ADR-012 D2/D3); кликов нет —
метрики не рендерятся; кэш рендера до 60 минут допустим.

### 2.2 Гео посетителей (приоритет 3)

- Геолокация по IP посетителя (геобаза MaxMind GeoLite2 или API Яндекса),
  город пишется в БД при событии.
- Экспорт: `business_geo_visitors_24h{city="<город>"}` — gauge, уникальные
  посетители по городам за 24ч, топ-10; не определился — `city="(unknown)"`.
  Образец реализации — da-dryclean.ru.

### 2.3 Доступ Метрики к счётчику 108488683 (приоритет 1, владелец)

- Выдать OAuth-токен Яндекс.Метрики с правом «Чтение статистики» на
  счётчик 108488683 (текущий токен получает 403; счётчик эвакуация.online
  текущим токеном читается — нужен токен, покрывающий ОБА счётчика, или
  отдельный токен для zabor).
- Токен передать владельцу мониторинга → `.env` прода (в git не попадает).

### 2.4 node_exporter (приоритет 4, серверная часть)

- Установить node_exporter на сервере сайта; сообщить URL вида
  `http://<host>:9100` — мониторинг добавит `node_exporter_url`
  в sites.yml (правка репо мониторинга, не сайта).

## 3. Критерии приёмки

1. Клик по номеру создаёт `click_phone` с корректным `created_at` (БД).
2. `curl -H "X-Monitoring-Key: <ключ>" https://zabor-i-naves.ru/metrics/tracking`
   отдаёт обе метрики телефона + geo_visitors; timestamp'ы точны до секунды;
   значения сходятся с БД.
3. После передачи токена (п.2.3): `monitoring_collector_success{source="metrika",
   site="zabor-i-naves.ru"}=1`, панели «Визиты/Посетители» с данными.
4. Повторные запросы эндпоинта не создают дублей событий; ответ < 1 с.
5. После п.2.4 и правки sites.yml — серверные панели «Обзора сайта» с данными.

## 4. Вопросы

Нет: форматы метрик зафиксированы образцом da-dryclean.ru и ADR-012.
