-- Migration script to add session cleanup features
-- Run this script to add the ended_at column to user_sessions table if it doesn't exist

-- Add ended_at column to user_sessions table if it doesn't exist
DO $$ 
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns 
                   WHERE table_name = 'user_sessions' AND column_name = 'ended_at') THEN
        ALTER TABLE user_sessions ADD COLUMN ended_at TIMESTAMP WITH TIME ZONE;
        COMMENT ON COLUMN user_sessions.ended_at IS 'Timestamp when the session was explicitly ended';
    END IF;
END $$;

-- Add indexes for session cleanup operations
CREATE INDEX IF NOT EXISTS idx_user_sessions_expires_at ON user_sessions(expires_at);
CREATE INDEX IF NOT EXISTS idx_user_sessions_is_active_expires_at ON user_sessions(is_active, expires_at);
CREATE INDEX IF NOT EXISTS idx_user_sessions_user_id_is_active ON user_sessions(user_id, is_active);

-- Add indexes for user cleanup operations
CREATE INDEX IF NOT EXISTS idx_users_last_login ON users(last_login);
CREATE INDEX IF NOT EXISTS idx_users_status_last_login ON users(status, last_login);
CREATE INDEX IF NOT EXISTS idx_users_created_at ON users(created_at);
CREATE INDEX IF NOT EXISTS idx_users_last_bot_interaction ON users(last_bot_interaction);
CREATE INDEX IF NOT EXISTS idx_users_password_reset_expires ON users(password_reset_expires);

-- Add indexes for cleanup statistics queries
CREATE INDEX IF NOT EXISTS idx_users_registration_source ON users(registration_source);
CREATE INDEX IF NOT EXISTS idx_users_is_bot_active ON users(is_bot_active);

-- Update any existing sessions that are expired but still marked as active
UPDATE user_sessions 
SET is_active = false, ended_at = expires_at 
WHERE expires_at < NOW() AND is_active = true;

-- Add constraint to ensure ended_at is only set when session is inactive
ALTER TABLE user_sessions 
ADD CONSTRAINT chk_ended_at_inactive 
CHECK ((is_active = true AND ended_at IS NULL) OR (is_active = false));

COMMENT ON TABLE user_sessions IS 'User authentication sessions with cleanup tracking';
COMMENT ON COLUMN user_sessions.session_token IS 'JWT ID (jti) for token blacklisting';
COMMENT ON COLUMN user_sessions.is_active IS 'Whether the session is currently active';
COMMENT ON COLUMN user_sessions.last_activity IS 'Last recorded activity for this session';
COMMENT ON COLUMN user_sessions.ended_at IS 'When the session was explicitly terminated';

-- Create a view for session statistics
CREATE OR REPLACE VIEW session_cleanup_stats AS
SELECT 
    COUNT(*) as total_sessions,
    COUNT(*) FILTER (WHERE is_active = true) as active_sessions,
    COUNT(*) FILTER (WHERE is_active = false) as inactive_sessions,
    COUNT(*) FILTER (WHERE expires_at < NOW() AND is_active = true) as expired_but_active,
    COUNT(*) FILTER (WHERE expires_at < NOW() - INTERVAL '30 days' AND is_active = false) as old_expired_sessions,
    MIN(created_at) as oldest_session,
    MAX(last_activity) as latest_activity
FROM user_sessions;

COMMENT ON VIEW session_cleanup_stats IS 'Statistics for session cleanup monitoring';

-- Create a view for user cleanup statistics
CREATE OR REPLACE VIEW user_cleanup_stats AS
SELECT 
    COUNT(*) as total_users,
    COUNT(*) FILTER (WHERE status = 'active') as active_users,
    COUNT(*) FILTER (WHERE status = 'inactive') as inactive_users,
    COUNT(*) FILTER (WHERE status = 'banned') as banned_users,
    COUNT(*) FILTER (WHERE status = 'pending_verification') as pending_users,
    COUNT(*) FILTER (WHERE last_login < NOW() - INTERVAL '365 days' AND status = 'active') as users_needing_cleanup,
    COUNT(*) FILTER (WHERE password_reset_expires < NOW() AND password_reset_token IS NOT NULL) as expired_reset_tokens,
    COUNT(*) FILTER (WHERE bot_state IS NOT NULL AND is_bot_active = false) as inactive_bot_states,
    MIN(created_at) as oldest_user,
    MAX(last_login) as latest_login
FROM users;

COMMENT ON VIEW user_cleanup_stats IS 'Statistics for user cleanup monitoring';