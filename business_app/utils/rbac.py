"""
Enhanced Role-Based Access Control (RBAC) system for BlueStream platform.
This module provides comprehensive access control with permissions, roles, and security features.
"""

import functools
import time
from typing import List, Dict, Set, Optional, Callable, Any, Union
from enum import Enum
from flask import request, g, current_app
from flask_jwt_extended import verify_jwt_in_request, get_jwt_identity, get_jwt
from datetime import datetime, UTC

from .exceptions import UnauthorizedError, ForbiddenError, RateLimitError
from .constants import UserRole, UserStatus


class Permission(Enum):
    """System permissions for granular access control."""
    
    # User management permissions
    VIEW_USERS = "view_users"
    CREATE_USERS = "create_users"
    EDIT_USERS = "edit_users"
    DELETE_USERS = "delete_users"
    MANAGE_USER_ROLES = "manage_user_roles"
    
    # Order management permissions
    VIEW_ORDERS = "view_orders"
    CREATE_ORDERS = "create_orders"
    EDIT_ORDERS = "edit_orders"
    CANCEL_ORDERS = "cancel_orders"
    PROCESS_ORDERS = "process_orders"
    VIEW_ALL_ORDERS = "view_all_orders"
    
    # Product management permissions
    VIEW_PRODUCTS = "view_products"
    CREATE_PRODUCTS = "create_products"
    EDIT_PRODUCTS = "edit_products"
    DELETE_PRODUCTS = "delete_products"
    MANAGE_INVENTORY = "manage_inventory"
    
    # Payment management permissions
    VIEW_PAYMENTS = "view_payments"
    PROCESS_PAYMENTS = "process_payments"
    ISSUE_REFUNDS = "issue_refunds"
    VIEW_FINANCIAL_REPORTS = "view_financial_reports"
    
    # Delivery management permissions
    VIEW_DELIVERIES = "view_deliveries"
    ASSIGN_DELIVERIES = "assign_deliveries"
    UPDATE_DELIVERY_STATUS = "update_delivery_status"
    MANAGE_DELIVERY_ROUTES = "manage_delivery_routes"
    
    # Analytics and reporting permissions
    VIEW_ANALYTICS = "view_analytics"
    EXPORT_DATA = "export_data"
    VIEW_SYSTEM_METRICS = "view_system_metrics"
    
    # System administration permissions
    MANAGE_SETTINGS = "manage_settings"
    VIEW_LOGS = "view_logs"
    MANAGE_NOTIFICATIONS = "manage_notifications"
    SYSTEM_MAINTENANCE = "system_maintenance"
    
    # Emergency operations
    EMERGENCY_ORDERS = "emergency_orders"
    OVERRIDE_LIMITS = "override_limits"
    FORCE_ACTIONS = "force_actions"


class AccessLevel(Enum):
    """Access levels for different types of operations."""
    
    READ = "read"
    WRITE = "write"
    DELETE = "delete"
    ADMIN = "admin"


# Role-to-permissions mapping
ROLE_PERMISSIONS: Dict[UserRole, Set[Permission]] = {
    UserRole.CUSTOMER: {
        Permission.VIEW_PRODUCTS,
        Permission.CREATE_ORDERS,
        Permission.VIEW_ORDERS,  # Own orders only
        Permission.CANCEL_ORDERS,  # Own orders only
    },
    
    UserRole.DELIVERY_DRIVER: {
        Permission.VIEW_PRODUCTS,
        Permission.VIEW_DELIVERIES,
        Permission.UPDATE_DELIVERY_STATUS,
        Permission.VIEW_ORDERS,  # Assigned orders only
    },
    
    UserRole.OPERATOR: {
        Permission.VIEW_USERS,
        Permission.VIEW_ORDERS,
        Permission.EDIT_ORDERS,
        Permission.PROCESS_ORDERS,
        Permission.VIEW_PRODUCTS,
        Permission.EDIT_PRODUCTS,
        Permission.MANAGE_INVENTORY,
        Permission.VIEW_DELIVERIES,
        Permission.ASSIGN_DELIVERIES,
        Permission.UPDATE_DELIVERY_STATUS,
        Permission.VIEW_PAYMENTS,
        Permission.MANAGE_NOTIFICATIONS,
    },
    
    UserRole.MANAGER: {
        Permission.VIEW_USERS,
        Permission.CREATE_USERS,
        Permission.EDIT_USERS,
        Permission.VIEW_ORDERS,
        Permission.CREATE_ORDERS,
        Permission.EDIT_ORDERS,
        Permission.CANCEL_ORDERS,
        Permission.PROCESS_ORDERS,
        Permission.VIEW_ALL_ORDERS,
        Permission.VIEW_PRODUCTS,
        Permission.CREATE_PRODUCTS,
        Permission.EDIT_PRODUCTS,
        Permission.DELETE_PRODUCTS,
        Permission.MANAGE_INVENTORY,
        Permission.VIEW_DELIVERIES,
        Permission.ASSIGN_DELIVERIES,
        Permission.UPDATE_DELIVERY_STATUS,
        Permission.MANAGE_DELIVERY_ROUTES,
        Permission.VIEW_PAYMENTS,
        Permission.PROCESS_PAYMENTS,
        Permission.ISSUE_REFUNDS,
        Permission.VIEW_FINANCIAL_REPORTS,
        Permission.VIEW_ANALYTICS,
        Permission.EXPORT_DATA,
        Permission.MANAGE_NOTIFICATIONS,
        Permission.EMERGENCY_ORDERS,
    },
    
    UserRole.ADMIN: set(Permission),  # Admin has all permissions
}


