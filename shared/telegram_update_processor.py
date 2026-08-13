"""Update processor: concurrent ACROSS chats, strictly ordered WITHIN a chat.

Why this exists
---------------
python-telegram-bot processes updates one at a time by default
(``concurrent_updates`` unset). That means a single slow handler blocks
*everyone*: on 2026-08-13 several drivers were active in overlapping windows
and two of them gave up and re-sent ``/start`` while another staff member's
update was still being handled. The same mechanism is already documented in
``telegram_bot/handlers/callback_dedup.py``, where serial queuing turns an
impatient double-tap into a duplicate message.

The obvious fix — ``concurrent_updates(True)`` — carries a warning from PTB
itself:

    Processing updates concurrently is not recommended when stateful handlers
    like ConversationHandler are used.

Both of our bots lean heavily on ``ConversationHandler`` (bottle collection,
cash collection, try-outs, operator order entry, checkout). Under blanket
concurrency, one person's rapid taps can be handled simultaneously and race
their own conversation state — swapping a bot-wide stall for per-user
corruption, which is a worse trade in a flow that moves money and bottles.

So this processor takes the middle path, which is the one that actually
matches the problem:

* **Different chats run concurrently** — one driver's slow request no longer
  freezes every other driver. That is the entire bug being fixed.
* **A single chat's updates stay strictly ordered** — ``ConversationHandler``
  sees exactly the sequential world it was written for, so PTB's warning does
  not apply to us.

Concurrency is still bounded by ``max_concurrent_updates`` (enforced by
``BaseUpdateProcessor`` itself via a semaphore), so this cannot become an
unbounded fan-out on a Raspberry Pi.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Dict, Optional, Tuple

from telegram.ext import BaseUpdateProcessor

logger = logging.getLogger(__name__)

_ChatKey = Tuple[Optional[int], Optional[int]]


class PerChatSerialUpdateProcessor(BaseUpdateProcessor):
    """Serialise per (chat, user); parallelise across them."""

    __slots__ = ("_locks", "_waiters")

    def __init__(self, max_concurrent_updates: int = 16):
        super().__init__(max_concurrent_updates)
        self._locks: Dict[_ChatKey, asyncio.Lock] = {}
        # Reference count per key so a lock can be dropped once nobody holds
        # or awaits it. Without this the dict grows one entry per chat ever
        # seen and never shrinks — small, but it is a leak, and this codebase
        # has already flagged exactly that pattern once.
        self._waiters: Dict[_ChatKey, int] = {}

    @staticmethod
    def _key(update: object) -> _ChatKey:
        chat = getattr(update, "effective_chat", None)
        user = getattr(update, "effective_user", None)
        return (getattr(chat, "id", None), getattr(user, "id", None))

    async def do_process_update(self, update: object, coroutine: "Awaitable[Any]") -> None:
        key = self._key(update)

        # An update with neither chat nor user (poll updates, some service
        # updates) has no conversation to protect — run it straight away
        # rather than funnelling every one of them through a shared lock.
        if key == (None, None):
            await coroutine
            return

        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks[key] = asyncio.Lock()
        self._waiters[key] = self._waiters.get(key, 0) + 1

        try:
            async with lock:
                await coroutine
        finally:
            remaining = self._waiters.get(key, 1) - 1
            if remaining <= 0:
                self._waiters.pop(key, None)
                self._locks.pop(key, None)
            else:
                self._waiters[key] = remaining

    async def initialize(self) -> None:
        """Nothing to set up."""

    async def shutdown(self) -> None:
        """Drop any lock bookkeeping so a restarted Application starts clean."""
        self._locks.clear()
        self._waiters.clear()
