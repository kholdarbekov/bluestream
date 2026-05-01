"""
Enhanced RBAC decorators for the BlueStream platform.
These decorators complement the existing decorators.py with new permission-based access control.
"""

from functools import wraps
from datetime import datetime, UTC
from typing import List

from flask import request, g, current_app
from flask_jwt_extended import verify_jwt_in_request, get_jwt_identity, get_jwt

from .rbac import rbac, require_permission, require_role, require_admin, audit_access, Permission
from shared.enums import UserRole
from .exceptions import UnauthorizedError, ForbiddenError


def require_user_management_access():
    """Require access to user management operations."""
    return require_permission([Permission.VIEW_USERS, Permission.EDIT_USERS], require_all=False)


def require_order_access():
    """Require access to order operations."""
    return require_permission(Permission.VIEW_ORDERS)


def require_product_management():
    """Require access to product management."""
    return require_permission([Permission.VIEW_PRODUCTS, Permission.EDIT_PRODUCTS], require_all=False)


def require_financial_access():
    """Require access to financial operations."""
    return require_permission([Permission.VIEW_PAYMENTS, Permission.VIEW_FINANCIAL_REPORTS], require_all=False)


def require_analytics_access():
    """Require access to analytics and reporting."""
    return require_permission(Permission.VIEW_ANALYTICS)


def require_delivery_management():
    """Require access to delivery management."""
    return require_permission([Permission.VIEW_DELIVERIES, Permission.ASSIGN_DELIVERIES], require_all=False)


def secure_admin_action(operation_name: str, required_permissions: List[Permission] = None):
    """
    Enhanced admin action decorator with auditing and permission checking.

    Args:
        operation_name: Name of the operation for auditing
        required_permissions: Specific permissions required for the operation
    """

    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            # Apply audit logging
            audit_decorator = audit_access(operation_name, "admin_operation")
            audited_func = audit_decorator(f)

            # Apply permission check if specified
            if required_permissions:
                permission_decorator = require_permission(required_permissions, require_all=True)
                return permission_decorator(audited_func)(*args, **kwargs)
            else:
                # Default to admin role requirement
                admin_decorator = require_admin()
                return admin_decorator(audited_func)(*args, **kwargs)

        return decorated_function

    return decorator


def emergency_operation(operation_name: str):
    """
    Decorator for emergency operations that require special permissions and logging.

    Args:
        operation_name: Name of the emergency operation
    """

    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            try:
                verify_jwt_in_request()
                user_id = get_jwt_identity()

                # Check emergency permission
                claims = get_jwt()
                user_role_str = claims.get("role")
                if not user_role_str:
                    raise ForbiddenError("No role found in token")

                user_role = UserRole(user_role_str)

                if not rbac.has_permission(user_role, Permission.EMERGENCY_ORDERS, user_id):
                    raise ForbiddenError("Emergency operation permission required")

                current_app.logger.critical(
                    f"EMERGENCY OPERATION: {operation_name} initiated by user {user_id} "
                    f"from IP {request.remote_addr} at {datetime.now(UTC)}"
                )

                result = f(*args, **kwargs)

                current_app.logger.critical(
                    f"EMERGENCY OPERATION: {operation_name} completed successfully by user {user_id}"
                )

                return result

            except Exception as e:
                current_app.logger.critical(
                    f"EMERGENCY OPERATION: {operation_name} failed for user {user_id}: {str(e)}"
                )
                raise

        return decorated_function

    return decorator


def sensitive_data_access(data_type: str):
    """
    Decorator for operations that access sensitive data.

    Args:
        data_type: Type of sensitive data being accessed
    """

    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            try:
                verify_jwt_in_request()
                user_id = get_jwt_identity()
                claims = get_jwt()
                user_role_str = claims.get("role")

                # Log sensitive data access
                current_app.logger.info(
                    f"SENSITIVE DATA ACCESS: User {user_id} ({user_role_str}) "
                    f"accessing {data_type} from {request.remote_addr}"
                )

                # Get user context for additional validation
                user_context = rbac.get_user_context(user_id)
                if not user_context or not rbac.validate_user_status(user_context["user"]):
                    raise ForbiddenError("User not authorized for sensitive data access")

                # Store context in g for the endpoint to use
                g.current_user_id = user_id
                g.current_user_role = UserRole(user_role_str)
                g.current_user = user_context["user"]

                return f(*args, **kwargs)

            except Exception as e:
                current_app.logger.error(
                    f"Sensitive data access failed for user {user_id}, data type {data_type}: {str(e)}"
                )
                raise

        return decorated_function

    return decorator


def time_restricted_operation(allowed_hours: List[int] = None):
    """
    Decorator to restrict operations to specific hours (security measure).

    Args:
        allowed_hours: List of allowed hours (0-23). If None, operates 24/7.
    """

    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if allowed_hours:
                current_hour = datetime.now(UTC).hour
                if current_hour not in allowed_hours:
                    current_app.logger.warning(
                        f"Time-restricted operation attempted outside allowed hours. "
                        f"Current hour: {current_hour}, Allowed: {allowed_hours}"
                    )
                    raise ForbiddenError("Operation not allowed at this time")

            return f(*args, **kwargs)

        return decorated_function

    return decorator


