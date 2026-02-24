"""
Standardized error handling system for the Water Business Platform
"""
import logging
import traceback
from functools import wraps
from typing import Dict, Any, Optional, Tuple, Union
from flask import current_app, request, jsonify, g
from flask_jwt_extended import get_jwt_identity, verify_jwt_in_request
from flask_jwt_extended.exceptions import (
    CSRFError,
    FreshTokenRequired,
    InvalidHeaderError,
    InvalidQueryParamError,
    JWTDecodeError,
    NoAuthorizationError,
    RevokedTokenError,
    UserClaimsVerificationError,
    UserLookupError,
    WrongTokenError,
)
from jwt.exceptions import DecodeError
from datetime import datetime, timezone

from .exceptions import (
    WaterBusinessException, ValidationError, NotFoundError, UnauthorizedError,
    ForbiddenError, ConflictError, PaymentError, DeliveryError,
    SubscriptionError, NotificationError, FileStorageError,
    ExternalServiceError, RateLimitError, ConfigurationError
)
from .error_messages import ErrorCode, get_error_message
from .helpers import get_current_language

logger = logging.getLogger(__name__)


class ErrorResponse:
    """Standardized error response builder"""
    
    @staticmethod
    def build_error_response(
        error_type: str,
        message: str,
        details: Optional[Dict[str, Any]] = None,
        request_id: Optional[str] = None,
        status_code: int = 500
    ) -> Tuple[Dict[str, Any], int]:
        """Build standardized error response"""
        
        response = {
            'error': error_type,
            'message': message,
            'status_code': status_code,
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'path': request.path if request else None,
            'method': request.method if request else None,
        }
        
        if details:
            response['details'] = details
            
        if request_id:
            response['request_id'] = request_id
            
        # Add trace ID if available
        if hasattr(g, 'trace_id'):
            response['trace_id'] = g.trace_id
            
        return response, status_code


class ExceptionMapper:
    """Maps exceptions to HTTP status codes and error types"""
    
    EXCEPTION_MAPPING = {
        ValidationError: (400, 'VALIDATION_ERROR'),
        NotFoundError: (404, 'NOT_FOUND'),
        UnauthorizedError: (401, 'UNAUTHORIZED'),
        ForbiddenError: (403, 'FORBIDDEN'),
        ConflictError: (409, 'CONFLICT'),
        PaymentError: (402, 'PAYMENT_REQUIRED'),
        DeliveryError: (422, 'DELIVERY_ERROR'),
        SubscriptionError: (402, 'SUBSCRIPTION_ERROR'),
        NotificationError: (500, 'NOTIFICATION_ERROR'),
        FileStorageError: (500, 'FILE_STORAGE_ERROR'),
        ExternalServiceError: (503, 'SERVICE_UNAVAILABLE'),
        RateLimitError: (429, 'RATE_LIMIT_EXCEEDED'),
        ConfigurationError: (500, 'CONFIGURATION_ERROR'),

        # Flask-JWT-Extended exceptions (should never be 500)
        NoAuthorizationError: (401, 'UNAUTHORIZED'),
        FreshTokenRequired: (401, 'UNAUTHORIZED'),
        RevokedTokenError: (401, 'UNAUTHORIZED'),
        UserLookupError: (401, 'UNAUTHORIZED'),
        InvalidHeaderError: (401, 'UNAUTHORIZED'),
        InvalidQueryParamError: (401, 'UNAUTHORIZED'),
        JWTDecodeError: (401, 'UNAUTHORIZED'),
        WrongTokenError: (401, 'UNAUTHORIZED'),
        UserClaimsVerificationError: (403, 'FORBIDDEN'),
        CSRFError: (401, 'UNAUTHORIZED'),
        DecodeError: (401, 'UNAUTHORIZED'),
        
        # Standard Python exceptions
        ValueError: (400, 'INVALID_VALUE'),
        TypeError: (400, 'TYPE_ERROR'),
        KeyError: (400, 'MISSING_KEY'),
        AttributeError: (500, 'ATTRIBUTE_ERROR'),
        ImportError: (500, 'IMPORT_ERROR'),
        ConnectionError: (503, 'CONNECTION_ERROR'),
        TimeoutError: (504, 'TIMEOUT'),
    }
    
    @classmethod
    def get_error_info(cls, exception: Exception) -> Tuple[int, str, str]:
        """Get status code, error type, and message for an exception"""
        
        # Check for custom business exceptions first
        if isinstance(exception, WaterBusinessException):
            status_code, error_type = cls.EXCEPTION_MAPPING.get(
                type(exception), (500, 'BUSINESS_ERROR')
            )
            return status_code, error_type, exception.message
        
        # Check for mapped standard exceptions
        for exc_type, (status_code, error_type) in cls.EXCEPTION_MAPPING.items():
            if isinstance(exception, exc_type):
                return status_code, error_type, str(exception)
        
        # Default for unmapped exceptions
        return 500, 'INTERNAL_ERROR', 'An unexpected error occurred'


