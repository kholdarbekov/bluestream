/**
 * The /price-changes route sits behind `PermissionGuard permission="can_view_price_changes"` (price-change spec §5.2).
 * The route table is driven for real (App.js, ProtectedRoute and PermissionGuard); the pages are markers.
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
vi.mock('../../pages/PriceChanges', () => ({ default: () => <div>price-changes-page</div> }));
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

it('shows the page to a holder of can_view_price_changes', async () => {
  grants.add('can_view_price_changes');

  renderAt('/price-changes');

  expect(await screen.findByText('price-changes-page')).toBeInTheDocument();
  expect(screen.getByTestId('location')).toHaveTextContent('/price-changes');
  expect(mockAuth.hasPermission).toHaveBeenCalledWith('can_view_price_changes');
});

it('sends anyone else to the dashboard, replacing the entry', async () => {
  grants.add('can_view_admin_panel');

  renderAt('/price-changes');

  expect(await screen.findByText('dashboard-page')).toBeInTheDocument();
  expect(screen.queryByText('price-changes-page')).toBeNull();
  expect(screen.getByTestId('location')).toHaveTextContent('/dashboard');
});
