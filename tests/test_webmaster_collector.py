"""Тесты WebmasterCollector: API-запросы, метрики, лимиты 429/401/404/5xx (respx)."""

import asyncio

import httpx
import pytest
import respx
from prometheus_client import generate_latest
from structlog.testing import capture_logs

from app.sites import SiteConfig
from collectors.webmaster import (
    QUERY_LABEL_MAX_LENGTH,
    WEBMASTER_API_BASE,
    WebmasterCollector,
    normalize_query,
)

HOST_ID = "https:w.com:443"


def make_collector(
    token: str = "test-token",
    host_id: str | None = HOST_ID,
    domain: str = "w.com",
    extra_domain: str | None = None,
    top_queries: int = 50,
) -> WebmasterCollector:
    sites = [SiteConfig(domain=domain, webmaster_host_id=host_id)]
    if extra_domain:
        sites.append(SiteConfig(domain=extra_domain))
    collector = WebmasterCollector(
        sites,
        oauth_token=token,
        timeout_seconds=1.0,
        top_queries=top_queries,
    )
    collector.retry_base_delay = 0
    return collector


def popular_url(user_id: int = 1, host_id: str = HOST_ID) -> str:
    return f"{WEBMASTER_API_BASE}/user/{user_id}/hosts/{host_id}/search-queries/popular"


def mock_user() -> respx.Route:
    return respx.get(f"{WEBMASTER_API_BASE}/user").mock(
        return_value=httpx.Response(200, json={"user_id": 1})
    )


def popular_response(queries: list[dict[str, object]]) -> httpx.Response:
    return httpx.Response(200, json={"queries": queries, "count": str(len(queries))})


QUERY_FULL: dict[str, object] = {
    "query_id": "a1",
    "query_text": "купить слона",
    "indicators": {"TOTAL_SHOWS": 1000, "TOTAL_CLICKS": 50, "AVG_SHOW_POSITION": 3.5},
}


@respx.mock
async def test_collect_success_writes_metrics_and_request_params() -> None:
    user_route = mock_user()
    route = respx.get(popular_url()).mock(
        return_value=popular_response(
            [
                QUERY_FULL,
                {
                    "query_id": "a2",
                    "query_text": "слон недорого",
                    "indicators": {
                        "TOTAL_SHOWS": 200,
                        "TOTAL_CLICKS": 10,
                        "AVG_SHOW_POSITION": 7.1,
                    },
                },
            ]
        )
    )
    collector = make_collector()

    with capture_logs() as captured:
        result = await collector.run_once()

    assert result == {"sites_ok": 1, "sites_total": 1, "points_total": 6}
    metrics = generate_latest().decode()
    assert 'monitoring_search_clicks_total{query="купить слона",site="w.com"} 50.0' in metrics
    assert 'monitoring_search_shows_total{query="купить слона",site="w.com"} 1000.0' in metrics
    assert 'monitoring_search_position{query="купить слона",site="w.com"} 3.5' in metrics
    assert 'monitoring_search_position{query="слон недорого",site="w.com"} 7.1' in metrics
    assert 'monitoring_collector_success{site="w.com",source="webmaster"} 1.0' in metrics

    user_request = user_route.calls.last.request
    assert user_request.headers["Authorization"] == "OAuth test-token"
    params = route.calls.last.request.url.params
    assert params["order_by"] == "TOTAL_CLICKS"
    assert params.get_list("query_indicator") == [
        "TOTAL_SHOWS",
        "TOTAL_CLICKS",
        "AVG_SHOW_POSITION",
    ]
    assert params["device_type_indicator"] == "ALL"
    assert params["limit"] == "50"

    done = [entry for entry in captured if entry["event"] == "webmaster_collect_done"]
    assert done[0]["queries"] == 2
    assert done[0]["points"] == 6
    assert all("test-token" not in str(entry) for entry in captured)


@respx.mock
async def test_user_id_resolved_once_and_cached() -> None:
    user_route = mock_user()
    popular = respx.get(popular_url()).mock(return_value=popular_response([QUERY_FULL]))
    collector = make_collector()

    await collector.run_once()
    await collector.run_once()

    assert user_route.call_count == 1
    assert popular.call_count == 2


@respx.mock
async def test_empty_queries_is_success_zero_points() -> None:
    mock_user()
    respx.get(popular_url()).mock(return_value=popular_response([]))
    collector = make_collector()

    result = await collector.run_once()

    assert result == {"sites_ok": 1, "sites_total": 1, "points_total": 0}
    metrics = generate_latest().decode()
    assert 'monitoring_collector_success{site="w.com",source="webmaster"} 1.0' in metrics