def log_exception(
    exception: Exception,
    endpoint: str = None,
    user_id: str = None,
    request_data: Dict[str, Any] = None,
    level: str = 'error'
):
    """Log exception with comprehensive context"""
    
    log_data = {
        'exception_type': type(exception).__name__,
        'exception_message': str(exception),
        'endpoint': endpoint or (request.endpoint if request else None),
        'method': request.method if request else None,
        'path': request.path if request else None,
        'user_id': user_id,
        'ip_address': request.remote_addr if request else None,
        'user_agent': request.headers.get('User-Agent') if request else None,
        'timestamp': datetime.now(timezone.utc).isoformat(),
    }
    
    if request_data:
        # Sanitize sensitive data
        sanitized_data = sanitize_request_data(request_data)
        log_data['request_data'] = sanitized_data
    
    if hasattr(g, 'trace_id'):
        log_data['trace_id'] = g.trace_id
    
    # Log stack trace for internal errors
    if not isinstance(exception, WaterBusinessException):
        log_data['stack_trace'] = traceback.format_exc()
    
    log_message = f"Exception in {log_data['endpoint']}: {log_data['exception_message']}"
    
    if level == 'warning':
        logger.warning(log_message, extra=log_data)
    elif level == 'critical':
        logger.critical(log_message, extra=log_data)
    else:
        logger.error(log_message, extra=log_data)


def sanitize_request_data(data: Dict[str, Any]) -> Dict[str, Any]:
    """Remove sensitive information from request data before logging"""
    
    SENSITIVE_KEYS = {
        'password', 'token', 'secret', 'key', 'authorization',
        'credit_card', 'card_number', 'cvv', 'pin', 'ssn',
        'passport', 'api_key', 'access_token', 'refresh_token'
    }
    
    if not isinstance(data, dict):
        return data
    
    sanitized = {}
    for key, value in data.items():
        key_lower = key.lower()
        
        # Check if key contains sensitive information
        if any(sensitive in key_lower for sensitive in SENSITIVE_KEYS):
            sanitized[key] = '[REDACTED]'
        elif isinstance(value, dict):
            sanitized[key] = sanitize_request_data(value)
        elif isinstance(value, list):
            sanitized[key] = [
                sanitize_request_data(item) if isinstance(item, dict) else item
                for item in value
            ]
        else:
            sanitized[key] = value
    
    return sanitized


