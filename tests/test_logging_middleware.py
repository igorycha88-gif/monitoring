"""Тесты middleware логирования запросов."""

from fastapi.testclient import TestClient
from structlog.testing import capture_logs


def test_http_request_logged(client: TestClient) -> None:
    with capture_logs() as captured:
        client.get("/health")
    entries = [entry for entry in captured if entry.get("event") == "http_request"]
    assert entries, "middleware должен логировать http_request"
    entry = entries[-1]
    assert entry["method"] == "GET"
    assert entry["path"] == "/health"
    assert entry["status"] == 200
    assert "duration_ms" in entry


def test_metrics_not_logged(client: TestClient) -> None:
    with capture_logs() as captured:
        client.get("/metrics")
    entries = [entry for entry in captured if entry.get("event") == "http_request"]
    assert not entries, "скрейпы /metrics не должны засорять логи"


def test_metrics_no_redirect(client: TestClient) -> None:
    """GET /metrics — прямой 200 без 307 (иначе Prometheus-чек PIPELINE_PROD VF1 падает)."""
    response = client.get("/metrics", follow_redirects=False)
    assert response.status_code == 200, "не должно быть редиректа на /metrics/"
    assert "text/plain" in response.headers["content-type"]
    assert "monitoring_" in response.text or "python_info" in response.text
