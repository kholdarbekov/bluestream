import React, { useEffect, useState } from 'react';
import { Navigate, useLocation } from 'react-router-dom';
import { Spin, Result, Button } from 'antd';
import { useAuthStore } from '../../stores/authStore';

const ProtectedRoute = ({ children, requiredPermission = null, requiredRole = null }) => {
  const { isAuthenticated, isLoading, initialize, hasPermission, getUserRole } = useAuthStore();
  const location = useLocation();
  const [initializationComplete, setInitializationComplete] = useState(false);

  useEffect(() => {
    const initAuth = async () => {
      try {
        await initialize();
      } catch (error) {
        console.error('Auth initialization failed:', error);
      } finally {
        setInitializationComplete(true);
      }
    };

    initAuth();
  }, [initialize]);

  // Show loading spinner while checking authentication
  if (isLoading || !initializationComplete) {
    return (
      <div style={{
        display: 'flex',
        justifyContent: 'center',
        alignItems: 'center',
        height: '100vh',
        flexDirection: 'column'
      }}>
        <Spin size="large" />
        <div style={{ marginTop: 16, color: '#666' }}>
          Verifying authentication...
        </div>
      </div>
    );
  }

  // Redirect to login if not authenticated
  if (!isAuthenticated) {
    return <Navigate to="/login" state={{ from: location }} replace />;
  }

  // Check role-based access
  if (requiredRole && getUserRole() !== requiredRole) {
    return (
      <Result
        status="403"
        title="403"
        subTitle={`Access denied. ${requiredRole} role required.`}
        extra={
          <Button type="primary" onClick={() => window.location.href = '/dashboard'}>
            Back to Dashboard
          </Button>
        }
      />
    );
  }

  // Check permission-based access
  if (requiredPermission && !hasPermission(requiredPermission)) {
    return (
      <Result
        status="403"
        title="403"
        subTitle="Access denied. You don't have permission to access this page."
        extra={
          <Button type="primary" onClick={() => window.location.href = '/dashboard'}>
            Back to Dashboard
          </Button>
        }
      />
    );
  }

  return children;
};

export default ProtectedRoute;
