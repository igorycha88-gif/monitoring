"""Тесты GET /api/v1/sites."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings

VALID_YAML = """
sites:
  - domain: Example.COM
    metrika_counter_id: 12345
  - domain: example.org
"""


def use_sites_file(tmp_path: Path, content: str, monkeypatch: pytest.MonkeyPatch) -> None:
    config_file = tmp_path / "sites.yml"
    config_file.write_text(content, encoding="utf-8")
    monkeypatch.setenv("SITES_CONFIG_PATH", str(config_file))
    get_settings.cache_clear()


def test_list_sites_ok(client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    use_sites_file(tmp_path, VALID_YAML, monkeypatch)
    response = client.get("/api/v1/sites")
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 2
    domains = [site["domain"] for site in body["sites"]]
    assert domains == ["example.com", "example.org"]
    assert body["sites"][0]["metrika_counter_id"] == 12345


def test_list_sites_file_missing(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SITES_CONFIG_PATH", str(tmp_path / "missing.yml"))
    get_settings.cache_clear()
    response = client.get("/api/v1/sites")
    assert response.status_code == 500
    assert "не найден" in response.json()["detail"]


def test_list_sites_invalid_config(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_sites_file(tmp_path, "sites:\n  - domain: https://bad.com\n", monkeypatch)
    response = client.get("/api/v1/sites")
    assert response.status_code == 500
    assert "без схемы" in response.json()["detail"]
