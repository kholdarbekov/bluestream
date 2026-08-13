"""
Centralized Redis key namespace for BlueStream services.

Rationale (audit RED-001):
    Before this module, every service built Redis keys via ad-hoc f-strings
    (`f"bot:tokens:{telegram_id}"`, `f"rate:otp:{user_id}"`, ...). Prefixes
    drifted between `bot:`, `rate:`, `bs:`, and there was no way to enumerate
    which components shared a namespace or to migrate keys safely.

Rules:
    1. All Redis keys produced anywhere in this codebase SHOULD be built
       through a `RedisKeyspace` static method. No raw f-strings for keys
       outside this module (enforced by code review; see also lint note at
       the bottom of this file).
    2. Key *format* is considered a production contract. Do NOT rename an
       existing method's return value without a documented migration plan
       (dual-read/write or SCAN+RENAME) — doing so silently invalidates
       every outstanding rate-limit window, dedup mark, and cached token.
    3. New keys SHOULD be registered here before first use.

Usage tiers (`RedisUsageTier`) tag each keyspace with its failure semantics
so callers can decide *how* to react when Redis is unreachable:

    TIER_SECURITY      — rate limits, OTP windows, replay guards.
                         Fail closed. Deny the action. (See BOT-005.)
    TIER_RELIABILITY   — webhook dedup, idempotency keys, distributed locks.
                         Fail closed; retry when practical. Duplicating an
                         operation is worse than rejecting it.
    TIER_CACHE         — bot token cache, response caches, computed snapshots.
                         Fall through to source-of-truth with a metric.
                         Degraded performance is acceptable; correctness is not.

    TIER_RESERVATION   — inventory holds. Effectively tier-reliability but
                         called out so ops know stock state lives here.
"""
from __future__ import annotations

from enum import Enum


class RedisUsageTier(Enum):
    TIER_SECURITY = 'security'
    TIER_RELIABILITY = 'reliability'
    TIER_CACHE = 'cache'
    TIER_RESERVATION = 'reservation'


