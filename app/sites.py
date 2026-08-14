"""Загрузка и валидация конфигурации сайтов (config/sites.yml)."""

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.logging import get_logger


class SiteConfigError(Exception):
    """Ошибка конфигурации сайтов."""


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
