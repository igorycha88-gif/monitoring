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


def test_site_metrics_jobs_use_http_sd_without_secrets() -> None:
    """ЭПИК-9/ADR-010: 4 job'а site-* скрейпят relay приложения без ключей."""
    config = load_config()
    for kind in SITE_METRICS_KINDS:
        job = jobs(config)[f"site-{kind}"]
        sd_configs = job["http_sd_configs"]
        assert len(sd_configs) == 1, kind
        assert sd_configs[0]["url"] == f"http://app:8088/api/v1/sd/site-metrics/{kind}", kind
        assert sd_configs[0]["refresh_interval"] == "60s", kind
        # Ключ подставляет relay приложения — в конфиге Prometheus секретов нет
        assert "http_headers" not in job, kind
        # Запас поверх таймаута relay (10 с)
        assert job["scrape_timeout"] == "12s", kind


def test_prometheus_config_no_real_key() -> None:
    """ADR-010: секреты не попадают в конфиг Prometheus (скрейп через relay)."""
    content = PROMETHEUS_CONFIG.read_text(encoding="utf-8")
    assert "__SITE_METRICS_API_KEY__" not in content
    for job in load_config()["scrape_configs"]:
        assert "http_headers" not in job, job["job_name"]
    entrypoint = (
        Path(__file__).resolve().parent.parent / "prometheus" / "prometheus-entrypoint.sh"
    ).read_text(encoding="utf-8")
    assert "SITE_METRICS_API_KEY" not in entrypoint


def test_prometheus_out_of_order_window_for_event_metrics() -> None:
    """ADR-012: событийные метрики сайта с ЯВНЫМИ timestamp'ами требуют OOO-окна.

    Сайт повторно выдаёт каждый клик (business_phone_clicks_event, окно
    рендера 24ч) на каждом скрейпе — это дубликаты; out_of_order_time_window
    ≥ 25h (окно рендера + запас) позволяет Prometheus молча дедуплицировать
    (ts+value совпадают) и принимать новые события внутри окна.
    Поле принадлежит секции storage.tsdb (BUG-001: в global — невалидно).
    """
    config = load_config()
    assert config["storage"]["tsdb"]["out_of_order_time_window"] == "25h"


def test_prometheus_retention_supports_year_slices() -> None:
    """ADR-008: годовые срезы [$period=365d] требуют долгого хранения данных.

    Фикс Prometheus 3.14 (коммит 9ed7d15): time-ретеншн отключён (баг
    BeyondTimeRetention µs-vs-ms удалял backfill-блоки как obsolete),
    вместо него size-ретеншн 20GB в КОНФИГЕ; entrypoint не задаёт флаг
    retention (deprecated) и пробрасывает "$@" из compose.
    """
    root = Path(__file__).resolve().parent.parent
    config = load_config()
    storage = config["storage"]["tsdb"]["retention"]
    assert "size" in storage, "size-ретеншн обязателен (замена time-ретеншну)"
    assert storage["size"] == "20GB"
    assert "time" not in storage, "time-ретеншн удаляет backfill-блоки (баг 3.14)"
    entrypoint = (root / "prometheus" / "prometheus-entrypoint.sh").read_text(encoding="utf-8")
    exec_block = entrypoint.split("exec prometheus", 1)[1]
    assert "--storage.tsdb.retention" not in exec_block, "флаг deprecated — ретеншн в конфиге"
    assert '"$@"' in entrypoint
    compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
    assert "storage.tsdb.retention" not in compose, "ретеншн только в конфиге (иначе повтор)"
