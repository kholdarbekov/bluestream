"""
Enhanced password security utilities with proper bcrypt configuration
"""
import os
import re
import bcrypt
import logging
from typing import Tuple, Optional
from flask import current_app

logger = logging.getLogger(__name__)


class PasswordSecurityConfig:
    """Password security configuration management"""
    
    # Recommended bcrypt rounds by use case
    # Production: 12-15 rounds for good security/performance balance
    # Development: 4-8 rounds for faster testing
    # High security: 15-18 rounds for maximum security
    
    DEFAULT_ROUNDS = 12  # Good balance for production
    MIN_ROUNDS = 4       # Minimum for any environment
    MAX_ROUNDS = 18      # Maximum reasonable rounds
    
    @classmethod
    def get_bcrypt_rounds(cls) -> int:
        """
        Get bcrypt rounds from configuration with proper validation
        
        Returns:
            int: Number of bcrypt rounds to use
        """
        try:
            # Try to get from Flask config first
            if current_app:
                rounds = current_app.config.get('BCRYPT_ROUNDS')
                if rounds is not None:
                    return cls._validate_rounds(rounds)
        except RuntimeError:
            # No application context
            pass
        
        # Fall back to environment variable
        rounds_env = os.environ.get('BCRYPT_ROUNDS')
        if rounds_env:
            try:
                rounds = int(rounds_env)
                return cls._validate_rounds(rounds)
            except ValueError:
                logger.warning(f"Invalid BCRYPT_ROUNDS environment variable: {rounds_env}")
        
        # Use default based on environment
        if os.environ.get('FLASK_ENV') == 'development':
            return 8  # Faster for development
        elif os.environ.get('FLASK_ENV') == 'testing':
            return 4  # Fastest for testing
        else:
            return cls.DEFAULT_ROUNDS  # Production default
    
    @classmethod
    def _validate_rounds(cls, rounds: int) -> int:
        """
        Validate bcrypt rounds
        
        Args:
            rounds: Number of rounds to validate
            
        Returns:
            int: Validated rounds
            
        Raises:
            ValueError: If rounds are invalid
        """
        if not isinstance(rounds, int):
            raise ValueError(f"Bcrypt rounds must be an integer, got {type(rounds)}")
        
        if rounds < cls.MIN_ROUNDS:
            logger.warning(f"Bcrypt rounds {rounds} is too low, using minimum {cls.MIN_ROUNDS}")
            return cls.MIN_ROUNDS
        
        if rounds > cls.MAX_ROUNDS:
            logger.warning(f"Bcrypt rounds {rounds} is too high, using maximum {cls.MAX_ROUNDS}")
            return cls.MAX_ROUNDS
        
        return rounds
    
    @classmethod
    def get_performance_estimate(cls, rounds: int) -> str:
        """
        Get performance estimate for given rounds
        
        Args:
            rounds: Number of bcrypt rounds
            
        Returns:
            str: Performance estimate description
        """
        if rounds <= 8:
            return "Fast (suitable for development/testing)"
        elif rounds <= 12:
            return "Balanced (recommended for production)"
        elif rounds <= 15:
            return "Secure (high security applications)"
        else:
            return "Very secure (maximum security, slower)"


class SecurePasswordHasher:
    """Secure password hashing with proper bcrypt configuration"""
    
    def __init__(self, rounds: Optional[int] = None):
        """
        Initialize password hasher
        
        Args:
            rounds: Optional custom rounds, uses config default if None
        """
        self.rounds = rounds or PasswordSecurityConfig.get_bcrypt_rounds()
        
        # Log configuration for security audit
        logger.info(f"Password hasher initialized with {self.rounds} bcrypt rounds "
                   f"({PasswordSecurityConfig.get_performance_estimate(self.rounds)})")
    
    def hash_password(self, password: str) -> str:
        """
        Hash password with configured bcrypt rounds
        
        Args:
            password: Plain text password to hash
            
        Returns:
            str: Bcrypt hashed password
            
        Raises:
            ValueError: If password is invalid
        """
        if not password:
            raise ValueError("Password cannot be empty")
        
        if not isinstance(password, str):
            raise ValueError("Password must be a string")
        
        # Generate salt with specified rounds
        salt = bcrypt.gensalt(rounds=self.rounds)
        
        # Hash password
        password_hash = bcrypt.hashpw(password.encode('utf-8'), salt)
        
        # Decode to string for storage
        return password_hash.decode('utf-8')
    
    def verify_password(self, password: str, password_hash: str) -> bool:
        """
        Verify password against hash
        
        Args:
            password: Plain text password to verify
            password_hash: Stored password hash
            
        Returns:
            bool: True if password matches, False otherwise
        """
        if not password or not password_hash:
            return False
        
        try:
            return bcrypt.checkpw(
                password.encode('utf-8'), 
                password_hash.encode('utf-8')
            )
        except (ValueError, TypeError) as e:
            logger.warning(f"Password verification error: {e}")
            return False
    
    def needs_rehash(self, password_hash: str) -> bool:
        """
        Check if password hash needs to be updated with current rounds
        
        Args:
            password_hash: Stored password hash
            
        Returns:
            bool: True if hash should be updated
        """
        if not password_hash:
            return True
        
        try:
            # Extract rounds from hash
            hash_rounds = self._extract_rounds_from_hash(password_hash)
            return hash_rounds != self.rounds
        except Exception as e:
            logger.warning(f"Could not determine hash rounds: {e}")
            return True
    
    def _extract_rounds_from_hash(self, password_hash: str) -> int:
        """
        Extract bcrypt rounds from password hash
        
        Args:
            password_hash: Bcrypt password hash
            
        Returns:
            int: Number of rounds used in hash
            
        Raises:
            ValueError: If hash format is invalid
        """
        # Bcrypt hash format: $2a$rounds$salthash
        match = re.match(r'^\$2[abxy]?\$(\d+)\$', password_hash)
        if not match:
            raise ValueError("Invalid bcrypt hash format")
        
        return int(match.group(1))


