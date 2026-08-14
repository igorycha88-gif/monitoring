"""Uptime- и SSL-коллекторы: HTTP-проверки и сроки TLS-сертификатов (ЭПИК-1, ADR-002)."""

import asyncio
import ssl
import time
from collections.abc import Sequence
from datetime import UTC, datetime

import httpx
from cryptography import x509

from app.metrics import (
    SSL_DAYS_LEFT,
    UPTIME_RESPONSE_CODE,
    UPTIME_RESPONSE_SECONDS,
    UPTIME_STATUS,
)
from app.sites import SiteConfig
from collectors.base import BaseCollector

SSL_EXPIRE_WARNING_DAYS = 14
SSL_RETRYABLE_EXCEPTIONS: tuple[type[BaseException], ...] = (TimeoutError, OSError)


async def _get_ssl_not_after(host: str, timeout: float) -> datetime:
    """Дата окончания действия TLS-сертификата host (UTC-aware).

    Хендшейк выполняется без верификации (CERT_NONE): срок читается даже у
    просроченного/самоподписанного сертификата, чтобы сообщать отрицательное
    число дней (ADR-002). Сертификат нигде не используется как доверенный.
    При CERT_NONE getpeercert() без binary_form возвращает пустой dict,
    поэтому сертификат читается в DER и парсится библиотекой cryptography.
    """
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE

    async def _handshake() -> datetime:
        _, writer = await asyncio.open_connection(host, 443, ssl=context)
        try:
            ssl_object = writer.get_extra_info("ssl_object")
            if ssl_object is None:
                raise ValueError(f"TLS не установлен для {host}")
            der = ssl_object.getpeercert(binary_form=True)
            if not der:
                raise ValueError(f"Пир {host} не предоставил сертификат")
        finally:
            writer.close()
        return x509.load_der_x509_certificate(der).not_valid_after_utc

    return await asyncio.wait_for(_handshake(), timeout=timeout)


class UptimeCollector(BaseCollector):
    """HTTP-проверки доступности сайтов (ЭПИК-1).

    Одна попытка за цикл (без ретраев — ADR-002): цикл 60 сек сам является
    ретраем. Сетевые ошибки (timeout/DNS/SSL/conn refused) — это данные
    «сайт недоступен», а не ошибка коллектора: collect_site завершается
    успешно, мониторит collector_success=1.
    """

    source = "uptime"
    parallel = True

    def __init__(
        self,
        sites: Sequence[SiteConfig],
        timeout_seconds: float,
        success_max_code: int = 399,
    ) -> None:
        super().__init__(sites)
        self.timeout_seconds = timeout_seconds
        self.success_max_code = success_max_code

    async def collect_site(self, site: SiteConfig) -> int:
        """Проверяет https://{domain}/ и пишет 3 метрики. Возвращает число точек."""
        url = f"https://{site.domain}/"
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds, follow_redirects=True
            ) as client:
                response = await client.get(url)
        except httpx.HTTPError as exc:
            elapsed = time.perf_counter() - started
            UPTIME_STATUS.labels(site=site.domain).set(0)
            UPTIME_RESPONSE_CODE.labels(site=site.domain).set(0)
            UPTIME_RESPONSE_SECONDS.labels(site=site.domain).set(elapsed)
            self.logger.warning(
                "uptime_check_failed",
                site=site.domain,
                url=url,
                error=str(exc),
                error_type=type(exc).__name__,
                operation="http_check",
            )
            return 1

        elapsed = time.perf_counter() - started
        up = 1 if response.status_code <= self.success_max_code else 0
        UPTIME_STATUS.labels(site=site.domain).set(up)
        UPTIME_RESPONSE_CODE.labels(site=site.domain).set(response.status_code)
        UPTIME_RESPONSE_SECONDS.labels(site=site.domain).set(elapsed)
        self.logger.info(
            "uptime_check_done",
            site=site.domain,
            url=url,
            status_code=response.status_code,
            up=up,
            duration_ms=round(elapsed * 1000, 2),
        )
        return 3


class SSLCollector(BaseCollector):
    """Проверка сроков TLS-сертификатов сайтов (ЭПИК-1).

    Редкий цикл (раз в час), поэтому транзиентные сбои ретраятся
    (retry_on=(TimeoutError, OSError)); исчерпание ретраев — ошибка
    коллектора, метрика ssl_days_left не обновляется (ADR-002).
    """

    source = "ssl"
    parallel = True

    def __init__(self, sites: Sequence[SiteConfig], timeout_seconds: float) -> None:
        super().__init__(sites)
        self.timeout_seconds = timeout_seconds

    async def collect_site(self, site: SiteConfig) -> int:
        """Читает notAfter сертификата и пишет monitoring_ssl_days_left."""
        not_after = await self.retry(
            lambda: _get_ssl_not_after(site.domain, self.timeout_seconds),
            retry_on=SSL_RETRYABLE_EXCEPTIONS,
        )
        days_left = (not_after - datetime.now(tz=UTC)).days
        SSL_DAYS_LEFT.labels(site=site.domain).set(days_left)
        self.logger.info(
            "ssl_check_done",
            site=site.domain,
            days_left=days_left,
            not_after=not_after.isoformat(),
        )
        if days_left <= SSL_EXPIRE_WARNING_DAYS:
            self.logger.warning(
                "ssl_expiring_soon",
                site=site.domain,
                days_left=days_left,
                threshold=SSL_EXPIRE_WARNING_DAYS,
            )
        return 1
