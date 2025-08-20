-- PostgreSQL 17 Database Schema for Water Business Platform
-- Generated for comprehensive water delivery business management system
-- Includes user management, products, orders, payments, delivery, subscriptions, loyalty, notifications, analytics, reviews, and translations
-- Optimized for PostgreSQL 17 features

-- Create database extensions (PostgreSQL 17 compatible)
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pgcrypto";
CREATE EXTENSION IF NOT EXISTS "pg_trgm"; -- For full-text search
CREATE EXTENSION IF NOT EXISTS "btree_gin"; -- For GIN indexes on multiple columns
CREATE EXTENSION IF NOT EXISTS "pg_stat_statements"; -- For query performance monitoring

-- Create custom enum types
CREATE TYPE order_status AS ENUM (
    'pending', 'confirmed', 'preparing', 'out_for_delivery', 
    'delivered', 'cancelled', 'returned'
);

CREATE TYPE payment_status AS ENUM (
    'pending', 'processing', 'completed', 'failed', 
    'cancelled', 'refunded', 'partially_refunded'
);

CREATE TYPE payment_method AS ENUM (
    'cash', 'card', 'payme', 'click', 'loyalty_points', 'business_account'
);

CREATE TYPE payment_method_type AS ENUM (
    'instant', 'card_payment', 'digital_wallet', 'points', 'account_balance'
);

CREATE TYPE delivery_status AS ENUM (
    'scheduled', 'pending', 'assigned', 'picked_up', 
    'in_transit', 'arrived', 'delivered', 'failed', 'returned'
);

CREATE TYPE subscription_status AS ENUM (
    'active', 'paused', 'cancelled', 'expired', 'trial'
);

CREATE TYPE subscription_frequency AS ENUM (
    'daily', 'weekly', 'biweekly', 'monthly'
);

CREATE TYPE user_role AS ENUM (
    'customer', 'admin', 'manager', 'delivery_driver', 'operator'
);

CREATE TYPE user_status AS ENUM (
    'active', 'inactive', 'banned', 'pending_verification'
);

CREATE TYPE notification_type AS ENUM (
    'order_confirmation', 'order_status_update', 'delivery_update',
    'payment_confirmation', 'subscription_reminder', 'promotional',
    'system_alert', 'loyalty_reward', 'order_update', 'delivery_reminder',
    'promotional_offer', 'system_announcement', 'subscription_renewal'
);

CREATE TYPE notification_channel AS ENUM (
    'email', 'sms', 'telegram', 'push', 'in_app'
);

CREATE TYPE notification_status AS ENUM (
    'pending', 'sent', 'delivered', 'failed', 'read'
);

CREATE TYPE price_rule_type AS ENUM (
    'bulk_discount', 'vip_discount', 'seasonal_discount', 'promotional_discount'
);

CREATE TYPE product_size AS ENUM (
    '0.5L', '1L', '1.5L', '5L', '19L'
);

CREATE TYPE priority AS ENUM (
    'low', 'normal', 'high', 'urgent'
);

CREATE TYPE delivery_type AS ENUM (
    'standard', 'express', 'scheduled', 'emergency'
);

CREATE TYPE discount_type AS ENUM (
    'percentage', 'fixed_amount', 'free_delivery', 'bogo'
);

CREATE TYPE loyalty_action_type AS ENUM (
    'purchase', 'referral', 'review', 'social_share', 
    'birthday_bonus', 'welcome_bonus'
);

CREATE TYPE loyalty_transaction_type AS ENUM (
    'earned', 'redeemed', 'expired', 'bonus', 'adjustment'
);

CREATE TYPE reward_status AS ENUM (
    'available', 'claimed', 'expired', 'used', 'cancelled'
);

CREATE TYPE price_rule_type AS ENUM (
    'bulk_discount', 'vip_discount', 'loyalty_discount', 
    'seasonal_discount', 'time_based'
);

CREATE TYPE file_type AS ENUM (
    'image', 'document', 'video', 'audio'
);

CREATE TYPE log_level AS ENUM (
    'debug', 'info', 'warning', 'error', 'critical'
);

-- Core tables

