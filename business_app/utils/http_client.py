"""
Resilient HTTP client utilities for backend → external provider calls (PAY-003).

Provides a sync `request_with_retry` helper that combines:
  - per-request timeout
  - jittered exponential backoff retries on transient failures (timeouts,
    connection errors, 5xx responses)
  - per-host circuit breaker that fails fast after consecutive failures

Sync (uses `requests`) because the backend is sync Flask + Celery. Mirrors
the async `CircuitBreaker` pattern in [telegram_bot/api_client.py](../../telegram_bot/api_client.py).

Usage:
    from business_app.utils.http_client import request_with_retry, RetryConfig
    from business_app.utils.exceptions import ProviderUnavailableError

    try:
        response = request_with_retry(
            method='POST',
            url='https://api.click.uz/v2/merchant/payment/...',
            json={...},
            timeout_seconds=10,
            retry_config=RetryConfig(max_retries=2, backoff_base_seconds=0.5),
            circuit_key='click_merchant_api',
        )
    except ProviderUnavailableError:
        # circuit open or all retries exhausted — surface 503 to caller
        ...
"""

from __future__ import annotations

import logging
import random
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import requests

from business_app.utils.exceptions import ProviderUnavailableError

logger = logging.getLogger(__name__)


@dataclass
class RetryConfig:
    """Retry behaviour for a single request.

    Total request budget = (max_retries + 1) attempts.
    Backoff between attempt N and N+1 = backoff_base * 2^N + jitter.
    """

    max_retries: int = 2
    backoff_base_seconds: float = 0.5
    backoff_max_seconds: float = 8.0
    # HTTP status codes that count as retryable.
    retry_on_status: tuple = (500, 502, 503, 504)


class CircuitBreaker:
    """Per-host circuit breaker — fails fast after threshold consecutive failures.

    States:
      CLOSED   – requests flow normally.
      OPEN     – requests fail immediately for `recovery_timeout_seconds`.
      HALF_OPEN – one probe request is allowed; success closes, failure re-opens.

    Thread-safe: backend Gunicorn config uses gthread workers; multiple threads
    in one process can share a breaker.
    """

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

    def __init__(self, failure_threshold: int = 5, recovery_timeout_seconds: float = 30.0):
        self.failure_threshold = failure_threshold
        self.recovery_timeout_seconds = recovery_timeout_seconds
        self._state = self.CLOSED
        self._failure_count = 0
        self._last_failure_time: Optional[datetime] = None
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        with self._lock:
            if self._state == self.OPEN and self._last_failure_time:
                elapsed = (datetime.now(timezone.utc) - self._last_failure_time).total_seconds()
                if elapsed >= self.recovery_timeout_seconds:
                    self._state = self.HALF_OPEN
            return self._state

    def allow_request(self) -> bool:
        return self.state != self.OPEN

    def record_success(self) -> None:
        with self._lock:
            self._failure_count = 0
            self._state = self.CLOSED

    def record_failure(self) -> None:
        with self._lock:
            self._failure_count += 1
            self._last_failure_time = datetime.now(timezone.utc)
            if self._failure_count >= self.failure_threshold:
                self._state = self.OPEN
                logger.warning(
                    "Circuit breaker OPEN after %d consecutive failures. " "Failing fast for %ss.",
                    self._failure_count,
                    self.recovery_timeout_seconds,
                )


# Module-level registry so independent call sites for the same provider share
# breaker state. Keyed by `circuit_key` (typically `<provider>_<endpoint_group>`).
_CIRCUIT_BREAKERS: Dict[str, CircuitBreaker] = {}
_CIRCUIT_BREAKERS_LOCK = threading.Lock()


def get_circuit_breaker(
    key: str,
    failure_threshold: int = 5,
    recovery_timeout_seconds: float = 30.0,
) -> CircuitBreaker:
    """Return (and lazily create) a shared CircuitBreaker for `key`.

    Threshold/timeout are read on first creation only — subsequent callers get
    the existing breaker regardless of what they pass. Callers that need
    different policy should use distinct keys.
    """
    with _CIRCUIT_BREAKERS_LOCK:
        breaker = _CIRCUIT_BREAKERS.get(key)
        if breaker is None:
            breaker = CircuitBreaker(failure_threshold, recovery_timeout_seconds)
            _CIRCUIT_BREAKERS[key] = breaker
        return breaker


