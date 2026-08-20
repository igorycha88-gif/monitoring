"""Тесты relay-прокси метрик сайтов: /api/v1/relay/site-metrics/{kind}/{site} (ADR-010 D2)."""

from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from structlog.testing import capture_logs

from app.config import get_settings

SITE = "relay.test"
URL = f"https://{SITE}/metrics/tracking"

SITES_YAML = f"sites:\n  - domain: {SITE}\n    metrics_urls:\n      tracking: {URL}\n"


def use_sites_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str = SITES_YAML
) -> None:
    config_file = tmp_path / "sites.yml"
    config_file.write_text(content, encoding="utf-8")
    monkeypatch.setenv("SITES_CONFIG_PATH", str(config_file))
    get_settings.cache_clear()


@respx.mock
def test_relay_proxies_body_and_content_type(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_sites_file(tmp_path, monkeypatch=monkeypatch)
    respx.get(URL).mock(
        return_value=httpx.Response(
            200,
            content=b'business_page_views_24h{site="relay.test"} 42\n',
            headers={"content-type": "text/plain; version=0.0.4"},
        )
    )

    response = client.get(f"/api/v1/relay/site-metrics/tracking/{SITE}")

    assert response.status_code == 200
    assert response.content == b'business_page_views_24h{site="relay.test"} 42\n'
    assert response.headers["content-type"].startswith("text/plain")


@respx.mock
def test_relay_sends_site_key_header(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_sites_file(tmp_path, monkeypatch=monkeypatch)
    monkeypatch.setenv("SITE_METRICS_API_KEYS", f'{{"{SITE}": "per-site-secret"}}')
    get_settings.cache_clear()
    route = respx.get(URL).mock(return_value=httpx.Response(200, content=b"up 1\n"))

    response = client.get(f"/api/v1/relay/site-metrics/tracking/{SITE}")

    assert response.status_code == 200
    assert route.calls.last.request.headers.get("X-Monitoring-Key") == "per-site-secret"


@respx.mock
def test_relay_falls_back_to_global_key(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_sites_file(tmp_path, monkeypatch=monkeypatch)
    monkeypatch.setenv("SITE_METRICS_API_KEYS", "{}")
    monkeypatch.setenv("SITE_METRICS_API_KEY", "global-secret")
    get_settings.cache_clear()
    route = respx.get(URL).mock(return_value=httpx.Response(200, content=b"up 1\n"))

    response = client.get(f"/api/v1/relay/site-metrics/tracking/{SITE}")

    assert response.status_code == 200
    assert route.calls.last.request.headers.get("X-Monitoring-Key") == "global-secret"


@respx.mock
def test_relay_upstream_error_returns_502(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-010 D2: не-2xx → 502 (для Prometheus up=0)."""
    use_sites_file(tmp_path, monkeypatch=monkeypatch)
    respx.get(URL).mock(return_value=httpx.Response(403))

    with capture_logs() as captured:
        response = client.get(f"/api/v1/relay/site-metrics/tracking/{SITE}")

    assert response.status_code == 502
    failed = [entry for entry in captured if entry["event"] == "relay_scrape_failed"]
    assert failed and failed[0]["upstream_status"] == 403


@respx.mock
def test_relay_network_error_returns_502(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_sites_file(tmp_path, monkeypatch=monkeypatch)
    respx.get(URL).mock(side_effect=httpx.ConnectError("refused"))

    with capture_logs() as captured:
        response = client.get(f"/api/v1/relay/site-metrics/tracking/{SITE}")

    assert response.status_code == 502
    failed = [entry for entry in captured if entry["event"] == "relay_scrape_failed"]
    assert failed and failed[0]["error_type"] == "ConnectError"


def test_relay_unknown_kind_404(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_sites_file(tmp_path, monkeypatch=monkeypatch)
    response = client.get(f"/api/v1/relay/site-metrics/billing/{SITE}")
    assert response.status_code == 404


def test_relay_unknown_site_404(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_sites_file(tmp_path, monkeypatch=monkeypatch)
    response = client.get("/api/v1/relay/site-metrics/tracking/unknown.test")
    assert response.status_code == 404


@respx.mock
def test_relay_site_without_kind_404(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_sites_file(
        tmp_path,
        monkeypatch,
        f"sites:\n  - domain: {SITE}\n    metrics_urls:\n      node: https://{SITE}/m/node\n",
    )
    response = client.get(f"/api/v1/relay/site-metrics/tracking/{SITE}")
    assert response.status_code == 404


@respx.mock
def test_relay_percent_encoded_idn_domain(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Таргет SD кодирует IDN-домен — FastAPI декодирует path-параметр."""
    config_file = tmp_path / "sites.yml"
    config_file.write_text(
        "sites:\n"
        "  - domain: эвакуация.online\n"
        "    metrics_urls:\n"
        "      tracking: https://эвакуация.online/metrics/tracking\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SITES_CONFIG_PATH", str(config_file))
    get_settings.cache_clear()
    respx.get("https://эвакуация.online/metrics/tracking").mock(
        return_value=httpx.Response(200, content=b"up 1\n")
    )

    response = client.get(
        "/api/v1/relay/site-metrics/tracking/%D1%8D%D0%B2%D0%B0%D0%BA%D1%83%D0%B0%D1%86%D0%B8%D1%8F.online"
    )

    assert response.status_code == 200
    assert response.content == b"up 1\n"


@respx.mock
def test_relay_key_not_logged(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_sites_file(tmp_path, monkeypatch=monkeypatch)
    monkeypatch.setenv("SITE_METRICS_API_KEYS", f'{{"{SITE}": "super-secret-key"}}')
    get_settings.cache_clear()
    respx.get(URL).mock(return_value=httpx.Response(200, content=b"up 1\n"))

    with capture_logs() as captured:
        response = client.get(f"/api/v1/relay/site-metrics/tracking/{SITE}")

    assert response.status_code == 200
    assert all("super-secret-key" not in str(entry) for entry in captured)


@respx.mock
def test_relay_requests_not_logged_on_info(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Скрейпы каждые 15 с не должны шуметь в INFO-логах middleware."""
    use_sites_file(tmp_path, monkeypatch=monkeypatch)
    respx.get(URL).mock(return_value=httpx.Response(200, content=b"up 1\n"))

    with capture_logs() as captured:
        response = client.get(f"/api/v1/relay/site-metrics/tracking/{SITE}")

    assert response.status_code == 200
    http_logs = [
        entry
        for entry in captured
        if entry["event"] == "http_request" and entry.get("path", "").startswith("/api/v1/relay/")
    ]
    assert not http_logs
