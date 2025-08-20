"""
Environment variable validation utilities
"""
import os
import re
import logging
from typing import List, Dict, Any, Optional, Tuple
from urllib.parse import urlparse


logger = logging.getLogger(__name__)


class EnvironmentValidator:
    """Validates environment variables for different environments"""
    
    def __init__(self, environment: str = None):
        self.environment = environment or os.environ.get('FLASK_ENV', 'development')
        self.errors = []
        self.warnings = []
    
    def validate_all(self) -> Tuple[bool, List[str], List[str]]:
        """Validate all environment variables and return status"""
        self.errors.clear()
        self.warnings.clear()
        
        # Core validations for all environments
        self._validate_secret_keys()
        self._validate_database_config()
        self._validate_redis_config()
        self._validate_basic_security()
        
        # Environment-specific validations
        if self.environment == 'production':
            self._validate_production_requirements()
        elif self.environment == 'staging':
            self._validate_staging_requirements()
        elif self.environment == 'development':
            self._validate_development_config()
        
        # Additional service validations
        self._validate_external_services()
        self._validate_feature_flags()
        
        is_valid = len(self.errors) == 0
        return is_valid, self.errors, self.warnings
    
    def _validate_secret_keys(self):
        """Validate secret keys"""
        secret_key = os.environ.get('SECRET_KEY')
        if not secret_key:
            self.errors.append("SECRET_KEY environment variable is required")
        elif len(secret_key) < 32:
            self.errors.append("SECRET_KEY must be at least 32 characters long")
        elif secret_key in ['dev-secret-key-change-in-production', 'your-secret-key-here']:
            self.errors.append("SECRET_KEY contains a placeholder value")
        
        jwt_secret = os.environ.get('JWT_SECRET_KEY')
        if not jwt_secret and self.environment in ['production', 'staging']:
            self.errors.append("JWT_SECRET_KEY is required for production/staging")
        elif jwt_secret and len(jwt_secret) < 32:
            self.errors.append("JWT_SECRET_KEY must be at least 32 characters long")
    
    def _validate_database_config(self):
        """Validate database configuration"""
        database_url = os.environ.get('DATABASE_URL')
        
        if database_url:
            # Validate DATABASE_URL format
            try:
                parsed = urlparse(database_url)
                if not all([parsed.scheme, parsed.netloc]):
                    self.errors.append("DATABASE_URL format is invalid")
                elif parsed.scheme not in ['postgresql', 'postgres', 'mysql', 'sqlite']:
                    self.warnings.append(f"Unsupported database scheme: {parsed.scheme}")
            except Exception:
                self.errors.append("DATABASE_URL cannot be parsed")
        else:
            # Check individual DB components
            db_password = os.environ.get('DB_PASSWORD')
            if not db_password and self.environment != 'testing':
                if self.environment == 'development':
                    self.warnings.append("DB_PASSWORD not set for development")
                else:
                    self.errors.append("DB_PASSWORD is required")
            
            db_host = os.environ.get('DB_HOST', 'localhost')
            if self.environment == 'production' and db_host in ['localhost', '127.0.0.1']:
                self.warnings.append("Using localhost for database in production")
    
    def _validate_redis_config(self):
        """Validate Redis configuration"""
        redis_url = os.environ.get('REDIS_URL')
        
        if redis_url:
            try:
                parsed = urlparse(redis_url)
                if not all([parsed.scheme, parsed.netloc]):
                    self.errors.append("REDIS_URL format is invalid")
                elif parsed.scheme not in ['redis', 'rediss']:
                    self.errors.append(f"Invalid Redis scheme: {parsed.scheme}")
                
                # Check for authentication in production
                if self.environment == 'production' and not parsed.password:
                    self.warnings.append("Redis URL does not include authentication for production")
                    
            except Exception:
                self.errors.append("REDIS_URL cannot be parsed")
        elif self.environment in ['production', 'staging']:
            self.errors.append("REDIS_URL is required for production/staging")
    
    def _validate_basic_security(self):
        """Validate basic security configurations"""
        # Check debug mode
        debug = os.environ.get('DEBUG', 'False').lower()
        if debug == 'true' and self.environment in ['production', 'staging']:
            self.errors.append("DEBUG mode must not be enabled in production/staging")
        
        # Check session security
        if self.environment in ['production', 'staging']:
            # These will be checked by the configuration classes
            pass
    
    def _validate_production_requirements(self):
        """Validate production-specific requirements"""
        required_vars = [
            'SENTRY_DSN',
            'SENDGRID_API_KEY',
            'AWS_ACCESS_KEY_ID',
            'AWS_SECRET_ACCESS_KEY',
            'AWS_S3_BUCKET'
        ]
        
        for var in required_vars:
            if not os.environ.get(var):
                self.errors.append(f"{var} is required for production")
        
        # Validate Sentry DSN format
        sentry_dsn = os.environ.get('SENTRY_DSN')
        if sentry_dsn and not sentry_dsn.startswith('https://'):
            self.errors.append("SENTRY_DSN must be a valid HTTPS URL")
        
        # Check payment gateway configuration
        payme_test = os.environ.get('PAYME_TEST_MODE', 'True').lower()
        click_test = os.environ.get('CLICK_TEST_MODE', 'True').lower()
        
        if payme_test == 'true':
            self.warnings.append("PAYME_TEST_MODE is enabled in production")
        if click_test == 'true':
            self.warnings.append("CLICK_TEST_MODE is enabled in production")
        
        # Check CORS origins
        cors_origins = os.environ.get('CORS_ORIGINS', '')
        if 'localhost' in cors_origins:
            self.warnings.append("CORS_ORIGINS includes localhost in production")
    
    def _validate_staging_requirements(self):
        """Validate staging-specific requirements"""
        recommended_vars = [
            'SENTRY_DSN',
            'SENDGRID_API_KEY',
            'AWS_ACCESS_KEY_ID',
            'AWS_SECRET_ACCESS_KEY'
        ]
        
        for var in recommended_vars:
            if not os.environ.get(var):
                self.warnings.append(f"{var} is recommended for staging")
    
    def _validate_development_config(self):
        """Validate development-specific configuration"""
        # Check if using production services in development
        sentry_dsn = os.environ.get('SENTRY_DSN')
        if sentry_dsn and 'prod' in sentry_dsn.lower():
            self.warnings.append("Using production Sentry DSN in development")
        
        # Check database
        db_name = os.environ.get('DB_NAME', 'bluestream_dev')
        if 'prod' in db_name.lower():
            self.errors.append("Database name suggests production database in development")
    
    def _validate_external_services(self):
        """Validate external service configurations"""
        # Email service
        sendgrid_key = os.environ.get('SENDGRID_API_KEY')
        if sendgrid_key:
            if not sendgrid_key.startswith('SG.'):
                self.warnings.append("SENDGRID_API_KEY format appears invalid")
        
        # SMS service
        twilio_sid = os.environ.get('TWILIO_ACCOUNT_SID')
        twilio_token = os.environ.get('TWILIO_AUTH_TOKEN')
        
        if twilio_sid and not twilio_token:
            self.warnings.append("TWILIO_ACCOUNT_SID set but TWILIO_AUTH_TOKEN missing")
        elif twilio_token and not twilio_sid:
            self.warnings.append("TWILIO_AUTH_TOKEN set but TWILIO_ACCOUNT_SID missing")
        
        # Payment gateways
        payme_merchant = os.environ.get('PAYME_MERCHANT_ID')
        payme_secret = os.environ.get('PAYME_SECRET_KEY')
        
        if payme_merchant and not payme_secret:
            self.warnings.append("PAYME_MERCHANT_ID set but PAYME_SECRET_KEY missing")
        
        # Maps API
        maps_provider = os.environ.get('MAPS_PROVIDER', 'google')
        if maps_provider == 'google' and not os.environ.get('GOOGLE_MAPS_API_KEY'):
            self.warnings.append("Using Google Maps but GOOGLE_MAPS_API_KEY not set")
    
    def _validate_feature_flags(self):
        """Validate feature flag configurations"""
        maintenance_mode = os.environ.get('MAINTENANCE_MODE', 'False').lower()
        if maintenance_mode == 'true':
            self.warnings.append("MAINTENANCE_MODE is enabled")
        
        allow_registration = os.environ.get('ALLOW_REGISTRATION', 'True').lower()
        if allow_registration == 'false' and self.environment == 'development':
            self.warnings.append("ALLOW_REGISTRATION is disabled in development")
    
    def validate_specific_var(self, var_name: str, value: str = None) -> Tuple[bool, List[str]]:
        """Validate a specific environment variable"""
        if value is None:
            value = os.environ.get(var_name)
        
        errors = []
        
        if var_name == 'SECRET_KEY':
            if not value:
                errors.append("SECRET_KEY is required")
            elif len(value) < 32:
                errors.append("SECRET_KEY must be at least 32 characters long")
        
        elif var_name == 'DATABASE_URL':
            if value:
                try:
                    parsed = urlparse(value)
                    if not all([parsed.scheme, parsed.netloc]):
                        errors.append("Invalid DATABASE_URL format")
                except Exception:
                    errors.append("Cannot parse DATABASE_URL")
        
        elif var_name == 'REDIS_URL':
            if value:
                try:
                    parsed = urlparse(value)
                    if parsed.scheme not in ['redis', 'rediss']:
                        errors.append("Invalid Redis URL scheme")
                except Exception:
                    errors.append("Cannot parse REDIS_URL")
        
        elif var_name == 'SENTRY_DSN':
            if value and not value.startswith('https://'):
                errors.append("SENTRY_DSN must be a valid HTTPS URL")
        
        elif var_name in ['PAYME_TEST_MODE', 'CLICK_TEST_MODE', 'DEBUG']:
            if value and value.lower() not in ['true', 'false']:
                errors.append(f"{var_name} must be 'true' or 'false'")
        
        return len(errors) == 0, errors
    
    def suggest_fixes(self) -> List[str]:
        """Suggest fixes for common configuration issues"""
        suggestions = []
        
        if not os.environ.get('SECRET_KEY'):
            suggestions.append("Generate SECRET_KEY: python -c \"import secrets; print(secrets.token_hex(32))\"")
        
        if not os.environ.get('JWT_SECRET_KEY'):
            suggestions.append("Generate JWT_SECRET_KEY: python -c \"import secrets; print(secrets.token_hex(32))\"")
        
        if self.environment == 'production':
            if not os.environ.get('SENTRY_DSN'):
                suggestions.append("Set up error tracking with Sentry: https://sentry.io")
            
            if not os.environ.get('SENDGRID_API_KEY'):
                suggestions.append("Configure email service with SendGrid: https://sendgrid.com")
        
        return suggestions


