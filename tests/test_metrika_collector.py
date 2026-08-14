"""Тесты MetrikaCollector: API-запрос, метрики, лимиты 429/403/404 (respx)."""

import asyncio
from datetime import UTC, datetime

import httpx
import pytest
import respx
from prometheus_client import generate_latest
from structlog.testing import capture_logs

from app.sites import SiteConfig
from collectors.metrika import (
    METRIKA_API_URL,
    MetrikaCollector,
    parse_retry_after,
)


def make_collector(
    token: str = "test-token",
    metrika_counter_id: int | None = 1,
    domain: str = "m.com",
    extra_domain: str | None = None,
) -> MetrikaCollector:
    sites = [SiteConfig(domain=domain, metrika_counter_id=metrika_counter_id)]
    if extra_domain:
        sites.append(SiteConfig(domain=extra_domain))
    collector = MetrikaCollector(
        sites,
        oauth_token=token,
        timeout_seconds=1.0,
    )
    collector.retry_base_delay = 0
    return collector


def today_utc() -> str:
    return datetime.now(tz=UTC).strftime("%Y-%m-%d")


@respx.mock
async def test_collect_success_writes_metrics_and_request_params() -> None:
    route = respx.get(METRIKA_API_URL).mock(
        return_value=httpx.Response(200, json={"totals": [150, 90]})
    )
    collector = make_collector()

    with capture_logs() as captured:
        result = await collector.run_once()

    assert result == {"sites_ok": 1, "sites_total": 1, "points_total": 2}
    metrics = generate_latest().decode()
    assert 'monitoring_site_visits_total{site="m.com",source="metrika"} 150.0' in metrics
    assert 'monitoring_site_visitors{site="m.com",source="metrika"} 90.0' in metrics
    assert 'monitoring_collector_success{site="m.com",source="metrika"} 1.0' in metrics
    assert 'monitoring_collector_errors_total{site="m.com",source="metrika"}' not in metrics

    request = route.calls.last.request
    assert request.headers["Authorization"] == "OAuth test-token"
    params = request.url.params
    assert params["ids"] == "1"
    assert params["metrics"] == "ym:s:visits,ym:s:users"
    assert params["date1"] == today_utc()
    assert params["date2"] == params["date1"]

    done = [entry for entry in captured if entry["event"] == "metrika_collect_done"]
    assert len(done) == 1
    assert done[0]["counter_id"] == 1
    assert done[0]["visits"] == 150.0
    assert done[0]["visitors"] == 90.0
    assert all("test-token" not in str(entry) for entry in captured)


@respx.mock
async def test_empty_report_is_zero_data_not_error() -> None:
    respx.get(METRIKA_API_URL).mock(
        return_value=httpx.Response(200, json={"total_rows": 0, "data": [], "totals": [0, 0]})
    )
    collector = make_collector()

    result = await collector.run_once()

    assert result["sites_ok"] == 1
    metrics = generate_latest().decode()
    assert 'monitoring_site_visits_total{site="m.com",source="metrika"} 0.0' in metrics
    assert 'monitoring_site_visitors{site="m.com",source="metrika"} 0.0' in metrics
    assert 'monitoring_collector_success{site="m.com",source="metrika"} 1.0' in metrics


@respx.mock
async def test_rate_limit_then_success_sleeps_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route = respx.get(METRIKA_API_URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "5"}),
            httpx.Response(200, json={"totals": [7, 3]}),
        ]
    )
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    collector = make_collector()

    with capture_logs() as captured:
        result = await collector.run_once()

    assert result["sites_ok"] == 1
    assert delays == [5.0]
    assert route.call_count == 2
    metrics = generate_latest().decode()
    assert 'monitoring_site_visits_total{site="m.com",source="metrika"} 7.0' in metrics
    retries = [entry for entry in captured if entry["event"] == "collector_retry"]
    assert len(retries) == 1
    assert retries[0]["retry_after_seconds"] == 5.0
    assert retries[0]["delay_seconds"] == 5.0


@respx.mock
async def test_rate_limit_exhausted_is_collector_error() -> None:
    route = respx.get(METRIKA_API_URL).mock(return_value=httpx.Response(429))
    collector = make_collector(domain="ratelimit-exhausted.com")

    with capture_logs() as captured:
        result = await collector.run_once()

    assert result == {"sites_ok": 0, "sites_total": 1, "points_total": 0}
    assert route.call_count == 3
    metrics = generate_latest().decode()
    assert (
        'monitoring_collector_success{site="ratelimit-exhausted.com",source="metrika"} 0.0'
        in metrics
    )
    assert (
        'monitoring_collector_errors_total{site="ratelimit-exhausted.com",source="metrika"} 1.0'
        in metrics
    )
    assert 'monitoring_site_visits_total{site="ratelimit-exhausted.com"' not in metrics
    errors = [entry for entry in captured if entry["event"] == "collector_site_error"]
    assert errors[0]["error_type"] == "RateLimitError"


@pytest.mark.parametrize("status", [401, 403])
@respx.mock
async def test_auth_error_no_retry(status: int) -> None:
    route = respx.get(METRIKA_API_URL).mock(return_value=httpx.Response(status))
    collector = make_collector()

    with capture_logs() as captured:
        result = await collector.run_once()

    assert result["sites_ok"] == 0
    assert route.call_count == 1
    errors = [entry for entry in captured if entry["event"] == "collector_site_error"]
    assert errors[0]["error_type"] == "MetrikaAuthError"


