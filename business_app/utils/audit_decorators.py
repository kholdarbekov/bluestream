"""
Audit decorators that integrate with the RBAC system for comprehensive logging.
These decorators provide automatic audit logging for API endpoints and sensitive operations.
"""

import time
from functools import wraps

from flask import request, g, current_app
from flask_jwt_extended import get_jwt_identity

from .audit_logger import (
    audit_logger,
    AuditEventType,
    AuditSeverity,
    audit_login_success,
    audit_login_failure,
    audit_permission_denied,
)
from .exceptions import UnauthorizedError, ForbiddenError


def audit_api_call(
    event_type: AuditEventType = None,
    resource_type: str = None,
    severity: AuditSeverity = AuditSeverity.MEDIUM,
    track_changes: bool = False,
):
    """
    Decorator to audit API endpoint calls.

    Args:
        event_type: Type of audit event (auto-detected if not provided)
        resource_type: Type of resource being operated on
        severity: Severity level of the operation
        track_changes: Whether to capture request/response data
    """

    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            start_time = time.time()
            request_data = None
            response_data = None

            # Capture request data if tracking changes
            if track_changes and request.is_json:
                request_data = request.get_json()

            # Auto-detect event type based on HTTP method and endpoint
            actual_event_type = event_type
            if not actual_event_type:
                method = request.method.upper()
                endpoint = request.endpoint or f.__name__

                if method == "POST":
                    if "user" in endpoint:
                        actual_event_type = AuditEventType.USER_CREATED
                    elif "order" in endpoint:
                        actual_event_type = AuditEventType.ORDER_CREATED
                    elif "product" in endpoint:
                        actual_event_type = AuditEventType.PRODUCT_CREATED
                    else:
                        actual_event_type = AuditEventType.SYSTEM_MAINTENANCE
                elif method == "PUT" or method == "PATCH":
                    if "user" in endpoint:
                        actual_event_type = AuditEventType.USER_UPDATED
                    elif "order" in endpoint:
                        actual_event_type = AuditEventType.ORDER_UPDATED
                    elif "product" in endpoint:
                        actual_event_type = AuditEventType.PRODUCT_UPDATED
                    else:
                        actual_event_type = AuditEventType.SYSTEM_MAINTENANCE
                elif method == "DELETE":
                    if "user" in endpoint:
                        actual_event_type = AuditEventType.USER_DELETED
                    elif "order" in endpoint:
                        actual_event_type = AuditEventType.ORDER_UPDATED  # Usually cancel, not delete
                    elif "product" in endpoint:
                        actual_event_type = AuditEventType.PRODUCT_DELETED
                    else:
                        actual_event_type = AuditEventType.SYSTEM_MAINTENANCE
                else:
                    actual_event_type = AuditEventType.SENSITIVE_DATA_ACCESS

            try:
                # Execute the function
                result = f(*args, **kwargs)

                # Capture response data if tracking changes
                if track_changes and hasattr(result, "get_json"):
                    response_data = result.get_json()
                elif track_changes and isinstance(result, dict):
                    response_data = result

                # Extract resource ID from result or args
                resource_id = None
                if hasattr(result, "id"):
                    resource_id = result.id
                elif isinstance(result, dict) and "id" in result:
                    resource_id = result["id"]
                elif "id" in kwargs:
                    resource_id = kwargs["id"]
                elif args and hasattr(args[0], "id"):
                    resource_id = args[0].id

                # Log successful operation
                duration_ms = int((time.time() - start_time) * 1000)
                additional_data = {}

                if track_changes:
                    if request_data:
                        additional_data["request_data"] = request_data
                    if response_data:
                        additional_data["response_data"] = response_data

                audit_logger.log_event(
                    event_type=actual_event_type,
                    action=f"{request.method} {request.endpoint or f.__name__}",
                    severity=severity,
                    resource_type=resource_type or "api",
                    resource_id=str(resource_id) if resource_id else None,
                    description=f"API call to {request.endpoint}",
                    success=True,
                    duration_ms=duration_ms,
                    additional_data=additional_data if additional_data else None,
                )

                return result

            except Exception as e:
                # Log failed operation
                duration_ms = int((time.time() - start_time) * 1000)

                audit_logger.log_event(
                    event_type=actual_event_type,
                    action=f"{request.method} {request.endpoint or f.__name__}",
                    severity=AuditSeverity.HIGH,
                    resource_type=resource_type or "api",
                    description=f"Failed API call to {request.endpoint}",
                    success=False,
                    error_message=str(e),
                    duration_ms=duration_ms,
                    additional_data={"request_data": request_data} if track_changes and request_data else None,
                )

                raise

        return decorated_function

    return decorator