def multi_factor_required(f):
    """
    Decorator requiring multi-factor authentication for sensitive operations.
    This is a placeholder for future MFA implementation.
    """

    @wraps(f)
    def decorated_function(*args, **kwargs):
        # For now, just log that MFA would be required
        verify_jwt_in_request()
        user_id = get_jwt_identity()

        current_app.logger.info(
            f"MFA-protected operation attempted by user {user_id}. "
            f"MFA validation would be performed here in production."
        )

        # TODO: Implement actual MFA validation
        # For now, proceed with normal execution
        return f(*args, **kwargs)

    return decorated_function


def require_session_validation(f):
    """
    Decorator to validate that the user session is still valid and secure.
    """

    @wraps(f)
    def decorated_function(*args, **kwargs):
        try:
            verify_jwt_in_request()
            user_id = get_jwt_identity()
            claims = get_jwt()

            # Get user context
            user_context = rbac.get_user_context(user_id)
            if not user_context:
                raise ForbiddenError("User not found")

            user = user_context["user"]

            # Check if account is still active
            if not rbac.validate_user_status(user):
                current_app.logger.warning(f"Inactive user {user_id} attempted to access protected resource")
                raise ForbiddenError("Account is not active")

            # Check for suspicious activity (e.g., too many failed logins)
            if hasattr(user, "failed_login_attempts") and user.failed_login_attempts > 5:
                current_app.logger.warning(
                    f"User {user_id} with {user.failed_login_attempts} failed attempts accessing resource"
                )

            # Check token age (optional additional security)
            token_issued_at = claims.get("iat")
            if token_issued_at:
                import time

                current_time = time.time()
                token_age_hours = (current_time - token_issued_at) / 3600

                if token_age_hours > 24:  # Token older than 24 hours
                    current_app.logger.info(f"Old token used by user {user_id}: {token_age_hours:.1f} hours old")

            # Store validated context
            g.current_user_id = user_id
            g.current_user = user
            g.current_user_role = user.role

            return f(*args, **kwargs)

        except Exception as e:
            if isinstance(e, (UnauthorizedError, ForbiddenError)):
                raise
            current_app.logger.error(f"Session validation error: {e}")
            raise ForbiddenError("Session validation failed")

    return decorated_function


def audit_sensitive_operation(operation_type: str, resource_type: str = None):
    """
    Decorator for comprehensive auditing of sensitive operations.

    Args:
        operation_type: Type of operation (e.g., 'delete_user', 'modify_order')
        resource_type: Type of resource being modified
    """

    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            start_time = datetime.now(UTC)
            user_id = None
            operation_id = None

            try:
                verify_jwt_in_request()
                user_id = get_jwt_identity()

                # Generate unique operation ID for tracking
                import uuid

                operation_id = str(uuid.uuid4())[:8]

                # Log operation start
                current_app.logger.info(
                    f"AUDIT [{operation_id}]: User {user_id} starting {operation_type}"
                    f"{f' on {resource_type}' if resource_type else ''} "
                    f"from {request.remote_addr}"
                )

                # Execute the operation
                result = f(*args, **kwargs)

                # Log successful completion
                duration = (datetime.now(UTC) - start_time).total_seconds()
                current_app.logger.info(
                    f"AUDIT [{operation_id}]: User {user_id} completed {operation_type} "
                    f"successfully in {duration:.3f}s"
                )

                return result

            except Exception as e:
                # Log operation failure
                duration = (datetime.now(UTC) - start_time).total_seconds()
                current_app.logger.error(
                    f"AUDIT [{operation_id}]: User {user_id} failed {operation_type} "
                    f"after {duration:.3f}s: {str(e)}"
                )
                raise

        return decorated_function

    return decorator


# Convenience decorators for common combinations
def admin_with_audit(operation_name: str):
    """Combine admin requirement with audit logging."""

    def decorator(f):
        admin_decorator = require_admin()
        audit_decorator = audit_sensitive_operation(operation_name, "admin_action")
        return admin_decorator(audit_decorator(f))

    return decorator


def manager_with_audit(operation_name: str):
    """Combine manager requirement with audit logging."""

    def decorator(f):
        manager_decorator = require_role([UserRole.MANAGER, UserRole.ADMIN])
        audit_decorator = audit_sensitive_operation(operation_name, "manager_action")
        return manager_decorator(audit_decorator(f))

    return decorator


def staff_with_validation(operation_name: str):
    """Combine staff requirement with session validation and audit logging."""

    def decorator(f):
        staff_decorator = require_role([UserRole.OPERATOR, UserRole.MANAGER, UserRole.ADMIN])
        session_decorator = require_session_validation
        audit_decorator = audit_sensitive_operation(operation_name, "staff_action")
        return staff_decorator(session_decorator(audit_decorator(f)))

    return decorator