@respx.mock
async def test_rate_limit_then_success_sleeps_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_user()
    route = respx.get(popular_url()).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "5"}),
            popular_response([QUERY_FULL]),
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
    retries = [entry for entry in captured if entry["event"] == "collector_retry"]
    assert retries[0]["retry_after_seconds"] == 5.0


@respx.mock
async def test_rate_limit_exhausted_is_collector_error() -> None:
    mock_user()
    route = respx.get(popular_url()).mock(return_value=httpx.Response(429))
    collector = make_collector(domain="ratelimit.com")

    with capture_logs() as captured:
        result = await collector.run_once()

    assert result == {"sites_ok": 0, "sites_total": 1, "points_total": 0}
    assert route.call_count == 3
    metrics = generate_latest().decode()
    assert 'monitoring_collector_success{site="ratelimit.com",source="webmaster"} 0.0' in metrics
    assert (
        'monitoring_collector_errors_total{site="ratelimit.com",source="webmaster"} 1.0' in metrics
    )
    site_lines = [line for line in metrics.splitlines() if 'site="ratelimit.com"' in line]
    assert all(not line.startswith("monitoring_search_") for line in site_lines)
    errors = [entry for entry in captured if entry["event"] == "collector_site_error"]
    assert errors[0]["error_type"] == "RateLimitError"


@pytest.mark.parametrize("status", [401, 403])
@respx.mock
async def test_auth_error_no_retry(status: int) -> None:
    mock_user()
    route = respx.get(popular_url()).mock(return_value=httpx.Response(status))
    collector = make_collector()

    with capture_logs() as captured:
        result = await collector.run_once()

    assert result["sites_ok"] == 0
    assert route.call_count == 1
    errors = [entry for entry in captured if entry["event"] == "collector_site_error"]
    assert errors[0]["error_type"] == "WebmasterAuthError"


@respx.mock
async def test_auth_error_on_user_endpoint() -> None:
    user_route = respx.get(f"{WEBMASTER_API_BASE}/user").mock(return_value=httpx.Response(403))
    popular = respx.get(popular_url()).mock(return_value=popular_response([QUERY_FULL]))
    collector = make_collector()

    with capture_logs() as captured:
        result = await collector.run_once()

    assert result["sites_ok"] == 0
    assert user_route.call_count == 1  # 403 без ретраев
    assert popular.call_count == 0
    errors = [entry for entry in captured if entry["event"] == "collector_site_error"]
    assert errors[0]["error_type"] == "WebmasterAuthError"


@respx.mock
async def test_not_found_host_no_retry() -> None:
    mock_user()
    route = respx.get(popular_url()).mock(
        return_value=httpx.Response(
            404,
            json={
                "error_code": "HOST_NOT_VERIFIED",
                "host_id": HOST_ID,
                "error_message": "rights not verified",
            },
        )
    )
    collector = make_collector()

    with capture_logs() as captured:
        result = await collector.run_once()

    assert result["sites_ok"] == 0
    assert route.call_count == 1
    errors = [entry for entry in captured if entry["event"] == "collector_site_error"]
    assert errors[0]["error_type"] == "WebmasterNotFoundError"


@respx.mock
async def test_timeout_retried_then_fails() -> None:
    mock_user()
    route = respx.get(popular_url()).mock(side_effect=httpx.ReadTimeout("timed out"))
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
    mock_user()
    route = respx.get(popular_url()).mock(
        side_effect=[httpx.Response(503), popular_response([QUERY_FULL])]
    )
    collector = make_collector()

    result = await collector.run_once()

    assert result["sites_ok"] == 1
    assert route.call_count == 2
    metrics = generate_latest().decode()
    assert 'monitoring_search_clicks_total{query="купить слона",site="w.com"} 50.0' in metrics


@respx.mock
async def test_server_error_exhausted_is_collector_error() -> None:
    mock_user()
    respx.get(popular_url()).mock(return_value=httpx.Response(500))
    collector = make_collector()

    with capture_logs() as captured:
        result = await collector.run_once()

    assert result["sites_ok"] == 0
    errors = [entry for entry in captured if entry["event"] == "collector_site_error"]
    assert errors[0]["error_type"] == "WebmasterServerError"


