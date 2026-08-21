"""Рендер поисковых метрик Вебмастера из SQLite в /metrics (ADR-011).

Кастомный collector prometheus_client: имена/лейблы метрик идентичны
прежним gauge (дашборды и алерты не меняются, ADR-011 D5). Чтение БД —
фоновым потоком по расписанию (scrape не делает I/O и не блокирует
event loop, ADR-011 D3); экспозиция — текущие значения БЕЗ timestamp'ов
(живой Prometheus отбрасывает старые семплы, повтор истории на каждом
scrape — шум out-of-order).
"""

import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector, CollectorRegistry

from app.logging import get_logger
from app.metrics import STORAGE_ERRORS_TOTAL
from app.storage import DailyPoint, WebmasterStorage, WeeklyPoint

logger = get_logger("app.webmaster_export")

SOURCE = "webmaster-export"

# Имена метрик рендера (белый список дашбордов, tests/test_dashboards.py).
METRIC_NAMES: frozenset[str] = frozenset(
    {
        "monitoring_search_clicks_total",
        "monitoring_search_shows_total",
        "monitoring_search_position",
        "monitoring_search_daily_clicks",
        "monitoring_search_daily_shows",
    }
)


def _gauge(name: str, help_text: str) -> GaugeMetricFamily:
    return GaugeMetricFamily(name, help_text, labels=["site", "query"])


class WebmasterExporter(Collector):
    """Рендер monitoring_search_* из БД; кэш обновляет фоновый поток (ADR-011)."""

    def __init__(
        self,
        storage: WebmasterStorage,
        render_days: int = 35,
        refresh_seconds: float = 60.0,
    ) -> None:
        self.storage = storage
        self.render_days = max(1, render_days)
        self.refresh_seconds = max(0.1, refresh_seconds)
        self._lock = threading.Lock()
        self._weekly: list[WeeklyPoint] = []
        self._daily: list[DailyPoint] = []
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    # --- Collector API (вызывается generate_latest на каждом scrape) ---

    def collect(self) -> Iterator[GaugeMetricFamily]:
        """Отдаёт метрики из кэша; без I/O (ADR-011 D3)."""
        with self._lock:
            weekly = list(self._weekly)
            daily = list(self._daily)
        clicks = _gauge(
            "monitoring_search_clicks_total",
            "Клики по поисковому запросу за последнюю неделю (скользящее окно "
            "Вебмастера); gauge — применять напрямую, без rate()",
        )
        shows = _gauge(
            "monitoring_search_shows_total",
            "Показы по поисковому запросу за последнюю неделю (скользящее окно "
            "Вебмастера); gauge — применять напрямую, без rate()",
        )
        position = _gauge(
            "monitoring_search_position",
            "Средняя позиция показа поискового запроса за последнюю неделю; меньше — лучше",
        )
        for row in weekly:
            if row.clicks is not None:
                clicks.add_metric([row.site, row.query], row.clicks)
            if row.shows is not None:
                shows.add_metric([row.site, row.query], row.shows)
            if row.position is not None:
                position.add_metric([row.site, row.query], row.position)
        daily_clicks = _gauge(
            "monitoring_search_daily_clicks",
            "Клики по поисковому запросу за последний ЗАВЕРШЁННЫЙ день; "
            "сумма по запросам = сайт; gauge — без rate()",
        )
        daily_shows = _gauge(
            "monitoring_search_daily_shows",
            "Показы по поисковому запросу за последний ЗАВЕРШЁННЫЙ день; "
            "сумма по запросам = сайт; gauge — без rate()",
        )
        for point in daily:
            if point.clicks is not None:
                daily_clicks.add_metric([point.site, point.query], point.clicks)
            if point.shows is not None:
                daily_shows.add_metric([point.site, point.query], point.shows)
        for family in (clicks, shows, position, daily_clicks, daily_shows):
            if family.samples:
                yield family

    # --- Фоновое обновление кэша ---

    def refresh(self) -> tuple[int, int]:
        """Читает БД → новый кэш. Возвращает (weekly, daily) размеры.

        Вызывается фоновым потоком и доступен для вызова вручную (тесты).
        Ошибка чтения НЕ поднимается наружу цикла потока — обрабатывается
        в _refresh_safe.
        """
        today = datetime.now(tz=UTC).date()
        since_date = (today - timedelta(days=self.render_days)).isoformat()
        weekly = self.storage.latest_weekly()
        daily = self.storage.latest_daily(since_date)
        with self._lock:
            self._weekly = weekly
            self._daily = daily
        return len(weekly), len(daily)

    def _refresh_safe(self) -> bool:
        """Обновление кэша с обработкой ошибок: лог + счётчик, кэш не трогаем."""
        try:
            weekly_size, daily_size = self.refresh()
        except Exception as exc:
            STORAGE_ERRORS_TOTAL.labels(source=SOURCE).inc()
            logger.error(
                "webmaster_export_refresh_failed",
                source=SOURCE,
                operation="refresh",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return False
        logger.debug(
            "webmaster_export_refreshed",
            source=SOURCE,
            weekly=weekly_size,
            daily=daily_size,
        )
        return True

    def _run(self) -> None:
        """Цикл потока: немедленный первый refresh, далее по расписанию."""
        while not self._stop_event.is_set():
            self._refresh_safe()
            self._stop_event.wait(self.refresh_seconds)

    def start(self) -> None:
        """Запускает фоновое обновление кэша (daemon-поток)."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="webmaster-export-refresh",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "webmaster_export_started",
            source=SOURCE,
            render_days=self.render_days,
            refresh_seconds=self.refresh_seconds,
        )

    def stop(self) -> None:
        """Останавливает фоновое обновление (join с таймаутом)."""
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=self.refresh_seconds + 1.0)
        self._thread = None
        logger.info("webmaster_export_stopped", source=SOURCE)


def register_webmaster_exporter(exporter: WebmasterExporter, registry: CollectorRegistry) -> None:
    """Регистрирует экспортёр в реестре prometheus_client (idempotent)."""
    try:
        registry.register(exporter)
    except ValueError:
        # уже зарегистрирован (повторный старт в том же процессе)
        logger.warning("webmaster_export_already_registered", source=SOURCE)
