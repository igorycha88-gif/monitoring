# ЧТЗ (для репо сайта эвакуация.online): Клики по телефону + полнота бизнес-метрик

**Версия:** 1.0
**Дата:** 2026-08-29
**Связанные:** ADR-012 (клики по телефону), ADR-007/ADR-010 (конвейер site-metrics)
**Заказчик:** владелец. Исполнитель — команд(ы) сайтовой части.

## 1. Анализ: какие данные для дашбордов НЕ поступают (факт 2026-08-29, прод)

Дашборд «Бизнес сайта» (site-business.json) — пустые панели у этого сайта:

| Метрика | Панель | Состояние |
|---|---|---|
| `business_phone_clicks_12h` / `business_phone_clicks_event` | «Клики по телефону (12ч)», «(время клика)» | ❌ метрики сайт не отдаёт |
| `business_referral_sources_24h{source}` | «Источники трафика (24ч)» | ❌ не отдаёт (у zabor/da-dryclean есть) |
| `business_geo_visitors_24h{city}` | «Гео посетителей (топ-10, 24ч)» | ❌ не отдаёт |
| `business_service_clicks_24h{service}` | «Клики по услугам (топ-10, 24ч)» | ❌ не отдаёт (событий service_click нет) |

Дашборд «Обзор сайта»: серверные панели (CPU/RAM/Диск/Load) — ❌ пустые:
`node_exporter_url` не настроен (job node-exporter не имеет целей).

Работает и закрывать не нужно: sessions/page_views/visitors/leads/conversion/
bounce/avg_duration/events (окна 24ч/1ч/30мин), Вебмастер, Метрика
(визиты/посетители), /metrics/content|node|postgres.

## 2. Требования

### 2.1 Клики по телефону (ADR-012, приоритет 1)

1. Делегированный JS-обработчик `click` на `a[href^="tel:"]`
   (номера: +79017054540 и новые при добавлении) → существующий канал
   событий сайта, `event_type="click_phone"`, точный `created_at` в БД.
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

### 2.2 Источники трафика (приоритет 2)

- При каждом событии трекинга сохранять `Referer` запроса (заголовок) в БД.
- Экспорт: `business_referral_sources_24h{source="<host>"}` — gauge, топ-10
  источников за 24ч; источник = host referer'а, прямой заход —
  `source="(direct)"`. Образец реализации — da-dryclean.ru.

### 2.3 Гео посетителей (приоритет 3)

- Геолокация по IP посетителя (геобаза MaxMind GeoLite2 или API Яндекса),
  город пишется в БД при событии.
- Экспорт: `business_geo_visitors_24h{city="<город>"}` — gauge, уникальные
  посетители по городам за 24ч, топ-10; не определился — `city="(unknown)"`.

### 2.4 Клики по услугам (приоритет 2)

- JS-событие `service_click` при клике по карточке/пункту услуги
  (идентификатор услуги — slug/название).
- Экспорт: `business_service_clicks_24h{service="<slug>"}` — gauge за 24ч.

### 2.5 node_exporter (приоритет 4, серверная часть)

- Установить node_exporter на сервере сайта, порт на 127.0.0.1 или выдать
  наружу только для IP мониторинга (VPS 130.49.129.241).
- Сообщить владельцу URL вида `http://<host>:9100` — мониторинг добавит
  `node_exporter_url` в sites.yml (правка репо мониторинга, не сайта).

## 3. Критерии приёмки

1. Клик по номеру создаёт `click_phone` с корректным `created_at` (БД).
2. `curl -H "X-Monitoring-Key: <ключ>" https://эвакуация.online/metrics/tracking`
   отдаёт обе метрики телефона + referral/geo/service_clicks; timestamp'ы
   кликов точны до секунды; значения сходятся с БД.
3. Повторные запросы эндпоинта не создают дублей событий.
4. Эндпоинт отвечает < 1 с; новые метрики не ломают существующие.
5. После п.2.5 и правки sites.yml — серверные панели «Обзора сайта» с данными.

## 4. Вопросы

Нет: форматы метрик зафиксированы образцом da-dryclean.ru и ADR-012.
