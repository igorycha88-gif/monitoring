"""Структурированное логирование (structlog)."""

import logging
from typing import cast

import structlog


def setup_logging(level: str = "INFO", log_format: str = "json") -> None:
    """Конфигурирует structlog. Вызывается при старте приложения."""
    shared_processors: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    renderer: structlog.typing.Processor
    if log_format == "console":
        renderer = structlog.dev.ConsoleRenderer()
    else:
        renderer = structlog.processors.JSONRenderer()

    level_number = logging.getLevelNamesMapping().get(level.upper(), logging.INFO)
    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level_number),
        cache_logger_on_first_use=False,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Фабрика именованных логгеров."""
    logger = cast(structlog.stdlib.BoundLogger, structlog.get_logger(name))
    return logger