class RBACValidator:
    """Role-Based Access Control validator with enhanced security features."""
    
    def __init__(self):
        self._permission_cache = {}
        self._cache_ttl = 300  # 5 minutes
        
    def get_user_permissions(self, user_role: UserRole, user_id: int = None) -> Set[Permission]:
        """
        Get permissions for a user role with caching.
        
        Args:
            user_role: The user's role
            user_id: Optional user ID for cache key
            
        Returns:
            Set of permissions for the role
        """
        cache_key = f"{user_role.value}:{user_id}" if user_id else user_role.value
        current_time = time.time()
        
        # Check cache
        if cache_key in self._permission_cache:
            cached_time, permissions = self._permission_cache[cache_key]
            if current_time - cached_time < self._cache_ttl:
                return permissions
        
        # Get permissions from role mapping
        permissions = ROLE_PERMISSIONS.get(user_role, set())
        
        # Cache the result
        self._permission_cache[cache_key] = (current_time, permissions)
        
        return permissions
    
    def has_permission(self, user_role: UserRole, required_permission: Permission, 
                      user_id: int = None) -> bool:
        """
        Check if user role has a specific permission.
        
        Args:
            user_role: The user's role
            required_permission: The permission to check
            user_id: Optional user ID for additional checks
            
        Returns:
            True if user has permission, False otherwise
        """
        user_permissions = self.get_user_permissions(user_role, user_id)
        return required_permission in user_permissions
    
    def has_any_permission(self, user_role: UserRole, required_permissions: List[Permission],
                          user_id: int = None) -> bool:
        """
        Check if user role has any of the specified permissions.
        
        Args:
            user_role: The user's role
            required_permissions: List of permissions to check
            user_id: Optional user ID for additional checks
            
        Returns:
            True if user has at least one permission, False otherwise
        """
        user_permissions = self.get_user_permissions(user_role, user_id)
        return any(perm in user_permissions for perm in required_permissions)
    
    def has_all_permissions(self, user_role: UserRole, required_permissions: List[Permission],
                           user_id: int = None) -> bool:
        """
        Check if user role has all of the specified permissions.
        
        Args:
            user_role: The user's role
            required_permissions: List of permissions to check
            user_id: Optional user ID for additional checks
            
        Returns:
            True if user has all permissions, False otherwise
        """
        user_permissions = self.get_user_permissions(user_role, user_id)
        return all(perm in user_permissions for perm in required_permissions)
    
    def validate_user_status(self, user) -> bool:
        """
        Validate that user account is in good standing.
        
        Args:
            user: User model instance
            
        Returns:
            True if user status is valid, False otherwise
        """
        if not user:
            return False
        
        # Check account status
        if user.status != UserStatus.ACTIVE.value:
            return False
        
        # Check if account is locked
        if hasattr(user, 'account_locked_until') and user.account_locked_until:
            if user.account_locked_until > datetime.now(UTC):
                return False
        
        # Check if user is verified for sensitive operations
        if hasattr(user, 'is_verified') and not user.is_verified:
            # For now, allow unverified users but log the access
            current_app.logger.warning(f"Unverified user {user.id} accessing protected resource")
        
        return True
    
    def get_user_context(self, user_id: int):
        """
        Get comprehensive user context for access control decisions.
        
        Args:
            user_id: The user ID
            
        Returns:
            Dictionary with user context information
        """
        from business_app.models.user import User
        
        user = User.query.get(user_id)
        if not user:
            return None
        
        return {
            'user': user,
            'role': user.role,
            'status': user.status,
            'is_verified': getattr(user, 'is_verified', False),
            'permissions': self.get_user_permissions(user.role, user_id),
            'last_login': getattr(user, 'last_login', None),
            'failed_attempts': getattr(user, 'failed_login_attempts', 0),
        }


# Global RBAC validator instance
rbac = RBACValidator()


