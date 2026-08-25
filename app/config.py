"""Настройки приложения (pydantic-settings). Секреты — только через .env."""

import json
from functools import lru_cache
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


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
    # Рендер поисковых метрик из БД (ADR-011): окно дневных точек (дни,
    # граница кардинальности) и период фонового обновления кэша (сек)
    webmaster_render_days: int = 35
    webmaster_render_refresh_seconds: float = 60.0
    # Горизонт готовности дневного рендера (дни): агрегаты Яндекса
    # финализируются с лагом более суток — самый свежий завершённый день
    # всегда нули; рендерится последний день старше этого лага
    # (инцидент 2026-08-25 «вечный ноль дневных метрик»)
    webmaster_render_lag_days: Annotated[int, Field(ge=1)] = 2

    yandex_metrika_oauth_token: str = ""
    yandex_webmaster_oauth_token: str = ""
    # Персайтные OAuth-токены Вебмастера: JSON {"<domain>": "<token>"}
    # (домен — unicode, как в sites.yml). Сайты верифицируются в разных
    # аккаунтах Яндекса → токен на сайт. Fallback для доменов без записи —
    # глобальный yandex_webmaster_oauth_token (ЧТЗ_Вебмастер_Персайтные_токены).
    # NoDecode: пустая строка из env валидна (не JSON-ошибка источника).
    yandex_webmaster_oauth_tokens: Annotated[dict[str, str], NoDecode] = Field(default_factory=dict)

    # ЭПИК-9: health-коллектор эндпоинтов метрик сайтов (ADR-007/ADR-010)
    site_metrics_interval_seconds: int = 60
    site_metrics_timeout_seconds: float = 10.0
    site_metrics_api_key: str = ""
    # Персайтные ключи X-Monitoring-Key (ADR-010 D1): JSON {"<domain>": "<ключ>"}.
    # Fallback для доменов без записи — site_metrics_api_key.
    # NoDecode: пустая строка из env валидна (не JSON-ошибка источника).
    site_metrics_api_keys: Annotated[dict[str, str], NoDecode] = Field(default_factory=dict)

    @field_validator("site_metrics_api_keys", mode="before")
    @classmethod
    def parse_site_metrics_api_keys(cls, value: object) -> object:
        """Строка из env → JSON; пустая строка/None → {}."""
        if value is None or (isinstance(value, str) and not value.strip()):
            return {}
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except ValueError as exc:
                raise ValueError(
                    "SITE_METRICS_API_KEYS должен быть JSON вида {'домен': 'ключ'}"
                ) from exc
            if not isinstance(parsed, dict):
                raise ValueError("SITE_METRICS_API_KEYS должен быть JSON-объектом {домен: ключ}")
            return parsed
        return value

    @field_validator("site_metrics_api_keys", mode="after")
    @classmethod
    def normalize_site_metrics_api_keys(cls, value: dict[str, str]) -> dict[str, str]:
        """Домены-ключи JSON — в нижнем регистре (как domain в sites.yml)."""
        return {domain.strip().lower(): key for domain, key in value.items()}

    def site_metrics_key(self, domain: str) -> str:
        """Ключ X-Monitoring-Key сайта (ADR-010 D1): персональный → глобальный → ""."""
        return self.site_metrics_api_keys.get(domain.strip().lower()) or self.site_metrics_api_key

    @field_validator("yandex_webmaster_oauth_tokens", mode="before")
    @classmethod
    def parse_webmaster_tokens(cls, value: object) -> object:
        """Строка из env → JSON; пустая строка/None → {}."""
        if value is None or (isinstance(value, str) and not value.strip()):
            return {}
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except ValueError as exc:
                raise ValueError(
                    "YANDEX_WEBMASTER_OAUTH_TOKENS должен быть JSON вида {'домен': 'токен'}"
                ) from exc
            if not isinstance(parsed, dict):
                raise ValueError(
                    "YANDEX_WEBMASTER_OAUTH_TOKENS должен быть JSON-объектом {домен: токен}"
                )
            return parsed
        return value

    @field_validator("yandex_webmaster_oauth_tokens", mode="after")
    @classmethod
    def normalize_webmaster_tokens(cls, value: dict[str, str]) -> dict[str, str]:
        """Домены-ключи JSON — в нижнем регистре (как domain в sites.yml)."""
        return {domain.strip().lower(): token for domain, token in value.items()}

    def webmaster_token(self, domain: str) -> str:
        """OAuth-токен Вебмастера сайта: персональный → глобальный → ""."""
        return (
            self.yandex_webmaster_oauth_tokens.get(domain.strip().lower())
            or self.yandex_webmaster_oauth_token
        )


@lru_cache
def get_settings() -> Settings:
    """Синглтон настроек (кэшируется на процесс)."""
    return Settings()
