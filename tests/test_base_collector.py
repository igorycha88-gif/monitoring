"""Тесты BaseCollector: метрики, логи цикла, ретраи, расписание, parallel."""

import asyncio
import time
from datetime import UTC, datetime

import httpx
import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from prometheus_client import generate_latest
from structlog.testing import capture_logs

from app.sites import SiteConfig
from collectors.base import BaseCollector, RateLimitError

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


async def test_retry_respects_retry_after_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """429 с Retry-After: пауза из заголовка вместо экспоненциального backoff."""
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    collector = DummyCollector([])
    collector.max_retries = 3
    collector.retry_base_delay = 1.0
    attempts = {"count": 0}

    async def rate_limited() -> str:
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise RateLimitError("429", retry_after_seconds=7.5)
        return "ok"

    with capture_logs() as captured:
        assert await collector.retry(rate_limited, retry_on=(RateLimitError,)) == "ok"

    assert delays == [7.5, 7.5]
    warnings = [entry for entry in captured if entry["event"] == "collector_retry"]
    assert len(warnings) == 2
    assert all(entry["retry_after_seconds"] == 7.5 for entry in warnings)
    assert all(entry["delay_seconds"] == 7.5 for entry in warnings)


async def test_retry_without_retry_after_uses_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """429 без Retry-After: обычный экспоненциальный backoff."""
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    collector = DummyCollector([])
    collector.max_retries = 3
    collector.retry_base_delay = 0.5
    attempts = {"count": 0}

    async def rate_limited() -> str:
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise RateLimitError("429", retry_after_seconds=None)
        return "ok"

    assert await collector.retry(rate_limited, retry_on=(RateLimitError,)) == "ok"
    assert delays == [0.5, 1.0]


def test_rate_limit_error_defaults() -> None:
    exc = RateLimitError("lim", retry_after_seconds=2.0)
    assert str(exc) == "lim"
    assert exc.retry_after_seconds == 2.0
    assert RateLimitError("lim").retry_after_seconds is None


def test_register_adds_interval_job() -> None:
    scheduler = AsyncIOScheduler()
    collector = DummyCollector(SITES)
    collector.register(scheduler, 60)
    job = scheduler.get_job("collector_dummy")
    assert job is not None
    assert job.trigger is not None
    assert job.trigger.interval.total_seconds() == 60


def test_register_first_run_is_immediate() -> None:
    """Первый запуск — сразу после старта, не через полный интервал."""
    scheduler = AsyncIOScheduler()
    collector = DummyCollector(SITES)
    before = datetime.now(tz=UTC)
    collector.register(scheduler, 3600)
    job = scheduler.get_job("collector_dummy")
    assert job is not None
    assert job.next_run_time is not None
    delay_seconds = (job.next_run_time - before).total_seconds()
    assert delay_seconds < 30, f"первый запуск через {delay_seconds:.0f}с — не немедленный"


def test_register_misfire_grace_covers_interval() -> None:
    """Опоздавший запуск выполняется, а не отменяется (misfire_grace_time)."""
    scheduler = AsyncIOScheduler()
    collector = DummyCollector(SITES)
    collector.register(scheduler, 3600)
    job = scheduler.get_job("collector_dummy")
    assert job is not None
    assert job.misfire_grace_time == 3600


def test_register_daily_cron_fields() -> None:
    """Ежедневная джоба: hour=7, minute=0, таймзона Europe/Moscow (= 04:00 UTC)."""
    scheduler = AsyncIOScheduler(timezone="UTC")
    collector = DummyCollector(SITES)
    collector.register_daily(scheduler, hour=7, timezone="Europe/Moscow")
    job = scheduler.get_job("collector_dummy")
    assert job is not None
    assert isinstance(job.trigger, CronTrigger)
    fields = {field.name: str(field) for field in job.trigger.fields}
    assert fields["hour"] == "7"
    assert fields["minute"] == "0"
    assert str(job.trigger.timezone) == "Europe/Moscow"