def require_permission(permission: Union[Permission, List[Permission]], 
                      access_level: AccessLevel = AccessLevel.READ,
                      require_all: bool = False):
    """
    Decorator to require specific permission(s) for endpoint access.
    
    Args:
        permission: Single permission or list of permissions required
        access_level: Level of access required (read, write, delete, admin)
        require_all: If True, user must have ALL permissions. If False, ANY permission is sufficient.
    """
    def decorator(f: Callable) -> Callable:
        @functools.wraps(f)
        def decorated_function(*args, **kwargs):
            try:
                verify_jwt_in_request()
                user_id = get_jwt_identity()
                claims = get_jwt()
                user_role_str = claims.get('role')
                
                if not user_role_str:
                    raise ForbiddenError("No role found in token")
                
                try:
                    user_role = UserRole(user_role_str)
                except ValueError:
                    raise ForbiddenError("Invalid role in token")
                
                # Get user context for validation
                user_context = rbac.get_user_context(user_id)
                if not user_context:
                    raise ForbiddenError("User not found")
                
                # Validate user status
                if not rbac.validate_user_status(user_context['user']):
                    raise ForbiddenError("Account is not in good standing")
                
                # Check role consistency
                if user_context['role'] != user_role:
                    current_app.logger.warning(
                        f"Role mismatch for user {user_id}: "
                        f"JWT={user_role}, DB={user_context['role']}"
                    )
                    raise ForbiddenError("Role validation failed")
                
                # Check permissions
                permissions_to_check = permission if isinstance(permission, list) else [permission]
                
                if require_all:
                    has_access = rbac.has_all_permissions(user_role, permissions_to_check, user_id)
                else:
                    has_access = rbac.has_any_permission(user_role, permissions_to_check, user_id)
                
                if not has_access:
                    perm_names = [p.value for p in permissions_to_check]
                    current_app.logger.warning(
                        f"Access denied for user {user_id} ({user_role.value}) "
                        f"to permission(s): {perm_names}"
                    )
                    raise ForbiddenError("Insufficient permissions")
                
                # Store user context in Flask g for use in endpoint
                g.current_user_id = user_id
                g.current_user_role = user_role
                g.current_user = user_context['user']
                g.user_permissions = user_context['permissions']
                
                return f(*args, **kwargs)
                
            except Exception as e:
                if isinstance(e, (UnauthorizedError, ForbiddenError)):
                    raise
                current_app.logger.error(f"RBAC validation error: {e}")
                raise ForbiddenError("Access validation failed")
        
        return decorated_function
    return decorator


def require_role(role: Union[UserRole, List[UserRole]], require_all: bool = False):
    """
    Decorator to require specific role(s) for endpoint access.
    
    Args:
        role: Single role or list of roles required
        require_all: If True, user must have ALL roles. If False, ANY role is sufficient.
    """
    def decorator(f: Callable) -> Callable:
        @functools.wraps(f)
        def decorated_function(*args, **kwargs):
            try:
                verify_jwt_in_request()
                user_id = get_jwt_identity()
                claims = get_jwt()
                user_role_str = claims.get('role')
                
                if not user_role_str:
                    raise ForbiddenError("No role found in token")
                
                try:
                    user_role = UserRole(user_role_str)
                except ValueError:
                    raise ForbiddenError("Invalid role in token")
                
                # Check if user role is in allowed roles
                allowed_roles = role if isinstance(role, list) else [role]
                
                if user_role not in allowed_roles:
                    current_app.logger.warning(
                        f"Role access denied for user {user_id}: "
                        f"has {user_role.value}, needs one of {[r.value for r in allowed_roles]}"
                    )
                    raise ForbiddenError("Insufficient role privileges")
                
                # Get and validate user context
                user_context = rbac.get_user_context(user_id)
                if not user_context:
                    raise ForbiddenError("User not found")
                
                if not rbac.validate_user_status(user_context['user']):
                    raise ForbiddenError("Account is not in good standing")
                
                # Store user context in Flask g
                g.current_user_id = user_id
                g.current_user_role = user_role
                g.current_user = user_context['user']
                g.user_permissions = user_context['permissions']
                
                return f(*args, **kwargs)
                
            except Exception as e:
                if isinstance(e, (UnauthorizedError, ForbiddenError)):
                    raise
                current_app.logger.error(f"Role validation error: {e}")
                raise ForbiddenError("Role validation failed")
        
        return decorated_function
    return decorator


def require_admin():
    """Decorator to require admin role with enhanced validation."""
    return require_role(UserRole.ADMIN)


def require_manager_or_admin():
    """Decorator to require manager or admin role."""
    return require_role([UserRole.MANAGER, UserRole.ADMIN])


