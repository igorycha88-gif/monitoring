"""Тесты WebmasterCollector: API-запросы, метрики, лимиты 429/401/404/5xx (respx)."""

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from prometheus_client import generate_latest
from structlog.testing import capture_logs

from app.sites import SiteConfig
from app.storage import DailyRow, WeeklyRow
from collectors.webmaster import (
    QUERY_LABEL_MAX_LENGTH,
    WEBMASTER_API_BASE,
    YANDEX_TZ,
    WebmasterCollector,
    WebmasterConfigError,
    normalize_query,
)

HOST_ID = "https:w.com:443"


def make_collector(
    token: str = "test-token",
    host_id: str | None = HOST_ID,
    domain: str = "w.com",
    extra_domain: str | None = None,
    top_queries: int = 50,
    history_days: int = 7,
) -> WebmasterCollector:
    sites = [SiteConfig(domain=domain, webmaster_host_id=host_id)]
    if extra_domain:
        sites.append(SiteConfig(domain=extra_domain))
    collector = WebmasterCollector(
        sites,
        oauth_tokens={domain: token},
        timeout_seconds=1.0,
        top_queries=top_queries,
        history_days=history_days,
    )
    collector.retry_base_delay = 0
    return collector


def popular_url(user_id: int = 1, host_id: str = HOST_ID) -> str:
    return f"{WEBMASTER_API_BASE}/user/{user_id}/hosts/{host_id}/search-queries/popular"


# per-query history: .../search-queries/{query_id}/history (ADR-008).
# path__regex (не url__regex): query-строка не должна ломать матчинг.
HISTORY_ROUTE_RE = r"/search-queries/[^/]+/history$"


def yandex_iso(days_ago: int) -> str:
    """ISO-дата в таймзоне Яндекса: days_ago суток назад от сегодня (+03:00)."""
    day = datetime.now(tz=YANDEX_TZ).date() - timedelta(days=days_ago)
    return f"{day.isoformat()}T00:00:00.000+03:00"


def mock_user() -> respx.Route:
    return respx.get(f"{WEBMASTER_API_BASE}/user").mock(
        return_value=httpx.Response(200, json={"user_id": 1})
    )


