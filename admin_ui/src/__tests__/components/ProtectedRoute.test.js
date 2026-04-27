import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';

import ProtectedRoute from '../../components/common/ProtectedRoute';

// One mock object the tests mutate per case; useAuthStore() returns the same
// reference every render so React picks up changes via the test's re-render.
const mockAuth = {
  isAuthenticated: false,
  isLoading: false,
  initialize: vi.fn().mockResolvedValue(undefined),
  hasPermission: vi.fn(() => true),
  getUserRole: vi.fn(() => 'admin'),
};

vi.mock('../../stores/authStore', () => ({
  useAuthStore: () => mockAuth,
}));

const renderAt = (entry = '/secret') =>
  render(
    <MemoryRouter initialEntries={[entry]}>
      <Routes>
        <Route path="/login" element={<div>login-page</div>} />
        <Route
          path="/secret"
          element={
            <ProtectedRoute>
              <div>secret-content</div>
            </ProtectedRoute>
          }
        />
        <Route
          path="/perm-only"
          element={
            <ProtectedRoute requiredPermission="can_manage_users">
              <div>perm-content</div>
            </ProtectedRoute>
          }
        />
        <Route
          path="/admin-only"
          element={
            <ProtectedRoute requiredRole="admin">
              <div>admin-content</div>
            </ProtectedRoute>
          }
        />
      </Routes>
    </MemoryRouter>,
  );

describe('ProtectedRoute', () => {
  beforeEach(() => {
    mockAuth.isAuthenticated = false;
    mockAuth.isLoading = false;
    mockAuth.initialize.mockClear();
    mockAuth.initialize.mockResolvedValue(undefined);
    mockAuth.hasPermission.mockReturnValue(true);
    mockAuth.getUserRole.mockReturnValue('admin');
  });

  it('redirects to /login when unauthenticated', async () => {
    renderAt('/secret');
    await waitFor(() => {
      expect(screen.getByText('login-page')).toBeInTheDocument();
    });
    expect(screen.queryByText('secret-content')).not.toBeInTheDocument();
  });

  it('renders children when authenticated', async () => {
    mockAuth.isAuthenticated = true;
    renderAt('/secret');
    await waitFor(() => {
      expect(screen.getByText('secret-content')).toBeInTheDocument();
    });
  });

  it('shows 403 result when authenticated user lacks the required role', async () => {
    mockAuth.isAuthenticated = true;
    mockAuth.getUserRole.mockReturnValue('operator');
    renderAt('/admin-only');
    await waitFor(() => {
      // antd's Result component renders the title 403 inside the result element.
      expect(screen.getByText(/admin role required/i)).toBeInTheDocument();
    });
    expect(screen.queryByText('admin-content')).not.toBeInTheDocument();
  });

  it('shows 403 result when authenticated user lacks the required permission', async () => {
    mockAuth.isAuthenticated = true;
    mockAuth.hasPermission.mockReturnValue(false);
    renderAt('/perm-only');
    await waitFor(() => {
      expect(screen.getByText(/don't have permission/i)).toBeInTheDocument();
    });
    expect(screen.queryByText('perm-content')).not.toBeInTheDocument();
  });

  it('runs initialize() exactly once on mount', async () => {
    mockAuth.isAuthenticated = true;
    renderAt('/secret');
    await waitFor(() => {
      expect(mockAuth.initialize).toHaveBeenCalledTimes(1);
    });
  });
});