class RedisKeyspace:
    """Single source of truth for Redis key construction.

    Methods return the *current production* key format. Callers should never
    concatenate their own prefix onto the return value.
    """

    # ---- Telegram customer bot -------------------------------------------

    # Tier: CACHE. Token refresh is expensive but tolerable to re-do if the
    # cache misses — we can always hit the backend /auth endpoint.
    @staticmethod
    def bot_token_cache(telegram_id: int) -> str:
        return f"bot:tokens:{telegram_id}"

    # Tier: RELIABILITY. Held during token refresh to prevent stampede.
    @staticmethod
    def bot_refresh_lock(telegram_id: int) -> str:
        return f"bot:refresh_lock:{telegram_id}"

    # Tier: SECURITY. Generic per-user bot rate limit (sliding window).
    @staticmethod
    def bot_rate_limit(user_id: int) -> str:
        return f"rate:bot:{user_id}"

    # Tier: SECURITY. OTP issuance rate limit.
    @staticmethod
    def bot_otp_rate_limit(user_id: int) -> str:
        return f"rate:otp:{user_id}"

    # Tier: RELIABILITY. De-dup marker for backend→bot payment notifications,
    # keyed by order id (see webhook_server + handlers/payments).
    @staticmethod
    def bot_payment_message(order_id: int | str) -> str:
        return f"bot:payment_msg:{order_id}"

    # Tier: RELIABILITY. Dedup marker for inbound /internal/* webhooks from
    # backend → customer bot. Keyed by `(endpoint, request_id)` so the same
    # request_id replayed against a different endpoint doesn't false-positive.
    # 24h TTL: cross-replica safe, survives bot restarts, matches PAY-002 SLA
    # for backend retry storms (gateway-side already capped at <24h).
    @staticmethod
    def bot_webhook_dedup(endpoint: str, request_id: str) -> str:
        return f"bot:webhook_dedup:{endpoint}:{request_id}"

    # Tier: RELIABILITY. Short-lived dedup lock for inline-button taps from a
    # user. Without this lock, double-taps (or Telegram redelivering the same
    # callback after a network glitch) caused our handlers to run twice
    # against the same `query.message.message_id`: the first run delete-and-
    # replaced the source message (the standard navigation pattern for photo-
    # hosted buttons), and the second run then tried to edit/delete a message
    # that no longer existed — producing the "Message to edit not found" /
    # "Message to delete not found" warning pair seen in production logs.
    # `data_digest` is a short (16-hex-char) sha256 of the raw callback_data,
    # so the key length is bounded and safe regardless of callback payload.
    # TTL is intentionally short (~2s) — long enough to swallow a double-tap
    # or a Telegram redelivery, short enough that a deliberate second tap
    # (e.g. "go back, do it again") still works.
    @staticmethod
    def bot_callback_dedup(user_id: int, data_digest: str) -> str:
        return f"bot:callback_dedup:{user_id}:{data_digest}"

    # ---- Staff bot -------------------------------------------------------

    # Tier: CACHE.
    @staticmethod
    def staff_bot_token_cache(telegram_id: int) -> str:
        return f"staff_bot:tokens:{telegram_id}"

    # Tier: RELIABILITY. Token-refresh stampede lock.
    @staticmethod
    def staff_bot_refresh_lock(telegram_id: int) -> str:
        return f"staff_bot:refresh_lock:{telegram_id}"

    # Tier: RELIABILITY. Webhook event dedup marker (`NX` on `set`).
    @staticmethod
    def staff_bot_webhook_event(event_id: str) -> str:
        return f"staff_bot:webhook_events:{event_id}"

    # Tier: SECURITY. One-time staff invite token (getdel). Token is the
    # secret — keep the key path opaque outside this module.
    @staticmethod
    def staff_bot_invite(invite_token: str) -> str:
        return f"staff_bot:invite:{invite_token}"

    # Tier: CACHE. Mirrors the in-process `pending_*_flow` flag for a driver
    # so the webhook server (running in the same process but outside the PTB
    # update context) can answer "is this user mid-flow?" without poking
    # `Application.user_data`. Value is the active flow name; absence means
    # "not in a text-input flow". TTL is a 30-min upper bound — any flow
    # taking longer than that is almost certainly abandoned, so letting the
    # mirror expire is safer than trusting a stale lock.
    @staticmethod
    def staff_bot_active_flow(telegram_id: int) -> str:
        return f"staff_bot:active_flow:{telegram_id}"

    # Tier: CACHE. Per-driver queue of pool-insertion suggestions deferred
    # while they were mid-flow. The drainer pops from this list when the
    # flow clears (or on the user's next non-flow callback). 15-min TTL
    # because pool composition shifts fast — older suggestions are stale.
    @staticmethod
    def staff_bot_pool_suggestion_queue(telegram_id: int) -> str:
        return f"staff_bot:pool_suggestion_queue:{telegram_id}"

    # Tier: CACHE. The staff bot's per-driver route-card state (card message
    # id, shift date, view, alert throttle). Shared between the PTB handlers
    # and the webhook server; survives bot restarts so the same card message
    # keeps being edited. 48h TTL == Telegram's deleteMessage window.
    @staticmethod
    def staff_bot_route_card(telegram_id: int) -> str:
        return f"staff_bot:route_card:{telegram_id}"

    # ---- Backend (business_app) ------------------------------------------

    # Tier: SECURITY. Per-provider webhook replay guard.
    @staticmethod
    def webhook_provider_rate(provider: str) -> str:
        return f"bs:webhook:provider_rate:{provider.lower()}"

    # Tier: SECURITY. Payment-webhook nonce replay guard (signature verifier).
    @staticmethod
    def webhook_replay_nonce(provider: str, nonce: str) -> str:
        return f"webhook_nonce:{provider.lower()}:{nonce}"

    # Tier: RESERVATION. Inventory hold (`setex` + TTL).
    @staticmethod
    def inventory_reservation(order_id: int | str, product_id: int | str) -> str:
        return f"inventory_reservation:{order_id}:{product_id}"

    # Tier: RESERVATION. Details hash for an active reservation.
    @staticmethod
    def reservation_details(order_id: int | str, product_id: int | str) -> str:
        return f"reservation_details:{order_id}:{product_id}"

    # ---- Scan patterns ---------------------------------------------------
    # SCAN patterns use `*` wildcards. Callers should prefer these helpers
    # over inlining `f"{prefix}:*"` so the wildcard shape stays in sync
    # with the writer above.

    @staticmethod
    def inventory_reservation_pattern(order_id: int | str) -> str:
        return f"inventory_reservation:{order_id}:*"

    @staticmethod
    def reservation_details_pattern(order_id: int | str) -> str:
        return f"reservation_details:{order_id}:*"

    @staticmethod
    def inventory_reservation_by_product_pattern(product_id: int | str) -> str:
        return f"inventory_reservation:*:{product_id}"

    @staticmethod
    def all_reservation_details_pattern() -> str:
        return "reservation_details:*"