# Global password hasher instance
_password_hasher = None


def get_password_hasher() -> SecurePasswordHasher:
    """
    Get global password hasher instance
    
    Returns:
        SecurePasswordHasher: Global password hasher
    """
    global _password_hasher
    if _password_hasher is None:
        _password_hasher = SecurePasswordHasher()
    return _password_hasher


def hash_password(password: str) -> str:
    """
    Hash password using secure configuration
    
    Args:
        password: Plain text password to hash
        
    Returns:
        str: Bcrypt hashed password
    """
    return get_password_hasher().hash_password(password)


def verify_password(password: str, password_hash: str) -> bool:
    """
    Verify password against hash
    
    Args:
        password: Plain text password to verify
        password_hash: Stored password hash
        
    Returns:
        bool: True if password matches, False otherwise
    """
    return get_password_hasher().verify_password(password, password_hash)


def needs_password_rehash(password_hash: str) -> bool:
    """
    Check if password hash needs to be updated
    
    Args:
        password_hash: Stored password hash
        
    Returns:
        bool: True if hash should be updated
    """
    return get_password_hasher().needs_rehash(password_hash)


def setup_password_security(app):
    """
    Setup password security configuration for Flask app
    
    Args:
        app: Flask application
    """
    # Set default bcrypt rounds if not configured
    if 'BCRYPT_ROUNDS' not in app.config:
        if app.config.get('TESTING'):
            app.config['BCRYPT_ROUNDS'] = 4  # Fast for testing
        elif app.config.get('DEBUG'):
            app.config['BCRYPT_ROUNDS'] = 8  # Reasonable for development
        else:
            app.config['BCRYPT_ROUNDS'] = 12  # Secure for production
    
    # Validate configuration
    rounds = PasswordSecurityConfig.get_bcrypt_rounds()
    performance = PasswordSecurityConfig.get_performance_estimate(rounds)
    
    app.logger.info(f"Password security configured: {rounds} bcrypt rounds ({performance})")
    
    # Initialize global hasher
    global _password_hasher
    _password_hasher = SecurePasswordHasher(rounds)


def validate_bcrypt_hash(password_hash: str) -> Tuple[bool, str]:
    """
    Validate bcrypt hash format and security
    
    Args:
        password_hash: Password hash to validate
        
    Returns:
        Tuple[bool, str]: (is_valid, message)
    """
    if not password_hash:
        return False, "Password hash is required"
    
    if not isinstance(password_hash, str):
        return False, "Password hash must be a string"
    
    # Check minimum length for bcrypt
    if len(password_hash) < 60:
        return False, "Password hash is too short for bcrypt"
    
    # Check bcrypt format
    bcrypt_pattern = r'^\$2[abxy]?\$(\d+)\$[./A-Za-z0-9]{53}$'
    match = re.match(bcrypt_pattern, password_hash)
    
    if not match:
        return False, "Invalid bcrypt hash format"
    
    # Extract and validate rounds
    try:
        rounds = int(match.group(1))
        if rounds < PasswordSecurityConfig.MIN_ROUNDS:
            return False, f"Bcrypt rounds {rounds} is too low (minimum {PasswordSecurityConfig.MIN_ROUNDS})"
        if rounds > PasswordSecurityConfig.MAX_ROUNDS:
            return False, f"Bcrypt rounds {rounds} is too high (maximum {PasswordSecurityConfig.MAX_ROUNDS})"
    except ValueError:
        return False, "Invalid bcrypt rounds in hash"
    
    return True, "Password hash is valid"