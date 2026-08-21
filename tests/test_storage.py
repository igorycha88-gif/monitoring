"""Тесты WebmasterStorage (SQLite, ADR-009): схема, привязка к сайту, UPSERT."""

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from app.storage import DailyRow, WebmasterStorage, WeeklyRow


@pytest.fixture
def storage(tmp_path: Path) -> WebmasterStorage:
    instance = WebmasterStorage(tmp_path / "webmaster.db")
    instance.init()
    return instance


def fetch_all(db_path: Path, query: str) -> list[tuple[Any, ...]]:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(query).fetchall()
    finally:
        conn.close()


WEEKLY_A = WeeklyRow(query_id="a1", query="купить слона", shows=1000.0, clicks=50.0, position=3.5)
WEEKLY_B = WeeklyRow(query_id="a2", query="слон недорого", shows=200.0, clicks=10.0, position=7.1)


def test_init_creates_tables_and_directories(tmp_path: Path) -> None:
    db_path = tmp_path / "nested" / "dir" / "webmaster.db"
    WebmasterStorage(db_path).init()
    tables = {
        name for (name,) in fetch_all(db_path, "SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"webmaster_weekly", "webmaster_daily"} <= tables


def test_init_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "webmaster.db"
    WebmasterStorage(db_path).init()
    WebmasterStorage(db_path).init()  # не падает на повторной инициализации


def test_weekly_rows_bound_to_site(storage: WebmasterStorage, tmp_path: Path) -> None:
    storage.save_weekly("w.com", [WEEKLY_A, WEEKLY_B], "2026-08-19T10:00:00+00:00")
    storage.save_weekly("other.ru", [WEEKLY_A], "2026-08-19T10:00:00+00:00")

    rows = fetch_all(tmp_path / "webmaster.db", "SELECT site, query_id FROM webmaster_weekly")
    assert ("w.com", "a1") in rows
    assert ("w.com", "a2") in rows
    assert ("other.ru", "a1") in rows
    # Привязка к сайту: данные разных сайтов не смешиваются
    assert len([row for row in rows if row[0] == "w.com"]) == 2
    assert len([row for row in rows if row[0] == "other.ru"]) == 1


def test_weekly_snapshots_accumulate_between_runs(
    storage: WebmasterStorage, tmp_path: Path
) -> None:
    storage.save_weekly("w.com", [WEEKLY_A], "2026-08-19T07:00:00+00:00")
    storage.save_weekly("w.com", [WEEKLY_A], "2026-08-19T19:00:00+00:00")

    rows = fetch_all(tmp_path / "webmaster.db", "SELECT fetched_at, clicks FROM webmaster_weekly")
    assert len(rows) == 2  # каждый прогон — свой снапшот


def test_weekly_same_run_is_upserted(storage: WebmasterStorage, tmp_path: Path) -> None:
    fetched_at = "2026-08-19T07:00:00+00:00"
    storage.save_weekly("w.com", [WEEKLY_A], fetched_at)
    corrected = WeeklyRow(
        query_id="a1", query="купить слона", shows=1100.0, clicks=55.0, position=3.0
    )
    storage.save_weekly("w.com", [corrected], fetched_at)

    rows = fetch_all(tmp_path / "webmaster.db", "SELECT shows, clicks FROM webmaster_weekly")
    assert rows == [(1100.0, 55.0)]  # тот же прогон — перезапись, не дубль


def test_weekly_null_indicator_stored_as_null(storage: WebmasterStorage, tmp_path: Path) -> None:
    partial = WeeklyRow(query_id="a3", query="без позиции", shows=5.0, clicks=None, position=None)
    storage.save_weekly("w.com", [partial], "2026-08-19T07:00:00+00:00")

    rows = fetch_all(tmp_path / "webmaster.db", "SELECT clicks, position FROM webmaster_weekly")
    assert rows == [(None, None)]  # «не определён», не 0


def test_weekly_empty_rows_noop(storage: WebmasterStorage, tmp_path: Path) -> None:
    assert storage.save_weekly("w.com", [], "2026-08-19T07:00:00+00:00") == 0
    assert fetch_all(tmp_path / "webmaster.db", "SELECT * FROM webmaster_weekly") == []


def test_daily_upsert_idempotent_and_correction(storage: WebmasterStorage, tmp_path: Path) -> None:
    day = "2026-08-18"
    storage.save_daily("w.com", [DailyRow("a1", "купить слона", day, 5.0, 100.0)], "t1")
    storage.save_daily("w.com", [DailyRow("a1", "купить слона", day, 6.0, 120.0)], "t2")

    rows = fetch_all(
        tmp_path / "webmaster.db", "SELECT clicks, shows, fetched_at FROM webmaster_daily"
    )
    assert rows == [(6.0, 120.0, "t2")]  # UPSERT: ретро-коррекция перезаписывает


def test_daily_null_indicator_preserves_previous(storage: WebmasterStorage, tmp_path: Path) -> None:
    day = "2026-08-18"
    storage.save_daily("w.com", [DailyRow("a1", "слон", day, 5.0, None)], "t1")
    storage.save_daily("w.com", [DailyRow("a1", "слон", day, None, 42.0)], "t2")

    rows = fetch_all(tmp_path / "webmaster.db", "SELECT clicks, shows FROM webmaster_daily")
    assert rows == [(5.0, 42.0)]  # NULL не затирает ранее известное значение


def test_daily_separate_days_and_sites(storage: WebmasterStorage, tmp_path: Path) -> None:
    storage.save_daily("w.com", [DailyRow("a1", "слон", "2026-08-17", 1.0, 10.0)], "t1")
    storage.save_daily("w.com", [DailyRow("a1", "слон", "2026-08-18", 2.0, 20.0)], "t2")
    storage.save_daily("other.ru", [DailyRow("a1", "слон", "2026-08-18", 9.0, 90.0)], "t3")

    rows = fetch_all(
        tmp_path / "webmaster.db",
        "SELECT site, date, clicks FROM webmaster_daily ORDER BY site, date",
    )
    assert rows == [
        ("other.ru", "2026-08-18", 9.0),
        ("w.com", "2026-08-17", 1.0),
        ("w.com", "2026-08-18", 2.0),
    ]


def test_close_is_safe(storage: WebmasterStorage) -> None:
    storage.close()  # подключения на вызов — закрывать нечего, не падает


# --- Чтение для рендера /metrics (ADR-011 D2) ---


def test_latest_weekly_returns_last_snapshot_per_query(storage: WebmasterStorage) -> None:
    storage.save_weekly("w.com", [WEEKLY_A], "2026-08-20T07:00:00+00:00")
    corrected = WeeklyRow("a1", "купить слона", 1100.0, 55.0, 3.0)
    storage.save_weekly("w.com", [corrected, WEEKLY_B], "2026-08-20T19:00:00+00:00")

    points = storage.latest_weekly()

    by_query = {point.query_id: point for point in points}
    assert set(by_query) == {"a1", "a2"}
    assert by_query["a1"].clicks == 55.0  # последний снапшот
    assert by_query["a1"].site == "w.com"
    assert by_query["a1"].shows == 1100.0


def test_latest_weekly_separate_sites(storage: WebmasterStorage) -> None:
    storage.save_weekly("w.com", [WEEKLY_A], "2026-08-20T07:00:00+00:00")
    storage.save_weekly("other.ru", [WEEKLY_A], "2026-08-21T07:00:00+00:00")

    points = storage.latest_weekly()

    sites = {point.site for point in points}
    assert sites == {"w.com", "other.ru"}


def test_latest_daily_newest_date_per_query(storage: WebmasterStorage) -> None:
    storage.save_daily("w.com", [DailyRow("a1", "слон", "2026-08-17", 1.0, 10.0)], "t1")
    storage.save_daily("w.com", [DailyRow("a1", "слон", "2026-08-18", 2.0, 20.0)], "t2")

    points = storage.latest_daily("2026-08-01")

    assert len(points) == 1
    assert points[0].date == "2026-08-18"
    assert points[0].clicks == 2.0


def test_latest_daily_window_filters_stale_queries(storage: WebmasterStorage) -> None:
    storage.save_daily("w.com", [DailyRow("a1", "свежий", "2026-08-20", 1.0, 10.0)], "t1")
    storage.save_daily("w.com", [DailyRow("a2", "старый", "2026-07-01", 2.0, 20.0)], "t2")

    points = storage.latest_daily("2026-08-15")

    assert [point.query_id for point in points] == ["a1"]


def test_latest_daily_empty(storage: WebmasterStorage) -> None:
    assert storage.latest_daily("2026-08-01") == []


def test_daily_history_end_bound(storage: WebmasterStorage) -> None:
    storage.save_daily("w.com", [DailyRow("a1", "слон", "2026-08-17", 1.0, 10.0)], "t1")
    storage.save_daily("w.com", [DailyRow("a1", "слон", "2026-08-18", 2.0, 20.0)], "t2")
    storage.save_daily("w.com", [DailyRow("a1", "слон", "2026-08-19", 3.0, 30.0)], "t3")

    points = storage.daily_history("2026-08-18")

    assert [point.date for point in points] == ["2026-08-17", "2026-08-18"]  # end включительно


def test_daily_history_preserves_none_indicators(storage: WebmasterStorage) -> None:
    storage.save_daily("w.com", [DailyRow("a1", "слон", "2026-08-18", None, 20.0)], "t1")

    points = storage.daily_history("2026-08-18")

    assert points[0].clicks is None
    assert points[0].shows == 20.0
