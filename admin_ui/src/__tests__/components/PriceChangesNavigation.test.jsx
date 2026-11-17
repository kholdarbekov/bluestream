import React from 'react';
import { render } from '@testing-library/react';
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

const renderLayout = () => render(
  <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter initialEntries={['/dashboard']}>
      <AdminLayout><div>content</div></AdminLayout>
    </MemoryRouter>
  </QueryClientProvider>,
);

const topLevelKeys = () => [...document.querySelectorAll('.ant-menu-root > [data-menu-id]')]
  .map((item) => item.getAttribute('data-menu-id').replace(/^.*?(\/[\w-]+)$/, '$1'));

beforeEach(() => {
  vi.clearAllMocks();
  mockAuth.hasPermission.mockReturnValue(true);
});

it('lists Price changes right after Products for a holder of can_view_price_changes', () => {
  renderLayout();

  const item = document.querySelector('[data-menu-id$="/price-changes"]');
  expect(item).toHaveTextContent('Price changes');
  expect(mockAuth.hasPermission).toHaveBeenCalledWith('can_view_price_changes');
  const keys = topLevelKeys();
  expect(keys.indexOf('/price-changes')).toBe(keys.indexOf('/products') + 1);
});

it('hides the menu item without the permission, and keeps Products', () => {
  mockAuth.hasPermission.mockImplementation((permission) => permission !== 'can_view_price_changes');
  renderLayout();

  expect(document.querySelector('[data-menu-id$="/products"]')).toBeTruthy();
  expect(document.querySelector('[data-menu-id$="/price-changes"]')).toBeNull();
});