-- Users table
CREATE TABLE users (
    id SERIAL PRIMARY KEY,
    email VARCHAR(255) UNIQUE NOT NULL,
    phone VARCHAR(20) UNIQUE,
    password_hash VARCHAR(255) NOT NULL,
    telegram_id VARCHAR(50) UNIQUE,
    
    -- Personal information
    first_name VARCHAR(100),
    last_name VARCHAR(100),
    full_name VARCHAR(200),
    date_of_birth DATE,
    gender VARCHAR(10),
    
    -- Account status
    role user_role DEFAULT 'customer',
    status user_status DEFAULT 'active',
    is_verified BOOLEAN DEFAULT FALSE,
    is_premium BOOLEAN DEFAULT FALSE,
    
    -- Preferences
    preferred_language VARCHAR(5) DEFAULT 'en',
    preferred_currency VARCHAR(3) DEFAULT 'UZS',
    timezone VARCHAR(50) DEFAULT 'Asia/Tashkent',
    registration_source VARCHAR(50) DEFAULT 'web',
    
    -- Contact preferences
    email_notifications BOOLEAN DEFAULT TRUE,
    sms_notifications BOOLEAN DEFAULT TRUE,
    push_notifications BOOLEAN DEFAULT TRUE,
    
    -- Business account fields
    company_name VARCHAR(200),
    tax_id VARCHAR(50),
    business_type VARCHAR(50),
    
    -- Security
    last_login TIMESTAMP WITH TIME ZONE,
    failed_login_attempts INTEGER DEFAULT 0,
    account_locked_until TIMESTAMP WITH TIME ZONE,
    password_reset_token VARCHAR(255),
    password_reset_expires TIMESTAMP WITH TIME ZONE,
    email_verification_token VARCHAR(255),
    email_verified_at TIMESTAMP WITH TIME ZONE,
    
    -- Tracking
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Addresses table
CREATE TABLE addresses (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    
    -- Address details
    title VARCHAR(100),
    full_address TEXT NOT NULL,
    street_address VARCHAR(255),
    city VARCHAR(100) DEFAULT 'Tashkent',
    district VARCHAR(100),
    postal_code VARCHAR(20),
    country VARCHAR(100) DEFAULT 'Uzbekistan',
    
    -- Geographic coordinates
    latitude DECIMAL(10, 8),
    longitude DECIMAL(11, 8),
    
    -- Address metadata
    is_default BOOLEAN DEFAULT FALSE,
    is_business BOOLEAN DEFAULT FALSE,
    delivery_instructions TEXT,
    landmark VARCHAR(255),
    floor_number VARCHAR(20),
    apartment_number VARCHAR(20),
    
    -- Timestamps
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Product categories table
CREATE TABLE product_categories (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    name_ru VARCHAR(100),
    name_en VARCHAR(100),
    description TEXT,
    description_ru TEXT,
    description_en TEXT,
    is_active BOOLEAN DEFAULT TRUE,
    sort_order INTEGER DEFAULT 0,
    icon_url VARCHAR(255),
    
    -- Timestamps
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Products table
CREATE TABLE products (
    id SERIAL PRIMARY KEY,
    name VARCHAR(200) NOT NULL,
    name_ru VARCHAR(200),
    name_en VARCHAR(200),
    description TEXT,
    description_ru TEXT,
    description_en TEXT,
    sku VARCHAR(50) UNIQUE NOT NULL,
    
    -- Pricing
    base_price DECIMAL(10,2) NOT NULL,
    current_price DECIMAL(10,2) NOT NULL,
    
    -- Product details
    volume_liters DECIMAL(5,2) NOT NULL,
    category_id INTEGER REFERENCES product_categories(id) NOT NULL,
    is_active BOOLEAN DEFAULT TRUE,
    is_featured BOOLEAN DEFAULT FALSE,
    
    -- Inventory
    stock_quantity INTEGER DEFAULT 0,
    min_stock_level INTEGER DEFAULT 10,
    max_order_quantity INTEGER DEFAULT 100,
    
    -- Media
    image_urls JSON DEFAULT '[]',
    
    -- Product specifications
    specifications JSON DEFAULT '{}',
    
    -- SEO and metadata
    meta_title VARCHAR(255),
    meta_description TEXT,
    tags JSON DEFAULT '[]',
    
    -- Analytics
    total_sold INTEGER DEFAULT 0,
    view_count INTEGER DEFAULT 0,
    average_rating DECIMAL(3,2) DEFAULT 0.00,
    review_count INTEGER DEFAULT 0,
    
    -- Timestamps
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Price rules table
CREATE TABLE price_rules (
    id SERIAL PRIMARY KEY,
    product_id INTEGER REFERENCES products(id) ON DELETE CASCADE,
    rule_type price_rule_type NOT NULL,
    name VARCHAR(100) NOT NULL,
    description TEXT,
    
    -- Rule conditions
    min_quantity INTEGER DEFAULT 1,
    max_quantity INTEGER,
    min_order_value DECIMAL(10,2),
    customer_type VARCHAR(50),
    
    -- Discount details
    discount_type VARCHAR(20) DEFAULT 'percentage',
    discount_value DECIMAL(10,2) NOT NULL,
    
    -- Validity
    is_active BOOLEAN DEFAULT TRUE,
    valid_from TIMESTAMP WITH TIME ZONE,
    valid_until TIMESTAMP WITH TIME ZONE,
    
    -- Timestamps
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Orders table
CREATE TABLE orders (
    id SERIAL PRIMARY KEY,
    order_number VARCHAR(50) UNIQUE NOT NULL,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    
    -- Order status
    status order_status DEFAULT 'pending',
    priority priority DEFAULT 'normal',
    order_source VARCHAR(20) DEFAULT 'web',
    
    -- Amounts
    subtotal DECIMAL(10,2) NOT NULL DEFAULT 0.00,
    discount_amount DECIMAL(10,2) DEFAULT 0.00,
    delivery_fee DECIMAL(10,2) DEFAULT 0.00,
    loyalty_discount DECIMAL(10,2) DEFAULT 0.00,
    total_amount DECIMAL(10,2) NOT NULL DEFAULT 0.00,
    
    -- Delivery information
    delivery_address_id INTEGER REFERENCES addresses(id),
    delivery_date TIMESTAMP WITH TIME ZONE,
    delivery_time_slot VARCHAR(20),
    delivery_notes TEXT,
    is_urgent BOOLEAN DEFAULT FALSE,
    
    -- Payment
    payment_method payment_method,
    is_paid BOOLEAN DEFAULT FALSE,
    paid_at TIMESTAMP WITH TIME ZONE,
    
    -- Special fields
    is_subscription_order BOOLEAN DEFAULT FALSE,
    subscription_id INTEGER REFERENCES subscriptions(id),
    loyalty_points_used INTEGER DEFAULT 0,
    loyalty_points_earned INTEGER DEFAULT 0,
    
    -- Tracking
    tracking_number VARCHAR(100),
    
    -- Applied discounts/promotions
    applied_promotions JSON DEFAULT '[]',
    
    -- Timestamps
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Order items table
CREATE TABLE order_items (
    id SERIAL PRIMARY KEY,
    order_id INTEGER REFERENCES orders(id) ON DELETE CASCADE,
    product_id INTEGER REFERENCES products(id) ON DELETE SET NULL,
    
    -- Item details
    quantity INTEGER NOT NULL,
    unit_price DECIMAL(10,2) NOT NULL,
    discount_amount DECIMAL(10,2) DEFAULT 0.00,
    total_price DECIMAL(10,2) NOT NULL,
    
    -- Additional details
    product_name VARCHAR(200) NOT NULL,
    product_sku VARCHAR(50) NOT NULL,
    
    -- Timestamps
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Order status history
CREATE TABLE order_status_history (
    id SERIAL PRIMARY KEY,
    order_id INTEGER REFERENCES orders(id) ON DELETE CASCADE,
    old_status order_status NOT NULL,
    new_status order_status NOT NULL,
    changed_by INTEGER REFERENCES users(id),
    changed_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    notes TEXT,
    
    -- Additional context
    reason VARCHAR(100),
    ip_address VARCHAR(45),
    user_agent VARCHAR(500),
    
    -- Timestamps
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Payment methods (saved cards, etc.)
CREATE TABLE payment_methods (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    
    -- Payment method details
    method_type payment_method_type NOT NULL,
    provider VARCHAR(50), -- payme, click, visa, etc.
    
    -- Card details (encrypted)
    last_four_digits VARCHAR(4),
    card_type VARCHAR(20), -- visa, mastercard, etc.
    expiry_month INTEGER,
    expiry_year INTEGER,
    cardholder_name VARCHAR(200),
    
    -- Digital wallet details
    wallet_id VARCHAR(100),
    wallet_phone VARCHAR(20),
    
    -- Status
    is_default BOOLEAN DEFAULT FALSE,
    is_active BOOLEAN DEFAULT TRUE,
    is_verified BOOLEAN DEFAULT FALSE,
    
    -- Timestamps
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Payment transactions
CREATE TABLE payment_transactions (
    id SERIAL PRIMARY KEY,
    order_id INTEGER REFERENCES orders(id) ON DELETE SET NULL,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    payment_method_id INTEGER REFERENCES payment_methods(id) ON DELETE SET NULL,
    
    -- Transaction details
    transaction_id VARCHAR(100) UNIQUE NOT NULL,
    external_transaction_id VARCHAR(100),
    payment_method payment_method NOT NULL,
    amount DECIMAL(10,2) NOT NULL,
    currency VARCHAR(3) DEFAULT 'UZS',
    
    -- Status
    status payment_status DEFAULT 'pending',
    
    -- Provider details
    payment_provider VARCHAR(50),
    provider_response JSON DEFAULT '{}',
    
    -- Transaction metadata
    description TEXT,
    reference_number VARCHAR(100),
    
    -- Processing details
    processed_at TIMESTAMP WITH TIME ZONE,
    failed_reason TEXT,
    refund_amount DECIMAL(10,2) DEFAULT 0,
    
    -- Timestamps
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Delivery personnel
CREATE TABLE delivery_persons (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE UNIQUE,
    
    -- Personal information
    full_name VARCHAR(100) NOT NULL,
    phone VARCHAR(20) NOT NULL,
    license_number VARCHAR(50),
    
    -- Work details
    vehicle_type VARCHAR(50),
    vehicle_number VARCHAR(20),
    max_delivery_capacity INTEGER DEFAULT 10,
    
    -- Status
    is_active BOOLEAN DEFAULT TRUE,
    is_available BOOLEAN DEFAULT TRUE,
    current_zone VARCHAR(100),
    
    -- Performance metrics
    total_deliveries INTEGER DEFAULT 0,
    average_rating DECIMAL(3,2) DEFAULT 0,
    on_time_percentage DECIMAL(5,2) DEFAULT 0,
    
    -- Location tracking
    last_known_latitude DECIMAL(10, 8),
    last_known_longitude DECIMAL(11, 8),
    last_location_update TIMESTAMP WITH TIME ZONE,
    
    -- Timestamps
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Deliveries table
CREATE TABLE deliveries (
    id SERIAL PRIMARY KEY,
    order_id INTEGER REFERENCES orders(id) ON DELETE CASCADE UNIQUE,
    delivery_person_id INTEGER REFERENCES delivery_persons(id) ON DELETE SET NULL,
    
    -- Delivery details
    status delivery_status DEFAULT 'scheduled',
    delivery_type delivery_type DEFAULT 'standard',
    
    -- Addresses
    pickup_address TEXT,
    delivery_address TEXT,
    pickup_latitude DECIMAL(10, 8),
    pickup_longitude DECIMAL(11, 8),
    delivery_latitude DECIMAL(10, 8),
    delivery_longitude DECIMAL(11, 8),
    
    -- Timing
    scheduled_pickup TIMESTAMP WITH TIME ZONE,
    scheduled_delivery TIMESTAMP WITH TIME ZONE,
    actual_pickup TIMESTAMP WITH TIME ZONE,
    actual_delivery TIMESTAMP WITH TIME ZONE,
    estimated_duration INTEGER, -- minutes
    
    -- Delivery metadata
    priority priority DEFAULT 'normal',
    delivery_instructions TEXT,
    special_requirements TEXT,
    
    -- Tracking
    tracking_updates JSON DEFAULT '[]',
    delivery_notes TEXT,
    customer_signature TEXT,
    proof_of_delivery_photos JSON DEFAULT '[]',
    
    -- Customer feedback
    customer_rating INTEGER,
    customer_feedback TEXT,
    
    -- Timestamps
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Subscriptions table
CREATE TABLE subscriptions (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    subscription_number VARCHAR(50) UNIQUE NOT NULL,
    status subscription_status DEFAULT 'active',
    
    -- Subscription details
    name VARCHAR(200) NOT NULL,
    description TEXT,
    
    -- Billing cycle
    billing_cycle VARCHAR(20) NOT NULL,
    billing_amount DECIMAL(10,2) NOT NULL,
    next_billing_date TIMESTAMP WITH TIME ZONE,
    last_billing_date TIMESTAMP WITH TIME ZONE,
    
    -- Delivery schedule
    delivery_frequency VARCHAR(20) NOT NULL,
    delivery_day_of_week INTEGER, -- 1-7, Monday=1
    delivery_day_of_month INTEGER, -- 1-31
    delivery_time_slot VARCHAR(20) NOT NULL,
    delivery_address_id INTEGER REFERENCES addresses(id) NOT NULL,
    
    -- Subscription period
    start_date TIMESTAMP WITH TIME ZONE NOT NULL,
    end_date TIMESTAMP WITH TIME ZONE,
    auto_renew BOOLEAN DEFAULT TRUE,
    
    -- Payment settings
    payment_method VARCHAR(20) NOT NULL,
    auto_payment BOOLEAN DEFAULT TRUE,
    
    -- Pause/Resume functionality
    paused_at TIMESTAMP WITH TIME ZONE,
    pause_reason VARCHAR(255),
    resume_date TIMESTAMP WITH TIME ZONE,
    
    -- Analytics
    total_orders_generated INTEGER DEFAULT 0,
    total_amount_billed DECIMAL(10,2) DEFAULT 0.0,
    failed_billing_attempts INTEGER DEFAULT 0,
    last_successful_billing TIMESTAMP WITH TIME ZONE,
    
    -- Special features
    discount_percentage DECIMAL(10,4) DEFAULT 0.0,
    loyalty_points_multiplier DECIMAL(10,4) DEFAULT 1.0,
    
    -- Legacy fields (keeping for compatibility)
    frequency subscription_frequency NOT NULL,
    delivery_time TIME,
    preferred_delivery_window VARCHAR(50),
    base_price DECIMAL(10,2) NOT NULL,
    next_delivery_date DATE,
    trial_days INTEGER DEFAULT 0,
    trial_end_date DATE,
    paused_until DATE,
    max_deliveries INTEGER,
    deliveries_completed INTEGER DEFAULT 0,
    billing_cycle_start DATE,
    last_billed_date DATE,
    next_billing_date DATE,
    
    -- Timestamps
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Subscription items
CREATE TABLE subscription_items (
    id SERIAL PRIMARY KEY,
    subscription_id INTEGER REFERENCES subscriptions(id) ON DELETE CASCADE,
    product_id INTEGER REFERENCES products(id) ON DELETE CASCADE,
    
    -- Item details
    quantity INTEGER NOT NULL,
    unit_price DECIMAL(10,2) NOT NULL,
    total_price DECIMAL(10,2) NOT NULL DEFAULT 0,
    
    -- Product snapshot (in case product changes)
    product_name VARCHAR(200) NOT NULL DEFAULT '',
    product_sku VARCHAR(50) NOT NULL DEFAULT '',
    
    -- Timestamps
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Subscription logs
CREATE TABLE subscription_logs (
    id SERIAL PRIMARY KEY,
    subscription_id INTEGER REFERENCES subscriptions(id) NOT NULL,
    action VARCHAR(50) NOT NULL, -- created, paused, resumed, cancelled, billed, etc.
    details TEXT,
    user_id INTEGER REFERENCES users(id), -- Who performed the action
    extra_data JSON DEFAULT '{}',
    
    -- Timestamps
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Subscription plans (predefined subscription plans)
CREATE TABLE subscription_plans (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    description TEXT,
    
    -- Plan details
    price DECIMAL(10,2) NOT NULL,
    billing_cycle VARCHAR(20) NOT NULL, -- daily, weekly, monthly
    delivery_frequency VARCHAR(20) NOT NULL,
    
    -- Plan features
    features JSON DEFAULT '[]', -- List of included features
    max_items_per_delivery INTEGER,
    free_delivery BOOLEAN DEFAULT FALSE,
    discount_percentage DECIMAL(10,2) DEFAULT 0.0,
    
    -- Plan status
    is_active BOOLEAN DEFAULT TRUE,
    is_popular BOOLEAN DEFAULT FALSE, -- Mark popular plans
    sort_order INTEGER DEFAULT 0,
    
    -- Restrictions
    minimum_commitment_months INTEGER DEFAULT 0, -- Minimum commitment period
    available_for_new_customers BOOLEAN DEFAULT TRUE,
    available_for_existing_customers BOOLEAN DEFAULT TRUE,
    
    -- Timestamps
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Loyalty programs
CREATE TABLE loyalty_programs (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    description TEXT,
    
    -- Program settings
    is_active BOOLEAN DEFAULT TRUE,
    is_default BOOLEAN DEFAULT FALSE,
    
    -- Earning rules
    points_per_uzs DECIMAL(10,4) DEFAULT 1.0,
    signup_bonus INTEGER DEFAULT 100,
    referral_bonus INTEGER DEFAULT 500,
    review_bonus INTEGER DEFAULT 50,
    birthday_bonus INTEGER DEFAULT 200,
    
    -- Point management
    points_expiry_days INTEGER DEFAULT 365,
    min_redemption_points INTEGER DEFAULT 100,
    
    -- Tier system
    tier_thresholds JSON DEFAULT '{}',
    tier_multipliers JSON DEFAULT '{}',
    
    -- Program metadata
    terms_and_conditions TEXT,
    start_date TIMESTAMP,
    end_date TIMESTAMP,
    
    -- Legacy fields (keeping for compatibility)
    min_points_redeem INTEGER DEFAULT 100,
    points_to_uzs_ratio DECIMAL(10,4) DEFAULT 0.01,
    max_earn_per_day INTEGER,
    max_earn_per_month INTEGER,
    points_expiry_months INTEGER DEFAULT 12,
    enable_tiers BOOLEAN DEFAULT FALSE,
    
    -- Timestamps
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- User loyalty points balance (summary table)
CREATE TABLE loyalty_points (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id) NOT NULL UNIQUE,
    program_id INTEGER REFERENCES loyalty_programs(id) NOT NULL,
    
    -- Point balances
    total_earned INTEGER DEFAULT 0,
    total_redeemed INTEGER DEFAULT 0,
    total_expired INTEGER DEFAULT 0,
    current_balance INTEGER DEFAULT 0,
    
    -- Tier information
    current_tier VARCHAR(50) DEFAULT 'Bronze',
    points_to_next_tier INTEGER DEFAULT 0,
    
    -- Metadata
    last_activity_date TIMESTAMP WITHOUT TIME ZONE,
    last_expiry_check TIMESTAMP WITHOUT TIME ZONE,
    
    -- Timestamps
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Loyalty transactions (detailed transaction history)
CREATE TABLE loyalty_transactions (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id) NOT NULL,
    points INTEGER NOT NULL, -- positive for earned, negative for redeemed
    transaction_type VARCHAR(20) NOT NULL, -- earned, redeemed, expired, bonus
    description VARCHAR(255) NOT NULL,
    
    -- Related entities
    order_id INTEGER REFERENCES orders(id),
    subscription_id INTEGER REFERENCES subscriptions(id),
    
    -- Expiration for earned points
    expires_at TIMESTAMP WITH TIME ZONE,
    is_expired BOOLEAN DEFAULT FALSE,
    
    -- Additional metadata
    extra_data JSON DEFAULT '{}',
    
    -- Timestamps
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Loyalty rewards
CREATE TABLE loyalty_rewards (
    id SERIAL PRIMARY KEY,
    program_id INTEGER REFERENCES loyalty_programs(id) ON DELETE CASCADE,
    
    -- Reward details
    name VARCHAR(200) NOT NULL,
    description TEXT,
    reward_type VARCHAR(50) NOT NULL, -- discount, free_product, free_delivery, voucher
    
    -- Redemption requirements
    points_cost INTEGER NOT NULL,
    min_order_value DECIMAL(10,2) DEFAULT 0,
    max_uses_per_user INTEGER DEFAULT 1,
    max_redemptions INTEGER,
    
    -- Reward value  
    discount_type VARCHAR(20),
    discount_value DECIMAL(10,2),
    free_product_id INTEGER REFERENCES products(id) ON DELETE SET NULL,
    voucher_code VARCHAR(50),
    
    -- Availability
    is_active BOOLEAN DEFAULT TRUE,
    is_featured BOOLEAN DEFAULT FALSE,
    valid_from DATE,
    valid_until DATE,
    
    -- Usage tracking
    redemptions_used INTEGER DEFAULT 0,
    
    -- Applicability
    applicable_products JSON DEFAULT '[]',
    applicable_categories JSON DEFAULT '[]',
    
    -- Metadata
    terms_conditions TEXT,
    image_url VARCHAR(255),
    sort_order INTEGER DEFAULT 0,
    
    -- Timestamps
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- User reward redemptions
CREATE TABLE user_rewards (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    reward_id INTEGER REFERENCES loyalty_rewards(id) ON DELETE CASCADE,
    
    -- Redemption details
    points_used INTEGER NOT NULL,
    status reward_status DEFAULT 'available',
    
    -- Usage
    used_in_order_id INTEGER REFERENCES orders(id) ON DELETE SET NULL,
    used_at TIMESTAMP WITH TIME ZONE,
    
    -- Expiry
    expires_at TIMESTAMP WITH TIME ZONE,
    
    -- Timestamps
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Referral programs
CREATE TABLE referral_programs (
    id SERIAL PRIMARY KEY,
    name VARCHAR(200) NOT NULL,
    description TEXT,
    
    -- Program settings
    is_active BOOLEAN DEFAULT TRUE,
    
    -- Rewards
    referrer_reward_type VARCHAR(50), -- points, discount, cash
    referrer_reward_value DECIMAL(10,2),
    referee_reward_type VARCHAR(50),
    referee_reward_value DECIMAL(10,2),
    
    -- Conditions
    min_referee_order_value DECIMAL(10,2),
    max_referrals_per_user INTEGER,
    
    -- Validity
    valid_from DATE,
    valid_until DATE,
    
    -- Timestamps
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Push notification tokens
CREATE TABLE push_notification_tokens (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    
    -- Token details
    token VARCHAR(500) UNIQUE NOT NULL,
    platform VARCHAR(20) NOT NULL, -- ios, android, web
    
    -- Device information
    device_id VARCHAR(255),
    device_name VARCHAR(255),
    app_version VARCHAR(50),
    os_version VARCHAR(50),
    
    -- Status
    is_active BOOLEAN DEFAULT TRUE,
    last_used TIMESTAMP WITH TIME ZONE,
    
    -- Timestamps
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Notification channels
CREATE TABLE notification_channels (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    
    -- Channel details
    channel_type notification_channel NOT NULL,
    channel_address VARCHAR(255) NOT NULL, -- email, phone, telegram_chat_id
    
    -- Verification
    is_verified BOOLEAN DEFAULT FALSE,
    verification_code VARCHAR(10),
    verification_expires TIMESTAMP WITH TIME ZONE,
    
    -- Preferences
    is_active BOOLEAN DEFAULT TRUE,
    notification_types JSON DEFAULT '[]', -- which types to send to this channel
    
    -- Timestamps
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Notifications table
CREATE TABLE notifications (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    recipient_telegram_id VARCHAR(50),
    
    -- Notification content
    type notification_type NOT NULL,
    title VARCHAR(255) NOT NULL,
    message TEXT NOT NULL,
    
    -- Rich content
    action_url VARCHAR(500),
    image_url VARCHAR(500),
    data JSON DEFAULT '{}',
    
    -- Channel and delivery
    channels notification_channel[] DEFAULT '{}', -- array of channels to send to
    status notification_status DEFAULT 'pending',
    
    -- Scheduling
    scheduled_for TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    sent_at TIMESTAMP WITH TIME ZONE,
    delivered_at TIMESTAMP WITH TIME ZONE,
    read_at TIMESTAMP WITH TIME ZONE,
    
    -- Associated entities
    order_id INTEGER REFERENCES orders(id) ON DELETE SET NULL,
    
    -- Retry mechanism
    retry_count INTEGER DEFAULT 0,
    max_retries INTEGER DEFAULT 3,
    
    -- Timestamps
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Reviews table
CREATE TABLE reviews (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    product_id INTEGER REFERENCES products(id) ON DELETE CASCADE,
    order_id INTEGER REFERENCES orders(id) ON DELETE SET NULL,
    
    -- Review content
    rating INTEGER NOT NULL CHECK (rating >= 1 AND rating <= 5),
    title VARCHAR(200),
    comment TEXT,
    
    -- Review moderation
    is_approved BOOLEAN DEFAULT FALSE,
    is_featured BOOLEAN DEFAULT FALSE,
    moderator_notes TEXT,
    
    -- Review metadata
    helpful_count INTEGER DEFAULT 0,
    photos JSON DEFAULT '[]',
    
    -- Timestamps
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Analytics and reporting tables

-- Customer segments
CREATE TABLE customer_segments (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    description TEXT,
    
    -- Segment criteria
    criteria JSON NOT NULL,
    
    -- Segment stats
    customer_count INTEGER DEFAULT 0,
    last_updated TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    
    -- Automated actions
    auto_apply_discount BOOLEAN DEFAULT FALSE,
    discount_percentage DECIMAL(5,2) DEFAULT 0.0,
    auto_loyalty_multiplier DECIMAL(5,2) DEFAULT 1.0,
    
    -- Timestamps
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Promotional campaigns
CREATE TABLE promotional_campaigns (
    id SERIAL PRIMARY KEY,
    name VARCHAR(200) NOT NULL,
    description TEXT,
    
    -- Campaign type
    campaign_type VARCHAR(50) NOT NULL,
    
    -- Target audience
    target_segments JSON DEFAULT '[]',
    target_all_customers BOOLEAN DEFAULT FALSE,
    target_new_customers BOOLEAN DEFAULT FALSE,
    target_vip_customers BOOLEAN DEFAULT FALSE,
    
    -- Campaign rules
    discount_type discount_type,
    discount_value DECIMAL(10,2),
    min_order_value DECIMAL(10,2),
    max_discount_amount DECIMAL(10,2),
    
    -- Validity
    is_active BOOLEAN DEFAULT TRUE,
    start_date TIMESTAMP WITH TIME ZONE NOT NULL,
    end_date TIMESTAMP WITH TIME ZONE,
    usage_limit INTEGER,
    usage_limit_per_customer INTEGER DEFAULT 1,
    
    -- Tracking
    total_uses INTEGER DEFAULT 0,
    total_discount_given DECIMAL(10,2) DEFAULT 0.0,
    total_revenue_generated DECIMAL(10,2) DEFAULT 0.0,
    
    -- Promo code
    promo_code VARCHAR(50) UNIQUE,
    
    -- Timestamps
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Campaign usage tracking
CREATE TABLE campaign_usage (
    id SERIAL PRIMARY KEY,
    campaign_id INTEGER REFERENCES promotional_campaigns(id) ON DELETE CASCADE,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    order_id INTEGER REFERENCES orders(id) ON DELETE SET NULL,
    
    -- Timestamps
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Analytics reports
CREATE TABLE analytics_reports (
    id SERIAL PRIMARY KEY,
    report_type VARCHAR(50) NOT NULL,
    title VARCHAR(200) NOT NULL,
    
    -- Report metadata
    start_date TIMESTAMP WITH TIME ZONE NOT NULL,
    end_date TIMESTAMP WITH TIME ZONE NOT NULL,
    generated_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    
    -- Report data
    report_data JSON NOT NULL,
    
    -- Status
    status VARCHAR(20) DEFAULT 'generated',
    is_public BOOLEAN DEFAULT FALSE,
    
    -- Timestamps
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- User behavior tracking
CREATE TABLE user_behavior (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    session_id VARCHAR(100),
    
    -- Action details
    action VARCHAR(100) NOT NULL,
    page_url VARCHAR(500),
    referrer_url VARCHAR(500),
    
    -- Technical details
    ip_address INET,
    user_agent TEXT,
    device_type VARCHAR(50),
    browser VARCHAR(100),
    
    -- Additional metadata
    extra_data JSON DEFAULT '{}',
    timestamp TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    
    -- Timestamps
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Sales metrics
CREATE TABLE sales_metrics (
    id SERIAL PRIMARY KEY,
    metric_name VARCHAR(100) NOT NULL,
    metric_type VARCHAR(50) NOT NULL,
    
    -- Time period
    period_start TIMESTAMP WITH TIME ZONE NOT NULL,
    period_end TIMESTAMP WITH TIME ZONE NOT NULL,
    
    -- Metric values
    value DECIMAL(15,2) NOT NULL,
    target_value DECIMAL(15,2),
    previous_value DECIMAL(15,2),
    
    -- Additional context
    unit VARCHAR(20),
    category VARCHAR(50),
    extra_data JSON DEFAULT '{}',
    
    -- Timestamps
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- User events tracking
CREATE TABLE user_events (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    session_id VARCHAR(100) NOT NULL,
    
    -- Event details
    event_type VARCHAR(50) NOT NULL,
    event_name VARCHAR(100) NOT NULL,
    event_category VARCHAR(50),
    
    -- Context
    page_url VARCHAR(500),
    referrer VARCHAR(500),
    user_agent VARCHAR(500),
    ip_address INET,
    
    -- Additional data
    event_data JSON DEFAULT '{}',
    
    -- Timestamps
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Product views tracking
CREATE TABLE product_views (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    product_id INTEGER REFERENCES products(id) ON DELETE CASCADE,
    session_id VARCHAR(100) NOT NULL,
    
    -- View details
    view_duration INTEGER DEFAULT 0,
    referrer_source VARCHAR(100),
    device_type VARCHAR(20),
    
    -- Timestamps
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Search queries tracking
CREATE TABLE search_queries (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    session_id VARCHAR(100) NOT NULL,
    
    -- Search details
    query_text VARCHAR(255) NOT NULL,
    results_count INTEGER DEFAULT 0,
    filters_applied JSON DEFAULT '{}',
    
    -- User interaction
    clicked_result_position INTEGER,
    clicked_product_id INTEGER REFERENCES products(id) ON DELETE SET NULL,
    
    -- Timestamps
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Conversion events tracking
CREATE TABLE conversion_events (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    session_id VARCHAR(100) NOT NULL,
    
    -- Conversion details
    event_type VARCHAR(50) NOT NULL,
    funnel_stage VARCHAR(50) NOT NULL,
    conversion_value DECIMAL(10,2) DEFAULT 0.0,
    
    -- Associated entities
    order_id INTEGER REFERENCES orders(id) ON DELETE SET NULL,
    product_id INTEGER REFERENCES products(id) ON DELETE SET NULL,
    
    -- Attribution
    source VARCHAR(100),
    medium VARCHAR(100),
    campaign VARCHAR(100),
    
    -- Timestamps
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Revenue metrics
CREATE TABLE revenue_metrics (
    id SERIAL PRIMARY KEY,
    
    -- Time period
    period_start TIMESTAMP WITH TIME ZONE NOT NULL,
    period_end TIMESTAMP WITH TIME ZONE NOT NULL,
    period_type VARCHAR(20) NOT NULL,
    
    -- Revenue metrics
    gross_revenue DECIMAL(15,2) DEFAULT 0.0,
    net_revenue DECIMAL(15,2) DEFAULT 0.0,
    recurring_revenue DECIMAL(15,2) DEFAULT 0.0,
    average_order_value DECIMAL(10,2) DEFAULT 0.0,
    
    -- Order metrics
    total_orders INTEGER DEFAULT 0,
    new_customer_orders INTEGER DEFAULT 0,
    repeat_customer_orders INTEGER DEFAULT 0,
    
    -- Customer metrics
    new_customers INTEGER DEFAULT 0,
    active_customers INTEGER DEFAULT 0,
    churned_customers INTEGER DEFAULT 0,
    
    -- Timestamps
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- User segments
CREATE TABLE user_segments (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    description TEXT,
    
    -- Segment criteria
    criteria JSON NOT NULL,
    
    -- Segment statistics
    user_count INTEGER DEFAULT 0,
    last_calculated TIMESTAMP WITH TIME ZONE,
    
    -- Status
    is_active BOOLEAN DEFAULT TRUE,
    auto_update BOOLEAN DEFAULT TRUE,
    
    -- Timestamps
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Translation system tables

-- Supported languages
CREATE TABLE languages (
    id SERIAL PRIMARY KEY,
    code VARCHAR(5) UNIQUE NOT NULL,
    name VARCHAR(50) NOT NULL,
    native_name VARCHAR(50) NOT NULL,
    is_active BOOLEAN DEFAULT TRUE,
    is_default BOOLEAN DEFAULT FALSE,
    sort_order INTEGER DEFAULT 0,
    flag_icon VARCHAR(10),
    
    -- Timestamps
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Translation categories
CREATE TABLE translation_categories (
    id SERIAL PRIMARY KEY,
    name VARCHAR(50) UNIQUE NOT NULL,
    description TEXT,
    is_active BOOLEAN DEFAULT TRUE,
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    
    -- Timestamps
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Translations
CREATE TABLE translations (
    id SERIAL PRIMARY KEY,
    key VARCHAR(255) NOT NULL,
    language VARCHAR(5) NOT NULL,
    value TEXT NOT NULL,
    category VARCHAR(50) DEFAULT 'general',
    description TEXT,
    is_active BOOLEAN DEFAULT TRUE,
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    updated_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    
    -- Timestamps
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    
    -- Constraints
    CONSTRAINT uq_translation_key_language UNIQUE (key, language)
);

-- Translation audit trail
CREATE TABLE translation_audit (
    id SERIAL PRIMARY KEY,
    translation_id INTEGER REFERENCES translations(id) ON DELETE CASCADE,
    action VARCHAR(20) NOT NULL,
    old_value TEXT,
    new_value TEXT,
    changed_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    ip_address INET,
    user_agent TEXT,
    
    -- Timestamps
    changed_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Indexes for performance optimization

-- Users table indexes
CREATE INDEX idx_users_email ON users(email);
CREATE INDEX idx_users_phone ON users(phone);
CREATE INDEX idx_users_telegram_id ON users(telegram_id);
CREATE INDEX idx_users_role ON users(role);
CREATE INDEX idx_users_status ON users(status);
CREATE INDEX idx_users_is_verified ON users(is_verified);
CREATE INDEX idx_users_registration_source ON users(registration_source);
CREATE INDEX idx_users_created_at ON users(created_at);

-- Addresses table indexes
CREATE INDEX idx_addresses_user_id ON addresses(user_id);
CREATE INDEX idx_addresses_is_default ON addresses(is_default);
CREATE INDEX idx_addresses_location ON addresses(latitude, longitude);

-- Product categories table indexes
CREATE INDEX idx_product_categories_active ON product_categories(is_active);
CREATE INDEX idx_product_categories_sort ON product_categories(sort_order);

-- Products table indexes
CREATE INDEX idx_products_category_id ON products(category_id);
CREATE INDEX idx_products_sku ON products(sku);
CREATE INDEX idx_products_is_active ON products(is_active);
CREATE INDEX idx_products_is_featured ON products(is_featured);
CREATE INDEX idx_products_stock_quantity ON products(stock_quantity);
CREATE INDEX idx_products_created_at ON products(created_at);
CREATE INDEX idx_products_name_trgm ON products USING gin(name gin_trgm_ops);

-- Price rules table indexes
CREATE INDEX idx_price_rules_product_id ON price_rules(product_id);
CREATE INDEX idx_price_rules_type ON price_rules(rule_type);
CREATE INDEX idx_price_rules_active ON price_rules(is_active);

-- Orders table indexes
CREATE INDEX idx_orders_user_id ON orders(user_id);
CREATE INDEX idx_orders_status ON orders(status);
CREATE INDEX idx_orders_order_number ON orders(order_number);
CREATE INDEX idx_orders_order_source ON orders(order_source);
CREATE INDEX idx_orders_created_at ON orders(created_at);
CREATE INDEX idx_orders_delivery_date ON orders(delivery_date);
CREATE INDEX idx_orders_is_paid ON orders(is_paid);
CREATE INDEX idx_orders_total_amount ON orders(total_amount);

-- Order items indexes
CREATE INDEX idx_order_items_order_id ON order_items(order_id);
CREATE INDEX idx_order_items_product_id ON order_items(product_id);

-- Payment transactions indexes
CREATE INDEX idx_payment_transactions_order_id ON payment_transactions(order_id);
CREATE INDEX idx_payment_transactions_user_id ON payment_transactions(user_id);
CREATE INDEX idx_payment_transactions_status ON payment_transactions(status);
CREATE INDEX idx_payment_transactions_created_at ON payment_transactions(created_at);
CREATE INDEX idx_payment_transactions_transaction_id ON payment_transactions(transaction_id);

-- Deliveries indexes
CREATE INDEX idx_deliveries_order_id ON deliveries(order_id);
CREATE INDEX idx_deliveries_delivery_person_id ON deliveries(delivery_person_id);
CREATE INDEX idx_deliveries_status ON deliveries(status);
CREATE INDEX idx_deliveries_scheduled_delivery ON deliveries(scheduled_delivery);

-- Subscriptions indexes
CREATE INDEX idx_subscriptions_user_id ON subscriptions(user_id);
CREATE INDEX idx_subscriptions_status ON subscriptions(status);
CREATE INDEX idx_subscriptions_next_delivery_date ON subscriptions(next_delivery_date);

-- Loyalty points indexes
CREATE INDEX idx_loyalty_points_user_id ON loyalty_points(user_id);
CREATE INDEX idx_loyalty_points_created_at ON loyalty_points(created_at);
CREATE INDEX idx_loyalty_points_expires_at ON loyalty_points(expires_at);

-- Notifications indexes
CREATE INDEX idx_notifications_user_id ON notifications(user_id);
CREATE INDEX idx_notifications_recipient_telegram_id ON notifications(recipient_telegram_id);
CREATE INDEX idx_notifications_type ON notifications(type);
CREATE INDEX idx_notifications_status ON notifications(status);
CREATE INDEX idx_notifications_created_at ON notifications(created_at);
CREATE INDEX idx_notifications_scheduled_for ON notifications(scheduled_for);

-- Reviews indexes
CREATE INDEX idx_reviews_product_id ON reviews(product_id);
CREATE INDEX idx_reviews_user_id ON reviews(user_id);
CREATE INDEX idx_reviews_rating ON reviews(rating);
CREATE INDEX idx_reviews_is_approved ON reviews(is_approved);
CREATE INDEX idx_reviews_created_at ON reviews(created_at);

-- Analytics indexes
CREATE INDEX idx_user_behavior_user_id ON user_behavior(user_id);
CREATE INDEX idx_user_behavior_action ON user_behavior(action);
CREATE INDEX idx_user_behavior_timestamp ON user_behavior(timestamp);

CREATE INDEX idx_user_events_user_id ON user_events(user_id);
CREATE INDEX idx_user_events_event_type ON user_events(event_type);
CREATE INDEX idx_user_events_session_id ON user_events(session_id);
CREATE INDEX idx_user_events_created_at ON user_events(created_at);

CREATE INDEX idx_product_views_user_id ON product_views(user_id);
CREATE INDEX idx_product_views_product_id ON product_views(product_id);
CREATE INDEX idx_product_views_session_id ON product_views(session_id);
CREATE INDEX idx_product_views_created_at ON product_views(created_at);

CREATE INDEX idx_search_queries_user_id ON search_queries(user_id);
CREATE INDEX idx_search_queries_query_text ON search_queries(query_text);
CREATE INDEX idx_search_queries_created_at ON search_queries(created_at);

-- Translation indexes
CREATE INDEX idx_translation_key_lang_active ON translations(key, language, is_active);
CREATE INDEX idx_translation_category_lang ON translations(category, language);
CREATE INDEX idx_translations_created_at ON translations(created_at);

-- Composite indexes for common queries
CREATE INDEX idx_orders_user_status_date ON orders(user_id, status, created_at);
CREATE INDEX idx_notifications_user_status_type ON notifications(user_id, status, type);
CREATE INDEX idx_loyalty_points_user_type_date ON loyalty_points(user_id, transaction_type, created_at);

-- Triggers for updated_at timestamps
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ language 'plpgsql';

-- Apply the trigger to tables with updated_at column
CREATE TRIGGER update_users_updated_at BEFORE UPDATE ON users 
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_addresses_updated_at BEFORE UPDATE ON addresses 
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_products_updated_at BEFORE UPDATE ON products 
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_orders_updated_at BEFORE UPDATE ON orders 
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_payment_methods_updated_at BEFORE UPDATE ON payment_methods 
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_payment_transactions_updated_at BEFORE UPDATE ON payment_transactions 
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_delivery_persons_updated_at BEFORE UPDATE ON delivery_persons 
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_deliveries_updated_at BEFORE UPDATE ON deliveries 
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_subscriptions_updated_at BEFORE UPDATE ON subscriptions 
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_loyalty_programs_updated_at BEFORE UPDATE ON loyalty_programs 
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_loyalty_rewards_updated_at BEFORE UPDATE ON loyalty_rewards 
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_user_rewards_updated_at BEFORE UPDATE ON user_rewards 
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_referral_programs_updated_at BEFORE UPDATE ON referral_programs 
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_push_notification_tokens_updated_at BEFORE UPDATE ON push_notification_tokens 
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_notification_channels_updated_at BEFORE UPDATE ON notification_channels 
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_notifications_updated_at BEFORE UPDATE ON notifications 
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_reviews_updated_at BEFORE UPDATE ON reviews 
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_translations_updated_at BEFORE UPDATE ON translations 
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- Insert initial data

-- Insert default languages
INSERT INTO languages (code, name, native_name, is_default, sort_order, flag_icon) VALUES
('en', 'English', 'English', true, 1, '🇺🇸'),
('uz', 'Uzbek', 'O''zbek', false, 2, '🇺🇿'),
('ru', 'Russian', 'Русский', false, 3, '🇷🇺');

-- Insert translation categories
INSERT INTO translation_categories (name, description) VALUES
('general', 'General application text'),
('ui', 'User interface elements'),
('messages', 'System messages and notifications'),
('errors', 'Error messages'),
('emails', 'Email templates'),
('sms', 'SMS templates'),
('products', 'Product-related text'),
('orders', 'Order-related text'),
('delivery', 'Delivery-related text'),
('payments', 'Payment-related text'),
('loyalty', 'Loyalty program text'),
('subscription', 'Subscription-related text'),
('admin', 'Admin interface text'),
('telegram', 'Telegram bot messages'),
('validation', 'Validation messages');

-- Insert default loyalty program
INSERT INTO loyalty_programs (name, description, is_active, is_default, points_per_uzs, signup_bonus) VALUES
('BlueStream Rewards', 'Default loyalty program for all customers', true, true, 0.01, 100);

-- Comments for documentation
COMMENT ON DATABASE bluestream IS 'Water Business Platform Database - Comprehensive water delivery business management system';

COMMENT ON TABLE users IS 'User accounts including customers, admins, and delivery personnel';
COMMENT ON TABLE addresses IS 'Delivery addresses associated with users';
COMMENT ON TABLE products IS 'Water products catalog with inventory management';
COMMENT ON TABLE orders IS 'Customer orders with full order lifecycle management';
COMMENT ON TABLE order_items IS 'Individual items within orders';
COMMENT ON TABLE payment_transactions IS 'Payment processing and transaction history';
COMMENT ON TABLE deliveries IS 'Delivery management and tracking';
COMMENT ON TABLE subscriptions IS 'Recurring delivery subscriptions';
COMMENT ON TABLE loyalty_points IS 'Loyalty points transactions and balances';
COMMENT ON TABLE notifications IS 'Multi-channel notification system';
COMMENT ON TABLE reviews IS 'Product reviews and ratings';
COMMENT ON TABLE translations IS 'Multi-language translation system';

-- Performance notes
COMMENT ON INDEX idx_products_name_trgm IS 'Full-text search index for product names (requires pg_trgm extension)';
COMMENT ON INDEX idx_orders_user_status_date IS 'Composite index for efficient user order queries';
COMMENT ON INDEX idx_notifications_user_status_type IS 'Composite index for user notification queries';