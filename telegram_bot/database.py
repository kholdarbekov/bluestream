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

    async def update_user_state(self, telegram_id: int, state: Dict[str, Any]):
        """Update user's bot state"""
        query = """
        UPDATE users
        SET bot_state = $1, last_bot_interaction = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
        WHERE telegram_id = $2
        """
        await self.db.execute(query, json.dumps(state), str(telegram_id))

    async def get_user_state(self, telegram_id: int) -> Dict[str, Any]:
        """Get user's bot state"""
        query = "SELECT bot_state FROM users WHERE telegram_id = $1"
        state_json = await self.db.fetchval(query, str(telegram_id))
        return json.loads(state_json) if state_json else {}

    async def clear_awaiting_input(self, telegram_id: int, *awaiting_inputs: str) -> bool:
        """Disarm ONLY the named prompts. The one expression of "they left MY flow".

        A screen that opens on top of a prompt it owns has to disarm that
        prompt, or the customer's next message is parsed as an answer to a
        question they walked away from — an unrelated sentence written over an
        address title. The obvious way to do that, ``update_user_state(id, {})``,
        is a BLANKET wipe: it also throws away a flow the screen knows nothing
        about. A customer who tapped "Report an issue" and then browsed
        Profile -> Addresses lost the armed report in silence, while its prompt
        and Cancel button stayed on screen still saying a report was open.

        So the caller names the prompts it owns, and a flow it does not own is
        left exactly as it was.

        WHY CLEARING THE WHOLE DOCUMENT IS THE RIGHT DISARM: ``bot_state`` holds
        at most ONE armed flow. ``awaiting_input`` is a single slot, and every
        writer (``handlers/support.py``, ``handlers/profile.py``, ``bot.py``)
        arms by WRITING A FRESH state rather than merging into the one already
        there — so every companion key in the document (``support_order_id``,
        ``support_order_number``, ``support_armed_at``, ``edit_address_id``,
        ``temp_location``) belongs to the flow being disarmed. Keep that
        invariant when adding a flow: ARM a new flow by writing a fresh state
        (a step moving WITHIN one flow may of course carry its own keys
        forward, as ``bot.py::_handle_location`` does), and this stays the only
        place that has to know which keys exist.

        Returns True when a flow was disarmed, False when the customer was
        standing in somebody else's flow (or in none) and the row was untouched.
        """
        state = await self.get_user_state(telegram_id)
        if state.get('awaiting_input') not in awaiting_inputs:
            return False

        await self.update_user_state(telegram_id, {})
        return True

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
