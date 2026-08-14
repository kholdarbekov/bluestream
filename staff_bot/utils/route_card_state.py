"""Per-driver route-card state, shared by PTB handlers and the webhook server.

WHY NOT context.user_data: the webhook server edits the card outside the PTB
update context (same process, no `context`). WHY REDIS: a bot restart
mid-shift must keep editing the SAME card message instead of orphaning it.
WHY MODULE-LEVEL LOCKS: the old per-user lock lived in user_data
(active_delivery.py:32-39) and guarded the read-delete-render-store cycle;
the single-card design has the same read-modify-write shape but two entry
paths (user tap, webhook push), so the lock must be reachable from both.
Both run in the SAME event loop, so asyncio.Lock is sufficient.

WHY NO IN-MEMORY FALLBACK: an earlier revision kept a process-local `_memory`
dict as a fallback layer. That created a split-brain: `load` reads Redis
first, so a card DOES survive a restart -- but on a Redis SET failure the
in-memory copy held the new value while Redis still held the old one, and a
later successful `load` (once Redis recovered) would silently regress the
card to the stale value. A failed DELETE similarly resurrected a cleared
card on the next load. Nothing reconciled the two stores. Redis is the
single source of truth here, matching the sibling module
`staff_bot/utils/flow_state.py` (no in-memory fallback either): when Redis
is unavailable or errors, `load` returns None, `save`/`clear`/`mark_borrowed`
no-op, and nothing is ever raised. A driver momentarily not seeing their
card re-render is recoverable (next event re-renders it); a stale card
silently reappearing after a Redis blip is not.

State dict keys: chat_id, message_id (None => no card message yet),
card_date ('YYYY-MM-DD' in DISPLAY_TIMEZONE — the card is SHIFT-scoped),
view ('next'|'all'|'borrowed'), content_sig, echoes_deleted (count of the
driver's own menu-tap messages we deleted from BELOW this card since it was
posted — input to the repost heuristic; see `note_echo_deleted` for why the
position matters), last_alert_at (ISO),
last_alert_message_id. TTL 48h — deleteMessage only works within 48h, so
older state is useless anyway (spec §6.3).
"""
import asyncio
import json
from typing import Dict, Optional

from shared.redis_failure import report_redis_failure
from shared.redis_keyspace import RedisKeyspace

VIEW_NEXT = "next"
VIEW_ALL = "all"
VIEW_BORROWED = "borrowed"
STATE_TTL_SECONDS = 48 * 3600

_redis = None
_locks: Dict[int, asyncio.Lock] = {}


def configure(redis_client) -> None:
    """Install the redis.asyncio client (or None to disable persistence
    entirely — every read/write below then degrades to a safe no-op)."""
    global _redis
    _redis = redis_client


def is_enabled() -> bool:
    return _redis is not None


def get_lock(telegram_id: int) -> asyncio.Lock:
    lock = _locks.get(int(telegram_id))
    if lock is None:
        lock = asyncio.Lock()
        _locks[int(telegram_id)] = lock
    return lock


async def load(telegram_id: int) -> Optional[dict]:
    if _redis is None:
        return None
    try:
        raw = await _redis.get(RedisKeyspace.staff_bot_route_card(int(telegram_id)))
        return json.loads(raw) if raw else None
    except Exception as exc:
        report_redis_failure("staff_bot.route_card_state.load", str(exc), tier="cache")
        return None


async def save(telegram_id: int, state: dict) -> None:
    if _redis is None:
        return
    try:
        await _redis.set(
            RedisKeyspace.staff_bot_route_card(int(telegram_id)),
            json.dumps(state, separators=(",", ":"), default=str),
            ex=STATE_TTL_SECONDS,
        )
    except Exception as exc:
        report_redis_failure("staff_bot.route_card_state.save", str(exc), tier="cache")


async def clear(telegram_id: int) -> None:
    if _redis is None:
        return
    try:
        await _redis.delete(RedisKeyspace.staff_bot_route_card(int(telegram_id)))
    except Exception as exc:
        report_redis_failure("staff_bot.route_card_state.clear", str(exc), tier="cache")


