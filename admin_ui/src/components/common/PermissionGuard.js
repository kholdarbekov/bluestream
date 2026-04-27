import React from 'react';
import { Result, Button } from 'antd';
import { useAuthStore } from '../../stores/authStore';
import { useNavigate } from 'react-router-dom';

/**
 * Permission Guard Component
 * Wraps components that require specific permissions
 */
const PermissionGuard = ({
  children,
  permission,
  permissions = [],
  requireAll = false,
  fallback = null,
  showFallback = true
}) => {
  const { hasPermission, getUserRole } = useAuthStore();
  const navigate = useNavigate();

  // Check single permission
  if (permission && !hasPermission(permission)) {
    return showFallback ? (
      fallback || (
        <Result
          status="403"
          title="403"
          subTitle="Sorry, you don't have permission to access this page."
          extra={
            <Button type="primary" onClick={() => navigate('/dashboard')}>
              Back to Dashboard
            </Button>
          }
        />
      )
    ) : null;
  }

  // Check multiple permissions
  if (permissions.length > 0) {
    const hasRequiredPermissions = requireAll
      ? permissions.every(perm => hasPermission(perm))
      : permissions.some(perm => hasPermission(perm));

    if (!hasRequiredPermissions) {
      return showFallback ? (
        fallback || (
          <Result
            status="403"
            title="403"
            subTitle="Sorry, you don't have permission to access this page."
            extra={
              <Button type="primary" onClick={() => navigate('/dashboard')}>
                Back to Dashboard
              </Button>
            }
          />
        )
      ) : null;
    }
  }

  return children;
};

/**
 * Higher-Order Component for permission-based access control
 */
export const withPermission = (permission, options = {}) => {
  return (WrappedComponent) => {
    const PermissionWrappedComponent = (props) => {
      return (
        <PermissionGuard permission={permission} {...options}>
          <WrappedComponent {...props} />
        </PermissionGuard>
      );
    };

    PermissionWrappedComponent.displayName = `withPermission(${WrappedComponent.displayName || WrappedComponent.name})`;
    return PermissionWrappedComponent;
  };
};

/**
 * Hook for checking permissions in components
 */
export const usePermissions = () => {
  const { hasPermission, getUserRole, permissions } = useAuthStore();

  return {
    hasPermission,
    getUserRole,
    permissions,
    // Convenience methods for common permission checks
    canManageUsers: () => hasPermission('can_manage_users'),
    canManageProducts: () => hasPermission('can_manage_products'),
    canManageOrders: () => hasPermission('can_manage_orders'),
    canViewAnalytics: () => hasPermission('can_view_analytics'),
    canManageDelivery: () => hasPermission('can_manage_delivery'),
    canManageSettings: () => hasPermission('can_manage_settings'),
    isAdmin: () => getUserRole() === 'admin',
    isManager: () => getUserRole() === 'manager',
    isOperator: () => getUserRole() === 'operator'
  };
};

/**
 * Component for conditional rendering based on permissions
 */
export const PermissionCheck = ({
  children,
  permission,
  permissions = [],
  requireAll = false,
  role = null,
  roles = [],
  fallback = null
}) => {
  const { hasPermission, getUserRole } = useAuthStore();
  const userRole = getUserRole();

  // Check role-based access
  if (role && userRole !== role) {
    return fallback;
  }

  if (roles.length > 0 && !roles.includes(userRole)) {
    return fallback;
  }

  // Check permission-based access
  if (permission && !hasPermission(permission)) {
    return fallback;
  }

  if (permissions.length > 0) {
    const hasRequiredPermissions = requireAll
      ? permissions.every(perm => hasPermission(perm))
      : permissions.some(perm => hasPermission(perm));

    if (!hasRequiredPermissions) {
      return fallback;
    }
  }

  return children;
};

export default PermissionGuard;