def handle_api_exception(f):
    """
    Comprehensive API exception handler decorator
    
    This decorator should be applied to all API endpoints to ensure
    consistent error handling and logging.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        try:
            # Generate trace ID for request tracking
            if not hasattr(g, 'trace_id'):
                import uuid
                g.trace_id = str(uuid.uuid4())
            
            return f(*args, **kwargs)
            
        except Exception as e:
            # Get user ID if available
            user_id = None
            try:
                verify_jwt_in_request(optional=True)
                user_id = get_jwt_identity()
            except:
                pass
            
            # Get request data for logging
            request_data = None
            try:
                if request.is_json:
                    request_data = request.get_json()
                elif request.form:
                    request_data = request.form.to_dict()
            except:
                pass
            
            # Map exception to response info
            status_code, error_type, message = ExceptionMapper.get_error_info(e)
            
            # Determine log level
            log_level = 'error'
            if isinstance(e, (ValidationError, NotFoundError, UnauthorizedError, ForbiddenError)):
                log_level = 'warning'
            elif status_code >= 500:
                log_level = 'critical'
            
            # Log the exception
            log_exception(
                exception=e,
                endpoint=f.__name__,
                user_id=user_id,
                request_data=request_data,
                level=log_level
            )
            
            # Build error details
            details = {}
            
            # Add exception-specific details
            if isinstance(e, WaterBusinessException):
                details.update(e.details)
            
            # Add rate limit info
            if isinstance(e, RateLimitError):
                retry_after = getattr(e, 'retry_after', None)
                if retry_after:
                    details['retry_after'] = retry_after
            
            # Add validation errors
            if hasattr(e, 'validation_errors'):
                details['validation_errors'] = e.validation_errors
            
            # Build and return error response
            return ErrorResponse.build_error_response(
                error_type=error_type,
                message=message,
                details=details if details else None,
                request_id=getattr(g, 'trace_id', None),
                status_code=status_code
            )
    
    return decorated_function


def handle_database_exceptions(f):
    """
    Handle database-specific exceptions
    
    Should be used with functions that perform database operations
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except Exception as e:
            from sqlalchemy.exc import (
                IntegrityError, DataError, OperationalError, 
                InvalidRequestError, DatabaseError
            )
            from business_app import db
            
            # Rollback transaction on database errors
            try:
                db.session.rollback()
            except:
                pass
            
            # Map database exceptions
            if isinstance(e, IntegrityError):
                if 'unique constraint' in str(e).lower():
                    raise ConflictError("Resource already exists", 
                                      details={'database_error': 'UNIQUE_CONSTRAINT'})
                elif 'foreign key constraint' in str(e).lower():
                    raise ValidationError("Invalid reference to related resource",
                                        details={'database_error': 'FOREIGN_KEY_CONSTRAINT'})
                else:
                    raise ValidationError("Data integrity violation",
                                        details={'database_error': 'INTEGRITY_ERROR'})
            
            elif isinstance(e, DataError):
                raise ValidationError("Invalid data format",
                                    details={'database_error': 'DATA_ERROR'})
            
            elif isinstance(e, OperationalError):
                raise ExternalServiceError("Database connection error",
                                         details={'database_error': 'CONNECTION_ERROR'})
            
            elif isinstance(e, InvalidRequestError):
                raise ConfigurationError("Database configuration error",
                                       details={'database_error': 'INVALID_REQUEST'})
            
            else:
                # Re-raise other exceptions to be handled by main handler
                raise e
    
    return decorated_function


def handle_external_service_exceptions(service_name: str):
    """
    Handle exceptions from external service calls
    
    Args:
        service_name: Name of the external service (e.g., 'payment_gateway', 'sms_service')
    """
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            try:
                return f(*args, **kwargs)
            except Exception as e:
                import requests
                
                # Map common external service exceptions
                if isinstance(e, requests.exceptions.Timeout):
                    raise ExternalServiceError(
                        f"{service_name} service timeout",
                        details={'service': service_name, 'error_type': 'TIMEOUT'}
                    )
                
                elif isinstance(e, requests.exceptions.ConnectionError):
                    raise ExternalServiceError(
                        f"{service_name} service unavailable",
                        details={'service': service_name, 'error_type': 'CONNECTION_ERROR'}
                    )
                
                elif isinstance(e, requests.exceptions.HTTPError):
                    status_code = getattr(e.response, 'status_code', None)
                    if status_code == 400:
                        raise ValidationError(
                            f"Invalid request to {service_name}",
                            details={'service': service_name, 'status_code': status_code}
                        )
                    elif status_code == 401:
                        raise ConfigurationError(
                            f"Authentication failed with {service_name}",
                            details={'service': service_name, 'status_code': status_code}
                        )
                    elif status_code == 403:
                        raise ConfigurationError(
                            f"Access denied by {service_name}",
                            details={'service': service_name, 'status_code': status_code}
                        )
                    else:
                        raise ExternalServiceError(
                            f"{service_name} service error",
                            details={'service': service_name, 'status_code': status_code}
                        )
                
                else:
                    # Re-raise other exceptions
                    raise e
        
        return decorated_function
    return decorator


def validate_and_handle_errors(validation_func):
    """
    Decorator that combines validation and error handling
    
    Args:
        validation_func: Function that validates input and returns errors list
    """
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            # Validate input
            data = None
            if request.is_json:
                data = request.get_json()
            elif request.form:
                data = request.form.to_dict()
            
            if data:
                errors = validation_func(data)
                if errors:
                    raise ValidationError(
                        "Input validation failed",
                        details={'validation_errors': errors}
                    )
                g.validated_data = data
            
            return f(*args, **kwargs)
        return decorated_function
    return decorator


