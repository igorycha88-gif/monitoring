"""Точка входа FastAPI: маршруты, метрики, планировщик коллекторов."""

import contextlib
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, generate_latest

from app.api.v1 import health, relay, sd, sites
from app.config import get_settings
from app.logging import get_logger, setup_logging
from app.sites import SiteConfig, load_sites
from app.storage import WebmasterStorage
from app.webmaster_export import WebmasterExporter, register_webmaster_exporter
from collectors.metrika import MetrikaCollector
from collectors.sitemetrics import SiteMetricsCollector
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
    webmaster_storage: WebmasterStorage | None = None
    webmaster_exporter: WebmasterExporter | None = None
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
        if settings.webmaster_db_path:
            webmaster_storage = WebmasterStorage(settings.webmaster_db_path)
            try:
                webmaster_storage.init()
            except Exception as exc:
                # БД — долговременное сырьё, не основной канал: отказ хранения
                # не должен ронять мониторинг (ADR-009 D4)
                logger.error(
                    "storage_init_failed",
                    path=settings.webmaster_db_path,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
                webmaster_storage = None
        else:
            logger.info("storage_disabled", reason="empty_webmaster_db_path")
        if webmaster_storage is not None:
            # Рендер поисковых метрик из БД (ADR-011): register + фоновый
            # поток обновления кэша; метрики доступны сразу после старта
            webmaster_exporter = WebmasterExporter(
                webmaster_storage,
                render_days=settings.webmaster_render_days,
                refresh_seconds=settings.webmaster_render_refresh_seconds,
                lag_days=settings.webmaster_render_lag_days,
            )
            register_webmaster_exporter(webmaster_exporter, REGISTRY)
            webmaster_exporter.start()
        else:
            logger.warning(
                "webmaster_export_skipped",
                reason="storage_unavailable",
            )
        webmaster_oauth_tokens = {
            site.domain: settings.webmaster_token(site.domain)
            for site in _sites_with_webmaster(sites_list)
        }
        without_token = sorted(
            domain for domain, token in webmaster_oauth_tokens.items() if not token
        )
        if without_token:
            logger.warning(
                "webmaster_oauth_tokens_empty",
                reason="персайтный токен и YANDEX_WEBMASTER_OAUTH_TOKEN не заданы — "
                "сбор сайта завершится ошибкой",
                sites=without_token,
            )
        webmaster_collector = WebmasterCollector(
            sites_list,
            oauth_tokens=webmaster_oauth_tokens,
            timeout_seconds=settings.webmaster_timeout_seconds,
            top_queries=settings.webmaster_top_queries,
            history_days=settings.webmaster_history_days,
            storage=webmaster_storage,
        )
        webmaster_collector.register_daily(
            scheduler,
            hour=settings.webmaster_cron_hour,
            timezone=settings.webmaster_cron_timezone,
        )
        collectors.append("webmaster")
    else:
        logger.info("webmaster_collector_skipped", reason="no_hosts")
    if _sites_with_metrics_urls(sites_list):
        api_keys = {
            site.domain: settings.site_metrics_key(site.domain)
            for site in _sites_with_metrics_urls(sites_list)
        }
        without_key = sorted(domain for domain, key in api_keys.items() if not key)
        if without_key:
            logger.warning(
                "site_metrics_api_keys_empty",
                reason="персайтный ключ и SITE_METRICS_API_KEY не заданы — эндпоинты ответят 403",
                sites=without_key,
            )
        site_metrics_collector = SiteMetricsCollector(
            sites_list,
            timeout_seconds=settings.site_metrics_timeout_seconds,
            api_keys=api_keys,
        )
        site_metrics_collector.register(scheduler, settings.site_metrics_interval_seconds)
        collectors.append("site-metrics")
    else:
        logger.info("site_metrics_collector_skipped", reason="no_metrics_urls")
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
        if webmaster_exporter is not None:
            webmaster_exporter.stop()
            with contextlib.suppress(KeyError):
                REGISTRY.unregister(webmaster_exporter)
        if webmaster_storage is not None:
            webmaster_storage.close()
        logger.info("app_stopped")


def _sites_with_counters(sites: list[SiteConfig]) -> list[SiteConfig]:
    """Сайты, у которых настроен счётчик Метрики."""
    return [site for site in sites if site.metrika_counter_id is not None]


def _sites_with_webmaster(sites: list[SiteConfig]) -> list[SiteConfig]:
    """Сайты, у которых настроен host_id Вебмастера."""
    return [site for site in sites if site.webmaster_host_id is not None]


def _sites_with_metrics_urls(sites: list[SiteConfig]) -> list[SiteConfig]:
    """Сайты с эндпоинтами метрик за X-Monitoring-Key (ЭПИК-9)."""
    return [site for site in sites if site.metrics_urls]


def create_app() -> FastAPI:
    """Создаёт и настраивает экземпляр приложения."""
    application = FastAPI(title="Monitoring", version="0.1.0", lifespan=lifespan)
    application.include_router(health.router)
    application.include_router(sites.router, prefix="/api/v1")
    application.include_router(sd.router, prefix="/api/v1")
    application.include_router(relay.router, prefix="/api/v1")

    @application.middleware("http")
    async def log_requests(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        started = time.perf_counter()
        response = await call_next(request)
        # /metrics и relay-скрейпы (каждые 15 с × kinds × сайты) не логируются
        # на INFO — иначе шум; relay пишет событие relay_scrape на debug
        path = request.url.path.rstrip("/")
        if path != "/metrics" and not path.startswith("/api/v1/relay/"):
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
