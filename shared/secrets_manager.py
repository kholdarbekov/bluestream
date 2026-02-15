"""
Docker Secrets Manager for Blue Stream Water Business Platform

Provides utilities for reading secrets from Docker secrets files
and environment variables with fallback support.
"""
import os
import logging
from typing import Optional, Dict, Any
from pathlib import Path

logger = logging.getLogger(__name__)


class SecretsManager:
    """Manages reading secrets from Docker secrets or environment variables"""
    
    def __init__(self, secrets_path: str = "/run/secrets"):
        """
        Initialize secrets manager
        
        Args:
            secrets_path: Path to Docker secrets directory
        """
        self.secrets_path = Path(secrets_path)
        self._cache: Dict[str, str] = {}
        
    def get_secret(self, secret_name: str, env_var: Optional[str] = None, 
                   default: Optional[str] = None, required: bool = True) -> Optional[str]:
        """
        Get secret from Docker secrets file or environment variable
        
        Args:
            secret_name: Name of the secret file
            env_var: Environment variable name (fallback)
            default: Default value if not found
            required: Whether the secret is required
            
        Returns:
            Secret value or None if not found and not required
            
        Raises:
            ValueError: If required secret is not found
        """
        # Check cache first
        cache_key = f"{secret_name}:{env_var}"
        if cache_key in self._cache:
            return self._cache[cache_key]
        
        secret_value = None
        
        # Try to read from Docker secrets file first
        secret_file = self.secrets_path / secret_name
        if secret_file.exists() and secret_file.is_file():
            try:
                with open(secret_file, 'r', encoding='utf-8') as f:
                    secret_value = f.read().strip()
                logger.debug(f"Loaded secret '{secret_name}' from Docker secrets")
            except Exception as e:
                logger.warning(f"Failed to read secret file '{secret_file}': {e}")
        
        # Fallback to environment variable
        if secret_value is None and env_var:
            secret_value = os.environ.get(env_var)
            if secret_value:
                logger.debug(f"Loaded secret '{secret_name}' from environment variable '{env_var}'")
        
        # Try _FILE suffix environment variable (Docker secrets pattern)
        if secret_value is None and env_var:
            file_env_var = f"{env_var}_FILE"
            secret_file_path = os.environ.get(file_env_var)
            if secret_file_path and Path(secret_file_path).exists():
                try:
                    with open(secret_file_path, 'r', encoding='utf-8') as f:
                        secret_value = f.read().strip()
                    logger.debug(f"Loaded secret '{secret_name}' from file specified in '{file_env_var}'")
                except Exception as e:
                    logger.warning(f"Failed to read secret from file '{secret_file_path}': {e}")
        
        # Use default if still not found
        if secret_value is None:
            secret_value = default
            if secret_value:
                logger.debug(f"Using default value for secret '{secret_name}'")
        
        # Check if required
        if secret_value is None and required:
            raise ValueError(f"Required secret '{secret_name}' not found in Docker secrets, "
                           f"environment variable '{env_var}', or default value")
        
        # Cache the result
        if secret_value is not None:
            self._cache[cache_key] = secret_value
        
        return secret_value
    
    def get_database_url(self) -> str:
        """
        Build database URL from secrets
        
        Returns:
            PostgreSQL connection URL
        """
        host = os.environ.get('POSTGRES_HOST', 'localhost')
        port = os.environ.get('POSTGRES_PORT', '5432')
        database = os.environ.get('POSTGRES_DB', 'bluestream_db')
        user = os.environ.get('POSTGRES_USER', 'postgres')
        
        password = self.get_secret('postgres_password', 'POSTGRES_PASSWORD', required=True)
        
        return f"postgresql://{user}:{password}@{host}:{port}/{database}"
    
    def get_redis_url(self) -> str:
        """
        Build Redis URL from secrets
        
        Returns:
            Redis connection URL
        """
        host = os.environ.get('REDIS_HOST', 'localhost')
        port = os.environ.get('REDIS_PORT', '6379')
        db = os.environ.get('REDIS_DB', '0')
        
        password = self.get_secret('redis_password', 'REDIS_PASSWORD', required=False)
        
        if password:
            return f"redis://:{password}@{host}:{port}/{db}"
        else:
            return f"redis://{host}:{port}/{db}"
    
    def get_all_secrets(self) -> Dict[str, str]:
        """
        Get all available secrets for debugging (values masked)
        
        Returns:
            Dictionary of secret names and masked values
        """
        secrets = {}
        
        # Check Docker secrets directory
        if self.secrets_path.exists():
            for secret_file in self.secrets_path.glob('*'):
                if secret_file.is_file():
                    secrets[secret_file.name] = "***SECRET***"
        
        # Check environment variables ending with _FILE
        for key, value in os.environ.items():
            if key.endswith('_FILE') and value:
                secret_name = key[:-5].lower()  # Remove _FILE suffix
                secrets[secret_name] = "***SECRET***"
        
        return secrets
    
    def validate_secrets(self, required_secrets: list) -> Dict[str, bool]:
        """
        Validate that all required secrets are available
        
        Args:
            required_secrets: List of required secret names
            
        Returns:
            Dictionary of secret names and their availability status
        """
        validation_results = {}
        
        for secret_name in required_secrets:
            try:
                # Try to get each secret without caching for validation
                env_var = secret_name.upper()
                value = self.get_secret(secret_name, env_var, required=False)
                validation_results[secret_name] = value is not None
            except Exception as e:
                logger.error(f"Error validating secret '{secret_name}': {e}")
                validation_results[secret_name] = False
        
        return validation_results


# Global instance
secrets_manager = SecretsManager()


def get_secret(secret_name: str, env_var: Optional[str] = None, 
               default: Optional[str] = None, required: bool = True) -> Optional[str]:
    """
    Convenience function to get a secret using the global secrets manager
    
    Args:
        secret_name: Name of the secret file
        env_var: Environment variable name (fallback)
        default: Default value if not found
        required: Whether the secret is required
        
    Returns:
        Secret value or None if not found and not required
    """
    return secrets_manager.get_secret(secret_name, env_var, default, required)


def get_database_url() -> str:
    """Get database URL using the global secrets manager"""
    return secrets_manager.get_database_url()


def get_redis_url() -> str:
    """Get Redis URL using the global secrets manager"""
    return secrets_manager.get_redis_url()


def validate_required_secrets() -> bool:
    """
    Validate all required secrets for the application
    
    Returns:
        True if all required secrets are available
    """
    required_secrets = [
        'secret_key',
        'postgres_password',
        'telegram_bot_token'
    ]
    
    optional_secrets = [
        'payme_secret_key',
        'click_secret_key',
        'sendgrid_api_key',
        'staff_bot_token',
        'google_maps_api_key',
        'yandex_maps_api_key',
        'aws_secret_access_key',
        'stripe_secret_key',
        'encryption_key',
        'redis_password'
    ]
    
    # Validate required secrets
    required_results = secrets_manager.validate_secrets(required_secrets)
    missing_required = [name for name, available in required_results.items() if not available]
    
    if missing_required:
        logger.error(f"Missing required secrets: {missing_required}")
        return False
    
    # Warn about missing optional secrets
    optional_results = secrets_manager.validate_secrets(optional_secrets)
    missing_optional = [name for name, available in optional_results.items() if not available]
    
    if missing_optional:
        logger.warning(f"Missing optional secrets (some features may not work): {missing_optional}")
    
    logger.info("All required secrets are available")
    return True