def require_staff():
    """Decorator to require staff access (operator, manager, or admin)."""
    return require_role([UserRole.OPERATOR, UserRole.MANAGER, UserRole.ADMIN])


def require_own_resource_or_staff(resource_user_id_param: str = 'user_id'):
    """
    Decorator to require either owning the resource or having staff privileges.
    
    Args:
        resource_user_id_param: Parameter name that contains the resource owner's user ID
    """
    def decorator(f: Callable) -> Callable:
        @functools.wraps(f)
        def decorated_function(*args, **kwargs):
            try:
                verify_jwt_in_request()
                user_id = get_jwt_identity()
                claims = get_jwt()
                user_role_str = claims.get('role')
                
                if not user_role_str:
                    raise ForbiddenError("No role found in token")
                
                user_role = UserRole(user_role_str)
                
                # Get user context
                user_context = rbac.get_user_context(user_id)
                if not user_context or not rbac.validate_user_status(user_context['user']):
                    raise ForbiddenError("User not found or account not in good standing")
                
                # Check if user is staff (has elevated privileges)
                staff_roles = [UserRole.OPERATOR, UserRole.MANAGER, UserRole.ADMIN]
                if user_role in staff_roles:
                    # Staff can access any resource
                    g.current_user_id = user_id
                    g.current_user_role = user_role
                    g.current_user = user_context['user']
                    g.user_permissions = user_context['permissions']
                    return f(*args, **kwargs)
                
                # For non-staff users, check if they own the resource
                resource_user_id = None
                
                # Try to get resource user ID from URL parameters
                if resource_user_id_param in kwargs:
                    resource_user_id = kwargs[resource_user_id_param]
                elif resource_user_id_param in request.view_args:
                    resource_user_id = request.view_args[resource_user_id_param]
                elif hasattr(request, 'json') and request.json and resource_user_id_param in request.json:
                    resource_user_id = request.json[resource_user_id_param]
                
                # Convert to int if it's a string
                if isinstance(resource_user_id, str) and resource_user_id.isdigit():
                    resource_user_id = int(resource_user_id)
                
                if resource_user_id != user_id:
                    raise ForbiddenError("Access denied: can only access own resources")
                
                g.current_user_id = user_id
                g.current_user_role = user_role
                g.current_user = user_context['user']
                g.user_permissions = user_context['permissions']
                
                return f(*args, **kwargs)
                
            except Exception as e:
                if isinstance(e, (UnauthorizedError, ForbiddenError)):
                    raise
                current_app.logger.error(f"Resource access validation error: {e}")
                raise ForbiddenError("Resource access validation failed")
        
        return decorated_function
    return decorator


def audit_access(operation: str, resource_type: str = None):
    """
    Decorator to audit access to sensitive operations.
    
    Args:
        operation: Description of the operation being performed
        resource_type: Type of resource being accessed
    """
    def decorator(f: Callable) -> Callable:
        @functools.wraps(f)
        def decorated_function(*args, **kwargs):
            start_time = time.time()
            user_id = None
            user_role = None
            
            try:
                # Get user context if authenticated
                if hasattr(g, 'current_user_id'):
                    user_id = g.current_user_id
                    user_role = g.current_user_role
                
                # Log access attempt
                current_app.logger.info(
                    f"AUDIT: User {user_id} ({user_role.value if user_role else 'anonymous'}) "
                    f"attempting {operation}"
                    f"{f' on {resource_type}' if resource_type else ''} "
                    f"from {request.remote_addr}"
                )
                
                result = f(*args, **kwargs)
                
                # Log successful access
                elapsed_time = time.time() - start_time
                current_app.logger.info(
                    f"AUDIT: User {user_id} successfully completed {operation} "
                    f"in {elapsed_time:.3f}s"
                )
                
                return result
                
            except Exception as e:
                # Log failed access
                elapsed_time = time.time() - start_time
                current_app.logger.warning(
                    f"AUDIT: User {user_id} failed {operation} "
                    f"after {elapsed_time:.3f}s: {str(e)}"
                )
                raise
        
        return decorated_function
    return decorator


# Convenience decorators for common operations
def require_user_management():
    """Require permissions for user management operations."""
    return require_permission([Permission.VIEW_USERS, Permission.EDIT_USERS], require_all=False)


def require_order_management():
    """Require permissions for order management operations."""
    return require_permission([Permission.VIEW_ORDERS, Permission.EDIT_ORDERS], require_all=False)


def require_financial_access():
    """Require permissions for financial operations."""
    return require_permission([Permission.VIEW_PAYMENTS, Permission.VIEW_FINANCIAL_REPORTS], require_all=False)


def require_system_admin():
    """Require system administration permissions."""
    return require_permission([Permission.MANAGE_SETTINGS, Permission.SYSTEM_MAINTENANCE], require_all=True)