def mock_history(
    shows: list[dict[str, object]] | None = None,
    clicks: list[dict[str, object]] | None = None,
    response: httpx.Response | None = None,
) -> respx.Route:
    """Мок per-query history (любой query_id).

    По умолчанию — indicators без точек: валидный ответ, 0 дневных значений
    (индикатор «не определён» — документированное поведение API).
    """
    if response is not None:
        return respx.get(path__regex=HISTORY_ROUTE_RE).mock(return_value=response)
    indicators: dict[str, object] = {}
    if shows is not None:
        indicators["TOTAL_SHOWS"] = shows
    if clicks is not None:
        indicators["TOTAL_CLICKS"] = clicks
    return respx.get(path__regex=HISTORY_ROUTE_RE).mock(
        return_value=httpx.Response(
            200, json={"query_id": "mock", "query_text": "mock", "indicators": indicators}
        )
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
    mock_history()
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
    mock_history()
    popular = respx.get(popular_url()).mock(return_value=popular_response([QUERY_FULL]))
    collector = make_collector()

    await collector.run_once()
    await collector.run_once()

    assert user_route.call_count == 1
    assert popular.call_count == 2


@respx.mock
async def test_empty_queries_is_success_zero_points() -> None:
    mock_user()
    mock_history()
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
    mock_history()
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
    mock_history()
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
    mock_history()
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
    mock_history()
    respx.get(popular_url()).mock(
        return_value=popular_response(
            [{"query_id": "q1", "query_text": "только клики", "indicators": {"TOTAL_CLICKS": 5}}]
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
    mock_history()
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
    mock_history()
    respx.get(popular_url()).mock(
        return_value=popular_response(
            [
                {
                    "query_id": "q1",
                    "query_text": "  много   пробелов\t",
                    "indicators": {"TOTAL_CLICKS": 1},
                },
                {"query_id": "q2", "query_text": long_text, "indicators": {"TOTAL_CLICKS": 2}},
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
    mock_history()
    route = respx.get(popular_url()).mock(return_value=popular_response([QUERY_FULL]))
    collector = make_collector(top_queries=top_queries)

    await collector.run_once()

    assert route.calls.last.request.url.params["limit"] == expected_limit


@respx.mock
async def test_repeated_collection_updates_values() -> None:
    """История позиций строится TSDB: повторный сбор обновляет значения метрик."""
    mock_user()
    mock_history()
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


# --- Дневная история поисковых запросов (per-query, ADR-008) ---


@respx.mock
async def test_history_writes_only_newest_complete_point() -> None:
    """Из окна истории пишется только новейшая ЗАВЕРШЁННАЯ точка (ADR-008 D1)."""
    mock_user()
    respx.get(popular_url()).mock(
        return_value=popular_response(
            [QUERY_FULL, {"query_id": "a2", "query_text": "слон недорого", "indicators": {}}]
        )
    )
    mock_history(
        shows=[
            {"date": yandex_iso(3), "value": 100},
            {"date": yandex_iso(1), "value": 200},
            {"date": yandex_iso(2), "value": 150},
        ],
        clicks=[{"date": yandex_iso(1), "value": 7}, {"date": yandex_iso(0), "value": 999}],
    )
    collector = make_collector(domain="hist.com")

    with capture_logs() as captured:
        result = await collector.run_once()

    # 2 popular-запроса × 3 метрики... QUERY_FULL даёт 3, второй (indicators={}) — 0;
    # history: 2 запроса × 2 индикатора = 4. Сегодня (999) исключено.
    assert result == {"sites_ok": 1, "sites_total": 1, "points_total": 3 + 4}
    metrics = generate_latest().decode()
    assert 'monitoring_search_daily_clicks{query="купить слона",site="hist.com"} 7.0' in metrics
    assert 'monitoring_search_daily_shows{query="купить слона",site="hist.com"} 200.0' in metrics
    assert 'monitoring_search_daily_clicks{query="слон недорого",site="hist.com"} 7.0' in metrics
    assert (
        'monitoring_search_daily_clicks{query="купить слона",site="hist.com"} 999' not in metrics
    )  # сегодняшний (незавершённый) день исключён
    written = [entry for entry in captured if entry["event"] == "webmaster_history_written"]
    assert len(written) == 2  # по одному событию на запрос (клики + показы внутри)
    assert all("test-token" not in str(entry) for entry in captured)


@respx.mock
async def test_history_excludes_incomplete_today() -> None:
    """Точки только за сегодня (незавершённый день) не пишутся."""
    mock_user()
    respx.get(popular_url()).mock(return_value=popular_response([QUERY_FULL]))
    mock_history(
        shows=[{"date": yandex_iso(0), "value": 999}],
        clicks=[{"date": yandex_iso(0), "value": 999}],
    )
    collector = make_collector(domain="only-today.com")

    result = await collector.run_once()

    assert result == {"sites_ok": 1, "sites_total": 1, "points_total": 3}
    metrics = generate_latest().decode()
    assert 'monitoring_search_daily_clicks{site="only-today.com"' not in metrics
    assert 'monitoring_search_daily_shows{site="only-today.com"' not in metrics


@respx.mock
async def test_history_empty_indicators_success() -> None:
    """Индикаторы без точек — данные: успех, дневные метрики не пишутся."""
    mock_user()
    respx.get(popular_url()).mock(return_value=popular_response([QUERY_FULL]))
    mock_history()
    collector = make_collector(domain="empty-hist.com")

    result = await collector.run_once()

    assert result == {"sites_ok": 1, "sites_total": 1, "points_total": 3}
    metrics = generate_latest().decode()
    assert 'monitoring_search_daily_clicks{site="empty-hist.com"' not in metrics
    assert 'monitoring_search_daily_shows{site="empty-hist.com"' not in metrics


@respx.mock
async def test_history_missing_indicator_partial_write() -> None:
    """Отсутствующий индикатор не пишется (не 0) — паттерн ADR-004."""
    mock_user()
    respx.get(popular_url()).mock(return_value=popular_response([QUERY_FULL]))
    mock_history(clicks=[{"date": yandex_iso(1), "value": 7}])
    collector = make_collector(domain="partial-hist.com")

    result = await collector.run_once()

    assert result["points_total"] == 4  # 3 popular + 1 history
    metrics = generate_latest().decode()
    assert 'monitoring_search_daily_clicks{query="купить слона",site="partial-hist.com"} 7.0' in (
        metrics
    )
    assert 'monitoring_search_daily_shows{query="купить слона",site="partial-hist.com"' not in (
        metrics
    )


@respx.mock
async def test_history_request_params_and_window() -> None:
    """query_indicator повторяемый + окно date_from/date_to = history_days дней."""
    mock_user()
    respx.get(popular_url()).mock(return_value=popular_response([QUERY_FULL]))
    route = mock_history(clicks=[{"date": yandex_iso(1), "value": 7}])
    collector = make_collector()

    await collector.run_once()

    request = route.calls.last.request
    assert request.headers["Authorization"] == "OAuth test-token"
    params = request.url.params
    assert params.get_list("query_indicator") == ["TOTAL_SHOWS", "TOTAL_CLICKS"]
    today = datetime.now(tz=UTC).date()
    assert params["date_to"] == today.isoformat()
    assert params["date_from"] == (today - timedelta(days=6)).isoformat()


@pytest.mark.parametrize(
    ("history_days", "back_days"),
    [(100, 30), (0, 0), (-5, 0), (14, 13)],
)
@respx.mock
async def test_history_days_clamped(history_days: int, back_days: int) -> None:
    mock_user()
    respx.get(popular_url()).mock(return_value=popular_response([QUERY_FULL]))
    route = mock_history()
    collector = make_collector(history_days=history_days)

    await collector.run_once()

    params = route.calls.last.request.url.params
    today = datetime.now(tz=UTC).date()
    assert params["date_from"] == (today - timedelta(days=back_days)).isoformat()
    assert params["date_to"] == today.isoformat()


@respx.mock
async def test_history_query_404_skipped_not_site_error() -> None:
    """404 на запрос (выпал из выдачи между /popular и /history) — пропуск."""
    mock_user()
    respx.get(popular_url()).mock(return_value=popular_response([QUERY_FULL]))
    route = respx.get(path__regex=HISTORY_ROUTE_RE).mock(
        return_value=httpx.Response(404, json={"error_code": "QUERY_ID_NOT_FOUND"})
    )
    collector = make_collector(domain="hist-404q.com")

    with capture_logs() as captured:
        result = await collector.run_once()

    assert result == {"sites_ok": 1, "sites_total": 1, "points_total": 3}
    assert route.call_count == 1
    metrics = generate_latest().decode()
    assert 'monitoring_collector_success{site="hist-404q.com",source="webmaster"} 1.0' in metrics
    skipped = [entry for entry in captured if entry["event"] == "webmaster_history_query_skipped"]
    assert len(skipped) == 1
    done = [entry for entry in captured if entry["event"] == "webmaster_history_done"]
    assert done[0]["points"] == 0


@respx.mock
async def test_history_rate_limit_exhausted_is_collector_error() -> None:
    mock_user()
    respx.get(popular_url()).mock(return_value=popular_response([QUERY_FULL]))
    route = respx.get(path__regex=HISTORY_ROUTE_RE).mock(return_value=httpx.Response(429))
    collector = make_collector(domain="hist-429.com")

    result = await collector.run_once()

    assert result["sites_ok"] == 0
    assert route.call_count == 3
    metrics = generate_latest().decode()
    assert 'monitoring_collector_success{site="hist-429.com",source="webmaster"} 0.0' in metrics


@respx.mock
async def test_history_auth_error_no_retry() -> None:
    mock_user()
    respx.get(popular_url()).mock(return_value=popular_response([QUERY_FULL]))
    route = respx.get(path__regex=HISTORY_ROUTE_RE).mock(return_value=httpx.Response(403))
    collector = make_collector(domain="hist-403.com")

    with capture_logs() as captured:
        result = await collector.run_once()

    assert result["sites_ok"] == 0
    assert route.call_count == 1
    errors = [entry for entry in captured if entry["event"] == "collector_site_error"]
    assert errors[0]["error_type"] == "WebmasterAuthError"


@respx.mock
async def test_history_server_error_retried_then_success() -> None:
    mock_user()
    respx.get(popular_url()).mock(return_value=popular_response([QUERY_FULL]))
    route = respx.get(path__regex=HISTORY_ROUTE_RE).mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(
                200,
                json={
                    "query_id": "a1",
                    "query_text": "купить слона",
                    "indicators": {
                        "TOTAL_CLICKS": [{"date": yandex_iso(1), "value": 9}],
                        "TOTAL_SHOWS": [{"date": yandex_iso(1), "value": 90}],
                    },
                },
            ),
        ]
    )
    collector = make_collector(domain="hist-503.com")

    result = await collector.run_once()

    assert result["sites_ok"] == 1
    assert route.call_count == 2
    metrics = generate_latest().decode()
    assert 'monitoring_search_daily_clicks{query="купить слона",site="hist-503.com"} 9.0' in (
        metrics
    )


@respx.mock
async def test_history_timeout_retried_then_fails() -> None:
    mock_user()
    respx.get(popular_url()).mock(return_value=popular_response([QUERY_FULL]))
    route = respx.get(path__regex=HISTORY_ROUTE_RE).mock(side_effect=httpx.ReadTimeout("t/o"))
    collector = make_collector(domain="hist-to.com")

    with capture_logs() as captured:
        result = await collector.run_once()

    assert result["sites_ok"] == 0
    assert route.call_count == 3
    errors = [entry for entry in captured if entry["event"] == "collector_site_error"]
    assert errors[0]["error_type"] == "ReadTimeout"


@pytest.mark.parametrize(
    "history_payload",
    [
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"query_id": "a1"}),
        httpx.Response(200, json={"query_id": "a1", "indicators": "not-a-dict"}),
        httpx.Response(200, json={"query_id": "a1", "indicators": {"TOTAL_SHOWS": "not-a-list"}}),
        httpx.Response(
            200,
            json={"query_id": "a1", "indicators": {"TOTAL_SHOWS": ["not-a-dict"]}},
        ),
        httpx.Response(
            200,
            json={"query_id": "a1", "indicators": {"TOTAL_SHOWS": [{"value": 1.0}]}},
        ),
        httpx.Response(
            200,
            json={"query_id": "a1", "indicators": {"TOTAL_SHOWS": [{"date": "d"}]}},
        ),
        httpx.Response(
            200,
            json={
                "query_id": "a1",
                "indicators": {"TOTAL_SHOWS": [{"date": "19-08-2026", "value": 1.0}]},
            },
        ),
    ],
    ids=[
        "not-json",
        "no-indicators",
        "indicators-not-dict",
        "points-not-list",
        "point-not-dict",
        "no-date",
        "no-value",
        "bad-date-format",
    ],
)
@respx.mock
async def test_history_malformed_response_is_error(history_payload: httpx.Response) -> None:
    mock_user()
    respx.get(popular_url()).mock(return_value=popular_response([QUERY_FULL]))
    respx.get(path__regex=HISTORY_ROUTE_RE).mock(return_value=history_payload)
    collector = make_collector(domain="bad-hist.com")

    with capture_logs() as captured:
        result = await collector.run_once()

    assert result["sites_ok"] == 0
    errors = [entry for entry in captured if entry["event"] == "collector_site_error"]
    assert errors[0]["error_type"] == "WebmasterApiError"


@respx.mock
async def test_popular_query_without_id_is_error() -> None:
    """query_id обязателен в popular (нужен для per-query history)."""
    mock_user()
    respx.get(popular_url()).mock(
        return_value=popular_response(
            [{"query_text": "без идентификатора", "indicators": {"TOTAL_CLICKS": 1}}]
        )
    )
    collector = make_collector(domain="no-qid.com")

    with capture_logs() as captured:
        result = await collector.run_once()

    assert result["sites_ok"] == 0
    errors = [entry for entry in captured if entry["event"] == "collector_site_error"]
    assert errors[0]["error_type"] == "WebmasterApiError"


@respx.mock
async def test_history_repeated_collection_updates_values() -> None:
    """Повторный сбор обновляет дневные метрики (ряды накапливаются в TSDB)."""
    mock_user()
    respx.get(popular_url()).mock(return_value=popular_response([QUERY_FULL]))
    respx.get(path__regex=HISTORY_ROUTE_RE).mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "query_id": "a1",
                    "indicators": {
                        "TOTAL_CLICKS": [{"date": yandex_iso(2), "value": 4}],
                        "TOTAL_SHOWS": [{"date": yandex_iso(2), "value": 40}],
                    },
                },
            ),
            httpx.Response(
                200,
                json={
                    "query_id": "a1",
                    "indicators": {
                        "TOTAL_CLICKS": [{"date": yandex_iso(1), "value": 8}],
                        "TOTAL_SHOWS": [{"date": yandex_iso(1), "value": 80}],
                    },
                },
            ),
        ]
    )
    collector = make_collector(domain="hist-repeat.com")

    await collector.run_once()
    first = generate_latest().decode()
    assert 'monitoring_search_daily_clicks{query="купить слона",site="hist-repeat.com"} 4.0' in (
        first
    )

    await collector.run_once()
    second = generate_latest().decode()
    assert 'monitoring_search_daily_clicks{query="купить слона",site="hist-repeat.com"} 8.0' in (
        second
    )
    assert 'monitoring_search_daily_shows{query="купить слона",site="hist-repeat.com"} 80.0' in (
        second
    )


