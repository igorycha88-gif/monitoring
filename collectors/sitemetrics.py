"""Health-коллектор эндпоинтов метрик сайтов (ЭПИК-9, ADR-007 D4)."""

import time
from collections.abc import Sequence

import httpx

from app.metrics import (
    SITE_METRICS_LATENCY_SECONDS,
    SITE_METRICS_RESPONSE_CODE,
    SITE_METRICS_UP,
)
from app.sites import SiteConfig
from collectors.base import BaseCollector

MONITORING_KEY_HEADER = "X-Monitoring-Key"


class SiteMetricsCollector(BaseCollector):
    """Проверка доступности эндпоинтов метрик сайтов.

    Философия uptime-коллектора (ADR-002): одна попытка за цикл (цикл 60 с —
    сам ретрай); HTTP 403/404/5xx и сетевые ошибки — это ДАННЫЕ
    «эндпоинт недоступен» (site_metrics_up=0), а не ошибка коллектора
    (collector_success=1). Ошибкой коллектора считается только неожиданное
    исключение вне HTTP-обмена.
    """

    source = "site-metrics"
    parallel = True

    def __init__(
        self,
        sites: Sequence[SiteConfig],
        timeout_seconds: float,
        api_key: str,
    ) -> None:
        super().__init__(sites)
        self.timeout_seconds = timeout_seconds
        self.api_key = api_key

    async def collect_site(self, site: SiteConfig) -> int:
        """Проверяет все metrics_urls сайта; пишет по 3 метрики на kind."""
        urls = site.metrics_urls or {}
        if not urls:
            return 0
        points = 0
        async with httpx.AsyncClient(
            timeout=self.timeout_seconds,
            headers={MONITORING_KEY_HEADER: self.api_key},
            follow_redirects=False,
        ) as client:
            for kind, url in sorted(urls.items()):
                started = time.perf_counter()
                try:
                    response = await client.get(url)
                except httpx.HTTPError as exc:
                    elapsed = time.perf_counter() - started
                    SITE_METRICS_UP.labels(site=site.domain, kind=kind).set(0)
                    SITE_METRICS_RESPONSE_CODE.labels(site=site.domain, kind=kind).set(0)
                    SITE_METRICS_LATENCY_SECONDS.labels(site=site.domain, kind=kind).set(elapsed)
                    self.logger.warning(
                        "site_metrics_check_failed",
                        site=site.domain,
                        kind=kind,
                        url=url,
                        error=str(exc),
                        error_type=type(exc).__name__,
                        operation="http_check",
                    )
                    points += 3
                    continue
                elapsed = time.perf_counter() - started
                up = 1 if 200 <= response.status_code < 300 else 0
                SITE_METRICS_UP.labels(site=site.domain, kind=kind).set(up)
                SITE_METRICS_RESPONSE_CODE.labels(site=site.domain, kind=kind).set(
                    response.status_code
                )
                SITE_METRICS_LATENCY_SECONDS.labels(site=site.domain, kind=kind).set(elapsed)
                self.logger.info(
                    "site_metrics_check_done",
                    site=site.domain,
                    kind=kind,
                    url=url,
                    status_code=response.status_code,
                    up=up,
                    duration_ms=round(elapsed * 1000, 2),
                )
                points += 3
        return points
