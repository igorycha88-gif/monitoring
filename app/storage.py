"""Слой долговременного хранения данных Вебмастера: SQLite + WAL (ADR-009).

Привязка к сайту — колонка site (domain из config/sites.yml) в каждой строке.
Синхронный sqlite3: коллектор вызывает методы через asyncio.to_thread
(ADR-009 D2). Подключение — на вызов; транзакция — на пакет (один прогон
сайта = один commit).
"""

import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import NamedTuple

from app.logging import get_logger

logger = get_logger("app.storage")

BUSY_TIMEOUT_SECONDS = 5.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS webmaster_weekly (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    site TEXT NOT NULL,
    query_id TEXT NOT NULL,
    query TEXT NOT NULL,
    shows REAL,
    clicks REAL,
    position REAL,
    fetched_at TEXT NOT NULL,
    UNIQUE(site, query_id, fetched_at)
);
CREATE INDEX IF NOT EXISTS idx_webmaster_weekly_site_fetched
    ON webmaster_weekly(site, fetched_at);

CREATE TABLE IF NOT EXISTS webmaster_daily (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    site TEXT NOT NULL,
    query_id TEXT NOT NULL,
    query TEXT NOT NULL,
    date TEXT NOT NULL,
    clicks REAL,
    shows REAL,
    fetched_at TEXT NOT NULL,
    UNIQUE(site, query_id, date)
);
CREATE INDEX IF NOT EXISTS idx_webmaster_daily_site_date
    ON webmaster_daily(site, date);
