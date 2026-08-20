"""HTTP SD для Prometheus: цели node_exporter и метрик сайтов (ЭПИК-6, ЭПИК-9, ADR-010)."""

from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException

from app.config import Settings, get_settings
from app.logging import get_logger
from app.sites import SITE_METRICS_KINDS, SiteConfig, SiteConfigError, load_sites

router = APIRouter(prefix="/sd", tags=["service-discovery"])
logger = get_logger("api.sd")

DEFAULT_NODE_EXPORTER_PORT = 9100
# ADR-010 D3: таргет relay (само приложение) для job'ов site-*
RELAY_TARGET = "app:8088"
RELAY_PATH_PREFIX = "/api/v1/relay/site-metrics"


def relay_path(kind: str, domain: str) -> str:
    """__metrics_path__ relay-таргета: домен — сырой unicode (ADR-010 D3).

    percent-кодирование UTF-8 выполняет Prometheus при построении URI:
    предзакодированное значение он кодирует повторно (% → %25).
    """
    return f"{RELAY_PATH_PREFIX}/{kind}/{domain}"


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
    """HTTP SD-ответ Prometheus (jobs site-*): relay-таргеты метрик сайтов (ADR-010 D3).

    kinds: tracking | content | node | postgres (whitelist из app.sites).
    Таргет — relay приложения (app:8088): ключ сайта подставляет relay,
    Prometheus скрейпит без заголовков. Лейбл site — из поля domain.
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
        if (site.metrics_urls or {}).get(kind) is None:
            continue
        groups.append(
            {
                "targets": [RELAY_TARGET],
                "labels": {
                    "site": site.domain,
                    "__metrics_path__": relay_path(kind, site.domain),
                },
            }
        )
    logger.info("sd_targets_served", job=f"site-{kind}", targets=len(groups))
    return groups