async def mark_borrowed(telegram_id: int) -> None:
    """The card MESSAGE is temporarily showing something else (stop detail,
    status confirm, at-door flow). Webhook-driven silent edits must not yank
    that UI out from under the driver — they check this flag and skip.
    Cleared by the next full card render.

    CORRECTED INVARIANT (FINAL review, C1): this function now takes
    `get_lock(telegram_id)` itself and merges ONLY the `view` field, the
    same shape `send_head_change_alert` uses at
    `staff_bot/handlers/delivery/route_card.py:730-734` (acquire the lock,
    reload fresh state inside it, mutate only your own field, save).

    An earlier revision documented the OPPOSITE contract here -- callers
    must hold the lock themselves, because self-locking would deadlock if
    this were ever called from inside `render_route_card`'s own critical
    section. That reasoning was correct but the enforcement never happened:
    this function's only production caller,
    `staff_bot/handlers/delivery/active_delivery.py`'s `view_active_delivery`,
    does NOT hold the lock and never calls `render_route_card` or
    `get_lock` anywhere in its body -- so the invariant was documented and
    silently unenforced. The result, reviewer-probed: a borrow landing
    while `render_route_card` is mid-edit gets overwritten the instant that
    edit's own in-lock save runs (`route_card.py:415-416`), because this
    function's old unlocked load-modify-save could interleave anywhere
    inside that window. The next silent webhook push then edits a card that
    is supposed to be frozen mid at-door-flow.

    DEADLOCK CHECK: self-locking is safe here specifically because
    `mark_borrowed` has exactly one caller and that caller provably never
    holds this lock or enters `render_route_card` before or after calling
    this function. If a future caller is added on a path that already
    holds `get_lock(telegram_id)` (directly, or indirectly via
    `render_route_card`), calling this function from there WILL deadlock --
    `asyncio.Lock` is not reentrant. Re-verify that before adding any new
    caller.
    """
    async with get_lock(telegram_id):
        state = await load(telegram_id)
        if not state:
            return
        state["view"] = VIEW_BORROWED
        await save(telegram_id, state)


async def note_echo_deleted(telegram_id: int, message_id: int) -> None:
    """The bot just deleted ONE of the driver's own reply-keyboard echo
    messages (tap-feedback spec §4.1). `message_id` is the id of the message
    that was deleted.

    Recorded so `render_route_card`'s repost heuristic can tell "N messages
    landed below the card" apart from "N messages landed below the card and
    we deleted every one of them". Without this counter a repeat tap is
    indistinguishable from real chat traffic burying the card, so the card
    reposts -- 1 send + 1 delete + 1 pin -- on roughly every other tap, and
    a fast-tapping driver trips flood control.

    POSITION CHECK (final review, fix 3): the counter only ever means
    "messages we removed from BELOW the card", because that is the only
    thing the heuristic's arithmetic can subtract -- it compares the card's
    message id against a LATER tap's id. An echo whose id is <= the card's
    id sits ABOVE the card and never contributed to the gap, so counting it
    inflates the threshold and silently suppresses a repost that should
    fire.

    That is not hypothetical: on a create/repost `render_route_card` resets
    `echoes_deleted` to 0 (route_card.py, `new_state`), and the very next
    thing the router does is delete the tap echo that TRIGGERED the create
    -- an id strictly BELOW the freshly sent card. Without this check every
    card starts life with a permanently inflated counter, and because each
    later tap raises the gap and the counter together the slack never
    closes: the spec §4.2 row-5 repost (an undeleted head-change alert
    buries the card) never fires.

    DEADLOCK CHECK: this self-locks, the same shape as `mark_borrowed`. Its
    only caller is `StaffBot._delete_menu_echo`, which holds no lock and
    does not enter `render_route_card` -- the dispatch that DID render has
    already returned by the time this runs. Re-verify before adding any
    caller that sits inside `render_route_card`'s critical section:
    `asyncio.Lock` is not reentrant.

    No state (Redis down, or no card yet) is a silent no-op, matching every
    other writer in this module. A non-integer id is treated the same way:
    a miscount is worse than no count.
    """
    async with get_lock(telegram_id):
        state = await load(telegram_id)
        if not state:
            return
        try:
            deleted_id = int(message_id)
            card_message_id = int(state.get("message_id") or 0)
        except (TypeError, ValueError):
            return
        if deleted_id <= card_message_id:
            # Above (or is) the card -- it never buried anything.
            return
        state["echoes_deleted"] = int(state.get("echoes_deleted") or 0) + 1
        await save(telegram_id, state)
