-- Create audit logs table for comprehensive audit logging
-- This table stores all sensitive operations and security events

BEGIN;

-- Create audit event type enum
CREATE TYPE audit_event_type AS ENUM (
    -- Authentication events
    'login_success', 'login_failure', 'logout', 'password_change', 'password_reset',
    
    -- User management events
    'user_created', 'user_updated', 'user_deleted', 'user_role_changed', 'user_status_changed',
    
    -- Order management events
    'order_created', 'order_updated', 'order_cancelled', 'order_processed', 'order_delivered',
    
    -- Payment events
    'payment_processed', 'payment_refunded', 'payment_failed',
    
    -- Product management events
    'product_created', 'product_updated', 'product_deleted', 'inventory_updated',
    
    -- System administration events
    'settings_changed', 'system_maintenance', 'data_export', 'bulk_operation',
    
    -- Security events
    'permission_denied', 'suspicious_activity', 'emergency_operation', 'sensitive_data_access',
    
    -- API events
    'api_key_created', 'api_key_revoked', 'webhook_received'
);

-- Create audit severity enum
CREATE TYPE audit_severity AS ENUM ('low', 'medium', 'high', 'critical');

-- Create audit logs table
CREATE TABLE audit_logs (
    id SERIAL PRIMARY KEY,
    event_id VARCHAR(36) UNIQUE NOT NULL,
    event_type audit_event_type NOT NULL,
    severity audit_severity NOT NULL,
    
    -- User context
    user_id INTEGER REFERENCES users(id),
    user_role VARCHAR(50),
    session_id VARCHAR(255),
    
    -- Request context
    ip_address INET,  -- PostgreSQL native IP address type
    user_agent TEXT,
    endpoint VARCHAR(255),
    method VARCHAR(10),
    
    -- Event details
    resource_type VARCHAR(100),
    resource_id VARCHAR(100),
    action VARCHAR(100) NOT NULL,
    description TEXT,
    
    -- Data changes
    old_values JSONB,
    new_values JSONB,
    
    -- Metadata
    duration_ms INTEGER,
    success BOOLEAN NOT NULL DEFAULT TRUE,
    error_message TEXT,
    additional_data JSONB,
    
    -- Timestamps
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Create indexes for performance
CREATE INDEX idx_audit_logs_event_id ON audit_logs(event_id);
CREATE INDEX idx_audit_logs_event_type ON audit_logs(event_type);
CREATE INDEX idx_audit_logs_severity ON audit_logs(severity);
CREATE INDEX idx_audit_logs_user_id ON audit_logs(user_id);
CREATE INDEX idx_audit_logs_ip_address ON audit_logs(ip_address);
CREATE INDEX idx_audit_logs_endpoint ON audit_logs(endpoint);
CREATE INDEX idx_audit_logs_resource_type ON audit_logs(resource_type);
CREATE INDEX idx_audit_logs_resource_id ON audit_logs(resource_id);
CREATE INDEX idx_audit_logs_action ON audit_logs(action);
CREATE INDEX idx_audit_logs_success ON audit_logs(success);
CREATE INDEX idx_audit_logs_created_at ON audit_logs(created_at);

-- Composite indexes for common queries
CREATE INDEX idx_audit_logs_user_event_date ON audit_logs(user_id, event_type, created_at);
CREATE INDEX idx_audit_logs_resource_action_date ON audit_logs(resource_type, action, created_at);
CREATE INDEX idx_audit_logs_severity_date ON audit_logs(severity, created_at);
CREATE INDEX idx_audit_logs_failed_events ON audit_logs(success, event_type, created_at) WHERE success = FALSE;

-- Partial indexes for security events
CREATE INDEX idx_audit_logs_security_events ON audit_logs(event_type, severity, created_at) 
    WHERE event_type IN ('permission_denied', 'suspicious_activity', 'emergency_operation');

CREATE INDEX idx_audit_logs_auth_failures ON audit_logs(user_id, ip_address, created_at) 
    WHERE event_type = 'login_failure';

-- JSONB indexes for searching within additional_data
CREATE INDEX idx_audit_logs_old_values_gin ON audit_logs USING GIN(old_values);
CREATE INDEX idx_audit_logs_new_values_gin ON audit_logs USING GIN(new_values);
CREATE INDEX idx_audit_logs_additional_data_gin ON audit_logs USING GIN(additional_data);

-- Function to automatically update updated_at timestamp
CREATE OR REPLACE FUNCTION update_audit_logs_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Trigger to automatically update updated_at
CREATE TRIGGER trigger_audit_logs_updated_at
    BEFORE UPDATE ON audit_logs
    FOR EACH ROW
    EXECUTE FUNCTION update_audit_logs_updated_at();

-- Add table and column comments for documentation
COMMENT ON TABLE audit_logs IS 'Comprehensive audit log for all sensitive operations and security events';
COMMENT ON COLUMN audit_logs.event_id IS 'Unique identifier for the audit event';
COMMENT ON COLUMN audit_logs.event_type IS 'Category of the audit event';
COMMENT ON COLUMN audit_logs.severity IS 'Severity level of the event (low, medium, high, critical)';
COMMENT ON COLUMN audit_logs.user_id IS 'ID of the user who performed the action';
COMMENT ON COLUMN audit_logs.ip_address IS 'IP address from which the action was performed';
COMMENT ON COLUMN audit_logs.resource_type IS 'Type of resource affected (user, order, product, etc.)';
COMMENT ON COLUMN audit_logs.resource_id IS 'ID of the specific resource affected';
COMMENT ON COLUMN audit_logs.old_values IS 'Values before the change (JSONB format)';
COMMENT ON COLUMN audit_logs.new_values IS 'Values after the change (JSONB format)';
COMMENT ON COLUMN audit_logs.duration_ms IS 'Duration of the operation in milliseconds';
COMMENT ON COLUMN audit_logs.additional_data IS 'Additional context data (JSONB format)';

-- Create view for security dashboard
CREATE VIEW security_audit_summary AS
SELECT 
    DATE_TRUNC('day', created_at) as date,
    event_type,
    severity,
    COUNT(*) as event_count,
    COUNT(CASE WHEN success = FALSE THEN 1 END) as failed_count,
    COUNT(DISTINCT user_id) as unique_users,
    COUNT(DISTINCT ip_address) as unique_ips
FROM audit_logs
WHERE created_at >= NOW() - INTERVAL '30 days'
GROUP BY DATE_TRUNC('day', created_at), event_type, severity
ORDER BY date DESC, event_count DESC;

COMMENT ON VIEW security_audit_summary IS 'Daily summary of audit events for security dashboard';

-- Create view for failed authentication attempts
CREATE VIEW failed_auth_attempts AS
SELECT 
    user_id,
    ip_address,
    COUNT(*) as attempt_count,
    MAX(created_at) as last_attempt,
    MIN(created_at) as first_attempt
FROM audit_logs
WHERE event_type = 'login_failure'
    AND created_at >= NOW() - INTERVAL '24 hours'
GROUP BY user_id, ip_address
HAVING COUNT(*) >= 3
ORDER BY attempt_count DESC, last_attempt DESC;

COMMENT ON VIEW failed_auth_attempts IS 'Users/IPs with 3+ failed login attempts in the last 24 hours';

-- Create view for emergency operations
CREATE VIEW emergency_operations_log AS
SELECT 
    event_id,
    user_id,
    ip_address,
    action,
    description,
    additional_data,
    created_at
FROM audit_logs
WHERE event_type = 'emergency_operation'
    OR severity = 'critical'
ORDER BY created_at DESC;

COMMENT ON VIEW emergency_operations_log IS 'Log of all emergency and critical operations';

-- Function to clean up old audit logs (retention policy)
CREATE OR REPLACE FUNCTION cleanup_old_audit_logs(retention_days INTEGER DEFAULT 365)
RETURNS INTEGER AS $$
DECLARE
    deleted_count INTEGER;
BEGIN
    DELETE FROM audit_logs 
    WHERE created_at < NOW() - (retention_days || ' days')::INTERVAL;
    
    GET DIAGNOSTICS deleted_count = ROW_COUNT;
    
    -- Log the cleanup operation
    INSERT INTO audit_logs (
        event_id, event_type, severity, action, description, 
        resource_type, success, additional_data
    ) VALUES (
        gen_random_uuid()::text,
        'system_maintenance',
        'medium',
        'audit_log_cleanup',
        'Automated cleanup of old audit logs',
        'system',
        TRUE,
        jsonb_build_object('deleted_count', deleted_count, 'retention_days', retention_days)
    );
    
    RETURN deleted_count;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION cleanup_old_audit_logs IS 'Cleanup old audit logs based on retention policy';

-- Create function to get audit statistics
CREATE OR REPLACE FUNCTION get_audit_statistics(days INTEGER DEFAULT 7)
RETURNS TABLE(
    total_events BIGINT,
    failed_events BIGINT,
    unique_users BIGINT,
    unique_ips BIGINT,
    top_event_type TEXT,
    most_active_user INTEGER
) AS $$
BEGIN
    RETURN QUERY
    SELECT 
        COUNT(*) as total_events,
        COUNT(CASE WHEN success = FALSE THEN 1 END) as failed_events,
        COUNT(DISTINCT user_id) as unique_users,
        COUNT(DISTINCT ip_address) as unique_ips,
        (SELECT event_type::text 
         FROM audit_logs 
         WHERE created_at >= NOW() - (days || ' days')::INTERVAL
         GROUP BY event_type 
         ORDER BY COUNT(*) DESC 
         LIMIT 1) as top_event_type,
        (SELECT user_id 
         FROM audit_logs 
         WHERE created_at >= NOW() - (days || ' days')::INTERVAL
           AND user_id IS NOT NULL
         GROUP BY user_id 
         ORDER BY COUNT(*) DESC 
         LIMIT 1) as most_active_user
    FROM audit_logs
    WHERE created_at >= NOW() - (days || ' days')::INTERVAL;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION get_audit_statistics IS 'Get audit statistics for the specified number of days';

-- Grant appropriate permissions
GRANT SELECT ON audit_logs TO PUBLIC;
GRANT INSERT ON audit_logs TO PUBLIC;
GRANT SELECT ON security_audit_summary TO PUBLIC;
GRANT SELECT ON failed_auth_attempts TO PUBLIC;
GRANT SELECT ON emergency_operations_log TO PUBLIC;

-- Only administrators can delete audit logs
GRANT DELETE ON audit_logs TO admin_role;

COMMIT;

-- Test the audit log system
DO $$
BEGIN
    -- Insert a test audit log entry
    INSERT INTO audit_logs (
        event_id, event_type, severity, action, description,
        resource_type, user_id, ip_address, success
    ) VALUES (
        gen_random_uuid()::text,
        'system_maintenance',
        'low',
        'audit_system_test',
        'Test audit log entry created during system setup',
        'system',
        NULL,
        '127.0.0.1',
        TRUE
    );
    
    RAISE NOTICE 'Audit logs table created successfully with test entry';
END
$$;

-- Show table information
SELECT 
    schemaname,
    tablename,
    tableowner,
    tablespace,
    hasindexes,
    hasrules,
    hastriggers
FROM pg_tables 
WHERE tablename = 'audit_logs';

-- Show index information
SELECT 
    indexname,
    indexdef
FROM pg_indexes 
WHERE tablename = 'audit_logs'
ORDER BY indexname;