def audit_user_action(
    action_name: str, resource_type: str = "user_action", severity: AuditSeverity = AuditSeverity.MEDIUM
):
    """
    Decorator to audit specific user actions.

    Args:
        action_name: Name of the action being performed
        resource_type: Type of resource involved
        severity: Severity level of the action
    """

    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            try:
                # Log action start
                start_time = time.time()

                result = f(*args, **kwargs)

                # Log successful action
                duration_ms = int((time.time() - start_time) * 1000)
                audit_logger.log_event(
                    event_type=AuditEventType.SENSITIVE_DATA_ACCESS,
                    action=action_name,
                    severity=severity,
                    resource_type=resource_type,
                    success=True,
                    duration_ms=duration_ms,
                    description=f"User action: {action_name}",
                )

                return result

            except Exception as e:
                # Log failed action
                duration_ms = int((time.time() - start_time) * 1000)
                audit_logger.log_event(
                    event_type=AuditEventType.SENSITIVE_DATA_ACCESS,
                    action=action_name,
                    severity=AuditSeverity.HIGH,
                    resource_type=resource_type,
                    success=False,
                    error_message=str(e),
                    duration_ms=duration_ms,
                    description=f"Failed user action: {action_name}",
                )
                raise

        return decorated_function

    return decorator


def audit_data_modification(resource_type: str, capture_changes: bool = True):
    """
    Decorator to audit data modification operations with before/after tracking.

    Args:
        resource_type: Type of resource being modified
        capture_changes: Whether to capture old and new values
    """

    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            old_values = None
            resource_id = None

            # Try to capture old values before modification
            if capture_changes:
                # Look for resource ID in arguments
                if "id" in kwargs:
                    resource_id = kwargs["id"]
                elif len(args) > 0 and hasattr(args[0], "id"):
                    resource_id = args[0].id

                # Try to get current state before modification
                if resource_id and hasattr(args[0], "to_dict"):
                    try:
                        old_values = args[0].to_dict()
                    except Exception:
                        old_values = {"note": "Could not capture old values"}

            start_time = time.time()

            try:
                result = f(*args, **kwargs)

                # Capture new values after modification
                new_values = None
                if capture_changes:
                    if hasattr(result, "to_dict"):
                        new_values = result.to_dict()
                    elif isinstance(result, dict):
                        new_values = result

                # Determine event type based on function name
                function_name = f.__name__.lower()
                if "create" in function_name:
                    if "user" in resource_type:
                        event_type = AuditEventType.USER_CREATED
                    elif "order" in resource_type:
                        event_type = AuditEventType.ORDER_CREATED
                    elif "product" in resource_type:
                        event_type = AuditEventType.PRODUCT_CREATED
                    else:
                        event_type = AuditEventType.SYSTEM_MAINTENANCE
                elif "update" in function_name or "modify" in function_name:
                    if "user" in resource_type:
                        event_type = AuditEventType.USER_UPDATED
                    elif "order" in resource_type:
                        event_type = AuditEventType.ORDER_UPDATED
                    elif "product" in resource_type:
                        event_type = AuditEventType.PRODUCT_UPDATED
                    else:
                        event_type = AuditEventType.SYSTEM_MAINTENANCE
                elif "delete" in function_name:
                    if "user" in resource_type:
                        event_type = AuditEventType.USER_DELETED
                    elif "product" in resource_type:
                        event_type = AuditEventType.PRODUCT_DELETED
                    else:
                        event_type = AuditEventType.SYSTEM_MAINTENANCE
                else:
                    event_type = AuditEventType.SYSTEM_MAINTENANCE

                # Log successful modification
                duration_ms = int((time.time() - start_time) * 1000)
                audit_logger.log_event(
                    event_type=event_type,
                    action=f.__name__,
                    severity=AuditSeverity.MEDIUM,
                    resource_type=resource_type,
                    resource_id=str(resource_id) if resource_id else None,
                    old_values=old_values,
                    new_values=new_values,
                    success=True,
                    duration_ms=duration_ms,
                    description=f"Data modification: {f.__name__}",
                )

                return result

            except Exception as e:
                # Log failed modification
                duration_ms = int((time.time() - start_time) * 1000)
                audit_logger.log_event(
                    event_type=AuditEventType.SYSTEM_MAINTENANCE,
                    action=f.__name__,
                    severity=AuditSeverity.HIGH,
                    resource_type=resource_type,
                    resource_id=str(resource_id) if resource_id else None,
                    success=False,
                    error_message=str(e),
                    duration_ms=duration_ms,
                    description=f"Failed data modification: {f.__name__}",
                )
                raise

        return decorated_function

    return decorator


