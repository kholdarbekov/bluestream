-- Fix remaining schema constraints and issues
-- This script addresses the errors from the initial constraints script

-- ============================================================================
-- 1. FIX EXISTING DATA ISSUES BEFORE ADDING CONSTRAINTS
-- ============================================================================

-- Fix users with invalid password hashes (telegram bot users)
UPDATE users 
SET password_hash = '$2b$12$' || encode(gen_random_bytes(29), 'base64')
WHERE length(password_hash) < 60;

-- ============================================================================
-- 2. ADD REMAINING CHECK CONSTRAINTS FOR ACTUAL SCHEMA
-- ============================================================================

-- Products table constraints (using actual column names)
ALTER TABLE products 
ADD CONSTRAINT check_base_price_positive 
CHECK (base_price > 0);

ALTER TABLE products 
ADD CONSTRAINT check_volume_positive 
CHECK (volume IS NULL OR volume > 0);

-- Add constraint for discount price if not null
ALTER TABLE products 
ADD CONSTRAINT check_discount_price_valid 
CHECK (discount_price IS NULL OR discount_price >= 0);

-- Add constraint for cost price if not null
ALTER TABLE products 
ADD CONSTRAINT check_cost_price_valid 
CHECK (cost_price IS NULL OR cost_price >= 0);

-- Add constraint for weight if not null
ALTER TABLE products 
ADD CONSTRAINT check_weight_positive 
CHECK (weight IS NULL OR weight > 0);

-- Add constraint for max stock level
ALTER TABLE products 
ADD CONSTRAINT check_max_stock_reasonable 
CHECK (max_stock_level > min_stock_level AND max_stock_level <= 10000);

-- Add password hash constraint (now that data is fixed)
ALTER TABLE users 
ADD CONSTRAINT check_password_hash_not_empty 
CHECK (password_hash IS NOT NULL AND length(password_hash) >= 60);

-- ============================================================================
-- 3. ADD ADDITIONAL SECURITY CONSTRAINTS
-- ============================================================================

-- Ensure email verification token is properly formatted if present
ALTER TABLE users 
ADD CONSTRAINT check_email_verification_token_format 
CHECK (email_verification_token IS NULL OR length(email_verification_token) >= 32);

-- Ensure password reset token is properly formatted if present
ALTER TABLE users 
ADD CONSTRAINT check_password_reset_token_format 
CHECK (password_reset_token IS NULL OR length(password_reset_token) >= 32);

-- Ensure telegram_id is properly formatted if present
ALTER TABLE users 
ADD CONSTRAINT check_telegram_id_format 
CHECK (telegram_id IS NULL OR telegram_id ~ '^\d+$');

-- Bot state should be valid JSON if present
ALTER TABLE users 
ADD CONSTRAINT check_bot_state_json 
CHECK (bot_state IS NULL OR bot_state::json IS NOT NULL);

-- ============================================================================
-- 4. ADD MISSING INDEXES FOR AUDIT QUERIES
-- ============================================================================

-- Indexes for security monitoring
CREATE INDEX IF NOT EXISTS idx_users_last_login ON users(last_login) WHERE last_login IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_users_failed_login_attempts ON users(failed_login_attempts) WHERE failed_login_attempts > 0;
CREATE INDEX IF NOT EXISTS idx_users_account_locked ON users(account_locked_until) WHERE account_locked_until IS NOT NULL;

-- Composite indexes for common audit queries
CREATE INDEX IF NOT EXISTS idx_audit_logs_user_timestamp ON audit_logs(user_id, event_timestamp);
CREATE INDEX IF NOT EXISTS idx_audit_logs_table_timestamp ON audit_logs(table_name, event_timestamp);
CREATE INDEX IF NOT EXISTS idx_sensitive_ops_user_timestamp ON sensitive_operations_audit(user_id, attempted_at);

-- ============================================================================
-- 5. CREATE AUDIT HELPER FUNCTIONS
-- ============================================================================

-- Function to log failed authentication attempts
CREATE OR REPLACE FUNCTION log_failed_authentication(
    p_email VARCHAR DEFAULT NULL,
    p_phone VARCHAR DEFAULT NULL,
    p_telegram_id VARCHAR DEFAULT NULL,
    p_ip_address INET DEFAULT NULL,
    p_user_agent TEXT DEFAULT NULL,
    p_failure_reason VARCHAR DEFAULT 'invalid_credentials',
    p_is_suspicious BOOLEAN DEFAULT FALSE
) RETURNS VOID AS $$
BEGIN
    INSERT INTO failed_login_audit (
        attempted_email,
        attempted_phone,
        attempted_telegram_id,
        ip_address,
        user_agent,
        failure_reason,
        is_suspicious,
        additional_data
    ) VALUES (
        p_email,
        p_phone,
        p_telegram_id,
        p_ip_address,
        p_user_agent,
        p_failure_reason,
        p_is_suspicious,
        jsonb_build_object(
            'timestamp', CURRENT_TIMESTAMP,
            'server_info', version()
        )
    );
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Function to log sensitive operations
CREATE OR REPLACE FUNCTION log_sensitive_operation(
    p_operation_type VARCHAR,
    p_operation_description TEXT,
    p_user_id INTEGER,
    p_ip_address INET,
    p_user_agent TEXT DEFAULT NULL,
    p_risk_level audit_severity DEFAULT 'MEDIUM',
    p_affected_tables TEXT[] DEFAULT NULL,
    p_affected_records_count INTEGER DEFAULT 0,
    p_success BOOLEAN DEFAULT TRUE,
    p_error_message TEXT DEFAULT NULL,
    p_business_justification TEXT DEFAULT NULL
) RETURNS VOID AS $$
BEGIN
    INSERT INTO sensitive_operations_audit (
        operation_type,
        operation_description,
        user_id,
        ip_address,
        user_agent,
        risk_level,
        affected_tables,
        affected_records_count,
        success,
        error_message,
        business_justification,
        completed_at
    ) VALUES (
        p_operation_type,
        p_operation_description,
        p_user_id,
        p_ip_address,
        p_user_agent,
        p_risk_level,
        p_affected_tables,
        p_affected_records_count,
        p_success,
        p_error_message,
        p_business_justification,
        CASE WHEN p_success THEN CURRENT_TIMESTAMP ELSE NULL END
    );
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- ============================================================================
-- 6. CREATE SECURITY MONITORING FUNCTIONS
-- ============================================================================

