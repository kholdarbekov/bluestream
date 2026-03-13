"""
Database connection and operations for Staff Bot
"""
import asyncpg
import logging
from typing import Optional, Dict, Any, List
from contextlib import asynccontextmanager
import json
from datetime import datetime, timezone

from staff_bot.config import config

logger = logging.getLogger(__name__)


class DatabaseManager:
    """Manages database connections and operations"""

    POOL_MIN_SIZE = 3

    def __init__(self):
        self.pool: Optional[asyncpg.Pool] = None
        self._is_connected = False

    async def connect(self):
        """Establish database connection pool"""
        try:
            self.pool = await asyncpg.create_pool(
                config.database.url,
                min_size=self.POOL_MIN_SIZE,
                max_size=config.database.pool_size,
                command_timeout=config.database.pool_timeout,
            )
            self._is_connected = True
            logger.info("Database connection pool established")

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


class StaffUserRepository:
    """Repository for staff user operations"""

    def __init__(self, db: DatabaseManager):
        self.db = db

    async def get_user_by_telegram_id(self, telegram_id: int) -> Optional[Dict[str, Any]]:
        """Get staff user by telegram ID"""
        query = """
        SELECT u.*, dp.id as delivery_person_id,
               dp.max_concurrent_deliveries, dp.current_active_deliveries,
               dp.notifications_muted
        FROM users u
        LEFT JOIN delivery_persons dp ON dp.user_id = u.id
        WHERE u.telegram_id = $1
        """
        row = await self.db.fetchone(query, str(telegram_id))
        return dict(row) if row else None

    async def get_staff_user_by_phone(self, phone: str) -> Optional[Dict[str, Any]]:
        """Get staff user by phone number (for authentication)"""
        query = """
        SELECT u.*, dp.id as delivery_person_id
        FROM users u
        LEFT JOIN delivery_persons dp ON dp.user_id = u.id
        WHERE u.phone = $1
          AND (u.staff_roles IS NOT NULL AND u.staff_roles != '[]'::jsonb)
        """
        row = await self.db.fetchone(query, phone)
        return dict(row) if row else None

    async def link_telegram_id(self, user_id: int, telegram_id: int):
        """Link telegram ID to staff user account"""
        query = """
        UPDATE users
        SET telegram_id = $1, updated_at = CURRENT_TIMESTAMP
        WHERE id = $2
        """
        await self.db.execute(query, str(telegram_id), user_id)

    async def update_staff_bot_state(self, telegram_id: int, state: Dict[str, Any]):
        """Update user's staff bot state"""
        query = """
        UPDATE users
        SET staff_bot_state = $1, updated_at = CURRENT_TIMESTAMP
        WHERE telegram_id = $2
        """
        await self.db.execute(query, json.dumps(state), str(telegram_id))

    async def get_staff_bot_state(self, telegram_id: int) -> Dict[str, Any]:
        """Get user's staff bot state"""
        query = "SELECT staff_bot_state FROM users WHERE telegram_id = $1"
        state_json = await self.db.fetchval(query, str(telegram_id))
        if isinstance(state_json, str):
            return json.loads(state_json) if state_json else {}
        return state_json or {}

    async def update_user_language(self, telegram_id: int, language_code: str):
        """Update user's language preference"""
        query = """
        UPDATE users
        SET preferred_language = $1, updated_at = CURRENT_TIMESTAMP
        WHERE telegram_id = $2
        """
        await self.db.execute(query, language_code, str(telegram_id))

    async def log_staff_activity(
        self, user_id: int, action: str,
        entity_type: str = None, entity_id: int = None,
        metadata: Dict = None
    ):
        """Log staff activity"""
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)

        query = """
        INSERT INTO staff_activity_log (user_id, action, entity_type, entity_id, metadata, created_at, updated_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        """
        await self.db.execute(
            query, user_id, action, entity_type, entity_id,
            json.dumps(metadata or {}), now, now
        )


# Global database manager instance
db_manager = DatabaseManager()
