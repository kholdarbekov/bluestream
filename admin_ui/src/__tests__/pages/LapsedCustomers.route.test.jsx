/**
 * The /lapsed-customers route sits behind `PermissionGuard permission="can_manage_lapsed_customers"`
 * (reorder-reminders spec §9.1). The route table is driven for real (App.js, ProtectedRoute and
 * PermissionGuard); the pages are markers, the layout is a passthrough.
 */
import React from 'react';
import { render, screen } from '@testing-library/react';
import { MemoryRouter, useLocation } from 'react-router-dom';

import App from '../../App';

const grants = new Set();
const mockAuth = {
  isAuthenticated: true,
  isLoading: false,
  initialize: vi.fn().mockResolvedValue(undefined),
  hasPermission: vi.fn((permission) => grants.has(permission)),
  getUserRole: vi.fn(() => 'manager'),
  user: { first_name: 'Aziza', last_name: 'Manager', role: 'manager' },
};
vi.mock('../../stores/authStore', () => ({ useAuthStore: () => mockAuth }));
vi.mock('../../components/layout/AdminLayout', () => ({ default: ({ children }) => <div>{children}</div> }));
vi.mock('../../pages/LapsedCustomers', () => ({ default: () => <div>lapsed-customers-page</div> }));
vi.mock('../../pages/Dashboard', () => ({ default: () => <div>dashboard-page</div> }));
vi.mock('../../components/common/LanguageSwitcher', () => ({ default: () => <div /> }));
vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key, opts) => (typeof opts === 'string' ? opts : opts?.defaultValue) || key }),
}));

const LocationProbe = () => <div data-testid="location">{useLocation().pathname}</div>;
const renderAt = (path) => render(
  <MemoryRouter initialEntries={[path]}>
    <App />
    <LocationProbe />
  </MemoryRouter>,
);

beforeEach(() => {
  grants.clear();
});

it('shows the worklist to an admin who holds can_manage_lapsed_customers', async () => {
  grants.add('can_manage_lapsed_customers');

  renderAt('/lapsed-customers');

  expect(await screen.findByText('lapsed-customers-page')).toBeInTheDocument();
  expect(screen.getByTestId('location')).toHaveTextContent('/lapsed-customers');
  expect(mockAuth.hasPermission).toHaveBeenCalledWith('can_manage_lapsed_customers');
});

it('sends anyone else to the dashboard, replacing the entry', async () => {
  grants.add('can_view_admin_panel');

  renderAt('/lapsed-customers');

  expect(await screen.findByText('dashboard-page')).toBeInTheDocument();
  expect(screen.queryByText('lapsed-customers-page')).toBeNull();
  expect(screen.getByTestId('location')).toHaveTextContent('/dashboard');
});
