"""Тесты lifespan: регистрация uptime/ssl коллекторов в планировщике."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from structlog.testing import capture_logs

import app.main as main_module
from app.config import get_settings
from app.main import app


def test_lifespan_registers_collectors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_file = tmp_path / "sites.yml"
    config_file.write_text("sites:\n  - domain: lifespan-test.com\n", encoding="utf-8")
    monkeypatch.setenv("SITES_CONFIG_PATH", str(config_file))
    get_settings.cache_clear()
    # setup_logging внутри lifespan переконфигурирует structlog и сломал бы
    # capture_logs; для теста регистрации достаточно уже настроенного логгера.
    monkeypatch.setattr(main_module, "setup_logging", lambda *a, **k: None)

    with capture_logs() as captured:
        with TestClient(app) as client:
            scheduler = app.state.scheduler
            uptime_job = scheduler.get_job("collector_uptime")
            ssl_job = scheduler.get_job("collector_ssl")

            response = client.get("/health")

        assert response.status_code == 200
        assert uptime_job is not None
        assert uptime_job.trigger is not None
        assert uptime_job.trigger.interval.total_seconds() == 60
        assert ssl_job is not None
        assert ssl_job.trigger is not None
        assert ssl_job.trigger.interval.total_seconds() == 3600

        registered = [entry for entry in captured if entry["event"] == "collector_registered"]
        assert {entry["source"] for entry in registered} == {"uptime", "ssl"}
        assert all(entry["sites"] == 1 for entry in registered)


def test_lifespan_invalid_sites_config_fails_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = tmp_path / "sites.yml"
    config_file.write_text("sites:\n  - domain: https://bad.com\n", encoding="utf-8")
    monkeypatch.setenv("SITES_CONFIG_PATH", str(config_file))
    get_settings.cache_clear()

    with pytest.raises(Exception, match="без схемы"), TestClient(app):
        pass
