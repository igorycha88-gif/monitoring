"""Тесты скрипта экспорта истории Вебмастера в OpenMetrics (ADR-011 D4)."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.storage import DailyPoint, DailyRow, WebmasterStorage
from scripts.export_webmaster_history import (
    SeriesKey,
    aggregate,
    escape_label,
    main,
    render_openmetrics,
    timestamp_seconds,
)


def test_escape_label() -> None:
    assert escape_label('ки "кавычкам"') == 'ки \\"кавычкам\\"'
    assert escape_label("a\\b") == "a\\\\b"
    assert escape_label("a\nb") == "a\\nb"


def test_timestamp_seconds_is_utc_midnight() -> None:
    assert timestamp_seconds("2026-08-18") == int(datetime(2026, 8, 18, tzinfo=UTC).timestamp())


def test_aggregate_sums_duplicate_query_texts() -> None:
    """Разные query_id с одинаковым текстом суммируются (дедуп, D4)."""
    points = [
        DailyPoint("w.com", "q1", "слон", "2026-08-18", 2.0, 20.0),
        DailyPoint("w.com", "q1", "слон", "2026-08-18", 3.0, 30.0),
        DailyPoint("w.com", "q2", "мамонт", "2026-08-18", 1.0, 10.0),
    ]

    aggregated = aggregate(points)

    w = aggregated[SeriesKey("w.com", "слон")]
    assert w["monitoring_search_daily_clicks"]["2026-08-18"] == 5.0
    assert w["monitoring_search_daily_shows"]["2026-08-18"] == 50.0


def test_render_openmetrics_format(tmp_path: Path) -> None:
    points = [
        DailyPoint("w.com", "q1", "слон", "2026-08-17", 2.0, 20.0),
        DailyPoint("w.com", "q1", "слон", "2026-08-18", 3.0, None),
    ]

    content = render_openmetrics(aggregate(points))

    lines = content.splitlines()
    assert "# TYPE monitoring_search_daily_clicks gauge" in lines
    assert content.endswith("# EOF\n")
    ts17 = str(timestamp_seconds("2026-08-17"))
    assert (
        f'monitoring_search_daily_clicks{{job="monitoring-app",'
        f'instance="app:8088",site="w.com",query="слон"}} 2 {ts17}' in lines
    )
    # shows=None 18-го не рендерится, clicks рендерится
    ts18 = str(timestamp_seconds("2026-08-18"))
    assert (
        f'monitoring_search_daily_clicks{{job="monitoring-app",'
        f'instance="app:8088",site="w.com",query="слон"}} 3 {ts18}' in lines
    )
    shows_lines = [line for line in lines if line.startswith("monitoring_search_daily_shows{")]
    assert shows_lines == [
        f'monitoring_search_daily_shows{{job="monitoring-app",'
        f'instance="app:8088",site="w.com",query="слон"}} 20 {ts17}'
    ]
    # Точки серии отсортированы по возрастанию времени
    clicks_lines = [line for line in lines if line.startswith("monitoring_search_daily_clicks{")]
    timestamps = [int(line.rsplit(" ", 1)[1]) for line in clicks_lines]
    assert timestamps == sorted(timestamps)
    # Секунды, не миллисекунды: порядок величины epoch-секунд
    assert 1e9 < timestamps[0] < 2e9


def test_render_openmetrics_custom_job_instance() -> None:
    """Лейблы тождественны скрейп-сериям — иначе серии задвоятся (ADR-011 D4)."""
    points = [DailyPoint("w.com", "q1", "слон", "2026-08-18", 1.0, None)]

    content = render_openmetrics(aggregate(points), job="other", instance="x:1")

    assert 'job="other",instance="x:1",site="w.com",query="слон"' in content


def test_render_openmetrics_escapes_labels() -> None:
    points = [DailyPoint('site"1', 'q"1', 'запрос "топ"', "2026-08-18", 1.0, None)]

    content = render_openmetrics(aggregate(points))

    assert 'site="site\\"1",query="запрос \\"топ\\""' in content


def test_main_writes_file_and_logs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "webmaster.db"
    storage = WebmasterStorage(db_path)
    storage.init()
    storage.save_daily(
        "w.com",
        [DailyRow("q1", "слон", "2026-08-17", 2.0, 20.0)],
        "t",
    )
    out = tmp_path / "history.omtex"

    code = main(["--end", "2026-08-18", "--out", str(out), "--db", str(db_path)])

    assert code == 0
    content = out.read_text(encoding="utf-8")
    assert "monitoring_search_daily_clicks" in content
    assert content.endswith("# EOF\n")


def test_main_no_data_returns_error(tmp_path: Path) -> None:
    db_path = tmp_path / "webmaster.db"
    storage = WebmasterStorage(db_path)
    storage.init()
    out = tmp_path / "history.omtex"

    code = main(["--end", "2026-08-18", "--out", str(out), "--db", str(db_path)])

    assert code == 1


def test_main_bad_end_date(tmp_path: Path) -> None:
    code = main(["--end", "18.08.2026", "--out", str(tmp_path / "x"), "--db", "any"])
    assert code == 2
