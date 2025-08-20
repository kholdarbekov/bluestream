-- Add missing database schema constraints and audit trails
-- This script enhances the BlueStream database with proper constraints and audit capabilities

-- ============================================================================
-- 1. ADD MISSING CHECK CONSTRAINTS FOR DATA INTEGRITY
-- ============================================================================

-- Users table constraints
ALTER TABLE users 
ADD CONSTRAINT check_email_format 
CHECK (email ~* '^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$');

ALTER TABLE users 
ADD CONSTRAINT check_phone_format 
CHECK (phone IS NULL OR phone ~* '^\+?[1-9]\d{1,14}$');

ALTER TABLE users 
ADD CONSTRAINT check_failed_login_attempts 
CHECK (failed_login_attempts >= 0 AND failed_login_attempts <= 10);

ALTER TABLE users 
ADD CONSTRAINT check_password_hash_not_empty 
CHECK (password_hash IS NOT NULL AND length(password_hash) >= 60);

ALTER TABLE users 
ADD CONSTRAINT check_dates_logical 
CHECK (
    (password_reset_expires IS NULL OR password_reset_expires > CURRENT_TIMESTAMP) AND
    (email_verified_at IS NULL OR email_verified_at <= CURRENT_TIMESTAMP) AND
    (last_login IS NULL OR last_login <= CURRENT_TIMESTAMP)
);

-- Products table constraints
ALTER TABLE products 
ADD CONSTRAINT check_prices_positive 
CHECK (base_price > 0 AND current_price > 0);

ALTER TABLE products 
ADD CONSTRAINT check_volume_positive 
CHECK (volume_liters > 0);

ALTER TABLE products 
ADD CONSTRAINT check_stock_non_negative 
CHECK (stock_quantity >= 0 AND min_stock_level >= 0);

ALTER TABLE products 
ADD CONSTRAINT check_max_order_reasonable 
CHECK (max_order_quantity > 0 AND max_order_quantity <= 1000);

ALTER TABLE products 
ADD CONSTRAINT check_rating_valid 
CHECK (average_rating >= 0 AND average_rating <= 5);

ALTER TABLE products 
ADD CONSTRAINT check_review_count_non_negative 
CHECK (review_count >= 0);

-- Orders table constraints
ALTER TABLE orders 
ADD CONSTRAINT check_amounts_non_negative 
CHECK (
    subtotal >= 0 AND 
    discount_amount >= 0 AND 
    delivery_fee >= 0 AND 
    loyalty_discount >= 0 AND 
    total_amount >= 0
);

ALTER TABLE orders 
ADD CONSTRAINT check_total_amount_calculation 
CHECK (total_amount = subtotal + delivery_fee - discount_amount - loyalty_discount);

ALTER TABLE orders 
ADD CONSTRAINT check_loyalty_points_non_negative 
CHECK (loyalty_points_used >= 0 AND loyalty_points_earned >= 0);

ALTER TABLE orders 
ADD CONSTRAINT check_paid_logic 
CHECK (
    (is_paid = false AND paid_at IS NULL) OR 
    (is_paid = true AND paid_at IS NOT NULL)
);

-- Order items table constraints
ALTER TABLE order_items 
ADD CONSTRAINT check_quantity_positive 
CHECK (quantity > 0 AND quantity <= 100);

ALTER TABLE order_items 
ADD CONSTRAINT check_prices_positive 
CHECK (unit_price > 0 AND total_price >= 0 AND discount_amount >= 0);

ALTER TABLE order_items 
ADD CONSTRAINT check_total_price_calculation 
CHECK (total_price = (unit_price * quantity) - discount_amount);

-- Payment transactions constraints
ALTER TABLE payment_transactions 
ADD CONSTRAINT check_amount_positive 
CHECK (amount > 0);

ALTER TABLE payment_transactions 
ADD CONSTRAINT check_refund_amount_valid 
CHECK (refund_amount >= 0 AND refund_amount <= amount);

ALTER TABLE payment_transactions 
ADD CONSTRAINT check_transaction_id_format 
CHECK (length(transaction_id) >= 10);

