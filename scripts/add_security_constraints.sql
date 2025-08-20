-- Security constraints for password_hash and sensitive fields
-- This script adds proper database constraints to ensure data integrity and security
-- Run this script to enhance security of sensitive data fields

BEGIN;

-- 1. Password hash constraints
-- Ensure password_hash is never empty and has minimum length
ALTER TABLE users 
    ADD CONSTRAINT check_password_hash_not_empty 
    CHECK (password_hash IS NOT NULL AND LENGTH(password_hash) > 0);

ALTER TABLE users 
    ADD CONSTRAINT check_password_hash_min_length 
    CHECK (LENGTH(password_hash) >= 60); -- bcrypt hashes are typically 60 characters

-- Ensure password_hash looks like a proper hash (starts with $2b$ for bcrypt)
ALTER TABLE users 
    ADD CONSTRAINT check_password_hash_format 
    CHECK (password_hash ~ '^\$2[abxy]?\$[0-9]+\$');

-- 2. Email constraints
-- Ensure email format is valid
ALTER TABLE users 
    ADD CONSTRAINT check_email_format 
    CHECK (email ~ '^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$');

-- Ensure email is lowercase (for consistency)
ALTER TABLE users 
    ADD CONSTRAINT check_email_lowercase 
    CHECK (email = LOWER(email));

-- Ensure email is not empty
ALTER TABLE users 
    ADD CONSTRAINT check_email_not_empty 
    CHECK (LENGTH(TRIM(email)) > 0);

-- 3. Phone number constraints  
-- Ensure phone format (international format starting with +)
ALTER TABLE users 
    ADD CONSTRAINT check_phone_format 
    CHECK (phone IS NULL OR phone ~ '^\+[1-9][0-9]{7,14}$');

-- Delivery person phone constraints
ALTER TABLE delivery_personnel 
    ADD CONSTRAINT check_delivery_phone_format 
    CHECK (phone ~ '^\+[1-9][0-9]{7,14}$');

ALTER TABLE delivery_personnel 
    ADD CONSTRAINT check_delivery_phone_not_empty 
    CHECK (LENGTH(TRIM(phone)) > 0);

-- Emergency contact phone format
ALTER TABLE delivery_personnel 
    ADD CONSTRAINT check_emergency_phone_format 
    CHECK (emergency_contact_phone IS NULL OR emergency_contact_phone ~ '^\+[1-9][0-9]{7,14}$');

-- 4. Security token constraints
-- Password reset tokens should be secure random strings
ALTER TABLE users 
    ADD CONSTRAINT check_password_reset_token_format 
    CHECK (password_reset_token IS NULL OR LENGTH(password_reset_token) >= 32);

-- Email verification tokens should be secure
ALTER TABLE users 
    ADD CONSTRAINT check_email_verification_token_format 
    CHECK (email_verification_token IS NULL OR LENGTH(email_verification_token) >= 32);

-- Ensure token expiry is in the future when token exists
ALTER TABLE users 
    ADD CONSTRAINT check_password_reset_token_expiry 
    CHECK (
        (password_reset_token IS NULL AND password_reset_expires IS NULL) OR
        (password_reset_token IS NOT NULL AND password_reset_expires > NOW())
    );

-- 5. Role and status constraints
-- Ensure valid role values
ALTER TABLE users 
    ADD CONSTRAINT check_user_role_valid 
    CHECK (role IN ('customer', 'admin', 'manager', 'delivery_driver', 'operator'));

-- Ensure valid status values
ALTER TABLE users 
    ADD CONSTRAINT check_user_status_valid 
    CHECK (status IN ('active', 'inactive', 'banned', 'pending_verification'));

-- Ensure valid gender values
ALTER TABLE users 
    ADD CONSTRAINT check_user_gender_valid 
    CHECK (gender IS NULL OR gender IN ('male', 'female', 'other', 'prefer_not_to_say'));

-- 6. Business logic constraints
-- Failed login attempts should be non-negative
ALTER TABLE users 
    ADD CONSTRAINT check_failed_login_attempts_positive 
    CHECK (failed_login_attempts >= 0);

-- Ensure reasonable failed login attempts limit
ALTER TABLE users 
    ADD CONSTRAINT check_failed_login_attempts_reasonable 
    CHECK (failed_login_attempts <= 100);

-- Account lock should only be set when there are failed attempts
ALTER TABLE users 
    ADD CONSTRAINT check_account_lock_logic 
    CHECK (
        (account_locked_until IS NULL) OR 
        (account_locked_until > NOW() AND failed_login_attempts > 0)
    );

