# ADR-010: Персайтные ключи X-Monitoring-Key и relay-канал site-metrics

> Статус: принято. Дата: 2026-08-19. Эпик: расширение ЭПИК-9 (изменение ADR-007).

## Контекст

При подключении второго сайта (эвакуация.online, ЧТЗ_Подключение_эвакуация_online)
владелец определил: **у каждого сайта свой ключ X-Monitoring-Key** (ключ выдаётся
команде каждого сайта и не является общим секретом всех сайтов).

Механизм ADR-007 этого не поддерживает:

- `SITE_METRICS_API_KEY` — единственный глобальный ключ в `.env`;
- Prometheus задаёт `http_headers` на уровне **job'а**, а не таргета —
  один заголовок X-Monitoring-Key применяется ко всем сайтам job'а site-*;
- смена ключа ломала бы остальные сайты (проверено: da-dryclean.ru отвечает
  200 со своим ключом и 403 с ключом эвакуация.online).

Ограничения без изменений: секреты только в `.env` (chmod 600), никогда в
коде/коммитах/SD-ответах/логах; подключение сайта — записью в sites.yml.

## Решение

### D1. Разрешение ключей (app)

`.env`:

- `SITE_METRICS_API_KEYS` — JSON-объект `{"<domain>": "<ключ>"}` для
  пер-сайтовых ключей (unicode-домен как в `sites.yml`);
- `SITE_METRICS_API_KEY` — fallback для доменов без записи в JSON
  (обратная совместимость: da-dryclean.ru продолжает работать на нём).

Резолв (app/config.py): `site_metrics_key(domain)` —
`SITE_METRICS_API_KEYS[domain]` → иначе `SITE_METRICS_API_KEY` → иначе `""`
(пустой ключ = проверки получают 403, `up=0` — данные, не ошибка; ADR-002).

### D2. Relay-эндпоинт (proxy скрейпа)

`GET /api/v1/relay/site-metrics/{kind}/{site}` (app/api/v1/relay.py):

- находит сайт по домену (path-параметр, URL-decoded FastAPI) и
  `metrics_urls[kind]`;
- делает GET к эндпоинту сайта с заголовком X-Monitoring-Key = ключ сайта
  (D1), `follow_redirects=False`, таймаут `SITE_METRICS_TIMEOUT_SECONDS`;
- 2xx → тело и Content-Type проксируются как есть (формат Prometheus);
- не-2xx / сетевая ошибка / нет сайта или kind → 502/404 — для Prometheus
  это `up=0`;
- ключ не логируется; relay-пути исключены из INFO-мiddleware (шум скрейпов
  15 с × kinds × сайты), событие `relay_scrape` на debug.

Аутентификация relay не нужна: app доступен только из monitoring-net и
127.0.0.1 (SSH-туннель); `/metrics` приложения открыт так же.

### D3. SD ведёт на relay

`GET /api/v1/sd/site-metrics/{kind}` возвращает вместо прямых таргетов
`host:443`:

```json
{
  "targets": ["app:8088"],
  "labels": {
    "site": "эвакуация.online",
    "__metrics_path__": "/api/v1/relay/site-metrics/tracking/%D1%8D...%"
  }
}
```

Домен в пути — percent-encoded (`urllib.parse.quote`, safe=""), лейбл `site`
остаётся читаемым доменом. Схема http (внутренняя сеть), порт app:8088.

### D4. Prometheus без ключей

- job'ы site-* теряют `http_headers` (ключ relay не нужен);
- `scrape_timeout: 12s` для site-* (запас поверх таймаута relay 10 с);
- `prometheus-entrypoint.sh` больше не подставляет ключ (плейсхолдер
  удалён); docker-compose не передаёт SITE_METRICS_API_KEY в prometheus.
  Ключи site-metrics живут только в env приложения.

### D5. SiteMetricsCollector — персайтные ключи

Конструктор принимает `api_keys: dict[domain → ключ]` (main резолвит по D1
для сайтов с metrics_urls); заголовок берётся по `site.domain`. Поведение
метрик/логов не меняется. Предупреждение при старте — по списку сайтов без
ключа (`site_metrics_api_keys_empty`), а не один глобальный варнинг.

### D6. Подключение нового сайта (обновлённый контракт ADR-007)

1. запись в `config/sites.yml` (`metrics_urls`, опц. счётчик/host_id);
2. ключ сайта — запись в `SITE_METRICS_API_KEYS` (`.env`).

Правок `prometheus.yml` и кода не требуется; SD подхватывает за 60 с.

## Последствия

- Плюсы: персайтные ключи; ключи не покидают `.env` приложения (Prometheus
  без секретов вообще); один механизм для всех сайтов; SD динамичен.
- Минус (tradeoff): Prometheus и SiteMetricsCollector теперь идут к сайтам
  через egress приложения — «двойной независимый канал» ADR-007 сужается
  до одного egress. Принято: сигнал доступности эндпоинтов даёт коллектор
  (`monitoring_site_metrics_up`), а `up` job'ов site-* остаётся
  end-to-end проверкой (relay→сайт).
- `instance` site-* таргетов становится `app:8088` (в дашбордах/алертах
  не используется).
