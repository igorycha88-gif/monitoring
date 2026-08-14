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
