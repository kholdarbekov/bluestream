"""
Database connection and operations for Telegram Bot
"""
import asyncpg
import logging
from typing import Optional, Dict, Any, List
from contextlib import asynccontextmanager
import json
from datetime import datetime, timezone

from config import config
from shared.loyalty_eligibility import LOYALTY_ELIGIBLE_SQL

logger = logging.getLogger(__name__)


class DatabaseManager:
    """Manages database connections and operations"""

    POOL_MIN_SIZE = 5

    def __init__(self):
        self.pool: Optional[asyncpg.Pool] = None
        self._is_connected = False

    async def connect(self):
        """Establish database connection pool"""
        logger.info(f"DB connect: {config.database.url[-10:]}, {config.database.pool_size}, {config.database.pool_timeout}")
        try:
            self.pool = await asyncpg.create_pool(
                config.database.url,
                min_size=self.POOL_MIN_SIZE,
                max_size=config.database.pool_size,
                command_timeout=config.database.pool_timeout,
            )
            self._is_connected = True
            logger.info("Database connection pool established")

            # Test connection
            async with self.pool.acquire() as conn:
                await conn.fetchval("SELECT 1")

        except Exception as e:
            logger.error(f"Failed to connect to database: {e}")
            self._is_connected = False
            raise

    async def disconnect(self):
        """Close database connection pool"""
        if self.pool:
            await self.pool.close()
            self._is_connected = False
            logger.info("Database connection pool closed")

    @property
    def is_connected(self) -> bool:
        """Check if database is connected"""
        return self._is_connected and self.pool is not None

    @asynccontextmanager
    async def get_connection(self):
        """Get database connection from pool"""
        if not self.is_connected:
            raise RuntimeError("Database not connected")

        async with self.pool.acquire() as conn:
            yield conn

    async def execute(self, query: str, *args) -> str:
        """Execute a query that doesn't return data"""
        async with self.get_connection() as conn:
            return await conn.execute(query, *args)

    async def fetchone(self, query: str, *args) -> Optional[asyncpg.Record]:
        """Fetch single record"""
        async with self.get_connection() as conn:
            return await conn.fetchrow(query, *args)

    async def fetchall(self, query: str, *args) -> List[asyncpg.Record]:
        """Fetch all records"""
        async with self.get_connection() as conn:
            return await conn.fetch(query, *args)

    async def fetchval(self, query: str, *args) -> Any:
        """Fetch single value"""
        async with self.get_connection() as conn:
            return await conn.fetchval(query, *args)


