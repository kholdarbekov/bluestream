"""
Legacy configuration file - DEPRECATED
Use business_app.config package instead

This file is maintained for backward compatibility.
New code should import from business_app.config
"""

# Re-export everything from the new config package for backward compatibility
from .config import (
    BaseConfig,
    DevelopmentConfig,
    StagingConfig, 
    ProductionConfig,
    TestingConfig,
    config,
    get_config,
    validate_environment,
    get_environment_info
)

# Legacy aliases for backward compatibility
Config = BaseConfig

# Maintain the old interface
def get_config() -> BaseConfig:
    """Get configuration based on environment - legacy function"""
    from .config import get_config as _get_config
    return _get_config()