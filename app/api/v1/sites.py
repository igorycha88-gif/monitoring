"""Служебный API: список мониторируемых сайтов."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from app.config import Settings, get_settings
from app.logging import get_logger
from app.sites import SiteConfigError, load_sites

router = APIRouter(prefix="/sites", tags=["sites"])
logger = get_logger("api.sites")


@router.get("")
async def list_sites(
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    """Возвращает список сайтов из config/sites.yml."""
    try:
        sites = load_sites(settings.sites_config_path)
    except SiteConfigError as exc:
        logger.error("sites_config_error", error=str(exc), operation="list_sites")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"count": len(sites), "sites": [site.model_dump() for site in sites]}
