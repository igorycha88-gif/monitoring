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
    assert settings.site_metrics_api_keys == {}


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


def test_site_metrics_api_keys_json_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    """ADR-010 D1: JSON из env → dict, домены нормализуются в нижний регистр."""
    monkeypatch.setenv("SITE_METRICS_API_KEYS", '{"эвакуация.online": "k1", "A.COM": "k2"}')

    settings = Settings(_env_file=None)

    assert settings.site_metrics_api_keys == {"эвакуация.online": "k1", "a.com": "k2"}


def test_site_metrics_api_keys_empty_string_is_empty_dict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SITE_METRICS_API_KEYS", "")
    assert Settings(_env_file=None).site_metrics_api_keys == {}


def test_site_metrics_key_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    """ADR-010 D1: персональный ключ → глобальный fallback → пусто."""
    monkeypatch.setenv("SITE_METRICS_API_KEY", "global-key")
    monkeypatch.setenv("SITE_METRICS_API_KEYS", '{"эвакуация.online": "own-key"}')

    settings = Settings(_env_file=None)

    assert settings.site_metrics_key("эвакуация.online") == "own-key"
    assert settings.site_metrics_key("da-dryclean.ru") == "global-key"
    assert settings.site_metrics_key("ЭВАКУАЦИЯ.online") == "own-key"


def test_site_metrics_key_no_keys_at_all(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SITE_METRICS_API_KEY", "")
    monkeypatch.setenv("SITE_METRICS_API_KEYS", "")

    settings = Settings(_env_file=None)

    assert settings.site_metrics_key("any.test") == ""


# --- Персайтные OAuth-токены Вебмастера (ЧТЗ_Вебмастер_Персайтные_токены) ---


def test_webmaster_tokens_json_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    """JSON из env → dict, домены нормализуются в нижний регистр."""
    monkeypatch.setenv("YANDEX_WEBMASTER_OAUTH_TOKENS", '{"эвакуация.online": "t1", "A.COM": "t2"}')

    settings = Settings(_env_file=None)

    assert settings.yandex_webmaster_oauth_tokens == {
        "эвакуация.online": "t1",
        "a.com": "t2",
    }


def test_webmaster_tokens_empty_string_is_empty_dict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("YANDEX_WEBMASTER_OAUTH_TOKENS", "")
    assert Settings(_env_file=None).yandex_webmaster_oauth_tokens == {}


def test_webmaster_tokens_invalid_json_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    import pydantic

    monkeypatch.setenv("YANDEX_WEBMASTER_OAUTH_TOKENS", "not-json")

    with pytest.raises(pydantic.ValidationError):
        Settings(_env_file=None)


def test_webmaster_token_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    """Персайтный токен → глобальный fallback → пусто."""
    monkeypatch.setenv("YANDEX_WEBMASTER_OAUTH_TOKEN", "global-token")
    monkeypatch.setenv("YANDEX_WEBMASTER_OAUTH_TOKENS", '{"эвакуация.online": "own-token"}')

    settings = Settings(_env_file=None)

    assert settings.webmaster_token("эвакуация.online") == "own-token"
    assert settings.webmaster_token("ЭВАКУАЦИЯ.online") == "own-token"
    assert settings.webmaster_token("da-dryclean.ru") == "global-token"
    assert settings.webmaster_token("other.test") == "global-token"


def test_webmaster_token_no_tokens_at_all(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YANDEX_WEBMASTER_OAUTH_TOKEN", "")
    monkeypatch.setenv("YANDEX_WEBMASTER_OAUTH_TOKENS", "")

    settings = Settings(_env_file=None)

    assert settings.webmaster_token("any.test") == ""
