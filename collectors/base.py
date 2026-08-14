"""Базовый класс коллекторов: метрики, логирование, ретраи, расписание."""

import asyncio
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Sequence
from typing import TypeVar

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.logging import get_logger
from app.metrics import (
    COLLECTOR_DURATION_SECONDS,
    COLLECTOR_ERRORS_TOTAL,
    COLLECTOR_SUCCESS,
)
from app.sites import SiteConfig

T = TypeVar("T")

RETRYABLE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    httpx.TimeoutException,
    httpx.TransportError,
)


class BaseCollector(ABC):
    """Базовый коллектор.

    Наследники реализуют collect_site(). Базовый класс обеспечивает метрики
    monitoring_collector_*, структурированные логи цикла, ретраи с
    экспоненциальным backoff и регистрацию в планировщике.
    """

    source: str = "base"
    max_retries: int = 3
    retry_base_delay: float = 1.0

    def __init__(self, sites: Sequence[SiteConfig]) -> None:
        self.sites = list(sites)
        self.logger = get_logger(f"collector.{self.source}")

    @abstractmethod
    async def collect_site(self, site: SiteConfig) -> int:
        """Собирает данные по одному сайту. Возвращает число собранных точек."""

    async def retry(
        self,
        func: Callable[[], Awaitable[T]],
        retry_on: tuple[type[BaseException], ...] = RETRYABLE_EXCEPTIONS,
    ) -> T:
        """Выполняет func с ретраями и экспоненциальным backoff."""
        attempt = 0
        while True:
            try:
                return await func()
            except retry_on as exc:
                attempt += 1
                if attempt >= self.max_retries:
                    raise
                delay = self.retry_base_delay * 2 ** (attempt - 1)
                self.logger.warning(
                    "collector_retry",
                    source=self.source,
                    attempt=attempt,
                    max_retries=self.max_retries,
                    delay_seconds=delay,
                    error=str(exc),
                )
                await asyncio.sleep(delay)

    async def run_once(self) -> dict[str, int]:
        """Один цикл сбора по всем сайтам: метрики + логи, ошибки не рвут цикл."""
        self.logger.info("collector_cycle_start", source=self.source, sites_total=len(self.sites))
        sites_ok = 0
        points_total = 0
        for site in self.sites:
            started = time.perf_counter()
            try:
                points = await self.collect_site(site)
            except Exception as exc:
                duration = time.perf_counter() - started
                COLLECTOR_ERRORS_TOTAL.labels(source=self.source, site=site.domain).inc()
                COLLECTOR_SUCCESS.labels(source=self.source, site=site.domain).set(0)
                COLLECTOR_DURATION_SECONDS.labels(source=self.source, site=site.domain).set(
                    duration
                )
                self.logger.error(
                    "collector_site_error",
                    source=self.source,
                    site=site.domain,
                    error=str(exc),
                    error_type=type(exc).__name__,
                    operation="collect_site",
                )
            else:
                duration = time.perf_counter() - started
                COLLECTOR_SUCCESS.labels(source=self.source, site=site.domain).set(1)
                COLLECTOR_DURATION_SECONDS.labels(source=self.source, site=site.domain).set(
                    duration
                )
                sites_ok += 1
                points_total += points
                self.logger.info(
                    "collector_site_success",
                    source=self.source,
                    site=site.domain,
                    points=points,
                    duration_ms=round(duration * 1000, 2),
                )
        self.logger.info(
            "collector_cycle_end",
            source=self.source,
            sites_ok=sites_ok,
            sites_total=len(self.sites),
            points_total=points_total,
        )
        return {
            "sites_ok": sites_ok,
            "sites_total": len(self.sites),
            "points_total": points_total,
        }

    def register(self, scheduler: AsyncIOScheduler, interval_seconds: int) -> None:
        """Регистрирует периодический запуск коллектора в планировщике."""
        scheduler.add_job(
            self.run_once,
            trigger="interval",
            seconds=interval_seconds,
            id=f"collector_{self.source}",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
        self.logger.info(
            "collector_registered",
            source=self.source,
            interval_seconds=interval_seconds,
            sites=len(self.sites),
        )