class BotUserRepository:
    """Repository for bot user operations"""

    def __init__(self, db: DatabaseManager):
        self.db = db

    async def get_user_by_telegram_id(self, telegram_id: int) -> Optional[Dict[str, Any]]:
        """Get user by telegram ID from unified users table"""
        query = """
        SELECT u.*
        FROM users u
        WHERE u.telegram_id = $1
        """
        # telegram_id DB column is varchar(50), so cast int to str for query
        row = await self.db.fetchone(query, str(telegram_id))
        return dict(row) if row else None

    async def update_user_state(
        self, telegram_id: int, state: Dict[str, Any], *, touch_activity: bool = True
    ):
        """Update user's bot state.

        `touch_activity=False` skips stamping `last_bot_interaction` — for a
        write that is NOT something the customer did. M1 (final whole-branch
        review, SDD 2026-08-26-address-flow-bot-state): the only caller today
        is `clear_address_draft`'s timeout teardown
        (`ProfileHandlers.address_flow_timeout` calls it on a SYNTHETIC PTB
        update, not a customer action — see that method's docstring for the
        incident this prevents). Every other caller keeps the default, so
        this changes nothing about the ~30 existing call sites that rely on
        this write meaning "the customer just did something".
        """
        activity_clause = "last_bot_interaction = CURRENT_TIMESTAMP, " if touch_activity else ""
        query = f"""
        UPDATE users
        SET bot_state = $1, {activity_clause}updated_at = CURRENT_TIMESTAMP
        WHERE telegram_id = $2
        """
        await self.db.execute(query, json.dumps(state), str(telegram_id))

    async def get_user_state(self, telegram_id: int) -> Dict[str, Any]:
        """Get user's bot state"""
        query = "SELECT bot_state FROM users WHERE telegram_id = $1"
        state_json = await self.db.fetchval(query, str(telegram_id))
        return json.loads(state_json) if state_json else {}

    # Keys that survive both `arm_awaiting_input` and `disarm`, no matter which
    # flow is being armed or torn down. Defined once so a future preserved key
    # (the address flow is the first, and the plan for it names only one) is
    # added in exactly one place rather than copy-pasted into both methods.
    _PRESERVED_KEYS = ('address_draft',)

    async def clear_awaiting_input(self, telegram_id: int, *awaiting_inputs: str) -> bool:
        """Disarm ONLY the named prompts. Thin alias for `disarm` (see its
        docstring for the full "why" — the one-armed-flow rule, the
        `_PRESERVED_KEYS` carve-out for `address_draft`, and the no-write
        guarantee on a not-mine call).

        Kept as a SEPARATE NAME rather than folding its ~11 existing callers
        (`handlers/profile.py`, `handlers/support.py`) over to call `disarm`
        directly: several pre-existing tests mock the repo as
        `SimpleNamespace(clear_awaiting_input=AsyncMock(...))` and assert on
        that attribute, so a rename would force editing tests this task must
        not touch. Converting those call sites is tracked separately — this
        alias is what makes deferring that safe: every one of them now gets
        `_PRESERVED_KEYS` preservation today, with zero call-site changes.
        """
        return await self.disarm(telegram_id, *awaiting_inputs)

    async def arm_awaiting_input(self, telegram_id: int, awaiting_input: str, **companions) -> None:
        """Arm one flow, carrying preserved keys (`_PRESERVED_KEYS`) forward.

        `update_user_state` is a whole-document replace, and every flow arms by
        writing a fresh document — e.g. `handlers/support.py`'s "Report an
        issue" prompt writes `{'awaiting_input': 'support_message', ...}` with
        no merge. That destroys an open `address_draft` at ARM time, before any
        disarm/parking logic ever runs. This is the fix: read-modify-write,
        carrying `_PRESERVED_KEYS` across the replace.

        The one-armed-flow invariant for `awaiting_input` itself is unchanged —
        this still overwrites whatever prompt was previously armed, and every
        companion key belonging to that PREVIOUS flow (e.g. a stale
        `support_order_id` left over from an order-scoped "Report an issue") is
        dropped, not carried forward into the new flow. Only `_PRESERVED_KEYS`
        survive automatically.

        PRECEDENCE: `companions` is splatted last —
        `{**preserved, 'awaiting_input': ..., **companions}` — so an explicit
        caller wins over an auto-preserved key. `arm_awaiting_input(uid, 'x',
        address_draft=new_draft)` REPLACES the stored draft rather than being
        shadowed by the one carried forward from `preserved`. That is
        deliberate, not an oversight: an arm-and-save call site is exactly what
        a caller passing `address_draft` explicitly wants.
        """
        state = await self.get_user_state(telegram_id)
        preserved = {k: state[k] for k in self._PRESERVED_KEYS if k in state}
        await self.update_user_state(
            telegram_id, {**preserved, 'awaiting_input': awaiting_input, **companions}
        )

    async def disarm(self, telegram_id: int, *owned: str) -> bool:
        """Disarm ONLY the named prompts, preserving `_PRESERVED_KEYS`.

        A screen that opens on top of a prompt it owns has to disarm that
        prompt, or the customer's next message is parsed as an answer to a
        question they walked away from — an unrelated sentence written over an
        address title. The obvious way to do that, ``update_user_state(id, {})``,
        is a BLANKET wipe: it also throws away a flow the screen knows nothing
        about. A customer who tapped "Report an issue" and then browsed
        Profile -> Addresses lost the armed report in silence, while its prompt
        and Cancel button stayed on screen still saying a report was open.

        So the caller names the prompts it owns (`owned`), and a flow it does
        not own is left exactly as it was: returns False, and issues NO write
        at all — not even one that would bump `last_bot_interaction` — so a
        screen that doesn't own the armed prompt cannot even side-effect it.

        WHY CLEARING TO JUST `_PRESERVED_KEYS` IS THE RIGHT DISARM: for
        ``awaiting_input`` itself, the one-armed-flow rule still holds.
        ``bot_state`` holds at most ONE armed flow at a time, so every
        companion key in the document that is NOT in ``_PRESERVED_KEYS``
        (``support_order_id``, ``support_order_number``, ``support_armed_at``,
        ``edit_address_id``) belongs to the flow being disarmed, and this stays
        the only place that has to know which keys exist.

        ``address_draft`` is the one exception, and it is not a flow: it is
        unfinished customer work — an address the customer started and has not
        finished — that outlives whoever currently holds ``awaiting_input``.
        Wiping it here on every disarm would throw away that work the moment
        any unrelated prompt closes. `clear_awaiting_input` is a thin alias for
        this method, so both names get the same preservation.

        Returns True when a flow was disarmed, False when the customer was
        standing in somebody else's flow (or in none) and the row was
        untouched.
        """
        state = await self.get_user_state(telegram_id)
        if state.get('awaiting_input') not in owned:
            return False

        preserved = {k: state[k] for k in self._PRESERVED_KEYS if k in state}
        await self.update_user_state(telegram_id, preserved)
        return True

    # --- Address-creation draft (SDD 2026-08-26-address-flow-bot-state, P2) --
    #
    # The address-creation conversation currently lives entirely in
    # `context.user_data['temp_address_data']`, which PTB's PicklePersistence
    # loses on a redeploy or a crash mid-flow — a customer seven steps into
    # naming their address restarts from nothing. These three methods make
    # the SAME data durable in `bot_state.address_draft`, read-modify-write so
    # neither touches `awaiting_input` or any other flow's companion keys.
    #
    # Task 6 (P2) is a DUAL-WRITE ONLY: `handlers/profile.py` calls
    # `save_address_draft` at every step the flow advances to and
    # `clear_address_draft` at every teardown, but nothing reads
    # `address_draft` back yet, and NOTHING here (or in profile.py) sets
    # `awaiting_input` to `'address_flow'`. Arming is P3a's job — a
    # ConversationHandler still owns the flow today, so if a text update ever
    # escaped it while `address_flow` were armed, it would reach
    # `bot.py::_handle_contextual_input`'s unknown-state branch, which
    # disarms and tells the customer their input was invalid.

    async def get_address_draft(self, telegram_id: int) -> Optional[Dict[str, Any]]:
        """The customer's unfinished address, or None when there isn't one.

        Read-only mirror of whatever `save_address_draft` last wrote — see
        that method's docstring for the stored shape.
        """
        state = await self.get_user_state(telegram_id)
        return state.get('address_draft')

    async def save_address_draft(
        self,
        telegram_id: int,
        *,
        step: str,
        data: Dict[str, Any],
        address_id: Any = None,
        origin: Optional[str] = None,
        parked: bool = False,
    ) -> None:
        """Persist the in-flight address-creation draft, touching ONLY this key.

        `update_user_state` is a whole-document replace, so writing just
        `{'address_draft': ...}` would silently erase an armed
        `awaiting_input` and its companions — e.g. a concern report the
        customer armed via "Report an issue" before wandering into address
        creation (the mirror-image hazard `arm_awaiting_input` already
        guards against for the opposite direction). Reading the full document
        first and spreading it into the write preserves every key that is not
        `address_draft`, unconditionally — unlike `arm_awaiting_input` /
        `disarm`, this has no need to special-case `_PRESERVED_KEYS`, because
        it never narrows the document down in the first place.

        `step` names where a later phase should resume the customer — the
        field the flow is moving TO, not the one just answered. `data` is the
        flow's own `temp_address_data` snapshot, EXCEPT `address_id` — that
        one has its own parameter below, so a resume reads it from exactly
        one place (`ProfileHandlers._persist_address_draft` strips it out of
        `data` before calling this; M3, final whole-branch review). This
        method itself does not enforce that split — it stores whatever `data`
        it is given verbatim — so a caller that legitimately wants
        `address_id` inside `data` too may still pass it that way; the one
        production caller today just does not. `address_id` is set once the
        pin branch's early-create has written a real row
        (`ProfileHandlers._create_address_now`); `origin` carries
        `'checkout'` so a resume can still return the customer to checkout;
        `parked` is always False from this task — P4 is what sets it True.
        """
        state = await self.get_user_state(telegram_id)
        await self.update_user_state(telegram_id, {
            **state,
            'address_draft': {
                'step': step,
                'data': data,
                'address_id': address_id,
                'origin': origin,
                'parked': parked,
            },
        })

    async def clear_address_draft(
        self, telegram_id: int, *, touch_activity: bool = True
    ) -> None:
        """Remove `address_draft` and nothing else — a no-op write when there
        is nothing to remove.

        Called from `ProfileHandlers._clear_address_flow_keys` at every
        teardown of the creation flow (cancel, timeout, or a completed save)
        so a finished or abandoned flow never leaves a stale draft for a
        later phase to wrongly resume into.

        Returns before writing when no draft is present at all, matching
        `disarm`'s own promise two methods above this one. That guard alone
        USED TO be enough to protect `last_bot_interaction`, back when a draft
        was something only SOME flows left behind. It no longer is: Task 6's
        dual-write means a draft exists from the moment `add_address` runs, so
        by the time any teardown fires there is almost always one to remove,
        and the guard falls through to the unconditional write below.

        M1 (final whole-branch review): `ProfileHandlers.address_flow_timeout`
        calls this on a SYNTHETIC update PTB itself generates when
        `conversation_timeout` fires — not something the customer did. Left
        unconditional, that write would stamp `last_bot_interaction = now()`
        on every timeout, including one where the customer sent nothing after
        arming the flow 24h earlier. That timestamp feeds
        `session_cleanup_service`'s 180-day inactivity sweep and the admin
        UI's "Last Bot Interaction" column, so a customer who went silent a
        day ago would look active today. `touch_activity=False` is that
        caller's way of saying so; every other teardown (cancel, cancel-text,
        a completed save) is a real customer action and keeps the default.
        """
        state = await self.get_user_state(telegram_id)
        if 'address_draft' not in state:
            return
        state.pop('address_draft', None)
        await self.update_user_state(telegram_id, state, touch_activity=touch_activity)

    async def update_user_language(self, telegram_id: int, language_code: str):
        """Update user's language preference"""
        query = """
        UPDATE users
        SET preferred_language = $1, updated_at = CURRENT_TIMESTAMP
        WHERE telegram_id = $2
        """
        await self.db.execute(query, language_code, str(telegram_id))

    async def set_user_phone(self, telegram_id: int, phone: str):
        """Set user's phone number"""
        query = """
        UPDATE users
        SET phone = $1, updated_at = CURRENT_TIMESTAMP
        WHERE telegram_id = $2
        """
        await self.db.execute(query, phone, str(telegram_id))

    async def set_user_phone_verified(self, telegram_id: int, phone: str):
        """Set user's phone number and mark it as verified."""
        query = """
        UPDATE users
        SET phone = $1, phone_verified_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
        WHERE telegram_id = $2
        """
        await self.db.execute(query, phone, str(telegram_id))

    async def get_user_loyalty_eligible(self, telegram_id: int) -> bool:
        """Check whether the user is eligible for the loyalty programme.

        Executes the canonical LOYALTY_ELIGIBLE_SQL expression (shared with the
        backend parity test) in a single fetchval call.  Returns True for unknown
        users (no row) so callers default-open rather than locking out unregistered
        visitors.
        """
        value = await self.db.fetchval(
            f"SELECT {LOYALTY_ELIGIBLE_SQL} FROM users u WHERE u.telegram_id = $1",
            str(telegram_id),
        )
        return True if value is None else bool(value)

    async def clear_user_session(self, telegram_id: int):
        """Clear user session data and bot state"""
        # Clear bot state in users table
        state_query = """
        UPDATE users
        SET bot_state = '{}', updated_at = CURRENT_TIMESTAMP
        WHERE telegram_id = $1
        """
        await self.db.execute(state_query, str(telegram_id))

        logger.info(f"Cleared session data for telegram user {telegram_id}")


# Global database manager instance
db_manager = DatabaseManager()
