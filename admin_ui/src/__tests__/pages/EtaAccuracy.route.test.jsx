/**
 * The /delivery/eta-accuracy route sits behind `PermissionGuard permission="can_view_eta_accuracy"` (R16). The route
 * table is driven for real (App.js, ProtectedRoute and PermissionGuard); the pages are markers.
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
vi.mock('../../pages/EtaAccuracy', () => ({ default: () => <div>eta-accuracy-page</div> }));
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

it('shows the report to a user who holds can_view_eta_accuracy', async () => {
  grants.add('can_view_eta_accuracy');

  renderAt('/delivery/eta-accuracy');

  expect(await screen.findByText('eta-accuracy-page')).toBeInTheDocument();
  expect(mockAuth.hasPermission).toHaveBeenCalledWith('can_view_eta_accuracy');
});

it('sends anyone else to the dashboard, replacing the entry', async () => {
  grants.add('can_view_admin_panel');

  renderAt('/delivery/eta-accuracy');

  expect(await screen.findByText('dashboard-page')).toBeInTheDocument();
  expect(screen.queryByText('eta-accuracy-page')).toBeNull();
  expect(screen.getByTestId('location')).toHaveTextContent('/dashboard');
});
