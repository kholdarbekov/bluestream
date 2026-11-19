import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import AdminLayout from '../../components/layout/AdminLayout';
import driverExpenseService from '../../services/driverExpenseService';

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
  default: {
    getReorderReminderSummary: vi.fn().mockResolvedValue({ counts: { follow_up: 0 } }),
    listSharedPlaceCases: vi.fn().mockResolvedValue({ open_count: 0 }),
  },
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
vi.mock('../../services/driverExpenseService', () => ({
  __esModule: true,
  default: { list: vi.fn() },
}));

const renderLayout = () => render(
  <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter initialEntries={['/delivery/reports']}>
      <AdminLayout><div>content</div></AdminLayout>
    </MemoryRouter>
  </QueryClientProvider>,
);

const openDelivery = () => fireEvent.click(screen.getByText('ui.nav.delivery'));
const item = () => document.querySelector('[data-menu-id$="/delivery/expenses"]');

beforeEach(() => {
  vi.clearAllMocks();
  mockAuth.hasPermission.mockReturnValue(true);
  driverExpenseService.list.mockResolvedValue({ items: [], pending_count: 3 });
});

it('shows the item with the waiting count to reviewers, after Reports', async () => {
  renderLayout();
  openDelivery();

  await waitFor(() => expect(item()).toHaveTextContent('Driver expenses 3'));
  expect(mockAuth.hasPermission).toHaveBeenCalledWith('can_review_driver_expenses');
  expect(driverExpenseService.list).toHaveBeenCalledWith({ status: 'pending', perPage: 1 });
  const children = [...document.querySelectorAll('[data-menu-id*="/delivery"]')]
    .map((node) => node.getAttribute('data-menu-id').replace(/^.*?(\/delivery[\w/-]*)$/, '$1'));
  expect(children.indexOf('/delivery/expenses')).toBeGreaterThan(children.indexOf('/delivery/reports'));
});

it('hides the item and never asks for the count without the permission', async () => {
  mockAuth.hasPermission.mockImplementation((permission) => permission !== 'can_review_driver_expenses');
  renderLayout();
  openDelivery();

  await waitFor(() => expect(document.querySelector('[data-menu-id$="/delivery/reports"]')).toBeTruthy());
  expect(item()).toBeNull();
  expect(driverExpenseService.list).not.toHaveBeenCalled();
});
