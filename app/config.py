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

    uptime_interval_seconds: int = 60
    uptime_timeout_seconds: float = 10.0
    uptime_success_max_code: int = 399
    ssl_interval_seconds: int = 3600
    ssl_timeout_seconds: float = 10.0

    metrika_interval_seconds: int = 300
    metrika_timeout_seconds: float = 10.0

    # Ежедневный cron запуска (Europe/Moscow). Часы через запятую: утренний
    # прогон + вечерний дозаполняющий — агрегаты Яндекса финализируются с
    # лагом, gauge перезаписывается финальным значением того же дня (ADR-008)
    webmaster_cron_hour: str = "7,19"
    webmaster_cron_timezone: str = "Europe/Moscow"
    webmaster_timeout_seconds: float = 10.0
    webmaster_top_queries: int = 50
    # Окно запроса дневной истории поиска (ADR-008): запас на лаг обновления данных Яндекса
    webmaster_history_days: int = 7
    # Долговременное хранение данных Вебмастера в SQLite (ADR-009);
    # пустая строка — хранение отключено
    webmaster_db_path: str = "data/webmaster.db"

    yandex_metrika_oauth_token: str = ""
    yandex_webmaster_oauth_token: str = ""

    # ЭПИК-9: health-коллектор эндпоинтов метрик сайтов (ADR-007)
    site_metrics_interval_seconds: int = 60
    site_metrics_timeout_seconds: float = 10.0
    site_metrics_api_key: str = ""


@lru_cache
def get_settings() -> Settings:
    """Синглтон настроек (кэшируется на процесс)."""
    return Settings()
