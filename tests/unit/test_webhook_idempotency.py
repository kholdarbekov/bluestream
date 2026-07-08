"""Unit tests for the payment webhook idempotency guard (PAY-002)."""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

import pytest

from business_app.utils.webhook_idempotency import (
    WEBHOOK_DEDUP_KEY_PREFIX,
    WEBHOOK_DEDUP_PROVISIONAL_TTL_SECONDS,
    WEBHOOK_DEDUP_TTL_SECONDS,
    WebhookIdempotencyGuard,
    extract_webhook_request_id,
)


class _FakeRedis:
    """Minimal fake implementing the subset of redis-py used by the guard."""

    def __init__(self) -> None:
        self.store: Dict[str, str] = {}
        self.ttls: Dict[str, int] = {}

    def set(self, key: str, value: str, nx: bool = False, ex: Optional[int] = None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        if ex is not None:
            self.ttls[key] = ex
        return True

    def setex(self, key: str, ttl: int, value: str):
        self.store[key] = value
        self.ttls[key] = ttl
        return True

    def get(self, key: str):
        return self.store.get(key)

    def delete(self, key: str):
        removed = 1 if key in self.store else 0
        self.store.pop(key, None)
        self.ttls.pop(key, None)
        return removed


# --- extract_webhook_request_id -------------------------------------------


def test_extract_request_id_click_uses_trans_id_and_action():
    body = {"click_trans_id": "98765", "action": "1", "amount": "100.00"}
    rid = extract_webhook_request_id("click", body, b"ignored")
    assert rid == "98765:1"


def test_extract_request_id_click_falls_back_to_trans_id_only():
    body = {"click_trans_id": "98765"}
    rid = extract_webhook_request_id("click", body, b"ignored")
    assert rid == "98765"


def test_extract_request_id_payme_uses_params_id_and_method():
    body = {"jsonrpc": "2.0", "id": 42, "method": "CreateTransaction", "params": {"id": "txn-42"}}
    rid = extract_webhook_request_id("payme", body, b"ignored")
    assert rid == "txn-42:CreateTransaction"


def test_extract_request_id_payme_falls_back_to_rpc_envelope_id():
    body = {"jsonrpc": "2.0", "id": 77, "method": "CheckPerformTransaction", "params": {}}
    rid = extract_webhook_request_id("payme", body, b"ignored")
    assert rid == "rpc:77"


def test_extract_request_id_hashes_body_when_no_provider_id():
    rid = extract_webhook_request_id("click", {}, b"arbitrary-body")
    assert len(rid) == 32
    assert rid == extract_webhook_request_id("click", {}, b"arbitrary-body")
    assert rid != extract_webhook_request_id("click", {}, b"other-body")


# --- WebhookIdempotencyGuard.check ----------------------------------------


def test_first_hit_claims_and_is_not_duplicate():
    guard = WebhookIdempotencyGuard(_FakeRedis())

    verdict = guard.check("click", "txn-1")

    assert verdict.is_duplicate is False
    assert verdict.request_id == "txn-1"
    assert verdict.cached_response is None


def test_second_hit_is_duplicate():
    redis = _FakeRedis()
    guard = WebhookIdempotencyGuard(redis)

    first = guard.check("click", "txn-1")
    second = guard.check("click", "txn-1")

    assert first.is_duplicate is False
    assert second.is_duplicate is True


def test_claim_ttl_is_provisional_window():
    # Two-phase claim (crash-window fix, see webhook_idempotency.py): check()
    # only takes a SHORT provisional claim now; it is promoted to the full
    # WEBHOOK_DEDUP_TTL_SECONDS window in store_response() instead. Updated
    # per Task 1 brief guidance (this test previously asserted the full 24h
    # TTL directly out of check()).
    redis = _FakeRedis()
    guard = WebhookIdempotencyGuard(redis)

    guard.check("payme", "txn-xyz")

    key = f"{WEBHOOK_DEDUP_KEY_PREFIX}:payme:txn-xyz"
    assert redis.store[key] == "1"
    assert redis.ttls[key] == WEBHOOK_DEDUP_PROVISIONAL_TTL_SECONDS


def test_duplicate_returns_cached_response_when_present():
    redis = _FakeRedis()
    guard = WebhookIdempotencyGuard(redis)

    guard.check("payme", "txn-abc")
    guard.store_response("payme", "txn-abc", {"result": {"allow": True}})

    verdict = guard.check("payme", "txn-abc")

    assert verdict.is_duplicate is True
    assert verdict.cached_response == {"result": {"allow": True}}


def test_different_providers_use_different_keyspaces():
    redis = _FakeRedis()
    guard = WebhookIdempotencyGuard(redis)

    guard.check("click", "same-id")
    verdict = guard.check("payme", "same-id")

    assert verdict.is_duplicate is False


def test_redis_failure_fails_open():
    class BoomRedis(_FakeRedis):
        def set(self, *a, **kw):
            raise RuntimeError("redis down")

    guard = WebhookIdempotencyGuard(BoomRedis())

    verdict = guard.check("click", "txn-1")

    # Availability wins over strict dedup when Redis is unreachable.
    assert verdict.is_duplicate is False


def test_release_allows_reprocessing_after_error():
    redis = _FakeRedis()
    guard = WebhookIdempotencyGuard(redis)

    guard.check("click", "txn-1")
    guard.release("click", "txn-1")
    verdict = guard.check("click", "txn-1")

    assert verdict.is_duplicate is False


# --- store_response -------------------------------------------------------


def test_store_response_serializes_payload_and_sets_ttl():
    redis = _FakeRedis()
    guard = WebhookIdempotencyGuard(redis)

    guard.store_response("click", "txn-1", {"error": 0, "error_note": "ok"})

    key = f"{WEBHOOK_DEDUP_KEY_PREFIX}:click:txn-1:response"
    assert json.loads(redis.store[key]) == {"error": 0, "error_note": "ok"}
    assert redis.ttls[key] == WEBHOOK_DEDUP_TTL_SECONDS


def test_store_response_ignores_none():
    redis = _FakeRedis()
    guard = WebhookIdempotencyGuard(redis)

    guard.store_response("click", "txn-1", None)

    assert redis.store == {}


def test_store_response_tolerates_unserializable_payload():
    redis = _FakeRedis()
    guard = WebhookIdempotencyGuard(redis)

    # Weird objects must not crash the webhook path; the guard falls back to str().
    guard.store_response("click", "txn-1", {"bad": object()})

    key = f"{WEBHOOK_DEDUP_KEY_PREFIX}:click:txn-1:response"
    assert key in redis.store  # stored as a best-effort string


# --- Guard with no Redis (None) -------------------------------------------


def test_guard_without_redis_is_no_op():
    guard = WebhookIdempotencyGuard(None)

    verdict = guard.check("click", "txn-1")
    guard.store_response("click", "txn-1", {"x": 1})
    guard.release("click", "txn-1")

    assert verdict.is_duplicate is False
