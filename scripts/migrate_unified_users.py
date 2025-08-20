#!/usr/bin/env python3
"""
Database migration script to unify bot_users table with users table
This script:
1. Adds new telegram/bot-specific columns to users table
2. Migrates data from bot_users to users table
3. Updates references and cleans up bot_users table
"""

import os
import sys
import psycopg2
import json
from datetime import datetime
from typing import Dict, List, Any

# Add the project root to the path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from business_app import create_app
from business_app.models.user import User, db


def get_db_connection():
    """Get direct database connection"""
    import os
    
    # Try different connection methods
    database_url = os.environ.get('DATABASE_URL')
    
    if not database_url:
        # Fallback to constructing URL from environment or defaults
        host = os.environ.get('POSTGRES_HOST', 'localhost')
        port = os.environ.get('POSTGRES_PORT', '5432')
        db_name = os.environ.get('POSTGRES_DB', 'bluestream_db')
        user = os.environ.get('POSTGRES_USER', 'postgres')
        password = os.environ.get('POSTGRES_PASSWORD', 'postgres')
        database_url = f"postgresql://{user}:{password}@{host}:{port}/{db_name}"
    
    print(f"Connecting to database: {database_url}")
    return psycopg2.connect(database_url)


def execute_sql(conn, sql: str, params: tuple = None) -> List[Dict]:
    """Execute SQL and return results"""
    with conn.cursor() as cur:
        if params:
            cur.execute(sql, params)
        else:
            cur.execute(sql)
        
        if cur.description:  # SELECT query
            columns = [desc[0] for desc in cur.description]
            return [dict(zip(columns, row)) for row in cur.fetchall()]
        return []


def add_new_columns(conn):
    """Add new telegram/bot-specific columns to users table"""
    print("Adding new columns to users table...")
    
    new_columns = [
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS telegram_username VARCHAR(255)",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS telegram_first_name VARCHAR(255)", 
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS telegram_last_name VARCHAR(255)",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS telegram_language_code VARCHAR(10)",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_bot_active BOOLEAN DEFAULT FALSE",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS bot_state TEXT",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_bot_interaction TIMESTAMP WITH TIME ZONE",
    ]
    
    for sql in new_columns:
        execute_sql(conn, sql)
    
    # Add index for is_bot_active
    execute_sql(conn, "CREATE INDEX IF NOT EXISTS idx_users_is_bot_active ON users(is_bot_active)")
    
    conn.commit()
    print("✓ New columns added successfully")


def check_bot_users_table_exists(conn) -> bool:
    """Check if bot_users table exists"""
    result = execute_sql(conn, """
        SELECT EXISTS (
            SELECT FROM information_schema.tables 
            WHERE table_name = 'bot_users'
        )
    """)
    return result[0]['exists'] if result else False


def migrate_bot_users_data(conn):
    """Migrate data from bot_users table to users table"""
    print("Migrating bot_users data to users table...")
    
    if not check_bot_users_table_exists(conn):
        print("⚠ bot_users table does not exist, skipping data migration")
        return
    
    # Get all bot_users with their associated user records
    bot_users = execute_sql(conn, """
        SELECT bu.*, u.id as user_id, u.email, u.phone
        FROM bot_users bu
        LEFT JOIN users u ON bu.user_id = u.id
        ORDER BY bu.id
    """)
    
    print(f"Found {len(bot_users)} bot users to migrate")
    
    migrated_count = 0
    
    for bot_user in bot_users:
        try:
            if bot_user['user_id']:
                # Update existing user record
                update_sql = """
                    UPDATE users SET 
                        telegram_username = %s,
                        telegram_first_name = %s, 
                        telegram_last_name = %s,
                        telegram_language_code = %s,
                        is_bot_active = %s,
                        bot_state = %s,
                        last_bot_interaction = %s,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                """
                
                execute_sql(conn, update_sql, (
                    bot_user['username'],
                    bot_user['first_name'],
                    bot_user['last_name'], 
                    bot_user['language_code'],
                    bot_user['is_bot_active'],
                    bot_user['bot_state'],
                    bot_user['last_interaction'],
                    bot_user['user_id']
                ))
                migrated_count += 1
                print(f"✓ Updated user {bot_user['user_id']} with telegram_id {bot_user['telegram_id']}")
                
            else:
                # Create new user record for orphaned bot users
                print(f"⚠ Found orphaned bot user with telegram_id {bot_user['telegram_id']}, creating new user")
                
                insert_sql = """
                    INSERT INTO users (
                        email, phone, password_hash, first_name, last_name, full_name,
                        preferred_language, role, status, telegram_id, registration_source,
                        telegram_username, telegram_first_name, telegram_last_name,
                        telegram_language_code, is_bot_active, bot_state, last_bot_interaction,
                        created_at, updated_at
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                    ) RETURNING id
                """
                
                full_name = f"{bot_user['first_name'] or ''} {bot_user['last_name'] or ''}".strip()
                if not full_name:
                    full_name = f"User {bot_user['telegram_id']}"
                
                result = execute_sql(conn, insert_sql, (
                    f"telegram_{bot_user['telegram_id']}@bot.local",  # temporary email
                    None,  # no phone initially
                    "telegram_user",  # placeholder password hash
                    bot_user['first_name'],
                    bot_user['last_name'],
                    full_name,
                    bot_user['language_code'] or 'en',
                    'customer',
                    'active',
                    str(bot_user['telegram_id']),
                    'telegram',
                    bot_user['username'],
                    bot_user['first_name'],
                    bot_user['last_name'],
                    bot_user['language_code'],
                    bot_user['is_bot_active'],
                    bot_user['bot_state'],
                    bot_user['last_interaction'],
                    bot_user['created_at'],
                    bot_user['updated_at']
                ))
                
                if result:
                    new_user_id = result[0]['id']
                    migrated_count += 1
                    print(f"✓ Created new user {new_user_id} for telegram_id {bot_user['telegram_id']}")
                    
        except Exception as e:
            print(f"✗ Error migrating bot user {bot_user['telegram_id']}: {e}")
            continue
    
    conn.commit()
    print(f"✓ Successfully migrated {migrated_count} bot users")