-- Reviews table constraints
ALTER TABLE reviews 
ADD CONSTRAINT check_rating_range 
CHECK (rating >= 1 AND rating <= 5);

ALTER TABLE reviews 
ADD CONSTRAINT check_helpful_count_non_negative 
CHECK (helpful_count >= 0);

-- Loyalty transactions constraints
ALTER TABLE loyalty_transactions 
ADD CONSTRAINT check_points_not_zero 
CHECK (points != 0);

ALTER TABLE loyalty_transactions 
ADD CONSTRAINT check_expiry_future 
CHECK (expires_at IS NULL OR expires_at > created_at);

-- Subscriptions constraints
ALTER TABLE subscriptions 
ADD CONSTRAINT check_billing_amount_positive 
CHECK (billing_amount > 0);

ALTER TABLE subscriptions 
ADD CONSTRAINT check_subscription_dates_logical 
CHECK (
    start_date <= COALESCE(end_date, '2099-12-31'::timestamp) AND
    (paused_at IS NULL OR paused_at >= start_date) AND
    (resume_date IS NULL OR resume_date > paused_at)
);

ALTER TABLE subscriptions 
ADD CONSTRAINT check_delivery_day_valid 
CHECK (
    (delivery_day_of_week IS NULL OR delivery_day_of_week BETWEEN 1 AND 7) AND
    (delivery_day_of_month IS NULL OR delivery_day_of_month BETWEEN 1 AND 31)
);

-- Addresses constraints
ALTER TABLE addresses 
ADD CONSTRAINT check_coordinates_valid 
CHECK (
    (latitude IS NULL OR latitude BETWEEN -90 AND 90) AND
    (longitude IS NULL OR longitude BETWEEN -180 AND 180)
);

-- Delivery persons constraints
ALTER TABLE delivery_persons 
ADD CONSTRAINT check_capacity_positive 
CHECK (max_delivery_capacity > 0 AND max_delivery_capacity <= 50);

ALTER TABLE delivery_persons 
ADD CONSTRAINT check_performance_metrics_valid 
CHECK (
    total_deliveries >= 0 AND
    average_rating >= 0 AND average_rating <= 5 AND
    on_time_percentage >= 0 AND on_time_percentage <= 100
);

-- ============================================================================
-- 2. CREATE COMPREHENSIVE AUDIT TRAIL SYSTEM
-- ============================================================================

-- Create audit event types
CREATE TYPE audit_event_type AS ENUM (
    'CREATE', 'UPDATE', 'DELETE', 'LOGIN', 'LOGOUT', 'PASSWORD_CHANGE',
    'PERMISSION_CHANGE', 'STATUS_CHANGE', 'PAYMENT_PROCESSED', 'ORDER_PLACED',
    'INVENTORY_ADJUSTED', 'SECURITY_EVENT', 'DATA_EXPORT', 'CONFIGURATION_CHANGE'
);

CREATE TYPE audit_severity AS ENUM ('LOW', 'MEDIUM', 'HIGH', 'CRITICAL');