def audit_security_event(event_description: str, severity: AuditSeverity = AuditSeverity.HIGH):
    """
    Decorator to audit security-related events.

    Args:
        event_description: Description of the security event
        severity: Severity level of the security event
    """

    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            start_time = time.time()

            try:
                result = f(*args, **kwargs)

                # Log successful security operation
                duration_ms = int((time.time() - start_time) * 1000)
                audit_logger.log_security_event(
                    event_type=AuditEventType.SENSITIVE_DATA_ACCESS,
                    description=event_description,
                    severity=severity,
                    additional_data={"function": f.__name__, "duration_ms": duration_ms, "success": True},
                )

                return result

            except Exception as e:
                # Log failed security operation
                duration_ms = int((time.time() - start_time) * 1000)
                audit_logger.log_security_event(
                    event_type=AuditEventType.SUSPICIOUS_ACTIVITY,
                    description=f"Failed security operation: {event_description}",
                    severity=AuditSeverity.CRITICAL,
                    additional_data={
                        "function": f.__name__,
                        "duration_ms": duration_ms,
                        "error": str(e),
                        "success": False,
                    },
                )
                raise

        return decorated_function

    return decorator


def audit_admin_action(action_name: str, requires_approval: bool = False):
    """
    Decorator to audit administrative actions with enhanced logging.

    Args:
        action_name: Name of the administrative action
        requires_approval: Whether this action requires approval workflow
    """

    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            start_time = time.time()

            # Enhanced logging for admin actions
            user_id = (
                get_jwt_identity() if hasattr(g, "current_user_id") or request.headers.get("Authorization") else None
            )  # noqa: E501

            try:
                current_app.logger.warning(
                    f"ADMIN ACTION: {action_name} initiated by user {user_id} "
                    f"from {request.remote_addr} at {start_time}"
                )

                result = f(*args, **kwargs)

                # Log successful admin action
                duration_ms = int((time.time() - start_time) * 1000)
                audit_logger.log_event(
                    event_type=AuditEventType.SYSTEM_MAINTENANCE,
                    action=action_name,
                    severity=AuditSeverity.HIGH,
                    resource_type="admin_action",
                    success=True,
                    duration_ms=duration_ms,
                    description=f"Administrative action: {action_name}",
                    additional_data={"requires_approval": requires_approval, "function": f.__name__},
                )

                current_app.logger.warning(
                    f"ADMIN ACTION: {action_name} completed successfully by user {user_id} " f"in {duration_ms}ms"
                )

                return result

            except Exception as e:
                # Log failed admin action
                duration_ms = int((time.time() - start_time) * 1000)
                audit_logger.log_event(
                    event_type=AuditEventType.SYSTEM_MAINTENANCE,
                    action=action_name,
                    severity=AuditSeverity.CRITICAL,
                    resource_type="admin_action",
                    success=False,
                    error_message=str(e),
                    duration_ms=duration_ms,
                    description=f"Failed administrative action: {action_name}",
                    additional_data={"requires_approval": requires_approval, "function": f.__name__},
                )

                current_app.logger.error(
                    f"ADMIN ACTION: {action_name} failed for user {user_id} " f"after {duration_ms}ms: {str(e)}"
                )

                raise

        return decorated_function

    return decorator