@respx.mock
async def test_not_found_error_no_retry() -> None:
    route = respx.get(METRIKA_API_URL).mock(return_value=httpx.Response(404))
    collector = make_collector(domain="notfound.com")

    result = await collector.run_once()

    assert result["sites_ok"] == 0
    assert route.call_count == 1
    metrics = generate_latest().decode()
    assert 'monitoring_collector_errors_total{site="notfound.com",source="metrika"} 1.0' in metrics


@respx.mock
async def test_timeout_retried_then_fails() -> None:
    route = respx.get(METRIKA_API_URL).mock(side_effect=httpx.ReadTimeout("timed out"))
    collector = make_collector()

    with capture_logs() as captured:
        result = await collector.run_once()

    assert result["sites_ok"] == 0
    assert route.call_count == 3
    retries = [entry for entry in captured if entry["event"] == "collector_retry"]
    assert len(retries) == 2
    errors = [entry for entry in captured if entry["event"] == "collector_site_error"]
    assert errors[0]["error_type"] == "ReadTimeout"


@respx.mock
async def test_server_error_retried_then_success() -> None:
    route = respx.get(METRIKA_API_URL).mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(200, json={"totals": [5, 2]}),
        ]
    )
    collector = make_collector()

    result = await collector.run_once()

    assert result["sites_ok"] == 1
    assert route.call_count == 2
    metrics = generate_latest().decode()
    assert 'monitoring_site_visits_total{site="m.com",source="metrika"} 5.0' in metrics


@respx.mock
async def test_server_error_exhausted_is_collector_error() -> None:
    route = respx.get(METRIKA_API_URL).mock(return_value=httpx.Response(500))
    collector = make_collector(domain="server-error.com")

    with capture_logs() as captured:
        result = await collector.run_once()

    assert result["sites_ok"] == 0
    assert route.call_count == 3
    errors = [entry for entry in captured if entry["event"] == "collector_site_error"]
    assert errors[0]["error_type"] == "MetrikaServerError"


@respx.mock
async def test_unexpected_status_is_error() -> None:
    respx.get(METRIKA_API_URL).mock(return_value=httpx.Response(418))
    collector = make_collector()

    with capture_logs() as captured:
        result = await collector.run_once()

    assert result["sites_ok"] == 0
    errors = [entry for entry in captured if entry["event"] == "collector_site_error"]
    assert errors[0]["error_type"] == "MetrikaApiError"


@pytest.mark.parametrize(
    "payload",
    [
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"data": []}),
        httpx.Response(200, json={"totals": [1]}),
        httpx.Response(200, json={"totals": ["a", "b"]}),
    ],
    ids=["not-json", "no-totals", "short-totals", "non-numeric"],
)
@respx.mock
async def test_malformed_response_is_error(payload: httpx.Response) -> None:
    respx.get(METRIKA_API_URL).mock(return_value=payload)
    collector = make_collector()

    result = await collector.run_once()

    assert result["sites_ok"] == 0
    metrics = generate_latest().decode()
    assert 'monitoring_collector_success{site="m.com",source="metrika"} 0.0' in metrics


@respx.mock
async def test_empty_token_config_error_no_request() -> None:
    route = respx.get(METRIKA_API_URL).mock(
        return_value=httpx.Response(200, json={"totals": [1, 1]})
    )
    collector = make_collector(token="")

    with capture_logs() as captured:
        result = await collector.run_once()

    assert result["sites_ok"] == 0
    assert route.call_count == 0
    errors = [entry for entry in captured if entry["event"] == "collector_site_error"]
    assert errors[0]["error_type"] == "MetrikaConfigError"


@respx.mock
async def test_sites_without_counter_skipped() -> None:
    with capture_logs() as captured:
        collector = make_collector(metrika_counter_id=None, domain="plain.com")

    assert collector.sites == []
    init = [entry for entry in captured if entry["event"] == "metrika_collector_init"]
    assert init[0]["skipped_sites"] == 1
    assert init[0]["counters"] == 0

    with capture_logs() as captured_cycle:
        result = await collector.run_once()

    assert result == {"sites_ok": 0, "sites_total": 0, "points_total": 0}
    assert "collector_site_success" not in [entry["event"] for entry in captured_cycle]


@respx.mock
async def test_counter_site_and_plain_site_mixed() -> None:
    respx.get(METRIKA_API_URL).mock(return_value=httpx.Response(200, json={"totals": [10, 4]}))
    collector = make_collector(domain="with.com", extra_domain="without.com")

    result = await collector.run_once()

    assert result == {"sites_ok": 1, "sites_total": 1, "points_total": 2}
    metrics = generate_latest().decode()
    assert 'monitoring_collector_success{site="with.com",source="metrika"} 1.0' in metrics
    assert 'monitoring_collector_success{site="without.com",source="metrika"}' not in metrics


def test_collector_attributes() -> None:
    collector = make_collector()
    assert collector.source == "metrika"
    assert collector.parallel is False


def test_parse_retry_after() -> None:
    assert parse_retry_after(httpx.Response(429, headers={"Retry-After": "30"})) == 30.0
    assert parse_retry_after(httpx.Response(429, headers={"Retry-After": "1.5"})) == 1.5
    assert parse_retry_after(httpx.Response(429)) is None
    http_date = httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2026"})
    assert parse_retry_after(http_date) is None
    assert parse_retry_after(httpx.Response(429, headers={"Retry-After": "-5"})) is None
