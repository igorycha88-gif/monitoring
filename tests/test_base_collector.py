"""Тесты BaseCollector: метрики, логи цикла, ретраи, расписание."""

import httpx
import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from prometheus_client import generate_latest
from structlog.testing import capture_logs

from app.sites import SiteConfig
from collectors.base import BaseCollector

SITES = [SiteConfig(domain="ok1.com"), SiteConfig(domain="ok2.com")]


class DummyCollector(BaseCollector):
    """Тестовый коллектор: падает на заданных доменах."""

    source = "dummy"

    def __init__(
        self,
        sites: list[SiteConfig],
        fail_domains: set[str] | None = None,
        points: int = 3,
    ) -> None:
        super().__init__(sites)
        self.fail_domains = fail_domains or set()
        self.points = points

    async def collect_site(self, site: SiteConfig) -> int:
        if site.domain in self.fail_domains:
            raise RuntimeError(f"boom {site.domain}")
        return self.points


async def test_run_once_success() -> None:
    collector = DummyCollector(SITES)
    with capture_logs() as captured:
        result = await collector.run_once()
    assert result == {"sites_ok": 2, "sites_total": 2, "points_total": 6}
    events = [entry["event"] for entry in captured]
    assert "collector_cycle_start" in events
    assert "collector_cycle_end" in events
    assert events.count("collector_site_success") == 2


async def test_run_once_success_metrics() -> None:
    collector = DummyCollector(SITES)
    await collector.run_once()
    metrics = generate_latest().decode()
    assert 'monitoring_collector_success{site="ok1.com",source="dummy"} 1.0' in metrics
    assert 'monitoring_collector_duration_seconds{site="ok1.com",source="dummy"}' in metrics


async def test_run_once_site_error_does_not_break_cycle() -> None:
    sites = [SiteConfig(domain="err.com"), SiteConfig(domain="fine.com")]
    collector = DummyCollector(sites, fail_domains={"err.com"})
    with capture_logs() as captured:
        result = await collector.run_once()
    assert result["sites_ok"] == 1
    assert result["points_total"] == 3
    error_logs = [entry for entry in captured if entry["event"] == "collector_site_error"]
    assert len(error_logs) == 1
    assert error_logs[0]["site"] == "err.com"
    assert error_logs[0]["error_type"] == "RuntimeError"
    assert error_logs[0]["operation"] == "collect_site"


async def test_run_once_error_metrics() -> None:
    collector = DummyCollector(
        [SiteConfig(domain="metric-err.com")], fail_domains={"metric-err.com"}
    )
    await collector.run_once()
    metrics = generate_latest().decode()
    assert 'monitoring_collector_errors_total{site="metric-err.com",source="dummy"}' in metrics
    assert 'monitoring_collector_success{site="metric-err.com",source="dummy"} 0.0' in metrics


async def test_retry_succeeds_after_transient_failures() -> None:
    collector = DummyCollector([])
    collector.max_retries = 3
    collector.retry_base_delay = 0
    attempts = {"count": 0}

    async def flaky() -> str:
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise httpx.ConnectError("connection refused")
        return "ok"

    with capture_logs() as captured:
        assert await collector.retry(flaky) == "ok"
    assert attempts["count"] == 3
    warnings = [entry for entry in captured if entry["event"] == "collector_retry"]
    assert len(warnings) == 2
    assert warnings[0]["attempt"] == 1
    assert warnings[0]["error"] == "connection refused"


async def test_retry_exhausted_raises() -> None:
    collector = DummyCollector([])
    collector.max_retries = 2
    collector.retry_base_delay = 0

    async def always_fails() -> None:
        raise httpx.ReadTimeout("timed out")

    with pytest.raises(httpx.ReadTimeout):
        await collector.retry(always_fails)


def test_register_adds_interval_job() -> None:
    scheduler = AsyncIOScheduler()
    collector = DummyCollector(SITES)
    collector.register(scheduler, 60)
    job = scheduler.get_job("collector_dummy")
    assert job is not None
    assert job.trigger is not None
    assert job.trigger.interval.total_seconds() == 60
