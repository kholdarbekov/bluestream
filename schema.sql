-- =========================================================================
-- BlueStream Water Business Platform - Database Schema
-- =========================================================================
-- Generated from business_app/models analysis - Complete 52 table schema
-- Preserving existing extensions and adding comprehensive model coverage
-- =========================================================================

-- Create database extensions (PostgreSQL 17 compatible)
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pgcrypto";
CREATE EXTENSION IF NOT EXISTS "pg_trgm"; -- For full-text search
CREATE EXTENSION IF NOT EXISTS "btree_gin"; -- For GIN indexes on multiple columns
CREATE EXTENSION IF NOT EXISTS "pg_stat_statements"; -- For query performance monitoring

-- Set proper encoding and configuration
SET client_encoding = 'UTF8';
SET timezone = 'UTC';

-- =========================================================================
-- ENUM TYPES (from business_app/utils/constants.py)
-- =========================================================================

-- Order related enums
CREATE TYPE order_status AS ENUM (
    'pending', 'confirmed', 'preparing', 'out_for_delivery', 
    'delivered', 'cancelled', 'returned'
);

-- Payment related enums
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

-- Delivery related enums
CREATE TYPE delivery_status AS ENUM (
    'scheduled', 'pending', 'assigned', 'picked_up', 'in_transit', 
    'arrived', 'delivered', 'failed', 'returned'
);

CREATE TYPE delivery_type AS ENUM (
    'standard', 'express', 'scheduled', 'emergency'
);

-- Subscription related enums
CREATE TYPE subscription_status AS ENUM (
    'active', 'paused', 'cancelled', 'expired', 'trial'
);

CREATE TYPE subscription_frequency AS ENUM (
    'daily', 'weekly', 'biweekly', 'monthly'
);

-- User related enums
CREATE TYPE user_role AS ENUM (
    'customer', 'admin', 'manager', 'delivery_driver', 'operator'
);

CREATE TYPE user_status AS ENUM (
    'active', 'inactive', 'banned', 'pending_verification'
);

-- Notification related enums
CREATE TYPE notification_type AS ENUM (
    'order_confirmation', 'order_status_update', 'delivery_update', 
    'payment_confirmation', 'subscription_reminder', 'promotional', 
    'system_alert', 'loyalty_reward'
);

CREATE TYPE notification_channel AS ENUM (
    'email', 'sms', 'telegram', 'push', 'in_app'
);

CREATE TYPE notification_status AS ENUM (
    'pending', 'sent', 'delivered', 'failed', 'read'
);

-- Product related enums
CREATE TYPE product_category_enum AS ENUM (
    'drinking_water', 'sparkling_water', 'flavored_water', 
    'alkaline_water', 'distilled_water', 'spring_water'
);

CREATE TYPE product_size_enum AS ENUM (
    '0.5L', '1L', '1.5L', '5L', '19L'
);

-- General enums
CREATE TYPE priority AS ENUM (
    'low', 'normal', 'high', 'urgent'
);

CREATE TYPE discount_type AS ENUM (
    'percentage', 'fixed_amount', 'free_delivery', 'bogo'
);

CREATE TYPE loyalty_action_type AS ENUM (
    'purchase', 'referral', 'review', 'social_share', 'birthday_bonus', 'welcome_bonus'
);

CREATE TYPE loyalty_transaction_type AS ENUM (
    'earned', 'redeemed', 'expired', 'bonus', 'adjustment'
);

CREATE TYPE reward_status AS ENUM (
    'available', 'claimed', 'expired', 'used', 'cancelled'
);

CREATE TYPE price_rule_type AS ENUM (
    'bulk_discount', 'vip_discount', 'loyalty_discount', 'seasonal_discount', 'time_based'
);

CREATE TYPE file_type AS ENUM (
    'image', 'document', 'video', 'audio'
);

CREATE TYPE log_level AS ENUM (
    'debug', 'info', 'warning', 'error', 'critical'
);

-- =========================================================================
-- CORE USER MANAGEMENT TABLES
-- =========================================================================