def handle_file_operation_exceptions(f):
    """Handle file operation specific exceptions"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except PermissionError:
            raise FileStorageError("Permission denied for file operation")
        except FileNotFoundError:
            raise NotFoundError("File not found")
        except OSError as e:
            if e.errno == 28:  # No space left on device
                raise FileStorageError("Insufficient storage space")
            else:
                raise FileStorageError(f"File system error: {str(e)}")
        except Exception as e:
            # Re-raise for main handler
            raise e
    
    return decorated_function


# Global error handlers for Flask app
def register_error_handlers(app):
    """Register global error handlers with Flask app"""
    
    @app.errorhandler(WaterBusinessException)
    def handle_business_exception(error):
        status_code, error_type, message = ExceptionMapper.get_error_info(error)
        return ErrorResponse.build_error_response(
            error_type=error_type,
            message=message,
            details=error.details,
            status_code=status_code
        )
    
    @app.errorhandler(ValidationError)
    def handle_validation_error(error):
        return ErrorResponse.build_error_response(
            error_type='VALIDATION_ERROR',
            message=error.message,
            details=error.details,
            status_code=400
        )
    
    @app.errorhandler(NotFoundError)
    def handle_not_found_error(error):
        return ErrorResponse.build_error_response(
            error_type='NOT_FOUND',
            message=error.message,
            details=error.details,
            status_code=404
        )
    
    @app.errorhandler(UnauthorizedError)
    def handle_unauthorized_error(error):
        return ErrorResponse.build_error_response(
            error_type='UNAUTHORIZED',
            message=error.message,
            details=error.details,
            status_code=401
        )
    
    @app.errorhandler(ForbiddenError)
    def handle_forbidden_error(error):
        return ErrorResponse.build_error_response(
            error_type='FORBIDDEN',
            message=error.message,
            details=error.details,
            status_code=403
        )
    
    @app.errorhandler(429)
    def handle_rate_limit_error(error):
        language = get_current_language()
        return ErrorResponse.build_error_response(
            error_type='RATE_LIMIT_EXCEEDED',
            message=get_error_message(ErrorCode.RATE_LIMIT_EXCEEDED, language=language),
            details={'retry_after': getattr(error, 'retry_after', 60)},
            status_code=429
        )

    @app.errorhandler(500)
    def handle_internal_error(error):
        from business_app import db
        try:
            db.session.rollback()
        except:
            pass

        language = get_current_language()
        return ErrorResponse.build_error_response(
            error_type='INTERNAL_ERROR',
            message=get_error_message(ErrorCode.INTERNAL_ERROR, language=language),
            status_code=500
        )

    @app.errorhandler(404)
    def handle_not_found(error):
        if request.path.startswith('/api/'):
            language = get_current_language()
            return ErrorResponse.build_error_response(
                error_type='ENDPOINT_NOT_FOUND',
                message=get_error_message(ErrorCode.RESOURCE_NOT_FOUND, language=language),
                status_code=404
            )
        # Return 404 page for web routes
        return "Page not found", 404



# Helper function to create error responses with translated messages
def create_translated_error_response(
    error_code: ErrorCode,
    details: Optional[Dict[str, Any]] = None,
    **params: Any
) -> Tuple[Dict[str, Any], int]:
    """
    Create error response with translated message based on ErrorCode.

    Args:
        error_code: ErrorCode enum value
        details: Additional error details
        **params: Parameters for message formatting

    Returns:
        Tuple of (response_dict, status_code)

    Example:
        >>> from business_app.utils.error_messages import ErrorCode
        >>> create_translated_error_response(ErrorCode.USER_NOT_FOUND)
        ({'error': 'NOT_FOUND', 'message': 'User not found', ...}, 404)
    """
    from .error_messages import get_error_status_code

    language = get_current_language()
    message = get_error_message(error_code, language=language, **params)
    status_code = get_error_status_code(error_code)

    # Map error code to error type string
    error_type = error_code.value

    return ErrorResponse.build_error_response(
        error_type=error_type,
        message=message,
        details=details,
        status_code=status_code
    )
