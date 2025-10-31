"""
Authentication service for the Water Business Platform
"""
import secrets
import string
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, Tuple
from flask import current_app, request
from flask_jwt_extended import get_jwt_identity
import redis

from business_app.models.user import User, UserSession
from business_app.utils.exceptions import ValidationError, UnauthorizedError, ConflictError
from business_app.utils.validators import EmailValidator, PhoneValidator, PasswordValidator
from business_app.utils.helpers import generate_otp, format_phone_number, generate_random_string
from business_app.utils.password_security import hash_password, verify_password, needs_password_rehash
from business_app.utils.constants import UserRole, UserStatus
from business_app.tasks.notification_tasks import send_verification_sms_task
from business_app import db

logger = logging.getLogger(__name__)


class AuthService:
    """Authentication and authorization service"""
    
    def __init__(self):
        # Get Redis URL with proper fallback
        import os
        try:
            redis_url = current_app.config['REDIS_URL']
        except RuntimeError:
            # No application context available, use environment variable
            redis_url = os.environ.get('REDIS_URL', 'redis://localhost:6379/0')
        
        self.redis_client = redis.from_url(redis_url)
        self.otp_expiry = 300  # 5 minutes
        self.max_login_attempts = current_app.config.get('MAX_LOGIN_ATTEMPTS', 5)
        self.lockout_duration = current_app.config.get('LOCKOUT_DURATION', 1800)  # 30 minutes
    
    def register_user(self, email: str, password: str, phone: str, 
                     first_name: str, last_name: str, **kwargs) -> Tuple[User, Dict[str, str]]:
        """
        Register a new user
        
        Args:
            email: User email
            password: User password
            phone: User phone number
            first_name: User first name
            last_name: User last name
            **kwargs: Additional user data
        
        Returns:
            Tuple of (User object, tokens dict)
        
        Raises:
            ValidationError: If validation fails
            ConflictError: If user already exists
        """
        # Validate input data
        self._validate_registration_data(email, password, phone, first_name, last_name)
        
        # Check if user already exists
        existing_user = User.query.filter(
            (User.email == email.lower()) | (User.phone == phone)
        ).first()
        
        if existing_user:
            if existing_user.email == email.lower():
                raise ConflictError("User with this email already exists")
            else:
                raise ConflictError("User with this phone number already exists")
        
        # Create new user
        # Filter out invalid User model fields from kwargs
        valid_user_fields = {
            'full_name', 'date_of_birth', 'gender', 'role', 'status', 'is_verified', 
            'is_premium', 'preferred_language', 'preferred_currency', 'timezone',
            'email_notifications', 'sms_notifications', 'push_notifications',
            'company_name', 'tax_id', 'business_type', 'email_verification_token',
            'email_verified_at', 'telegram_id', 'registration_source',
            'telegram_username', 'telegram_first_name', 'telegram_last_name',
            'telegram_language_code', 'is_bot_active', 'bot_state', 'last_bot_interaction'
        }
        filtered_kwargs = {k: v for k, v in kwargs.items() if k in valid_user_fields}
        
        user = User(
            email=email.lower().strip(),
            phone=format_phone_number(phone),
            first_name=first_name.strip(),
            last_name=last_name.strip(),
            password_hash=self._hash_password(password),
            role=UserRole.CUSTOMER.value,
            status=UserStatus.PENDING_VERIFICATION.value,
            **filtered_kwargs
        )
        
        db.session.add(user)
        db.session.commit()
        
        # Generate tokens
        tokens = self._generate_tokens(user)
        
        # Send verification emails/SMS
        self._send_verification_notifications(user)
        
        # Log user session
        self._create_user_session(user.id, tokens['access_token'])
        
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
        user = User.query.filter(
            (User.email == identifier.lower()) |
            (User.phone == identifier)
        ).first()

        if not user:
            self._increment_failed_attempts(identifier)
            raise UnauthorizedError("Invalid credentials")

        # Check if this is a telegram-only user trying to login with placeholder email
        if self._is_telegram_only_user(user) and identifier.lower() == user.email:
            raise UnauthorizedError(
                "This account was created via Telegram. Please set a password first "
                "or use the Telegram bot to access your account."
            )

        if not self._verify_password(password, user.password_hash):
            # Increment failed login attempts
            self._increment_failed_attempts(identifier)
            raise UnauthorizedError("Invalid credentials")

        # Check if user is active
        if user.status in [UserStatus.BANNED.value, UserStatus.INACTIVE.value]:
            raise UnauthorizedError("Account is disabled")

        # Reset failed login attempts
        self._reset_failed_attempts(identifier)

        # Update last login
        user.last_login = datetime.now(timezone.utc)
        db.session.commit()

        # Generate tokens
        tokens = self._generate_tokens(user)

        # Log user session
        self._create_user_session(user.id, tokens['access_token'])

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
            user_id = decoded_token['sub']
            
            user = User.query.get(user_id)
            if not user or user.status in [UserStatus.BANNED.value, UserStatus.INACTIVE.value]:
                raise UnauthorizedError("Invalid user")
            
            # Generate new tokens
            tokens = self._generate_tokens(user)
            
            # Update session
            self._update_user_session(user.id, tokens['access_token'])
            
            return tokens
            
        except Exception:
            raise UnauthorizedError("Invalid refresh token")
    
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
                user_id = decoded_token['sub']
                
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
        token = self._generate_verification_token(user.id, 'email')
        
        # Send email (implement email service)
        from ..tasks.notification_tasks import send_verification_email_task
        send_verification_email_task.delay(user.id, token)
        
        return True
    
    def send_verification_sms(self, user_id: int, phone: str = None) -> bool:
        """
        Send SMS verification
        
        Args:
            user_id: User ID
            phone: Phone number to send OTP to (optional, uses user's phone if not provided)
        
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
        phone_validator = PhoneValidator(target_phone, 'phone')
        phone_validator.validate()
        if not phone_validator.is_valid():
            logger.error(f"Invalid phone number for SMS verification: {target_phone}")
            return False
        
        # Format phone number
        formatted_phone = format_phone_number(target_phone)
        
        # Update user's phone if a new phone was provided
        if phone and phone != user.phone:
            logger.info(f"Updating user {user_id} phone from {user.phone} to {formatted_phone}")
            user.phone = formatted_phone
            db.session.commit()
        
        # Generate OTP
        otp = generate_otp()
        
        # Store OTP in Redis
        key = f"sms_verification:{user.id}"
        self.redis_client.setex(key, self.otp_expiry, otp)
        
        # Send SMS
        send_verification_sms_task.delay(user.id, otp, formatted_phone)
        
        logger.info(f"SMS verification sent to {formatted_phone} for user {user_id}")
        return True
    
    def verify_email(self, token: str) -> bool:
        """Verify email with token"""
        user_id = self._verify_verification_token(token, 'email')
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
            if user.status == UserStatus.PENDING_VERIFICATION.value and user.email_verified:
                user.status = UserStatus.ACTIVE.value
                user.is_verified = True

            db.session.commit()
            return True

        return False
    
    def request_password_reset(self, identifier: str) -> bool:
        """Request password reset"""
        user = User.query.filter(
            (User.email == identifier.lower()) |
            (User.phone == identifier)
        ).first()
        
        if not user:
            # Return True to prevent email enumeration
            return True
        
        # Generate reset token
        token = self._generate_verification_token(user.id, 'password_reset')
        
        # Send reset email
        from ..tasks.notification_tasks import send_password_reset_email_task
        send_password_reset_email_task.delay(user.id, token)
        
        return True
    
    def reset_password(self, token: str, new_password: str) -> bool:
        """Reset password with token"""
        # Validate new password
        validator = PasswordValidator(new_password, 'password')
        validator.validate()
        if not validator.is_valid():
            raise ValidationError("Invalid password", {'password': validator.get_errors()})
        
        user_id = self._verify_verification_token(token, 'password_reset')
        if not user_id:
            return False
        
        user = User.query.get(user_id)
        if user:
            user.password_hash = self._hash_password(new_password)
            user.password_changed_at = datetime.now(timezone.utc)
            db.session.commit()
            
            # Invalidate all user sessions
            self._invalidate_all_user_sessions(user.id)
            
            return True
        
        return False
    
    def change_password(self, user_id: int, current_password: str, new_password: str) -> bool:
        """Change password for authenticated user"""
        user = User.query.get(user_id)
        if not user:
            return False
        
        # Verify current password
        if not self._verify_password(current_password, user.password_hash):
            raise UnauthorizedError("Current password is incorrect")
        
        # Validate new password
        validator = PasswordValidator(new_password, 'password')
        validator.validate()
        if not validator.is_valid():
            raise ValidationError("Invalid password", {'password': validator.get_errors()})
        
        # Update password
        user.password_hash = self._hash_password(new_password)
        user.password_changed_at = datetime.now(timezone.utc)
        db.session.commit()
        
        return True
    
    def create_admin_user(self, email: str, password: str, first_name: str = "Admin", 
                         last_name: str = "User") -> User:
        """Create admin user"""
        # Check if admin already exists
        existing_admin = User.query.filter_by(role=UserRole.ADMIN.value).first()
        if existing_admin:
            raise ConflictError("Admin user already exists")
        
        admin_user = User(
            email=email.lower().strip(),
            first_name=first_name,
            last_name=last_name,
            password_hash=self._hash_password(password),
            role=UserRole.ADMIN.value,
            status=UserStatus.ACTIVE.value,
            email_verified_at=datetime.now(timezone.utc)
        )
        
        db.session.add(admin_user)
        db.session.commit()
        
        return admin_user
    
    def get_user_permissions(self, user_id: int) -> Dict[str, bool]:
        """Get user permissions based on role"""
        user = User.query.get(user_id)
        if not user:
            return {}
        
        permissions = {
            'can_view_orders': True,
            'can_place_orders': True,
            'can_view_profile': True,
            'can_edit_profile': True,
            'can_view_analytics': False,
            'can_manage_users': False,
            'can_manage_products': False,
            'can_manage_orders': False,
            'can_manage_delivery': False,
            'can_view_admin_panel': False,
            'can_manage_settings': False,
            'can_manage_translations': False,
        }
        logger.info(f"USER.ROLE: {user.role}, ADMIN ROLES: {[UserRole.ADMIN.value, UserRole.MANAGER.value]}")
        if user.role in [UserRole.ADMIN.value, UserRole.MANAGER.value]:
            permissions.update({
                'can_view_analytics': True,
                'can_manage_users': True,
                'can_manage_products': True,
                'can_manage_orders': True,
                'can_manage_delivery': True,
                'can_view_admin_panel': True,
                'can_manage_settings': user.role == UserRole.ADMIN,
                'can_manage_translations': user.role == UserRole.ADMIN,
            })
        elif user.role == UserRole.OPERATOR.value:
            permissions.update({
                'can_view_analytics': True,
                'can_manage_orders': True,
                'can_view_admin_panel': True,
            })
        elif user.role == UserRole.DELIVERY_DRIVER.value:
            permissions.update({
                'can_manage_delivery': True,
                'can_view_admin_panel': True,
            })
        
        return permissions
    
    # Private methods
    def _validate_registration_data(self, email: str, password: str, phone: str, 
                                  first_name: str, last_name: str):
        """Validate registration data"""
        errors = {}
        
        # Validate email
        email_validator = EmailValidator(email, 'email')
        email_validator.validate()
        if not email_validator.is_valid():
            errors['email'] = email_validator.get_errors()
        
        # Validate password
        password_validator = PasswordValidator(password, 'password')
        password_validator.validate()
        if not password_validator.is_valid():
            errors['password'] = password_validator.get_errors()
        
        # Validate phone
        phone_validator = PhoneValidator(phone, 'phone')
        phone_validator.validate()
        if not phone_validator.is_valid():
            errors['phone'] = phone_validator.get_errors()
        
        # Validate names
        if not first_name or not first_name.strip():
            errors['first_name'] = ['First name is required']
        
        if not last_name or not last_name.strip():
            errors['last_name'] = ['Last name is required']
        
        if errors:
            raise ValidationError("Validation failed", errors)
    
    def _hash_password(self, password: str) -> str:
        """Hash password with configured bcrypt rounds"""
        return hash_password(password)
    
    def _verify_password(self, password: str, password_hash: str) -> bool:
        """Verify password and optionally rehash if needed"""
        # Verify password
        is_valid = verify_password(password, password_hash)
        
        # If verification successful and rehashing is enabled, check if hash needs update
        if is_valid and current_app.config.get('PASSWORD_REHASH_ON_LOGIN', True):
            if needs_password_rehash(password_hash):
                try:
                    # Find user and update password hash with new rounds
                    user = User.query.filter(User.password_hash == password_hash).first()
                    if user:
                        new_hash = self._hash_password(password)
                        user.password_hash = new_hash
                        db.session.commit()
                        logger.info(f"Password hash updated for user {user.id} with new bcrypt rounds")
                except Exception as e:
                    logger.error(f"Failed to update password hash: {e}")
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
    
    def _create_user_session(self, user_id: int, access_token: str):
        """Create user session record"""
        session = UserSession(
            user_id=user_id,
            session_token=self._get_token_jti(access_token),
            ip_address=request.remote_addr if request else None,
            user_agent=request.headers.get('User-Agent') if request else None,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=24)
        )
        
        db.session.add(session)
        db.session.commit()
    
    def _update_user_session(self, user_id: int, access_token: str):
        """Update user session with new token"""
        # End current sessions
        UserSession.query.filter_by(user_id=user_id, is_active=True).update({
            'is_active': False,
            'ended_at': datetime.now(timezone.utc)
        })
        
        # Create new session
        self._create_user_session(user_id, access_token)
    
    def _end_user_session(self, user_id: int, access_token: str):
        """End user session"""
        jti = self._get_token_jti(access_token)
        session = UserSession.query.filter_by(
            user_id=user_id,
            session_token=jti,
            is_active=True
        ).first()
        
        if session:
            session.is_active = False
            session.ended_at = datetime.now(timezone.utc)
            db.session.commit()
    
    def _invalidate_all_user_sessions(self, user_id: int):
        """Invalidate all user sessions"""
        UserSession.query.filter_by(user_id=user_id, is_active=True).update({
            'is_active': False,
            'ended_at': datetime.now(timezone.utc)
        })
        db.session.commit()
    
    def _get_token_jti(self, token: str) -> str:
        """Get JWT ID from token"""
        try:
            from flask_jwt_extended import decode_token
            decoded = decode_token(token)
            return decoded.get('jti', '')
        except:
            return ''
    
    # Removed _blacklist_token method - now using TokenService.blacklist_token_by_string()
    
    def _check_account_lockout(self, identifier: str):
        """Check if account is locked due to failed attempts"""
        key = f"login_attempts:{identifier}"
        attempts = self.redis_client.get(key)
        
        if attempts and int(attempts) >= self.max_login_attempts:
            lockout_key = f"account_lockout:{identifier}"
            if self.redis_client.exists(lockout_key):
                raise ValidationError("Account temporarily locked due to too many failed login attempts")
    
    def _increment_failed_attempts(self, identifier: str):
        """Increment failed login attempts"""
        key = f"login_attempts:{identifier}"
        attempts = self.redis_client.incr(key)
        self.redis_client.expire(key, self.lockout_duration)
        
        if attempts >= self.max_login_attempts:
            lockout_key = f"account_lockout:{identifier}"
            self.redis_client.setex(lockout_key, self.lockout_duration, '1')
    
    def _reset_failed_attempts(self, identifier: str):
        """Reset failed login attempts"""
        key = f"login_attempts:{identifier}"
        lockout_key = f"account_lockout:{identifier}"

        self.redis_client.delete(key, lockout_key)

    def _is_telegram_only_user(self, user: User) -> bool:
        """
        Check if user is telegram-only (hasn't set web password yet)

        Args:
            user: User object

        Returns:
            True if user is telegram-only, False otherwise
        """
        return (
            user.registration_source == 'telegram' and
            user.email and
            user.email.startswith('telegram_') and
            user.email.endswith('@bot.internal')
        )
    
    def cleanup_user_sessions(self, user_id: int = None, 
                             exclude_current: bool = True) -> Dict[str, int]:
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
                except:
                    pass
            
            # Mark old sessions as inactive
            query = UserSession.query.filter_by(user_id=user_id, is_active=True)
            
            if current_session_jti:
                query = query.filter(UserSession.session_token != current_session_jti)
            
            updated_count = query.update({
                'is_active': False,
                'ended_at': now
            })
            
            db.session.commit()
            
            logger.info(f"Cleaned up {updated_count} sessions for user {user_id}")
            return {'user_sessions_cleaned': updated_count}
        else:
            # Full session cleanup
            return cleanup_service.cleanup_expired_sessions()
    
    def authenticate_telegram_user(self, telegram_id: int, username: str = None, 
                                 first_name: str = None, last_name: str = None) -> Tuple[User, Dict[str, str]]:
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
                full_name = f"{first_name or ''} {last_name or ''}".strip()
                if not full_name:
                    full_name = f"User {telegram_id}"
                    
                # Generate a secure random password that the user will never use
                # Telegram users will set their own password if they want web access
                import secrets
                random_password = secrets.token_urlsafe(32)
                secure_password_hash = self._hash_password(random_password)

                user = User(
                    telegram_id=str(telegram_id),
                    first_name=first_name or "Telegram User",
                    last_name=last_name or "",
                    full_name=full_name,
                    email=f"telegram_{telegram_id}@bot.internal",  # Placeholder email with proper domain
                    phone=None,  # No phone initially
                    password_hash=secure_password_hash,  # Secure random password hash
                    role=UserRole.CUSTOMER.value,  # Use enum
                    status=UserStatus.ACTIVE.value,  # Use enum
                    is_verified=False,
                    registration_source='telegram',
                    # Bot-specific fields in unified table
                    telegram_username=username,
                    telegram_first_name=first_name,
                    telegram_last_name=last_name,
                    is_bot_active=True,
                    bot_state='{}',  # Empty initial state
                    last_bot_interaction=datetime.now(timezone.utc)
                )
                
                logger.info(f"Adding new user to unified database: telegram_id={telegram_id}, username={username}")
                db.session.add(user)
                db.session.commit()
                
                logger.info(f"Successfully created new telegram user with ID: {user.id}")
                
            except Exception as e:
                logger.error(f"Error creating telegram user: {e}")
                logger.error(f"Exception type: {type(e)}")
                db.session.rollback()
                raise UnauthorizedError("Failed to create user")
        else:
            # Update existing user information from Telegram if provided
            logger.info(f"Found existing user: ID={user.id}")
            updates_made = False
            
            # Update basic user info
            if first_name and first_name != user.first_name:
                logger.info(f"Updating first_name: {user.first_name} -> {first_name}")
                user.first_name = first_name
                updates_made = True
                
            if last_name and last_name != user.last_name:
                logger.info(f"Updating last_name: {user.last_name} -> {last_name}")
                user.last_name = last_name
                updates_made = True
            
            # Update telegram-specific fields in unified table
            if username and username != user.telegram_username:
                logger.info(f"Updating telegram_username: {user.telegram_username} -> {username}")
                user.telegram_username = username
                updates_made = True
                
            if first_name and first_name != user.telegram_first_name:
                user.telegram_first_name = first_name
                updates_made = True
                
            if last_name and last_name != user.telegram_last_name:
                user.telegram_last_name = last_name
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
                    logger.error(f"Database error during telegram auth: {e}")
                    logger.error(f"Exception type: {type(e)}")
                    db.session.rollback()
                    raise UnauthorizedError("Authentication failed")
        
        # Check if user account is active
        logger.info(f"Checking user status: {user.status}")
        if user.status != UserStatus.ACTIVE.value:
            logger.error(f"User account not active: {user.status}")
            raise UnauthorizedError("User account is not active")
        
        # Generate tokens
        logger.info("Generating JWT tokens for user")
        tokens = self._generate_tokens(user)
        logger.info(f"Tokens generated successfully: access_token={'present' if tokens.get('access_token') else 'missing'}")
        
        # Reset any failed login attempts
        logger.info("Resetting any failed login attempts")
        self._reset_failed_attempts(str(telegram_id))
        
        logger.info("=== TELEGRAM USER AUTHENTICATION SUCCESS ===")
        return user, tokens