-- Users table (matches User model)
CREATE TABLE users (
    id SERIAL PRIMARY KEY,
    email VARCHAR(255) UNIQUE NOT NULL,
    phone VARCHAR(20) UNIQUE,
    password_hash VARCHAR(255) NOT NULL,
    
    -- Personal information
    first_name VARCHAR(100),
    last_name VARCHAR(100),
    full_name VARCHAR(200),
    date_of_birth TIMESTAMP WITH TIME ZONE,
    gender VARCHAR(10),
    
    -- Account status and role
    role user_role DEFAULT 'customer',
    status user_status DEFAULT 'active',
    is_verified BOOLEAN DEFAULT FALSE,
    is_premium BOOLEAN DEFAULT FALSE,
    
    -- Preferences
    preferred_language VARCHAR(5) DEFAULT 'en',
    preferred_currency VARCHAR(3) DEFAULT 'UZS',
    timezone VARCHAR(50) DEFAULT 'Asia/Tashkent',
    email_notifications BOOLEAN DEFAULT TRUE,
    sms_notifications BOOLEAN DEFAULT TRUE,
    push_notifications BOOLEAN DEFAULT TRUE,
    
    -- Business account fields
    company_name VARCHAR(200),
    tax_id VARCHAR(50),
    business_type VARCHAR(50),
    
    -- Security and authentication
    last_login TIMESTAMP WITH TIME ZONE,
    failed_login_attempts INTEGER DEFAULT 0,
    account_locked_until TIMESTAMP WITH TIME ZONE,
    password_reset_token VARCHAR(255),
    password_reset_expires TIMESTAMP WITH TIME ZONE,
    email_verification_token VARCHAR(255),
    email_verified_at TIMESTAMP WITH TIME ZONE,
    
    -- Telegram/Bot integration fields
    telegram_id VARCHAR(50) UNIQUE,
    telegram_username VARCHAR(255),
    telegram_first_name VARCHAR(255),
    telegram_last_name VARCHAR(255),
    telegram_language_code VARCHAR(10),
    is_bot_active BOOLEAN DEFAULT FALSE,
    bot_state TEXT, -- JSON string for bot conversation state
    last_bot_interaction TIMESTAMP WITH TIME ZONE,
    
    -- Registration source tracking
    registration_source VARCHAR(50) DEFAULT 'web',
    
    -- Timestamps (TimestampMixin)
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- User addresses table (matches UserAddress model)
CREATE TABLE addresses (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    
    -- Address details
    title VARCHAR(100),
    full_address TEXT NOT NULL,
    street_address VARCHAR(255),
    city VARCHAR(100) DEFAULT 'Tashkent',
    district VARCHAR(100),
    postal_code VARCHAR(20),
    country VARCHAR(100) DEFAULT 'Uzbekistan',
    
    -- Geographic coordinates
    latitude FLOAT,
    longitude FLOAT,
    
    -- Address metadata
    is_default BOOLEAN DEFAULT FALSE,
    is_business BOOLEAN DEFAULT FALSE,
    delivery_instructions TEXT,
    landmark VARCHAR(255),
    floor_number VARCHAR(20),
    apartment_number VARCHAR(20),
    
    -- Timestamps (TimestampMixin)
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- User sessions table (matches UserSession model)
CREATE TABLE user_sessions (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    session_token VARCHAR(255) UNIQUE NOT NULL,
    device_info VARCHAR(255),
    ip_address VARCHAR(45),
    user_agent VARCHAR(500),
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    is_active BOOLEAN DEFAULT TRUE,
    last_activity TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    ended_at TIMESTAMP WITH TIME ZONE,
    
    -- Timestamps (TimestampMixin)
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- =========================================================================
-- PRODUCT CATALOG TABLES
-- =========================================================================

-- Product categories table (matches ProductCategory model)
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
    
    -- Timestamps (TimestampMixin)
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Products table (matches Product model)
CREATE TABLE products (
    id SERIAL PRIMARY KEY,
    name VARCHAR(200) NOT NULL,
    description TEXT,
    short_description VARCHAR(500),
    sku VARCHAR(100),
    
    -- Pricing (using NUMERIC for precision)
    base_price NUMERIC(10,2) NOT NULL,
    cost_price NUMERIC(10,2),
    discount_price NUMERIC(10,2),
    
    -- Product details
    category_id INTEGER NOT NULL REFERENCES product_categories(id),
    size product_size_enum NOT NULL,
    volume FLOAT,
    volume_unit VARCHAR(10) DEFAULT 'L',
    weight FLOAT,
    weight_unit VARCHAR(10) DEFAULT 'kg',
    is_active BOOLEAN DEFAULT TRUE,
    is_featured BOOLEAN DEFAULT FALSE,
    requires_prescription BOOLEAN DEFAULT FALSE,
    
    -- Inventory
    track_inventory BOOLEAN DEFAULT TRUE,
    stock_quantity INTEGER DEFAULT 0,
    min_stock_level INTEGER DEFAULT 0,
    max_stock_level INTEGER DEFAULT 1000,
    
    -- Media and content (JSON fields)
    images JSON DEFAULT '[]',
    nutrition_facts JSON DEFAULT '{}',
    ingredients TEXT,
    barcode VARCHAR(100),
    
    -- SEO and metadata
    slug VARCHAR(255),
    meta_title VARCHAR(200),
    meta_description TEXT,
    
    -- Timestamps (TimestampMixin)
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Price rules table (matches PriceRule model)
CREATE TABLE price_rules (
    id SERIAL PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    rule_type price_rule_type NOT NULL,
    name VARCHAR(100) NOT NULL,
    description TEXT,
    
    -- Rule conditions
    min_quantity INTEGER DEFAULT 1,
    max_quantity INTEGER,
    min_order_value NUMERIC(10,2),
    customer_type VARCHAR(50),
    
    -- Discount details
    discount_type VARCHAR(20) DEFAULT 'percentage',
    discount_value NUMERIC(10,2) NOT NULL,
    
    -- Validity
    is_active BOOLEAN DEFAULT TRUE,
    valid_from TIMESTAMP WITH TIME ZONE,
    valid_until TIMESTAMP WITH TIME ZONE,
    
    -- Timestamps (TimestampMixin)
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- =========================================================================
-- ORDER MANAGEMENT TABLES
-- =========================================================================

-- Orders table (matches Order model)
CREATE TABLE orders (
    id SERIAL PRIMARY KEY,
    order_number VARCHAR(50) UNIQUE NOT NULL,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    status order_status DEFAULT 'pending',
    
    -- Pricing (using NUMERIC for precision)
    subtotal NUMERIC(10,2) NOT NULL DEFAULT 0.00,
    discount_amount NUMERIC(10,2) DEFAULT 0.00,
    delivery_fee NUMERIC(10,2) DEFAULT 0.00,
    loyalty_discount NUMERIC(10,2) DEFAULT 0.00,
    total_amount NUMERIC(10,2) NOT NULL DEFAULT 0.00,
    
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
    subscription_id INTEGER, -- Will reference subscriptions table
    loyalty_points_used INTEGER DEFAULT 0,
    loyalty_points_earned INTEGER DEFAULT 0,
    
    -- Order source tracking
    order_source VARCHAR(20) DEFAULT 'web',
    
    -- Timestamps (TimestampMixin)
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Order items table (matches OrderItem model)
CREATE TABLE order_items (
    id SERIAL PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    product_id INTEGER NOT NULL REFERENCES products(id),
    quantity INTEGER NOT NULL,
    unit_price NUMERIC(10,2) NOT NULL,
    discount_amount NUMERIC(10,2) DEFAULT 0.00,
    total_price NUMERIC(10,2) NOT NULL,
    
    -- Product snapshot (stored at time of order)
    product_name VARCHAR(200) NOT NULL,
    product_sku VARCHAR(50) NOT NULL
);

-- Order status history table (matches OrderStatusHistory model)
CREATE TABLE order_status_history (
    id SERIAL PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    old_status order_status NOT NULL,
    new_status order_status NOT NULL,
    changed_by INTEGER REFERENCES users(id),
    changed_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    notes TEXT,
    
    -- Additional context
    reason VARCHAR(100),
    ip_address VARCHAR(45),
    user_agent VARCHAR(500),
    
    -- Timestamps (TimestampMixin)
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- =========================================================================
-- PAYMENT PROCESSING TABLES
-- =========================================================================

-- Payments table (matches Payment model)
CREATE TABLE payments (
    id SERIAL PRIMARY KEY,
    payment_id VARCHAR(100) UNIQUE NOT NULL,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    order_id INTEGER REFERENCES orders(id),
    subscription_id INTEGER, -- Will reference subscriptions table
    
    amount NUMERIC(10,2) NOT NULL,
    currency VARCHAR(3) DEFAULT 'UZS',
    payment_method payment_method NOT NULL,
    status payment_status DEFAULT 'pending',
    
    -- Payment provider specific data
    provider_transaction_id VARCHAR(255),
    provider_data JSON DEFAULT '{}',
    
    -- Payment link details
    payment_link VARCHAR(500),
    payment_link_expires_at TIMESTAMP WITH TIME ZONE,
    
    -- Webhook processing
    webhook_processed BOOLEAN DEFAULT FALSE,
    webhook_attempts INTEGER DEFAULT 0,
    
    -- Metadata
    description VARCHAR(255),
    callback_url VARCHAR(500),
    failure_reason VARCHAR(500),
    
    -- Timestamps (TimestampMixin)
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Payment transactions table (matches PaymentTransaction model)
CREATE TABLE payment_transactions (
    id SERIAL PRIMARY KEY,
    payment_id INTEGER NOT NULL REFERENCES payments(id) ON DELETE CASCADE,
    transaction_type VARCHAR(50) NOT NULL, -- charge, refund, capture, cancel
    
    -- Transaction details
    amount NUMERIC(10,2) NOT NULL,
    currency VARCHAR(3) DEFAULT 'UZS',
    status VARCHAR(20) NOT NULL, -- success, failed, pending
    
    -- External provider details
    provider_transaction_id VARCHAR(255),
    provider_reference VARCHAR(255),
    provider_response JSON DEFAULT '{}',
    
    -- Transaction context
    initiated_by INTEGER REFERENCES users(id),
    ip_address VARCHAR(45),
    user_agent VARCHAR(500),
    
    -- Result details
    success BOOLEAN NOT NULL DEFAULT FALSE,
    failure_reason VARCHAR(500),
    
    -- Processing details
    processed_at TIMESTAMP WITH TIME ZONE,
    processing_time_ms INTEGER,
    
    -- Additional data
    extra_data JSON DEFAULT '{}',
    notes TEXT,
    
    -- Timestamps (TimestampMixin)
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Credit cards table (matches CreditCard model)
CREATE TABLE credit_cards (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    
    -- Card details (encrypted/tokenized)
    card_token VARCHAR(255) UNIQUE NOT NULL,
    card_brand VARCHAR(20) NOT NULL,
    last_four_digits VARCHAR(4) NOT NULL,
    expiry_month INTEGER NOT NULL,
    expiry_year INTEGER NOT NULL,
    
    -- Card holder info
    cardholder_name VARCHAR(100) NOT NULL,
    
    -- Status and settings
    is_default BOOLEAN DEFAULT FALSE,
    is_active BOOLEAN DEFAULT TRUE,
    is_verified BOOLEAN DEFAULT FALSE,
    
    -- Provider info
    provider VARCHAR(50) NOT NULL,
    provider_card_id VARCHAR(255),
    
    -- Security
    fingerprint VARCHAR(100),
    
    -- Usage tracking
    last_used_at TIMESTAMP WITH TIME ZONE,
    usage_count INTEGER DEFAULT 0,
    
    -- Timestamps (TimestampMixin)
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- =========================================================================
-- SUBSCRIPTION MANAGEMENT TABLES
-- =========================================================================

-- Subscription plans table (matches SubscriptionPlan model)
CREATE TABLE subscription_plans (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    description TEXT,
    
    -- Plan details
    price NUMERIC(10,2) NOT NULL,
    billing_cycle VARCHAR(20) NOT NULL,
    delivery_frequency VARCHAR(20) NOT NULL,
    
    -- Plan features
    features JSON DEFAULT '[]',
    max_items_per_delivery INTEGER,
    free_delivery BOOLEAN DEFAULT FALSE,
    discount_percentage FLOAT DEFAULT 0.0,
    
    -- Plan status
    is_active BOOLEAN DEFAULT TRUE,
    is_popular BOOLEAN DEFAULT FALSE,
    sort_order INTEGER DEFAULT 0,
    
    -- Restrictions
    minimum_commitment_months INTEGER DEFAULT 0,
    available_for_new_customers BOOLEAN DEFAULT TRUE,
    available_for_existing_customers BOOLEAN DEFAULT TRUE,
    
    -- Timestamps (TimestampMixin)
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Subscriptions table (matches Subscription model)
CREATE TABLE subscriptions (
    id SERIAL PRIMARY KEY,
    subscription_number VARCHAR(50) UNIQUE NOT NULL,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    status subscription_status DEFAULT 'active',
    
    -- Subscription details
    name VARCHAR(200) NOT NULL,
    description TEXT,
    
    -- Billing cycle
    billing_cycle VARCHAR(20) NOT NULL, -- daily, weekly, monthly
    billing_amount NUMERIC(10,2) NOT NULL,
    next_billing_date TIMESTAMP WITH TIME ZONE NOT NULL,
    last_billing_date TIMESTAMP WITH TIME ZONE,
    
    -- Delivery schedule
    delivery_frequency VARCHAR(20) NOT NULL,
    delivery_day_of_week INTEGER,
    delivery_day_of_month INTEGER,
    delivery_time_slot VARCHAR(20) NOT NULL,
    delivery_address_id INTEGER NOT NULL REFERENCES addresses(id),
    
    -- Subscription period
    start_date TIMESTAMP WITH TIME ZONE NOT NULL,
    end_date TIMESTAMP WITH TIME ZONE,
    auto_renew BOOLEAN DEFAULT TRUE,
    
    -- Payment settings
    payment_method payment_method NOT NULL,
    auto_payment BOOLEAN DEFAULT TRUE,
    
    -- Pause/Resume functionality
    paused_at TIMESTAMP WITH TIME ZONE,
    pause_reason VARCHAR(255),
    resume_date TIMESTAMP WITH TIME ZONE,
    
    -- Analytics
    total_orders_generated INTEGER DEFAULT 0,
    total_amount_billed NUMERIC(10,2) DEFAULT 0.00,
    failed_billing_attempts INTEGER DEFAULT 0,
    last_successful_billing TIMESTAMP WITH TIME ZONE,
    
    -- Special features
    discount_percentage FLOAT DEFAULT 0.0,
    loyalty_points_multiplier FLOAT DEFAULT 1.0,
    
    -- Timestamps (TimestampMixin)
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Subscription items table (matches SubscriptionItem model)
CREATE TABLE subscription_items (
    id SERIAL PRIMARY KEY,
    subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
    product_id INTEGER NOT NULL REFERENCES products(id),
    quantity INTEGER NOT NULL,
    unit_price NUMERIC(10,2) NOT NULL,
    total_price NUMERIC(10,2) NOT NULL,
    
    -- Product snapshot
    product_name VARCHAR(200) NOT NULL,
    product_sku VARCHAR(50) NOT NULL
);

-- Subscription logs table (matches SubscriptionLog model)
CREATE TABLE subscription_logs (
    id SERIAL PRIMARY KEY,
    subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
    action VARCHAR(50) NOT NULL, -- created, paused, resumed, cancelled, billed, etc.
    details TEXT,
    user_id INTEGER REFERENCES users(id),
    extra_data JSON DEFAULT '{}',
    
    -- Timestamps (TimestampMixin)
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- =========================================================================
-- DELIVERY SYSTEM TABLES  
-- =========================================================================

-- Delivery time slots table (matches DeliveryTimeSlot model)
CREATE TABLE delivery_time_slots (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    start_time VARCHAR(5) NOT NULL, -- "09:00"
    end_time VARCHAR(5) NOT NULL,   -- "12:00"
    is_active BOOLEAN DEFAULT TRUE,
    max_orders INTEGER DEFAULT 50,
    delivery_fee NUMERIC(10,2) DEFAULT 0.00,
    
    -- Availability by day of week
    available_days JSON DEFAULT '[0,1,2,3,4,5,6]',
    
    -- Special pricing
    is_premium BOOLEAN DEFAULT FALSE,
    premium_fee NUMERIC(10,2) DEFAULT 0.00
);

-- Delivery persons table (matches DeliveryPerson model)
CREATE TABLE delivery_persons (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    
    -- Personal information
    full_name VARCHAR(100) NOT NULL,
    phone VARCHAR(20) NOT NULL,
    email VARCHAR(120),
    
    -- Work details
    employee_id VARCHAR(50) UNIQUE,
    hire_date TIMESTAMP WITH TIME ZONE,
    
    -- Vehicle information
    vehicle_type VARCHAR(50),
    vehicle_number VARCHAR(20),
    vehicle_capacity_kg FLOAT DEFAULT 0.0,
    
    -- Work schedule
    working_hours_start VARCHAR(5) DEFAULT '09:00',
    working_hours_end VARCHAR(5) DEFAULT '18:00',
    working_days JSON DEFAULT '["monday","tuesday","wednesday","thursday","friday","saturday"]',
    
    -- Location tracking
    current_location_lat FLOAT,
    current_location_lng FLOAT,
    last_location_update TIMESTAMP WITH TIME ZONE,
    
    -- Status and metrics
    is_active BOOLEAN DEFAULT TRUE,
    is_available BOOLEAN DEFAULT TRUE,
    
    -- Performance metrics
    total_deliveries INTEGER DEFAULT 0,
    successful_deliveries INTEGER DEFAULT 0,
    average_rating FLOAT DEFAULT 0.0,
    total_distance_km FLOAT DEFAULT 0.0,
    
    -- Emergency contact
    emergency_contact_name VARCHAR(100),
    emergency_contact_phone VARCHAR(20),
    
    -- Timestamps (TimestampMixin)
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Deliveries table (matches Delivery model)
CREATE TABLE deliveries (
    id SERIAL PRIMARY KEY,
    order_id INTEGER NOT NULL UNIQUE REFERENCES orders(id) ON DELETE CASCADE,
    delivery_person_id INTEGER REFERENCES users(id),
    status delivery_status DEFAULT 'scheduled',
    
    -- Scheduling
    scheduled_date TIMESTAMP WITH TIME ZONE NOT NULL,
    scheduled_time_slot VARCHAR(20) NOT NULL,
    estimated_delivery_time TIMESTAMP WITH TIME ZONE,
    actual_delivery_time TIMESTAMP WITH TIME ZONE,
    
    -- Route and tracking
    route_data JSON DEFAULT '{}',
    tracking_number VARCHAR(50) UNIQUE,
    distance_km FLOAT,
    estimated_duration_minutes INTEGER,
    
    -- Real-time tracking
    current_location_lat FLOAT,
    current_location_lng FLOAT,
    last_location_update TIMESTAMP WITH TIME ZONE,
    
    -- Completion details
    delivered_at TIMESTAMP WITH TIME ZONE,
    delivery_confirmation_photos JSON DEFAULT '[]',
    recipient_signature VARCHAR(500),
    delivery_notes TEXT,
    customer_rating INTEGER, -- 1-5 stars
    customer_feedback TEXT,
    
    -- Delivery attempts
    delivery_attempts INTEGER DEFAULT 0,
    failed_delivery_reason VARCHAR(255),
    
    -- Timestamps (TimestampMixin)
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Delivery routes table (matches DeliveryRoute model)
CREATE TABLE delivery_routes (
    id SERIAL PRIMARY KEY,
    name VARCHAR(200) NOT NULL,
    delivery_person_id INTEGER NOT NULL REFERENCES users(id),
    
    -- Route details
    start_location_lat FLOAT NOT NULL,
    start_location_lng FLOAT NOT NULL,
    route_date TIMESTAMP WITH TIME ZONE NOT NULL,
    
    -- Route optimization data
    optimized_order JSON DEFAULT '[]',
    total_distance_km FLOAT,
    estimated_duration_minutes INTEGER,
    
    -- Route status
    status VARCHAR(20) DEFAULT 'planned',
    started_at TIMESTAMP WITH TIME ZONE,
    completed_at TIMESTAMP WITH TIME ZONE,
    
    -- Performance metrics
    actual_distance_km FLOAT,
    actual_duration_minutes INTEGER,
    deliveries_completed INTEGER DEFAULT 0,
    deliveries_failed INTEGER DEFAULT 0,
    
    -- Additional data
    extra_data JSON DEFAULT '{}',
    notes TEXT,
    
    -- Timestamps (TimestampMixin)
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Delivery status history table (matches DeliveryStatusHistory model)
CREATE TABLE delivery_status_history (
    id SERIAL PRIMARY KEY,
    delivery_id INTEGER NOT NULL REFERENCES deliveries(id) ON DELETE CASCADE,
    old_status delivery_status NOT NULL,
    new_status delivery_status NOT NULL,
    changed_by INTEGER REFERENCES users(id),
    changed_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    
    -- Location when status changed
    location_lat FLOAT,
    location_lng FLOAT,
    location_accuracy FLOAT,
    
    -- Context
    reason VARCHAR(100),
    notes TEXT,
    automatic BOOLEAN DEFAULT FALSE,
    
    -- Additional metadata
    extra_data JSON DEFAULT '{}',
    device_info VARCHAR(255),
    ip_address VARCHAR(45),
    
    -- Timestamps (TimestampMixin)
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- =========================================================================
-- LOYALTY PROGRAM TABLES
-- =========================================================================

-- Loyalty programs table (matches LoyaltyProgram model)
CREATE TABLE loyalty_programs (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    description TEXT,
    
    -- Program settings
    is_active BOOLEAN DEFAULT TRUE,
    is_default BOOLEAN DEFAULT FALSE,
    
    -- Earning rules
    points_per_uzs FLOAT DEFAULT 1.0,
    signup_bonus INTEGER DEFAULT 100,
    referral_bonus INTEGER DEFAULT 50,
    birthday_bonus INTEGER DEFAULT 25,
    
    -- Point management
    points_expiry_days INTEGER DEFAULT 365,
    min_redemption_points INTEGER DEFAULT 100,
    
    -- Tier system
    tier_thresholds JSON DEFAULT '{}',
    tier_multipliers JSON DEFAULT '{}',
    
    -- Program metadata
    terms_and_conditions TEXT,
    start_date TIMESTAMP WITH TIME ZONE,
    end_date TIMESTAMP WITH TIME ZONE,
    
    -- Timestamps (TimestampMixin)
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Loyalty points table (matches LoyaltyPoints model)
CREATE TABLE loyalty_points (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    program_id INTEGER NOT NULL REFERENCES loyalty_programs(id),
    
    -- Point balances
    total_earned INTEGER DEFAULT 0,
    total_redeemed INTEGER DEFAULT 0,
    total_expired INTEGER DEFAULT 0,
    current_balance INTEGER DEFAULT 0,
    
    -- Tier information
    current_tier VARCHAR(50) DEFAULT 'Bronze',
    points_to_next_tier INTEGER DEFAULT 0,
    
    -- Metadata
    last_activity_date TIMESTAMP WITH TIME ZONE,
    last_expiry_check TIMESTAMP WITH TIME ZONE,
    
    -- Timestamps (TimestampMixin)
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Loyalty transactions table (matches LoyaltyTransaction model)
CREATE TABLE loyalty_transactions (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    points INTEGER NOT NULL, -- Can be negative for redemptions
    transaction_type loyalty_transaction_type NOT NULL,
    description VARCHAR(255) NOT NULL,
    
    -- Related entities
    order_id INTEGER REFERENCES orders(id),
    subscription_id INTEGER REFERENCES subscriptions(id),
    
    -- Expiration for earned points
    expires_at TIMESTAMP WITH TIME ZONE,
    is_expired BOOLEAN DEFAULT FALSE,
    
    -- Additional metadata
    extra_data JSON DEFAULT '{}',
    
    -- Timestamps (TimestampMixin)
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Loyalty rewards table (matches LoyaltyReward model)
CREATE TABLE loyalty_rewards (
    id SERIAL PRIMARY KEY,
    program_id INTEGER NOT NULL REFERENCES loyalty_programs(id),
    
    -- Reward details
    name VARCHAR(200) NOT NULL,
    description TEXT,
    reward_type VARCHAR(50) NOT NULL,
    
    -- Redemption requirements
    points_cost INTEGER NOT NULL,
    min_order_value NUMERIC(10,2) DEFAULT 0.00,
    max_uses_per_user INTEGER DEFAULT 1,
    max_redemptions INTEGER,
    
    -- Reward value
    discount_type VARCHAR(20),
    discount_value NUMERIC(10,2),
    free_product_id INTEGER REFERENCES products(id),
    voucher_code VARCHAR(50),
    
    -- Availability
    is_active BOOLEAN DEFAULT TRUE,
    is_featured BOOLEAN DEFAULT FALSE,
    valid_from TIMESTAMP WITH TIME ZONE,
    valid_until TIMESTAMP WITH TIME ZONE,
    
    -- Usage tracking
    redemptions_used INTEGER DEFAULT 0,
    
    -- Applicability
    applicable_products JSON DEFAULT '[]',
    applicable_categories JSON DEFAULT '[]',
    
    -- Metadata
    terms_conditions TEXT,
    image_url VARCHAR(255),
    sort_order INTEGER DEFAULT 0,
    
    -- Timestamps (TimestampMixin)
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Referral programs table (matches ReferralProgram model)
CREATE TABLE referral_programs (
    id SERIAL PRIMARY KEY,
    referrer_id INTEGER NOT NULL REFERENCES users(id),
    referee_id INTEGER NOT NULL REFERENCES users(id),
    
    -- Referral details
    referral_code VARCHAR(20) NOT NULL UNIQUE,
    status VARCHAR(20) DEFAULT 'pending',
    
    -- Rewards
    referrer_bonus_points INTEGER DEFAULT 0,
    referee_bonus_points INTEGER DEFAULT 0,
    
    -- Tracking
    referred_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP WITH TIME ZONE,
    first_order_id INTEGER REFERENCES orders(id),
    
    -- Timestamps (TimestampMixin)
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- =========================================================================
-- NOTIFICATION SYSTEM TABLES
-- =========================================================================

-- Notification channels table (matches NotificationChannel model)
CREATE TABLE notification_channels (
    id SERIAL PRIMARY KEY,
    name VARCHAR(50) NOT NULL UNIQUE,
    display_name VARCHAR(100) NOT NULL,
    description TEXT,
    
    -- Channel configuration
    is_active BOOLEAN DEFAULT TRUE,
    requires_confirmation BOOLEAN DEFAULT FALSE,
    rate_limit_per_hour INTEGER DEFAULT 100,
    priority INTEGER DEFAULT 1,
    
    -- Provider settings (JSON)
    provider_settings JSON DEFAULT '{}',
    
    -- Timestamps (TimestampMixin)
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Notification templates table (matches NotificationTemplate model)
CREATE TABLE notification_templates (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    notification_type VARCHAR(50) NOT NULL,
    channel VARCHAR(20) NOT NULL, -- email, sms, push, in_app
    language VARCHAR(5) DEFAULT 'uz',
    subject VARCHAR(255),
    content TEXT NOT NULL,
    is_active BOOLEAN DEFAULT TRUE,
    
    -- Timestamps (TimestampMixin)
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Notification preferences table (matches NotificationPreference model)
CREATE TABLE notification_preferences (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    notification_type VARCHAR(50) NOT NULL,
    channel notification_channel NOT NULL,
    is_enabled BOOLEAN DEFAULT TRUE NOT NULL,
    
    -- Timestamps (TimestampMixin)
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Push notification tokens table (matches PushNotificationToken model)
CREATE TABLE push_notification_tokens (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token VARCHAR(255) NOT NULL UNIQUE,
    
    -- Device information
    platform VARCHAR(10) NOT NULL, -- ios, android, web
    device_id VARCHAR(255),
    device_name VARCHAR(100),
    app_version VARCHAR(20),
    
    -- Status
    is_active BOOLEAN DEFAULT TRUE,
    last_used TIMESTAMP WITH TIME ZONE,
    
    -- Timestamps (TimestampMixin)
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Notifications table (matches Notification model)
CREATE TABLE notifications (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    notification_type VARCHAR(50) NOT NULL,
    channel notification_channel NOT NULL,
    
    -- Content
    title VARCHAR(255) NOT NULL,
    message TEXT NOT NULL,
    
    -- Delivery status
    is_sent BOOLEAN DEFAULT FALSE,
    sent_at TIMESTAMP WITH TIME ZONE,
    delivery_status notification_status DEFAULT 'pending',
    failure_reason VARCHAR(255),
    
    -- Recipient details
    recipient_phone VARCHAR(20),
    recipient_email VARCHAR(120),
    recipient_telegram_id VARCHAR(50),
    
    -- Related entities
    order_id INTEGER REFERENCES orders(id),
    delivery_id INTEGER REFERENCES deliveries(id),
    
    -- Scheduling
    scheduled_for TIMESTAMP WITH TIME ZONE,
    priority priority DEFAULT 'normal',
    
    -- Additional data
    extra_data JSON DEFAULT '{}',
    
    -- Timestamps (TimestampMixin)
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- =========================================================================
-- REVIEW SYSTEM TABLES
-- =========================================================================

-- Reviews table (matches Review model)
CREATE TABLE reviews (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    order_id INTEGER REFERENCES orders(id),
    
    rating INTEGER NOT NULL, -- 1-5 stars
    title VARCHAR(200),
    comment TEXT,
    
    -- Review moderation
    is_approved BOOLEAN DEFAULT FALSE,
    is_featured BOOLEAN DEFAULT FALSE,
    moderator_notes TEXT,
    
    -- Review metadata
    helpful_count INTEGER DEFAULT 0,
    photos JSON DEFAULT '[]',
    
    -- Timestamps (TimestampMixin)
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- =========================================================================
-- ANALYTICS & TRACKING TABLES (from analytics.py)
-- =========================================================================

-- Customer segments table (matches CustomerSegment model)
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
    discount_percentage FLOAT DEFAULT 0.0,
    auto_loyalty_multiplier FLOAT DEFAULT 1.0,
    
    -- Timestamps (TimestampMixin)
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Promotional campaigns table (matches PromotionalCampaign model)
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
    discount_type VARCHAR(20),
    discount_value NUMERIC(10,2),
    min_order_value NUMERIC(10,2),
    max_discount_amount NUMERIC(10,2),
    
    -- Validity
    is_active BOOLEAN DEFAULT TRUE,
    start_date TIMESTAMP WITH TIME ZONE NOT NULL,
    end_date TIMESTAMP WITH TIME ZONE,
    usage_limit INTEGER,
    usage_limit_per_customer INTEGER DEFAULT 1,
    
    -- Tracking
    total_uses INTEGER DEFAULT 0,
    total_discount_given NUMERIC(10,2) DEFAULT 0.00,
    total_revenue_generated NUMERIC(10,2) DEFAULT 0.00,
    
    -- Promo code
    promo_code VARCHAR(50) UNIQUE,
    
    -- Timestamps (TimestampMixin)
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Campaign usage table (matches CampaignUsage model)
CREATE TABLE campaign_usage (
    id SERIAL PRIMARY KEY,
    campaign_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    order_id INTEGER,
    
    -- Timestamps (TimestampMixin)
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Analytics reports table (matches AnalyticsReport model)
CREATE TABLE analytics_reports (
    id SERIAL PRIMARY KEY,
    report_type VARCHAR(50) NOT NULL,
    title VARCHAR(200) NOT NULL,
    
    -- Report metadata
    start_date TIMESTAMP WITH TIME ZONE NOT NULL,
    end_date TIMESTAMP WITH TIME ZONE NOT NULL,
    generated_by INTEGER REFERENCES users(id),
    
    -- Report data
    report_data JSON NOT NULL,
    
    -- Status
    status VARCHAR(20) DEFAULT 'generated',
    is_public BOOLEAN DEFAULT FALSE,
    
    -- Timestamps (TimestampMixin)
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- User behavior table (matches UserBehavior model)
CREATE TABLE user_behavior (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id),
    session_id VARCHAR(100),
    
    -- Action details
    action VARCHAR(100) NOT NULL,
    page_url VARCHAR(500),
    referrer_url VARCHAR(500),
    
    -- Technical details
    ip_address VARCHAR(45),
    user_agent TEXT,
    device_type VARCHAR(50),
    browser VARCHAR(100),
    
    -- Additional metadata
    extra_data JSON DEFAULT '{}',
    timestamp TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
    
    -- Timestamps (TimestampMixin)
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Sales metrics table (matches SalesMetric model)
CREATE TABLE sales_metrics (
    id SERIAL PRIMARY KEY,
    metric_name VARCHAR(100) NOT NULL,
    metric_type VARCHAR(50) NOT NULL,
    
    -- Time period
    period_start TIMESTAMP WITH TIME ZONE NOT NULL,
    period_end TIMESTAMP WITH TIME ZONE NOT NULL,
    
    -- Metric values
    value NUMERIC(10,2) NOT NULL,
    target_value NUMERIC(10,2),
    previous_value NUMERIC(10,2),
    
    -- Additional context
    unit VARCHAR(20),
    category VARCHAR(50),
    extra_data JSON DEFAULT '{}',
    
    -- Timestamps (TimestampMixin)
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- User events table (matches UserEvent model)
CREATE TABLE user_events (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id),
    session_id VARCHAR(100) NOT NULL,
    
    -- Event details
    event_type VARCHAR(50) NOT NULL,
    event_name VARCHAR(100) NOT NULL,
    event_category VARCHAR(50),
    
    -- Context
    page_url VARCHAR(500),
    referrer VARCHAR(500),
    user_agent VARCHAR(500),
    ip_address VARCHAR(45),
    
    -- Additional data
    event_data JSON DEFAULT '{}',
    
    -- Timestamps (TimestampMixin)
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Product views table (matches ProductView model)
CREATE TABLE product_views (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id),
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    session_id VARCHAR(100) NOT NULL,
    
    -- View details
    view_duration INTEGER DEFAULT 0, -- seconds
    referrer_source VARCHAR(100),
    device_type VARCHAR(20),
    
    -- Timestamps (TimestampMixin)
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Search queries table (matches SearchQuery model)
CREATE TABLE search_queries (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id),
    session_id VARCHAR(100) NOT NULL,
    
    -- Search details
    query_text VARCHAR(255) NOT NULL,
    results_count INTEGER DEFAULT 0,
    filters_applied JSON DEFAULT '{}',
    
    -- User interaction
    clicked_result_position INTEGER,
    clicked_product_id INTEGER REFERENCES products(id),
    
    -- Timestamps (TimestampMixin)
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Conversion events table (matches ConversionEvent model)
CREATE TABLE conversion_events (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id),
    session_id VARCHAR(100) NOT NULL,
    
    -- Conversion details
    event_type VARCHAR(50) NOT NULL,
    funnel_stage VARCHAR(50) NOT NULL,
    conversion_value NUMERIC(10,2) DEFAULT 0.00,
    
    -- Associated entities
    order_id INTEGER REFERENCES orders(id),
    product_id INTEGER REFERENCES products(id),
    
    -- Attribution
    source VARCHAR(100),
    medium VARCHAR(100),
    campaign VARCHAR(100),
    
    -- Timestamps (TimestampMixin)
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Revenue metrics table (matches RevenueMetric model)
CREATE TABLE revenue_metrics (
    id SERIAL PRIMARY KEY,
    
    -- Time period
    period_start TIMESTAMP WITH TIME ZONE NOT NULL,
    period_end TIMESTAMP WITH TIME ZONE NOT NULL,
    period_type VARCHAR(20) NOT NULL,
    
    -- Revenue metrics
    gross_revenue NUMERIC(10,2) DEFAULT 0.00,
    net_revenue NUMERIC(10,2) DEFAULT 0.00,
    recurring_revenue NUMERIC(10,2) DEFAULT 0.00,
    average_order_value NUMERIC(10,2) DEFAULT 0.00,
    
    -- Order metrics
    total_orders INTEGER DEFAULT 0,
    new_customer_orders INTEGER DEFAULT 0,
    repeat_customer_orders INTEGER DEFAULT 0,
    
    -- Customer metrics
    new_customers INTEGER DEFAULT 0,
    active_customers INTEGER DEFAULT 0,
    churned_customers INTEGER DEFAULT 0,
    
    -- Timestamps (TimestampMixin)
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- User segments table (matches UserSegment model)
CREATE TABLE user_segments (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    description TEXT,
    
    -- Segment criteria (stored as JSON)
    criteria JSON NOT NULL,
    
    -- Segment statistics
    user_count INTEGER DEFAULT 0,
    last_calculated TIMESTAMP WITH TIME ZONE,
    
    -- Status
    is_active BOOLEAN DEFAULT TRUE,
    auto_update BOOLEAN DEFAULT TRUE,
    
    -- Timestamps (TimestampMixin)
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- =========================================================================
-- TRANSLATION & INTERNATIONALIZATION TABLES (from translation.py)
-- =========================================================================

-- Languages table (matches Language model)
CREATE TABLE languages (
    id SERIAL PRIMARY KEY,
    code VARCHAR(5) UNIQUE NOT NULL,
    name VARCHAR(50) NOT NULL,
    native_name VARCHAR(50) NOT NULL,
    is_active BOOLEAN DEFAULT TRUE NOT NULL,
    is_default BOOLEAN DEFAULT FALSE NOT NULL,
    sort_order INTEGER DEFAULT 0,
    flag_icon VARCHAR(10),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL
);

-- Translation categories table (matches TranslationCategory model)
CREATE TABLE translation_categories (
    id SERIAL PRIMARY KEY,
    name VARCHAR(50) UNIQUE NOT NULL,
    description TEXT,
    is_active BOOLEAN DEFAULT TRUE NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
    created_by INTEGER REFERENCES users(id)
);

-- Translations table (matches Translation model)
CREATE TABLE translations (
    id SERIAL PRIMARY KEY,
    key TEXT NOT NULL,
    language VARCHAR(5) NOT NULL,
    value TEXT NOT NULL,
    category VARCHAR(50) DEFAULT 'general',
    description TEXT,
    is_active BOOLEAN DEFAULT TRUE NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
    created_by INTEGER REFERENCES users(id),
    updated_by INTEGER REFERENCES users(id),
    UNIQUE(key, language)
);

-- Translation audit table (matches TranslationAudit model)
CREATE TABLE translation_audit (
    id SERIAL PRIMARY KEY,
    translation_id INTEGER NOT NULL REFERENCES translations(id) ON DELETE CASCADE,
    action VARCHAR(20) NOT NULL,
    old_value TEXT,
    new_value TEXT,
    changed_by INTEGER REFERENCES users(id),
    changed_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
    ip_address VARCHAR(45),
    user_agent TEXT
);

-- =========================================================================
-- ADD FOREIGN KEY CONSTRAINTS THAT COULDN'T BE ADDED EARLIER
-- =========================================================================

-- Add foreign key constraints for subscription references
ALTER TABLE orders ADD CONSTRAINT fk_orders_subscription FOREIGN KEY (subscription_id) REFERENCES subscriptions(id);
ALTER TABLE payments ADD CONSTRAINT fk_payments_subscription FOREIGN KEY (subscription_id) REFERENCES subscriptions(id);

-- =========================================================================
-- CREATE INDEXES FOR BETTER PERFORMANCE
-- =========================================================================

-- User table indexes
CREATE INDEX idx_users_email ON users(email);
CREATE INDEX idx_users_phone ON users(phone);
CREATE INDEX idx_users_telegram_id ON users(telegram_id);
CREATE INDEX idx_users_role ON users(role);
CREATE INDEX idx_users_status ON users(status);
CREATE INDEX idx_users_is_verified ON users(is_verified);
CREATE INDEX idx_users_registration_source ON users(registration_source);
CREATE INDEX idx_users_is_bot_active ON users(is_bot_active);

-- Address indexes
CREATE INDEX idx_addresses_user_id ON addresses(user_id);
CREATE INDEX idx_addresses_is_default ON addresses(is_default);

-- Session indexes
CREATE INDEX idx_user_sessions_user_id ON user_sessions(user_id);
CREATE INDEX idx_user_sessions_session_token ON user_sessions(session_token);
CREATE INDEX idx_user_sessions_is_active ON user_sessions(is_active);

-- Product indexes
CREATE INDEX idx_products_category_id ON products(category_id);
CREATE INDEX idx_products_sku ON products(sku);
CREATE INDEX idx_products_is_active ON products(is_active);
CREATE INDEX idx_products_is_featured ON products(is_featured);

-- Order indexes
CREATE INDEX idx_orders_user_id ON orders(user_id);
CREATE INDEX idx_orders_status ON orders(status);
CREATE INDEX idx_orders_order_number ON orders(order_number);
CREATE INDEX idx_orders_is_paid ON orders(is_paid);
CREATE INDEX idx_orders_created_at ON orders(created_at);

-- Order items indexes
CREATE INDEX idx_order_items_order_id ON order_items(order_id);
CREATE INDEX idx_order_items_product_id ON order_items(product_id);

-- Payment indexes
CREATE INDEX idx_payments_user_id ON payments(user_id);
CREATE INDEX idx_payments_status ON payments(status);
CREATE INDEX idx_payments_payment_id ON payments(payment_id);

-- Delivery indexes
CREATE INDEX idx_deliveries_order_id ON deliveries(order_id);
CREATE INDEX idx_deliveries_delivery_person_id ON deliveries(delivery_person_id);
CREATE INDEX idx_deliveries_status ON deliveries(status);
CREATE INDEX idx_deliveries_tracking_number ON deliveries(tracking_number);

-- Subscription indexes
CREATE INDEX idx_subscriptions_user_id ON subscriptions(user_id);
CREATE INDEX idx_subscriptions_status ON subscriptions(status);
CREATE INDEX idx_subscriptions_subscription_number ON subscriptions(subscription_number);

-- Loyalty indexes
CREATE INDEX idx_loyalty_transactions_user_id ON loyalty_transactions(user_id);
CREATE INDEX idx_loyalty_transactions_transaction_type ON loyalty_transactions(transaction_type);

-- Notification indexes
CREATE INDEX idx_notifications_user_id ON notifications(user_id);
CREATE INDEX idx_notifications_is_sent ON notifications(is_sent);
CREATE INDEX idx_notifications_notification_type ON notifications(notification_type);

-- Analytics indexes
CREATE INDEX idx_user_behavior_user_id ON user_behavior(user_id);
CREATE INDEX idx_user_behavior_session_id ON user_behavior(session_id);
CREATE INDEX idx_product_views_product_id ON product_views(product_id);
CREATE INDEX idx_search_queries_query_text ON search_queries(query_text);

-- Translation indexes
CREATE INDEX idx_translations_key ON translations(key);
CREATE INDEX idx_translations_language ON translations(language);
CREATE INDEX idx_translations_key_lang_active ON translations(key, language, is_active);

-- Review indexes
CREATE INDEX idx_reviews_product_id ON reviews(product_id);
CREATE INDEX idx_reviews_user_id ON reviews(user_id);
CREATE INDEX idx_reviews_rating ON reviews(rating);

-- =========================================================================
-- TRIGGERS FOR AUTOMATIC TIMESTAMP UPDATES
-- =========================================================================

-- Function to update updated_at timestamp
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ language 'plpgsql';

-- Create triggers for all tables with updated_at column
CREATE TRIGGER update_users_updated_at BEFORE UPDATE ON users FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_addresses_updated_at BEFORE UPDATE ON addresses FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_user_sessions_updated_at BEFORE UPDATE ON user_sessions FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_product_categories_updated_at BEFORE UPDATE ON product_categories FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_products_updated_at BEFORE UPDATE ON products FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_price_rules_updated_at BEFORE UPDATE ON price_rules FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_orders_updated_at BEFORE UPDATE ON orders FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_order_status_history_updated_at BEFORE UPDATE ON order_status_history FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_payments_updated_at BEFORE UPDATE ON payments FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_payment_transactions_updated_at BEFORE UPDATE ON payment_transactions FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_credit_cards_updated_at BEFORE UPDATE ON credit_cards FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_subscription_plans_updated_at BEFORE UPDATE ON subscription_plans FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_subscriptions_updated_at BEFORE UPDATE ON subscriptions FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_subscription_logs_updated_at BEFORE UPDATE ON subscription_logs FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_delivery_persons_updated_at BEFORE UPDATE ON delivery_persons FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_deliveries_updated_at BEFORE UPDATE ON deliveries FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_delivery_routes_updated_at BEFORE UPDATE ON delivery_routes FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_delivery_status_history_updated_at BEFORE UPDATE ON delivery_status_history FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_loyalty_programs_updated_at BEFORE UPDATE ON loyalty_programs FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_loyalty_points_updated_at BEFORE UPDATE ON loyalty_points FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_loyalty_transactions_updated_at BEFORE UPDATE ON loyalty_transactions FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_loyalty_rewards_updated_at BEFORE UPDATE ON loyalty_rewards FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_referral_programs_updated_at BEFORE UPDATE ON referral_programs FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_notification_channels_updated_at BEFORE UPDATE ON notification_channels FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_notification_templates_updated_at BEFORE UPDATE ON notification_templates FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_notification_preferences_updated_at BEFORE UPDATE ON notification_preferences FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_push_notification_tokens_updated_at BEFORE UPDATE ON push_notification_tokens FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_notifications_updated_at BEFORE UPDATE ON notifications FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_reviews_updated_at BEFORE UPDATE ON reviews FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_customer_segments_updated_at BEFORE UPDATE ON customer_segments FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_promotional_campaigns_updated_at BEFORE UPDATE ON promotional_campaigns FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_campaign_usage_updated_at BEFORE UPDATE ON campaign_usage FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_analytics_reports_updated_at BEFORE UPDATE ON analytics_reports FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_user_behavior_updated_at BEFORE UPDATE ON user_behavior FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_sales_metrics_updated_at BEFORE UPDATE ON sales_metrics FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_user_events_updated_at BEFORE UPDATE ON user_events FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_product_views_updated_at BEFORE UPDATE ON product_views FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_search_queries_updated_at BEFORE UPDATE ON search_queries FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_conversion_events_updated_at BEFORE UPDATE ON conversion_events FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_revenue_metrics_updated_at BEFORE UPDATE ON revenue_metrics FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_user_segments_updated_at BEFORE UPDATE ON user_segments FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_translations_updated_at BEFORE UPDATE ON translations FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- =========================================================================
-- SEED DATA FOR ESSENTIAL RECORDS
-- =========================================================================

-- Insert default languages
INSERT INTO languages (code, name, native_name, is_default, sort_order, flag_icon) VALUES
('en', 'English', 'English', true, 1, '🇺🇸'),
('uz', 'Uzbek', 'O''zbek', false, 2, '🇺🇿'),
('ru', 'Russian', 'Русский', false, 3, '🇷🇺');

-- Insert default translation categories
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

-- Insert default product categories
INSERT INTO product_categories (name, name_en, name_ru, description, is_active, sort_order) VALUES
('Ichimlik suvi', 'Drinking Water', 'Питьевая вода', 'Tozalangan ichimlik suvi', true, 1),
('Gazlangan suv', 'Sparkling Water', 'Газированная вода', 'Gazlangan mineral suv', true, 2),
('Ta''mli suv', 'Flavored Water', 'Ароматизированная вода', 'Ta''m qo''shilgan suv', true, 3),
('Alkalin suv', 'Alkaline Water', 'Щелочная вода', 'Alkalin mineral suv', true, 4),
('Distillangan suv', 'Distilled Water', 'Дистиллированная вода', 'Distillangan tozalangan suv', true, 5),
('Buloq suvi', 'Spring Water', 'Родниковая вода', 'Tabiiy buloq suvi', true, 6);

-- Insert default delivery time slots
INSERT INTO delivery_time_slots (name, start_time, end_time, is_active, max_orders, delivery_fee) VALUES
('Ertalabki', '09:00', '12:00', true, 50, 0.00),
('Tushlik', '12:00', '15:00', true, 50, 0.00),
('Peshinlik', '15:00', '18:00', true, 50, 0.00),
('Kechqurun', '18:00', '21:00', true, 30, 2000.00);

-- Insert default loyalty program
INSERT INTO loyalty_programs (name, description, is_active, is_default, points_per_uzs, signup_bonus, referral_bonus, birthday_bonus) VALUES
('BlueStream Loyalty', 'Asosiy sodiqlik dasturi', true, true, 0.01, 100, 50, 25);

-- Insert default notification channels
INSERT INTO notification_channels (name, display_name, description, is_active, rate_limit_per_hour, priority) VALUES
('email', 'Email', 'Email xabarnomalar', true, 100, 1),
('sms', 'SMS', 'SMS xabarnomalar', true, 50, 2),
('telegram', 'Telegram', 'Telegram bot xabarnomalar', true, 200, 3),
('push', 'Push', 'Push xabarnomalar', true, 500, 4),
('in_app', 'In-App', 'Ilova ichidagi xabarnomalar', true, 1000, 5);

-- =========================================================================
-- COMMENTS FOR DOCUMENTATION
-- =========================================================================

COMMENT ON DATABASE bluestream IS 'BlueStream Water Business Platform - Complete 52 table schema';

-- Key table comments
COMMENT ON TABLE users IS 'Primary user accounts with full profile and Telegram bot integration';
COMMENT ON TABLE orders IS 'Customer orders with comprehensive pricing and delivery tracking';
COMMENT ON TABLE subscriptions IS 'Recurring delivery subscriptions with flexible billing cycles';
COMMENT ON TABLE deliveries IS 'Real-time delivery tracking with route optimization';
COMMENT ON TABLE loyalty_transactions IS 'Customer loyalty points transactions with expiration handling';

-- =========================================================================
-- FINAL STATISTICS OUTPUT
-- =========================================================================

DO $$
DECLARE
    table_count INTEGER;
    enum_count INTEGER;
    index_count INTEGER;
    trigger_count INTEGER;
BEGIN
    -- Count tables
    SELECT COUNT(*) INTO table_count 
    FROM information_schema.tables 
    WHERE table_schema = 'public' AND table_type = 'BASE TABLE';
    
    -- Count enums
    SELECT COUNT(*) INTO enum_count 
    FROM pg_type 
    WHERE typtype = 'e';
    
    -- Count indexes
    SELECT COUNT(*) INTO index_count 
    FROM pg_indexes 
    WHERE schemaname = 'public';
    
    -- Count triggers
    SELECT COUNT(*) INTO trigger_count 
    FROM information_schema.triggers 
    WHERE trigger_schema = 'public';
    
    RAISE NOTICE '================================================';
    RAISE NOTICE 'BlueStream Database Schema Successfully Created!';
    RAISE NOTICE '================================================';
    RAISE NOTICE 'Tables created: %', table_count;
    RAISE NOTICE 'Enum types created: %', enum_count;
    RAISE NOTICE 'Indexes created: %', index_count;
    RAISE NOTICE 'Triggers created: %', trigger_count;
    RAISE NOTICE 'Completion time: %', NOW();
    RAISE NOTICE '================================================';
END $$;