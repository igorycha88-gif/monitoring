"""Тесты WebmasterExporter: рендер поисковых метрик из SQLite (ADR-011)."""

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from prometheus_client import CollectorRegistry, generate_latest

from app.storage import DailyRow, WebmasterStorage, WeeklyRow
from app.webmaster_export import METRIC_NAMES, WebmasterExporter


def make_storage(tmp_path: Path) -> WebmasterStorage:
    storage = WebmasterStorage(tmp_path / "webmaster.db")
    storage.init()
    return storage


def today_minus(days: int) -> str:
    return (datetime.now(tz=UTC).date() - timedelta(days=days)).isoformat()


def render(exporter: WebmasterExporter) -> str:
    registry = CollectorRegistry()
    registry.register(exporter)
    return generate_latest(registry).decode()


def test_renders_weekly_and_daily_from_db(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    storage.save_weekly(
        "w.com",
        [WeeklyRow("a1", "купить слона", shows=1000.0, clicks=50.0, position=3.5)],
        "2026-08-20T07:00:00+00:00",
    )
    storage.save_daily("w.com", [DailyRow("a1", "купить слона", today_minus(1), 7.0, 200.0)], "t1")
    exporter = WebmasterExporter(storage, render_days=35, refresh_seconds=60)
    weekly_size, daily_size = exporter.refresh()

    assert (weekly_size, daily_size) == (1, 1)
    text = render(exporter)
    assert 'monitoring_search_clicks_total{query="купить слона",site="w.com"} 50.0' in text
    assert 'monitoring_search_shows_total{query="купить слона",site="w.com"} 1000.0' in text
    assert 'monitoring_search_position{query="купить слона",site="w.com"} 3.5' in text
    assert 'monitoring_search_daily_clicks{query="купить слона",site="w.com"} 7.0' in text
    assert 'monitoring_search_daily_shows{query="купить слона",site="w.com"} 200.0' in text
    # Без timestamp'ов (ADR-011 D3): значение одно, без миллисекунд в конце
    for line in text.splitlines():
        if line.startswith("monitoring_search_"):
            parts = line.rsplit(" ", 1)
            assert "." in parts[1] or parts[1].isdigit(), line


def test_latest_snapshot_wins(tmp_path: Path) -> None:
    """Вечерний снапшот перезаписывает утренний: рендер берёт MAX(fetched_at)."""
    storage = make_storage(tmp_path)
    storage.save_weekly(
        "w.com", [WeeklyRow("a1", "слон", 100.0, 5.0, 9.0)], "2026-08-20T07:00:00+00:00"
    )
    storage.save_weekly(
        "w.com", [WeeklyRow("a1", "слон", 120.0, 6.0, 8.0)], "2026-08-20T19:00:00+00:00"
    )
    exporter = WebmasterExporter(storage)
    exporter.refresh()

    text = render(exporter)
    assert 'monitoring_search_clicks_total{query="слон",site="w.com"} 6.0' in text
    assert 'monitoring_search_clicks_total{query="слон",site="w.com"} 5.0' not in text


def test_none_indicator_not_rendered(tmp_path: Path) -> None:
    """None-индикатор не рендерится (не 0) — паттерн ADR-004."""
    storage = make_storage(tmp_path)
    storage.save_weekly("w.com", [WeeklyRow("a1", "без позиции", 10.0, 1.0, None)], "t")
    exporter = WebmasterExporter(storage)
    exporter.refresh()

    text = render(exporter)
    assert 'monitoring_search_clicks_total{query="без позиции",site="w.com"} 1.0' in text
    assert "monitoring_search_position{" not in text


def test_empty_db_renders_nothing(tmp_path: Path) -> None:
    """Пустая БД — метрики отсутствуют (честное «нет данных», ADR-011 D5)."""
    storage = make_storage(tmp_path)
    exporter = WebmasterExporter(storage)
    exporter.refresh()

    assert render(exporter) == ""


def test_render_window_excludes_stale_queries(tmp_path: Path) -> None:
    """Запросы без точек в окне рендера не отдаются (кардинальность, D2)."""
    storage = make_storage(tmp_path)
    storage.save_daily("w.com", [DailyRow("a1", "свежий", today_minus(1), 1.0, 10.0)], "t")
    storage.save_daily("w.com", [DailyRow("a2", "старый", today_minus(40), 2.0, 20.0)], "t")
    exporter = WebmasterExporter(storage, render_days=35)
    weekly_size, daily_size = exporter.refresh()

    assert daily_size == 1
    text = render(exporter)
    assert 'monitoring_search_daily_clicks{query="свежий",site="w.com"} 1.0' in text
    assert 'monitoring_search_daily_clicks{query="старый"' not in text


def test_refresh_error_keeps_last_cache(tmp_path: Path) -> None:
    """Ошибка чтения БД: счётчик storage_errors, отдаётся последний кэш."""
    from structlog.testing import capture_logs

    from app.metrics import STORAGE_ERRORS_TOTAL

    storage = make_storage(tmp_path)
    storage.save_weekly("w.com", [WeeklyRow("a1", "слон", 10.0, 1.0, None)], "t")
    exporter = WebmasterExporter(storage)
    exporter.refresh()

    before = STORAGE_ERRORS_TOTAL.labels(source="webmaster-export")._value.get()
    exporter.storage = WebmasterStorage(tmp_path / "missing.db")  # без init — нет таблиц
    with capture_logs() as captured:
        ok = exporter._refresh_safe()

    assert ok is False
    failures = [entry for entry in captured if entry["event"] == "webmaster_export_refresh_failed"]
    assert failures and failures[0]["operation"] == "refresh"
    assert STORAGE_ERRORS_TOTAL.labels(source="webmaster-export")._value.get() == before + 1
    # Кэш не тронут: метрики по-прежнему отдаются
    text = render(exporter)
    assert 'monitoring_search_clicks_total{query="слон",site="w.com"} 1.0' in text


def test_background_thread_updates_cache(tmp_path: Path) -> None:
    """Поток стартует, наполняет кэш и останавливается (D3)."""
    from structlog.testing import capture_logs

    storage = make_storage(tmp_path)
    storage.save_weekly("w.com", [WeeklyRow("a1", "слон", 10.0, 1.0, None)], "t")
    exporter = WebmasterExporter(storage, refresh_seconds=0.05)

    with capture_logs() as captured:
        exporter.start()
        for _ in range(100):
            if exporter._weekly:
                break
            time.sleep(0.02)
        exporter.stop()

    assert exporter._weekly  # кэш наполнен фоновым потоком
    started = [entry for entry in captured if entry["event"] == "webmaster_export_started"]
    assert started and started[0]["render_days"] == 35
    assert [entry for entry in captured if entry["event"] == "webmaster_export_stopped"]
    # Повторный stop безопасен
    exporter.stop()


def test_metric_names_match_dashboards_whitelist() -> None:
    assert (
        frozenset(
            {
                "monitoring_search_clicks_total",
                "monitoring_search_shows_total",
                "monitoring_search_position",
                "monitoring_search_daily_clicks",
                "monitoring_search_daily_shows",
            }
        )
        == METRIC_NAMES
    )
