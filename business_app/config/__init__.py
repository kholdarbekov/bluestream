"""
Configuration package for BlueStream Water Platform
"""

import os
from .base import BaseConfig
from .development import DevelopmentConfig
from .staging import StagingConfig
from .production import ProductionConfig
from .testing import TestingConfig


# Configuration mapping
config = {
    "development": DevelopmentConfig,
    "dev": DevelopmentConfig,
    "staging": StagingConfig,
    "stage": StagingConfig,
    "production": ProductionConfig,
    "prod": ProductionConfig,
    "testing": TestingConfig,
    "test": TestingConfig,
    "default": DevelopmentConfig,
}


def get_config() -> BaseConfig:
    """
    Get configuration based on environment variables

    Checks FLASK_ENV, APP_ENV, and ENVIRONMENT variables in that order
    Falls back to 'development' if none are set
    """
    # Check multiple possible environment variable names
    env_vars = ["FLASK_ENV", "APP_ENV", "ENVIRONMENT"]
    env = None

    for var in env_vars:
        env = os.environ.get(var)
        if env:
            break

    if not env:
        env = "development"

    # Normalize environment name
    env = env.lower().strip()

    # Get configuration class
    config_class = config.get(env, config["default"])

    return config_class


def validate_environment():
    """
    Validate that the current environment is properly configured
    """
    env = os.environ.get("FLASK_ENV", "development").lower()
    config_class = get_config()

    try:
        # Validate basic configuration
        config_class.validate_required_env_vars()
        config_class.validate_secret_key()
        config_class.validate_debug_mode()

        # Validate environment-specific settings
        if hasattr(config_class, "validate_production_settings") and env == "production":
            config_class.validate_production_settings()
        elif hasattr(config_class, "validate_staging_settings") and env == "staging":
            config_class.validate_staging_settings()

        return True, "Environment configuration is valid"

    except ValueError as e:
        return False, f"Environment configuration error: {str(e)}"
    except Exception as e:
        return False, f"Unexpected configuration error: {str(e)}"


def get_environment_info():
    """
    Get information about the current environment configuration
    """
    env = os.environ.get("FLASK_ENV", "development")
    config_class = get_config()

    # Create an instance to access properties
    try:
        config_instance = config_class()
        database_uri = (
            config_instance.SQLALCHEMY_DATABASE_URI
            if hasattr(config_instance, "SQLALCHEMY_DATABASE_URI")
            else "Not configured"
        )
        redis_url = config_instance.REDIS_URL if hasattr(config_instance, "REDIS_URL") else "Not configured"
    except Exception:
        database_uri = "Configuration error"
        redis_url = "Configuration error"

    return {
        "environment": env,
        "config_class": config_class.__name__,
        "debug": getattr(config_class, "DEBUG", False),
        "testing": getattr(config_class, "TESTING", False),
        "database_uri": database_uri,
        "redis_url": redis_url,
        "secret_key_set": bool(os.environ.get("SECRET_KEY")),
        "jwt_secret_set": bool(os.environ.get("JWT_SECRET_KEY")),
    }


__all__ = [
    "BaseConfig",
    "DevelopmentConfig",
    "StagingConfig",
    "ProductionConfig",
    "TestingConfig",
    "config",
    "get_config",
    "validate_environment",
    "get_environment_info",
]
