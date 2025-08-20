-- Additional database indexes for BlueStream platform
-- Critical performance indexes identified during security and performance audit
-- Run this script after the main schema.sql to add additional optimization indexes

-- JWT Token Blacklist indexes (if table exists)
-- These are critical for JWT token validation performance
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'jwt_blacklist') THEN
        -- Index for token lookups during JWT validation
        CREATE INDEX IF NOT EXISTS idx_jwt_blacklist_token ON jwt_blacklist(token);
        CREATE INDEX IF NOT EXISTS idx_jwt_blacklist_expires_at ON jwt_blacklist(expires_at);
        CREATE INDEX IF NOT EXISTS idx_jwt_blacklist_user_id ON jwt_blacklist(user_id);
    END IF;
END
$$;

-- Payment gateway specific indexes
-- Critical for payment processing and webhook validation
CREATE INDEX IF NOT EXISTS idx_payment_transactions_gateway ON payment_transactions(payment_gateway);
CREATE INDEX IF NOT EXISTS idx_payment_transactions_gateway_id ON payment_transactions(gateway_transaction_id);
CREATE INDEX IF NOT EXISTS idx_payment_transactions_reference ON payment_transactions(external_reference);

-- Webhook processing indexes (if webhook tables exist)
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'webhook_events') THEN
        CREATE INDEX IF NOT EXISTS idx_webhook_events_status ON webhook_events(status);
        CREATE INDEX IF NOT EXISTS idx_webhook_events_created_at ON webhook_events(created_at);
        CREATE INDEX IF NOT EXISTS idx_webhook_events_source ON webhook_events(source);
    END IF;
END
$$;

-- User session and authentication indexes
CREATE INDEX IF NOT EXISTS idx_users_last_login ON users(last_login_at);
CREATE INDEX IF NOT EXISTS idx_users_email_verified ON users(email_verified);
CREATE INDEX IF NOT EXISTS idx_users_phone_verified ON users(phone_verified);

-- Order analytics specific indexes
-- These improve performance for analytics queries significantly
CREATE INDEX IF NOT EXISTS idx_orders_status_date ON orders(status, created_at);
CREATE INDEX IF NOT EXISTS idx_orders_user_date_status ON orders(user_id, created_at, status);
CREATE INDEX IF NOT EXISTS idx_orders_delivery_date_status ON orders(delivery_date, status);

-- Payment analytics indexes
CREATE INDEX IF NOT EXISTS idx_payment_transactions_status_date ON payment_transactions(status, created_at);
CREATE INDEX IF NOT EXISTS idx_payment_transactions_gateway_status ON payment_transactions(payment_gateway, status);

-- Delivery performance indexes
CREATE INDEX IF NOT EXISTS idx_deliveries_status_date ON deliveries(status, scheduled_delivery);
CREATE INDEX IF NOT EXISTS idx_deliveries_driver_status ON deliveries(delivery_person_id, status);

-- Subscription management indexes
CREATE INDEX IF NOT EXISTS idx_subscriptions_status_next_delivery ON subscriptions(status, next_delivery_date);
CREATE INDEX IF NOT EXISTS idx_subscriptions_user_status ON subscriptions(user_id, status);

-- Loyalty program performance indexes
CREATE INDEX IF NOT EXISTS idx_loyalty_transactions_type_date ON loyalty_transactions(transaction_type, created_at);
CREATE INDEX IF NOT EXISTS idx_loyalty_transactions_user_type ON loyalty_transactions(user_id, transaction_type);
CREATE INDEX IF NOT EXISTS idx_loyalty_points_user_active ON loyalty_points(user_id, is_active);

-- Product search and filtering indexes
CREATE INDEX IF NOT EXISTS idx_products_active_featured ON products(is_active, is_featured);
CREATE INDEX IF NOT EXISTS idx_products_category_active ON products(category_id, is_active);
CREATE INDEX IF NOT EXISTS idx_products_price_range ON products(base_price) WHERE is_active = true;

-- Analytics and reporting indexes
CREATE INDEX IF NOT EXISTS idx_user_events_type_date ON user_events(event_type, created_at);
CREATE INDEX IF NOT EXISTS idx_product_views_product_date ON product_views(product_id, created_at);
CREATE INDEX IF NOT EXISTS idx_search_queries_text_date ON search_queries(query_text, created_at);

