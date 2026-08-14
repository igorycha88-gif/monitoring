"""Коллектор Яндекс.Вебмастера: топ поисковых запросов за неделю (ЭПИК-5, ADR-004)."""

import re
from collections.abc import Sequence
from typing import Any, NamedTuple

import httpx

from app.metrics import SEARCH_CLICKS_TOTAL, SEARCH_POSITION, SEARCH_SHOWS_TOTAL
from app.sites import SiteConfig
from collectors.base import BaseCollector, RateLimitError
from collectors.metrika import parse_retry_after

WEBMASTER_API_BASE = "https://api.webmaster.yandex.net/v4"
WEBMASTER_USER_URL = f"{WEBMASTER_API_BASE}/user"
QUERY_LABEL_MAX_LENGTH = 100
TOP_QUERIES_LIMIT = 100

QUERY_INDICATORS = ("TOTAL_SHOWS", "TOTAL_CLICKS", "AVG_SHOW_POSITION")

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

    query: str
    shows: float | None
    clicks: float | None
    position: float | None


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
        self._user_id: int | None = None
        self.logger.info(
            "webmaster_collector_init",
            hosts=len(self.sites),
            skipped_sites=self.skipped_sites,
            top_queries=self.top_queries,
        )

    async def collect_site(self, site: SiteConfig) -> int:
        """Пишет метрики по топу запросов сайта. Возвращает число записанных значений."""
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
            rows.append(
                QueryStats(
                    query=query,
                    shows=_number(indicators.get("TOTAL_SHOWS")),
                    clicks=_number(indicators.get("TOTAL_CLICKS")),
                    position=_number(indicators.get("AVG_SHOW_POSITION")),
                )
            )
        return rows

    def _raise_for_status(
        self, response: httpx.Response, operation: str, host_id: str = ""
    ) -> None:
        """Статус ответа API → профильные исключения (ADR-004 §4)."""
        target = host_id or operation
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
