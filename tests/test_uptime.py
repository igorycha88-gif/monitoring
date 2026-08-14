"""Тесты UptimeCollector: HTTP-проверки, метрики, сетевые ошибки (respx)."""

import httpx
import pytest
import respx
from prometheus_client import generate_latest
from structlog.testing import capture_logs

from app.sites import SiteConfig
from collectors.uptime import UptimeCollector

BASE_OK = "https://check.com/"


def make_collector(
    domains: list[str] | None = None,
    timeout_seconds: float = 1.0,
    success_max_code: int = 399,
) -> UptimeCollector:
    return UptimeCollector(
        [SiteConfig(domain=domain) for domain in (domains or ["check.com"])],
        timeout_seconds=timeout_seconds,
        success_max_code=success_max_code,
    )


@respx.mock
async def test_up_on_200() -> None:
    respx.get(BASE_OK).mock(return_value=httpx.Response(200))
    collector = make_collector()

    with capture_logs() as captured:
        result = await collector.run_once()

    assert result == {"sites_ok": 1, "sites_total": 1, "points_total": 3}
    metrics = generate_latest().decode()
    assert 'monitoring_uptime_status{site="check.com"} 1.0' in metrics
    assert 'monitoring_uptime_response_code{site="check.com"} 200.0' in metrics
    assert 'monitoring_uptime_response_seconds{site="check.com"}' in metrics
    assert 'monitoring_collector_success{site="check.com",source="uptime"} 1.0' in metrics
    done = [entry for entry in captured if entry["event"] == "uptime_check_done"]
    assert len(done) == 1
    assert done[0]["status_code"] == 200
    assert done[0]["up"] == 1


@respx.mock
async def test_redirect_followed_counts_as_up() -> None:
    respx.route(method="GET", host="check.com").mock(
        side_effect=[
            httpx.Response(301, headers={"Location": "https://check.com/final"}),
            httpx.Response(200),
        ]
    )
    collector = make_collector()

    result = await collector.run_once()

    assert result["sites_ok"] == 1
    metrics = generate_latest().decode()
    assert 'monitoring_uptime_status{site="check.com"} 1.0' in metrics


@pytest.mark.parametrize("code", [404, 500, 503])
@respx.mock
async def test_http_error_keeps_real_code_and_is_not_collector_error(code: int) -> None:
    respx.get(BASE_OK).mock(return_value=httpx.Response(code))
    collector = make_collector()

    with capture_logs() as captured:
        result = await collector.run_once()

    assert result["sites_ok"] == 1
    assert result["points_total"] == 3
    metrics = generate_latest().decode()
    assert 'monitoring_uptime_status{site="check.com"} 0.0' in metrics
    assert f'monitoring_uptime_response_code{{site="check.com"}} {float(code)}' in metrics
    assert 'monitoring_collector_success{site="check.com",source="uptime"} 1.0' in metrics
    assert 'monitoring_collector_errors_total{site="check.com",source="uptime"}' not in metrics
    assert [entry for entry in captured if entry["event"] == "collector_site_error"] == []


@respx.mock
async def test_success_max_code_configurable() -> None:
    respx.get(BASE_OK).mock(return_value=httpx.Response(302))
    collector = make_collector(success_max_code=299)

    await collector.run_once()

    metrics = generate_latest().decode()
    assert 'monitoring_uptime_status{site="check.com"} 0.0' in metrics
    assert 'monitoring_uptime_response_code{site="check.com"} 302.0' in metrics


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectTimeout("connection timed out"),
        httpx.ConnectError("dns lookup failed"),
        httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"),
    ],
    ids=["timeout", "dns", "bad-ssl"],
)
@respx.mock
async def test_network_failure_is_data_not_collector_error(exc: Exception) -> None:
    respx.get(BASE_OK).mock(side_effect=exc)
    collector = make_collector()

    with capture_logs() as captured:
        result = await collector.run_once()

    assert result["sites_ok"] == 1
    assert result["points_total"] == 1
    metrics = generate_latest().decode()
    assert 'monitoring_uptime_status{site="check.com"} 0.0' in metrics
    assert 'monitoring_uptime_response_code{site="check.com"} 0.0' in metrics
    assert 'monitoring_uptime_response_seconds{site="check.com"}' in metrics
    assert 'monitoring_collector_success{site="check.com",source="uptime"} 1.0' in metrics
    assert 'monitoring_collector_errors_total{site="check.com",source="uptime"}' not in metrics
    failed = [entry for entry in captured if entry["event"] == "uptime_check_failed"]
    assert len(failed) == 1
    assert failed[0]["error_type"] == type(exc).__name__
    assert failed[0]["operation"] == "http_check"


@respx.mock
async def test_unexpected_error_marks_collector_failed_but_cycle_continues() -> None:
    respx.get("https://broken.com/").mock(side_effect=RuntimeError("bug"))
    respx.get(BASE_OK).mock(return_value=httpx.Response(200))
    collector = make_collector(domains=["broken.com", "check.com"])

    result = await collector.run_once()

    assert result["sites_ok"] == 1
    assert result["points_total"] == 3
    metrics = generate_latest().decode()
    assert 'monitoring_collector_errors_total{site="broken.com",source="uptime"} 1.0' in metrics
    assert 'monitoring_collector_success{site="broken.com",source="uptime"} 0.0' in metrics
    assert 'monitoring_collector_success{site="check.com",source="uptime"} 1.0' in metrics


@respx.mock
async def test_mixed_cycle_one_down_one_up() -> None:
    respx.get("https://down.com/").mock(side_effect=httpx.ConnectError("refused"))
    respx.get(BASE_OK).mock(return_value=httpx.Response(200))
    collector = make_collector(domains=["down.com", "check.com"])

    result = await collector.run_once()

    assert result == {"sites_ok": 2, "sites_total": 2, "points_total": 4}
    metrics = generate_latest().decode()
    assert 'monitoring_uptime_status{site="down.com"} 0.0' in metrics
    assert 'monitoring_uptime_status{site="check.com"} 1.0' in metrics


def test_collector_attributes() -> None:
    collector = make_collector(timeout_seconds=5.0, success_max_code=299)
    assert collector.source == "uptime"
    assert collector.parallel is True
    assert collector.timeout_seconds == 5.0
    assert collector.success_max_code == 299
