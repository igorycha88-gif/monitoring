"""Relay-прокси метрик сайтов: Prometheus → app → сайт за персайтным ключом (ADR-010 D2)."""

import time
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, Response

from app.config import Settings, get_settings
from app.logging import get_logger
from app.sites import SITE_METRICS_KINDS, SiteConfigError, load_sites
from collectors.sitemetrics import MONITORING_KEY_HEADER

router = APIRouter(prefix="/relay", tags=["relay"])
logger = get_logger("api.relay")


@router.get("/site-metrics/{kind}/{site}")
async def relay_site_metrics(
    kind: str,
    site: str,
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    """Проксирует выгрузку метрик сайта с его ключом X-Monitoring-Key.

    Таргет SD (ADR-010 D3): app:8088 + __metrics_path__/api/v1/relay/...
    Не-2xx/сетевая ошибка → 502 (для Prometheus up=0). Ключ не логируется.
    """
    started = time.perf_counter()
    if kind not in SITE_METRICS_KINDS:
        raise HTTPException(
            status_code=404,
            detail=f"Неизвестный kind {kind!r}; допустимо: {', '.join(sorted(SITE_METRICS_KINDS))}",
        )
    try:
        sites = load_sites(settings.sites_config_path)
    except SiteConfigError as exc:
        logger.error("sites_config_error", error=str(exc), operation="relay_site_metrics")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    domain = site.strip().lower()
    target = next((s for s in sites if s.domain == domain), None)
    url = (target.metrics_urls or {}).get(kind) if target else None
    if target is None or url is None:
        raise HTTPException(
            status_code=404,
            detail=f"Сайт {domain!r} не настроен для kind {kind!r} (sites.yml)",
        )
    key = settings.site_metrics_key(domain)
    try:
        async with httpx.AsyncClient(
            timeout=settings.site_metrics_timeout_seconds,
            headers={MONITORING_KEY_HEADER: key},
            follow_redirects=False,
        ) as client:
            upstream = await client.get(url)
    except httpx.HTTPError as exc:
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        logger.warning(
            "relay_scrape_failed",
            site=domain,
            kind=kind,
            error=str(exc),
            error_type=type(exc).__name__,
            duration_ms=duration_ms,
            operation="relay_fetch",
        )
        raise HTTPException(status_code=502, detail=f"Эндпоинт метрик недоступен: {exc}") from exc
    if not 200 <= upstream.status_code < 300:
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        logger.warning(
            "relay_scrape_failed",
            site=domain,
            kind=kind,
            upstream_status=upstream.status_code,
            duration_ms=duration_ms,
            operation="relay_fetch",
        )
        raise HTTPException(
            status_code=502,
            detail=f"Эндпоинт метрик ответил {upstream.status_code}",
        )
    logger.debug(
        "relay_scrape",
        site=domain,
        kind=kind,
        upstream_status=upstream.status_code,
        bytes=len(upstream.content),
        duration_ms=round((time.perf_counter() - started) * 1000, 2),
    )
    return Response(
        content=upstream.content,
        media_type=upstream.headers.get("content-type", "text/plain; charset=utf-8"),
    )
