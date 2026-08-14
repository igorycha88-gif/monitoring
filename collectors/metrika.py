"""Коллектор Яндекс.Метрики: визиты и посетители за текущий день (ЭПИК-2, ADR-003)."""

from collections.abc import Sequence
from datetime import UTC, datetime

import httpx

from app.metrics import SITE_VISITORS, SITE_VISITS_TOTAL
from app.sites import SiteConfig
from collectors.base import BaseCollector, RateLimitError

METRIKA_API_URL = "https://api-metrika.yandex.net/stat/v1/data"
METRIKA_METRICS = "ym:s:visits,ym:s:users"


class MetrikaApiError(Exception):
    """Ошибка Reporting API Яндекс.Метрики."""


class MetrikaAuthError(MetrikaApiError):
    """401/403: токен недействителен или нет доступа к счётчику (ретраи бесполезны)."""


class MetrikaNotFoundError(MetrikaApiError):
    """404: счётчик не найден (ретраи бесполезны)."""


class MetrikaConfigError(MetrikaApiError):
    """Ошибка конфигурации: пустой OAuth-токен."""


class MetrikaServerError(MetrikaApiError):
    """5xx: временный сбой API Метрики — ретраится (ADR-003)."""


METRIKA_RETRYABLE: tuple[type[BaseException], ...] = (
    httpx.TimeoutException,
    httpx.TransportError,
    RateLimitError,
    MetrikaServerError,
)


def parse_retry_after(response: httpx.Response) -> float | None:
    """Retry-After в секундах (delta-seconds). HTTP-date/отсутствие/мусор → None."""
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


class MetrikaCollector(BaseCollector):
    """Сбор визитов/посетителей за сегодня нарастающим итогом (ЭПИК-2, ADR-003).

    Последовательный обход (лимиты API); значения из totals ответа — пустой
    отчёт это нули (данные, не ошибка). Сайты без metrika_counter_id
    пропускаются при инициализации: метрики collector_* для них не создаются.
    """

    source = "metrika"
    parallel = False

    def __init__(
        self,
        sites: Sequence[SiteConfig],
        oauth_token: str,
        timeout_seconds: float,
        api_url: str = METRIKA_API_URL,
    ) -> None:
        super().__init__(sites)
        all_sites = self.sites
        self.sites = [site for site in all_sites if site.metrika_counter_id is not None]
        self.skipped_sites = len(all_sites) - len(self.sites)
        self.oauth_token = oauth_token
        self.timeout_seconds = timeout_seconds
        self.api_url = api_url
        self.logger.info(
            "metrika_collector_init",
            counters=len(self.sites),
            skipped_sites=self.skipped_sites,
        )

    async def collect_site(self, site: SiteConfig) -> int:
        """Пишет 2 метрики (визиты/посетители за сегодня). Возвращает число точек."""
        counter_id = site.metrika_counter_id
        if counter_id is None:
            return 0
        if not self.oauth_token:
            raise MetrikaConfigError("YANDEX_METRIKA_OAUTH_TOKEN пуст (секреты в .env)")
        date = datetime.now(tz=UTC).strftime("%Y-%m-%d")
        visits, visitors = await self.retry(
            lambda: self._fetch_totals(counter_id, date),
            retry_on=METRIKA_RETRYABLE,
        )
        SITE_VISITS_TOTAL.labels(site=site.domain, source=self.source).set(visits)
        SITE_VISITORS.labels(site=site.domain, source=self.source).set(visitors)
        self.logger.info(
            "metrika_collect_done",
            site=site.domain,
            counter_id=counter_id,
            date=date,
            visits=visits,
            visitors=visitors,
        )
        return 2

    async def _fetch_totals(self, counter_id: int, date: str) -> tuple[float, float]:
        """Запрашивает totals (visits, users) за один день. Обрабатывает статусы API."""
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.get(
                    self.api_url,
                    params={
                        "ids": counter_id,
                        "metrics": METRIKA_METRICS,
                        "date1": date,
                        "date2": date,
                    },
                    headers={"Authorization": f"OAuth {self.oauth_token}"},
                )
        except httpx.HTTPError as exc:
            self.logger.error(
                "metrika_api_transport_error",
                counter_id=counter_id,
                error=str(exc),
                error_type=type(exc).__name__,
                operation="fetch_totals",
            )
            raise
        if response.status_code == 429:
            raise RateLimitError(
                f"Метрика: лимит API (429) для счётчика {counter_id}",
                retry_after_seconds=parse_retry_after(response),
            )
        if response.status_code in (401, 403):
            raise MetrikaAuthError(
                f"Метрика: {response.status_code} для счётчика {counter_id} "
                "(токен истёк или нет доступа)"
            )
        if response.status_code == 404:
            raise MetrikaNotFoundError(f"Метрика: счётчик {counter_id} не найден")
        if response.status_code >= 500:
            raise MetrikaServerError(
                f"Метрика: {response.status_code} для счётчика {counter_id} (временный сбой)"
            )
        if response.status_code != 200:
            raise MetrikaApiError(
                f"Метрика: неожиданный статус {response.status_code} для счётчика {counter_id}"
            )
        try:
            totals = response.json()["totals"]
        except (KeyError, ValueError) as exc:
            raise MetrikaApiError(
                f"Метрика: некорректный JSON ответа для счётчика {counter_id}"
            ) from exc
        if not isinstance(totals, list) or len(totals) != 2:
            raise MetrikaApiError(f"Метрика: ожидался totals из 2 значений, получено: {totals!r}")
        try:
            visits, visitors = float(totals[0]), float(totals[1])
        except (TypeError, ValueError) as exc:
            raise MetrikaApiError(
                f"Метрика: нечисловой totals для счётчика {counter_id}: {totals!r}"
            ) from exc
        return visits, visitors