def test_normalize_query_unit() -> None:
    assert normalize_query("  a   b  ") == "a b"
    assert normalize_query("\tраз\tдва \n") == "раз два"
    assert len(normalize_query("x" * 300)) == QUERY_LABEL_MAX_LENGTH
    assert normalize_query("   ") == ""


class FakeStorage:
    """Подмена WebmasterStorage: запись вызовов; save_daily может падать."""

    def __init__(self, daily_error: Exception | None = None) -> None:
        self.weekly_calls: list[tuple[str, list[WeeklyRow], str]] = []
        self.daily_calls: list[tuple[str, list[DailyRow], str]] = []
        self.daily_error = daily_error

    def save_weekly(self, site: str, rows: list[WeeklyRow], fetched_at: str) -> int:
        self.weekly_calls.append((site, list(rows), fetched_at))
        return len(rows)

    def save_daily(self, site: str, rows: list[DailyRow], fetched_at: str) -> int:
        if self.daily_error is not None:
            raise self.daily_error
        self.daily_calls.append((site, list(rows), fetched_at))
        return len(rows)


def make_storage_collector(storage: FakeStorage, domain: str) -> WebmasterCollector:
    sites = [SiteConfig(domain=domain, webmaster_host_id=HOST_ID)]
    collector = WebmasterCollector(
        sites,
        oauth_tokens={domain: "test-token"},
        timeout_seconds=1.0,
        storage=storage,  # type: ignore[arg-type]
    )
    collector.retry_base_delay = 0
    return collector


