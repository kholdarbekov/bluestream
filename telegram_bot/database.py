"""
Database connection and operations for Telegram Bot
"""
import asyncpg
import logging
from typing import Optional, Dict, Any, List, Union
from contextlib import asynccontextmanager
import json
from datetime import datetime, timezone

from config import config

logger = logging.getLogger(__name__)


class DatabaseManager:
    """Manages database connections and operations"""
    
    def __init__(self):
        self.pool: Optional[asyncpg.Pool] = None
        self._is_connected = False
    
    async def connect(self):
        """Establish database connection pool"""
        logger.info(f"DB connect: {config.database}")
        try:
            self.pool = await asyncpg.create_pool(
                config.database.url,
                min_size=5,
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
        row = await self.db.fetchone(query, str(telegram_id))
        return dict(row) if row else None
    
    async def create_bot_user(self, telegram_id: int, username: str = None, 
                             first_name: str = None, last_name: str = None,
                             language_code: str = 'en') -> Dict[str, Any]:
        """Create new bot user in unified users table"""
        async with self.db.get_connection() as conn:
            # Create user in unified users table with telegram fields
            user_query = """
            INSERT INTO users (
                email, phone, password_hash, first_name, last_name, full_name, 
                preferred_language, role, status, telegram_id, registration_source,
                telegram_username, telegram_first_name, telegram_last_name,
                telegram_language_code, is_bot_active, bot_state, last_bot_interaction
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18)
            RETURNING *
            """
            
            full_name = f"{first_name or ''} {last_name or ''}".strip() or f"User {telegram_id}"
            
            user_row = await conn.fetchrow(
                user_query,
                f"telegram_{telegram_id}@bot.local",  # Temporary email
                None,  # No phone initially
                "telegram_user",  # Placeholder password hash
                first_name,
                last_name,
                full_name,
                language_code,
                'customer',
                'active',
                str(telegram_id),
                'telegram',
                username,
                first_name,
                last_name,
                language_code,
                True,
                json.dumps({}),  # Empty state
                datetime.now(timezone.utc)
            )
            
            return dict(user_row) if user_row else None
    
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
        SET preferred_language = $1, telegram_language_code = $1, updated_at = CURRENT_TIMESTAMP
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
        # Clear bot state
        state_query = """
        UPDATE users 
        SET bot_state = '{}', updated_at = CURRENT_TIMESTAMP
        WHERE telegram_id = $1
        """
        await self.db.execute(state_query, str(telegram_id))
        
        # Clear any bot sessions
        session_query = """
        DELETE FROM bot_sessions WHERE telegram_id = $1
        """
        await self.db.execute(session_query, telegram_id)
        
        logger.info(f"Cleared session data for telegram user {telegram_id}")


class BotSessionRepository:
    """Repository for bot session management"""
    
    def __init__(self, db: DatabaseManager):
        self.db = db
    
    async def create_session(self, telegram_id: int, session_data: Dict[str, Any]) -> str:
        """Create new bot session"""
        query = """
        INSERT INTO bot_sessions (telegram_id, session_data, expires_at)
        VALUES ($1, $2, $3)
        RETURNING session_id
        """
        expires_at = datetime.now(timezone.utc).replace(hour=23, minute=59, second=59, microsecond=999999)
        return await self.db.fetchval(query, telegram_id, json.dumps(session_data), expires_at)
    
    async def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Get session by ID"""
        query = """
        SELECT * FROM bot_sessions 
        WHERE session_id = $1 AND expires_at > CURRENT_TIMESTAMP
        """
        row = await self.db.fetchone(query, session_id)
        if row:
            data = dict(row)
            data['session_data'] = json.loads(data['session_data'])
            return data
        return None
    
    async def update_session(self, session_id: str, session_data: Dict[str, Any]):
        """Update session data"""
        query = """
        UPDATE bot_sessions 
        SET session_data = $1, updated_at = CURRENT_TIMESTAMP
        WHERE session_id = $2
        """
        await self.db.execute(query, json.dumps(session_data), session_id)
    
    async def delete_session(self, session_id: str):
        """Delete session"""
        query = "DELETE FROM bot_sessions WHERE session_id = $1"
        await self.db.execute(query, session_id)
    
    async def cleanup_expired_sessions(self):
        """Clean up expired sessions"""
        query = "DELETE FROM bot_sessions WHERE expires_at < CURRENT_TIMESTAMP"
        await self.db.execute(query)


# Create database tables for bot-specific data
async def create_bot_tables(db: DatabaseManager):
    """Create bot-specific database tables"""
    bot_users_table = """
    CREATE TABLE IF NOT EXISTS bot_users (
        id SERIAL PRIMARY KEY,
        user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
        telegram_id BIGINT UNIQUE NOT NULL,
        username VARCHAR(255),
        first_name VARCHAR(255),
        last_name VARCHAR(255),
        language_code VARCHAR(10) DEFAULT 'en',
        is_bot_active BOOLEAN DEFAULT TRUE,
        bot_state JSONB DEFAULT '{}',
        last_interaction TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
    );
    """
    
    bot_sessions_table = """
    CREATE TABLE IF NOT EXISTS bot_sessions (
        session_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        telegram_id BIGINT NOT NULL,
        session_data JSONB DEFAULT '{}',
        expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
    );
    """
    
    bot_analytics_table = """
    CREATE TABLE IF NOT EXISTS bot_analytics (
        id SERIAL PRIMARY KEY,
        telegram_id BIGINT,
        command VARCHAR(100),
        action VARCHAR(100),
        data JSONB DEFAULT '{}',
        success BOOLEAN DEFAULT TRUE,
        error_message TEXT,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
    );
    """
    
    # Create indexes
    indexes = [
        "CREATE INDEX IF NOT EXISTS idx_bot_users_telegram_id ON bot_users(telegram_id);",
        "CREATE INDEX IF NOT EXISTS idx_bot_users_user_id ON bot_users(user_id);",
        "CREATE INDEX IF NOT EXISTS idx_bot_sessions_telegram_id ON bot_sessions(telegram_id);",
        "CREATE INDEX IF NOT EXISTS idx_bot_sessions_expires_at ON bot_sessions(expires_at);",
        "CREATE INDEX IF NOT EXISTS idx_bot_analytics_telegram_id ON bot_analytics(telegram_id);",
        "CREATE INDEX IF NOT EXISTS idx_bot_analytics_created_at ON bot_analytics(created_at);",
    ]
    
    async with db.get_connection() as conn:
        await conn.execute(bot_users_table)
        await conn.execute(bot_sessions_table)
        await conn.execute(bot_analytics_table)
        
        for index in indexes:
            await conn.execute(index)
    
    logger.info("Bot database tables created/verified")


# Global database manager instance
db_manager = DatabaseManager()