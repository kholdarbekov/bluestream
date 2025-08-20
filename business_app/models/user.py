from datetime import datetime, timedelta, timezone
from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, Text, ForeignKey, Index
from sqlalchemy.orm import relationship, backref
from werkzeug.security import generate_password_hash, check_password_hash
from flask_sqlalchemy import SQLAlchemy
from business_app import db
from business_app.models import TimestampMixin



class User(db.Model, TimestampMixin):
    __tablename__ = 'users'
    
    id = Column(Integer, primary_key=True)
    email = Column(String(255), unique=True, nullable=False)
    phone = Column(String(20), unique=True, nullable=True)
    password_hash = Column(String(255), nullable=False)
    first_name = Column(String(100), nullable=True)
    last_name = Column(String(100), nullable=True)
    full_name = Column(String(200), nullable=True)
    date_of_birth = Column(DateTime, nullable=True)
    gender = Column(String(10), nullable=True)
    role = Column(String(20), default='customer', index=True)
    status = Column(String(20), default='active', index=True)
    is_verified = Column(Boolean, default=False, index=True)
    is_premium = Column(Boolean, default=False)
    preferred_language = Column(String(5), default='en')
    preferred_currency = Column(String(3), default='UZS')
    timezone = Column(String(50), default='Asia/Tashkent')
    email_notifications = Column(Boolean, default=True)
    sms_notifications = Column(Boolean, default=True)
    push_notifications = Column(Boolean, default=True)
    company_name = Column(String(200), nullable=True)
    tax_id = Column(String(50), nullable=True)
    business_type = Column(String(50), nullable=True)
    last_login = Column(DateTime, nullable=True)
    failed_login_attempts = Column(Integer, default=0)
    account_locked_until = Column(DateTime, nullable=True)
    password_reset_token = Column(String(255), nullable=True)
    password_reset_expires = Column(DateTime, nullable=True)
    email_verification_token = Column(String(255), nullable=True)
    email_verified_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    telegram_id = Column(String(50), unique=True, nullable=True, index=True)
    registration_source = Column(String(50), default='web', index=True)
    
    # Telegram/Bot-specific fields
    telegram_username = Column(String(255), nullable=True)
    telegram_first_name = Column(String(255), nullable=True)
    telegram_last_name = Column(String(255), nullable=True)
    telegram_language_code = Column(String(10), nullable=True)
    is_bot_active = Column(Boolean, default=False, index=True)
    bot_state = Column(Text, nullable=True)  # JSON string for bot conversation state
    last_bot_interaction = Column(DateTime, nullable=True)

    # Relationships
    addresses = relationship('UserAddress', back_populates='user', cascade='all, delete-orphan')
    orders = relationship('Order', back_populates='user')
    subscriptions = relationship('Subscription', back_populates='user')
    payments = relationship('Payment', back_populates='user')
    loyalty_transactions = relationship('LoyaltyTransaction', back_populates='user')
    reviews = relationship('Review', back_populates='user')
    notifications = relationship('Notification', back_populates='user')
    deliveries = relationship('Delivery', foreign_keys='Delivery.delivery_person_id', back_populates='delivery_person')
    
    def set_password(self, password):
        self.password_hash = generate_password_hash(password)
    
    def check_password(self, password):
        if not self.password_hash:
            return False
        return check_password_hash(self.password_hash, password)
    
    @staticmethod
    def validate_password_strength(password):
        """Validate password meets security requirements"""
        import re
        
        if not password or len(password) < 8:
            return False, "Password must be at least 8 characters long"
        
        if not re.search(r'[A-Z]', password):
            return False, "Password must contain at least one uppercase letter"
        
        if not re.search(r'[a-z]', password):
            return False, "Password must contain at least one lowercase letter"
        
        if not re.search(r'[0-9]', password):
            return False, "Password must contain at least one digit"
        
        if not re.search(r'[!@#$%^&*(),.?":{}|<>]', password):
            return False, "Password must contain at least one special character"
        
        # Check for common weak patterns
        weak_patterns = ['password', '123456', 'qwerty', 'admin', 'user', 'test']
        if any(weak in password.lower() for weak in weak_patterns):
            return False, "Password contains common weak patterns"
        
        return True, "Password is strong"
    
    @staticmethod
    def validate_email(email):
        """Validate email format"""
        import re
        
        if not email:
            return False, "Email is required"
        
        # Basic email regex
        email_pattern = r'^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$'
        if not re.match(email_pattern, email):
            return False, "Invalid email format"
        
        if email != email.lower():
            return False, "Email must be lowercase"
        
        return True, "Email is valid"
    
    @staticmethod
    def validate_phone(phone):
        """Validate phone number format"""
        import re
        
        if not phone:
            return True, "Phone is optional"  # Phone is optional
        
        # International format starting with +
        phone_pattern = r'^\+[1-9][0-9]{7,14}$'
        if not re.match(phone_pattern, phone):
            return False, "Phone must be in international format (+1234567890)"
        
        return True, "Phone is valid"
    
    @staticmethod
    def sanitize_user_input(input_text):
        """Sanitize user input to prevent XSS and injection"""
        import re
        
        if not input_text:
            return input_text
        
        # Remove potentially dangerous characters
        sanitized = re.sub(r'[<>"\'\`&;|$(){}[\]\\]', '', input_text)
        
        # Trim whitespace
        sanitized = sanitized.strip()
        
        return sanitized if sanitized else None
    
    def validate_user_data(self):
        """Validate all user data before saving"""
        errors = []
        
        # Validate email
        is_valid, message = self.validate_email(self.email)
        if not is_valid:
            errors.append(f"Email: {message}")
        
        # Validate phone if provided
        is_valid, message = self.validate_phone(self.phone)
        if not is_valid:
            errors.append(f"Phone: {message}")
        
        # Validate role
        valid_roles = ['customer', 'admin', 'manager', 'delivery_driver', 'operator']
        if self.role not in valid_roles:
            errors.append(f"Role must be one of: {', '.join(valid_roles)}")
        
        # Validate status
        valid_statuses = ['active', 'inactive', 'banned', 'pending_verification']
        if self.status not in valid_statuses:
            errors.append(f"Status must be one of: {', '.join(valid_statuses)}")
        
        # Validate names if provided
        if self.first_name:
            sanitized = self.sanitize_user_input(self.first_name)
            if not sanitized or len(sanitized) > 100:
                errors.append("First name contains invalid characters or is too long")
            else:
                self.first_name = sanitized
        
        if self.last_name:
            sanitized = self.sanitize_user_input(self.last_name)
            if not sanitized or len(sanitized) > 100:
                errors.append("Last name contains invalid characters or is too long")
            else:
                self.last_name = sanitized
        
        # Validate telegram_id if provided
        if self.telegram_id:
            if not self.telegram_id.isdigit() or len(self.telegram_id) < 5 or len(self.telegram_id) > 15:
                errors.append("Telegram ID must be a numeric string between 5-15 characters")
        
        # Validate business fields if provided
        if self.company_name:
            sanitized = self.sanitize_user_input(self.company_name)
            if not sanitized or len(sanitized) > 200:
                errors.append("Company name contains invalid characters or is too long")
            else:
                self.company_name = sanitized
        
        if self.tax_id:
            import re
            if not re.match(r'^[A-Z0-9-]+$', self.tax_id) or len(self.tax_id) < 5 or len(self.tax_id) > 20:
                errors.append("Tax ID must contain only alphanumeric characters and dashes, 5-20 characters long")
        
        return errors
    
    def to_dict(self):
        return {
            'id': self.id,
            'phone': self.phone,
            'email': self.email,
            'first_name': self.first_name,
            'last_name': self.last_name,
            'full_name': self.full_name or '',
            'role': self.role,
            'status': self.status,
            'is_verified': self.is_verified,
            'is_premium': self.is_premium,
            'preferred_language': self.preferred_language,
            'telegram_id': self.telegram_id,
            'registration_source': self.registration_source,
            'telegram_username': self.telegram_username,
            'telegram_first_name': self.telegram_first_name,
            'telegram_last_name': self.telegram_last_name,
            'telegram_language_code': self.telegram_language_code,
            'is_bot_active': self.is_bot_active,
            'bot_state': self.bot_state,
            'last_bot_interaction': self.last_bot_interaction.isoformat() if self.last_bot_interaction else None,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }

class UserAddress(db.Model, TimestampMixin):
    __tablename__ = 'addresses'

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    title = Column(String(100), nullable=True)
    full_address = Column(Text, nullable=False)
    street_address = Column(String(255), nullable=True)
    city = Column(String(100), nullable=True, default='Tashkent')
    district = Column(String(100), nullable=True)
    postal_code = Column(String(20), nullable=True)
    country = Column(String(100), nullable=True, default='Uzbekistan')
    latitude = Column(Float, nullable=True)
    longitude = Column(Float, nullable=True)
    is_default = Column(Boolean, default=False)
    is_business = Column(Boolean, default=False)
    delivery_instructions = Column(Text, nullable=True)
    landmark = Column(String(255), nullable=True)
    floor_number = Column(String(20), nullable=True)
    apartment_number = Column(String(20), nullable=True)
    
    user = relationship('User', back_populates='addresses')
    orders = relationship('Order', back_populates='delivery_address')

    def to_dict(self):
        return {
            'id': self.id,
            'title': self.title,
            'full_address': self.full_address,
            'street_address': self.street_address,
            'city': self.city,
            'district': self.district,
            'postal_code': self.postal_code,
            'country': self.country,
            'latitude': float(self.latitude) if self.latitude else None,
            'longitude': float(self.longitude) if self.longitude else None,
            'is_default': self.is_default,
            'is_business': self.is_business,
            'delivery_instructions': self.delivery_instructions,
            'landmark': self.landmark,
            'floor_number': self.floor_number,
            'apartment_number': self.apartment_number
        }


class UserSession(db.Model, TimestampMixin):
    """User session model for tracking authentication sessions"""
    __tablename__ = 'user_sessions'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    session_token = Column(String(255), unique=True, nullable=False, index=True)
    device_info = Column(String(255), nullable=True)
    ip_address = Column(String(45), nullable=True)
    user_agent = Column(String(500), nullable=True)
    expires_at = Column(DateTime, nullable=False)
    is_active = Column(Boolean, default=True, index=True)
    last_activity = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    ended_at = Column(DateTime, nullable=True)
    
    user = relationship('User', backref='sessions')
    
    def is_expired(self):
        """Check if session is expired"""
        return datetime.now(timezone.utc) > self.expires_at
    
    def to_dict(self):
        return {
            'id': self.id,
            'user_id': self.user_id,
            'session_token': self.session_token,
            'device_info': self.device_info,
            'ip_address': self.ip_address,
            'expires_at': self.expires_at.isoformat() if self.expires_at else None,
            'is_active': self.is_active,
            'last_activity': self.last_activity.isoformat() if self.last_activity else None,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }