"""Тесты Settings: ключи uptime/ssl (дефолты и переопределение из env)."""

import pytest

from app.config import Settings


def test_uptime_ssl_defaults() -> None:
    settings = Settings(_env_file=None)
    assert settings.uptime_interval_seconds == 60
    assert settings.uptime_timeout_seconds == 10.0
    assert settings.uptime_success_max_code == 399
    assert settings.ssl_interval_seconds == 3600
    assert settings.ssl_timeout_seconds == 10.0


def test_uptime_ssl_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UPTIME_INTERVAL_SECONDS", "120")
    monkeypatch.setenv("UPTIME_TIMEOUT_SECONDS", "5.5")
    monkeypatch.setenv("UPTIME_SUCCESS_MAX_CODE", "299")
    monkeypatch.setenv("SSL_INTERVAL_SECONDS", "7200")
    monkeypatch.setenv("SSL_TIMEOUT_SECONDS", "3.0")

    settings = Settings(_env_file=None)

    assert settings.uptime_interval_seconds == 120
    assert settings.uptime_timeout_seconds == 5.5
    assert settings.uptime_success_max_code == 299
    assert settings.ssl_interval_seconds == 7200
    assert settings.ssl_timeout_seconds == 3.0


def test_site_metrics_defaults() -> None:
    settings = Settings(_env_file=None)
    assert settings.site_metrics_interval_seconds == 60
    assert settings.site_metrics_timeout_seconds == 10.0
    assert settings.site_metrics_api_key == ""


def test_webmaster_history_days_default() -> None:
    settings = Settings(_env_file=None)
    assert settings.webmaster_history_days == 7


def test_webmaster_history_days_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEBMASTER_HISTORY_DAYS", "14")
    settings = Settings(_env_file=None)
    assert settings.webmaster_history_days == 14


def test_site_metrics_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SITE_METRICS_INTERVAL_SECONDS", "30")
    monkeypatch.setenv("SITE_METRICS_TIMEOUT_SECONDS", "5.0")
    monkeypatch.setenv("SITE_METRICS_API_KEY", "test-key")

    settings = Settings(_env_file=None)

    assert settings.site_metrics_interval_seconds == 30
    assert settings.site_metrics_timeout_seconds == 5.0
    assert settings.site_metrics_api_key == "test-key"
