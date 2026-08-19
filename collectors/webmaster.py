"""Коллектор Яндекс.Вебмастера: топ запросов за неделю + дневная история (ЭПИК-5, ADR-004/008)."""

import re
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any, NamedTuple
from zoneinfo import ZoneInfo

import httpx

from app.metrics import (
    SEARCH_CLICKS_TOTAL,
    SEARCH_DAILY_CLICKS,
    SEARCH_DAILY_SHOWS,
    SEARCH_POSITION,
    SEARCH_SHOWS_TOTAL,
)
from app.sites import SiteConfig
from collectors.base import BaseCollector, RateLimitError
from collectors.metrika import parse_retry_after

WEBMASTER_API_BASE = "https://api.webmaster.yandex.net/v4"
WEBMASTER_USER_URL = f"{WEBMASTER_API_BASE}/user"
QUERY_LABEL_MAX_LENGTH = 100
TOP_QUERIES_LIMIT = 100
HISTORY_DAYS_LIMIT = 31
# Даты в ответах Вебмастера — в таймзоне Яндекса (+03:00); «сегодня» в ней
# всегда неполное (нули) — исключается при выборе новейшей точки (ADR-008).
YANDEX_TZ = ZoneInfo("Europe/Moscow")

QUERY_INDICATORS = ("TOTAL_SHOWS", "TOTAL_CLICKS", "AVG_SHOW_POSITION")
HISTORY_INDICATORS = ("TOTAL_SHOWS", "TOTAL_CLICKS")

_WHITESPACE_RE = re.compile(r"\s+")


class WebmasterApiError(Exception):
    """Ошибка API Яндекс.Вебмастера."""


class WebmasterAuthError(WebmasterApiError):
    """401/403: токен недействителен или нет прав на сайт (ретраи бесполезны)."""


class WebmasterNotFoundError(WebmasterApiError):
    """404: сайт не подтверждён/не проиндексирован/данные не загружены (ретраи бесполезны)."""


class WebmasterConfigError(WebmasterApiError):
    """Ошибка конфигурации: пустой OAuth-токен."""


class WebmasterServerError(WebmasterApiError):
    """5xx: временный сбой API Вебмастера — ретраится (ADR-004)."""


WEBMASTER_RETRYABLE: tuple[type[BaseException], ...] = (
    httpx.TimeoutException,
    httpx.TransportError,
    RateLimitError,
    WebmasterServerError,
)


def normalize_query(text: str) -> str:
    """Нормализация текста запроса для лейбла query: пробелы + обрезка (кардинальность, ADR-004)."""
    return _WHITESPACE_RE.sub(" ", text).strip()[:QUERY_LABEL_MAX_LENGTH]


class QueryStats(NamedTuple):
    """Разобранный поисковый запрос; None = индикатор не определён в ответе."""

    query_id: str
    query: str
    shows: float | None
    clicks: float | None
    position: float | None


class DailyValue(NamedTuple):
    """Значение индикатора за последний завершённый день (ADR-008)."""

    date: str
    value: float


