"""HTTP SD для Prometheus: цели node_exporter и метрик сайтов (ЭПИК-6, ЭПИК-9)."""

from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException

from app.config import Settings, get_settings
from app.logging import get_logger
from app.sites import SITE_METRICS_KINDS, SiteConfig, SiteConfigError, load_sites

router = APIRouter(prefix="/sd", tags=["service-discovery"])
logger = get_logger("api.sd")

DEFAULT_NODE_EXPORTER_PORT = 9100


def build_target(url: str, default_port: int = DEFAULT_NODE_EXPORTER_PORT) -> str:
    """URL exporter'а → SD-target host:port (порт по умолчанию 9100)."""
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    port = parsed.port or default_port
    if ":" in host:
        return f"[{host}]:{port}"
    return f"{host}:{port}"


def url_labels(url: str) -> dict[str, str]:
    """SD-лейблы из URL: __scheme__/__metrics_path__ — при отклонении от дефолта."""
    parsed = urlsplit(url)
    labels: dict[str, str] = {}
    if parsed.scheme == "https":
        labels["__scheme__"] = "https"
    if parsed.path and parsed.path != "/":
        labels["__metrics_path__"] = parsed.path
    return labels


def build_labels(site: SiteConfig) -> dict[str, str]:
    """SD-лейблы job'а node-exporter: site + лейблы из node_exporter_url."""
    return {"site": site.domain, **url_labels(site.node_exporter_url or "")}


@router.get("/node-exporter")
async def discover_node_exporter(
    settings: Annotated[Settings, Depends(get_settings)],
) -> list[dict[str, object]]:
    """HTTP SD-ответ Prometheus (job node-exporter): группы targets + labels."""
    try:
        sites = load_sites(settings.sites_config_path)
    except SiteConfigError as exc:
        logger.error("sites_config_error", error=str(exc), operation="sd_node_exporter")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    configured = [site for site in sites if site.node_exporter_url]
    groups: list[dict[str, object]] = [
        {
            "targets": [build_target(site.node_exporter_url or "")],
            "labels": build_labels(site),
        }
        for site in configured
    ]
    logger.info("sd_targets_served", job="node-exporter", targets=len(groups))
    return groups


@router.get("/site-metrics/{kind}")
async def discover_site_metrics(
    kind: str,
    settings: Annotated[Settings, Depends(get_settings)],
) -> list[dict[str, object]]:
    """HTTP SD-ответ Prometheus (jobs site-*): эндпоинты метрик сайтов (ADR-007 D3).

    kinds: tracking | content | node | postgres (whitelist из app.sites).
    Таргеты вида host:443 с __scheme__=https и __metrics_path__ из URL.
    """
    if kind not in SITE_METRICS_KINDS:
        raise HTTPException(
            status_code=404,
            detail=f"Неизвестный kind {kind!r}; допустимо: {', '.join(sorted(SITE_METRICS_KINDS))}",
        )
    try:
        sites = load_sites(settings.sites_config_path)
    except SiteConfigError as exc:
        logger.error("sites_config_error", error=str(exc), operation="sd_site_metrics")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    groups: list[dict[str, object]] = []
    for site in sites:
        url = (site.metrics_urls or {}).get(kind)
        if url is None:
            continue
        groups.append(
            {
                "targets": [build_target(url, default_port=443)],
                "labels": {"site": site.domain, **url_labels(url)},
            }
        )
    logger.info("sd_targets_served", job=f"site-{kind}", targets=len(groups))
    return groups
