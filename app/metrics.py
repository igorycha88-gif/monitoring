"""Реестр метрик Prometheus приложения мониторинга."""

from prometheus_client import Counter, Gauge

COLLECTOR_SUCCESS = Gauge(
    "monitoring_collector_success",
    "Успешность последнего сбора по сайту (1 — успех, 0 — ошибка)",
    ["source", "site"],
)

COLLECTOR_ERRORS_TOTAL = Counter(
    "monitoring_collector_errors_total",
    "Суммарное количество ошибок сбора по источнику и сайту",
    ["source", "site"],
)

COLLECTOR_DURATION_SECONDS = Gauge(
    "monitoring_collector_duration_seconds",
    "Длительность последнего сбора по сайту, в секундах",
    ["source", "site"],
)

UPTIME_STATUS = Gauge(
    "monitoring_uptime_status",
    "Доступность сайта: 1 — HTTP-ответ с кодом <= порога, 0 — недоступен или ошибка",
    ["site"],
)

UPTIME_RESPONSE_SECONDS = Gauge(
    "monitoring_uptime_response_seconds",
    "Время ответа сайта на HTTP-проверку, в секундах",
    ["site"],
)

UPTIME_RESPONSE_CODE = Gauge(
    "monitoring_uptime_response_code",
    "Итоговый HTTP-код ответа сайта; 0 — ответа не получено",
    ["site"],
)

SSL_DAYS_LEFT = Gauge(
    "monitoring_ssl_days_left",
    "Дней до истечения TLS-сертификата сайта; отрицательное значение — просрочен",
    ["site"],
)

SITE_VISITS_TOTAL = Gauge(
    "monitoring_site_visits_total",
    "Визиты сайта за текущий день (нарастающий итог, UTC); gauge — применять напрямую, без rate()",
    ["site", "source"],
)

SITE_VISITORS = Gauge(
    "monitoring_site_visitors",
    "Уникальные посетители сайта за текущий день (нарастающий итог, UTC)",
    ["site", "source"],
)

SEARCH_CLICKS_TOTAL = Gauge(
    "monitoring_search_clicks_total",
    "Клики по поисковому запросу за последнюю неделю (скользящее окно Вебмастера); "
    "gauge — применять напрямую, без rate()",
    ["site", "query"],
)

SEARCH_SHOWS_TOTAL = Gauge(
    "monitoring_search_shows_total",
    "Показы по поисковому запросу за последнюю неделю (скользящее окно Вебмастера); "
    "gauge — применять напрямую, без rate()",
    ["site", "query"],
)

SEARCH_POSITION = Gauge(
    "monitoring_search_position",
    "Средняя позиция показа поискового запроса за последнюю неделю; меньше — лучше",
    ["site", "query"],
)
