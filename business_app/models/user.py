import re
from datetime import datetime, timedelta, timezone
from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, Text, ForeignKey, Index, Enum
from sqlalchemy.orm import relationship, backref
from flask_sqlalchemy import SQLAlchemy
from business_app import db
from business_app.models import TimestampMixin
from business_app.utils.constants import UserRole, UserStatus, UserGender
from shared.constants import DISPLAY_TIMEZONE
from shared.validators import (
    validate_password_strength, validate_email, sanitize_user_input,
    validate_uzbekistan_phone
)


class User(db.Model, TimestampMixin):
    __tablename__ = 'users'
    
    id = Column(Integer, primary_key=True)
    first_name = Column(String(100), nullable=True)
    last_name = Column(String(100), nullable=True)
    email = Column(String(255), unique=True, nullable=True, index=True)
    phone = Column(String(20), unique=True, nullable=True, index=True)  # Nullable for telegram registration, required before ordering
    password_hash = Column(String(255), nullable=False)
    date_of_birth = Column(DateTime(timezone=True), nullable=True)
    gender = Column(Enum(UserGender, name='user_gender', values_callable=lambda x: [e.value for e in x]), default=UserGender.UNKNOWN, index=True)
    role = Column(Enum(UserRole, name='user_role', values_callable=lambda x: [e.value for e in x]), default=UserRole.CUSTOMER, index=True)
    status = Column(Enum(UserStatus, name='user_status', values_callable=lambda x: [e.value for e in x]), default=UserStatus.ACTIVE, index=True)
    is_verified = Column(Boolean, default=False, index=True)
    is_premium = Column(Boolean, default=False)
    preferred_language = Column(String(5), default='en')
    preferred_currency = Column(String(3), default='UZS')
    timezone = Column(String(50), default=DISPLAY_TIMEZONE)
    
    # Notification preferences
    email_notifications = Column(Boolean, default=True)
    sms_notifications = Column(Boolean, default=True)
    push_notifications = Column(Boolean, default=True)
    
    # business account fields
    company_name = Column(String(200), nullable=True)
    tax_id = Column(String(50), nullable=True)
    business_type = Column(String(50), nullable=True)

    last_login = Column(DateTime(timezone=True), nullable=True)
    failed_login_attempts = Column(Integer, default=0)
    account_locked_until = Column(DateTime(timezone=True), nullable=True)
    password_reset_token = Column(String(255), nullable=True)
    password_reset_expires = Column(DateTime(timezone=True), nullable=True)
    email_verification_token = Column(String(255), nullable=True)
    email_verified_at = Column(DateTime(timezone=True), nullable=True)
    phone_verified_at = Column(DateTime(timezone=True), nullable=True)
    # created_at and updated_at provided by TimestampMixin (timezone-aware)
    registration_source = Column(String(50), default='web', index=True)
    registration_method = Column(String(20), default='email', index=True)  # 'email', 'phone', 'telegram'

    # Telegram/Bot-specific fields
    telegram_id = Column(String(50), unique=True, nullable=True, index=True)
    telegram_username = Column(String(255), nullable=True)
    is_bot_active = Column(Boolean, default=False, index=True)
    bot_state = Column(Text, nullable=True)  # JSON string for bot conversation state
    last_bot_interaction = Column(DateTime(timezone=True), nullable=True)

    # Cart
    cart = relationship("Cart", back_populates="user", uselist=False)

    # Relationships
    addresses = relationship('UserAddress', back_populates='user', cascade='all, delete-orphan')
    orders = relationship('Order', back_populates='user')
    subscriptions = relationship('Subscription', back_populates='user')
    payments = relationship('Payment', back_populates='user')
    loyalty_transactions = relationship('LoyaltyTransaction', back_populates='user')
    reviews = relationship('Review', back_populates='user')
    notifications = relationship('Notification', back_populates='user')
    deliveries = relationship('Delivery', foreign_keys='Delivery.delivery_person_id', back_populates='delivery_person')

    @property
    def full_name(self) -> str:
        """Get full name by combining first and last names"""
        parts = []
        if self.first_name:
            parts.append(self.first_name)
        if self.last_name:
            parts.append(self.last_name)
        return ' '.join(parts)
    @property
    def email_verified(self) -> bool:
        """Check if email is verified"""
        return self.email_verified_at is not None

    @property
    def phone_verified(self) -> bool:
        """Check if phone is verified"""
        return self.phone_verified_at is not None

    @property
    def is_admin(self) -> bool:
        """Check if user has admin role"""
        return self.role == UserRole.ADMIN if isinstance(self.role, UserRole) else self.role == UserRole.ADMIN.value

    def validate_user_data(self):
        """Validate all user data before saving"""
        errors = []

        # Validate email (shared validator)
        is_valid, message = validate_email(self.email)
        if not is_valid:
            errors.append(f"Email: {message}")

        # Validate phone if provided (shared Uzbekistan validator)
        if self.phone:
            is_valid, message, normalized = validate_uzbekistan_phone(self.phone)
            if not is_valid:
                errors.append(f"Phone: {message}")
            elif normalized:
                self.phone = normalized

        # Validate role
        if self.role and not isinstance(self.role, UserRole):
            try:
                self.role = UserRole(self.role) if isinstance(self.role, str) else self.role
            except ValueError:
                valid_roles = [r.value for r in UserRole]
                errors.append(f"Role must be one of: {', '.join(valid_roles)}")

        # Validate status
        if self.status and not isinstance(self.status, UserStatus):
            try:
                self.status = UserStatus(self.status) if isinstance(self.status, str) else self.status
            except ValueError:
                valid_statuses = [s.value for s in UserStatus]
                errors.append(f"Status must be one of: {', '.join(valid_statuses)}")

        # Validate names if provided (shared sanitizer)
        if self.first_name:
            sanitized = sanitize_user_input(self.first_name)
            if not sanitized or len(sanitized) > 100:
                errors.append("First name contains invalid characters or is too long")
            else:
                self.first_name = sanitized

        if self.last_name:
            sanitized = sanitize_user_input(self.last_name)
            if not sanitized or len(sanitized) > 100:
                errors.append("Last name contains invalid characters or is too long")
            else:
                self.last_name = sanitized

        # Validate telegram_id if provided
        if self.telegram_id:
            if not self.telegram_id.isdigit() or len(self.telegram_id) < 5 or len(self.telegram_id) > 15:
                errors.append("Telegram ID must be a numeric string between 5-15 characters")

        # Validate business fields if provided (shared sanitizer)
        if self.company_name:
            sanitized = sanitize_user_input(self.company_name)
            if not sanitized or len(sanitized) > 200:
                errors.append("Company name contains invalid characters or is too long")
            else:
                self.company_name = sanitized

        if self.tax_id:
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
            'full_name': f"{self.first_name} {self.last_name}".strip() or '',
            'role': self.role.value if isinstance(self.role, UserRole) else self.role,
            'status': self.status.value if isinstance(self.status, UserStatus) else self.status,
            'is_verified': self.is_verified,
            'is_premium': self.is_premium,
            'preferred_language': self.preferred_language,
            'telegram_id': self.telegram_id,
            'registration_source': self.registration_source,
            'registration_method': self.registration_method,
            'telegram_username': self.telegram_username,
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
    expires_at = Column(DateTime(timezone=True), nullable=False)
    is_active = Column(Boolean, default=True, index=True)
    last_activity = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    ended_at = Column(DateTime(timezone=True), nullable=True)
    
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