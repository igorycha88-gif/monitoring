"""Тесты SSLCollector: сроки сертификатов, ретраи, ошибки TLS (моки хелпера)."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from prometheus_client import generate_latest
from structlog.testing import capture_logs

import collectors.uptime as uptime_module
from app.sites import SiteConfig
from collectors.uptime import SSLCollector, _get_ssl_not_after


def make_collector(domain: str = "check.com", max_retries: int = 3) -> SSLCollector:
    collector = SSLCollector([SiteConfig(domain=domain)], timeout_seconds=1.0)
    collector.max_retries = max_retries
    collector.retry_base_delay = 0
    return collector


def patch_not_after(
    monkeypatch: pytest.MonkeyPatch,
    behavior: datetime | BaseException | list[datetime | BaseException],
) -> None:
    """Подменяет TLS-хелпер: behavior — datetime, исключение или список результатов."""

    async def fake(host: str, timeout: float) -> datetime:
        outcome = behavior.pop(0) if isinstance(behavior, list) else behavior
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(uptime_module, "_get_ssl_not_after", fake)


async def test_valid_cert_sets_days_left(monkeypatch: pytest.MonkeyPatch) -> None:
    not_after = datetime.now(tz=UTC) + timedelta(days=30, hours=12)
    patch_not_after(monkeypatch, not_after)
    collector = make_collector(domain="ssl-ok.com")

    with capture_logs() as captured:
        result = await collector.run_once()

    assert result == {"sites_ok": 1, "sites_total": 1, "points_total": 1}
    metrics = generate_latest().decode()
    assert 'monitoring_ssl_days_left{site="ssl-ok.com"} 30.0' in metrics
    assert 'monitoring_collector_success{site="ssl-ok.com",source="ssl"} 1.0' in metrics
    done = [entry for entry in captured if entry["event"] == "ssl_check_done"]
    assert done[0]["days_left"] == 30
    assert [entry for entry in captured if entry["event"] == "ssl_expiring_soon"] == []


async def test_expired_cert_sets_negative_days(monkeypatch: pytest.MonkeyPatch) -> None:
    not_after = datetime.now(tz=UTC) - timedelta(days=5, hours=12)
    patch_not_after(monkeypatch, not_after)
    collector = make_collector(domain="ssl-expired.com")

    await collector.run_once()

    metrics = generate_latest().decode()
    assert 'monitoring_ssl_days_left{site="ssl-expired.com"} -6.0' in metrics
    assert 'monitoring_collector_success{site="ssl-expired.com",source="ssl"} 1.0' in metrics


async def test_expiring_soon_logs_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    not_after = datetime.now(tz=UTC) + timedelta(days=10, hours=12)
    patch_not_after(monkeypatch, not_after)
    collector = make_collector(domain="ssl-soon.com")

    with capture_logs() as captured:
        await collector.run_once()

    metrics = generate_latest().decode()
    assert 'monitoring_ssl_days_left{site="ssl-soon.com"} 10.0' in metrics
    warnings = [entry for entry in captured if entry["event"] == "ssl_expiring_soon"]
    assert len(warnings) == 1
    assert warnings[0]["site"] == "ssl-soon.com"
    assert warnings[0]["threshold"] == 14


async def test_persistent_oserror_retried_then_collector_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_not_after(monkeypatch, OSError("connection reset by peer"))
    collector = make_collector(domain="ssl-oserr.com")

    with capture_logs() as captured:
        result = await collector.run_once()

    assert result == {"sites_ok": 0, "sites_total": 1, "points_total": 0}
    metrics = generate_latest().decode()
    assert 'monitoring_collector_errors_total{site="ssl-oserr.com",source="ssl"} 1.0' in metrics
    assert 'monitoring_collector_success{site="ssl-oserr.com",source="ssl"} 0.0' in metrics
    assert 'monitoring_ssl_days_left{site="ssl-oserr.com"}' not in metrics
    retries = [entry for entry in captured if entry["event"] == "collector_retry"]
    assert len(retries) == 2
    errors = [entry for entry in captured if entry["event"] == "collector_site_error"]
    assert errors[0]["error_type"] == "OSError"
    assert errors[0]["operation"] == "collect_site"


async def test_transient_failures_retried_then_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    not_after = datetime.now(tz=UTC) + timedelta(days=30, hours=12)
    behavior: list[datetime | BaseException] = [OSError("blip"), TimeoutError("slow"), not_after]
    patch_not_after(monkeypatch, behavior)
    collector = make_collector(domain="ssl-trans.com")

    with capture_logs() as captured:
        result = await collector.run_once()

    assert result["sites_ok"] == 1
    metrics = generate_latest().decode()
    assert 'monitoring_ssl_days_left{site="ssl-trans.com"} 30.0' in metrics
    retries = [entry for entry in captured if entry["event"] == "collector_retry"]
    assert len(retries) == 2


async def test_value_error_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_not_after(monkeypatch, ValueError("Пир ssl-noval.com не предоставил сертификат"))
    collector = make_collector(domain="ssl-noval.com")

    with capture_logs() as captured:
        result = await collector.run_once()

    assert result["sites_ok"] == 0
    assert [entry for entry in captured if entry["event"] == "collector_retry"] == []
    metrics = generate_latest().decode()
    assert 'monitoring_collector_errors_total{site="ssl-noval.com",source="ssl"} 1.0' in metrics


def _build_self_signed_der(not_after: datetime) -> bytes:
    """Собирает настоящий self-signed сертификат (DER) для проверки парсинга."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "check.com")])
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(tz=UTC) - timedelta(days=1))
        .not_valid_after(not_after)
        .sign(key, hashes.SHA256())
    )
    return builder.public_bytes(serialization.Encoding.DER)