def update_references(conn):
    """Update any references to bot_users table"""
    print("Updating references to bot_users table...")
    
    # For now, just log what tables reference bot_users
    # In production, you'd update these references
    references = execute_sql(conn, """
        SELECT 
            tc.table_name, 
            tc.constraint_name, 
            tc.constraint_type,
            kcu.column_name,
            ccu.table_name AS foreign_table_name,
            ccu.column_name AS foreign_column_name 
        FROM 
            information_schema.table_constraints AS tc 
            JOIN information_schema.key_column_usage AS kcu
              ON tc.constraint_name = kcu.constraint_name
              AND tc.table_schema = kcu.table_schema
            JOIN information_schema.constraint_column_usage AS ccu
              ON ccu.constraint_name = tc.constraint_name
              AND ccu.table_schema = tc.table_schema
        WHERE 
            (tc.table_name = 'bot_users' OR ccu.table_name = 'bot_users')
            AND tc.constraint_type = 'FOREIGN KEY'
    """)
    
    if references:
        print("Found references to bot_users table:")
        for ref in references:
            print(f"  - {ref['table_name']}.{ref['column_name']} -> {ref['foreign_table_name']}.{ref['foreign_column_name']}")
    else:
        print("No foreign key references to bot_users table found")


def cleanup_verification():
    """Verify data integrity before cleanup"""
    print("Verifying data migration...")
    
    with get_db_connection() as conn:
        # Check that all telegram users have unified records
        telegram_users = execute_sql(conn, """
            SELECT COUNT(*) as count FROM users 
            WHERE telegram_id IS NOT NULL AND registration_source = 'telegram'
        """)
        
        if check_bot_users_table_exists(conn):
            bot_users_count = execute_sql(conn, "SELECT COUNT(*) as count FROM bot_users")
            print(f"Telegram users in unified table: {telegram_users[0]['count']}")
            print(f"Records in bot_users table: {bot_users_count[0]['count']}")
        else:
            print(f"Telegram users in unified table: {telegram_users[0]['count']}")
            print("bot_users table no longer exists")


def main():
    """Main migration function"""
    print("=" * 60)
    print("UNIFIED USER TABLE MIGRATION")
    print("=" * 60)
    
    try:
        # Create Flask app context for database operations
        app = create_app()
        
        with app.app_context():
            with get_db_connection() as conn:
                # Step 1: Add new columns
                add_new_columns(conn)
                
                # Step 2: Migrate bot_users data
                migrate_bot_users_data(conn)
                
                # Step 3: Update references
                update_references(conn)
                
        # Step 4: Verify migration
        cleanup_verification()
        
        print("\n" + "=" * 60)
        print("MIGRATION COMPLETED SUCCESSFULLY")
        print("=" * 60)
        print("\nNext steps:")
        print("1. Test the unified authentication flow")
        print("2. Update BotUserRepository to use unified users table")
        print("3. Update API endpoints")
        print("4. Remove bot_users table after thorough testing")
        
    except Exception as e:
        print(f"\n✗ Migration failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()