-- 7. Name field constraints
-- Ensure names don't contain obviously malicious content
ALTER TABLE users 
    ADD CONSTRAINT check_first_name_safe 
    CHECK (first_name IS NULL OR (
        LENGTH(TRIM(first_name)) > 0 AND
        first_name !~ '[<>"\''`&;|$(){}[\]\\]' AND
        LENGTH(first_name) <= 100
    ));

ALTER TABLE users 
    ADD CONSTRAINT check_last_name_safe 
    CHECK (last_name IS NULL OR (
        LENGTH(TRIM(last_name)) > 0 AND
        last_name !~ '[<>"\''`&;|$(){}[\]\\]' AND
        LENGTH(last_name) <= 100
    ));

-- 8. Telegram ID constraints
-- Ensure telegram_id is numeric string if provided
ALTER TABLE users 
    ADD CONSTRAINT check_telegram_id_format 
    CHECK (telegram_id IS NULL OR telegram_id ~ '^[0-9]+$');

-- Ensure telegram_id is reasonable length
ALTER TABLE users 
    ADD CONSTRAINT check_telegram_id_length 
    CHECK (telegram_id IS NULL OR LENGTH(telegram_id) BETWEEN 5 AND 15);

-- 9. Language and timezone constraints
-- Ensure valid language codes
ALTER TABLE users 
    ADD CONSTRAINT check_preferred_language_valid 
    CHECK (preferred_language IN ('en', 'uz', 'ru', 'tr'));

-- Ensure valid currency codes
ALTER TABLE users 
    ADD CONSTRAINT check_preferred_currency_valid 
    CHECK (preferred_currency IN ('UZS', 'USD', 'EUR', 'RUB'));

-- Ensure valid timezone
ALTER TABLE users 
    ADD CONSTRAINT check_timezone_valid 
    CHECK (timezone IN (
        'Asia/Tashkent', 'Europe/Moscow', 'UTC', 'Europe/London', 
        'America/New_York', 'Asia/Dubai', 'Asia/Istanbul'
    ));

-- 10. Business fields constraints
-- Tax ID format (if provided)
ALTER TABLE users 
    ADD CONSTRAINT check_tax_id_format 
    CHECK (tax_id IS NULL OR (
        LENGTH(TRIM(tax_id)) > 0 AND
        tax_id ~ '^[A-Z0-9-]+$' AND
        LENGTH(tax_id) BETWEEN 5 AND 20
    ));

-- Company name safety
ALTER TABLE users 
    ADD CONSTRAINT check_company_name_safe 
    CHECK (company_name IS NULL OR (
        LENGTH(TRIM(company_name)) > 0 AND
        company_name !~ '[<>"\''`&;|$(){}[\]\\]' AND
        LENGTH(company_name) <= 200
    ));

-- Business type validation
ALTER TABLE users 
    ADD CONSTRAINT check_business_type_valid 
    CHECK (business_type IS NULL OR business_type IN (
        'individual', 'small_business', 'corporation', 'non_profit', 'government'
    ));

-- 11. Notification recipient constraints (for notification tables)
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'notifications') THEN
        -- Ensure recipient email format
        ALTER TABLE notifications 
            ADD CONSTRAINT check_recipient_email_format 
            CHECK (recipient_email IS NULL OR recipient_email ~ '^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$');
        
        -- Ensure recipient phone format
        ALTER TABLE notifications 
            ADD CONSTRAINT check_recipient_phone_format 
            CHECK (recipient_phone IS NULL OR recipient_phone ~ '^\+[1-9][0-9]{7,14}$');
        
        -- Ensure at least one recipient method is provided
        ALTER TABLE notifications 
            ADD CONSTRAINT check_notification_recipient_exists 
            CHECK (
                user_id IS NOT NULL OR 
                recipient_email IS NOT NULL OR 
                recipient_phone IS NOT NULL OR 
                recipient_telegram_id IS NOT NULL
            );
    END IF;
END
$$;

-- 12. JWT Token constraints (if blacklist table exists)
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'jwt_blacklist') THEN
        -- Ensure token is not empty
        ALTER TABLE jwt_blacklist 
            ADD CONSTRAINT check_jwt_token_not_empty 
            CHECK (LENGTH(TRIM(token)) > 0);
        
        -- Ensure token has reasonable length (JWT tokens are typically long)
        ALTER TABLE jwt_blacklist 
            ADD CONSTRAINT check_jwt_token_length 
            CHECK (LENGTH(token) BETWEEN 100 AND 2000);
        
        -- Ensure expiry is in the future or token is immediately invalid
        ALTER TABLE jwt_blacklist 
            ADD CONSTRAINT check_jwt_expires_reasonable 
            CHECK (expires_at > created_at);
    END IF;
END
$$;

-- 13. Payment sensitive data constraints
-- Ensure transaction IDs are not empty when provided
ALTER TABLE payment_transactions 
    ADD CONSTRAINT check_transaction_id_not_empty 
    CHECK (transaction_id IS NULL OR LENGTH(TRIM(transaction_id)) > 0);

-- Ensure external reference format
ALTER TABLE payment_transactions 
    ADD CONSTRAINT check_external_reference_format 
    CHECK (external_reference IS NULL OR (
        LENGTH(TRIM(external_reference)) > 0 AND
        LENGTH(external_reference) <= 255
    ));

-- 14. API key constraints (if api_keys table exists)
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'api_keys') THEN
        -- Ensure API key is long enough
        ALTER TABLE api_keys 
            ADD CONSTRAINT check_api_key_length 
            CHECK (LENGTH(key_hash) >= 60);
        
        -- Ensure API key name is safe
        ALTER TABLE api_keys 
            ADD CONSTRAINT check_api_key_name_safe 
            CHECK (
                LENGTH(TRIM(name)) > 0 AND
                name !~ '[<>"\''`&;|$(){}[\]\\]' AND
                LENGTH(name) <= 100
            );
    END IF;
