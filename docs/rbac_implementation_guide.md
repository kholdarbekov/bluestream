# Role-Based Access Control (RBAC) Implementation Guide

## Overview

The BlueStream platform now implements a comprehensive Role-Based Access Control (RBAC) system that provides granular permissions, enhanced security, and audit logging. This system complements the existing authentication mechanism with fine-grained authorization.

## Architecture

### Core Components

1. **Permission System** (`utils/rbac.py`)
   - Defines granular permissions for different operations
   - Maps roles to permissions
   - Provides validation and caching

2. **Enhanced Decorators** (`utils/enhanced_rbac_decorators.py`)
   - Permission-based decorators
   - Audit logging decorators
   - Security validation decorators

3. **Legacy Compatibility** (`utils/decorators.py`)
   - Maintains existing role-based decorators
   - Integrates with new RBAC system
   - Provides backward compatibility

## Permission System

### Available Permissions

#### User Management
- `VIEW_USERS` - View user information
- `CREATE_USERS` - Create new users
- `EDIT_USERS` - Modify user information
- `DELETE_USERS` - Delete user accounts
- `MANAGE_USER_ROLES` - Change user roles

#### Order Management
- `VIEW_ORDERS` - View order information
- `CREATE_ORDERS` - Create new orders
- `EDIT_ORDERS` - Modify existing orders
- `CANCEL_ORDERS` - Cancel orders
- `PROCESS_ORDERS` - Process and fulfill orders
- `VIEW_ALL_ORDERS` - View all orders (not just own)

#### Product Management
- `VIEW_PRODUCTS` - View product catalog
- `CREATE_PRODUCTS` - Add new products
- `EDIT_PRODUCTS` - Modify product information
- `DELETE_PRODUCTS` - Remove products
- `MANAGE_INVENTORY` - Manage stock levels

#### Financial Operations
- `VIEW_PAYMENTS` - View payment information
- `PROCESS_PAYMENTS` - Process transactions
- `ISSUE_REFUNDS` - Issue refunds
- `VIEW_FINANCIAL_REPORTS` - Access financial reports

#### Delivery Management
- `VIEW_DELIVERIES` - View delivery information
- `ASSIGN_DELIVERIES` - Assign deliveries to drivers
- `UPDATE_DELIVERY_STATUS` - Update delivery status
- `MANAGE_DELIVERY_ROUTES` - Manage delivery routes

#### Analytics & Reporting
- `VIEW_ANALYTICS` - Access analytics dashboard
- `EXPORT_DATA` - Export data and reports
- `VIEW_SYSTEM_METRICS` - View system metrics

#### System Administration
- `MANAGE_SETTINGS` - Modify system settings
- `VIEW_LOGS` - Access system logs
- `MANAGE_NOTIFICATIONS` - Manage notifications
- `SYSTEM_MAINTENANCE` - Perform maintenance tasks

#### Emergency Operations
- `EMERGENCY_ORDERS` - Create emergency orders
- `OVERRIDE_LIMITS` - Override system limits
- `FORCE_ACTIONS` - Force actions bypassing normal checks

### Role-Permission Mapping

```python
ROLE_PERMISSIONS = {
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
        # ... (extensive permissions)
    },

    UserRole.ADMIN: set(Permission),  # All permissions
}
```

## Usage Examples

### Basic Permission Decorators

```python
from business_app.utils.rbac import require_permission, Permission

@app.route('/api/users')
@require_permission(Permission.VIEW_USERS)
def get_users():
    """Only users with VIEW_USERS permission can access this."""
    return jsonify(users)

@app.route('/api/orders', methods=['POST'])
@require_permission(Permission.CREATE_ORDERS)
def create_order():
    """Only users with CREATE_ORDERS permission can create orders."""
    return jsonify(order)
```

### Multiple Permissions

```python
# Require ANY of the listed permissions
@require_permission([Permission.VIEW_ORDERS, Permission.EDIT_ORDERS], require_all=False)
def view_order_details():
    """Users with either VIEW_ORDERS OR EDIT_ORDERS can access this."""
    pass

# Require ALL of the listed permissions
@require_permission([Permission.EDIT_ORDERS, Permission.PROCESS_ORDERS], require_all=True)
def process_order():
    """Users must have BOTH EDIT_ORDERS AND PROCESS_ORDERS permissions."""
    pass
```

### Role-Based Decorators

```python
from business_app.utils.rbac import require_role, require_admin, require_staff

@require_role(UserRole.MANAGER)
def manager_only_function():
    """Only managers can access this."""
    pass

@require_role([UserRole.MANAGER, UserRole.ADMIN])
def manager_or_admin_function():
    """Managers or admins can access this."""
    pass

@require_admin()
def admin_only_function():
    """Only admins can access this."""
    pass

@require_staff()
def staff_function():
    """Operators, managers, or admins can access this."""
    pass
```

### Enhanced Security Decorators

```python
from business_app.utils.enhanced_rbac_decorators import (
    secure_admin_action, emergency_operation, sensitive_data_access,
    audit_sensitive_operation
)

@secure_admin_action("delete_user", [Permission.DELETE_USERS])
def delete_user(user_id):
    """Secure admin action with audit logging."""
    pass

@emergency_operation("emergency_order_creation")
def create_emergency_order():
    """Emergency operation with special logging."""
    pass

@sensitive_data_access("user_financial_data")
def get_user_payment_info():
    """Access to sensitive data with logging."""
    pass

@audit_sensitive_operation("order_modification", "order")
def modify_order():
    """Comprehensive audit logging for order modifications."""
    pass
```

### Resource Ownership Validation

```python
from business_app.utils.rbac import require_own_resource_or_staff

@require_own_resource_or_staff('user_id')
def get_user_profile(user_id):
    """Users can only access their own profile, staff can access any."""
    pass
```

