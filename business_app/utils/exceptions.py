"""
Custom exceptions for the Water Business Platform
"""
from typing import Dict, Any, Optional, List


class WaterBusinessException(Exception):
    """Base exception class for the water business platform"""
    
    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None, 
                 error_code: Optional[str] = None):
        self.message = message
        self.details = details or {}
        self.error_code = error_code
        super().__init__(self.message)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert exception to dictionary format"""
        result = {
            'error_type': self.__class__.__name__,
            'message': self.message,
            'details': self.details
        }
        if self.error_code:
            result['error_code'] = self.error_code
        return result


class ValidationError(WaterBusinessException):
    """Raised when data validation fails"""
    
    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None, 
                 validation_errors: Optional[List[str]] = None, error_code: Optional[str] = None):
        super().__init__(message, details, error_code)
        self.validation_errors = validation_errors or []


class NotFoundError(WaterBusinessException):
    """Raised when a requested resource is not found"""
    
    def __init__(self, message: str = "Resource not found", 
                 details: Optional[Dict[str, Any]] = None, 
                 resource_type: Optional[str] = None, 
                 resource_id: Optional[str] = None,
                 error_code: Optional[str] = None):
        super().__init__(message, details, error_code)
        self.resource_type = resource_type
        self.resource_id = resource_id


class UnauthorizedError(WaterBusinessException):
    """Raised when user is not authorized to perform an action"""
    
    def __init__(self, message: str = "Authentication required", 
                 details: Optional[Dict[str, Any]] = None,
                 error_code: Optional[str] = None):
        super().__init__(message, details, error_code)


class ForbiddenError(WaterBusinessException):
    """Raised when user lacks permission to access a resource"""
    
    def __init__(self, message: str = "Access forbidden", 
                 details: Optional[Dict[str, Any]] = None,
                 required_permission: Optional[str] = None,
                 error_code: Optional[str] = None):
        super().__init__(message, details, error_code)
        self.required_permission = required_permission


class ConflictError(WaterBusinessException):
    """Raised when there's a conflict with the current state"""
    
    def __init__(self, message: str = "Resource conflict", 
                 details: Optional[Dict[str, Any]] = None,
                 conflict_type: Optional[str] = None,
                 error_code: Optional[str] = None):
        super().__init__(message, details, error_code)
        self.conflict_type = conflict_type


class PaymentError(WaterBusinessException):
    """Raised when payment processing fails"""
    
    def __init__(self, message: str = "Payment processing failed", 
                 details: Optional[Dict[str, Any]] = None,
                 payment_gateway: Optional[str] = None,
                 gateway_error_code: Optional[str] = None,
                 error_code: Optional[str] = None):
        super().__init__(message, details, error_code)
        self.payment_gateway = payment_gateway
        self.gateway_error_code = gateway_error_code


class DeliveryError(WaterBusinessException):
    """Raised when delivery operation fails"""
    
    def __init__(self, message: str = "Delivery operation failed", 
                 details: Optional[Dict[str, Any]] = None,
                 delivery_stage: Optional[str] = None,
                 error_code: Optional[str] = None):
        super().__init__(message, details, error_code)
        self.delivery_stage = delivery_stage


class SubscriptionError(WaterBusinessException):
    """Raised when subscription operation fails"""
    
    def __init__(self, message: str = "Subscription operation failed", 
                 details: Optional[Dict[str, Any]] = None,
                 subscription_id: Optional[str] = None,
                 error_code: Optional[str] = None):
        super().__init__(message, details, error_code)
        self.subscription_id = subscription_id


class NotificationError(WaterBusinessException):
    """Raised when notification sending fails"""
    
    def __init__(self, message: str = "Notification sending failed", 
                 details: Optional[Dict[str, Any]] = None,
                 notification_type: Optional[str] = None,
                 provider: Optional[str] = None,
                 error_code: Optional[str] = None):
        super().__init__(message, details, error_code)
        self.notification_type = notification_type
        self.provider = provider


class FileStorageError(WaterBusinessException):
    """Raised when file storage operation fails"""
    
    def __init__(self, message: str = "File storage operation failed", 
                 details: Optional[Dict[str, Any]] = None,
                 operation: Optional[str] = None,
                 file_path: Optional[str] = None,
                 error_code: Optional[str] = None):
        super().__init__(message, details, error_code)
        self.operation = operation
        self.file_path = file_path


class ExternalServiceError(WaterBusinessException):
    """Raised when external service integration fails"""
    
    def __init__(self, message: str = "External service error", 
                 details: Optional[Dict[str, Any]] = None,
                 service_name: Optional[str] = None,
                 service_error_code: Optional[str] = None,
                 error_code: Optional[str] = None):
        super().__init__(message, details, error_code)
        self.service_name = service_name
        self.service_error_code = service_error_code


class RateLimitError(WaterBusinessException):
    """Raised when rate limit is exceeded"""
    
    def __init__(self, message: str = "Rate limit exceeded", 
                 details: Optional[Dict[str, Any]] = None,
                 retry_after: Optional[int] = None,
                 limit: Optional[int] = None,
                 error_code: Optional[str] = None):
        super().__init__(message, details, error_code)
        self.retry_after = retry_after
        self.limit = limit


class ConfigurationError(WaterBusinessException):
    """Raised when there's a configuration error"""
    
    def __init__(self, message: str = "Configuration error", 
                 details: Optional[Dict[str, Any]] = None,
                 config_key: Optional[str] = None,
                 error_code: Optional[str] = None):
        super().__init__(message, details, error_code)
        self.config_key = config_key


class BusinessLogicError(WaterBusinessException):
    """Raised when business logic validation fails"""
    
    def __init__(self, message: str = "Business logic validation failed", 
                 details: Optional[Dict[str, Any]] = None,
                 rule_name: Optional[str] = None,
                 error_code: Optional[str] = None):
        super().__init__(message, details, error_code)
        self.rule_name = rule_name


class InventoryError(WaterBusinessException):
    """Raised when inventory operations fail"""
    
    def __init__(self, message: str = "Inventory operation failed", 
                 details: Optional[Dict[str, Any]] = None,
                 product_id: Optional[str] = None,
                 requested_quantity: Optional[int] = None,
                 available_quantity: Optional[int] = None,
                 error_code: Optional[str] = None):
        super().__init__(message, details, error_code)
        self.product_id = product_id
        self.requested_quantity = requested_quantity
        self.available_quantity = available_quantity


class SecurityError(WaterBusinessException):
    """Raised when security validation fails"""
    
    def __init__(self, message: str = "Security validation failed", 
                 details: Optional[Dict[str, Any]] = None,
                 security_rule: Optional[str] = None,
                 error_code: Optional[str] = None):
        super().__init__(message, details, error_code)
        self.security_rule = security_rule