class _FakeSSLObject:
    def __init__(self, der: bytes | None) -> None:
        self._der = der

    def getpeercert(self, binary_form: bool = False) -> bytes | dict[str, str] | None:
        if binary_form:
            return self._der
        return {}  # при CERT_NONE текстовая форма пуста (см. ADR-002)


class _FakeWriter:
    def __init__(self, ssl_object: _FakeSSLObject) -> None:
        self._ssl_object = ssl_object

    def get_extra_info(self, name: str) -> object:
        return self._ssl_object if name == "ssl_object" else None

    def close(self) -> None:
        pass


def patch_open_connection(monkeypatch: pytest.MonkeyPatch, der: bytes | None) -> None:
    async def fake_open_connection(
        host: str, port: int, ssl: object = None
    ) -> tuple[None, _FakeWriter]:
        return None, _FakeWriter(_FakeSSLObject(der))

    monkeypatch.setattr(asyncio, "open_connection", fake_open_connection)


async def test_get_ssl_not_after_parses_der(monkeypatch: pytest.MonkeyPatch) -> None:
    not_after = datetime.now(tz=UTC) + timedelta(days=90)
    patch_open_connection(monkeypatch, _build_self_signed_der(not_after))

    result = await _get_ssl_not_after("check.com", timeout=5)

    assert abs((result - not_after).total_seconds()) < 1


async def test_get_ssl_not_after_no_certificate(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_open_connection(monkeypatch, None)

    with pytest.raises(ValueError, match="не предоставил сертификат"):
        await _get_ssl_not_after("check.com", timeout=5)


async def test_get_ssl_not_after_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    async def slow_open_connection(
        host: str, port: int, ssl: object = None
    ) -> tuple[None, _FakeWriter]:
        await asyncio.sleep(1)
        raise AssertionError("не должно быть достигнуто")

    monkeypatch.setattr(asyncio, "open_connection", slow_open_connection)

    with pytest.raises(TimeoutError):
        await _get_ssl_not_after("check.com", timeout=0.05)


def test_collector_attributes() -> None:
    collector = SSLCollector([SiteConfig(domain="check.com")], timeout_seconds=7.0)
    assert collector.source == "ssl"
    assert collector.parallel is True
    assert collector.timeout_seconds == 7.0