def validate_environment_startup(app) -> bool:
    """
    Validate environment on application startup
    Returns True if validation passes, False otherwise
    """
    env = os.environ.get('FLASK_ENV', 'development')
    validator = EnvironmentValidator(env)
    
    is_valid, errors, warnings = validator.validate_all()
    
    # Log results
    if warnings:
        for warning in warnings:
            app.logger.warning(f"Environment warning: {warning}")
    
    if errors:
        for error in errors:
            app.logger.error(f"Environment error: {error}")
        
        # In production, fail hard on configuration errors
        if env == 'production':
            app.logger.critical("Environment validation failed in production")
            return False
        else:
            app.logger.warning("Environment validation failed, but continuing in non-production")
    
    if is_valid:
        app.logger.info(f"Environment validation passed for {env}")
    
    return is_valid


def get_missing_vars(environment: str = None) -> List[str]:
    """Get list of missing required environment variables"""
    validator = EnvironmentValidator(environment)
    is_valid, errors, warnings = validator.validate_all()
    
    missing_vars = []
    for error in errors:
        if 'is required' in error:
            # Extract variable name from error message
            var_name = error.split()[0]
            missing_vars.append(var_name)
    
    return missing_vars


def check_security_issues(environment: str = None) -> Dict[str, List[str]]:
    """Check for security-related configuration issues"""
    env = environment or os.environ.get('FLASK_ENV', 'development')
    
    issues = {
        'critical': [],
        'high': [],
        'medium': []
    }
    
    # Critical issues
    if env == 'production':
        if os.environ.get('DEBUG', 'False').lower() == 'true':
            issues['critical'].append("DEBUG mode enabled in production")
        
        secret_key = os.environ.get('SECRET_KEY', '')
        if len(secret_key) < 32:
            issues['critical'].append("SECRET_KEY too short for production")
    
    # High severity issues
    secret_key = os.environ.get('SECRET_KEY', '')
    if 'dev-secret-key' in secret_key or 'change' in secret_key.lower():
        issues['high'].append("SECRET_KEY appears to be a placeholder")
    
    # Medium severity issues
    if env in ['production', 'staging']:
        cors_origins = os.environ.get('CORS_ORIGINS', '')
        if 'localhost' in cors_origins:
            issues['medium'].append("CORS origins include localhost in production/staging")
    
    return issues