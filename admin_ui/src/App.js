import React from 'react';
import { Routes, Route, Navigate } from 'react-router-dom';
import { Layout } from 'antd';
import AdminLayout from './components/layout/AdminLayout';
import Login from './pages/Login';
import Dashboard from './pages/Dashboard';
import Users from './pages/Users';
import Orders from './pages/Orders';
import CorporateContracts from './pages/CorporateContracts';
import Products from './pages/Products';
import ProductCategories from './pages/ProductCategories';
import Delivery from './pages/Delivery';
import DeliveryReports from './pages/DeliveryReports';
import Tryouts from './pages/Tryouts';
import BottleTracking from './pages/BottleTracking';
import Loyalty from './pages/Loyalty';
import LoyaltyMembers from './pages/LoyaltyMembers';
import LoyaltyPrograms from './pages/LoyaltyPrograms';
import LoyaltyRewards from './pages/LoyaltyRewards';
import Notifications from './pages/Notifications';
import Analytics from './pages/Analytics';
import Translations from './pages/Translations';
import Settings from './pages/Settings';
import Blog from './pages/Blog';
import TimeSlots from './pages/TimeSlots';
import DeliveryPersons from './pages/DeliveryPersons';
import Operators from './pages/Operators';
import StaffManagement from './pages/StaffManagement';
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
                  <Route path="/corporate-contracts" element={<CorporateContracts />} />
                  <Route path="/products" element={<Products />} />
                  <Route path="/product-categories" element={<ProductCategories />} />
                  <Route path="/delivery" element={<Delivery />} />
                  <Route path="/delivery/reports" element={<DeliveryReports />} />
                  <Route path="/tryouts" element={<Tryouts />} />
                  <Route path="/delivery/bottles" element={<BottleTracking />} />
                  <Route path="/delivery-time-slots" element={<TimeSlots />} />
                  <Route path="/loyalty" element={<Loyalty />} />
                  <Route path="/loyalty/members" element={<LoyaltyMembers />} />
                  <Route path="/loyalty/programs" element={<LoyaltyPrograms />} />
                  <Route path="/loyalty/rewards" element={<LoyaltyRewards />} />
                  <Route path="/loyalty-programs" element={<Navigate to="/loyalty/programs" replace />} />
                  <Route path="/notifications" element={<Notifications />} />
                  <Route path="/analytics" element={<Analytics />} />
                  <Route path="/blog" element={<Blog />} />
                  <Route path="/translations" element={<Translations />} />
                  <Route path="/settings" element={<Settings />} />
                  <Route path="/staff/delivery-persons" element={<DeliveryPersons />} />
                  <Route path="/staff/operators" element={<Operators />} />
                  <Route path="/staff/management" element={<StaffManagement />} />
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
