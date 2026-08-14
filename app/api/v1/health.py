"""Healthcheck endpoint."""

from fastapi import APIRouter

from app.logging import get_logger

router = APIRouter(tags=["health"])
logger = get_logger("api.health")


@router.get("/health")
async def health() -> dict[str, str]:
    """Живость приложения (Docker healthcheck, балансировщики)."""
    logger.debug("health_checked")
    return {"status": "ok"}
