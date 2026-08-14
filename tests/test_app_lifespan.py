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
        # Без счётчиков Метрики коллектор метрики не регистрируется.
        assert scheduler.get_job("collector_metrika") is None

        registered = [entry for entry in captured if entry["event"] == "collector_registered"]
        assert {entry["source"] for entry in registered} == {"uptime", "ssl"}
        assert all(entry["sites"] == 1 for entry in registered)
        assert [entry for entry in captured if entry["event"] == "metrika_collector_skipped"]


def test_lifespan_registers_metrika_collector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = tmp_path / "sites.yml"
    config_file.write_text(
        "sites:\n  - domain: metrika-test.com\n    metrika_counter_id: 42\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SITES_CONFIG_PATH", str(config_file))
    monkeypatch.setenv("YANDEX_METRIKA_OAUTH_TOKEN", "test-token")
    monkeypatch.setattr(main_module, "setup_logging", lambda *a, **k: None)
    get_settings.cache_clear()

    with capture_logs() as captured:
        with TestClient(app) as client:
            scheduler = app.state.scheduler
            metrika_job = scheduler.get_job("collector_metrika")

            response = client.get("/health")

        assert response.status_code == 200
        assert metrika_job is not None
        assert metrika_job.trigger is not None
        assert metrika_job.trigger.interval.total_seconds() == 300

        registered = [entry for entry in captured if entry["event"] == "collector_registered"]
        metrika_registered = [entry for entry in registered if entry["source"] == "metrika"]
        assert metrika_registered[0]["sites"] == 1
        started = [entry for entry in captured if entry["event"] == "app_started"]
        assert "metrika" in started[0]["collectors"]
        # Токен не должен попадать в логи.
        assert all("test-token" not in str(entry) for entry in captured)


def test_lifespan_invalid_sites_config_fails_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = tmp_path / "sites.yml"
    config_file.write_text("sites:\n  - domain: https://bad.com\n", encoding="utf-8")
    monkeypatch.setenv("SITES_CONFIG_PATH", str(config_file))
    get_settings.cache_clear()

    with pytest.raises(Exception, match="без схемы"), TestClient(app):
        pass
