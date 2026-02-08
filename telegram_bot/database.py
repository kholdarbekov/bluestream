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