"""Точка входа FastAPI: маршруты, метрики, планировщик коллекторов."""

import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, Request, Response
from prometheus_client import make_asgi_app

from app.api.v1 import health, sites
from app.config import get_settings
from app.logging import get_logger, setup_logging

logger = get_logger("app")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Инициализация/остановка: логирование + планировщик коллекторов."""
    settings = get_settings()
    setup_logging(settings.log_level, settings.log_format)
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.start()
    app.state.scheduler = scheduler
    logger.info("app_started", scheduler_running=scheduler.running)
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)
        logger.info("app_stopped")


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

    application.mount("/metrics", make_asgi_app())
    return application


app = create_app()
