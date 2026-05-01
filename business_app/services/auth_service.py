"""
Authentication service for the Water Business Platform
"""

import secrets
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, Tuple, List
from flask import current_app, request
from flask_jwt_extended import get_jwt_identity
import redis
from sqlalchemy.exc import IntegrityError

from business_app.models.user import User, UserAddress, UserSession
from business_app.utils.exceptions import ValidationError, UnauthorizedError, ConflictError, NotFoundError
from business_app.utils.security_validators import SecurityValidator
from business_app.utils.validators import EmailValidator, PhoneValidator, PasswordValidator
from business_app.utils.helpers import generate_otp, format_phone_number, generate_random_string
from business_app.utils.password_security import hash_password, verify_password, needs_password_rehash
from shared.enums import UserRole, UserStatus, UserType
from business_app.utils.user_types import infer_non_staff_user_type, normalize_entity_subtype
from shared.enums import EntitySubtype
from business_app.utils.translations import get_translation
from business_app.tasks.notification_tasks import send_verification_sms_task
from business_app import db

logger = logging.getLogger(__name__)


class AuthService:
    """Authentication and authorization service"""

    def __init__(self):
        # Get Redis URL with proper fallback
        import os

        try:
            redis_url = current_app.config["REDIS_URL"]
        except RuntimeError:
            # No application context available, use environment variable
            redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

        self.redis_client = redis.from_url(redis_url)
        self.otp_expiry = 300  # 5 minutes
        self.max_login_attempts = current_app.config.get("MAX_LOGIN_ATTEMPTS", 5)
        self.lockout_duration = current_app.config.get("LOCKOUT_DURATION", 1800)  # 30 minutes

    def register_user(
        self, email: str, password: str, phone: str = None, first_name: str = None, last_name: str = None, **kwargs
    ) -> Tuple[User, Dict[str, str]]:
        """
        Register a new user (email-based registration)

        Args:
            email: User email (required)
            password: User password (required)
            phone: User phone number (optional, can be added later)
            first_name: User first name (required)
            last_name: User last name (optional)
            **kwargs: Additional user data including:
                - registration_method: 'email' or 'phone' (default: 'email')

        Returns:
            Tuple of (User object, tokens dict)

        Raises:
            ValidationError: If validation fails
            ConflictError: If user already exists
        """
        # Get registration method from kwargs
        registration_method = kwargs.pop("registration_method", "email")

        # Validate input data (phone is now optional)
        self._validate_registration_data(email, password, phone, first_name, last_name)

        # Build query to check for existing users
        conditions = []
        if email:
            conditions.append(User.email == email.lower())
        if phone:
            conditions.append(User.phone == phone)

        if conditions:
            from sqlalchemy import or_

            existing_user = User.query.filter(or_(*conditions)).first()

            if existing_user:
                if email and existing_user.email == email.lower():
                    raise ConflictError(get_translation("api.auth.email_already_exists"))
                elif phone and existing_user.phone == phone:
                    raise ConflictError(get_translation("error.validation.phone_already_exists"))

        # Create new user
        # Filter out invalid User model fields from kwargs
        valid_user_fields = {
            "date_of_birth",
            "gender",
            "role",
            "status",
            "is_verified",
            "is_premium",
            "preferred_language",
            "preferred_currency",
            "timezone",
            "email_notifications",
            "sms_notifications",
            "push_notifications",
            "user_type",
            "company_name",
            "tax_id",
            "email_verification_token",
            "email_verified_at",
            "telegram_id",
            "registration_source",
            "telegram_username",
            "is_bot_active",
            "bot_state",
            "last_bot_interaction",
        }
        filtered_kwargs = {k: v for k, v in kwargs.items() if k in valid_user_fields}

        # Format phone if provided
        formatted_phone = format_phone_number(phone) if phone else None

        user = User(
            email=email.lower().strip() if email else None,
            phone=formatted_phone,
            first_name=first_name.strip() if first_name else "",
            last_name=last_name.strip() if last_name else "",
            password_hash=self._hash_password(password),
            role=UserRole.CUSTOMER.value,
            status=UserStatus.PENDING_VERIFICATION.value,
            registration_method=registration_method,
            **filtered_kwargs,
        )

        db.session.add(user)
        db.session.commit()

        # Generate tokens
        tokens = self._generate_tokens(user)

        # Send verification emails/SMS
        self._send_verification_notifications(user)

        # Log user session
        self._create_user_session(user.id, tokens["access_token"])

        return user, tokens

    def login_user(self, identifier: str, password: str) -> Tuple[User, Dict[str, str]]:
        """
        Login user with email/phone and password

        Args:
            identifier: Email or phone number
            password: User password

        Returns:
            Tuple of (User object, tokens dict)

        Raises:
            UnauthorizedError: If credentials are invalid
            ValidationError: If account is locked
        """
        # Check for account lockout
        self._check_account_lockout(identifier)

        # Find user by email or phone
        user = User.query.filter((User.email == identifier.lower()) | (User.phone == identifier)).first()

        if not user:
            self._increment_failed_attempts(identifier)
            raise UnauthorizedError(get_translation("api.auth.invalid_credentials"))

        # Check if this is a telegram-only user trying to login with placeholder email
        if self._is_telegram_only_user(user) and identifier.lower() == user.email:
            raise UnauthorizedError(get_translation("api.auth.telegram_account_password_required"))

        if not self._verify_password(password, user.password_hash):
            # Increment failed login attempts
            self._increment_failed_attempts(identifier)
            raise UnauthorizedError(get_translation("api.auth.invalid_credentials"))

        # Check if user is active
        status_value = user.status.value if hasattr(user.status, "value") else user.status
        if status_value in [UserStatus.BANNED.value, UserStatus.INACTIVE.value]:
            raise UnauthorizedError(get_translation("api.auth.account_disabled"))

        # Reset failed login attempts
        self._reset_failed_attempts(identifier)

        # Update last login
        user.last_login = datetime.now(timezone.utc)
        db.session.commit()

        # Generate tokens
        tokens = self._generate_tokens(user)

        # Log user session
        self._create_user_session(user.id, tokens["access_token"])

        return user, tokens

    def refresh_token(self, refresh_token: str) -> Dict[str, str]:
        """
        Refresh access token using refresh token

        Args:
            refresh_token: Valid refresh token

        Returns:
            New tokens dict
        """
        try:
            from flask_jwt_extended import decode_token

            decoded_token = decode_token(refresh_token)
            user_id = decoded_token["sub"]

            user = User.query.get(user_id)
            if not user:
                raise UnauthorizedError(get_translation("api.auth.invalid_user"))

            # Extract status value for comparison
            status_value = user.status.value if hasattr(user.status, "value") else user.status
            if status_value in [UserStatus.BANNED.value, UserStatus.INACTIVE.value]:
                raise UnauthorizedError(get_translation("api.auth.invalid_user"))

            # Generate new tokens
            tokens = self._generate_tokens(user)

            # Update session
            self._update_user_session(user.id, tokens["access_token"])

            return tokens

        except Exception:
            raise UnauthorizedError(get_translation("api.auth.token_invalid"))

    def logout_user(self, access_token: str = None) -> bool:
        """
        Logout user and invalidate token

        Args:
            access_token: Access token to invalidate

        Returns:
            Success status
        """
        try:
            if access_token:
                # Add token to blacklist using TokenService for proper TTL
                from business_app.services.token_service import TokenService

                token_service = TokenService()
                token_service.blacklist_token_by_string(access_token)

                # Get user ID from token
                from flask_jwt_extended import decode_token

                decoded_token = decode_token(access_token)
                user_id = decoded_token["sub"]

                # End user session
                self._end_user_session(user_id, access_token)

            return True
        except Exception:
            return False

    def send_verification_email(self, user_id: int) -> bool:
        """Send email verification"""
        user = User.query.get(user_id)
        if not user:
            return False

        # Generate verification token
        token = self._generate_verification_token(user.id, "email")

        # Send email (implement email service)
        from ..tasks.notification_tasks import send_verification_email_task

        send_verification_email_task.delay(user.id, token)

        return True

    def send_verification_sms(self, user_id: int, phone: str = None, update_phone: bool = True) -> bool:
        """
        Send SMS verification

        Args:
            user_id: User ID
            phone: Phone number to send OTP to (optional, uses user's phone if not provided)
            update_phone: If True, update the user's phone number in DB. Set to False for
                         account linking where the phone belongs to another user.

        Returns:
            Success status
        """
        user = User.query.get(user_id)
        if not user:
            return False

        # Use provided phone or user's phone
        target_phone = phone or user.phone
        if not target_phone:
            logger.error(f"No phone number available for user {user_id}")
            return False

        # Validate phone number
        phone_validator = PhoneValidator(target_phone, "phone")
        phone_validator.validate()
        if not phone_validator.is_valid():
            logger.error(f"Invalid phone number for SMS verification: {target_phone}")
            return False

        # Format phone number
        formatted_phone = format_phone_number(target_phone)

        # Update user's phone if a new phone was provided (and update is allowed)
        if update_phone and phone and phone != user.phone:
            logger.info(f"Updating user {user_id} phone from {user.phone} to {formatted_phone}")
            user.phone = formatted_phone
            db.session.commit()

        # Generate OTP
        otp = generate_otp()

        # Store OTP in Redis
        key = f"sms_verification:{user.id}"
        self.redis_client.setex(key, self.otp_expiry, otp)

        # Send SMS - call directly instead of using Celery to avoid connection issues
        try:
            send_verification_sms_task(user.id, otp, formatted_phone)
            logger.info(f"SMS verification sent to {formatted_phone} for user {user_id}")
            return True
        except Exception:
            logger.exception("Failed to send SMS verification")
            return False

    def verify_email(self, token: str) -> bool:
        """Verify email with token"""
        user_id = self._verify_verification_token(token, "email")
        if not user_id:
            return False

        user = User.query.get(user_id)
        if user:
            # Set email verification timestamp
            user.email_verified_at = datetime.now(timezone.utc)

            # Update general verification status and activate account
            user.is_verified = True
            user.status = UserStatus.ACTIVE.value

            # Clear the verification token from database if it exists
            user.email_verification_token = None

            db.session.commit()

            # Remove token from Redis
            key = f"verification:email:{token}"
            try:
                self.redis_client.delete(key)
            except Exception as e:
                logger.warning(f"Failed to delete verification token from Redis: {e}")

            return True

        return False

    def verify_phone(self, user_id: int, otp: str) -> bool:
        """Verify phone with OTP"""
        key = f"sms_verification:{user_id}"
        stored_otp = self.redis_client.get(key)

        if not stored_otp or stored_otp.decode() != otp:
            return False

        # Remove OTP from Redis
        self.redis_client.delete(key)

        user = User.query.get(user_id)
        if user:
            user.phone_verified_at = datetime.now(timezone.utc)

            # If this was the only pending verification, activate account
            status_value = user.status.value if hasattr(user.status, "value") else user.status
            if status_value == UserStatus.PENDING_VERIFICATION.value and user.email_verified:
                user.status = UserStatus.ACTIVE.value
                user.is_verified = True

            db.session.commit()
            return True

        return False

    def request_password_reset(self, identifier: str) -> bool:
        """
        Request password reset.

        For telegram users with placeholder emails, sends SMS if phone is verified.
        Returns True even if user not found (prevents enumeration).
        """
        user = User.query.filter((User.email == identifier.lower()) | (User.phone == identifier)).first()

        if not user:
            # Return True to prevent email enumeration
            return True

        # Check if user is a telegram-only user with placeholder email
        is_telegram_placeholder = self._is_telegram_only_user(user)

        if is_telegram_placeholder:
            # Telegram users with placeholder emails can't receive email resets
            if user.phone and user.phone_verified_at:
                # Send OTP via SMS for phone-based password reset
                try:
                    self._send_phone_password_reset(user)
                    logger.info(f"Password reset OTP sent via SMS for telegram user {user.id}")
                    return True
                except Exception:
                    logger.exception("Failed to send SMS password reset")
                    return True  # Still return True to prevent enumeration
            else:
                # Telegram user without verified phone - they need to verify phone first
                logger.warning(f"Telegram user {user.id} requested password reset but has no verified phone")
                return True  # Still return True to prevent enumeration

        # Standard email-based password reset
        token = self._generate_verification_token(user.id, "password_reset")

        from ..tasks.notification_tasks import send_password_reset_email_task

        send_password_reset_email_task.delay(user.id, token)

        return True

    def _send_phone_password_reset(self, user: User):
        """Send password reset OTP via SMS for phone-verified users"""
        import hashlib

        phone_hash = hashlib.sha256(user.phone.encode()).hexdigest()[:16]

        # Generate 6-digit OTP
        otp_code = generate_otp(length=6)

        # Store OTP with 10 minute expiry
        otp_key = f"password_reset_otp:{phone_hash}"
        user_key = f"password_reset_user:{phone_hash}"
        self.redis_client.setex(otp_key, 600, otp_code)
        self.redis_client.setex(user_key, 600, user.id)

        # Send SMS
        from ..tasks.notification_tasks import send_password_reset_sms_task

        send_password_reset_sms_task.delay(user.id, otp_code)

    def reset_password(self, token: str, new_password: str) -> bool:
        """Reset password with token and notify user via Telegram if connected"""
        # Validate new password
        validator = PasswordValidator(new_password, "password")
        validator.validate()
        if not validator.is_valid():
            raise ValidationError(
                get_translation("error.validation.invalid_password"), {"password": validator.get_errors()}
            )

        user_id = self._verify_verification_token(token, "password_reset")
        if not user_id:
            return False

        user = User.query.get(user_id)
        if user:
            user.password_hash = self._hash_password(new_password)
            user.password_changed_at = datetime.now(timezone.utc)
            db.session.commit()

            # Invalidate all user sessions
            self._invalidate_all_user_sessions(user.id)

            # Send telegram notification if user has telegram_id
            self._send_password_change_telegram_notification(user, event_type="reset")

            return True

        return False

    def change_password(self, user_id: int, current_password: str, new_password: str) -> bool:
        """Change password for authenticated user and notify via Telegram"""
        user = User.query.get(user_id)
        if not user:
            return False

        # Verify current password
        if not self._verify_password(current_password, user.password_hash):
            raise UnauthorizedError(get_translation("api.auth.current_password_incorrect"))

        # Validate new password
        validator = PasswordValidator(new_password, "password")
        validator.validate()
        if not validator.is_valid():
            raise ValidationError(
                get_translation("error.validation.invalid_password"), {"password": validator.get_errors()}
            )

        # Update password
        user.password_hash = self._hash_password(new_password)
        user.password_changed_at = datetime.now(timezone.utc)
        db.session.commit()

        # Send telegram notification if user has telegram_id
        self._send_password_change_telegram_notification(user, event_type="change")

        return True

    # =============================================================================
    # Phone Registration Methods (Uzbekistan +998 only)
    # =============================================================================

    # OTP expiry time in seconds (3 minutes as per plan)
    PHONE_OTP_EXPIRY = 180
    # Cooldown between OTP resends in seconds
    PHONE_OTP_RESEND_COOLDOWN = 60
    # Max OTP verification attempts
    PHONE_OTP_MAX_ATTEMPTS = 5
    # Lockout duration after max attempts (10 minutes)
    PHONE_OTP_LOCKOUT_DURATION = 600

    def initiate_phone_registration(self, phone: str, language: str = "uz") -> Dict[str, Any]:
        """
        Step 1: Send OTP to phone for registration

        Flow:
        1. Validate phone format (Uzbekistan +998 only)
        2. Check if phone already registered
        3. Check rate limiting (cooldown)
        4. Generate 6-digit OTP
        5. Store OTP in Redis with phone as key (3 min expiry)
        6. Send SMS via Eskiz
        7. Return success with masked phone

        Args:
            phone: Normalized Uzbekistan phone number (+998XXXXXXXXX)
            language: Preferred language for SMS

        Returns:
            Dict with phone_masked, expires_in, resend_available_in

        Raises:
            ValidationError: If validation fails
            ConflictError: If phone already registered
        """
        import hashlib
        from business_app.utils.validators import validate_uzbekistan_phone, mask_phone_number

        # Validate phone (should already be normalized by serializer)
        is_valid, error_msg, normalized_phone = validate_uzbekistan_phone(phone)
        if not is_valid:
            raise ValidationError(error_msg, {"phone": [error_msg]})

        phone = normalized_phone

        # Check if phone already registered
        existing_user = User.query.filter_by(phone=phone).first()
        if existing_user:
            raise ConflictError(
                get_translation("error.validation.phone_already_exists"), error_code="PHONE_ALREADY_REGISTERED"
            )

        # Create phone hash for Redis keys (privacy)
        phone_hash = hashlib.sha256(phone.encode()).hexdigest()[:16]

        # Check cooldown (prevent spam)
        cooldown_key = f"phone_otp_cooldown:{phone_hash}"
        cooldown_ttl = self.redis_client.ttl(cooldown_key)
        if cooldown_ttl > 0:
            raise ValidationError(
                f"Please wait {cooldown_ttl} seconds before requesting a new code.",
                {"phone": [f"Resend available in {cooldown_ttl} seconds"]},
                error_code="RESEND_COOLDOWN",
            )

        # Check if locked out due to too many attempts
        lockout_key = f"phone_otp_lockout:{phone_hash}"
        if self.redis_client.exists(lockout_key):
            lockout_ttl = self.redis_client.ttl(lockout_key)
            raise ValidationError(
                f"Too many attempts. Please try again in {lockout_ttl} seconds.",
                {"phone": ["Account temporarily locked"]},
                error_code="OTP_MAX_ATTEMPTS",
            )

        # Generate 6-digit OTP
        otp_code = generate_otp(length=6)

        # Store OTP in Redis (3 min expiry)
        otp_key = f"phone_reg_otp:{phone_hash}"
        self.redis_client.setex(otp_key, self.PHONE_OTP_EXPIRY, otp_code)

        # Store phone-to-hash mapping for verification step
        phone_mapping_key = f"phone_reg_mapping:{phone_hash}"
        self.redis_client.setex(phone_mapping_key, self.PHONE_OTP_EXPIRY, phone)

        # Store language preference
        lang_key = f"phone_reg_lang:{phone_hash}"
        self.redis_client.setex(lang_key, self.PHONE_OTP_EXPIRY, language)

        # Set cooldown to prevent immediate resend
        self.redis_client.setex(cooldown_key, self.PHONE_OTP_RESEND_COOLDOWN, "1")

        # Send SMS via Celery task
        try:
            from business_app.tasks.notification_tasks import send_registration_otp_task

            send_registration_otp_task.delay(phone, otp_code, language)
            logger.info(f"Registration OTP sent to {mask_phone_number(phone)}")
        except Exception:
            logger.exception("Failed to send registration OTP")
            # Still return success - OTP is stored, SMS might be delayed
            # In production, you might want to handle this differently

        return {
            "phone_masked": mask_phone_number(phone),
            "expires_in": self.PHONE_OTP_EXPIRY,
            "resend_available_in": self.PHONE_OTP_RESEND_COOLDOWN,
        }

    def complete_phone_registration(
        self, phone: str, otp_code: str, first_name: str, last_name: str, password: str, referral_code: str = None
    ) -> Tuple[User, Dict[str, str]]:
        """
        Step 2: Verify OTP and create account

        Flow:
        1. Verify OTP from Redis
        2. Create user with:
           - phone (verified)
           - password
           - first_name, last_name
           - email = None (optional, can add later)
           - status = ACTIVE (already verified)
           - phone_verified_at = now
           - registration_method = 'phone'
           - registration_source = 'web'
        3. Process referral code if provided
        4. Generate JWT tokens
        5. Send welcome SMS
        6. Return user + tokens

        Args:
            phone: Normalized Uzbekistan phone number
            otp_code: 6-digit OTP code
            first_name: User first name
            last_name: User last name (optional)
            password: User password
            referral_code: Optional referral code

        Returns:
            Tuple of (User object, tokens dict)

        Raises:
            ValidationError: If OTP is invalid/expired or validation fails
            ConflictError: If phone already registered
        """
        import hashlib
        from business_app.utils.validators import validate_uzbekistan_phone

        # Validate phone
        is_valid, error_msg, normalized_phone = validate_uzbekistan_phone(phone)
        if not is_valid:
            raise ValidationError(error_msg, {"phone": [error_msg]})

        phone = normalized_phone

        # Create phone hash
        phone_hash = hashlib.sha256(phone.encode()).hexdigest()[:16]

        # Check lockout
        lockout_key = f"phone_otp_lockout:{phone_hash}"
        if self.redis_client.exists(lockout_key):
            lockout_ttl = self.redis_client.ttl(lockout_key)
            raise ValidationError(
                f"Too many attempts. Please try again in {lockout_ttl} seconds.",
                {"otp_code": ["Account temporarily locked"]},
                error_code="OTP_MAX_ATTEMPTS",
            )

        # Verify OTP
        otp_key = f"phone_reg_otp:{phone_hash}"
        stored_otp = self.redis_client.get(otp_key)

        if not stored_otp:
            raise ValidationError(
                "Verification code has expired. Please request a new one.",
                {"otp_code": ["OTP expired"]},
                error_code="OTP_EXPIRED",
            )

        # Track attempts
        attempts_key = f"phone_otp_attempts:{phone_hash}"

        if stored_otp.decode() != otp_code:
            # Increment failed attempts
            attempts = self.redis_client.incr(attempts_key)
            self.redis_client.expire(attempts_key, self.PHONE_OTP_LOCKOUT_DURATION)

            if attempts >= self.PHONE_OTP_MAX_ATTEMPTS:
                # Lock out
                self.redis_client.setex(lockout_key, self.PHONE_OTP_LOCKOUT_DURATION, "1")
                # Clear OTP
                self.redis_client.delete(otp_key)
                raise ValidationError(
                    "Too many incorrect attempts. Please request a new code.",
                    {"otp_code": ["Max attempts exceeded"]},
                    error_code="OTP_MAX_ATTEMPTS",
                )

            remaining = self.PHONE_OTP_MAX_ATTEMPTS - attempts
            raise ValidationError(
                f"Invalid verification code. {remaining} attempts remaining.",
                {"otp_code": ["Invalid OTP"]},
                error_code="INVALID_OTP",
            )

        # OTP is valid - clear it and attempts
        self.redis_client.delete(otp_key, attempts_key)

        # Check if phone already registered (race condition check)
        existing_user = User.query.filter_by(phone=phone).first()
        if existing_user:
            raise ConflictError(
                get_translation("error.validation.phone_already_exists"), error_code="PHONE_ALREADY_REGISTERED"
            )

        # Validate password
        password_validator = PasswordValidator(password, "password")
        password_validator.validate()
        if not password_validator.is_valid():
            raise ValidationError(
                get_translation("error.validation.invalid_password"), {"password": password_validator.get_errors()}
            )

        # Get stored language preference
        lang_key = f"phone_reg_lang:{phone_hash}"
        language = self.redis_client.get(lang_key)
        language = language.decode() if language else "uz"

        # Create user
        user = User(
            phone=phone,
            email=None,  # Email is optional for phone registration
            first_name=first_name.strip(),
            last_name=last_name.strip() if last_name else None,
            password_hash=self._hash_password(password),
            role=UserRole.CUSTOMER.value,
            status=UserStatus.ACTIVE.value,  # Active immediately - phone already verified
            is_verified=True,
            phone_verified_at=datetime.now(timezone.utc),
            registration_source="web",
            registration_method="phone",
            preferred_language=language,
        )

        db.session.add(user)
        db.session.commit()

        # Process referral code if provided
        if referral_code:
            try:
                self._process_referral_code(user.id, referral_code)
            except Exception as e:
                logger.warning(f"Failed to process referral code {referral_code}: {e}")

        # Generate tokens
        tokens = self._generate_tokens(user)

        # Create user session
        self._create_user_session(user.id, tokens["access_token"])

        # Send welcome SMS
        try:
            from business_app.tasks.notification_tasks import send_welcome_sms_task

            send_welcome_sms_task.delay(user.id)
        except Exception as e:
            logger.warning(f"Failed to send welcome SMS: {e}")

        # Clean up Redis keys
        self.redis_client.delete(
            f"phone_reg_mapping:{phone_hash}", f"phone_reg_lang:{phone_hash}", f"phone_otp_cooldown:{phone_hash}"
        )

        logger.info(f"Phone registration completed for user {user.id}")

        return user, tokens

    def resend_phone_registration_otp(self, phone: str) -> Dict[str, Any]:
        """
        Resend OTP for phone registration

        Args:
            phone: Normalized Uzbekistan phone number

        Returns:
            Dict with expires_in, resend_available_in

        Raises:
            ValidationError: If cooldown not expired or phone invalid
        """
        import hashlib
        from business_app.utils.validators import validate_uzbekistan_phone, mask_phone_number

        # Validate phone
        is_valid, error_msg, normalized_phone = validate_uzbekistan_phone(phone)
        if not is_valid:
            raise ValidationError(error_msg, {"phone": [error_msg]})

        phone = normalized_phone
        phone_hash = hashlib.sha256(phone.encode()).hexdigest()[:16]

        # Check if phone already registered
        existing_user = User.query.filter_by(phone=phone).first()
        if existing_user:
            raise ConflictError(
                get_translation("error.validation.phone_already_exists"), error_code="PHONE_ALREADY_REGISTERED"
            )

        # Check cooldown
        cooldown_key = f"phone_otp_cooldown:{phone_hash}"
        cooldown_ttl = self.redis_client.ttl(cooldown_key)
        if cooldown_ttl > 0:
            raise ValidationError(
                f"Please wait {cooldown_ttl} seconds before requesting a new code.",
                {"phone": [f"Resend available in {cooldown_ttl} seconds"]},
                error_code="RESEND_COOLDOWN",
            )

        # Check lockout
        lockout_key = f"phone_otp_lockout:{phone_hash}"
        if self.redis_client.exists(lockout_key):
            lockout_ttl = self.redis_client.ttl(lockout_key)
            raise ValidationError(
                f"Too many attempts. Please try again in {lockout_ttl} seconds.",
                {"phone": ["Account temporarily locked"]},
                error_code="OTP_MAX_ATTEMPTS",
            )

        # Get stored language or default
        lang_key = f"phone_reg_lang:{phone_hash}"
        language = self.redis_client.get(lang_key)
        language = language.decode() if language else "uz"

        # Generate new OTP
        otp_code = generate_otp(length=6)

        # Store OTP
        otp_key = f"phone_reg_otp:{phone_hash}"
        self.redis_client.setex(otp_key, self.PHONE_OTP_EXPIRY, otp_code)

        # Update phone mapping expiry
        phone_mapping_key = f"phone_reg_mapping:{phone_hash}"
        self.redis_client.setex(phone_mapping_key, self.PHONE_OTP_EXPIRY, phone)

        # Update language expiry
        self.redis_client.setex(lang_key, self.PHONE_OTP_EXPIRY, language)

        # Set cooldown
        self.redis_client.setex(cooldown_key, self.PHONE_OTP_RESEND_COOLDOWN, "1")

        # Clear previous attempts on resend
        attempts_key = f"phone_otp_attempts:{phone_hash}"
        self.redis_client.delete(attempts_key)

        # Send SMS
        try:
            from business_app.tasks.notification_tasks import send_registration_otp_task

            send_registration_otp_task.delay(phone, otp_code, language)
            logger.info(f"Registration OTP resent to {mask_phone_number(phone)}")
        except Exception:
            logger.exception("Failed to resend registration OTP")

        return {
            "phone_masked": mask_phone_number(phone),
            "expires_in": self.PHONE_OTP_EXPIRY,
            "resend_available_in": self.PHONE_OTP_RESEND_COOLDOWN,
        }

    def _process_referral_code(self, user_id: int, referral_code: str):
        """Process referral code for new user (placeholder - implement based on your referral system)"""
        # TODO: Implement referral code processing if you have a referral system
        logger.info(f"Processing referral code {referral_code} for user {user_id}")

    # =============================================================================
    # End Phone Registration Methods
    # =============================================================================

    def create_admin_user(
        self, phone: str, email: str, password: str, first_name: str = "Admin", last_name: str = "User"
    ) -> User:
        """Create admin user"""
        # Check if admin already exists
        existing_admin = User.query.filter_by(role=UserRole.ADMIN.value).first()
        if existing_admin:
            raise ConflictError(get_translation("api.auth.admin_already_exists"))

        admin_user = User(
            phone=format_phone_number(phone),
            email=email.lower().strip(),
            first_name=first_name,
            last_name=last_name,
            password_hash=self._hash_password(password),
            user_type=UserType.STAFF.value,
            role=UserRole.ADMIN.value,
            status=UserStatus.ACTIVE.value,
            phone_verified_at=datetime.now(timezone.utc),
            email_verified_at=datetime.now(timezone.utc),
        )

        db.session.add(admin_user)
        db.session.commit()

        return admin_user

    def create_user_by_admin(
        self,
        phone: str,
        first_name: str,
        created_by_admin_id: int,
        last_name: str = None,
        email: str = None,
        notes: str = None,
        company_name: str = None,
        tax_id: str = None,
        user_type: str = None,
        entity_subtype: str = None,
    ) -> User:
        """
        Create a user account via admin panel (for call center operations).

        Users created this way:
        - Are marked with registration_source='admin_created'
        - Have status=ACTIVE (can receive orders)
        - Have is_verified=False (cannot login to cabinet)
        - Have a random password hash (cannot login with password)

        Args:
            phone: Required - User phone number (must be unique)
            first_name: Required - User first name
            created_by_admin_id: ID of admin creating the user
            last_name: Optional - User last name
            email: Optional - User email (must be unique if provided)
            notes: Optional - Admin notes about the user
            company_name: Optional - Company or legal entity name
            tax_id: Optional - Tax identifier
            user_type: Optional - User classification (`individual` or `entity`)

        Returns:
            Created User object

        Raises:
            ValidationError: If validation fails
            ConflictError: If phone or email already exists
        """
        # Validate phone number
        phone_validator = PhoneValidator(phone, "phone")
        phone_validator.validate()
        if not phone_validator.is_valid():
            raise ValidationError(
                get_translation("error.validation.invalid_phone"), {"phone": phone_validator.get_errors()}
            )

        formatted_phone = format_phone_number(phone)

        # Check if phone already exists
        existing_by_phone = User.query.filter_by(phone=formatted_phone).first()
        if existing_by_phone:
            raise ConflictError(get_translation("error.validation.phone_already_exists"))

        # Validate and check email if provided
        formatted_email = None
        if email:
            email_validator = EmailValidator(email, "email")
            email_validator.validate()
            if not email_validator.is_valid():
                raise ValidationError(
                    get_translation("error.validation.invalid_email"), {"email": email_validator.get_errors()}
                )

            formatted_email = email.lower().strip()
            existing_by_email = User.query.filter_by(email=formatted_email).first()
            if existing_by_email:
                raise ConflictError(get_translation("api.auth.email_already_exists"))

        # Validate first name
        if not first_name or not first_name.strip():
            raise ValidationError(
                get_translation("error.validation.failed"), {"first_name": ["First name is required"]}
            )

        normalized_company_name = company_name.strip() if company_name else None
        normalized_user_type = infer_non_staff_user_type(user_type)
        normalized_tax_id = tax_id.strip().upper() if tax_id else None

        if user_type:
            is_valid, message = SecurityValidator.validate_user_type(user_type)
            if not is_valid:
                raise ValidationError(get_translation("error.validation.failed"), {"user_type": [message]})
            if user_type.strip().lower() == UserType.STAFF.value:
                raise ValidationError(
                    get_translation("error.validation.failed"),
                    {"user_type": ["Staff users must be managed through staff administration flows"]},
                )

        if normalized_tax_id:
            is_valid, message = SecurityValidator.validate_tax_id(normalized_tax_id)
            if not is_valid:
                raise ValidationError(get_translation("error.validation.failed"), {"tax_id": [message]})

        if normalized_user_type == UserType.ENTITY.value and not normalized_company_name:
            raise ValidationError(
                get_translation("error.validation.failed"),
                {"company_name": ["Company name is required for entity users"]},
            )

        # Entity subtype: required when user_type is entity (so admin must
        # explicitly pick workplace vs grocery_store). Disallowed for non-entities.
        normalized_entity_subtype = None
        if normalized_user_type == UserType.ENTITY.value:
            normalized_entity_subtype = normalize_entity_subtype(entity_subtype)
            if normalized_entity_subtype is None:
                raise ValidationError(
                    get_translation("error.validation.failed"),
                    {"entity_subtype": ["Entity subtype must be 'workplace' or 'grocery_store' for entity users"]},
                )
        else:
            if entity_subtype is not None:
                raise ValidationError(
                    get_translation("error.validation.failed"),
                    {"entity_subtype": ["Entity subtype may only be set when user_type is 'entity'"]},
                )

        if normalized_user_type != UserType.ENTITY.value:
            normalized_company_name = None
            normalized_tax_id = None

        # Generate a secure random password that the user will never know
        # This prevents login via password - admin-created users can only be managed via admin panel
        random_password = secrets.token_urlsafe(32)
        secure_password_hash = self._hash_password(random_password)

        # Create the user
        user = User(
            phone=formatted_phone,
            email=formatted_email,
            first_name=first_name.strip(),
            last_name=last_name.strip() if last_name else None,
            password_hash=secure_password_hash,
            user_type=normalized_user_type,
            role=UserRole.CUSTOMER.value,
            status=UserStatus.ACTIVE.value,  # Active so orders can be placed
            is_verified=False,  # Not verified - cannot access cabinet pages
            registration_source="admin_created",
            preferred_language="uz",  # Default language for Uzbekistan
            company_name=normalized_company_name,
            tax_id=normalized_tax_id,
            entity_subtype=(EntitySubtype(normalized_entity_subtype) if normalized_entity_subtype else None),
        )

        db.session.add(user)
        db.session.commit()

        # Log creation with notes if provided
        log_message = (
            f"User created by admin: user_id={user.id}, phone={formatted_phone}, "
            f"created_by_admin_id={created_by_admin_id}"
        )
        if notes:
            log_message += f", notes={notes}"
        logger.info(log_message)

        return user

    def update_user_by_admin(
        self,
        user_id: int,
        *,
        first_name: str,
        updated_by_admin_id: int,
        last_name: str = None,
        phone: str = None,
        email: str = None,
        company_name: str = None,
        tax_id: str = None,
        user_type: str = None,
        entity_subtype: Any = ...,
    ) -> User:
        """Update a user from the admin panel using the simplified two-type business model."""
        user = User.query.get(user_id)
        if not user:
            raise NotFoundError("User not found")

        if not first_name or not first_name.strip():
            raise ValidationError(
                get_translation("error.validation.failed"), {"first_name": ["First name is required"]}
            )

        formatted_phone = None
        if phone:
            phone_validator = PhoneValidator(phone, "phone")
            phone_validator.validate()
            if not phone_validator.is_valid():
                raise ValidationError(
                    get_translation("error.validation.invalid_phone"), {"phone": phone_validator.get_errors()}
                )
            formatted_phone = format_phone_number(phone)
            existing_by_phone = User.query.filter(User.phone == formatted_phone, User.id != user_id).first()
            if existing_by_phone:
                raise ConflictError(get_translation("error.validation.phone_already_exists"))

        formatted_email = None
        if email:
            email_validator = EmailValidator(email, "email")
            email_validator.validate()
            if not email_validator.is_valid():
                raise ValidationError(
                    get_translation("error.validation.invalid_email"), {"email": email_validator.get_errors()}
                )
            formatted_email = email.lower().strip()
            existing_by_email = User.query.filter(User.email == formatted_email, User.id != user_id).first()
            if existing_by_email:
                raise ConflictError(get_translation("api.auth.email_already_exists"))

        current_user_type = user.normalized_user_type
        raw_user_type = user_type if user_type is not None else getattr(user.user_type, "value", user.user_type)
        normalized_user_type = infer_non_staff_user_type(raw_user_type)
        if user_type:
            is_valid, message = SecurityValidator.validate_user_type(user_type)
            if not is_valid:
                raise ValidationError(get_translation("error.validation.failed"), {"user_type": [message]})
            requested_user_type = user_type.strip().lower()
            if current_user_type == UserType.STAFF.value and requested_user_type != UserType.STAFF.value:
                raise ValidationError(
                    get_translation("error.validation.failed"),
                    {"user_type": ["Staff type changes must be managed through staff administration flows"]},
                )
            if current_user_type != UserType.STAFF.value and requested_user_type == UserType.STAFF.value:
                raise ValidationError(
                    get_translation("error.validation.failed"),
                    {"user_type": ["Staff users must be managed through staff administration flows"]},
                )

        normalized_company_name = user.company_name if company_name is None else (company_name.strip() or None)
        normalized_tax_id = user.tax_id if tax_id is None else (tax_id.strip().upper() or None)

        if normalized_tax_id:
            is_valid, message = SecurityValidator.validate_tax_id(normalized_tax_id)
            if not is_valid:
                raise ValidationError(get_translation("error.validation.failed"), {"tax_id": [message]})

        if normalized_user_type == UserType.ENTITY.value and not normalized_company_name:
            raise ValidationError(
                get_translation("error.validation.failed"),
                {"company_name": ["Company name is required for entity users"]},
            )

        # Entity subtype handling on update.
        # `entity_subtype` defaults to the sentinel `...` -- meaning "not provided,
        # leave unchanged". Passing None explicitly clears it. Switching subtype
        # while the user has any non-terminated contract is blocked.
        current_subtype_value = (
            user.entity_subtype.value
            if user.entity_subtype is not None and hasattr(user.entity_subtype, "value")
            else user.entity_subtype
        )
        new_subtype_value: Any = current_subtype_value
        if entity_subtype is not ...:
            if entity_subtype is None:
                new_subtype_value = None
            else:
                new_subtype_value = normalize_entity_subtype(entity_subtype)
                if new_subtype_value is None:
                    raise ValidationError(
                        get_translation("error.validation.failed"),
                        {"entity_subtype": ["Entity subtype must be 'workplace' or 'grocery_store'"]},
                    )

        if normalized_user_type != UserType.ENTITY.value:
            new_subtype_value = None

        # Block actual subtype switches (workplace <-> grocery_store) when the
        # user still has non-terminated contracts: the contract's tracking_mode
        # is locked at create-time and won't survive the switch. The first-time
        # assignment from NULL is always allowed -- legacy entity users default
        # to NULL after the migration and admins must be able to assign one.
        if (
            current_subtype_value is not None
            and new_subtype_value is not None
            and new_subtype_value != current_subtype_value
        ):
            from business_app.models.corporate import CorporateContract, CorporateContractStatus

            non_terminated_contracts = CorporateContract.query.filter(
                CorporateContract.user_id == user_id,
                CorporateContract.status != CorporateContractStatus.TERMINATED,
            ).count()
            if non_terminated_contracts > 0:
                raise ValidationError(
                    get_translation("error.validation.failed"),
                    {
                        "entity_subtype": [
                            "Terminate active or non-terminated contracts before switching entity subtype",
                        ]
                    },
                )

        if normalized_user_type != UserType.ENTITY.value:
            normalized_company_name = None
            normalized_tax_id = None

        user.first_name = first_name.strip()
        user.last_name = last_name.strip() if last_name else None
        user.phone = formatted_phone
        user.email = formatted_email
        if current_user_type != UserType.STAFF.value:
            user.user_type = normalized_user_type
        user.company_name = normalized_company_name
        user.tax_id = normalized_tax_id
        user.entity_subtype = EntitySubtype(new_subtype_value) if new_subtype_value else None

        db.session.add(user)
        db.session.commit()

        logger.info(
            "User updated by admin: user_id=%s, updated_by_admin_id=%s, user_type=%s",
            user.id,
            updated_by_admin_id,
            getattr(user.user_type, "value", user.user_type),
        )

        return user

    def get_user_permissions(self, user_id: int) -> Dict[str, bool]:
        """Get user permissions based on role"""
        user = User.query.get(user_id)
        if not user:
            return {}

        permissions = {
            "can_view_orders": True,
            "can_place_orders": True,
            "can_view_profile": True,
            "can_edit_profile": True,
            "can_view_analytics": False,
            "can_manage_users": False,
            "can_manage_products": False,
            "can_manage_orders": False,
            "can_manage_delivery": False,
            "can_view_admin_panel": False,
            "can_manage_settings": False,
            "can_manage_translations": False,
        }
        # Get role value for comparison (handle both enum and string)
        role_value = user.role.value if hasattr(user.role, "value") else user.role

        logger.info(
            f"USER.ROLE: {user.role}, ROLE VALUE: {role_value}, ADMIN ROLES: {[UserRole.ADMIN.value, UserRole.MANAGER.value]}"  # noqa: E501
        )

        if role_value in [UserRole.ADMIN.value, UserRole.MANAGER.value]:
            permissions.update(
                {
                    "can_view_analytics": True,
                    "can_manage_users": True,
                    "can_manage_products": True,
                    "can_manage_orders": True,
                    "can_manage_delivery": True,
                    "can_view_admin_panel": True,
                    "can_manage_settings": role_value == UserRole.ADMIN.value,
                    "can_manage_translations": role_value == UserRole.ADMIN.value,
                }
            )
        elif role_value == UserRole.OPERATOR.value:
            permissions.update(
                {
                    "can_view_analytics": True,
                    "can_manage_orders": True,
                    "can_view_admin_panel": True,
                }
            )
        elif role_value == UserRole.DELIVERY_DRIVER.value:
            permissions.update(
                {
                    "can_manage_delivery": True,
                    "can_view_admin_panel": True,
                }
            )

        return permissions

    def get_user_profile_data(self, user_id: int) -> Dict[str, Any]:
        """Get profile payload for a user."""
        user = User.query.get(user_id)
        if not user:
            raise NotFoundError(get_translation("error.not_found"))

        return {
            "id": user.id,
            "email": user.email,
            "phone": user.phone,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "date_of_birth": user.date_of_birth.isoformat() if user.date_of_birth else None,
            "gender": user.gender.value if hasattr(user.gender, "value") else user.gender,
            "role": user.role.value if hasattr(user.role, "value") else user.role,
            "status": user.status.value if hasattr(user.status, "value") else user.status,
            "email_verified": user.email_verified_at is not None,
            "phone_verified": user.phone_verified_at is not None,
            "created_at": user.created_at.isoformat(),
            "last_login": user.last_login.isoformat() if user.last_login else None,
            "preferred_language": getattr(user, "preferred_language", "en"),
            "permissions": self.get_user_permissions(user_id),
        }

    def get_user_addresses(self, user_id: int) -> List[UserAddress]:
        """Get all addresses for a user."""
        return UserAddress.query.filter_by(user_id=user_id).all()

    def add_user_address(self, user_id: int, data: Dict[str, Any]) -> UserAddress:
        """Create address for a user."""
        if data.get("is_default", False):
            UserAddress.query.filter_by(user_id=user_id, is_default=True).update({"is_default": False})

        address = UserAddress(
            user_id=user_id,
            title=data["title"],
            full_address=data.get("full_address", ""),
            street_address=data.get("street_address"),
            city=data.get("city", "Tashkent"),
            district=data.get("district"),
            postal_code=data.get("postal_code"),
            country=data.get("country", "Uzbekistan"),
            latitude=data.get("latitude"),
            longitude=data.get("longitude"),
            is_default=data.get("is_default", False),
            is_business=data.get("is_business", False),
            delivery_instructions=data.get("delivery_instructions", data.get("delivery_notes")),
            landmark=data.get("landmark"),
            floor_number=data.get("floor_number"),
            apartment_number=data.get("apartment_number"),
        )
        db.session.add(address)
        db.session.commit()
        return address

    def update_user_address(self, user_id: int, address_id: int, data: Dict[str, Any]) -> UserAddress:
        """Update address fields for a user-owned address."""
        address = UserAddress.query.filter_by(id=address_id, user_id=user_id).first()
        if not address:
            raise NotFoundError(get_translation("api.auth.address_not_found"))

        updatable_fields = (
            "title",
            "full_address",
            "street_address",
            "city",
            "district",
            "postal_code",
            "latitude",
            "longitude",
            "delivery_instructions",
            "landmark",
            "floor_number",
            "apartment_number",
        )
        for field in updatable_fields:
            if field in data:
                setattr(address, field, data[field])

        db.session.commit()
        return address

    def delete_user_address(self, user_id: int, address_id: int) -> None:
        """Delete a user-owned address."""
        address = UserAddress.query.filter_by(id=address_id, user_id=user_id).first()
        if not address:
            raise NotFoundError(get_translation("api.auth.address_not_found"))

        from business_app.models.subscription import Subscription

        has_subscription_reference = (
            Subscription.query.filter_by(
                user_id=user_id,
                delivery_address_id=address_id,
            ).first()
            is not None
        )
        if has_subscription_reference:
            message = get_translation("api.addresses.error.in_use_by_subscription")
            if message == "api.addresses.error.in_use_by_subscription":
                message = "Cannot delete an address used by subscriptions"
            raise ValidationError(message)

        if address.is_default:
            other_addresses_count = UserAddress.query.filter(
                UserAddress.user_id == user_id,
                UserAddress.id != address_id,
            ).count()
            if other_addresses_count > 0:
                raise ValidationError(get_translation("error.forbidden"))

        try:
            db.session.delete(address)
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            raise ValidationError("Cannot delete an address referenced by existing records")

    def set_default_user_address(self, user_id: int, address_id: int) -> UserAddress:
        """Set a specific user-owned address as default."""
        address = UserAddress.query.filter_by(id=address_id, user_id=user_id).first()
        if not address:
            raise NotFoundError(get_translation("api.auth.address_not_found"))

        UserAddress.query.filter_by(user_id=user_id, is_default=True).update({"is_default": False})
        address.is_default = True
        db.session.commit()
        return address

    def update_user_profile_data(self, user_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        """Update allowed profile fields and return standardized payload."""
        user = User.query.get(user_id)
        if not user:
            raise NotFoundError(get_translation("user_not_found"))

        phone_update_attempted = False

        if "first_name" in data:
            user.first_name = data["first_name"]
        if "last_name" in data:
            user.last_name = data["last_name"]
        if "phone" in data:
            phone_update_attempted = True
        if "date_of_birth" in data:
            date_value = data["date_of_birth"]
            if date_value:
                try:
                    user.date_of_birth = datetime.fromisoformat(date_value)
                except (ValueError, TypeError):
                    pass
            else:
                user.date_of_birth = None
        if "gender" in data:
            user.gender = data["gender"]
        if "preferred_language" in data:
            user.preferred_language = data["preferred_language"]

        user.updated_at = datetime.now(timezone.utc)
        db.session.commit()

        user_data = {
            "id": user.id,
            "email": user.email,
            "phone": user.phone,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "full_name": user.full_name,
            "date_of_birth": user.date_of_birth.isoformat() if user.date_of_birth else None,
            "gender": user.gender.value if hasattr(user.gender, "value") else user.gender,
            "role": user.role.value if hasattr(user.role, "value") else user.role,
            "status": user.status.value if hasattr(user.status, "value") else user.status,
            "preferred_language": user.preferred_language,
            "updated_at": user.updated_at.isoformat(),
        }

        return {
            "user": user_data,
            "phone_update_attempted": phone_update_attempted,
        }

    def link_telegram_account(self, current_user_id: int, data: Dict[str, Any]) -> User:
        """Link telegram account to an existing authenticated web user."""
        telegram_id = str(data["telegram_id"])

        existing_user = User.query.filter(
            User.telegram_id == telegram_id,
            User.id != current_user_id,
        ).first()
        if existing_user:
            raise ConflictError(get_translation("api.auth.email_already_exists"))

        user = User.query.get(current_user_id)
        if not user:
            raise NotFoundError(get_translation("error.not_found"))

        user.telegram_id = telegram_id

        if data.get("first_name") and (not user.first_name or user.first_name == "Telegram User"):
            user.first_name = data.get("first_name")
        if data.get("last_name") and not user.last_name:
            user.last_name = data.get("last_name")

        db.session.commit()
        return user

    def link_web_account(self, telegram_id: str, email: str, password: str) -> Dict[str, Any]:
        """Link a telegram user with an existing web account and return fresh tokens."""
        telegram_id_str = str(telegram_id)
        normalized_email = email.lower().strip()

        telegram_user = User.query.filter_by(telegram_id=telegram_id_str).first()
        if not telegram_user:
            raise NotFoundError(get_translation("error.not_found"))

        web_user = User.query.filter_by(email=normalized_email).first()
        if not web_user or not self._verify_password(password, web_user.password_hash):
            raise UnauthorizedError(get_translation("api.auth.invalid_credentials"))

        expected_placeholders = {
            f"telegram_{telegram_id_str}@bluestream.local",
            f"telegram_{telegram_id_str}@bot.internal",
        }
        if web_user.telegram_id or telegram_user.email not in expected_placeholders:
            raise ConflictError(get_translation("api.auth.email_already_exists"))

        from business_app.services.cross_platform_sync_service import cross_platform_sync_service

        result = cross_platform_sync_service.auto_link_accounts(
            primary_user=web_user,
            secondary_user=telegram_user,
            link_type="merge",
        )
        if not result.get("success"):
            raise ValidationError(result.get("error", get_translation("api.auth.accounts_linking_failed")))

        tokens = self._generate_tokens(web_user)
        return {
            "user": web_user,
            "tokens": tokens,
        }

    def check_phone_availability_for_telegram(self, phone: str, telegram_id: str) -> Dict[str, Any]:
        """Check whether a phone can be linked from a telegram flow."""
        from business_app.utils.validators import normalize_phone_number

        normalized_phone = normalize_phone_number(phone)
        telegram_id_str = str(telegram_id)
        existing_user = User.query.filter_by(phone=normalized_phone).first()

        if not existing_user:
            return {
                "available": True,
                "can_link": False,
                "existing_user_masked": None,
            }

        if existing_user.telegram_id and str(existing_user.telegram_id) == telegram_id_str:
            return {
                "available": True,
                "can_link": False,
                "existing_user_masked": None,
            }

        user_status = (
            existing_user.status.value if isinstance(existing_user.status, UserStatus) else existing_user.status
        )
        can_link = (
            existing_user.telegram_id is None
            and user_status == UserStatus.ACTIVE.value
            and existing_user.registration_source in ["web", "email", "phone", "admin_created"]
        )

        masked_name = existing_user.first_name[:1] + "***" if existing_user.first_name else "***"
        if existing_user.last_name:
            masked_name += " " + existing_user.last_name[:1] + "***"

        masked_email = None
        if existing_user.email and not existing_user.email.endswith("@bluestream.local"):
            parts = existing_user.email.split("@")
            if len(parts) == 2:
                masked_email = parts[0][:2] + "***@" + parts[1]

        return {
            "available": False,
            "can_link": can_link,
            "existing_user_masked": {
                "name": masked_name,
                "email": masked_email,
                "registration_source": existing_user.registration_source,
            },
        }

    def send_phone_link_otp(self, phone: str, telegram_id: str) -> Dict[str, Any]:
        """Store linking intent and send OTP for phone-account linking."""
        from business_app.utils.validators import normalize_phone_number
        import json

        normalized_phone = normalize_phone_number(phone)
        telegram_user = User.query.filter_by(telegram_id=str(telegram_id)).first()
        if not telegram_user:
            raise NotFoundError(get_translation("api.auth.telegram_user_not_found"))

        web_user = User.query.filter_by(phone=normalized_phone).first()
        if not web_user:
            raise NotFoundError(get_translation("api.auth.phone_account_not_found"))

        if web_user.telegram_id:
            raise ConflictError(get_translation("api.auth.phone_already_linked_to_telegram"))

        web_user_status = web_user.status.value if isinstance(web_user.status, UserStatus) else web_user.status
        if web_user_status != UserStatus.ACTIVE.value:
            raise ConflictError(get_translation("api.auth.phone_account_inactive"))

        link_key = f"phone_link:{telegram_id}"
        link_data = {
            "phone": normalized_phone,
            "web_user_id": web_user.id,
            "telegram_user_id": telegram_user.id,
        }
        self.redis_client.setex(link_key, 600, json.dumps(link_data))

        success = self.send_verification_sms(telegram_user.id, normalized_phone, update_phone=False)
        if not success:
            raise ValidationError(get_translation("api.auth.otp_send_failed"))

        return {
            "phone_masked": (
                normalized_phone[:7] + "****" + normalized_phone[-2:] if len(normalized_phone) > 9 else "***"
            )
        }

    def verify_phone_link_and_merge_accounts(self, telegram_id: str, otp: str) -> Dict[str, Any]:
        """Verify OTP for linking and merge telegram account into web account."""
        import json
        from business_app.services.cross_platform_sync_service import cross_platform_sync_service

        link_key = f"phone_link:{telegram_id}"
        link_data_raw = self.redis_client.get(link_key)
        if not link_data_raw:
            raise NotFoundError(get_translation("api.auth.pending_link_not_found"))

        link_data = json.loads(link_data_raw.decode("utf-8"))
        web_user_id = link_data["web_user_id"]
        telegram_user_id = link_data["telegram_user_id"]

        telegram_user = User.query.get(telegram_user_id)
        if not telegram_user:
            raise NotFoundError(get_translation("api.auth.telegram_user_not_found"))

        success = self.verify_phone(telegram_user_id, otp)
        if not success:
            raise ValidationError(get_translation("api.auth.link_otp_invalid"))

        web_user = User.query.get(web_user_id)
        if not web_user:
            raise NotFoundError(get_translation("api.auth.web_account_not_found"))

        now_utc = datetime.now(timezone.utc)
        telegram_user.is_verified = True
        web_user.is_verified = True
        web_user.phone_verified_at = now_utc
        db.session.commit()

        result = cross_platform_sync_service.auto_link_accounts(
            primary_user=web_user,
            secondary_user=telegram_user,
            link_type="merge",
        )
        if not result.get("success"):
            raise ValidationError(result.get("error", get_translation("api.auth.accounts_linking_failed")))

        self.redis_client.delete(link_key)
        return {
            "user": web_user,
            "tokens": self._generate_tokens(web_user),
            "linked": True,
        }

    # Private methods
    def _validate_registration_data(
        self, email: Optional[str], password: str, phone: Optional[str], first_name: str, last_name: Optional[str]
    ):
        """
        Validate registration data.

        At least one of email or phone must be provided.
        Last name is optional.
        """
        errors = {}

        # At least email or phone must be provided
        if not email and not phone:
            errors["email"] = ["Either email or phone number is required"]
            errors["phone"] = ["Either email or phone number is required"]

        # Validate email if provided
        if email:
            email_validator = EmailValidator(email, "email")
            email_validator.validate()
            if not email_validator.is_valid():
                errors["email"] = email_validator.get_errors()

        # Validate password (always required)
        password_validator = PasswordValidator(password, "password")
        password_validator.validate()
        if not password_validator.is_valid():
            errors["password"] = password_validator.get_errors()

        # Validate phone if provided
        if phone:
            phone_validator = PhoneValidator(phone, "phone")
            phone_validator.validate()
            if not phone_validator.is_valid():
                errors["phone"] = phone_validator.get_errors()

        # Validate first name (required)
        if not first_name or not first_name.strip():
            errors["first_name"] = ["First name is required"]

        # Last name is optional - no validation needed

        if errors:
            raise ValidationError(get_translation("error.validation.failed"), errors)

    def _hash_password(self, password: str) -> str:
        """Hash password with configured bcrypt rounds"""
        return hash_password(password)

    def _verify_password(self, password: str, password_hash: str) -> bool:
        """Verify password and optionally rehash if needed"""
        # Verify password
        is_valid = verify_password(password, password_hash)

        # If verification successful and rehashing is enabled, check if hash needs update
        if is_valid and current_app.config.get("PASSWORD_REHASH_ON_LOGIN", True):
            if needs_password_rehash(password_hash):
                try:
                    # Find user and update password hash with new rounds
                    user = User.query.filter(User.password_hash == password_hash).first()
                    if user:
                        new_hash = self._hash_password(password)
                        user.password_hash = new_hash
                        db.session.commit()
                        logger.info(f"Password hash updated for user {user.id} with new bcrypt rounds")
                except Exception:
                    logger.exception("Failed to update password hash")
                    # Don't fail login due to rehashing error

        return is_valid

    def _generate_tokens(self, user: User) -> Dict[str, str]:
        """Generate JWT tokens using TokenService"""
        from business_app.services.token_service import TokenService

        token_service = TokenService()

        # Use the new TokenService for comprehensive token management
        return token_service.generate_tokens(user)

    def _generate_verification_token(self, user_id: int, token_type: str) -> str:
        """Generate verification token"""
        token = generate_random_string(32)
        key = f"verification:{token_type}:{token}"

        # Store token with 24 hour expiry
        self.redis_client.setex(key, 86400, user_id)

        return token

    def _verify_verification_token(self, token: str, token_type: str) -> Optional[int]:
        """Verify verification token"""
        key = f"verification:{token_type}:{token}"
        user_id = self.redis_client.get(key)

        if user_id:
            # Delete token after use
            self.redis_client.delete(key)
            return int(user_id)

        return None

    def _send_verification_notifications(self, user: User):
        """Send verification notifications"""
        try:
            # Send email verification
            if user.email:
                self.send_verification_email(user.id)
        except Exception as e:
            logger.warning(f"Failed to send email verification for user {user.id}: {e}")

        try:
            # Send SMS verification
            if user.phone:
                self.send_verification_sms(user.id)
        except Exception as e:
            logger.warning(f"Failed to send SMS verification for user {user.id}: {e}")

    def _send_password_change_telegram_notification(self, user: User, event_type: str = "change"):
        """
        Send telegram notification when password is changed or reset.

        Args:
            user: User whose password was changed
            event_type: 'change' or 'reset'
        """
        if not user.telegram_id:
            return

        try:
            from ..tasks.notification_tasks import send_telegram_security_alert_task

            event_messages = {"change": "Your password was changed", "reset": "Your password was reset"}

            message = event_messages.get(event_type, "Your password was updated")

            send_telegram_security_alert_task.delay(user_id=user.id, alert_type="password_change", message=message)

            logger.info(f"Telegram password {event_type} notification queued for user {user.id}")

        except Exception as e:
            logger.warning(f"Failed to send telegram password notification for user {user.id}: {e}")

    def _send_account_locked_notification(self, user: User, lockout_until: datetime):
        """
        Send notification when account is locked due to failed login attempts.

        Alerts user via SMS and Telegram about potential unauthorized access.

        Args:
            user: User whose account was locked
            lockout_until: When the lockout expires
        """
        try:
            from ..tasks.notification_tasks import send_account_locked_notification_task

            # Calculate lockout duration in minutes
            lockout_minutes = self.lockout_duration // 60

            send_account_locked_notification_task.delay(
                user_id=user.id, lockout_until=lockout_until.isoformat(), lockout_minutes=lockout_minutes
            )

            logger.info(f"Account locked notification queued for user {user.id}")

        except Exception as e:
            logger.warning(f"Failed to queue account locked notification for user {user.id}: {e}")

    def _create_user_session(self, user_id: int, access_token: str):
        """Create user session record"""
        session = UserSession(
            user_id=user_id,
            session_token=self._get_token_jti(access_token),
            ip_address=request.remote_addr if request else None,
            user_agent=request.headers.get("User-Agent") if request else None,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
        )

        db.session.add(session)
        db.session.commit()

    def _update_user_session(self, user_id: int, access_token: str):
        """Update user session with new token"""
        # End current sessions
        UserSession.query.filter_by(user_id=user_id, is_active=True).update(
            {"is_active": False, "ended_at": datetime.now(timezone.utc)}
        )

        # Create new session
        self._create_user_session(user_id, access_token)

    def _end_user_session(self, user_id: int, access_token: str):
        """End user session"""
        jti = self._get_token_jti(access_token)
        session = UserSession.query.filter_by(user_id=user_id, session_token=jti, is_active=True).first()

        if session:
            session.is_active = False
            session.ended_at = datetime.now(timezone.utc)
            db.session.commit()

    def _invalidate_all_user_sessions(self, user_id: int):
        """Invalidate all user sessions"""
        UserSession.query.filter_by(user_id=user_id, is_active=True).update(
            {"is_active": False, "ended_at": datetime.now(timezone.utc)}
        )
        db.session.commit()

    def _get_token_jti(self, token: str) -> str:
        """Get JWT ID from token"""
        try:
            from flask_jwt_extended import decode_token

            decoded = decode_token(token)
            return decoded.get("jti", "")
        except:  # noqa: E722
            return ""

    # Removed _blacklist_token method - now using TokenService.blacklist_token_by_string()

    def _check_account_lockout(self, identifier: str):
        """Check if account is locked due to failed attempts (checks both Redis and DB)"""
        # Check Redis first (primary lockout mechanism)
        key = f"login_attempts:{identifier}"
        attempts = self.redis_client.get(key)

        if attempts and int(attempts) >= self.max_login_attempts:
            lockout_key = f"account_lockout:{identifier}"
            if self.redis_client.exists(lockout_key):
                raise ValidationError(get_translation("api.auth.account_locked"))

        # Also check database as fallback (in case Redis was restarted)
        try:
            user = User.query.filter((User.email == identifier) | (User.phone == identifier)).first()

            if user and user.account_locked_until:
                if user.account_locked_until > datetime.now(timezone.utc):
                    raise ValidationError(get_translation("api.auth.account_locked"))
                else:
                    # Lockout has expired, clear it
                    user.account_locked_until = None
                    user.failed_login_attempts = 0
                    db.session.commit()
        except ValidationError:
            raise
        except Exception:
            logger.exception("Error checking DB lockout")

    def _increment_failed_attempts(self, identifier: str):
        """Increment failed login attempts and lock account in DB if max reached"""
        key = f"login_attempts:{identifier}"
        attempts = self.redis_client.incr(key)
        self.redis_client.expire(key, self.lockout_duration)

        if attempts >= self.max_login_attempts:
            # Set Redis lockout key
            lockout_key = f"account_lockout:{identifier}"
            self.redis_client.setex(lockout_key, self.lockout_duration, "1")

            # Also update account_locked_until in database for persistence
            # This allows admin to see/unlock accounts and survives Redis restarts
            try:
                lockout_until = datetime.now(timezone.utc) + timedelta(seconds=self.lockout_duration)

                # Look up user by email or phone
                user = User.query.filter((User.email == identifier) | (User.phone == identifier)).first()

                if user:
                    user.account_locked_until = lockout_until
                    user.failed_login_attempts = attempts
                    db.session.commit()
                    logger.warning(f"Account locked for user {user.id} until {lockout_until}")

                    # Send notification about account lockout
                    self._send_account_locked_notification(user, lockout_until)
            except Exception:
                logger.exception("Failed to update account_locked_until in DB")
                # Don't fail the overall operation - Redis lockout still works

    def _reset_failed_attempts(self, identifier: str):
        """Reset failed login attempts and clear account lockout"""
        key = f"login_attempts:{identifier}"
        lockout_key = f"account_lockout:{identifier}"

        self.redis_client.delete(key, lockout_key)

        # Also clear account_locked_until in database
        try:
            user = User.query.filter((User.email == identifier) | (User.phone == identifier)).first()

            if user and (user.account_locked_until or user.failed_login_attempts > 0):
                user.account_locked_until = None
                user.failed_login_attempts = 0
                db.session.commit()
                logger.info(f"Account lockout cleared for user {user.id}")
        except Exception:
            logger.exception("Failed to clear account_locked_until in DB")

    def unlock_user_account(self, user_id: int, admin_user_id: int) -> bool:
        """
        Unlock a locked user account (admin action).

        Clears both Redis lockout keys and DB account_locked_until field.
        Logs the action to audit trail.

        Args:
            user_id: ID of user to unlock
            admin_user_id: ID of admin performing the unlock

        Returns:
            True if unlock was successful

        Raises:
            NotFoundError: If user doesn't exist
        """
        user = User.query.get(user_id)
        if not user:
            from business_app.utils.exceptions import NotFoundError

            raise NotFoundError(f"User {user_id} not found")

        # Determine identifier for Redis keys (email or phone)
        identifier = user.email or user.phone

        if identifier:
            # Clear Redis lockout keys
            key = f"login_attempts:{identifier}"
            lockout_key = f"account_lockout:{identifier}"
            self.redis_client.delete(key, lockout_key)

        # Clear DB fields
        was_locked = user.account_locked_until is not None
        user.account_locked_until = None
        user.failed_login_attempts = 0
        db.session.commit()

        # Log to audit trail
        try:
            from business_app.models.audit import AuditLog, AuditEventType

            audit_log = AuditLog(
                user_id=admin_user_id,
                event_type=AuditEventType.ADMIN_ACTION.value,
                event_details={"action": "unlock_account", "target_user_id": user_id, "was_locked": was_locked},
                ip_address=request.remote_addr if request else None,
            )
            db.session.add(audit_log)
            db.session.commit()
        except Exception as e:
            logger.warning(f"Failed to create audit log for account unlock: {e}")

        logger.info(f"Admin {admin_user_id} unlocked account for user {user_id}")
        return True

    def _is_telegram_only_user(self, user: User) -> bool:
        """
        Check if user is telegram-only (hasn't set web password yet)

        Args:
            user: User object

        Returns:
            True if user is telegram-only, False otherwise
        """
        return (
            user.registration_source == "telegram"
            and user.email
            and user.email.startswith("telegram_")
            and user.email.endswith("@bot.internal")
        )

    def cleanup_user_sessions(self, user_id: int = None, exclude_current: bool = True) -> Dict[str, int]:
        """
        Clean up user sessions - either for specific user or all users

        Args:
            user_id: Specific user ID to clean up (None for all users)
            exclude_current: Whether to exclude current session from cleanup

        Returns:
            Dictionary with cleanup statistics
        """
        from business_app.services.session_cleanup_service import SessionCleanupService

        cleanup_service = SessionCleanupService()

        if user_id:
            # Cleanup specific user's sessions
            now = datetime.now(timezone.utc)

            # Get current session token if we should exclude it
            current_session_jti = None
            if exclude_current:
                try:
                    current_session_jti = get_jwt_identity()
                except:  # noqa: E722
                    pass

            # Mark old sessions as inactive
            query = UserSession.query.filter_by(user_id=user_id, is_active=True)

            if current_session_jti:
                query = query.filter(UserSession.session_token != current_session_jti)

            updated_count = query.update({"is_active": False, "ended_at": now})

            db.session.commit()

            logger.info(f"Cleaned up {updated_count} sessions for user {user_id}")
            return {"user_sessions_cleaned": updated_count}
        else:
            # Full session cleanup
            return cleanup_service.cleanup_expired_sessions()

    def authenticate_telegram_user(
        self, telegram_id: int, username: str = None, first_name: str = None, last_name: str = None
    ) -> Tuple[User, Dict[str, str]]:
        """
        Authenticate user via Telegram ID. Creates a new user if not found.

        Args:
            telegram_id: Telegram user ID
            username: Telegram username (optional)
            first_name: Telegram first name (optional)
            last_name: Telegram last name (optional)

        Returns:
            Tuple of (User object, tokens dict)

        Raises:
            UnauthorizedError: If authentication fails
        """
        logger.info("=== TELEGRAM USER AUTHENTICATION START ===")
        logger.info(f"Authenticating telegram user: {telegram_id}")
        logger.info(f"User info - username: {username}, first_name: {first_name}, last_name: {last_name}")

        # Find user by telegram_id
        logger.info(f"Searching for user with telegram_id: {telegram_id}")
        user = User.query.filter_by(telegram_id=str(telegram_id)).first()
        logger.info(f"Database lookup result: {'User found' if user else 'User not found'}")

        if not user:
            # Create a new user with telegram_id in unified table
            logger.info("Creating new telegram user in unified table...")
            try:
                # Generate a secure random password that the user will never use
                # Telegram users will set their own password if they want web access
                random_password = secrets.token_urlsafe(32)
                secure_password_hash = self._hash_password(random_password)

                user = User(
                    telegram_id=str(telegram_id),
                    first_name=first_name or "Telegram User",
                    last_name=last_name or "",
                    email=f"telegram_{telegram_id}@bot.internal",  # Placeholder email with proper domain
                    phone=None,  # No phone initially
                    password_hash=secure_password_hash,  # Secure random password hash
                    role=UserRole.CUSTOMER.value,  # Use enum
                    status=UserStatus.ACTIVE.value,  # Use enum
                    is_verified=False,
                    registration_source="telegram",
                    # Bot-specific fields in unified table
                    telegram_username=username,
                    is_bot_active=True,
                    bot_state="{}",  # Empty initial state
                    last_bot_interaction=datetime.now(timezone.utc),
                )

                logger.info(f"Adding new user to unified database: telegram_id={telegram_id}, username={username}")
                db.session.add(user)
                db.session.commit()

                logger.info(f"Successfully created new telegram user with ID: {user.id}")

            except Exception as e:
                logger.exception("Error creating telegram user (type=%s)", type(e).__name__)
                db.session.rollback()
                raise UnauthorizedError(get_translation("error.server_error"))
        else:
            # Update existing user information from Telegram if provided
            logger.info(f"Found existing user: ID={user.id}")
            updates_made = False
            is_telegram_only_user = self._is_telegram_only_user(user)

            # Update basic user info
            if first_name and first_name != user.first_name:
                can_update_first_name = (
                    is_telegram_only_user or not user.first_name or user.first_name == "Telegram User"
                )
                if can_update_first_name:
                    logger.info(f"Updating first_name: {user.first_name} -> {first_name}")
                    user.first_name = first_name
                    updates_made = True
                else:
                    logger.info(f"Preserving existing first_name for merged user {user.id}")

            if last_name and last_name != user.last_name:
                can_update_last_name = is_telegram_only_user or not user.last_name
                if can_update_last_name:
                    logger.info(f"Updating last_name: {user.last_name} -> {last_name}")
                    user.last_name = last_name
                    updates_made = True
                else:
                    logger.info(f"Preserving existing last_name for merged user {user.id}")

            # Update telegram-specific fields in unified table
            if username and username != user.telegram_username:
                logger.info(f"Updating telegram_username: {user.telegram_username} -> {username}")
                user.telegram_username = username
                updates_made = True

            # Update bot activity
            user.is_bot_active = True
            user.last_bot_interaction = datetime.now(timezone.utc)
            user.last_login = datetime.now(timezone.utc)
            updates_made = True

            if updates_made:
                logger.info("Committing user updates to database")
                try:
                    db.session.commit()
                    logger.info("User updates committed successfully")
                except Exception as e:
                    logger.exception("Database error during telegram auth (type=%s)", type(e).__name__)
                    db.session.rollback()
                    raise UnauthorizedError(get_translation("api.auth.authentication_failed"))

        # Check if user account is active
        logger.info(f"Checking user status: {user.status}")
        status_value = user.status.value if hasattr(user.status, "value") else user.status
        if status_value != UserStatus.ACTIVE.value:
            logger.error(f"User account not active: {user.status}")
            raise UnauthorizedError(get_translation("api.auth.account_disabled"))

        # Generate tokens
        logger.info("Generating JWT tokens for user")
        tokens = self._generate_tokens(user)
        logger.info(
            f"Tokens generated successfully: access_token={'present' if tokens.get('access_token') else 'missing'}"
        )

        # Reset any failed login attempts
        logger.info("Resetting any failed login attempts")
        self._reset_failed_attempts(str(telegram_id))

        logger.info("=== TELEGRAM USER AUTHENTICATION SUCCESS ===")
        return user, tokens
