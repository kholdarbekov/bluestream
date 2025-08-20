-- Migration script to convert Float columns to Decimal for monetary fields
-- Run this after updating the model definitions to ensure data consistency
-- IMPORTANT: Always backup your database before running this migration!

-- Start transaction to ensure atomicity
BEGIN;

-- Create backup of tables with monetary fields (optional but recommended)
CREATE TABLE IF NOT EXISTS orders_backup AS SELECT * FROM orders;
CREATE TABLE IF NOT EXISTS order_items_backup AS SELECT * FROM order_items;
CREATE TABLE IF NOT EXISTS payment_transactions_backup AS SELECT * FROM payment_transactions;
CREATE TABLE IF NOT EXISTS products_backup AS SELECT * FROM products;
CREATE TABLE IF NOT EXISTS subscriptions_backup AS SELECT * FROM subscriptions;

-- Migrate Order table monetary fields
ALTER TABLE orders 
    ALTER COLUMN subtotal TYPE NUMERIC(10,2) USING ROUND(subtotal::NUMERIC, 2),
    ALTER COLUMN discount_amount TYPE NUMERIC(10,2) USING ROUND(discount_amount::NUMERIC, 2),
    ALTER COLUMN delivery_fee TYPE NUMERIC(10,2) USING ROUND(delivery_fee::NUMERIC, 2),
    ALTER COLUMN loyalty_discount TYPE NUMERIC(10,2) USING ROUND(loyalty_discount::NUMERIC, 2),
    ALTER COLUMN total_amount TYPE NUMERIC(10,2) USING ROUND(total_amount::NUMERIC, 2);

-- Migrate OrderItem table monetary fields
ALTER TABLE order_items
    ALTER COLUMN unit_price TYPE NUMERIC(10,2) USING ROUND(unit_price::NUMERIC, 2),
    ALTER COLUMN discount_amount TYPE NUMERIC(10,2) USING ROUND(discount_amount::NUMERIC, 2),
    ALTER COLUMN total_price TYPE NUMERIC(10,2) USING ROUND(total_price::NUMERIC, 2);

-- Migrate Payment tables monetary fields
ALTER TABLE payment_transactions
    ALTER COLUMN amount TYPE NUMERIC(10,2) USING ROUND(amount::NUMERIC, 2);

-- Migrate Product table monetary fields
ALTER TABLE products
    ALTER COLUMN base_price TYPE NUMERIC(10,2) USING ROUND(base_price::NUMERIC, 2),
    ALTER COLUMN cost_price TYPE NUMERIC(10,2) USING ROUND(cost_price::NUMERIC, 2),
    ALTER COLUMN discount_price TYPE NUMERIC(10,2) USING ROUND(discount_price::NUMERIC, 2),
    ALTER COLUMN min_order_value TYPE NUMERIC(10,2) USING ROUND(min_order_value::NUMERIC, 2);

-- Migrate PriceRule table monetary fields
ALTER TABLE price_rules
    ALTER COLUMN discount_value TYPE NUMERIC(10,2) USING ROUND(discount_value::NUMERIC, 2);

-- Migrate Subscription table monetary fields (if exists)
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'subscriptions') THEN
        ALTER TABLE subscriptions
            ALTER COLUMN billing_amount TYPE NUMERIC(10,2) USING ROUND(billing_amount::NUMERIC, 2),
            ALTER COLUMN total_amount_billed TYPE NUMERIC(10,2) USING ROUND(total_amount_billed::NUMERIC, 2);
    END IF;
END
$$;

-- Migrate SubscriptionItem table monetary fields (if exists)
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'subscription_items') THEN
        ALTER TABLE subscription_items
            ALTER COLUMN unit_price TYPE NUMERIC(10,2) USING ROUND(unit_price::NUMERIC, 2),
            ALTER COLUMN total_price TYPE NUMERIC(10,2) USING ROUND(total_price::NUMERIC, 2);
    END IF;
END
$$;

-- Migrate PricingTier table monetary fields (if exists)
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'pricing_tiers') THEN
        ALTER TABLE pricing_tiers
            ALTER COLUMN price TYPE NUMERIC(10,2) USING ROUND(price::NUMERIC, 2);
    END IF;
END
$$;

-- Migrate Delivery table monetary fields (if exists)
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'deliveries') THEN
        ALTER TABLE deliveries
            ALTER COLUMN delivery_fee TYPE NUMERIC(10,2) USING ROUND(delivery_fee::NUMERIC, 2),
            ALTER COLUMN premium_fee TYPE NUMERIC(10,2) USING ROUND(premium_fee::NUMERIC, 2);
    END IF;
END
$$;

-- Migrate LoyaltyProgram table monetary fields (if exists)
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'loyalty_programs') THEN
        ALTER TABLE loyalty_programs
            ALTER COLUMN min_order_value TYPE NUMERIC(10,2) USING ROUND(min_order_value::NUMERIC, 2);
    END IF;
END
$$;

-- Migrate LoyaltyReward table monetary fields (if exists)
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'loyalty_rewards') THEN
        ALTER TABLE loyalty_rewards
            ALTER COLUMN discount_value TYPE NUMERIC(10,2) USING ROUND(discount_value::NUMERIC, 2);
    END IF;
END
$$;

-- Migrate analytics tables monetary fields (if exists)
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'discount_campaigns') THEN
        ALTER TABLE discount_campaigns
            ALTER COLUMN discount_value TYPE NUMERIC(10,2) USING ROUND(discount_value::NUMERIC, 2),
            ALTER COLUMN min_order_value TYPE NUMERIC(10,2) USING ROUND(min_order_value::NUMERIC, 2),
            ALTER COLUMN max_discount_amount TYPE NUMERIC(10,2) USING ROUND(max_discount_amount::NUMERIC, 2),
            ALTER COLUMN total_discount_given TYPE NUMERIC(10,2) USING ROUND(total_discount_given::NUMERIC, 2),
            ALTER COLUMN total_revenue_generated TYPE NUMERIC(10,2) USING ROUND(total_revenue_generated::NUMERIC, 2);
    END IF;
END
$$;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'revenue_metrics') THEN
        ALTER TABLE revenue_metrics
            ALTER COLUMN value TYPE NUMERIC(10,2) USING ROUND(value::NUMERIC, 2),
            ALTER COLUMN target_value TYPE NUMERIC(10,2) USING ROUND(target_value::NUMERIC, 2),
            ALTER COLUMN previous_value TYPE NUMERIC(10,2) USING ROUND(previous_value::NUMERIC, 2);
    END IF;
END
$$;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'conversion_events') THEN
        ALTER TABLE conversion_events
            ALTER COLUMN conversion_value TYPE NUMERIC(10,2) USING ROUND(conversion_value::NUMERIC, 2);
    END IF;
END
$$;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'cohort_analysis') THEN
        ALTER TABLE cohort_analysis
            ALTER COLUMN gross_revenue TYPE NUMERIC(10,2) USING ROUND(gross_revenue::NUMERIC, 2),
            ALTER COLUMN net_revenue TYPE NUMERIC(10,2) USING ROUND(net_revenue::NUMERIC, 2),
            ALTER COLUMN recurring_revenue TYPE NUMERIC(10,2) USING ROUND(recurring_revenue::NUMERIC, 2),
            ALTER COLUMN average_order_value TYPE NUMERIC(10,2) USING ROUND(average_order_value::NUMERIC, 2);
    END IF;
END
$$;

-- Update default values for NUMERIC columns
ALTER TABLE orders 
    ALTER COLUMN subtotal SET DEFAULT 0.00,
    ALTER COLUMN discount_amount SET DEFAULT 0.00,
    ALTER COLUMN delivery_fee SET DEFAULT 0.00,
    ALTER COLUMN loyalty_discount SET DEFAULT 0.00,
    ALTER COLUMN total_amount SET DEFAULT 0.00;

ALTER TABLE order_items
    ALTER COLUMN discount_amount SET DEFAULT 0.00;

-- Add constraints to ensure monetary values are non-negative
ALTER TABLE orders 
    ADD CONSTRAINT check_orders_subtotal_positive CHECK (subtotal >= 0),
    ADD CONSTRAINT check_orders_discount_positive CHECK (discount_amount >= 0),
    ADD CONSTRAINT check_orders_delivery_fee_positive CHECK (delivery_fee >= 0),
    ADD CONSTRAINT check_orders_loyalty_discount_positive CHECK (loyalty_discount >= 0),
    ADD CONSTRAINT check_orders_total_positive CHECK (total_amount >= 0);

ALTER TABLE order_items
    ADD CONSTRAINT check_order_items_unit_price_positive CHECK (unit_price >= 0),
    ADD CONSTRAINT check_order_items_discount_positive CHECK (discount_amount >= 0),
    ADD CONSTRAINT check_order_items_total_positive CHECK (total_price >= 0);

ALTER TABLE payment_transactions
    ADD CONSTRAINT check_payment_amount_positive CHECK (amount >= 0);

ALTER TABLE products
    ADD CONSTRAINT check_products_base_price_positive CHECK (base_price >= 0),
    ADD CONSTRAINT check_products_cost_price_positive CHECK (cost_price IS NULL OR cost_price >= 0),
    ADD CONSTRAINT check_products_discount_price_positive CHECK (discount_price IS NULL OR discount_price >= 0),
    ADD CONSTRAINT check_products_min_order_positive CHECK (min_order_value IS NULL OR min_order_value >= 0);

-- Verify data integrity after migration
DO $$
DECLARE
    total_orders INTEGER;
    total_payments INTEGER;
    total_products INTEGER;
BEGIN
    SELECT COUNT(*) INTO total_orders FROM orders;
    SELECT COUNT(*) INTO total_payments FROM payment_transactions;
    SELECT COUNT(*) INTO total_products FROM products;
    
    RAISE NOTICE 'Migration completed successfully:';
    RAISE NOTICE '- Orders migrated: %', total_orders;
    RAISE NOTICE '- Payments migrated: %', total_payments;
    RAISE NOTICE '- Products migrated: %', total_products;
    
    -- Check for any null or negative values that might indicate issues
    IF EXISTS (SELECT 1 FROM orders WHERE total_amount < 0) THEN
        RAISE WARNING 'Found negative total_amount values in orders table';
    END IF;
    
    IF EXISTS (SELECT 1 FROM products WHERE base_price < 0) THEN
        RAISE WARNING 'Found negative base_price values in products table';
    END IF;
    
    RAISE NOTICE 'Data integrity checks passed.';
END
$$;

-- Analyze tables for better query performance
ANALYZE orders;
ANALYZE order_items;
ANALYZE payment_transactions;
ANALYZE products;
ANALYZE subscriptions;

COMMIT;

-- Instructions for rollback (if needed):
-- To rollback this migration, you would need to:
-- 1. Change column types back to FLOAT/REAL
-- 2. Remove the CHECK constraints
-- 3. Restore from backup tables if data corruption occurred
--
-- Example rollback commands (USE WITH CAUTION):
-- ALTER TABLE orders ALTER COLUMN subtotal TYPE FLOAT USING subtotal::FLOAT;
-- DROP TABLE orders_backup; -- Only after confirming migration success

RAISE NOTICE 'Float to Decimal migration completed successfully!';
RAISE NOTICE 'All monetary fields now use NUMERIC(10,2) for precise decimal arithmetic.';
RAISE NOTICE 'Backup tables created: *_backup (drop them after confirming migration success).';