-- Notification performance indexes
CREATE INDEX IF NOT EXISTS idx_notifications_user_read ON notifications(user_id, is_read);
CREATE INDEX IF NOT EXISTS idx_notifications_type_status ON notifications(type, status);
CREATE INDEX IF NOT EXISTS idx_notifications_channel_status ON notifications(channel, status);

-- Geographic and delivery area indexes
CREATE INDEX IF NOT EXISTS idx_orders_delivery_city ON orders(delivery_address_city);
CREATE INDEX IF NOT EXISTS idx_users_city ON users(city);

-- Push notification token indexes (if table exists)
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'push_notification_tokens') THEN
        CREATE INDEX IF NOT EXISTS idx_push_tokens_user_active ON push_notification_tokens(user_id, is_active);
        CREATE INDEX IF NOT EXISTS idx_push_tokens_platform ON push_notification_tokens(platform);
        CREATE INDEX IF NOT EXISTS idx_push_tokens_token ON push_notification_tokens(token);
    END IF;
END
$$;

-- Audit trail indexes (if audit tables exist)
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'audit_logs') THEN
        CREATE INDEX IF NOT EXISTS idx_audit_logs_table_action ON audit_logs(table_name, action);
        CREATE INDEX IF NOT EXISTS idx_audit_logs_user_date ON audit_logs(user_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_audit_logs_date ON audit_logs(created_at);
    END IF;
END
$$;

-- Performance optimization indexes for common query patterns
-- Orders with items for reporting
CREATE INDEX IF NOT EXISTS idx_order_items_order_product ON order_items(order_id, product_id);

-- User activity tracking
CREATE INDEX IF NOT EXISTS idx_user_behavior_user_date ON user_behavior(user_id, timestamp);

-- Revenue and sales analytics
CREATE INDEX IF NOT EXISTS idx_orders_total_date ON orders(total_amount, created_at) WHERE status != 'cancelled';

-- Delivery time analytics
CREATE INDEX IF NOT EXISTS idx_deliveries_scheduled_actual ON deliveries(scheduled_delivery, actual_delivery_time);

-- Customer segmentation indexes
CREATE INDEX IF NOT EXISTS idx_users_created_active ON users(created_at, is_active);
CREATE INDEX IF NOT EXISTS idx_orders_user_total ON orders(user_id, total_amount);

-- Add comments for documentation
COMMENT ON INDEX idx_payment_transactions_gateway IS 'Index for payment gateway filtering and reporting';
COMMENT ON INDEX idx_orders_status_date IS 'Composite index for order status analytics';
COMMENT ON INDEX idx_notifications_user_read IS 'Index for unread notification queries';
COMMENT ON INDEX idx_loyalty_transactions_type_date IS 'Index for loyalty transaction analytics';
COMMENT ON INDEX idx_products_active_featured IS 'Index for product listing with active/featured filters';

-- Create partial indexes for common filtered queries
CREATE INDEX IF NOT EXISTS idx_orders_active_recent 
    ON orders(created_at) 
    WHERE status IN ('pending', 'confirmed', 'preparing', 'out_for_delivery');

CREATE INDEX IF NOT EXISTS idx_notifications_unread 
    ON notifications(user_id, created_at) 
    WHERE is_read = false;

CREATE INDEX IF NOT EXISTS idx_users_active_customers 
    ON users(created_at, last_login_at) 
    WHERE role = 'customer' AND status = 'active';

-- Analytics query optimization indexes
CREATE INDEX IF NOT EXISTS idx_orders_analytics_composite 
    ON orders(created_at, status, total_amount, user_id);

CREATE INDEX IF NOT EXISTS idx_payment_analytics_composite 
    ON payment_transactions(created_at, status, amount, payment_gateway);

COMMENT ON INDEX idx_orders_active_recent IS 'Partial index for active orders only';
COMMENT ON INDEX idx_notifications_unread IS 'Partial index for unread notifications only';
COMMENT ON INDEX idx_users_active_customers IS 'Partial index for active customer analytics';

-- Analyze tables after index creation for better query planning
ANALYZE users;
ANALYZE orders;
ANALYZE order_items;
ANALYZE payment_transactions;
ANALYZE deliveries;
ANALYZE notifications;
ANALYZE products;
ANALYZE loyalty_transactions;
ANALYZE user_events;