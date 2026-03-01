"""
Security validation utilities for the BlueStream platform.
This module provides comprehensive validation functions for sensitive data fields.
"""

import re
import hashlib
from typing import Tuple, Optional, List
from datetime import datetime, UTC
import secrets
import string

from business_app.utils.user_types import VALID_USER_TYPE_VALUES


class SecurityValidator:
    """Centralized security validation class for all sensitive data."""
    
    # Common weak passwords to reject
    WEAK_PASSWORDS = [
        'password', '123456', 'qwerty', 'admin', 'user', 'test', 
        'password123', 'admin123', '12345678', 'welcome', 'login',
        'root', 'toor', 'pass', 'guest', 'demo', '111111', '000000'
    ]
    
    # Valid roles in the system
    VALID_ROLES = ['customer', 'admin', 'manager', 'delivery_driver', 'operator']
    
    # Valid user statuses
    VALID_STATUSES = ['active', 'inactive', 'banned', 'pending_verification']
    
    # Valid languages
    VALID_LANGUAGES = ['en', 'uz', 'ru', 'tr']
    
    # Valid currencies
    VALID_CURRENCIES = ['UZS', 'USD', 'EUR', 'RUB']
    
    # Valid timezones
    VALID_TIMEZONES = [
        'Asia/Tashkent', 'Europe/Moscow', 'UTC', 'Europe/London',
        'America/New_York', 'Asia/Dubai', 'Asia/Istanbul'
    ]
    
    @staticmethod
    def validate_password_strength(password: str) -> Tuple[bool, str]:
        """
        Validate password meets security requirements.
        
        Args:
            password: The password to validate
            
        Returns:
            Tuple of (is_valid, error_message)
        """
        if not password:
            return False, "Password is required"
        
        if len(password) < 8:
            return False, "Password must be at least 8 characters long"
        
        if len(password) > 128:
            return False, "Password must be less than 128 characters"
        
        if not re.search(r'[A-Z]', password):
            return False, "Password must contain at least one uppercase letter"
        
        if not re.search(r'[a-z]', password):
            return False, "Password must contain at least one lowercase letter"
        
        if not re.search(r'[0-9]', password):
            return False, "Password must contain at least one digit"
        
        if not re.search(r'[!@#$%^&*(),.?":{}|<>]', password):
            return False, "Password must contain at least one special character"
        
        # Check for common weak patterns
        password_lower = password.lower()
        for weak in SecurityValidator.WEAK_PASSWORDS:
            if weak in password_lower:
                return False, f"Password contains weak pattern: {weak}"
        
        # Check for sequential characters
        if re.search(r'(012|123|234|345|456|567|678|789|890)', password):
            return False, "Password contains sequential numbers"
        
        if re.search(r'(abc|bcd|cde|def|efg|fgh|ghi|hij|ijk|jkl|klm|lmn|mno|nop|opq|pqr|qrs|rst|stu|tuv|uvw|vwx|wxy|xyz)', password_lower):
            return False, "Password contains sequential letters"
        
        # Check for repeated characters
        if re.search(r'(.)\1{2,}', password):
            return False, "Password contains too many repeated characters"
        
        return True, "Password is strong"
    
    @staticmethod
    def validate_email(email: str) -> Tuple[bool, str]:
        """
        Validate email format and security requirements.
        
        Args:
            email: The email to validate
            
        Returns:
            Tuple of (is_valid, error_message)
        """
        if not email:
            return False, "Email is required"
        
        if len(email) > 255:
            return False, "Email is too long (max 255 characters)"
        
        # Basic email regex - more comprehensive than simple one
        email_pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
        if not re.match(email_pattern, email):
            return False, "Invalid email format"
        
        if email != email.lower():
            return False, "Email must be lowercase"
        
        # Check for suspicious patterns
        if '..' in email or email.startswith('.') or email.endswith('.'):
            return False, "Email contains invalid dot patterns"
        
        # Validate domain part
        domain = email.split('@')[1]
        if len(domain) > 253:
            return False, "Email domain is too long"
        
        # Check for common test/temp email patterns
        temp_domains = ['temp', 'tempmail', '10minutemail', 'guerrillamail', 'mailinator']
        if any(temp in domain.lower() for temp in temp_domains):
            return False, "Temporary email addresses are not allowed"
        
        return True, "Email is valid"
    
    @staticmethod
    def validate_phone(phone: str) -> Tuple[bool, str]:
        """
        Validate phone number format.
        
        Args:
            phone: The phone number to validate
            
        Returns:
            Tuple of (is_valid, error_message)
        """
        if not phone:
            return True, "Phone is optional"
        
        if len(phone) > 20:
            return False, "Phone number is too long"
        
        # International format starting with +
        phone_pattern = r'^\+[1-9][0-9]{7,14}$'
        if not re.match(phone_pattern, phone):
            return False, "Phone must be in international format (+1234567890)"
        
        return True, "Phone is valid"
    
    @staticmethod
    def sanitize_user_input(input_text: str, max_length: int = None) -> Optional[str]:
        """
        Sanitize user input to prevent XSS and injection attacks.
        
        Args:
            input_text: The text to sanitize
            max_length: Maximum allowed length
            
        Returns:
            Sanitized text or None if empty after sanitization
        """
        if not input_text:
            return input_text
        
        # Remove potentially dangerous characters
        sanitized = re.sub(r'[<>"\'\`&;|$(){}[\]\\]', '', input_text)
        
        # Remove control characters
        sanitized = re.sub(r'[\x00-\x1f\x7f-\x9f]', '', sanitized)
        
        # Trim whitespace
        sanitized = sanitized.strip()
        
        # Check length if specified
        if max_length and len(sanitized) > max_length:
            return None
        
        return sanitized if sanitized else None
    
    @staticmethod
    def validate_telegram_id(telegram_id: str) -> Tuple[bool, str]:
        """
        Validate Telegram ID format.
        
        Args:
            telegram_id: The Telegram ID to validate
            
        Returns:
            Tuple of (is_valid, error_message)
        """
        if not telegram_id:
            return True, "Telegram ID is optional"
        
        if not telegram_id.isdigit():
            return False, "Telegram ID must contain only digits"
        
        if len(telegram_id) < 5 or len(telegram_id) > 15:
            return False, "Telegram ID must be between 5-15 characters"
        
        return True, "Telegram ID is valid"
    
    @staticmethod
    def validate_role(role: str) -> Tuple[bool, str]:
        """
        Validate user role.
        
        Args:
            role: The role to validate
            
        Returns:
            Tuple of (is_valid, error_message)
        """
        if not role:
            return False, "Role is required"
        
        if role not in SecurityValidator.VALID_ROLES:
            return False, f"Role must be one of: {', '.join(SecurityValidator.VALID_ROLES)}"
        
        return True, "Role is valid"
    
    @staticmethod
    def validate_status(status: str) -> Tuple[bool, str]:
        """
        Validate user status.
        
        Args:
            status: The status to validate
            
        Returns:
            Tuple of (is_valid, error_message)
        """
        if not status:
            return False, "Status is required"
        
        if status not in SecurityValidator.VALID_STATUSES:
            return False, f"Status must be one of: {', '.join(SecurityValidator.VALID_STATUSES)}"
        
        return True, "Status is valid"
    
    @staticmethod
    def validate_tax_id(tax_id: str) -> Tuple[bool, str]:
        """
        Validate tax ID format.
        
        Args:
            tax_id: The tax ID to validate
            
        Returns:
            Tuple of (is_valid, error_message)
        """
        if not tax_id:
            return True, "Tax ID is optional"
        
        if len(tax_id) < 5 or len(tax_id) > 20:
            return False, "Tax ID must be between 5-20 characters"
        
        if not re.match(r'^[A-Z0-9-]+$', tax_id):
            return False, "Tax ID must contain only uppercase letters, digits, and dashes"
        
        return True, "Tax ID is valid"
    
    @staticmethod
    def validate_user_type(user_type: str) -> Tuple[bool, str]:
        """
        Validate top-level user type.
        
        Args:
            user_type: The user type to validate
            
        Returns:
            Tuple of (is_valid, error_message)
        """
        if not user_type:
            return True, "User type is optional"

        normalized = user_type.strip().lower()
        if normalized not in VALID_USER_TYPE_VALUES:
            return False, f"User type must be one of: {', '.join(VALID_USER_TYPE_VALUES)}"
        
        return True, "User type is valid"
    
    @staticmethod
    def generate_secure_token(length: int = 32) -> str:
        """
        Generate a cryptographically secure random token.
        
        Args:
            length: Length of the token
            
        Returns:
            Secure random token
        """
        alphabet = string.ascii_letters + string.digits
        return ''.join(secrets.choice(alphabet) for _ in range(length))
    
    @staticmethod
    def hash_sensitive_data(data: str) -> str:
        """
        Hash sensitive data for storage or comparison.
        
        Args:
            data: The data to hash
            
        Returns:
            SHA-256 hash of the data
        """
        return hashlib.sha256(data.encode()).hexdigest()
    
    @staticmethod
    def validate_password_hash(password_hash: str) -> Tuple[bool, str]:
        """
        Validate password hash format.
        
        Args:
            password_hash: The password hash to validate
            
        Returns:
            Tuple of (is_valid, error_message)
        """
        if not password_hash:
            return False, "Password hash is required"
        
        if len(password_hash) < 60:
            return False, "Password hash is too short for bcrypt"
        
        # Check bcrypt format
        if not password_hash.startswith('$2'):
            return False, "Password hash must be in bcrypt format"
        
        # Basic bcrypt pattern check
        bcrypt_pattern = r'^\$2[abxy]?\$[0-9]+\$'
        if not re.match(bcrypt_pattern, password_hash):
            return False, "Invalid bcrypt hash format"
        
        return True, "Password hash is valid"
    
    @staticmethod
    def validate_jwt_token(token: str) -> Tuple[bool, str]:
        """
        Validate JWT token format.
        
        Args:
            token: The JWT token to validate
            
        Returns:
            Tuple of (is_valid, error_message)
        """
        if not token:
            return False, "Token is required"
        
        if len(token) < 100 or len(token) > 2000:
            return False, "Token length is invalid for JWT"
        
        # JWT tokens have 3 parts separated by dots
        parts = token.split('.')
        if len(parts) != 3:
            return False, "JWT must have exactly 3 parts separated by dots"
        
        # Check that each part contains only valid base64 characters
        base64_pattern = r'^[A-Za-z0-9_-]+$'
        for part in parts:
            if not re.match(base64_pattern, part):
                return False, "JWT contains invalid characters"
        
        return True, "JWT token format is valid"
    
    @classmethod
    def validate_all_user_fields(cls, user_data: dict) -> List[str]:
        """
        Validate all user fields at once.
        
        Args:
            user_data: Dictionary containing user data
            
        Returns:
            List of validation error messages
        """
        errors = []
        
        # Email validation
        if 'email' in user_data:
            is_valid, message = cls.validate_email(user_data['email'])
            if not is_valid:
                errors.append(f"Email: {message}")
        
        # Phone validation
        if 'phone' in user_data:
            is_valid, message = cls.validate_phone(user_data['phone'])
            if not is_valid:
                errors.append(f"Phone: {message}")
        
        # Role validation
        if 'role' in user_data:
            is_valid, message = cls.validate_role(user_data['role'])
            if not is_valid:
                errors.append(f"Role: {message}")
        
        # Status validation
        if 'status' in user_data:
            is_valid, message = cls.validate_status(user_data['status'])
            if not is_valid:
                errors.append(f"Status: {message}")
        
        # Telegram ID validation
        if 'telegram_id' in user_data:
            is_valid, message = cls.validate_telegram_id(user_data['telegram_id'])
            if not is_valid:
                errors.append(f"Telegram ID: {message}")
        
        # Tax ID validation
        if 'tax_id' in user_data:
            is_valid, message = cls.validate_tax_id(user_data['tax_id'])
            if not is_valid:
                errors.append(f"Tax ID: {message}")
        
        # User type validation
        if 'user_type' in user_data:
            is_valid, message = cls.validate_user_type(user_data['user_type'])
            if not is_valid:
                errors.append(f"User type: {message}")
        
        # Name field sanitization and validation
        name_fields = ['first_name', 'last_name', 'company_name']
        for field in name_fields:
            if field in user_data and user_data[field]:
                max_length = 200 if field == 'company_name' else 100
                sanitized = cls.sanitize_user_input(user_data[field], max_length)
                if not sanitized:
                    errors.append(f"{field.replace('_', ' ').title()}: Contains invalid characters or is too long")
                else:
                    user_data[field] = sanitized
        
        return errors


# Validation decorators for API endpoints
def validate_password_strength(func):
    """Decorator to validate password strength in API endpoints."""
    def wrapper(*args, **kwargs):
        from flask import request, jsonify
        
        data = request.get_json()
        if data and 'password' in data:
            is_valid, message = SecurityValidator.validate_password_strength(data['password'])
            if not is_valid:
                return jsonify({'error': f'Password validation failed: {message}'}), 400
        
        return func(*args, **kwargs)
    
    wrapper.__name__ = func.__name__
    return wrapper


def validate_user_data(func):
    """Decorator to validate user data in API endpoints."""
    def wrapper(*args, **kwargs):
        from flask import request, jsonify
        
        data = request.get_json()
        if data:
            errors = SecurityValidator.validate_all_user_fields(data)
            if errors:
                return jsonify({'error': 'Validation failed', 'details': errors}), 400
        
        return func(*args, **kwargs)
    
    wrapper.__name__ = func.__name__
    return wrapper
