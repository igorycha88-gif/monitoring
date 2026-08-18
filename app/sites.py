"""Загрузка и валидация конфигурации сайтов (config/sites.yml)."""

from pathlib import Path
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.logging import get_logger


class SiteConfigError(Exception):
    """Ошибка конфигурации сайтов."""


# ЭПИК-9 (ADR-007): допустимые kinds эндпоинтов метрик сайта
SITE_METRICS_KINDS: frozenset[str] = frozenset({"tracking", "content", "node", "postgres"})


class SiteConfig(BaseModel):
    """Один мониторируемый сайт."""

    model_config = ConfigDict(extra="forbid")

    domain: str = Field(min_length=1, description="Домен сайта, например example.com")
    metrika_counter_id: int | None = Field(default=None, description="ID счётчика Яндекс.Метрики")
    webmaster_host_id: str | None = Field(
        default=None, description="host_id сайта в Яндекс.Вебмастере"
    )
    node_exporter_url: str | None = Field(
        default=None, description="URL node_exporter сервера сайта"
    )
    metrics_urls: dict[str, str] | None = Field(
        default=None,
        description="Эндпоинты метрик сайта за X-Monitoring-Key (ADR-007)",
    )

    @field_validator("domain")
    @classmethod
    def validate_domain(cls, value: str) -> str:
        """Домен: без схемы, без пути, в нижнем регистре."""
        domain = value.strip().lower()
        if not domain:
            raise ValueError("domain не может быть пустым")
        if "://" in domain or domain.startswith(("http://", "https://")):
            raise ValueError("domain должен быть доменом без схемы (без https://)")
        if "/" in domain:
            raise ValueError("domain должен быть доменом без пути")
        return domain

    @field_validator("node_exporter_url")
    @classmethod
    def validate_node_exporter_url(cls, value: str | None) -> str | None:
        """URL node_exporter: схема http/https, непустой host, без query/fragment."""
        if value is None:
            return value
        url = value.strip()
        if not url:
            raise ValueError("node_exporter_url не может быть пустым (уберите поле)")
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https"):
            raise ValueError("node_exporter_url должен начинаться с http:// или https://")
        if not parsed.hostname:
            raise ValueError("node_exporter_url должен содержать host")
        if parsed.query or parsed.fragment:
            raise ValueError("node_exporter_url не должен содержать query (?) или fragment (#)")
        try:
            _ = parsed.port
        except ValueError as exc:
            raise ValueError(f"некорректный порт в node_exporter_url: {exc}") from exc
        return url

    @field_validator("metrics_urls")
    @classmethod
    def validate_metrics_urls(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        """Эндпоинты метрик: kinds из whitelist, URL — строго https (ADR-007 D2)."""
        if value is None:
            return value
        if not value:
            raise ValueError("metrics_urls не может быть пустым (уберите поле)")
        unknown = sorted(set(value) - SITE_METRICS_KINDS)
        if unknown:
            raise ValueError(
                "неизвестные kinds в metrics_urls: "
                f"{', '.join(unknown)} (допустимо: {', '.join(sorted(SITE_METRICS_KINDS))})"
            )
        for kind, url in value.items():
            parsed = urlsplit(url)
            if parsed.scheme != "https":
                raise ValueError(f"metrics_urls.{kind} должен начинаться с https://")
            if not parsed.hostname:
                raise ValueError(f"metrics_urls.{kind} должен содержать host")
            if parsed.query or parsed.fragment:
                raise ValueError(
                    f"metrics_urls.{kind} не должен содержать query (?) или fragment (#)"
                )
            if not parsed.path or parsed.path == "/":
                raise ValueError(
                    f"metrics_urls.{kind} должен содержать путь (например /metrics/{kind})"
                )
        return value


def load_sites(path: str | Path) -> list[SiteConfig]:
    """Загружает список сайтов. Бросает SiteConfigError при любой проблеме."""
    config_path = Path(path)
    logger = get_logger("sites")

    if not config_path.is_file():
        raise SiteConfigError(f"Файл конфигурации сайтов не найден: {config_path}")

    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise SiteConfigError(f"Некорректный YAML в {config_path}: {exc}") from exc

    if not isinstance(raw, dict) or not isinstance(raw.get("sites"), list):
        raise SiteConfigError(f"Ожидается структура 'sites: [<сайт>, ...]' в {config_path}")

    sites: list[SiteConfig] = []
    for index, item in enumerate(raw["sites"]):
        try:
            sites.append(SiteConfig.model_validate(item))
        except ValidationError as exc:
            raise SiteConfigError(f"Некорректный сайт #{index + 1} в {config_path}: {exc}") from exc

    domains = [site.domain for site in sites]
    duplicates = sorted({domain for domain in domains if domains.count(domain) > 1})
    if duplicates:
        raise SiteConfigError(f"Дублирующиеся домены в {config_path}: {', '.join(duplicates)}")

    logger.info("sites_config_loaded", path=str(config_path), sites=len(sites))
    return sites
