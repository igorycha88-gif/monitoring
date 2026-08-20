"""Разовый backfill по-дневной истории Вебмастера в SQLite (ЧТЗ_Вебмастер_Backfill_по_дням).

Заполняет webmaster_daily задним числом: API Вебмастера отдаёт per-query
history за окно до 31 дня. Переиспользует HTTP-обмен и обработку статусов
WebmasterCollector + WebmasterStorage.save_daily (upsert — повторный прогон
безопасен).

Запуск (внутри контейнера, окно 31 день):
    docker exec -e WEBMASTER_HISTORY_DAYS=31 monitoring-app \
        python scripts/backfill_webmaster_daily.py
"""

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import httpx

from app.config import get_settings
from app.logging import get_logger, setup_logging
from app.sites import SiteConfig, load_sites
from app.storage import DailyRow, WebmasterStorage
from collectors.webmaster import (
    HISTORY_INDICATORS,
    YANDEX_TZ,
    WebmasterCollector,
    _number,
)
from collectors.webmaster import (
    WEBMASTER_RETRYABLE as RETRYABLE_EXCEPTIONS,
)

REQUEST_PAUSE_SECONDS = 0.5

logger = get_logger("scripts.backfill_webmaster_daily")


class Backfiller(WebmasterCollector):
    """Коллектор в режиме backfill: все завершённые дни окна в SQLite."""

    async def backfill_site(self, site: SiteConfig) -> int:
        """Пишет всю по-дневную историю топа запросов сайта. Возвращает число строк."""
        host_id = site.webmaster_host_id
        if host_id is None:
            return 0
        token = self.oauth_tokens.get(site.domain.strip().lower(), "")
        if not token:
            logger.error("backfill_site_skipped", site=site.domain, reason="no_oauth_token")
            return 0
        user_id = await self._get_user_id(token)
        rows = await self.retry(
            lambda: self._fetch_queries(user_id, host_id, token),
            retry_on=RETRYABLE_EXCEPTIONS,
        )
        written = 0
        for row in rows:

            async def fetch_points(qid: str = row.query_id) -> dict[str, dict[str, float]]:
                return await self._fetch_history_all_days(user_id, host_id, qid, token)

            points = await self.retry(
                fetch_points,
                retry_on=RETRYABLE_EXCEPTIONS,
            )
            daily_rows = self._to_daily_rows(row.query_id, row.query, points)
            if daily_rows:
                await self._save_daily_backfill(site.domain, daily_rows)
                written += len(daily_rows)
            await asyncio.sleep(REQUEST_PAUSE_SECONDS)
        logger.info(
            "backfill_site_done",
            site=site.domain,
            host_id=host_id,
            queries=len(rows),
            rows_written=written,
        )
        return written

    async def _fetch_history_all_days(
        self, user_id: int, host_id: str, query_id: str, token: str
    ) -> dict[str, dict[str, float]]:
        """GET history — все ЗАВЕРШЁННЫЕ дни окна: {индикатор: {день: значение}}.

        Отличие от _fetch_query_history коллектора (ADR-008 D1, только новейшая
        точка): backfill забирает весь ряд, исключая сегодняшний незавершённый
        день Яндекса.
        """
        url = f"{self.api_base}/user/{user_id}/hosts/{host_id}/search-queries/{query_id}/history"
        date_from, date_to = self._history_range()
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.get(
                    url,
                    params={
                        "query_indicator": list(HISTORY_INDICATORS),
                        "date_from": date_from,
                        "date_to": date_to,
                    },
                    headers={"Authorization": f"OAuth {token}"},
                )
        except httpx.HTTPError as exc:
            self.logger.error(
                "backfill_transport_error",
                operation="fetch_history_all_days",
                host_id=host_id,
                query_id=query_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise
        self._raise_for_status(
            response, operation="fetch_history_all_days", host_id=host_id, query_id=query_id
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ValueError(f"Некорректный JSON истории запроса {query_id}") from exc
        return self._parse_history_all_days(payload, query_id)

    def _parse_history_all_days(self, payload: Any, query_id: str) -> dict[str, dict[str, float]]:
        """Разбирает indicators на полные ряды завершённых дней."""
        if not isinstance(payload, dict) or not isinstance(payload.get("indicators"), dict):
            raise ValueError(f"Нет indicators в истории запроса {query_id}")
        today_yandex = datetime.now(tz=YANDEX_TZ).date()
        result: dict[str, dict[str, float]] = {}
        for indicator in HISTORY_INDICATORS:
            points = payload["indicators"].get(indicator)
            if points is None:
                continue
            if not isinstance(points, list):
                raise ValueError(f"{indicator} не список в истории запроса {query_id}")
            series: dict[str, float] = {}
            for item in points:
                if not isinstance(item, dict):
                    raise ValueError(f"Некорректная точка {indicator} запроса {query_id}")
                point_date = item.get("date")
                value = _number(item.get("value"))
                if not isinstance(point_date, str) or value is None:
                    raise ValueError(f"Нет date/value у точки {indicator} запроса {query_id}")
                try:
                    point_day = datetime.fromisoformat(point_date).date()
                except ValueError as exc:
                    raise ValueError(
                        f"Некорректная date {point_date!r} запроса {query_id}"
                    ) from exc
                if point_day >= today_yandex:
                    continue  # сегодняшний день всегда незавершён
                series[point_day.isoformat()] = value
            if series:
                result[indicator] = series
        return result

    def _to_daily_rows(
        self, query_id: str, query: str, points: dict[str, dict[str, float]]
    ) -> list[DailyRow]:
        """Полные ряды → DailyRow на каждый день окна (кликки/показы, None если нет)."""
        clicks = points.get("TOTAL_CLICKS", {})
        shows = points.get("TOTAL_SHOWS", {})
        days = sorted(set(clicks) | set(shows))
        return [
            DailyRow(
                query_id=query_id,
                query=query,
                date=day,
                clicks=clicks.get(day),
                shows=shows.get(day),
            )
            for day in days
        ]

    async def _save_daily_backfill(self, domain: str, rows: Sequence[DailyRow]) -> None:
        """Пишет строки в SQLite тем же upsert, что и штатный сбор (ADR-009)."""
        storage = self.storage
        if storage is None:
            return
        fetched_at = datetime.now(tz=UTC).isoformat()
        await asyncio.to_thread(storage.save_daily, domain, list(rows), fetched_at)


async def main() -> None:
    """Точка входа: backfill по всем сайтам с webmaster_host_id."""
    settings = get_settings()
    setup_logging(settings.log_level, settings.log_format)
    sites = load_sites(settings.sites_config_path)
    wm_sites = [site for site in sites if site.webmaster_host_id is not None]
    if not wm_sites:
        logger.error("backfill_failed", reason="no_webmaster_sites")
        raise SystemExit(1)
    tokens = {site.domain: settings.webmaster_token(site.domain) for site in wm_sites}
    storage = WebmasterStorage(settings.webmaster_db_path)
    storage.init()
    backfiller = Backfiller(
        sites,
        oauth_tokens=tokens,
        timeout_seconds=settings.webmaster_timeout_seconds,
        top_queries=settings.webmaster_top_queries,
        history_days=settings.webmaster_history_days,
        storage=storage,
    )
    backfiller.retry_base_delay = 1.0
    logger.info(
        "backfill_started",
        sites=[site.domain for site in wm_sites],
        history_days=settings.webmaster_history_days,
    )
    total = 0
    try:
        for site in wm_sites:
            total += await backfiller.backfill_site(site)
    finally:
        storage.close()
    logger.info("backfill_finished", rows_written=total)


if __name__ == "__main__":
    asyncio.run(main())
