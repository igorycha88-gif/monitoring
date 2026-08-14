"""Общие фикстуры тестов."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app


@pytest.fixture(autouse=True)
def _reset_settings_cache() -> Iterator[None]:
    """Сбрасывает кэш настроек после каждого теста (изоляция monkeypatch env)."""
    yield
    get_settings.cache_clear()


@pytest.fixture
def client() -> Iterator[TestClient]:
    """TestClient с выполнением lifespan (логирование + планировщик)."""
    with TestClient(app) as test_client:
        yield test_client