def test_register_daily_multiple_hours() -> None:
    """Список часов "7,19" — утренний + вечерний дозаполняющий прогоны (ADR-008)."""
    scheduler = AsyncIOScheduler(timezone="UTC")
    collector = DummyCollector(SITES)
    collector.register_daily(scheduler, hour="7,19", timezone="Europe/Moscow")
    job = scheduler.get_job("collector_dummy")
    assert job is not None
    assert isinstance(job.trigger, CronTrigger)
    fields = {field.name: str(field) for field in job.trigger.fields}
    assert fields["hour"] == "7,19"


def test_register_daily_first_run_is_immediate() -> None:
    """Первый запуск ежедневной джобы — сразу после старта, не в 07:00 следующего дня."""
    scheduler = AsyncIOScheduler(timezone="UTC")
    collector = DummyCollector(SITES)
    before = datetime.now(tz=UTC)
    collector.register_daily(scheduler, hour=7)
    job = scheduler.get_job("collector_dummy")
    assert job is not None
    assert job.next_run_time is not None
    delay_seconds = (job.next_run_time - before).total_seconds()
    assert delay_seconds < 30, f"первый запуск через {delay_seconds:.0f}с — не немедленный"


def test_register_daily_misfire_grace() -> None:
    """Пропущенный из-за сна машины запуск выполняется при пробуждении (20ч)."""
    scheduler = AsyncIOScheduler(timezone="UTC")
    collector = DummyCollector(SITES)
    collector.register_daily(scheduler, hour=7)
    job = scheduler.get_job("collector_dummy")
    assert job is not None
    assert job.misfire_grace_time == 20 * 3600


class ParallelDummyCollector(DummyCollector):
    """То же, что DummyCollector, но с параллельным обходом."""

    parallel = True


async def test_parallel_site_error_does_not_break_cycle() -> None:
    sites = [SiteConfig(domain="perr.com"), SiteConfig(domain="pfine.com")]
    collector = ParallelDummyCollector(sites, fail_domains={"perr.com"})
    with capture_logs() as captured:
        result = await collector.run_once()
    assert result == {"sites_ok": 1, "sites_total": 2, "points_total": 3}
    metrics = generate_latest().decode()
    assert 'monitoring_collector_success{site="perr.com",source="dummy"} 0.0' in metrics
    assert 'monitoring_collector_success{site="pfine.com",source="dummy"} 1.0' in metrics
    assert len([entry for entry in captured if entry["event"] == "collector_site_error"]) == 1


class SleepyCollector(BaseCollector):
    """Коллектор с задержкой — для проверки реальной конкурентности."""

    source = "sleepy"
    parallel = True

    def __init__(self, sites: list[SiteConfig], delay: float) -> None:
        super().__init__(sites)
        self.delay = delay

    async def collect_site(self, site: SiteConfig) -> int:
        await asyncio.sleep(self.delay)
        return 1


async def test_parallel_runs_sites_concurrently() -> None:
    sites = [SiteConfig(domain=f"s{i}.com") for i in range(4)]
    collector = SleepyCollector(sites, delay=0.2)

    started = time.perf_counter()
    result = await collector.run_once()
    elapsed = time.perf_counter() - started

    assert result == {"sites_ok": 4, "sites_total": 4, "points_total": 4}
    # Последовательно было бы >= 0.8 сек; параллельно — около 0.2 сек.
    assert elapsed < 0.6


class SleepySequentialCollector(SleepyCollector):
    """Тот же сон, но последовательный — контрольный режим ЭПИК-0."""

    parallel = False


async def test_sequential_stays_sequential_by_default() -> None:
    sites = [SiteConfig(domain=f"s{i}.com") for i in range(3)]
    collector = SleepySequentialCollector(sites, delay=0.05)

    started = time.perf_counter()
    result = await collector.run_once()
    elapsed = time.perf_counter() - started

    assert result["sites_ok"] == 3
    assert elapsed >= 0.15  # 3 задержки по 0.05 сек суммируются
