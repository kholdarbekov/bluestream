import React from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import AdminLayout from '../../components/layout/AdminLayout';

const mockAuth = {
  isAuthenticated: true,
  isLoading: false,
  initialize: vi.fn().mockResolvedValue(undefined),
  hasPermission: vi.fn(() => true),
  getUserRole: vi.fn(() => 'manager'),
  user: { first_name: 'Malika', last_name: 'Menejer', role: 'manager' },
  logout: vi.fn(),
};
vi.mock('../../stores/authStore', () => ({ useAuthStore: () => mockAuth }));
vi.mock('../../hooks/useRealTimeUpdates', () => ({
  useRealTimeWithFallback: () => ({ isConnected: true, connectionType: 'websocket' }),
  useRealTimeUpdates: () => ({ isConnected: true }),
  usePollingUpdates: () => ({ isConnected: true }),
  default: () => ({ isConnected: true }),
}));
vi.mock('../../components/common/LanguageSwitcher', () => ({ default: () => <div data-testid="lang-switcher" /> }));
vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key, opts) => (typeof opts === 'string' ? opts : opts?.defaultValue) || key }),
}));
vi.mock('../../services/adminService', () => ({
  __esModule: true,
  default: { getReorderReminderSummary: vi.fn().mockResolvedValue({ counts: { follow_up: 0 } }) },
}));
vi.mock('../../services/salesService', () => ({
  __esModule: true,
  default: { listOrderApprovals: vi.fn().mockResolvedValue({ pending_count: 0 }) },
  ORDER_APPROVAL_HANDLED_CODES: [],
}));
vi.mock('../../services/salesPayService', () => ({
  __esModule: true,
  default: { getPeriods: vi.fn().mockResolvedValue({ pending_penalty_count: 0 }) },
}));

const renderLayout = () => render(
  <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter initialEntries={['/dashboard']}>
      <AdminLayout><div>content</div></AdminLayout>
    </MemoryRouter>
  </QueryClientProvider>,
);

const openDelivery = () => fireEvent.click(screen.getByText('ui.nav.delivery'));
const etaItem = () => document.querySelector('[data-menu-id$="/delivery/eta-accuracy"]');
const deliveryChildren = () => [...document.querySelectorAll('[data-menu-id*="/delivery"]')]
  .map((item) => item.getAttribute('data-menu-id').replace(/^.*?(\/delivery.*)$/, '$1'));

beforeEach(() => {
  vi.clearAllMocks();
  mockAuth.hasPermission.mockReturnValue(true);
});

it('lists Delivery time accuracy under Delivery, right after Reports, for a holder of can_view_eta_accuracy', () => {
  renderLayout();
  openDelivery();

  expect(etaItem()).toHaveTextContent('Delivery time accuracy');
  expect(mockAuth.hasPermission).toHaveBeenCalledWith('can_view_eta_accuracy');
  const keys = deliveryChildren();
  expect(keys.indexOf('/delivery/eta-accuracy')).toBe(keys.indexOf('/delivery/reports') + 1);
});

it('hides the menu item without the permission, and keeps the rest of the Delivery menu', () => {
  mockAuth.hasPermission.mockImplementation((permission) => permission !== 'can_view_eta_accuracy');
  renderLayout();
  openDelivery();

  expect(document.querySelector('[data-menu-id$="/delivery/reports"]')).toBeTruthy();
  expect(etaItem()).toBeNull();
});
