"""Настройки приложения (pydantic-settings). Секреты — только через .env."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Глобальные настройки приложения мониторинга."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    log_level: str = "INFO"
    log_format: str = "json"
    sites_config_path: str = "config/sites.yml"

    yandex_metrika_oauth_token: str = ""
    yandex_webmaster_oauth_token: str = ""


@lru_cache
def get_settings() -> Settings:
    """Синглтон настроек (кэшируется на процесс)."""
    return Settings()
