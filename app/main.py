"""Точка входа FastAPI: маршруты, метрики, планировщик коллекторов."""

import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.api.v1 import health, sites
from app.config import get_settings
from app.logging import get_logger, setup_logging
from app.sites import SiteConfig, load_sites
from collectors.metrika import MetrikaCollector
from collectors.uptime import SSLCollector, UptimeCollector
from collectors.webmaster import WebmasterCollector

logger = get_logger("app")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Инициализация/остановка: логирование + планировщик коллекторов."""
    settings = get_settings()
    setup_logging(settings.log_level, settings.log_format)
    sites_list = load_sites(settings.sites_config_path)
    scheduler = AsyncIOScheduler(timezone="UTC")
    uptime_collector = UptimeCollector(
        sites_list,
        timeout_seconds=settings.uptime_timeout_seconds,
        success_max_code=settings.uptime_success_max_code,
    )
    uptime_collector.register(scheduler, settings.uptime_interval_seconds)
    ssl_collector = SSLCollector(sites_list, timeout_seconds=settings.ssl_timeout_seconds)
    ssl_collector.register(scheduler, settings.ssl_interval_seconds)
    collectors = ["uptime", "ssl"]
    metrika_sites = _sites_with_counters(sites_list)
    if metrika_sites:
        metrika_collector = MetrikaCollector(
            sites_list,
            oauth_token=settings.yandex_metrika_oauth_token,
            timeout_seconds=settings.metrika_timeout_seconds,
        )
        metrika_collector.register(scheduler, settings.metrika_interval_seconds)
        collectors.append("metrika")
    else:
        logger.info("metrika_collector_skipped", reason="no_counters")
    if _sites_with_webmaster(sites_list):
        webmaster_collector = WebmasterCollector(
            sites_list,
            oauth_token=settings.yandex_webmaster_oauth_token,
            timeout_seconds=settings.webmaster_timeout_seconds,
            top_queries=settings.webmaster_top_queries,
        )
        webmaster_collector.register(scheduler, settings.webmaster_interval_seconds)
        collectors.append("webmaster")
    else:
        logger.info("webmaster_collector_skipped", reason="no_hosts")
    scheduler.start()
    app.state.scheduler = scheduler
    logger.info(
        "app_started",
        scheduler_running=scheduler.running,
        sites=len(sites_list),
        collectors=collectors,
    )
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)
        logger.info("app_stopped")


def _sites_with_counters(sites: list[SiteConfig]) -> list[SiteConfig]:
    """Сайты, у которых настроен счётчик Метрики."""
    return [site for site in sites if site.metrika_counter_id is not None]


def _sites_with_webmaster(sites: list[SiteConfig]) -> list[SiteConfig]:
    """Сайты, у которых настроен host_id Вебмастера."""
    return [site for site in sites if site.webmaster_host_id is not None]


def create_app() -> FastAPI:
    """Создаёт и настраивает экземпляр приложения."""
    application = FastAPI(title="Monitoring", version="0.1.0", lifespan=lifespan)
    application.include_router(health.router)
    application.include_router(sites.router, prefix="/api/v1")

    @application.middleware("http")
    async def log_requests(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        started = time.perf_counter()
        response = await call_next(request)
        if request.url.path.rstrip("/") != "/metrics":
            logger.info(
                "http_request",
                method=request.method,
                path=request.url.path,
                status=response.status_code,
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
            )
        return response

    @application.get("/metrics")
    async def metrics_endpoint() -> Response:
        """Экспорт метрик Prometheus без редиректа (не Mount → без 307)."""
        logger.debug("metrics_scraped")
        return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return application


app = create_app()
