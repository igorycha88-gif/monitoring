"""Тесты SiteMetricsCollector: health-проверки эндпоинтов метрик (respx)."""

import httpx
import pytest
import respx
from prometheus_client import generate_latest
from structlog.testing import capture_logs

from app.sites import SiteConfig
from collectors.sitemetrics import MONITORING_KEY_HEADER, SiteMetricsCollector

API_KEY = "test-api-key"
SITE = "sitemetrics.test"

FULL_URLS = {
    "tracking": f"https://{SITE}/metrics/tracking",
    "content": f"https://{SITE}/metrics/content",
    "node": f"https://{SITE}/metrics/node",
    "postgres": f"https://{SITE}/metrics/postgres",
}


def make_collector(urls: dict[str, str] | None = FULL_URLS) -> SiteMetricsCollector:
    return SiteMetricsCollector(
        [SiteConfig(domain=SITE, metrics_urls=urls)],
        timeout_seconds=1.0,
        api_key=API_KEY,
    )


@respx.mock
async def test_all_kinds_up_on_200() -> None:
    for url in FULL_URLS.values():
        respx.get(url).mock(return_value=httpx.Response(200))

    with capture_logs() as captured:
        result = await make_collector().run_once()

    assert result == {"sites_ok": 1, "sites_total": 1, "points_total": 12}
    metrics = generate_latest().decode()
    for kind in FULL_URLS:
        assert f'monitoring_site_metrics_up{{kind="{kind}",site="{SITE}"}} 1.0' in metrics
        assert (
            f'monitoring_site_metrics_response_code{{kind="{kind}",site="{SITE}"}} 200.0' in metrics
        )
        assert f'monitoring_site_metrics_latency_seconds{{kind="{kind}",site="{SITE}"}}' in metrics
    assert f'monitoring_collector_success{{site="{SITE}",source="site-metrics"}} 1.0' in metrics
    done = [entry for entry in captured if entry["event"] == "site_metrics_check_done"]
    assert len(done) == 4


@respx.mock
async def test_api_key_header_sent() -> None:
    route = respx.get(FULL_URLS["tracking"]).mock(return_value=httpx.Response(200))
    for kind in ("content", "node", "postgres"):
        respx.get(FULL_URLS[kind]).mock(return_value=httpx.Response(200))

    await make_collector({"tracking": FULL_URLS["tracking"]}).run_once()

    request = route.calls.last.request
    assert request.headers.get(MONITORING_KEY_HEADER) == API_KEY


@pytest.mark.parametrize("code", [403, 404, 500, 503])
@respx.mock
async def test_http_error_is_data_not_collector_error(code: int) -> None:
    respx.get(FULL_URLS["tracking"]).mock(return_value=httpx.Response(code))

    with capture_logs() as captured:
        result = await make_collector({"tracking": FULL_URLS["tracking"]}).run_once()

    assert result["sites_ok"] == 1
    metrics = generate_latest().decode()
    assert f'monitoring_site_metrics_up{{kind="tracking",site="{SITE}"}} 0.0' in metrics
    assert (
        f'monitoring_site_metrics_response_code{{kind="tracking",site="{SITE}"}} {float(code)}'
        in metrics
    )
    assert f'monitoring_collector_success{{site="{SITE}",source="site-metrics"}} 1.0' in metrics
    assert "collector_site_error" not in [entry["event"] for entry in captured]


@respx.mock
async def test_network_error_sets_code_zero() -> None:
    respx.get(FULL_URLS["node"]).mock(side_effect=httpx.ConnectError("refused"))

    with capture_logs() as captured:
        result = await make_collector({"node": FULL_URLS["node"]}).run_once()

    assert result["sites_ok"] == 1
    assert result["points_total"] == 3
    metrics = generate_latest().decode()
    assert f'monitoring_site_metrics_up{{kind="node",site="{SITE}"}} 0.0' in metrics
    assert f'monitoring_site_metrics_response_code{{kind="node",site="{SITE}"}} 0.0' in metrics
    assert f'monitoring_collector_success{{site="{SITE}",source="site-metrics"}} 1.0' in metrics
    failed = [entry for entry in captured if entry["event"] == "site_metrics_check_failed"]
    assert failed and failed[0]["error_type"] == "ConnectError"


@respx.mock
async def test_site_without_metrics_urls_yields_zero_points() -> None:
    collector = SiteMetricsCollector(
        [SiteConfig(domain=SITE)], timeout_seconds=1.0, api_key=API_KEY
    )

    result = await collector.run_once()

    assert result == {"sites_ok": 1, "sites_total": 1, "points_total": 0}


@respx.mock
async def test_partial_failure_does_not_break_other_kinds() -> None:
    respx.get(FULL_URLS["tracking"]).mock(return_value=httpx.Response(200))
    respx.get(FULL_URLS["content"]).mock(side_effect=httpx.ReadTimeout("timeout"))

    result = await make_collector(
        {"tracking": FULL_URLS["tracking"], "content": FULL_URLS["content"]}
    ).run_once()

    assert result["sites_ok"] == 1
    assert result["points_total"] == 6
    metrics = generate_latest().decode()
    assert f'monitoring_site_metrics_up{{kind="tracking",site="{SITE}"}} 1.0' in metrics
    assert f'monitoring_site_metrics_up{{kind="content",site="{SITE}"}} 0.0' in metrics


@respx.mock
async def test_no_redirects_followed() -> None:
    respx.get(FULL_URLS["tracking"]).mock(
        return_value=httpx.Response(301, headers={"Location": "https://elsewhere/"})
    )

    result = await make_collector({"tracking": FULL_URLS["tracking"]}).run_once()

    assert result["sites_ok"] == 1
    metrics = generate_latest().decode()
    assert f'monitoring_site_metrics_up{{kind="tracking",site="{SITE}"}} 0.0' in metrics
    assert (
        f'monitoring_site_metrics_response_code{{kind="tracking",site="{SITE}"}} 301.0' in metrics
    )
