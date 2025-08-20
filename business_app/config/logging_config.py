"""
Logging configuration for different environments
"""
import os
from typing import Dict, Any


class LoggingConfig:
    """Base logging configuration"""
    
    # Log levels by environment
    DEFAULT_LOG_LEVEL = 'INFO'
    PERFORMANCE_LOG_LEVEL = 'INFO'
    SECURITY_LOG_LEVEL = 'INFO' 
    BUSINESS_LOG_LEVEL = 'INFO'
    DATABASE_LOG_LEVEL = 'WARNING'  # Only log slow queries by default
    
    # Log file rotation settings
    LOG_MAX_BYTES = 10 * 1024 * 1024  # 10MB
    LOG_BACKUP_COUNT = 5
    
    # Performance thresholds
    SLOW_REQUEST_THRESHOLD = 2.0  # seconds
    SLOW_QUERY_THRESHOLD = 1.0    # seconds
    
    # Security settings
    LOG_SENSITIVE_DATA = False
    MASK_SENSITIVE_FIELDS = True
    
    # Monitoring settings
    ENABLE_PERFORMANCE_MONITORING = True
    ENABLE_SYSTEM_METRICS = True
    ENABLE_HEALTH_CHECKS = True
    
    # Retention settings (in days)
    LOG_RETENTION_DAYS = 30
    METRICS_RETENTION_DAYS = 7


class DevelopmentLoggingConfig(LoggingConfig):
    """Development logging configuration"""
    
    DEFAULT_LOG_LEVEL = 'DEBUG'
    PERFORMANCE_LOG_LEVEL = 'DEBUG'
    SECURITY_LOG_LEVEL = 'INFO'
    BUSINESS_LOG_LEVEL = 'DEBUG'
    DATABASE_LOG_LEVEL = 'INFO'
    
    # More verbose in development
    LOG_SENSITIVE_DATA = True  # Allow in development for debugging
    MASK_SENSITIVE_FIELDS = False
    
    # Lower thresholds for development
    SLOW_REQUEST_THRESHOLD = 1.0
    SLOW_QUERY_THRESHOLD = 0.5


class ProductionLoggingConfig(LoggingConfig):
    """Production logging configuration"""
    
    DEFAULT_LOG_LEVEL = 'WARNING'
    PERFORMANCE_LOG_LEVEL = 'INFO'
    SECURITY_LOG_LEVEL = 'INFO'
    BUSINESS_LOG_LEVEL = 'INFO'
    DATABASE_LOG_LEVEL = 'WARNING'
    
    # Strict security in production
    LOG_SENSITIVE_DATA = False
    MASK_SENSITIVE_FIELDS = True
    
    # Higher thresholds for production
    SLOW_REQUEST_THRESHOLD = 3.0
    SLOW_QUERY_THRESHOLD = 2.0
    
    # Longer retention in production
    LOG_RETENTION_DAYS = 90
    METRICS_RETENTION_DAYS = 30


class StagingLoggingConfig(LoggingConfig):
    """Staging logging configuration"""
    
    DEFAULT_LOG_LEVEL = 'INFO'
    PERFORMANCE_LOG_LEVEL = 'INFO'
    SECURITY_LOG_LEVEL = 'INFO'
    BUSINESS_LOG_LEVEL = 'INFO'
    DATABASE_LOG_LEVEL = 'INFO'
    
    # Moderate security in staging
    LOG_SENSITIVE_DATA = False
    MASK_SENSITIVE_FIELDS = True
    
    # Medium thresholds for staging
    SLOW_REQUEST_THRESHOLD = 2.0
    SLOW_QUERY_THRESHOLD = 1.0
    
    # Medium retention in staging
    LOG_RETENTION_DAYS = 60
    METRICS_RETENTION_DAYS = 14


class TestingLoggingConfig(LoggingConfig):
    """Testing logging configuration"""
    
    DEFAULT_LOG_LEVEL = 'ERROR'  # Minimize noise in tests
    PERFORMANCE_LOG_LEVEL = 'ERROR'
    SECURITY_LOG_LEVEL = 'ERROR'
    BUSINESS_LOG_LEVEL = 'ERROR'
    DATABASE_LOG_LEVEL = 'ERROR'
    
    # Disable most features in testing
    ENABLE_PERFORMANCE_MONITORING = False
    ENABLE_SYSTEM_METRICS = False
    ENABLE_HEALTH_CHECKS = False
    
    # Short retention for tests
    LOG_RETENTION_DAYS = 1
    METRICS_RETENTION_DAYS = 1


def get_logging_config() -> LoggingConfig:
    """Get logging configuration based on environment"""
    env = os.environ.get('FLASK_ENV', 'development').lower()
    
    if env == 'production':
        return ProductionLoggingConfig()
    elif env == 'staging':
        return StagingLoggingConfig()
    elif env == 'testing':
        return TestingLoggingConfig()
    else:
        return DevelopmentLoggingConfig()


def get_log_format_config() -> Dict[str, Any]:
    """Get log format configuration"""
    env = os.environ.get('FLASK_ENV', 'development').lower()
    
    if env in ['production', 'staging']:
        # Structured JSON logging for production
        return {
            'format_type': 'json',
            'include_request_id': True,
            'include_user_id': True,
            'include_performance_metrics': True,
            'timestamp_format': 'iso8601'
        }
    else:
        # Human-readable format for development
        return {
            'format_type': 'human',
            'include_request_id': True,
            'include_user_id': True,
            'include_performance_metrics': False,
            'timestamp_format': 'local'
        }


def get_monitoring_config() -> Dict[str, Any]:
    """Get monitoring configuration"""
    config = get_logging_config()
    
    return {
        'enable_performance_monitoring': config.ENABLE_PERFORMANCE_MONITORING,
        'enable_system_metrics': config.ENABLE_SYSTEM_METRICS,
        'enable_health_checks': config.ENABLE_HEALTH_CHECKS,
        'slow_request_threshold': config.SLOW_REQUEST_THRESHOLD,
        'slow_query_threshold': config.SLOW_QUERY_THRESHOLD,
        'metrics_retention_days': config.METRICS_RETENTION_DAYS,
        'log_retention_days': config.LOG_RETENTION_DAYS
    }


# Export configurations
__all__ = [
    'LoggingConfig',
    'DevelopmentLoggingConfig', 
    'ProductionLoggingConfig',
    'StagingLoggingConfig',
    'TestingLoggingConfig',
    'get_logging_config',
    'get_log_format_config',
    'get_monitoring_config'
]