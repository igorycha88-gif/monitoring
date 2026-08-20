"""Тесты GET /api/v1/sd/node-exporter — HTTP SD для Prometheus (ЭПИК-6)."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from structlog.testing import capture_logs

from app.api.v1.sd import build_labels, build_target
from app.config import get_settings
from app.sites import SiteConfig

VALID_YAML = """
sites:
  - domain: example.com
    node_exporter_url: "http://10.0.0.1:9100"
  - domain: example.org
    node_exporter_url: "http://exporter.example.org:9101/metrics"
  - domain: example.net
    node_exporter_url: "https://203.0.113.10"
  - domain: example.io
"""


def use_sites_file(tmp_path: Path, content: str, monkeypatch: pytest.MonkeyPatch) -> None:
    config_file = tmp_path / "sites.yml"
    config_file.write_text(content, encoding="utf-8")
    monkeypatch.setenv("SITES_CONFIG_PATH", str(config_file))
    get_settings.cache_clear()


def test_sd_empty_when_no_exporters(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_sites_file(tmp_path, "sites:\n  - domain: example.com\n", monkeypatch)
    response = client.get("/api/v1/sd/node-exporter")
    assert response.status_code == 200
    assert response.json() == []


def test_sd_targets_and_labels(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_sites_file(tmp_path, VALID_YAML, monkeypatch)
    response = client.get("/api/v1/sd/node-exporter")
    assert response.status_code == 200
    groups = response.json()
    assert len(groups) == 3

    assert groups[0] == {"targets": ["10.0.0.1:9100"], "labels": {"site": "example.com"}}
    assert groups[1] == {
        "targets": ["exporter.example.org:9101"],
        "labels": {"site": "example.org", "__metrics_path__": "/metrics"},
    }
    assert groups[2] == {
        "targets": ["203.0.113.10:9100"],
        "labels": {"site": "example.net", "__scheme__": "https"},
    }


def test_sd_broken_config_returns_500(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SITES_CONFIG_PATH", str(tmp_path / "missing.yml"))
    get_settings.cache_clear()
    response = client.get("/api/v1/sd/node-exporter")
    assert response.status_code == 500
    assert "не найден" in response.json()["detail"]


def test_sd_logs_targets_served(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_sites_file(tmp_path, VALID_YAML, monkeypatch)
    with capture_logs() as captured:
        response = client.get("/api/v1/sd/node-exporter")
    assert response.status_code == 200
    served = [event for event in captured if event["event"] == "sd_targets_served"]
    assert served and served[0]["targets"] == 3


def test_build_target_defaults_and_ipv6() -> None:
    assert build_target("http://10.0.0.1") == "10.0.0.1:9100"
    assert build_target("http://10.0.0.1:9100") == "10.0.0.1:9100"
    assert build_target("http://[2001:db8::1]:9100") == "[2001:db8::1]:9100"
    assert build_target("http://[2001:db8::1]") == "[2001:db8::1]:9100"


def test_build_labels_root_path_and_http_omitted() -> None:
    site = SiteConfig(domain="x.com", node_exporter_url="http://1.2.3.4:9100/")
    assert build_labels(site) == {"site": "x.com"}


VALID_SITE_METRICS_YAML = """
sites:
  - domain: da-dryclean.ru
    metrics_urls:
      tracking: https://da-dryclean.ru/metrics/tracking
      postgres: https://da-dryclean.ru/metrics/postgres
  - domain: example.com
"""


def test_site_metrics_sd_unknown_kind_404(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_sites_file(tmp_path, VALID_SITE_METRICS_YAML, monkeypatch)
    response = client.get("/api/v1/sd/site-metrics/billing")
    assert response.status_code == 404
    assert "billing" in response.json()["detail"]


def test_site_metrics_sd_targets_and_labels(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_sites_file(tmp_path, VALID_SITE_METRICS_YAML, monkeypatch)
    response = client.get("/api/v1/sd/site-metrics/tracking")
    assert response.status_code == 200
    groups = response.json()
    assert groups == [
        {
            "targets": ["app:8088"],
            "labels": {
                "site": "da-dryclean.ru",
                "__metrics_path__": "/api/v1/relay/site-metrics/tracking/da-dryclean.ru",
            },
        }
    ]


def test_site_metrics_sd_idn_domain_percent_encoded(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-010 D3: IDN-домен в пути — percent-encoded, лейбл site — читаемый."""
    use_sites_file(
        tmp_path,
        "sites:\n"
        "  - domain: эвакуация.online\n"
        "    metrics_urls:\n"
        "      tracking: https://эвакуация.online/metrics/tracking\n",
        monkeypatch,
    )
    response = client.get("/api/v1/sd/site-metrics/tracking")
    assert response.status_code == 200
    groups = response.json()
    assert groups == [
        {
            "targets": ["app:8088"],
            "labels": {
                "site": "эвакуация.online",
                # Домен — сырой unicode: percent-кодирование UTF-8 делает
                # Prometheus (предзакодированное значение он кодирует повторно)
                "__metrics_path__": "/api/v1/relay/site-metrics/tracking/эвакуация.online",
            },
        }
    ]


def test_site_metrics_sd_kind_without_urls_empty(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_sites_file(tmp_path, VALID_SITE_METRICS_YAML, monkeypatch)
    response = client.get("/api/v1/sd/site-metrics/node")
    assert response.status_code == 200
    assert response.json() == []


def test_site_metrics_sd_broken_config_500(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SITES_CONFIG_PATH", str(tmp_path / "missing.yml"))
    get_settings.cache_clear()
    response = client.get("/api/v1/sd/site-metrics/tracking")
    assert response.status_code == 500
    assert "не найден" in response.json()["detail"]


def test_site_metrics_sd_logs_targets_served(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_sites_file(tmp_path, VALID_SITE_METRICS_YAML, monkeypatch)
    with capture_logs() as captured:
        response = client.get("/api/v1/sd/site-metrics/postgres")
    assert response.status_code == 200
    served = [event for event in captured if event["event"] == "sd_targets_served"]
    assert served and served[0]["job"] == "site-postgres"
    assert served[0]["targets"] == 1