END
$$;

-- 15. Add comments for documentation
COMMENT ON CONSTRAINT check_password_hash_not_empty ON users IS 'Ensures password hash is never null or empty';
COMMENT ON CONSTRAINT check_password_hash_min_length ON users IS 'Ensures password hash has minimum bcrypt length';
COMMENT ON CONSTRAINT check_password_hash_format ON users IS 'Ensures password hash follows bcrypt format';
COMMENT ON CONSTRAINT check_email_format ON users IS 'Validates email address format';
COMMENT ON CONSTRAINT check_email_lowercase ON users IS 'Ensures email is stored in lowercase';
COMMENT ON CONSTRAINT check_phone_format ON users IS 'Validates international phone number format';
COMMENT ON CONSTRAINT check_user_role_valid ON users IS 'Restricts user roles to valid values';
COMMENT ON CONSTRAINT check_user_status_valid ON users IS 'Restricts user status to valid values';
COMMENT ON CONSTRAINT check_failed_login_attempts_positive ON users IS 'Ensures failed login attempts is non-negative';
COMMENT ON CONSTRAINT check_account_lock_logic ON users IS 'Ensures account lock is only set with failed attempts';
COMMENT ON CONSTRAINT check_telegram_id_format ON users IS 'Validates Telegram ID as numeric string';

-- 16. Create function to validate password strength (for application use)
CREATE OR REPLACE FUNCTION validate_password_strength(password TEXT) 
RETURNS BOOLEAN AS $$
BEGIN
    -- Password must be at least 8 characters
    IF LENGTH(password) < 8 THEN
        RETURN FALSE;
    END IF;
    
    -- Must contain at least one uppercase letter
    IF password !~ '[A-Z]' THEN
        RETURN FALSE;
    END IF;
    
    -- Must contain at least one lowercase letter
    IF password !~ '[a-z]' THEN
        RETURN FALSE;
    END IF;
    
    -- Must contain at least one digit
    IF password !~ '[0-9]' THEN
        RETURN FALSE;
    END IF;
    
    -- Must contain at least one special character
    IF password !~ '[!@#$%^&*(),.?":{}|<>]' THEN
        RETURN FALSE;
    END IF;
    
    -- Must not contain common weak patterns
    IF password ~* 'password|123456|qwerty|admin|user|test' THEN
        RETURN FALSE;
    END IF;
    
    RETURN TRUE;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION validate_password_strength(TEXT) IS 'Validates password meets security requirements';

-- 17. Create function to sanitize user input
CREATE OR REPLACE FUNCTION sanitize_user_input(input TEXT) 
RETURNS TEXT AS $$
BEGIN
    IF input IS NULL THEN
        RETURN NULL;
    END IF;
    
    -- Remove potentially dangerous characters
    input := REGEXP_REPLACE(input, '[<>"\''`&;|$(){}[\]\\]', '', 'g');
    
    -- Trim whitespace
    input := TRIM(input);
    
    -- Return null if empty after sanitization
    IF LENGTH(input) = 0 THEN
        RETURN NULL;
    END IF;
    
    RETURN input;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION sanitize_user_input(TEXT) IS 'Sanitizes user input by removing dangerous characters';

-- 18. Analyze tables after adding constraints
ANALYZE users;
ANALYZE payment_transactions;

COMMIT;

-- Summary of constraints added:
-- ✓ Password hash format and length validation
-- ✓ Email format and case validation  
-- ✓ Phone number international format validation
-- ✓ Security token length and format validation
-- ✓ Role and status enumeration validation
-- ✓ Failed login attempts business logic validation
-- ✓ Name field safety validation (XSS prevention)
-- ✓ Telegram ID format validation
-- ✓ Language, currency, and timezone validation
-- ✓ Business field format validation
-- ✓ Notification recipient validation
-- ✓ JWT token format validation
-- ✓ Payment transaction data validation
-- ✓ Helper functions for password validation and input sanitization

RAISE NOTICE 'Security constraints added successfully!';
RAISE NOTICE 'Password hash, email, phone, and other sensitive fields now have proper validation.';
RAISE NOTICE 'Use validate_password_strength() function in application code for password validation.';
RAISE NOTICE 'Use sanitize_user_input() function for user input sanitization.';