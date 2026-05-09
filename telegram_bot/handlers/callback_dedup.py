"""Dispatcher-level dedup for inline-button callbacks.

Production logs were showing a paired warning::

    handlers.base - WARNING - _edit_or_replace_callback_message:46
        Failed to edit callback message text; falling back to replace
        message: Message to edit not found
    handlers.base - WARNING - _edit_or_replace_callback_message:55
        Failed to delete callback message before fallback send:
        Message to delete not found

Root cause (verified via the cart-add flow that produced the trace): the bot
acknowledges callbacks at the *end* of each handler — see ``add_to_cart`` in
``handlers/products.py`` — so Telegram keeps the inline-button loading
spinner up for the entire handler duration (typically 1-2s of API
roundtrips). Users naturally tap the same button again. With PTB's default
``concurrent_updates=False`` the two taps queue up serially:

    Tap 1: handler runs to completion. ``_edit_or_replace_callback_message``
           falls back to ``message.delete() + reply_text()`` — the standard
           path for photo-hosted buttons (``edit_message_text`` doesn't work
           on photo messages, so navigation flows always delete-and-replace).
           Original message is now deleted; user sees a fresh quantity
           selector.

    Tap 2: handler runs again. ``query.message.message_id`` still references
           the *deleted* message from tap 1. ``edit_message_text`` raises
           ``BadRequest("Message to edit not found")``; the fallback's
           ``delete()`` raises ``BadRequest("Message to delete not found")``.
           ``reply_text`` finally succeeds — so the user gets a *second*
           quantity selector and a double-incremented cart.

This middleware sits at the dispatcher level and short-circuits tap 2 (and
all subsequent duplicates within the dedup window) before any handler runs,
so we never get to the point of trying to operate on a message we deleted.

Failure semantics:

    Redis up    → SET NX EX claims the lock atomically; cross-replica safe.
    Redis down  → in-memory fallback (single-replica only). RED-005's
                  ``report_redis_failure`` makes the degradation observable
                  via Sentry so we know we've degraded.

Trade-offs:

    The TTL is intentionally short (2s). Longer would make a deliberate
    re-tap (e.g. "I changed my mind, do it again") feel unresponsive.
    Shorter wouldn't reliably catch a slow double-tap or a Telegram
    redelivery after a network blip.

    We dedup on ``(user_id, callback_data)`` — same user, same button data
    within the window. We deliberately do *not* dedup on ``message_id``
    because the message_id can change between taps (Telegram delivers stale
    callbacks against deleted message ids); the user's *intent* lives in
    ``callback_data``.
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Final

from telegram import Update
from telegram.ext import ApplicationHandlerStop, ContextTypes

from shared.redis_failure import report_redis_failure
from shared.redis_keyspace import RedisKeyspace

logger = logging.getLogger(__name__)


# Tap-debounce window. Sized to swallow human double-taps and Telegram
# redeliveries without blocking deliberate re-taps. See module docstring for
# the trade-off discussion.
_DEDUP_TTL_SECONDS: Final[int] = 2

# Hash length for the callback_data digest used in the Redis key. 16 hex
# chars = 8 bytes of sha256 — collision probability is negligible at our
# per-user scale and keeps key length bounded regardless of payload size.
_DATA_DIGEST_LEN: Final[int] = 16

# In-memory fallback used only when Redis is unreachable. Single-replica
# only: multi-replica deploys MUST have Redis up or duplicate taps will
# leak through cross-replica. We accept that, alert via Sentry, and keep
# serving rather than fail the whole bot.
_in_memory_locks: dict[str, float] = {}


def _hash_callback_data(callback_data: str) -> str:
    return hashlib.sha256(callback_data.encode("utf-8")).hexdigest()[:_DATA_DIGEST_LEN]


def _claim_in_memory(key: str) -> bool:
    """Return True if we just claimed the lock, False if it already existed.

    Side-effect: prunes expired entries to keep the dict bounded.
    """
    now = time.monotonic()
    # Prune expired entries opportunistically (cheap, runs only on the path
    # that already needs to mutate the dict).
    expired = [k for k, expire_at in _in_memory_locks.items() if expire_at <= now]
    for k in expired:
        _in_memory_locks.pop(k, None)

    if key in _in_memory_locks:
        return False
    _in_memory_locks[key] = now + _DEDUP_TTL_SECONDS
    return True


async def callback_dedup_middleware(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Dispatcher-level guard: ack the callback, dedup, raise
    ``ApplicationHandlerStop`` on duplicates so they never reach the
    registered ``CallbackQueryHandler``s.

    Registered as a ``TypeHandler(Update, ...)`` at a group strictly *after*
    the debug ``log_all_updates`` (so ops still sees every received update,
    including duplicates we drop) and strictly *before* the conversation /
    main handler groups.
    """
    query = update.callback_query
    # Only callback queries get debounced. Pre-checkout queries, messages,
    # inline queries, etc. flow through untouched.
    if query is None or not query.data:
        return

    user = update.effective_user
    if user is None:
        return  # No user id to key the lock on — let it through.

    user_id = user.id
    data_digest = _hash_callback_data(query.data)
    key = RedisKeyspace.bot_callback_dedup(user_id, data_digest)

    # Acknowledge BEFORE the dedup check so the loading spinner dismisses
    # whether or not we're going to process this tap. Doing it after dedup
    # would leave the spinner stuck on the duplicate tap. Tolerate "Query
    # is too old" — Telegram drops callbacks older than 60s and we'd rather
    # not fail the whole dispatch on that.
    try:
        await query.answer()
    except Exception as ack_err:
        logger.debug("callback_dedup: query.answer() failed: %s", ack_err)

    token_manager = context.bot_data.get("token_manager") if context.bot_data else None
    duplicate = False
    redis_used = False

    if token_manager is not None and getattr(token_manager, "redis", None) is not None:
        try:
            # SET NX EX is atomic: returns truthy if we claimed the key,
            # None / falsy if it already existed (someone else claimed it
            # within the TTL — i.e. a duplicate within the window).
            claimed = await token_manager.redis.set(
                key, "1", nx=True, ex=_DEDUP_TTL_SECONDS
            )
            duplicate = not claimed
            redis_used = True
        except Exception as redis_err:
            # Don't fail the dispatch on Redis errors — degrade to in-memory.
            # Sentry alert via report_redis_failure makes the degradation
            # observable; tier=reliability so the dashboard shows it
            # alongside the webhook-dedup keyspace.
            report_redis_failure(
                "callback_dedup", str(redis_err), tier="reliability"
            )

    if not redis_used:
        # In-memory single-replica fallback.
        duplicate = not _claim_in_memory(key)

    if duplicate:
        logger.info(
            "Dropped duplicate callback (user=%s data=%r within %ds): root-cause "
            "fix for the 'Message to edit/delete not found' warning pair.",
            user_id, query.data, _DEDUP_TTL_SECONDS,
        )
        # ApplicationHandlerStop in PTB v20 stops dispatch across all
        # remaining groups, which is exactly what we want — no real handler
        # ever sees the duplicate.
        raise ApplicationHandlerStop()