@respx.mock
async def test_unexpected_status_is_error() -> None:
    mock_user()
    respx.get(popular_url()).mock(return_value=httpx.Response(418))
    collector = make_collector()

    with capture_logs() as captured:
        result = await collector.run_once()

    assert result["sites_ok"] == 0
    errors = [entry for entry in captured if entry["event"] == "collector_site_error"]
    assert errors[0]["error_type"] == "WebmasterApiError"


@pytest.mark.parametrize(
    "popular_payload",
    [
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"count": "0"}),
        httpx.Response(200, json={"queries": [{"query_id": "x"}]}),
        httpx.Response(200, json={"queries": [{"query_text": "q"}]}),
        httpx.Response(200, json={"queries": [{"query_text": 1, "indicators": {}}]}),
        httpx.Response(200, json={"queries": "not-a-list"}),
    ],
    ids=[
        "not-json",
        "no-queries",
        "no-query-text",
        "no-indicators",
        "non-str-query-text",
        "queries-not-list",
    ],
)
@respx.mock
async def test_malformed_response_is_error(popular_payload: httpx.Response) -> None:
    mock_user()
    respx.get(popular_url()).mock(return_value=popular_payload)
    collector = make_collector()

    with capture_logs() as captured:
        result = await collector.run_once()

    assert result["sites_ok"] == 0
    errors = [entry for entry in captured if entry["event"] == "collector_site_error"]
    assert errors[0]["error_type"] == "WebmasterApiError"


@respx.mock
async def test_user_endpoint_malformed_payload_is_error() -> None:
    respx.get(f"{WEBMASTER_API_BASE}/user").mock(
        return_value=httpx.Response(200, json={"login": "no-id"})
    )
    collector = make_collector()

    with capture_logs() as captured:
        result = await collector.run_once()

    assert result["sites_ok"] == 0
    errors = [entry for entry in captured if entry["event"] == "collector_site_error"]
    assert errors[0]["error_type"] == "WebmasterApiError"


@respx.mock
async def test_user_endpoint_not_json_is_error() -> None:
    respx.get(f"{WEBMASTER_API_BASE}/user").mock(return_value=httpx.Response(200, text="oops"))
    collector = make_collector()

    with capture_logs() as captured:
        result = await collector.run_once()

    assert result["sites_ok"] == 0
    errors = [entry for entry in captured if entry["event"] == "collector_site_error"]
    assert errors[0]["error_type"] == "WebmasterApiError"


@respx.mock
async def test_user_endpoint_timeout_retried_then_fails() -> None:
    user_route = respx.get(f"{WEBMASTER_API_BASE}/user").mock(
        side_effect=httpx.ReadTimeout("timed out")
    )
    popular = respx.get(popular_url()).mock(return_value=popular_response([QUERY_FULL]))
    collector = make_collector()

    result = await collector.run_once()

    assert result == {"sites_ok": 0, "sites_total": 1, "points_total": 0}
    assert user_route.call_count == 3
    assert popular.call_count == 0


@respx.mock
async def test_empty_token_config_error_no_request() -> None:
    user_route = mock_user()
    collector = make_collector(token="")

    with capture_logs() as captured:
        result = await collector.run_once()

    assert result["sites_ok"] == 0
    assert user_route.call_count == 0
    errors = [entry for entry in captured if entry["event"] == "collector_site_error"]
    assert errors[0]["error_type"] == "WebmasterConfigError"


@respx.mock
async def test_sites_without_host_skipped() -> None:
    with capture_logs() as captured:
        collector = make_collector(host_id=None, domain="plain.com")

    assert collector.sites == []
    init = [entry for entry in captured if entry["event"] == "webmaster_collector_init"]
    assert init[0]["skipped_sites"] == 1
    assert init[0]["hosts"] == 0

    result = await collector.run_once()

    assert result == {"sites_ok": 0, "sites_total": 0, "points_total": 0}


@respx.mock
async def test_host_site_and_plain_site_mixed() -> None:
    mock_user()
    respx.get(popular_url()).mock(return_value=popular_response([QUERY_FULL]))
    collector = make_collector(domain="with.com", extra_domain="without.com")

    result = await collector.run_once()

    assert result == {"sites_ok": 1, "sites_total": 1, "points_total": 3}
    metrics = generate_latest().decode()
    assert 'monitoring_collector_success{site="with.com",source="webmaster"} 1.0' in metrics
    assert 'monitoring_collector_success{site="without.com",source="webmaster"}' not in metrics


