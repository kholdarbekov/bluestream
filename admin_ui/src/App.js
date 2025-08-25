import React from 'react';
import { Routes, Route, Navigate } from 'react-router-dom';
import { Layout } from 'antd';
import AdminLayout from './components/layout/AdminLayout';
import Login from './pages/Login';
import Dashboard from './pages/Dashboard';
import Users from './pages/Users';
import Orders from './pages/Orders';
import Products from './pages/Products';
import Delivery from './pages/Delivery';
import Loyalty from './pages/Loyalty';
import Notifications from './pages/Notifications';
import Analytics from './pages/Analytics';
import Translations from './pages/Translations';
import Settings from './pages/Settings';
import { useAuthStore } from './stores/authStore';
import ProtectedRoute from './components/common/ProtectedRoute';

function App() {
  const { isAuthenticated } = useAuthStore();

  return (
    <Layout className="admin-layout">
      <Routes>
        <Route
          path="/login"
          element={!isAuthenticated ? <Login /> : <Navigate to="/dashboard" />}
        />
        <Route
          path="/*"
          element={(
            <ProtectedRoute>
              <AdminLayout>
                <Routes>
                  <Route path="/" element={<Navigate to="/dashboard" />} />
                  <Route path="/dashboard" element={<Dashboard />} />
                  <Route path="/users" element={<Users />} />
                  <Route path="/orders" element={<Orders />} />
                  <Route path="/products" element={<Products />} />
                  <Route path="/delivery" element={<Delivery />} />
                  <Route path="/loyalty" element={<Loyalty />} />
                  <Route path="/notifications" element={<Notifications />} />
                  <Route path="/analytics" element={<Analytics />} />
                  <Route path="/translations" element={<Translations />} />
                  <Route path="/settings" element={<Settings />} />
                </Routes>
              </AdminLayout>
            </ProtectedRoute>
          )}
        />
      </Routes>
    </Layout>
  );
}

export default App;