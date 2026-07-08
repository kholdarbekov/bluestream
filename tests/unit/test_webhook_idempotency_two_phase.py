"""Two-phase claim semantics for the webhook idempotency guard (crash-window fix)."""

import json

from business_app.utils.webhook_idempotency import (
    WEBHOOK_DEDUP_PROVISIONAL_TTL_SECONDS,
    WEBHOOK_DEDUP_TTL_SECONDS,
    WebhookIdempotencyGuard,
)


class FakeRedis:
    """Minimal redis stand-in recording set/setex calls with their TTLs."""

    def __init__(self):
        self.store = {}
        self.ttls = {}
        self.set_calls = []

    def set(self, key, value, nx=False, ex=None):
        self.set_calls.append({"key": key, "value": value, "nx": nx, "ex": ex})
        if nx and key in self.store:
            return None
        self.store[key] = value
        if ex is not None:
            self.ttls[key] = ex
        return True

    def setex(self, key, ttl, value):
        self.store[key] = value
        self.ttls[key] = ttl
        return True

    def get(self, key):
        return self.store.get(key)

    def delete(self, key):
        self.store.pop(key, None)
        self.ttls.pop(key, None)


def test_check_claims_with_provisional_ttl():
    redis = FakeRedis()
    guard = WebhookIdempotencyGuard(redis)
    verdict = guard.check("click", "12345:1")
    assert verdict.is_duplicate is False
    claim_key = "bs:webhook:dedup:click:12345:1"
    assert redis.ttls[claim_key] == WEBHOOK_DEDUP_PROVISIONAL_TTL_SECONDS
    assert WEBHOOK_DEDUP_PROVISIONAL_TTL_SECONDS == 90


def test_store_response_promotes_claim_to_full_ttl():
    redis = FakeRedis()
    guard = WebhookIdempotencyGuard(redis)
    guard.check("click", "12345:1")
    guard.store_response("click", "12345:1", {"error": 0, "error_note": "Success"})
    claim_key = "bs:webhook:dedup:click:12345:1"
    response_key = claim_key + ":response"
    assert redis.ttls[claim_key] == WEBHOOK_DEDUP_TTL_SECONDS
    assert redis.ttls[response_key] == WEBHOOK_DEDUP_TTL_SECONDS
    assert json.loads(redis.store[response_key]) == {"error": 0, "error_note": "Success"}


def test_store_response_unserializable_leaves_provisional_claim():
    redis = FakeRedis()
    guard = WebhookIdempotencyGuard(redis)
    guard.check("click", "12345:1")
    # A plain object() is NOT sufficient here: store_response serializes with
    # json.dumps(..., default=str), which stringifies arbitrary objects (e.g.
    # `object()` -> "<object object at 0x...>") and therefore succeeds. Only a
    # genuinely unserializable payload (e.g. a circular reference, which even
    # `default=str` cannot rescue -- json raises ValueError("Circular reference
    # detected") before ever calling `default`) exercises the except branch.
    # See task-1-report.md for why this differs from the brief's literal
    # `{"bad": object()}` payload.
    circular = {}
    circular["self"] = circular
    guard.store_response("click", "12345:1", circular)
    claim_key = "bs:webhook:dedup:click:12345:1"
    # Claim NOT promoted: a retry after provisional expiry must reprocess.
    assert redis.ttls[claim_key] == WEBHOOK_DEDUP_PROVISIONAL_TTL_SECONDS
    assert claim_key + ":response" not in redis.store


def test_custom_provisional_ttl():
    redis = FakeRedis()
    guard = WebhookIdempotencyGuard(redis, provisional_ttl_seconds=45)
    guard.check("click", "999:1")
    assert redis.ttls["bs:webhook:dedup:click:999:1"] == 45


def test_duplicate_without_cached_response_reports_no_cache():
    redis = FakeRedis()
    guard = WebhookIdempotencyGuard(redis)
    guard.check("click", "777:1")
    verdict = guard.check("click", "777:1")
    assert verdict.is_duplicate is True
    assert verdict.cached_response is None