### Convenience Decorators

```python
from business_app.utils.enhanced_rbac_decorators import (
    require_user_management_access,
    require_financial_access,
    require_analytics_access
)

@require_user_management_access()
def manage_users():
    """Requires user management permissions."""
    pass

@require_financial_access()
def view_financial_report():
    """Requires financial access permissions."""
    pass

@require_analytics_access()
def view_analytics():
    """Requires analytics permissions."""
    pass
```

## Migration Guide

### Updating Existing Endpoints

1. **Replace role-based decorators with permission-based ones:**

```python
# OLD
@admin_required
def delete_user():
    pass

# NEW - More specific
@require_permission(Permission.DELETE_USERS)
def delete_user():
    pass
```

2. **Add audit logging for sensitive operations:**

```python
# OLD
@admin_required
def modify_order():
    pass

# NEW - With audit logging
@audit_sensitive_operation("order_modification", "order")
@require_permission(Permission.EDIT_ORDERS)
def modify_order():
    pass
```

3. **Use convenience decorators for common patterns:**

```python
# OLD
@manager_or_admin_required
def view_reports():
    pass

# NEW - More descriptive
@require_analytics_access()
def view_reports():
    pass
```

### Backward Compatibility

The existing decorators in `utils/decorators.py` continue to work:

```python
# These still work
@admin_required
@manager_or_admin_required
@staff_required
```

But new code should use the enhanced RBAC system for better security and maintainability.

## Security Features

### 1. Permission Caching
- Permissions are cached for 5 minutes to improve performance
- Cache is automatically invalidated when needed

### 2. User Status Validation
- Account status is checked on every request
- Locked accounts are automatically denied access
- Unverified accounts are logged but may be allowed (configurable)

### 3. Role Consistency Checking
- JWT role claims are validated against database
- Mismatches are logged and access is denied

### 4. Comprehensive Audit Logging
- All sensitive operations are logged
- Failed access attempts are recorded
- Operation timing and user context included

### 5. Emergency Operation Tracking
- Special logging for emergency operations
- Enhanced monitoring and alerting

## Best Practices

### 1. Use Specific Permissions
```python
# GOOD - Specific permission
@require_permission(Permission.EDIT_ORDERS)
def update_order():
    pass

# AVOID - Too broad
@require_admin()
def update_order():
    pass
```

### 2. Apply Principle of Least Privilege
```python
# GOOD - Only requires view permission
@require_permission(Permission.VIEW_ORDERS)
def get_order_summary():
    pass

# AVOID - Requires edit when only view is needed
@require_permission(Permission.EDIT_ORDERS)
def get_order_summary():
    pass
```

### 3. Use Audit Logging for Sensitive Operations
```python
# GOOD - Sensitive operation with audit
@audit_sensitive_operation("user_deletion", "user")
@require_permission(Permission.DELETE_USERS)
def delete_user():
    pass
```

### 4. Combine Decorators Appropriately
```python
# GOOD - Logical combination
@require_session_validation
@audit_sensitive_operation("financial_report_access", "financial_data")
@require_permission(Permission.VIEW_FINANCIAL_REPORTS)
def generate_financial_report():
    pass
```

### 5. Handle Resource Ownership
```python
# GOOD - Allows users to access own data, staff to access any
@require_own_resource_or_staff('user_id')
def get_user_orders(user_id):
    pass
```

## Testing RBAC

### Unit Tests Example
```python
def test_user_management_access():
    # Test with user having permission
    with app.test_client() as client:
        token = create_jwt_token(user_with_permission)
        response = client.get('/api/users', headers={'Authorization': f'Bearer {token}'})
        assert response.status_code == 200

    # Test with user lacking permission
    with app.test_client() as client:
        token = create_jwt_token(user_without_permission)
        response = client.get('/api/users', headers={'Authorization': f'Bearer {token}'})
        assert response.status_code == 403
```

### Integration Tests
```python
def test_role_permission_consistency():
    """Test that roles have expected permissions."""
    admin_permissions = rbac.get_user_permissions(UserRole.ADMIN)
    assert Permission.DELETE_USERS in admin_permissions

    customer_permissions = rbac.get_user_permissions(UserRole.CUSTOMER)
    assert Permission.DELETE_USERS not in customer_permissions
```

## Monitoring and Alerting

### Log Analysis
- Monitor for repeated permission denied attempts
- Alert on emergency operation usage
- Track permission usage patterns

### Metrics to Track
- Permission denial rates by endpoint
- Role usage distribution
- Failed authentication attempts
- Emergency operation frequency

## Troubleshooting

### Common Issues

1. **Permission Denied Errors**
   - Check user role and permissions
   - Verify JWT token contains correct role
   - Check user account status

2. **Role Mismatch Warnings**
   - User role in JWT doesn't match database
   - May indicate token tampering or stale tokens

3. **Cache Issues**
   - Permission cache may need manual invalidation
   - Restart application if permissions seem stale

### Debug Mode
Enable debug logging to see detailed RBAC operations:

```python
import logging
logging.getLogger('business_app.utils.rbac').setLevel(logging.DEBUG)
```

## Future Enhancements

1. **Dynamic Permissions**
   - Database-driven permission system
   - Runtime permission modification

2. **Multi-Factor Authentication**
   - Enhanced security for sensitive operations
   - Integration with MFA providers

3. **Context-Aware Permissions**
   - Time-based restrictions
   - Location-based access control

4. **Permission Inheritance**
   - Hierarchical permission structure
   - Inherited role capabilities

5. **API Rate Limiting by Role**
   - Different rate limits for different roles
   - Enhanced abuse prevention