# Mapping used by ops tooling / dashboards to group keys by failure-mode tier.
# Kept adjacent to the methods above so a new keyspace without a tier entry is
# obvious in code review.
KEYSPACE_TIERS: dict[str, RedisUsageTier] = {
    'bot_token_cache':                   RedisUsageTier.TIER_CACHE,
    'bot_refresh_lock':                  RedisUsageTier.TIER_RELIABILITY,
    'bot_rate_limit':                    RedisUsageTier.TIER_SECURITY,
    'bot_otp_rate_limit':                RedisUsageTier.TIER_SECURITY,
    'bot_payment_message':               RedisUsageTier.TIER_RELIABILITY,
    'bot_webhook_dedup':                 RedisUsageTier.TIER_RELIABILITY,
    'bot_callback_dedup':                RedisUsageTier.TIER_RELIABILITY,
    'staff_bot_token_cache':             RedisUsageTier.TIER_CACHE,
    'staff_bot_refresh_lock':            RedisUsageTier.TIER_RELIABILITY,
    'staff_bot_webhook_event':           RedisUsageTier.TIER_RELIABILITY,
    'staff_bot_invite':                  RedisUsageTier.TIER_SECURITY,
    'staff_bot_active_flow':             RedisUsageTier.TIER_CACHE,
    'staff_bot_pool_suggestion_queue':   RedisUsageTier.TIER_CACHE,
    'staff_bot_route_card':              RedisUsageTier.TIER_CACHE,
    'webhook_provider_rate':             RedisUsageTier.TIER_SECURITY,
    'webhook_replay_nonce':              RedisUsageTier.TIER_SECURITY,
    'inventory_reservation':             RedisUsageTier.TIER_RESERVATION,
    'reservation_details':               RedisUsageTier.TIER_RESERVATION,
}


# ---- Lint / convention --------------------------------------------------
# A ripgrep-based CI check enforces rule (1) above. The intended invocation
# (see docs/audit/05-caching-and-redis.md RED-001 closure notes):
#
#     rg -n --glob '!shared/redis_keyspace.py' \
#        --glob '!docs/**' --glob '!tests/**' \
#        -e 'f"(bot:|rate:|otp:|bs:|inventory_reservation:|reservation_details:|staff_bot:|webhook_nonce:)' \
#        -e "f'(bot:|rate:|otp:|bs:|inventory_reservation:|reservation_details:|staff_bot:|webhook_nonce:)"
#
# Any match in a non-excluded path is a violation: the caller should use a
# RedisKeyspace method instead. We exclude tests because test fixtures
# sometimes pre-populate fake-redis with exact production key shapes (pinning
# them via the same module is a future nice-to-have but not required today).
