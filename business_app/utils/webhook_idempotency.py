"""
Webhook idempotency guard (PAY-002).

Provides endpoint-level deduplication for payment provider webhooks keyed on
identifiers the gateways already include in the payload body:

- Click: ``click_trans_id`` (+ ``action`` when present) from the POST form body.
- Payme: JSON-RPC ``params.id`` from the JSON body.
- Generic fallback: ``sha256(raw_body)[:32]`` when no provider id is present.

The guard is the primary webhook replay/duplicate defense now that
[PAY-001] is deferred (gateways do not ship ``X-Nonce``/``X-Timestamp``).

Design
------
The guard keeps two Redis entries per request id, both under the same TTL:

- ``<prefix>:<provider>:<request_id>``        — claim marker (SET NX)
- ``<prefix>:<provider>:<request_id>:response`` — cached response (optional)

When a second webhook arrives with the same request id we return the cached
response so that synchronous protocols (Payme JSON-RPC, Click Prepare/Complete)
continue to see the exact response the gateway expects. For asynchronous paths
that do not produce a response body (generic Celery enqueue) the duplicate is
simply swallowed with an empty ``200`` — the gateway will stop retrying.

A failed Redis lookup does not block webhook processing; the guard logs and
falls through. Failing closed on Redis outage would create an availability
cliff for payment callbacks, which the audit explicitly warns against.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from flask import current_app


WEBHOOK_DEDUP_KEY_PREFIX = "bs:webhook:dedup"
WEBHOOK_DEDUP_TTL_SECONDS = 24 * 3600  # 24h comfortably outlasts gateway retry windows

# Two-phase claim (crash-window fix): check() takes only a SHORT provisional
# claim; store_response() promotes it to the full TTL together with caching
# the response. A hard crash mid-processing therefore leaves a claim that
# expires within seconds — the gateway's retry then reprocesses (safe: the
# Complete handler is idempotent under its row lock + status guards) instead
# of being answered from a claim whose work never committed.
WEBHOOK_DEDUP_PROVISIONAL_TTL_SECONDS = 90


@dataclass(frozen=True)
class IdempotencyVerdict:
    """Outcome of an idempotency check for a single webhook hit."""

    is_duplicate: bool
    request_id: str
    cached_response: Optional[dict] = None


def _extract_click_request_id(body: Mapping[str, Any]) -> Optional[str]:
    trans_id = body.get("click_trans_id") or body.get("clickTransId")
    if trans_id in (None, ""):
        return None
    action = body.get("action")
    if action in (None, ""):
        return str(trans_id)
    return f"{trans_id}:{action}"


def _extract_payme_request_id(body: Mapping[str, Any]) -> Optional[str]:
    params = body.get("params")
    method = body.get("method")
    if isinstance(params, dict):
        txn_id = params.get("id")
        if txn_id not in (None, ""):
            if method:
                return f"{txn_id}:{method}"
            return str(txn_id)
    # Payme wraps all calls in JSON-RPC; the envelope id is a weaker fallback.
    rpc_id = body.get("id")
    if rpc_id not in (None, ""):
        return f"rpc:{rpc_id}"
    return None


def _fallback_request_id(raw_body: bytes) -> str:
    return hashlib.sha256(raw_body or b"").hexdigest()[:32]


def extract_webhook_request_id(provider: str, body: Any, raw_body: bytes) -> str:
    """Return a stable dedup id for the webhook request.

    Uses gateway-supplied identifiers when present and falls back to a body
    hash so we never allow the dedup key to collapse across different payloads.
    """
    provider_lc = (provider or "").lower()
    parsed: Optional[Mapping[str, Any]] = body if isinstance(body, Mapping) else None

    request_id: Optional[str] = None
    if parsed is not None:
        if provider_lc == "click":
            request_id = _extract_click_request_id(parsed)
        elif provider_lc == "payme":
            request_id = _extract_payme_request_id(parsed)
    if request_id:
        return request_id
    return _fallback_request_id(raw_body)


class WebhookIdempotencyGuard:
    """Redis-backed idempotency for payment webhooks.

    ``redis_client`` is required; ``logger`` is optional and defaults to the
    Flask app logger at call time. Response caching is opt-in per call so async
    paths that only enqueue a task do not pay the serialization cost.
    """

    def __init__(
        self,
        redis_client,
        *,
        key_prefix: str = WEBHOOK_DEDUP_KEY_PREFIX,
        ttl_seconds: int = WEBHOOK_DEDUP_TTL_SECONDS,
        provisional_ttl_seconds: Optional[int] = None,
    ) -> None:
        self._redis = redis_client
        self._prefix = key_prefix
        self._ttl = ttl_seconds
        self._provisional_ttl = int(provisional_ttl_seconds or WEBHOOK_DEDUP_PROVISIONAL_TTL_SECONDS)

    # ---- keys -----------------------------------------------------------------

    def _claim_key(self, provider: str, request_id: str) -> str:
        return f"{self._prefix}:{provider.lower()}:{request_id}"

    def _response_key(self, provider: str, request_id: str) -> str:
        return f"{self._prefix}:{provider.lower()}:{request_id}:response"

    # ---- public API -----------------------------------------------------------

    def check(self, provider: str, request_id: str) -> IdempotencyVerdict:
        """Atomically claim the request id. Returns whether this is a duplicate."""
        if self._redis is None:
            return IdempotencyVerdict(is_duplicate=False, request_id=request_id)
        claim_key = self._claim_key(provider, request_id)
        try:
            claimed = self._redis.set(claim_key, "1", nx=True, ex=self._provisional_ttl)
        except Exception as exc:  # pragma: no cover — availability over correctness
            self._log().warning(f"Webhook idempotency check failed (provider={provider}, err={exc})")
            return IdempotencyVerdict(is_duplicate=False, request_id=request_id)

        if claimed:
            return IdempotencyVerdict(is_duplicate=False, request_id=request_id)

        cached = self._load_response(provider, request_id)
        return IdempotencyVerdict(
            is_duplicate=True,
            request_id=request_id,
            cached_response=cached,
        )

    def store_response(self, provider: str, request_id: str, response: Any) -> None:
        """Cache a response body AND promote the claim to the full dedup TTL.

        Promotion happens only together with a successful cache write: a
        long-lived claim with no cached response would starve gateway retries
        (they would see duplicate-without-cache until the claim expired).
        """
        if self._redis is None or response is None:
            return
        try:
            payload = json.dumps(response, default=str)
        except (TypeError, ValueError):
            # Non-serializable response — leave the provisional claim so the
            # gateway's retry reprocesses after it expires.
            return
        try:
            self._redis.setex(self._response_key(provider, request_id), self._ttl, payload)
            self._redis.set(self._claim_key(provider, request_id), "1", ex=self._ttl)
        except Exception as exc:  # pragma: no cover
            self._log().warning(f"Webhook response cache store failed (provider={provider}, err={exc})")

    def release(self, provider: str, request_id: str) -> None:
        """Remove the dedup claim so a retry can re-run.

        Call this when the handler raised before producing a durable side effect.
        """
        if self._redis is None:
            return
        try:
            self._redis.delete(self._claim_key(provider, request_id))
        except Exception as exc:  # pragma: no cover
            self._log().warning(f"Webhook idempotency release failed (provider={provider}, err={exc})")

    # ---- helpers --------------------------------------------------------------

    def _load_response(self, provider: str, request_id: str) -> Optional[dict]:
        try:
            raw = self._redis.get(self._response_key(provider, request_id))
        except Exception:
            return None
        if not raw:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="ignore")
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else None

    def _log(self):
        try:
            return current_app.logger
        except RuntimeError:
            import logging

            return logging.getLogger(__name__)


def build_default_guard(redis_client) -> WebhookIdempotencyGuard:
    """Factory that wires the guard to module defaults + Flask config overrides."""
    provisional = None
    try:
        provisional = current_app.config.get("WEBHOOK_CLAIM_PROVISIONAL_TTL_SECONDS")
    except RuntimeError:
        provisional = None
    return WebhookIdempotencyGuard(redis_client, provisional_ttl_seconds=provisional)


__all__ = [
    "IdempotencyVerdict",
    "WebhookIdempotencyGuard",
    "WEBHOOK_DEDUP_KEY_PREFIX",
    "WEBHOOK_DEDUP_TTL_SECONDS",
    "WEBHOOK_DEDUP_PROVISIONAL_TTL_SECONDS",
    "build_default_guard",
    "extract_webhook_request_id",
]