def _number(value: Any) -> float | None:
    """Число из JSON или None (bool — не число, мусор — не число)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


class WebmasterCollector(BaseCollector):
    """Сбор топа поисковых запросов (клики/показы/позиция) за последнюю неделю (ЭПИК-5, ADR-004).

    Последовательный обход (лимиты API). user_id Яндекса разрешается лениво
    через GET /v4/user и кэшируется. Сайты без webmaster_host_id пропускаются
    при инициализации: метрики collector_* для них не создаются.
    """

    source = "webmaster"
    parallel = False

    def __init__(
        self,
        sites: Sequence[SiteConfig],
        oauth_token: str,
        timeout_seconds: float,
        top_queries: int = 50,
        api_base: str = WEBMASTER_API_BASE,
        history_days: int = 7,
    ) -> None:
        super().__init__(sites)
        all_sites = self.sites
        self.sites = [site for site in all_sites if site.webmaster_host_id is not None]
        self.skipped_sites = len(all_sites) - len(self.sites)
        self.oauth_token = oauth_token
        self.timeout_seconds = timeout_seconds
        self.top_queries = max(1, min(top_queries, TOP_QUERIES_LIMIT))
        self.api_base = api_base.rstrip("/")
        self.user_url = f"{self.api_base}/user"
        self.history_days = max(1, min(history_days, HISTORY_DAYS_LIMIT))
        self._user_id: int | None = None
        self.logger.info(
            "webmaster_collector_init",
            hosts=len(self.sites),
            skipped_sites=self.skipped_sites,
            top_queries=self.top_queries,
            history_days=self.history_days,
        )

    async def collect_site(self, site: SiteConfig) -> int:
        """Пишет метрики по топу запросов и дневной истории сайта.

        Возвращает число записанных значений. Ошибка любого из двух запросов
        (/popular, /history) — ошибка сайта (транзиентность покрывают ретраи,
        ADR-008 D1).
        """
        host_id = site.webmaster_host_id
        if host_id is None:
            return 0
        if not self.oauth_token:
            raise WebmasterConfigError("YANDEX_WEBMASTER_OAUTH_TOKEN пуст (секреты в .env)")
        user_id = await self._get_user_id()
        rows = await self.retry(
            lambda: self._fetch_queries(user_id, host_id),
            retry_on=WEBMASTER_RETRYABLE,
        )
        points = 0
        for row in rows:
            points += self._write_row(site.domain, row)
        points += await self._write_daily_history(user_id, host_id, site.domain, rows)
        self.logger.info(
            "webmaster_collect_done",
            site=site.domain,
            host_id=host_id,
            queries=len(rows),
            points=points,
        )
        return points

    def _write_row(self, domain: str, row: QueryStats) -> int:
        """Пишет доступные метрики запроса. Отсутствующий индикатор пропускается (не 0)."""
        points = 0
        if row.clicks is not None:
            SEARCH_CLICKS_TOTAL.labels(site=domain, query=row.query).set(row.clicks)
            points += 1
        if row.shows is not None:
            SEARCH_SHOWS_TOTAL.labels(site=domain, query=row.query).set(row.shows)
            points += 1
        if row.position is not None:
            SEARCH_POSITION.labels(site=domain, query=row.query).set(row.position)
            points += 1
        return points

    async def _get_user_id(self) -> int:
        """user_id Яндекса (кэшируется в экземпляре после первого успеха)."""
        if self._user_id is not None:
            return self._user_id
        user_id = await self.retry(
            lambda: self._fetch_user_id(),
            retry_on=WEBMASTER_RETRYABLE,
        )
        self._user_id = user_id
        return user_id

    async def _fetch_user_id(self) -> int:
        """GET /v4/user — идентификатор владельца OAuth-токена."""
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.get(
                    self.user_url,
                    headers={"Authorization": f"OAuth {self.oauth_token}"},
                )
        except httpx.HTTPError as exc:
            self.logger.error(
                "webmaster_api_transport_error",
                operation="fetch_user_id",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise
        self._raise_for_status(response, operation="fetch_user_id")
        try:
            payload = response.json()
        except ValueError as exc:
            raise WebmasterApiError("Вебмастер: некорректный JSON ответа /user") from exc
        user_id = payload.get("user_id") if isinstance(payload, dict) else None
        user_id_value = _number(user_id)
        if user_id_value is None:
            raise WebmasterApiError(f"Вебмастер: некорректный user_id в ответе: {payload!r}")
        return int(user_id_value)

    async def _fetch_queries(self, user_id: int, host_id: str) -> list[QueryStats]:
        """GET .../search-queries/popular — топ запросов сайта за последнюю неделю."""
        url = f"{self.api_base}/user/{user_id}/hosts/{host_id}/search-queries/popular"
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.get(
                    url,
                    params={
                        "order_by": "TOTAL_CLICKS",
                        "query_indicator": list(QUERY_INDICATORS),
                        "device_type_indicator": "ALL",
                        "limit": self.top_queries,
                    },
                    headers={"Authorization": f"OAuth {self.oauth_token}"},
                )
        except httpx.HTTPError as exc:
            self.logger.error(
                "webmaster_api_transport_error",
                operation="fetch_queries",
                host_id=host_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise
        self._raise_for_status(response, operation="fetch_queries", host_id=host_id)
        try:
            payload = response.json()
        except ValueError as exc:
            raise WebmasterApiError(f"Вебмастер: некорректный JSON ответа для {host_id}") from exc
        return self._parse_queries(payload, host_id)

    def _parse_queries(self, payload: Any, host_id: str) -> list[QueryStats]:
        """Разбирает список queries; пустой список — валидные данные (0 запросов)."""
        if not isinstance(payload, dict) or not isinstance(payload.get("queries"), list):
            raise WebmasterApiError(f"Вебмастер: нет списка queries для {host_id}: {payload!r}")
        rows: list[QueryStats] = []
        for item in payload["queries"]:
            if not isinstance(item, dict):
                raise WebmasterApiError(f"Вебмастер: некорректный элемент queries для {host_id}")
            text = item.get("query_text")
            if not isinstance(text, str):
                raise WebmasterApiError(f"Вебмастер: нет query_text у элемента {item!r}")
            indicators = item.get("indicators")
            if not isinstance(indicators, dict):
                raise WebmasterApiError(f"Вебмастер: нет indicators у запроса {text!r}")
            query = normalize_query(text)
            if not query:
                self.logger.warning("webmaster_blank_query_skipped", host_id=host_id)
                continue
            query_id = item.get("query_id")
            if not isinstance(query_id, str) or not query_id:
                raise WebmasterApiError(f"Вебмастер: нет query_id у запроса {text!r}")
            rows.append(
                QueryStats(
                    query_id=query_id,
                    query=query,
                    shows=_number(indicators.get("TOTAL_SHOWS")),
                    clicks=_number(indicators.get("TOTAL_CLICKS")),
                    position=_number(indicators.get("AVG_SHOW_POSITION")),
                )
            )
        return rows

    async def _write_daily_history(
        self, user_id: int, host_id: str, domain: str, rows: list[QueryStats]
    ) -> int:
        """Дневная история каждого запроса топа (per-query, ADR-008 D1).

        prometheus_client не умеет backfill: пишется только новейшая
        завершённая (не сегодняшняя) точка каждого индикатора — ежедневный
        запуск даёт 1 точку/день, TSDB накапливает ряды. 404 на конкретный
        запрос (выпал из выдачи между /popular и /history) — пропуск, не
        ошибка сайта; остальные ошибки после ретраев — ошибка сайта.
        """
        points = 0
        for row in rows:
            try:
                history = await self._fetch_history_with_retry(user_id, host_id, row.query_id)
            except WebmasterNotFoundError as exc:
                # 404 здесь может быть только QUERY_ID_NOT_FOUND: host-уровень
                # (HOST_NOT_VERIFIED и т.п.) уже отсечён запросом /popular,
                # который выполняется первым.
                self.logger.warning(
                    "webmaster_history_query_skipped",
                    site=domain,
                    host_id=host_id,
                    query=row.query,
                    error=str(exc),
                )
                continue
            points += self._write_daily_row(domain, row.query, history)
        self.logger.info(
            "webmaster_history_done",
            site=domain,
            host_id=host_id,
            queries=len(rows),
            points=points,
        )
        return points

    async def _fetch_history_with_retry(
        self, user_id: int, host_id: str, query_id: str
    ) -> dict[str, DailyValue]:
        """История одного запроса с ретраями (единый паттерн коллектора)."""
        return await self.retry(
            lambda: self._fetch_query_history(user_id, host_id, query_id),
            retry_on=WEBMASTER_RETRYABLE,
        )

    def _write_daily_row(self, domain: str, query: str, history: dict[str, DailyValue]) -> int:
        """Пишет дневные метрики запроса. Отсутствующий индикатор пропускается (не 0)."""
        written = 0
        clicks = history.get("TOTAL_CLICKS")
        if clicks is not None:
            SEARCH_DAILY_CLICKS.labels(site=domain, query=query).set(clicks.value)
            written += 1
        shows = history.get("TOTAL_SHOWS")
        if shows is not None:
            SEARCH_DAILY_SHOWS.labels(site=domain, query=query).set(shows.value)
            written += 1
        if written:
            self.logger.info(
                "webmaster_history_written",
                site=domain,
                query=query,
                clicks=None if clicks is None else clicks.value,
                shows=None if shows is None else shows.value,
                date=clicks.date if clicks is not None else shows.date if shows else None,
                points=written,
            )
        return written

    def _history_range(self) -> tuple[str, str]:
        """Окно дат history-запросов: последние history_days дней включая сегодня."""
        today = datetime.now(tz=UTC).date()
        date_to = today.isoformat()
        date_from = (today - timedelta(days=self.history_days - 1)).isoformat()
        return date_from, date_to

    async def _fetch_query_history(
        self, user_id: int, host_id: str, query_id: str
    ) -> dict[str, DailyValue]:
        """GET .../search-queries/{query_id}/history — дневные показатели запроса.

        Возвращает {индикатор: новейшая завершённая точка}. Индикатор,
        отсутствующий в ответе или без завершённых дней, в словарь не
        попадает (метрика не пишется).
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
                    headers={"Authorization": f"OAuth {self.oauth_token}"},
                )
        except httpx.HTTPError as exc:
            self.logger.error(
                "webmaster_api_transport_error",
                operation="fetch_query_history",
                host_id=host_id,
                query_id=query_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise
        self._raise_for_status(
            response, operation="fetch_query_history", host_id=host_id, query_id=query_id
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise WebmasterApiError(
                f"Вебмастер: некорректный JSON истории запроса {query_id}"
            ) from exc
        return self._parse_query_history(payload, host_id, query_id)

    def _parse_query_history(
        self, payload: Any, host_id: str, query_id: str
    ) -> dict[str, DailyValue]:
        """Разбирает ответ per-query history: индикатор → новейшая завершённая точка."""
        if not isinstance(payload, dict) or not isinstance(payload.get("indicators"), dict):
            raise WebmasterApiError(
                f"Вебмастер: нет indicators в истории запроса {query_id} для {host_id}: {payload!r}"
            )
        today_yandex = datetime.now(tz=YANDEX_TZ).date()
        result: dict[str, DailyValue] = {}
        for indicator in HISTORY_INDICATORS:
            points = payload["indicators"].get(indicator)
            if points is None:
                continue  # индикатор не определён (документировано API)
            newest = self._newest_complete_point(points, indicator, query_id, today_yandex)
            if newest is not None:
                result[indicator] = newest
        return result

    def _newest_complete_point(
        self,
        points: Any,
        indicator: str,
        query_id: str,
        today_yandex: date,
    ) -> DailyValue | None:
        """Новейшая точка индикатора С ИСКЛЮЧЕНИЕМ незавершённого сегодня."""
        if not isinstance(points, list):
            raise WebmasterApiError(
                f"Вебмастер: {indicator} не список в истории запроса {query_id}"
            )
        newest: DailyValue | None = None
        newest_dt: datetime | None = None
        for item in points:
            if not isinstance(item, dict):
                raise WebmasterApiError(
                    f"Вебмастер: некорректная точка {indicator} запроса {query_id}: {item!r}"
                )
            point_date = item.get("date")
            value = _number(item.get("value"))
            if not isinstance(point_date, str) or value is None:
                raise WebmasterApiError(
                    f"Вебмастер: нет date/value у точки {indicator} запроса {query_id}: {item!r}"
                )
            try:
                point_dt = datetime.fromisoformat(point_date)
            except ValueError as exc:
                raise WebmasterApiError(
                    f"Вебмастер: некорректная date {point_date!r} запроса {query_id}"
                ) from exc
            if point_dt.date() >= today_yandex:
                continue  # сегодняшний (незавершённый) день всегда нули
            if newest_dt is None or point_dt > newest_dt:
                newest_dt = point_dt
                newest = DailyValue(date=point_date, value=value)
        return newest

    def _raise_for_status(
        self,
        response: httpx.Response,
        operation: str,
        host_id: str = "",
        query_id: str = "",
    ) -> None:
        """Статус ответа API → профильные исключения (ADR-004 §4)."""
        target = " > ".join(part for part in (host_id, query_id) if part) or operation
        if response.status_code == 429:
            raise RateLimitError(
                f"Вебмастер: лимит API (429) при {operation} для {target}",
                retry_after_seconds=parse_retry_after(response),
            )
        if response.status_code in (401, 403):
            raise WebmasterAuthError(
                f"Вебмастер: {response.status_code} при {operation} для {target} "
                "(токен истёк или нет прав)"
            )
        if response.status_code == 404:
            raise WebmasterNotFoundError(
                f"Вебмастер: 404 при {operation} для {target} "
                "(host_id неверен, сайт не подтверждён/не проиндексирован)"
            )
        if response.status_code >= 500:
            raise WebmasterServerError(
                f"Вебмастер: {response.status_code} при {operation} для {target} (временный сбой)"
            )
        if response.status_code != 200:
            raise WebmasterApiError(
                f"Вебмастер: неожиданный статус {response.status_code} при {operation}"
            )
