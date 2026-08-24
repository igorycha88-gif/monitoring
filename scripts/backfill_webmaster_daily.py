"""Разовый backfill по-дневной истории Вебмастера в SQLite (ЧТЗ_Вебмастер_Backfill_по_дням).

Заполняет webmaster_daily задним числом: API Вебмастера отдаёт per-query
history за окно до 31 дня. Переиспользует WebmasterCollector.collect_site —
штатный сбор с фиксацией 2026-08-24 пишет ВСЕ завершённые дни окна (upsert
по site+query_id+date — повторный прогон безопасен), backfill отличается
только широким окном (env WEBMASTER_HISTORY_DAYS) и паузами между
history-запросами (лимиты API).

Запуск (внутри контейнера, окно 31 день):
    docker exec -e WEBMASTER_HISTORY_DAYS=31 monitoring-app \
        python scripts/backfill_webmaster_daily.py
"""

import asyncio

from app.config import get_settings
from app.logging import get_logger, setup_logging
from app.sites import load_sites
from app.storage import WebmasterStorage
from collectors.webmaster import WebmasterCollector, WebmasterConfigError

REQUEST_PAUSE_SECONDS = 0.5

logger = get_logger("scripts.backfill_webmaster_daily")


async def main() -> None:
    """Точка входа: backfill по всем сайтам с webmaster_host_id."""
    settings = get_settings()
    setup_logging(settings.log_level, settings.log_format)
    sites = load_sites(settings.sites_config_path)
    wm_sites = [site for site in sites if site.webmaster_host_id is not None]
    if not wm_sites:
        logger.error("backfill_failed", reason="no_webmaster_sites")
        raise SystemExit(1)
    tokens = {site.domain: settings.webmaster_token(site.domain) for site in wm_sites}
    storage = WebmasterStorage(settings.webmaster_db_path)
    storage.init()
    collector = WebmasterCollector(
        sites,
        oauth_tokens=tokens,
        timeout_seconds=settings.webmaster_timeout_seconds,
        top_queries=settings.webmaster_top_queries,
        history_days=settings.webmaster_history_days,
        storage=storage,
        request_pause_seconds=REQUEST_PAUSE_SECONDS,
    )
    collector.retry_base_delay = 1.0
    logger.info(
        "backfill_started",
        sites=[site.domain for site in wm_sites],
        history_days=settings.webmaster_history_days,
        request_pause_seconds=REQUEST_PAUSE_SECONDS,
    )
    total = 0
    try:
        for site in collector.sites:
            try:
                written = await collector.collect_site(site)
            except WebmasterConfigError as exc:
                # сайт без токена — пропуск, остальные сайты бэкапятся
                logger.error("backfill_site_skipped", site=site.domain, reason=str(exc))
                continue
            logger.info(
                "backfill_site_done",
                site=site.domain,
                rows_written=written,
            )
            total += written
    finally:
        storage.close()
    logger.info("backfill_finished", rows_written=total)


if __name__ == "__main__":
    asyncio.run(main())