"""


class WeeklyRow(NamedTuple):
    """Недельный снапшот запроса (/popular); None = индикатор не определён."""

    query_id: str
    query: str
    shows: float | None
    clicks: float | None
    position: float | None


class WeeklyPoint(NamedTuple):
    """Недельный снапшот с привязкой к сайту (последний прогон, ADR-011 D2)."""

    site: str
    query_id: str
    query: str
    shows: float | None
    clicks: float | None
    position: float | None


class DailyRow(NamedTuple):
    """Дневная точка запроса (per-query history); None = индикатор не определён."""

    query_id: str
    query: str
    date: str
    clicks: float | None
    shows: float | None


class DailyPoint(NamedTuple):
    """Дневная точка с привязкой к сайту (ADR-011 D2)."""

    site: str
    query_id: str
    query: str
    date: str
    clicks: float | None
    shows: float | None


class WebmasterStorage:
    """Хранилище данных Вебмастера в SQLite-файле (ADR-009 D1–D3)."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)

    def init(self) -> None:
        """Создаёт каталог, схему и включает WAL (идемпотентно)."""
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")  # персистентен для файла (ADR-009 D1)
            with conn:
                conn.executescript(_SCHEMA)
        finally:
            conn.close()
        logger.info("storage_init", path=self.path, journal_mode="wal")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT_SECONDS)
        conn.execute(f"PRAGMA busy_timeout={int(BUSY_TIMEOUT_SECONDS * 1000)}")
        return conn

    def save_weekly(self, site: str, rows: Sequence[WeeklyRow], fetched_at: str) -> int:
        """Пишет недельный снапшот сайта (одна транзакция на прогон).

        Конфликт (site, query_id, fetched_at) — перезапись (повтор того же
        прогона); снапшоты разных прогонов накапливаются.
        """
        written = self._save_weekly(site, rows, fetched_at)
        if written:
            logger.info("storage_weekly_written", site=site, rows=written, fetched_at=fetched_at)
        return written

    def _save_weekly(self, site: str, rows: Sequence[WeeklyRow], fetched_at: str) -> int:
        if not rows:
            return 0
        conn = self._connect()
        try:
            with conn:
                conn.executemany(
                    """
                    INSERT INTO webmaster_weekly
                        (site, query_id, query, shows, clicks, position, fetched_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(site, query_id, fetched_at) DO UPDATE SET
                        shows = excluded.shows,
                        clicks = excluded.clicks,
                        position = excluded.position
                    """,
                    [
                        (
                            site,
                            row.query_id,
                            row.query,
                            row.shows,
                            row.clicks,
                            row.position,
                            fetched_at,
                        )
                        for row in rows
                    ],
                )
        finally:
            conn.close()
        return len(rows)

    def save_daily(self, site: str, rows: Sequence[DailyRow], fetched_at: str) -> int:
        """Пишет дневные точки сайта (UPSERT по site+query_id+date).

        Ретро-корректировки Яндекса перезаписывают значение того же дня;
        NULL-индикатор не затирает ранее известное значение (COALESCE).
        """
        written = self._save_daily(site, rows, fetched_at)
        if written:
            logger.info("storage_daily_written", site=site, rows=written, fetched_at=fetched_at)
        return written

    def _save_daily(self, site: str, rows: Sequence[DailyRow], fetched_at: str) -> int:
        if not rows:
            return 0
        conn = self._connect()
        try:
            with conn:
                conn.executemany(
                    """
                    INSERT INTO webmaster_daily
                        (site, query_id, query, date, clicks, shows, fetched_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(site, query_id, date) DO UPDATE SET
                        query = excluded.query,
                        clicks = COALESCE(excluded.clicks, webmaster_daily.clicks),
                        shows = COALESCE(excluded.shows, webmaster_daily.shows),
                        fetched_at = excluded.fetched_at
                    """,
                    [
                        (site, row.query_id, row.query, row.date, row.clicks, row.shows, fetched_at)
                        for row in rows
                    ],
                )
        finally:
            conn.close()
        return len(rows)

    def close(self) -> None:
        """Закрывает хранилище (подключения — на вызов, закрывать нечего)."""
        logger.info("storage_closed", path=self.path)

    # --- Чтение (рендер /metrics из БД, ADR-011 D2) ---

    def latest_weekly(self) -> list[WeeklyPoint]:
        """Последний снапшот КАЖДОГО САЙТА целиком — по MAX(fetched_at) сайта.

        Снапшот /popular — когерентный срез окна Вебмастера на момент прогона:
        смешение снапшотов разных прогонов завышало бы суммы (запросы, выпавшие
        из топа, оставались бы в рендере вечно). Вечерний прогон перезаписывает
        утренний: MAX выбирает актуальный срез; запросы, покинувшие топ,
        исчезают из рендера вместе со старым снапшотом.
        """
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT site, query_id, query, shows, clicks, position
                FROM webmaster_weekly AS w
                WHERE fetched_at = (
                    SELECT MAX(w2.fetched_at) FROM webmaster_weekly AS w2
                    WHERE w2.site = w.site
                )
                """
            ).fetchall()
        finally:
            conn.close()
        return [
            WeeklyPoint(
                site=str(site),
                query_id=str(query_id),
                query=str(query),
                shows=shows,
                clicks=clicks,
                position=position,
            )
            for site, query_id, query, shows, clicks, position in rows
        ]

    def latest_daily(self, since_date: str, until_date: str) -> list[DailyPoint]:
        """Последняя ГОТОВАЯ дата каждого (site, query_id) в окне [since, until].

        until_date — горизонт готовности (лаг финализации агрегатов Яндекса,
        инцидент 2026-08-25 «вечный ноль»): самый свежий завершённый день
        всегда записан нулями, рендерить его нельзя — MAX(date) считается
        только среди дат <= until_date. Запросы, не появлявшиеся в окне
        рендера, не отдаются — кардинальность ограничена окном (ADR-011 D2).
        """
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT site, query_id, query, date, clicks, shows
                FROM webmaster_daily AS w
                WHERE date >= ? AND date <= ?
                  AND date = (
                      SELECT MAX(w2.date) FROM webmaster_daily AS w2
                      WHERE w2.site = w.site AND w2.query_id = w.query_id
                        AND w2.date <= ?
                  )
                """,
                (since_date, until_date, until_date),
            ).fetchall()
        finally:
            conn.close()
        return [
            DailyPoint(
                site=str(site),
                query_id=str(query_id),
                query=str(query),
                date=str(day),
                clicks=clicks,
                shows=shows,
            )
            for site, query_id, query, day, clicks, shows in rows
        ]

    def daily_history(self, end_date: str) -> list[DailyPoint]:
        """Все дневные точки с датой <= end_date (экспорт истории, ADR-011 D4)."""
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT site, query_id, query, date, clicks, shows
                FROM webmaster_daily
                WHERE date <= ?
                ORDER BY site, query, date
                """,
                (end_date,),
            ).fetchall()
        finally:
            conn.close()
        return [
            DailyPoint(
                site=str(site),
                query_id=str(query_id),
                query=str(query),
                date=str(day),
                clicks=clicks,
                shows=shows,
            )
            for site, query_id, query, day, clicks, shows in rows
        ]