@respx.mock
async def test_collect_writes_storage_weekly_and_daily() -> None:
    """ADR-009: оба среза пишутся в БД с привязкой к сайту (domain)."""
    mock_user()
    respx.get(popular_url()).mock(return_value=popular_response([QUERY_FULL]))
    respx.get(path__regex=HISTORY_ROUTE_RE).mock(
        return_value=httpx.Response(
            200,
            json={
                "query_id": "a1",
                "indicators": {
                    "TOTAL_CLICKS": [{"date": yandex_iso(1), "value": 4}],
                    "TOTAL_SHOWS": [{"date": yandex_iso(1), "value": 40}],
                },
            },
        )
    )
    storage = FakeStorage()
    collector = make_storage_collector(storage, domain="db-site.com")

    result = await collector.run_once()

    assert result["sites_ok"] == 1
    # Недельный снапшот: сайт + строка на каждый запрос топа
    assert len(storage.weekly_calls) == 1
    site, weekly_rows, fetched_at = storage.weekly_calls[0]
    assert site == "db-site.com"
    assert len(weekly_rows) == 1
    assert weekly_rows[0].query_id == "a1"
    assert weekly_rows[0].clicks == 50.0
    assert fetched_at  # ISO-метка времени прогона
    # Дневная история: новейшая завершённая точка с датой дня Яндекса
    assert len(storage.daily_calls) == 1
    site, daily_rows, _ = storage.daily_calls[0]
    assert site == "db-site.com"
    assert len(daily_rows) == 1
    expected_day = (datetime.now(tz=YANDEX_TZ).date() - timedelta(days=1)).isoformat()
    assert daily_rows[0].date == expected_day
    assert daily_rows[0].clicks == 4.0
    assert daily_rows[0].shows == 40.0


