"""Проверка конфигурации Prometheus (ЭПИК-6: http_sd node-exporter)."""

from pathlib import Path
from typing import Any

import yaml

PROMETHEUS_CONFIG = Path(__file__).resolve().parent.parent / "prometheus" / "prometheus.yml"


def load_config() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(PROMETHEUS_CONFIG.read_text(encoding="utf-8"))
    return data


def jobs(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {job["job_name"]: job for job in config["scrape_configs"]}


def test_app_job_scrapes_application() -> None:
    job = jobs(load_config())["monitoring-app"]
    assert job["static_configs"] == [{"targets": ["app:8088"]}]


def test_node_exporter_job_uses_http_sd() -> None:
    job = jobs(load_config())["node-exporter"]
    sd_configs = job["http_sd_configs"]
    assert len(sd_configs) == 1
    assert sd_configs[0]["url"] == "http://app:8088/api/v1/sd/node-exporter"
    assert sd_configs[0]["refresh_interval"] == "60s"


SITE_METRICS_KINDS = ("tracking", "content", "node", "postgres")


def test_site_metrics_jobs_use_http_sd_and_key_header() -> None:
    """ЭПИК-9: 4 job'а site-* скрейпят эндпоинты сайтов с X-Monitoring-Key."""
    config = load_config()
    for kind in SITE_METRICS_KINDS:
        job = jobs(config)[f"site-{kind}"]
        sd_configs = job["http_sd_configs"]
        assert len(sd_configs) == 1, kind
        assert sd_configs[0]["url"] == f"http://app:8088/api/v1/sd/site-metrics/{kind}", kind
        assert sd_configs[0]["refresh_interval"] == "60s", kind
        header = job["http_headers"]["X-Monitoring-Key"]
        # В конфиге — только плейсхолдер; реальный ключ подставляет entrypoint
        assert header == {"values": ["__SITE_METRICS_API_KEY__"]}, kind


def test_prometheus_config_no_real_key() -> None:
    """Секрет не должен попадать в конфиг-шаблон (ADR-007 D3)."""
    content = PROMETHEUS_CONFIG.read_text(encoding="utf-8")
    assert "__SITE_METRICS_API_KEY__" in content
    assert "X-Monitoring-Key" in content