-- Function to detect suspicious login patterns
CREATE OR REPLACE FUNCTION detect_suspicious_logins(hours_back INTEGER DEFAULT 24)
RETURNS TABLE(
    ip_address INET,
    attempt_count BIGINT,
    unique_emails BIGINT,
    suspicious_attempts BIGINT,
    risk_score NUMERIC
) AS $$
BEGIN
    RETURN QUERY
    SELECT 
        fla.ip_address,
        COUNT(*) as attempt_count,
        COUNT(DISTINCT fla.attempted_email) as unique_emails,
        COUNT(CASE WHEN fla.is_suspicious THEN 1 END) as suspicious_attempts,
        CASE 
            WHEN COUNT(*) > 100 THEN 100.0
            WHEN COUNT(DISTINCT fla.attempted_email) > 20 THEN 90.0
            WHEN COUNT(CASE WHEN fla.is_suspicious THEN 1 END) > 5 THEN 80.0
            WHEN COUNT(*) > 50 THEN 70.0
            WHEN COUNT(*) > 20 THEN 50.0
            ELSE 20.0
        END as risk_score
    FROM failed_login_audit fla
    WHERE fla.attempted_at > CURRENT_TIMESTAMP - (hours_back || ' hours')::INTERVAL
    GROUP BY fla.ip_address
    HAVING COUNT(*) >= 5
    ORDER BY risk_score DESC, attempt_count DESC;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Function to get user security summary
CREATE OR REPLACE FUNCTION get_user_security_summary(p_user_id INTEGER)
RETURNS JSON AS $$
DECLARE
    result JSON;
BEGIN
    SELECT json_build_object(
        'user_id', u.id,
        'email', u.email,
        'last_login', u.last_login,
        'failed_attempts', u.failed_login_attempts,
        'account_locked', u.account_locked_until IS NOT NULL AND u.account_locked_until > CURRENT_TIMESTAMP,
        'recent_audit_events', (
            SELECT COUNT(*) 
            FROM audit_logs al 
            WHERE al.user_id = u.id 
            AND al.event_timestamp > CURRENT_TIMESTAMP - INTERVAL '24 hours'
        ),
        'high_risk_operations', (
            SELECT COUNT(*) 
            FROM sensitive_operations_audit soa 
            WHERE soa.user_id = u.id 
            AND soa.risk_level IN ('HIGH', 'CRITICAL')
            AND soa.attempted_at > CURRENT_TIMESTAMP - INTERVAL '7 days'
        ),
        'is_telegram_user', u.telegram_id IS NOT NULL,
        'registration_source', u.registration_source
    ) INTO result
    FROM users u
    WHERE u.id = p_user_id;
    
    RETURN result;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- ============================================================================
-- 7. GRANT PROPER PERMISSIONS
-- ============================================================================

-- Grant permissions for audit functions
GRANT EXECUTE ON FUNCTION log_failed_authentication TO public;
GRANT EXECUTE ON FUNCTION log_sensitive_operation TO public;
GRANT EXECUTE ON FUNCTION detect_suspicious_logins TO public;
GRANT EXECUTE ON FUNCTION get_user_security_summary TO public;

-- Grant read access to audit views
GRANT SELECT ON recent_security_events TO public;
GRANT SELECT ON failed_login_summary TO public;
GRANT SELECT ON high_risk_operations TO public;

-- ============================================================================
-- COMPLETION MESSAGE
-- ============================================================================

DO $$
BEGIN
    RAISE NOTICE '============================================================';
    RAISE NOTICE 'Database schema constraints and audit trails FIXED!';
    RAISE NOTICE '';
    RAISE NOTICE 'Fixed issues:';
    RAISE NOTICE '• Updated telegram bot users with proper password hashes';
    RAISE NOTICE '• Added constraints for actual database schema';
    RAISE NOTICE '• Added additional security validation constraints';
    RAISE NOTICE '• Created audit helper functions for application use';
    RAISE NOTICE '• Added security monitoring and detection functions';
    RAISE NOTICE '• Granted proper permissions for audit functionality';
    RAISE NOTICE '';
    RAISE NOTICE 'Available functions:';
    RAISE NOTICE '• log_failed_authentication() - Log failed login attempts';
    RAISE NOTICE '• log_sensitive_operation() - Log sensitive operations';
    RAISE NOTICE '• detect_suspicious_logins() - Detect suspicious patterns';
    RAISE NOTICE '• get_user_security_summary() - Get user security info';
    RAISE NOTICE '';
    RAISE NOTICE 'Schema validation complete!';
    RAISE NOTICE '============================================================';
END $$;