@respx.mock
async def test_storage_error_does_not_fail_collection() -> None:
    """ADR-009 D4: ошибка записи в БД не роняет сбор метрик Prometheus."""
    mock_user()
    respx.get(popular_url()).mock(return_value=popular_response([QUERY_FULL]))
    mock_history(
        shows=[{"date": yandex_iso(1), "value": 40}],
        clicks=[{"date": yandex_iso(1), "value": 4}],
    )
    storage = FakeStorage(daily_error=RuntimeError("disk full"))
    collector = make_storage_collector(storage, domain="db-fail.com")

    with capture_logs() as captured:
        result = await collector.run_once()

    assert result["sites_ok"] == 1  # сбор прошёл
    metrics = generate_latest().decode()
    assert 'monitoring_search_clicks_total{query="купить слона",site="db-fail.com"} 50.0' in metrics
    assert 'monitoring_collector_success{site="db-fail.com",source="webmaster"} 1.0' in metrics
    # Счётчик ошибок хранилища доступен на /metrics
    assert 'monitoring_storage_errors_total{source="webmaster"}' in metrics
    failures = [entry for entry in captured if entry["event"] == "webmaster_db_write_failed"]
    assert failures and failures[0]["operation"] == "save_daily"


@respx.mock
async def test_history_without_complete_points_skips_daily_storage() -> None:
    """Нет завершённых точек — в БД дневная запись не пишется (не пишем пустышку)."""
    mock_user()
    respx.get(popular_url()).mock(return_value=popular_response([QUERY_FULL]))
    mock_history()  # индикаторы без точек
    storage = FakeStorage()
    collector = make_storage_collector(storage, domain="db-empty.com")

    result = await collector.run_once()

    assert result["sites_ok"] == 1
    assert len(storage.weekly_calls) == 1  # недельный срез пишется
    assert storage.daily_calls == []  # дневной — нет


