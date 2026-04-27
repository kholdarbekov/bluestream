"""
JWT Token Management Service for Blue Stream Water Business Platform
"""

import logging
import redis
import json
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, List
from flask import current_app, request
from flask_jwt_extended import create_access_token, create_refresh_token, decode_token, get_jti
import jwt

from business_app.models.user import User
from business_app.utils.constants import UserStatus

logger = logging.getLogger(__name__)


class TokenService:
    """Service for managing JWT tokens and sessions"""

    _instance = None
    _initialized = False

    def __new__(cls):
        """Singleton pattern to ensure only one instance exists"""
        if cls._instance is None:
            cls._instance = super(TokenService, cls).__new__(cls)
        return cls._instance

    def __init__(self):
        # Only initialize once (singleton pattern)
        if TokenService._initialized:
            return

        # Initialize Redis connection for token blacklist and session management
        self.redis_client = None
        self.redis_available = False
        self._in_memory_blacklist = {}  # Changed to dict to store expiry times
        self._initialize_redis()

        TokenService._initialized = True

    def _initialize_redis(self):
        """Initialize Redis connection with proper fallback"""
        try:
            # Try to get Redis URL from Flask config first, then environment
            import os

            redis_url = None
            try:
                redis_url = current_app.config.get("REDIS_URL")
            except RuntimeError:
                # No application context available
                pass

            if not redis_url:
                redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

            self.redis_client = redis.from_url(redis_url, decode_responses=True)
            # Test connection
            self.redis_client.ping()
            self.redis_available = True
            # Only log on initial connection or reconnection
            if not hasattr(self, "_redis_connected"):
                logger.info(f"Redis connected successfully to {redis_url}")
                self._redis_connected = True
        except Exception as e:
            # Only log if this is first connection attempt or status changed
            if not hasattr(self, "_redis_connected") or self.redis_available:
                logger.warning(f"Redis not available, using in-memory blacklist: {e}")
            self.redis_available = False
            self.redis_client = None
            self._redis_connected = False

    @staticmethod
    def _normalize_user_lookup_id(user_id: Any) -> Any:
        """Normalize JWT subject values before using them in ORM lookups."""
        if isinstance(user_id, str):
            normalized = user_id.strip()
            if normalized.isdigit():
                return int(normalized)
            return normalized
        return user_id

    @staticmethod
    def _is_user_active(user: Optional[User]) -> bool:
        """Treat both enum-backed and raw-string statuses as active when equivalent."""
        if not user:
            return False

        status = user.status.value if hasattr(user.status, "value") else user.status
        return status == UserStatus.ACTIVE.value

    def generate_tokens(self, user: User, additional_claims: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Generate access and refresh tokens for a user

        Args:
            user: User object
            additional_claims: Additional claims to include in the token

        Returns:
            Dictionary with access_token, refresh_token, and metadata
        """
        try:
            # Prepare additional claims
            claims = {
                "user_id": user.id,
                "email": user.email,
                "role": user.role.value if hasattr(user.role, "value") else user.role,
                "status": user.status.value if hasattr(user.status, "value") else user.status,
                "verified": user.is_verified,
                "platform": getattr(request, "platform", "web"),
                "ip": request.headers.get("X-Forwarded-For", request.remote_addr),
                "user_agent": request.headers.get("User-Agent", ""),
                "issued_at": datetime.now(timezone.utc).isoformat(),
                "session_id": self._generate_session_id(user.id),
            }

            if additional_claims:
                claims.update(additional_claims)

            # Get token expiration times from configuration
            access_token_expires = current_app.config.get("JWT_ACCESS_TOKEN_EXPIRES", timedelta(hours=1))
            refresh_token_expires = current_app.config.get("JWT_REFRESH_TOKEN_EXPIRES", timedelta(days=30))

            # Create tokens with configured expiration times
            access_token = create_access_token(
                identity=str(user.id), expires_delta=access_token_expires, additional_claims=claims
            )

            refresh_token = create_refresh_token(
                identity=str(user.id),
                expires_delta=refresh_token_expires,
                additional_claims={
                    "user_id": user.id,
                    "session_id": claims["session_id"],
                    "platform": claims["platform"],
                },
            )

            # Store session information
            self._store_session_info(
                user.id,
                claims["session_id"],
                {
                    "platform": claims["platform"],
                    "ip": claims["ip"],
                    "user_agent": claims["user_agent"],
                    "created_at": claims["issued_at"],
                    "access_token_jti": get_jti(access_token),
                    "refresh_token_jti": get_jti(refresh_token),
                },
            )

            # Update user's last login
            user.last_login = datetime.now(timezone.utc)
            user.last_platform_activity = claims["platform"]

            return {
                "access_token": access_token,
                "refresh_token": refresh_token,
                "token_type": "Bearer",
                "expires_in": int(access_token_expires.total_seconds()),
                "session_id": claims["session_id"],
                "user": {
                    "id": user.id,
                    "email": user.email,
                    "first_name": user.first_name,
                    "last_name": user.last_name,
                    "role": user.role.value if hasattr(user.role, "value") else user.role,
                    "verified": user.is_verified,
                },
            }

        except Exception:
            logger.exception("Failed to generate tokens for user %s", user.id)
            raise

    def refresh_access_token(self, refresh_token: str) -> Dict[str, Any]:
        """
        Generate new access token using refresh token

        Args:
            refresh_token: Valid refresh token

        Returns:
            Dictionary with new access_token and metadata
        """
        try:
            # Decode refresh token to get claims
            claims = decode_token(refresh_token)
            user_id = claims["sub"]
            lookup_user_id = self._normalize_user_lookup_id(user_id)
            session_id = claims.get("session_id")

            # Check if refresh token is blacklisted
            refresh_jti = claims["jti"]
            if self.is_token_blacklisted(refresh_jti):
                raise ValueError("Refresh token is blacklisted")

            # Get user
            user = User.query.get(lookup_user_id)
            if not self._is_user_active(user):
                raise ValueError("User not found or inactive")

            # Validate session
            if not self._validate_session(lookup_user_id, session_id):
                raise ValueError("Invalid session")

            # Get access token expiration from configuration
            access_token_expires = current_app.config.get("JWT_ACCESS_TOKEN_EXPIRES", timedelta(hours=1))

            # Generate new access token
            new_claims = {
                "user_id": user.id,
                "email": user.email,
                "role": user.role.value if hasattr(user.role, "value") else user.role,
                "status": user.status.value if hasattr(user.status, "value") else user.status,
                "verified": user.is_verified,
                "platform": claims.get("platform", "web"),
                "ip": request.headers.get("X-Forwarded-For", request.remote_addr),
                "user_agent": request.headers.get("User-Agent", ""),
                "issued_at": datetime.now(timezone.utc).isoformat(),
                "session_id": session_id,
            }
            # Preserve staff context across refreshes for staff-bot tokens.
            if getattr(user, "staff_roles", None):
                new_claims["staff_roles"] = user.staff_roles

            access_token = create_access_token(
                identity=str(user.id), expires_delta=access_token_expires, additional_claims=new_claims
            )

            # Update session info
            self._update_session_info(
                lookup_user_id,
                session_id,
                {"last_refresh": datetime.now(timezone.utc).isoformat(), "access_token_jti": get_jti(access_token)},
            )

            return {
                "access_token": access_token,
                "token_type": "Bearer",
                "expires_in": int(access_token_expires.total_seconds()),
                "session_id": session_id,
            }

        except Exception:
            logger.exception("Failed to refresh token")
            raise

    def blacklist_token(
        self, token_jti: str, expires_delta: Optional[timedelta] = None, token: Optional[str] = None
    ) -> bool:
        """
        Add token to blacklist with proper TTL matching token expiration

        Args:
            token_jti: JTI (JWT ID) of the token to blacklist
            expires_delta: How long to keep token in blacklist (defaults to calculated expiry)
            token: The actual token to extract expiry from (optional)

        Returns:
            True if successfully blacklisted
        """
        try:
            # Calculate proper expiry time
            if expires_delta:
                expiry_seconds = int(expires_delta.total_seconds())
            elif token:
                # Extract expiry from token itself
                try:
                    claims = decode_token(token)
                    exp_timestamp = claims.get("exp")
                    if exp_timestamp:
                        now = datetime.now(timezone.utc)
                        exp_datetime = datetime.fromtimestamp(exp_timestamp, tz=timezone.utc)
                        remaining_time = exp_datetime - now
                        expiry_seconds = max(int(remaining_time.total_seconds()), 1)  # At least 1 second
                    else:
                        # Fallback to default access token expiry
                        default_expires = current_app.config.get("JWT_ACCESS_TOKEN_EXPIRES", timedelta(hours=1))
                        expiry_seconds = int(default_expires.total_seconds())
                except Exception as e:
                    logger.warning(f"Could not extract token expiry, using default: {e}")
                    default_expires = current_app.config.get("JWT_ACCESS_TOKEN_EXPIRES", timedelta(hours=1))
                    expiry_seconds = int(default_expires.total_seconds())
            else:
                # Default to access token expiry from config
                default_expires = current_app.config.get("JWT_ACCESS_TOKEN_EXPIRES", timedelta(hours=1))
                expiry_seconds = int(default_expires.total_seconds())

            if self.redis_available:
                # Store in Redis with proper expiration
                return self.redis_client.setex(
                    f"blacklist:{token_jti}", expiry_seconds, datetime.now(timezone.utc).isoformat()
                )
            else:
                # Store in memory with expiry time (not persistent across restarts)
                expiry_time = datetime.now(timezone.utc) + timedelta(seconds=expiry_seconds)
                self._in_memory_blacklist[token_jti] = expiry_time
                return True

        except Exception:
            logger.exception("Failed to blacklist token %s", token_jti)
            return False

    def is_token_blacklisted(self, token_jti: str) -> bool:
        """
        Check if token is blacklisted

        Args:
            token_jti: JTI (JWT ID) of the token to check

        Returns:
            True if token is blacklisted
        """
        try:
            if self.redis_available:
                is_blacklisted = bool(self.redis_client.exists(f"blacklist:{token_jti}"))
                if is_blacklisted:
                    logger.debug(f"Token {token_jti} found in Redis blacklist")
                return is_blacklisted
            else:
                # Check in-memory blacklist with expiry
                if token_jti in self._in_memory_blacklist:
                    expiry_time = self._in_memory_blacklist[token_jti]
                    if datetime.now(timezone.utc) < expiry_time:
                        logger.debug(f"Token {token_jti} found in in-memory blacklist")
                        return True
                    else:
                        # Token has expired, remove it from blacklist
                        del self._in_memory_blacklist[token_jti]
                        return False
                return False

        except Exception:
            logger.exception("Failed to check blacklist for token %s", token_jti)
            # Fail open: return False to avoid authentication disruption
            # In stricter environments, you might want to fail closed (return True)
            return False

    def blacklist_token_by_string(self, token: str) -> bool:
        """
        Blacklist a token using the token string itself

        Args:
            token: The JWT token string to blacklist

        Returns:
            True if successfully blacklisted
        """
        try:
            # Decode token to get JTI
            claims = decode_token(token)
            token_jti = claims["jti"]

            # Blacklist with proper expiry extracted from token
            return self.blacklist_token(token_jti, token=token)

        except Exception:
            logger.exception("Failed to blacklist token by string")
            return False

    def revoke_user_tokens(self, user_id: int, exclude_session_id: Optional[str] = None) -> bool:
        """
        Revoke all tokens for a user (useful for logout all sessions)

        Args:
            user_id: User ID
            exclude_session_id: Session ID to exclude from revocation

        Returns:
            True if successfully revoked
        """
        try:
            sessions = self.get_user_sessions(user_id)

            for session in sessions:
                if exclude_session_id and session["session_id"] == exclude_session_id:
                    continue

                # Blacklist access and refresh tokens for this session with proper expiry
                if "access_token_jti" in session:
                    # Use access token expiry for access token
                    access_expires = current_app.config.get("JWT_ACCESS_TOKEN_EXPIRES", timedelta(hours=1))
                    self.blacklist_token(session["access_token_jti"], expires_delta=access_expires)

                if "refresh_token_jti" in session:
                    # Use refresh token expiry for refresh token
                    refresh_expires = current_app.config.get("JWT_REFRESH_TOKEN_EXPIRES", timedelta(days=30))
                    self.blacklist_token(session["refresh_token_jti"], expires_delta=refresh_expires)

                # Remove session info
                self._remove_session_info(user_id, session["session_id"])

            return True

        except Exception:
            logger.exception("Failed to revoke tokens for user %s", user_id)
            return False

    def validate_token_integrity(self, token: str) -> Dict[str, Any]:
        """
        Validate token integrity and return claims

        Args:
            token: JWT token to validate

        Returns:
            Dictionary with validation result and claims
        """
        try:
            # Decode token
            claims = decode_token(token)
            token_jti = claims["jti"]
            user_id = claims["sub"]
            lookup_user_id = self._normalize_user_lookup_id(user_id)
            session_id = claims.get("session_id")

            # Check if token is blacklisted
            if self.is_token_blacklisted(token_jti):
                return {"valid": False, "reason": "Token is blacklisted", "error_code": "TOKEN_BLACKLISTED"}

            # Check user status
            user = User.query.get(lookup_user_id)
            if not user:
                return {"valid": False, "reason": "User not found", "error_code": "USER_NOT_FOUND"}

            if not self._is_user_active(user):
                return {"valid": False, "reason": "User account is not active", "error_code": "USER_INACTIVE"}

            # Validate session if session_id is present
            if session_id and not self._validate_session(lookup_user_id, session_id):
                return {"valid": False, "reason": "Invalid session", "error_code": "INVALID_SESSION"}

            return {"valid": True, "claims": claims, "user": user}

        except jwt.ExpiredSignatureError:
            return {"valid": False, "reason": "Token has expired", "error_code": "TOKEN_EXPIRED"}
        except jwt.InvalidTokenError as e:
            return {"valid": False, "reason": f"Invalid token: {str(e)}", "error_code": "TOKEN_INVALID"}
        except Exception:
            logger.exception("Token validation error")
            return {"valid": False, "reason": "Token validation failed", "error_code": "VALIDATION_FAILED"}

    def get_user_sessions(self, user_id: int) -> List[Dict[str, Any]]:
        """
        Get all active sessions for a user

        Args:
            user_id: User ID

        Returns:
            List of session dictionaries
        """
        try:
            if not self.redis_available:
                return []

            pattern = f"session:{user_id}:*"
            session_keys = self.redis_client.keys(pattern)

            sessions = []
            for key in session_keys:
                session_data = self.redis_client.get(key)
                if session_data:
                    session = json.loads(session_data)
                    session["session_id"] = key.split(":")[-1]
                    sessions.append(session)

            return sorted(sessions, key=lambda x: x.get("created_at", ""), reverse=True)

        except Exception:
            logger.exception("Failed to get sessions for user %s", user_id)
            return []

    def cleanup_expired_sessions(self) -> int:
        """
        Clean up expired sessions and blacklisted tokens

        Returns:
            Number of cleaned up items
        """
        cleaned_count = 0

        try:
            # Clean up expired in-memory blacklist entries
            if not self.redis_available:
                now = datetime.now(timezone.utc)
                expired_tokens = [
                    token_jti for token_jti, expiry_time in self._in_memory_blacklist.items() if now >= expiry_time
                ]
                for token_jti in expired_tokens:
                    del self._in_memory_blacklist[token_jti]
                    cleaned_count += 1

                logger.info(f"Cleaned up {cleaned_count} expired in-memory blacklist entries")
                return cleaned_count

            # Clean up expired blacklist entries (Redis handles this automatically with TTL)
            # Clean up expired sessions
            pattern = "session:*"
            session_keys = self.redis_client.keys(pattern)

            for key in session_keys:
                session_data = self.redis_client.get(key)
                if session_data:
                    session = json.loads(session_data)
                    created_at = datetime.fromisoformat(session.get("created_at", ""))

                    # Remove sessions older than 30 days
                    if datetime.now(timezone.utc) - created_at > timedelta(days=30):
                        self.redis_client.delete(key)
                        cleaned_count += 1

            logger.info(f"Cleaned up {cleaned_count} expired sessions")
            return cleaned_count

        except Exception:
            logger.exception("Failed to cleanup expired sessions")
            return 0

    def _generate_session_id(self, user_id: int) -> str:
        """Generate unique session ID"""
        import uuid

        return f"{user_id}_{uuid.uuid4().hex[:16]}"

    def _ensure_redis_connection(self):
        """Ensure Redis connection is available, reinitialize if needed"""
        # Only attempt reconnection if we know it's not available
        # Avoid redundant pings on every operation
        if not self.redis_available or not self.redis_client:
            try:
                # Quick check if connection exists and is responsive
                if self.redis_client:
                    self.redis_client.ping()
                    self.redis_available = True
                else:
                    self._initialize_redis()
            except Exception:
                # Connection failed, reinitialize
                self._initialize_redis()

    def _store_session_info(self, user_id: int, session_id: str, session_data: Dict) -> bool:
        """Store session information"""
        try:
            if not self.redis_available:
                return True

            key = f"session:{user_id}:{session_id.split('_')[-1]}"
            return self.redis_client.setex(
                key, int(timedelta(days=30).total_seconds()), json.dumps(session_data)  # 30 days expiry
            )
        except Exception as e:
            logger.warning(f"Failed to store session info: {e}")
            # Don't fail registration due to session storage issues
            return True

    def _update_session_info(self, user_id: int, session_id: str, updates: Dict) -> bool:
        """Update existing session information"""
        try:
            if not self.redis_available:
                return True

            key = f"session:{user_id}:{session_id.split('_')[-1]}"
            existing_data = self.redis_client.get(key)

            if existing_data:
                session_data = json.loads(existing_data)
                session_data.update(updates)
                return self.redis_client.setex(key, timedelta(days=30).total_seconds(), json.dumps(session_data))

            return False
        except Exception:
            logger.exception("Failed to update session info")
            return False

    def _validate_session(self, user_id: int, session_id: str) -> bool:
        """Validate if session exists and is active"""
        try:
            if not self.redis_available:
                return True  # Skip validation if Redis unavailable

            key = f"session:{user_id}:{session_id.split('_')[-1]}"
            return self.redis_client.exists(key)
        except Exception:
            logger.exception("Failed to validate session")
            return False

    def _remove_session_info(self, user_id: int, session_id: str) -> bool:
        """Remove session information"""
        try:
            if not self.redis_available:
                return True

            key = f"session:{user_id}:{session_id.split('_')[-1]}"
            return self.redis_client.delete(key) > 0
        except Exception:
            logger.exception("Failed to remove session info")
            return False