@respx.mock
async def test_missing_indicator_not_written() -> None:
    mock_user()
    respx.get(popular_url()).mock(
        return_value=popular_response(
            [{"query_text": "только клики", "indicators": {"TOTAL_CLICKS": 5}}]
        )
    )
    collector = make_collector()

    with capture_logs() as captured:
        result = await collector.run_once()

    assert result["points_total"] == 1
    metrics = generate_latest().decode()
    assert 'monitoring_search_clicks_total{query="только клики",site="w.com"} 5.0' in metrics
    assert 'monitoring_search_shows_total{query="только клики"' not in metrics
    assert 'monitoring_search_position{query="только клики"' not in metrics
    assert not [entry for entry in captured if entry["event"] == "webmaster_blank_query_skipped"]


@respx.mock
async def test_blank_query_skipped() -> None:
    mock_user()
    respx.get(popular_url()).mock(
        return_value=popular_response([{"query_text": "   ", "indicators": {}}])
    )
    collector = make_collector(domain="blank.com")

    with capture_logs() as captured:
        result = await collector.run_once()

    assert result == {"sites_ok": 1, "sites_total": 1, "points_total": 0}
    assert [entry["event"] for entry in captured].count("webmaster_blank_query_skipped") == 1
    metrics = generate_latest().decode()
    site_lines = [line for line in metrics.splitlines() if 'site="blank.com"' in line]
    assert all(not line.startswith("monitoring_search_") for line in site_lines)


@respx.mock
async def test_query_label_normalized() -> None:
    mock_user()
    long_text = "a" * (QUERY_LABEL_MAX_LENGTH + 10)
    respx.get(popular_url()).mock(
        return_value=popular_response(
            [
                {"query_text": "  много   пробелов\t", "indicators": {"TOTAL_CLICKS": 1}},
                {"query_text": long_text, "indicators": {"TOTAL_CLICKS": 2}},
            ]
        )
    )
    collector = make_collector()

    result = await collector.run_once()

    assert result["points_total"] == 2
    metrics = generate_latest().decode()
    assert 'monitoring_search_clicks_total{query="много пробелов",site="w.com"} 1.0' in metrics
    assert f'{{query="{"a" * QUERY_LABEL_MAX_LENGTH}",site="w.com"}} 2.0' in metrics


@pytest.mark.parametrize(
    ("top_queries", "expected_limit"),
    [(500, "100"), (0, "1"), (-5, "1"), (50, "50")],
)
@respx.mock
async def test_top_queries_clamped(top_queries: int, expected_limit: str) -> None:
    mock_user()
    route = respx.get(popular_url()).mock(return_value=popular_response([QUERY_FULL]))
    collector = make_collector(top_queries=top_queries)

    await collector.run_once()

    assert route.calls.last.request.url.params["limit"] == expected_limit


@respx.mock
async def test_repeated_collection_updates_values() -> None:
    """История позиций строится TSDB: повторный сбор обновляет значения метрик."""
    mock_user()
    respx.get(popular_url()).mock(
        side_effect=[
            popular_response([QUERY_FULL]),
            popular_response(
                [
                    {
                        "query_id": "a1",
                        "query_text": "купить слона",
                        "indicators": {
                            "TOTAL_SHOWS": 1200,
                            "TOTAL_CLICKS": 75,
                            "AVG_SHOW_POSITION": 2.0,
                        },
                    }
                ]
            ),
        ]
    )
    collector = make_collector()

    await collector.run_once()
    metrics_after_first = generate_latest().decode()
    assert (
        'monitoring_search_position{query="купить слона",site="w.com"} 3.5' in metrics_after_first
    )

    await collector.run_once()
    metrics_after_second = generate_latest().decode()
    assert (
        'monitoring_search_position{query="купить слона",site="w.com"} 2.0' in metrics_after_second
    )
    assert (
        'monitoring_search_clicks_total{query="купить слона",site="w.com"} 75.0'
        in metrics_after_second
    )


def test_collector_attributes() -> None:
    collector = make_collector()
    assert collector.source == "webmaster"
    assert collector.parallel is False


def test_normalize_query_unit() -> None:
    assert normalize_query("  a   b  ") == "a b"
    assert normalize_query("\tраз\tдва \n") == "раз два"
    assert len(normalize_query("x" * 300)) == QUERY_LABEL_MAX_LENGTH
    assert normalize_query("   ") == ""
