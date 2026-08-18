"""HTTP SD для Prometheus: цели node_exporter из config/sites.yml (ЭПИК-6)."""

from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException

from app.config import Settings, get_settings
from app.logging import get_logger
from app.sites import SiteConfig, SiteConfigError, load_sites

router = APIRouter(prefix="/sd", tags=["service-discovery"])
logger = get_logger("api.sd")

DEFAULT_NODE_EXPORTER_PORT = 9100


def build_target(url: str) -> str:
    """URL node_exporter → SD-target host:port (порт по умолчанию 9100)."""
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    port = parsed.port or DEFAULT_NODE_EXPORTER_PORT
    if ":" in host:
        return f"[{host}]:{port}"
    return f"{host}:{port}"


def build_labels(site: SiteConfig) -> dict[str, str]:
    """SD-лейблы: site — всегда; __scheme__/__metrics_path__ — при отклонении от дефолта."""
    parsed = urlsplit(site.node_exporter_url or "")
    labels = {"site": site.domain}
    if parsed.scheme == "https":
        labels["__scheme__"] = "https"
    if parsed.path and parsed.path != "/":
        labels["__metrics_path__"] = parsed.path
    return labels


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