-- Main audit log table
CREATE TABLE audit_logs (
    id BIGSERIAL PRIMARY KEY,
    
    -- Event identification
    event_type audit_event_type NOT NULL,
    severity audit_severity DEFAULT 'MEDIUM',
    event_category VARCHAR(50) NOT NULL,
    event_action VARCHAR(100) NOT NULL,
    
    -- Entity information
    table_name VARCHAR(100),
    record_id VARCHAR(100),
    resource_type VARCHAR(100),
    resource_id VARCHAR(100),
    
    -- User and session information
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    session_id VARCHAR(100),
    ip_address INET,
    user_agent TEXT,
    
    -- Event details
    description TEXT,
    old_values JSONB,
    new_values JSONB,
    additional_data JSONB DEFAULT '{}',
    
    -- Request context
    endpoint VARCHAR(200),
    http_method VARCHAR(10),
    request_id VARCHAR(100),
    
    -- Timestamps
    event_timestamp TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Indexes for audit logs
CREATE INDEX idx_audit_logs_event_type ON audit_logs(event_type);
CREATE INDEX idx_audit_logs_severity ON audit_logs(severity);
CREATE INDEX idx_audit_logs_user_id ON audit_logs(user_id);
CREATE INDEX idx_audit_logs_table_name ON audit_logs(table_name);
CREATE INDEX idx_audit_logs_timestamp ON audit_logs(event_timestamp);
CREATE INDEX idx_audit_logs_ip_address ON audit_logs(ip_address);
CREATE INDEX idx_audit_logs_resource ON audit_logs(resource_type, resource_id);

-- Sensitive operations audit table
CREATE TABLE sensitive_operations_audit (
    id BIGSERIAL PRIMARY KEY,
    
    -- Operation details
    operation_type VARCHAR(100) NOT NULL,
    operation_description TEXT NOT NULL,
    
    -- User and authorization
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    user_role user_role,
    requires_approval BOOLEAN DEFAULT FALSE,
    approved_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    approved_at TIMESTAMP WITH TIME ZONE,
    
    -- Security context
    ip_address INET NOT NULL,
    user_agent TEXT,
    session_id VARCHAR(100),
    mfa_verified BOOLEAN DEFAULT FALSE,
    
    -- Risk assessment
    risk_level audit_severity DEFAULT 'MEDIUM',
    risk_factors JSONB DEFAULT '{}',
    
    -- Data affected
    affected_tables TEXT[],
    affected_records_count INTEGER DEFAULT 0,
    data_classification VARCHAR(50) DEFAULT 'internal', -- public, internal, confidential, restricted
    
    -- Operation result
    success BOOLEAN NOT NULL,
    error_message TEXT,
    execution_time_ms INTEGER,
    
    -- Additional context
    business_justification TEXT,
    additional_metadata JSONB DEFAULT '{}',
    
    -- Timestamps
    attempted_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP WITH TIME ZONE
);

-- Indexes for sensitive operations audit
CREATE INDEX idx_sensitive_ops_user_id ON sensitive_operations_audit(user_id);
CREATE INDEX idx_sensitive_ops_type ON sensitive_operations_audit(operation_type);
CREATE INDEX idx_sensitive_ops_risk_level ON sensitive_operations_audit(risk_level);
CREATE INDEX idx_sensitive_ops_attempted_at ON sensitive_operations_audit(attempted_at);
CREATE INDEX idx_sensitive_ops_success ON sensitive_operations_audit(success);

-- Failed login attempts audit
CREATE TABLE failed_login_audit (
    id BIGSERIAL PRIMARY KEY,
    
    -- Attempt details
    attempted_email VARCHAR(255),
    attempted_phone VARCHAR(20),
    attempted_telegram_id VARCHAR(50),
    
    -- Security context
    ip_address INET NOT NULL,
    user_agent TEXT,
    
    -- Failure details
    failure_reason VARCHAR(100) NOT NULL,
    attempt_count INTEGER DEFAULT 1,
    
    -- Risk indicators
    is_suspicious BOOLEAN DEFAULT FALSE,
    blocked_duration INTEGER, -- seconds
    
    -- Geographic information
    country_code VARCHAR(2),
    city VARCHAR(100),
    
    -- Additional context
    additional_data JSONB DEFAULT '{}',
    
    -- Timestamp
    attempted_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Indexes for failed login audit
CREATE INDEX idx_failed_login_ip ON failed_login_audit(ip_address);
CREATE INDEX idx_failed_login_email ON failed_login_audit(attempted_email);
CREATE INDEX idx_failed_login_attempted_at ON failed_login_audit(attempted_at);
CREATE INDEX idx_failed_login_suspicious ON failed_login_audit(is_suspicious);

-- Data access audit table
CREATE TABLE data_access_audit (
    id BIGSERIAL PRIMARY KEY,
    
    -- Access details
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    accessed_table VARCHAR(100) NOT NULL,
    accessed_record_id VARCHAR(100),
    access_type VARCHAR(20) NOT NULL, -- SELECT, INSERT, UPDATE, DELETE
    
    -- Query information
    query_hash VARCHAR(64), -- Hash of the executed query
    record_count INTEGER DEFAULT 0,
    
    -- Security context
    ip_address INET,
    user_agent TEXT,
    session_id VARCHAR(100),
    
    -- Data classification
    data_sensitivity VARCHAR(20) DEFAULT 'normal', -- low, normal, high, critical
    contains_pii BOOLEAN DEFAULT FALSE,
    contains_financial_data BOOLEAN DEFAULT FALSE,
    
    -- Access justification
    business_purpose VARCHAR(200),
    
    -- Timestamps
    accessed_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Indexes for data access audit
CREATE INDEX idx_data_access_user_id ON data_access_audit(user_id);
CREATE INDEX idx_data_access_table ON data_access_audit(accessed_table);
CREATE INDEX idx_data_access_type ON data_access_audit(access_type);
CREATE INDEX idx_data_access_timestamp ON data_access_audit(accessed_at);
CREATE INDEX idx_data_access_pii ON data_access_audit(contains_pii);

-- ============================================================================
-- 3. CREATE AUDIT TRIGGERS FOR CRITICAL TABLES
-- ============================================================================

-- Generic audit trigger function
CREATE OR REPLACE FUNCTION audit_trigger_function()
RETURNS TRIGGER AS $$
DECLARE
    old_data JSONB;
    new_data JSONB;
    event_type_val audit_event_type;
    severity_val audit_severity;
BEGIN
    -- Determine event type
    IF TG_OP = 'DELETE' THEN
        old_data = to_jsonb(OLD);
        new_data = NULL;
        event_type_val = 'DELETE';
        severity_val = 'HIGH';
    ELSIF TG_OP = 'UPDATE' THEN
        old_data = to_jsonb(OLD);
        new_data = to_jsonb(NEW);
        event_type_val = 'UPDATE';
        severity_val = 'MEDIUM';
    ELSIF TG_OP = 'INSERT' THEN
        old_data = NULL;
        new_data = to_jsonb(NEW);
        event_type_val = 'CREATE';
        severity_val = 'MEDIUM';
    END IF;
    
    -- Insert audit record
    INSERT INTO audit_logs (
        event_type,
        severity,
        event_category,
        event_action,
        table_name,
        record_id,
        old_values,
        new_values,
        description
    ) VALUES (
        event_type_val,
        severity_val,
        'DATABASE',
        TG_OP || '_' || TG_TABLE_NAME,
        TG_TABLE_NAME,
        COALESCE(
            CAST(NEW.id AS TEXT), 
            CAST(OLD.id AS TEXT)
        ),
        old_data,
        new_data,
        'Automated audit log for ' || TG_OP || ' operation on ' || TG_TABLE_NAME
    );
    
    -- Return appropriate record
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    ELSE
        RETURN NEW;
    END IF;
    
EXCEPTION
    WHEN OTHERS THEN
        -- Log error but don't fail the main operation
        RAISE WARNING 'Audit trigger failed: %', SQLERRM;
        IF TG_OP = 'DELETE' THEN
            RETURN OLD;
        ELSE
            RETURN NEW;
        END IF;
END;
$$ LANGUAGE plpgsql;

-- Apply audit triggers to critical tables
CREATE TRIGGER audit_users_trigger
    AFTER INSERT OR UPDATE OR DELETE ON users
    FOR EACH ROW EXECUTE FUNCTION audit_trigger_function();

CREATE TRIGGER audit_orders_trigger
    AFTER INSERT OR UPDATE OR DELETE ON orders
    FOR EACH ROW EXECUTE FUNCTION audit_trigger_function();

CREATE TRIGGER audit_payment_transactions_trigger
    AFTER INSERT OR UPDATE OR DELETE ON payment_transactions
    FOR EACH ROW EXECUTE FUNCTION audit_trigger_function();

CREATE TRIGGER audit_products_trigger
    AFTER INSERT OR UPDATE OR DELETE ON products
    FOR EACH ROW EXECUTE FUNCTION audit_trigger_function();

CREATE TRIGGER audit_subscriptions_trigger
    AFTER INSERT OR UPDATE OR DELETE ON subscriptions
    FOR EACH ROW EXECUTE FUNCTION audit_trigger_function();

CREATE TRIGGER audit_loyalty_transactions_trigger
    AFTER INSERT OR UPDATE OR DELETE ON loyalty_transactions
    FOR EACH ROW EXECUTE FUNCTION audit_trigger_function();

-- ============================================================================
-- 4. ADD ADDITIONAL FOREIGN KEY CONSTRAINTS WITH PROPER ACTIONS
-- ============================================================================

-- Add missing foreign key constraints with CASCADE/SET NULL as appropriate

-- Order status history should cascade when order is deleted
ALTER TABLE order_status_history 
DROP CONSTRAINT IF EXISTS order_status_history_order_id_fkey,
ADD CONSTRAINT order_status_history_order_id_fkey 
    FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE;

-- Subscription items should cascade when subscription is deleted
ALTER TABLE subscription_items 
DROP CONSTRAINT IF EXISTS subscription_items_subscription_id_fkey,
ADD CONSTRAINT subscription_items_subscription_id_fkey 
    FOREIGN KEY (subscription_id) REFERENCES subscriptions(id) ON DELETE CASCADE;

-- Loyalty transactions should set null when user is deleted (keep transaction history)
ALTER TABLE loyalty_transactions 
DROP CONSTRAINT IF EXISTS loyalty_transactions_user_id_fkey,
ADD CONSTRAINT loyalty_transactions_user_id_fkey 
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL;

-- ============================================================================
-- 5. CREATE SECURITY MONITORING VIEWS
-- ============================================================================

-- View for recent security events
CREATE OR REPLACE VIEW recent_security_events AS
SELECT 
    al.id,
    al.event_type,
    al.severity,
    al.event_action,
    al.user_id,
    u.email,
    u.role,
    al.ip_address,
    al.description,
    al.event_timestamp
FROM audit_logs al
LEFT JOIN users u ON al.user_id = u.id
WHERE al.severity IN ('HIGH', 'CRITICAL') 
   OR al.event_type IN ('SECURITY_EVENT', 'LOGIN', 'LOGOUT', 'PERMISSION_CHANGE')
ORDER BY al.event_timestamp DESC;

-- View for failed login attempts summary
CREATE OR REPLACE VIEW failed_login_summary AS
SELECT 
    ip_address,
    attempted_email,
    COUNT(*) as attempt_count,
    MAX(attempted_at) as last_attempt,
    COUNT(CASE WHEN is_suspicious THEN 1 END) as suspicious_attempts,
    ARRAY_AGG(DISTINCT failure_reason) as failure_reasons
FROM failed_login_audit 
WHERE attempted_at > CURRENT_TIMESTAMP - INTERVAL '24 hours'
GROUP BY ip_address, attempted_email
HAVING COUNT(*) >= 3
ORDER BY attempt_count DESC;

-- View for high-risk operations
CREATE OR REPLACE VIEW high_risk_operations AS
SELECT 
    soa.id,
    soa.operation_type,
    soa.operation_description,
    soa.user_id,
    u.email,
    u.role,
    soa.risk_level,
    soa.success,
    soa.attempted_at,
    soa.affected_records_count,
    soa.data_classification
FROM sensitive_operations_audit soa
LEFT JOIN users u ON soa.user_id = u.id
WHERE soa.risk_level IN ('HIGH', 'CRITICAL')
   OR soa.success = FALSE
   OR soa.affected_records_count > 100
ORDER BY soa.attempted_at DESC;

-- ============================================================================
-- 6. CREATE AUDIT TRAIL CLEANUP FUNCTION
-- ============================================================================

-- Function to clean up old audit records
CREATE OR REPLACE FUNCTION cleanup_audit_logs(retention_days INTEGER DEFAULT 365)
RETURNS INTEGER AS $$
DECLARE
    deleted_count INTEGER;
    cutoff_date TIMESTAMP WITH TIME ZONE;
BEGIN
    cutoff_date := CURRENT_TIMESTAMP - (retention_days || ' days')::INTERVAL;
    
    -- Delete old audit logs (except CRITICAL severity)
    DELETE FROM audit_logs 
    WHERE event_timestamp < cutoff_date 
      AND severity != 'CRITICAL';
    
    GET DIAGNOSTICS deleted_count = ROW_COUNT;
    
    -- Delete old failed login attempts
    DELETE FROM failed_login_audit 
    WHERE attempted_at < cutoff_date;
    
    -- Delete old data access audit (keep only 90 days for normal sensitivity)
    DELETE FROM data_access_audit 
    WHERE accessed_at < (CURRENT_TIMESTAMP - INTERVAL '90 days')
      AND data_sensitivity = 'normal';
    
    RETURN deleted_count;
END;
$$ LANGUAGE plpgsql;

-- ============================================================================
-- 7. ADD PERFORMANCE MONITORING
-- ============================================================================

-- Create table for query performance monitoring
CREATE TABLE query_performance_log (
    id BIGSERIAL PRIMARY KEY,
    query_hash VARCHAR(64) NOT NULL,
    query_text TEXT,
    execution_time_ms INTEGER NOT NULL,
    rows_examined INTEGER,
    rows_returned INTEGER,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    endpoint VARCHAR(200),
    slow_query BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Index for query performance
CREATE INDEX idx_query_perf_hash ON query_performance_log(query_hash);
CREATE INDEX idx_query_perf_slow ON query_performance_log(slow_query);
CREATE INDEX idx_query_perf_time ON query_performance_log(execution_time_ms);
CREATE INDEX idx_query_perf_created_at ON query_performance_log(created_at);

-- ============================================================================
-- 8. ADD COMMENTS FOR DOCUMENTATION
-- ============================================================================

COMMENT ON TABLE audit_logs IS 'Comprehensive audit trail for all significant system events';
COMMENT ON TABLE sensitive_operations_audit IS 'Detailed audit trail for sensitive business operations';
COMMENT ON TABLE failed_login_audit IS 'Security audit trail for failed authentication attempts';
COMMENT ON TABLE data_access_audit IS 'Audit trail for data access and privacy compliance';
COMMENT ON TABLE query_performance_log IS 'Performance monitoring for database queries';

COMMENT ON FUNCTION audit_trigger_function() IS 'Generic trigger function for automated audit logging';
COMMENT ON FUNCTION cleanup_audit_logs(INTEGER) IS 'Cleanup function for maintaining audit log retention policy';

COMMENT ON VIEW recent_security_events IS 'Recent high-severity security events for monitoring dashboard';
COMMENT ON VIEW failed_login_summary IS 'Summary of failed login attempts for security analysis';
COMMENT ON VIEW high_risk_operations IS 'High-risk operations requiring additional scrutiny';

-- ============================================================================
-- SCRIPT COMPLETION MESSAGE
-- ============================================================================

DO $$
BEGIN
    RAISE NOTICE '=============================================================';
    RAISE NOTICE 'Database schema constraints and audit trails successfully added!';
    RAISE NOTICE '';
    RAISE NOTICE 'Added features:';
    RAISE NOTICE '• Check constraints for data integrity validation';
    RAISE NOTICE '• Comprehensive audit trail system with multiple tables';
    RAISE NOTICE '• Automatic audit triggers for critical tables';
    RAISE NOTICE '• Enhanced foreign key constraints with proper actions';
    RAISE NOTICE '• Security monitoring views for real-time analysis';
    RAISE NOTICE '• Performance monitoring capabilities';
    RAISE NOTICE '• Audit log cleanup and retention management';
    RAISE NOTICE '';
    RAISE NOTICE 'Security improvements:';
    RAISE NOTICE '• Failed login attempt tracking';
    RAISE NOTICE '• Sensitive operations audit trail';
    RAISE NOTICE '• Data access monitoring for compliance';
    RAISE NOTICE '• User behavior tracking for security analysis';
    RAISE NOTICE '';
    RAISE NOTICE 'Next steps:';
    RAISE NOTICE '1. Review and test all new constraints';
    RAISE NOTICE '2. Update application code to handle constraint violations';
    RAISE NOTICE '3. Set up monitoring alerts for security events';
    RAISE NOTICE '4. Configure audit log retention policies';
    RAISE NOTICE '5. Train staff on new audit capabilities';
    RAISE NOTICE '=============================================================';
END $$;