# --- Персайтные OAuth-токены (ЧТЗ_Вебмастер_Персайтные_токены) ---


@respx.mock
async def test_per_site_tokens_two_accounts() -> None:
    """Два сайта из двух аккаунтов: каждый запрос идёт с токеном своего сайта,
    user_id кэшируется на токен (второй цикл — без новых /user)."""
    host_a = "https:a.com:443"
    host_b = "https:b.com:443"
    token_a, token_b = "token-a", "token-b"

    def user_side_effect(request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("Authorization", "")
        if auth == f"OAuth {token_a}":
            return httpx.Response(200, json={"user_id": 11})
        return httpx.Response(200, json={"user_id": 22})

    user_route = respx.get(f"{WEBMASTER_API_BASE}/user").mock(side_effect=user_side_effect)
    popular_a = respx.get(
        f"{WEBMASTER_API_BASE}/user/11/hosts/{host_a}/search-queries/popular"
    ).mock(return_value=popular_response([QUERY_FULL]))
    popular_b = respx.get(
        f"{WEBMASTER_API_BASE}/user/22/hosts/{host_b}/search-queries/popular"
    ).mock(return_value=popular_response([QUERY_FULL]))
    mock_history()

    sites = [
        SiteConfig(domain="a.com", webmaster_host_id=host_a),
        SiteConfig(domain="b.com", webmaster_host_id=host_b),
    ]
    collector = WebmasterCollector(
        sites,
        oauth_tokens={"a.com": token_a, "b.com": token_b},
        timeout_seconds=1.0,
    )
    collector.retry_base_delay = 0

    with capture_logs() as captured:
        result = await collector.run_once()
        result2 = await collector.run_once()

    assert result["sites_ok"] == 2
    assert result2["sites_ok"] == 2
    # Каждый popular-запрос — с токеном своего сайта
    auth_a = [call.request.headers.get("Authorization") for call in popular_a.calls]
    auth_b = [call.request.headers.get("Authorization") for call in popular_b.calls]
    assert set(auth_a) == {f"OAuth {token_a}"}
    assert set(auth_b) == {f"OAuth {token_b}"}
    # user_id кэшируется на токен: 2 токена → 2 вызова /user за два цикла
    assert user_route.call_count == 2
    # Метрики обоих сайтов записаны
    metrics = generate_latest().decode()
    assert 'monitoring_collector_success{site="a.com",source="webmaster"} 1.0' in metrics
    assert 'monitoring_collector_success{site="b.com",source="webmaster"} 1.0' in metrics
    # Значения токенов не попадают в логи
    assert not any(token_a in str(entry) or token_b in str(entry) for entry in captured)


@respx.mock
async def test_missing_site_token_is_site_error_not_collector() -> None:
    """Пустой токен сайта — пер-сайтовая ошибка (WebmasterConfigError),
    коллектор жив, другие сайты собираются."""
    mock_user()
    respx.get(popular_url()).mock(return_value=popular_response([QUERY_FULL]))
    mock_history()

    sites = [
        SiteConfig(domain="no-token.com", webmaster_host_id=HOST_ID),
        SiteConfig(domain="w.com", webmaster_host_id=HOST_ID),
    ]
    collector = WebmasterCollector(
        sites,
        oauth_tokens={"no-token.com": "", "w.com": "test-token"},
        timeout_seconds=1.0,
    )
    collector.retry_base_delay = 0

    with capture_logs() as captured:
        result = await collector.run_once()

    assert result["sites_ok"] == 1  # w.com собран, no-token.com — нет
    site_errors = [entry for entry in captured if entry.get("event") == "collector_site_error"]
    assert site_errors and site_errors[0]["site"] == "no-token.com"
    assert site_errors[0]["error_type"] == "WebmasterConfigError"
    metrics = generate_latest().decode()
    assert 'monitoring_collector_errors_total{site="no-token.com",source="webmaster"}' in metrics
    assert 'monitoring_collector_success{site="w.com",source="webmaster"} 1.0' in metrics


async def test_missing_site_token_raises_config_error() -> None:
    """Единичный collect_site без токена поднимает WebmasterConfigError."""
    collector = WebmasterCollector(
        [SiteConfig(domain="x.com", webmaster_host_id=HOST_ID)],
        oauth_tokens={},
        timeout_seconds=1.0,
    )
    with pytest.raises(WebmasterConfigError):
        await collector.collect_site(collector.sites[0])