def _is_retryable_exception(exc: Exception) -> bool:
    """Treat connection/read timeouts and connection errors as retryable."""
    return isinstance(
        exc,
        (
            requests.ConnectionError,
            requests.Timeout,  # both ConnectTimeout + ReadTimeout
        ),
    )


def request_with_retry(
    *,
    method: str,
    url: str,
    timeout_seconds: float,
    retry_config: Optional[RetryConfig] = None,
    circuit_key: Optional[str] = None,
    circuit_failure_threshold: int = 5,
    circuit_recovery_seconds: float = 30.0,
    **request_kwargs: Any,
) -> requests.Response:
    """Make an HTTP request with retry + optional circuit breaker.

    Returns the final `requests.Response` on success (status code in
    `retry_config.retry_on_status` only counts as failure for retry purposes;
    the caller still sees the response if all retries are exhausted with a
    non-2xx status).

    Raises:
        ProviderUnavailableError: circuit open OR all retry attempts failed
            with a network/connection error. Carries `retry_after_seconds` so
            the caller can propagate `Retry-After` to upstream gateways.

    Args:
        method: HTTP method (GET, POST, etc.)
        url: target URL
        timeout_seconds: per-attempt timeout (NOT cumulative across retries)
        retry_config: retry policy. Defaults to RetryConfig() if None.
        circuit_key: if set, share circuit-breaker state across calls with the
            same key. Recommended: `<provider>_<endpoint_group>`.
        request_kwargs: forwarded to `requests.request` (json, params, headers, ...)
    """
    config = retry_config or RetryConfig()
    breaker = (
        get_circuit_breaker(circuit_key, circuit_failure_threshold, circuit_recovery_seconds) if circuit_key else None
    )

    if breaker is not None and not breaker.allow_request():
        logger.warning("Circuit OPEN for %s — failing fast (url=%s)", circuit_key, url)
        raise ProviderUnavailableError(
            f"Provider circuit open for {circuit_key}",
            provider=circuit_key,
            retry_after_seconds=int(breaker.recovery_timeout_seconds),
        )

    last_exception: Optional[Exception] = None
    last_response: Optional[requests.Response] = None
    total_attempts = config.max_retries + 1

    for attempt in range(total_attempts):
        try:
            response = requests.request(
                method=method,
                url=url,
                timeout=timeout_seconds,
                **request_kwargs,
            )
        except Exception as exc:
            last_exception = exc
            if not _is_retryable_exception(exc):
                # Permanent client-side error — don't retry, don't trip breaker.
                # (Retrying a malformed URL won't help.)
                raise
            logger.warning(
                "HTTP request failed (attempt %d/%d) %s %s: %s",
                attempt + 1,
                total_attempts,
                method,
                url,
                exc,
            )
        else:
            # Got a response. Decide if it's a retry-worthy 5xx.
            if response.status_code not in config.retry_on_status:
                if breaker is not None:
                    breaker.record_success()
                return response
            last_response = response
            logger.warning(
                "HTTP request returned retryable status %d (attempt %d/%d) %s %s",
                response.status_code,
                attempt + 1,
                total_attempts,
                method,
                url,
            )

        # Either an exception or a retryable status — back off if attempts remain.
        if attempt < total_attempts - 1:
            backoff = min(
                config.backoff_base_seconds * (2**attempt),
                config.backoff_max_seconds,
            )
            jitter = random.uniform(0, backoff * 0.25)
            sleep_for = backoff + jitter
            logger.info("Retrying in %.2fs", sleep_for)
            import time

            time.sleep(sleep_for)

    # All attempts exhausted.
    if breaker is not None:
        breaker.record_failure()

    if last_response is not None:
        # Last attempt was a 5xx — return it so caller can read body / status.
        # This is intentional: 5xx isn't always "down", might be a single bad
        # row. Caller decides whether to surface as ProviderUnavailable.
        return last_response

    # All retries failed with network errors — circuit-tripping case.
    raise ProviderUnavailableError(
        f"Provider {circuit_key or url} unreachable after {total_attempts} attempts: " f"{last_exception}",
        provider=circuit_key,
        retry_after_seconds=int(config.backoff_max_seconds),
    )