def audit_financial_operation(operation_type: str, amount_field: str = None):
    """
    Decorator to audit financial operations with special attention to amounts.

    Args:
        operation_type: Type of financial operation (payment, refund, etc.)
        amount_field: Field name containing the financial amount
    """

    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            start_time = time.time()

            # Extract amount information if specified
            amount = None
            if amount_field:
                if amount_field in kwargs:
                    amount = kwargs[amount_field]
                elif hasattr(request, "json") and request.json and amount_field in request.json:
                    amount = request.json[amount_field]

            try:
                result = f(*args, **kwargs)

                # Log successful financial operation
                duration_ms = int((time.time() - start_time) * 1000)
                additional_data = {"operation_type": operation_type}
                if amount:
                    additional_data["amount"] = float(amount)

                audit_logger.log_event(
                    event_type=AuditEventType.PAYMENT_PROCESSED,
                    action=f"{operation_type}_{f.__name__}",
                    severity=AuditSeverity.HIGH,
                    resource_type="financial",
                    success=True,
                    duration_ms=duration_ms,
                    description=f"Financial operation: {operation_type}",
                    additional_data=additional_data,
                )

                return result

            except Exception as e:
                # Log failed financial operation
                duration_ms = int((time.time() - start_time) * 1000)
                additional_data = {"operation_type": operation_type}
                if amount:
                    additional_data["attempted_amount"] = float(amount)

                audit_logger.log_event(
                    event_type=AuditEventType.PAYMENT_FAILED,
                    action=f"{operation_type}_{f.__name__}",
                    severity=AuditSeverity.CRITICAL,
                    resource_type="financial",
                    success=False,
                    error_message=str(e),
                    duration_ms=duration_ms,
                    description=f"Failed financial operation: {operation_type}",
                    additional_data=additional_data,
                )

                raise

        return decorated_function

    return decorator


# Integration with existing RBAC decorators
def audit_permission_check(original_decorator):
    """
    Wrapper to add audit logging to existing permission decorators.

    Args:
        original_decorator: The original RBAC decorator to enhance
    """

    def enhanced_decorator(*decorator_args, **decorator_kwargs):
        def decorator(f):
            # Apply the original decorator first
            original_decorated = original_decorator(*decorator_args, **decorator_kwargs)(f)

            @wraps(original_decorated)
            def decorated_function(*args, **kwargs):
                try:
                    # Execute the original decorated function
                    result = original_decorated(*args, **kwargs)
                    return result

                except (UnauthorizedError, ForbiddenError):
                    # Log permission denied event
                    audit_permission_denied(
                        resource_type=request.endpoint,
                        required_permission=str(decorator_args) if decorator_args else "unknown",
                    )
                    raise

                except Exception as e:
                    # Log unexpected errors
                    audit_logger.log_security_event(
                        event_type=AuditEventType.SUSPICIOUS_ACTIVITY,
                        description=f"Unexpected error in permission check: {str(e)}",
                        severity=AuditSeverity.HIGH,
                    )
                    raise

            return decorated_function

        return decorator

    return enhanced_decorator


# Convenience decorators combining audit with common operations
def audit_login_attempt(f):
    """Decorator to audit login attempts."""

    @wraps(f)
    def decorated_function(*args, **kwargs):
        user_id = None

        try:
            # Extract user identifier from request
            if request.json:
                user_id = request.json.get("user_id") or request.json.get("email") or request.json.get("phone")

            result = f(*args, **kwargs)

            # Successful login
            if hasattr(result, "get") and result.get("user_id"):
                audit_login_success(result["user_id"])
            elif isinstance(result, dict) and "user" in result:
                audit_login_success(result["user"].get("id"))

            return result

        except Exception as e:
            # Failed login
            audit_login_failure(user_id=user_id, error_message=str(e))
            raise

    return decorated_function


def audit_sensitive_data_access(data_type: str):
    """Decorator to audit access to sensitive data."""

    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            # Log sensitive data access
            audit_logger.log_security_event(
                event_type=AuditEventType.SENSITIVE_DATA_ACCESS,
                description=f"Access to sensitive data: {data_type}",
                severity=AuditSeverity.MEDIUM,
                additional_data={"data_type": data_type, "endpoint": request.endpoint, "function": f.__name__},
            )

            return f(*args, **kwargs)

        return decorated_function

    return decorator
