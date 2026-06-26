"""
Per-driver flow-state mirror + deferred pool-suggestion queue.

Why this module exists
----------------------
The bot tracks "this user is currently waiting to type something" via flags
on `context.user_data` (`pending_delivery_cash_flow`, `pending_*_flow`,
`tryout_pickup_task_id`).  Those flags live inside PTB's per-user context —
visible to handlers, invisible to the webhook server even though both run
in the same process.

Without coordination, an asynchronous webhook (`/internal/pool-insertion-suggestion`,
`/internal/route-updated`) can land an Accept-style keyboard mid-cash-collection,
the driver taps it, and the previous `pending_delivery_cash_flow` is left
orphaned — the next text they type is parsed as cash for the wrong order.

This module solves it by mirroring the *fact that a flow is active* into Redis
under `RedisKeyspace.staff_bot_active_flow(telegram_id)`.  The webhook handler
reads that key before sending; if a flow is active, the suggestion is pushed
to a per-driver queue (`staff_bot_pool_suggestion_queue`) instead.  When the
flow clears, the bot's flow-cancel/complete paths drain that queue and dispatch
the deferred suggestions.

Failure semantics
-----------------
The mirror is best-effort.  A Redis outage degrades to "no flow active" — the
webhook proceeds as it did before this module existed, which is no worse than
the prior behaviour.  All failures route through `report_redis_failure(...,
tier="cache")` so ops sees sustained outages without the bot itself crashing.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

import redis.asyncio as redis_async

from shared.redis_failure import report_redis_failure
from shared.redis_keyspace import RedisKeyspace

logger = logging.getLogger(__name__)

# 30 min: longer than any sane delivery-completion text input, short enough
# that a forgotten / crashed flow doesn't shadow real notifications forever.
ACTIVE_FLOW_TTL_SECONDS = 30 * 60
# 15 min: pool composition shifts as drivers accept/decline; older suggestions
# point at deliveries someone else may already own.
QUEUE_TTL_SECONDS = 15 * 60
# Cap the queue so a chronic mid-flow driver doesn't accumulate dozens of
# stale suggestions.  The newest 5 are kept (LPUSH + LTRIM 0..4).
QUEUE_MAX_LENGTH = 5

_redis: Optional[redis_async.Redis] = None

# SSOT for the `context.user_data` keys that, while present, make the bot's
# catch-all text router treat the next text update as flow input. Consumed by
# `StaffBot._clear_all_pending_flows`, `_handle_flow_cancel`, and every
# navigation landing handler (main menu / cash hub) so leaving a flow — by any
# route — never strands a flag that would mis-route the next text.
PENDING_FLOW_USER_DATA_KEYS = (
    'pending_delivery_cash_flow',
    'pending_reconciliation_flow',
    'pending_cod_collection_flow',
    'pending_bottle_collection_flow',
    'tryout_pickup_task_id',
    'tryout_pickup_products',
    'tryout_pickup_state',
)


async def clear_pending_flows(context, update=None) -> None:
    """Drop every in-memory flow flag AND the Redis mirror, draining any deferred
    pool-insertion suggestions.

    Duck-typed on PTB's `context`/`update` to stay import-light. Best-effort:
    safe to call when Redis is unconfigured (the drain degrades to a no-op).
    """
    user_data = getattr(context, 'user_data', None)
    if user_data is None:
        return
    for key in PENDING_FLOW_USER_DATA_KEYS:
        user_data.pop(key, None)
    effective_user = getattr(update, 'effective_user', None) if update is not None else None
    if effective_user is not None:
        language = user_data.get('language')
        await clear_and_drain(effective_user.id, getattr(context, 'bot', None), language=language)


def configure(redis_client: Optional[redis_async.Redis]) -> None:
    """Install the Redis client used by this module.

    Called once at bot startup with a connected `redis.asyncio.Redis` instance
    (or `None` to disable mirroring entirely — the latter is useful for tests
    and for `DISABLE_TOKEN_CACHE`-style runs).
    """
    global _redis
    _redis = redis_client


def is_enabled() -> bool:
    return _redis is not None


async def mark_active(telegram_id: int, flow_name: str) -> None:
    """Mirror the fact that `telegram_id` is now in `flow_name`.

    Safe to call repeatedly — overwrites the existing marker and refreshes
    the TTL.  Failures are logged + reported but never raised.
    """
    if _redis is None:
        return
    try:
        key = RedisKeyspace.staff_bot_active_flow(int(telegram_id))
        await _redis.set(key, flow_name, ex=ACTIVE_FLOW_TTL_SECONDS)
    except Exception as exc:
        report_redis_failure("staff_bot.flow_state.mark_active", str(exc), tier="cache")


async def clear_active(telegram_id: int) -> None:
    """Remove the active-flow marker. Idempotent."""
    if _redis is None:
        return
    try:
        key = RedisKeyspace.staff_bot_active_flow(int(telegram_id))
        await _redis.delete(key)
    except Exception as exc:
        report_redis_failure("staff_bot.flow_state.clear_active", str(exc), tier="cache")


async def get_active_flow(telegram_id: int) -> Optional[str]:
    """Read the active-flow marker.

    Returns the flow name if set, `None` if absent or on Redis failure
    (failure-mode is "proceed as not-in-flow" — see module docstring).
    """
    if _redis is None:
        return None
    try:
        key = RedisKeyspace.staff_bot_active_flow(int(telegram_id))
        return await _redis.get(key)
    except Exception as exc:
        report_redis_failure("staff_bot.flow_state.get_active_flow", str(exc), tier="cache")
        return None


async def queue_pool_suggestion(telegram_id: int, payload: Dict[str, Any]) -> bool:
    """Defer a pool-insertion suggestion until the user's next idle moment.

    Uses LPUSH + LTRIM so the newest N suggestions survive a backlog; older
    ones drop on the floor (they're stale by the time the user is free).

    Returns True if queued, False if Redis is unavailable.
    """
    if _redis is None:
        return False
    try:
        key = RedisKeyspace.staff_bot_pool_suggestion_queue(int(telegram_id))
        serialized = json.dumps(payload, separators=(",", ":"), default=str)
        async with _redis.pipeline(transaction=False) as pipe:
            pipe.lpush(key, serialized)
            pipe.ltrim(key, 0, QUEUE_MAX_LENGTH - 1)
            pipe.expire(key, QUEUE_TTL_SECONDS)
            await pipe.execute()
        return True
    except Exception as exc:
        report_redis_failure(
            "staff_bot.flow_state.queue_pool_suggestion", str(exc), tier="cache"
        )
        return False


async def drain_pool_suggestions(telegram_id: int) -> List[Dict[str, Any]]:
    """Pop and return every queued suggestion for this user.

    Atomic via a pipeline (`LRANGE 0 -1` + `DEL`). Returns oldest-to-newest
    so the caller can dispatch them in arrival order.
    """
    if _redis is None:
        return []
    key = RedisKeyspace.staff_bot_pool_suggestion_queue(int(telegram_id))
    try:
        async with _redis.pipeline(transaction=True) as pipe:
            pipe.lrange(key, 0, -1)
            pipe.delete(key)
            results = await pipe.execute()
    except Exception as exc:
        report_redis_failure(
            "staff_bot.flow_state.drain_pool_suggestions", str(exc), tier="cache"
        )
        return []

    raw_items = results[0] or []
    suggestions: List[Dict[str, Any]] = []
    # LRANGE returns newest-first because we LPUSH; reverse for chronological.
    for raw in reversed(raw_items):
        try:
            suggestions.append(json.loads(raw))
        except Exception:
            logger.debug("Skipping malformed queued suggestion: %r", raw)
    return suggestions


async def clear_and_drain(telegram_id: int, bot, language: Optional[str] = None) -> None:
    """Convenience: drop the active-flow marker AND deliver any deferred
    pool-insertion suggestions through `bot.send_message`.

    Called from every flow-exit point (success, cancel, error). `bot` is the
    `telegram.Bot` instance from `context.bot` or `Application.bot` —
    passing it here keeps this module dependency-free of PTB types.

    Renders the same Accept/Cancel keyboard the live webhook would send.
    Lazy-imports telegram to keep test suites that mock the bot light.
    """
    await clear_active(telegram_id)
    payloads = await drain_pool_suggestions(telegram_id)
    if not payloads or bot is None:
        return

    # Lazy import: keeps `flow_state` importable in test environments that
    # don't have python-telegram-bot installed.
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    from staff_bot.i18n import i18n

    if language is None:
        try:
            language = await i18n.get_user_language(int(telegram_id))
        except Exception:
            language = 'en'

    for payload in payloads:
        delivery_id = payload.get('delivery_id')
        if not delivery_id:
            continue
        try:
            text = i18n.get(
                'staff.delivery.pool_insertion_offer',
                language,
                order_no=payload.get('order_no', ''),
                km=f"{float(payload.get('detour_km', 0) or 0):.1f}",
                minutes=int(round(float(payload.get('detour_minutes', 0) or 0))),
            )
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    f"✅ {i18n.get('staff.delivery.accept', language)}",
                    callback_data=f"staff_confirm_accept_{int(delivery_id)}",
                ),
                InlineKeyboardButton(
                    f"❌ {i18n.get('staff.cancel', language)}",
                    callback_data=f"staff_decline_suggestion_{int(delivery_id)}",
                ),
            ]])
            await bot.send_message(
                chat_id=int(telegram_id), text=text, reply_markup=keyboard,
            )
        except Exception as exc:
            logger.warning(
                f"Failed to deliver deferred pool suggestion "
                f"to {telegram_id} (delivery={delivery_id}): {exc}"
            )
