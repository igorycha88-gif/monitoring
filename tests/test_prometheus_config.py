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
