import React from 'react';
import { act, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, useLocation } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import App from '../../App';
import AdminLayout from '../../components/layout/AdminLayout';
import adminService from '../../services/adminService';
import { LAPSED_CUSTOMERS_SUMMARY_KEY } from '../../utils/constants';

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
vi.mock('../../pages/LapsedCustomers', () => ({ default: () => <div>lapsed-customers-page</div> }));
vi.mock('../../pages/Dashboard', () => ({ default: () => <div>dashboard-page</div> }));
vi.mock('../../services/adminService', () => ({
  __esModule: true,
  default: { getReorderReminderSummary: vi.fn() },
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

const LocationProbe = () => <div data-testid="location">{useLocation().pathname}</div>;

// The client is returned so a test can read the summary's cache entry by its shared key.
const renderLayout = () => {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={['/dashboard']}>
        <AdminLayout><div>content</div></AdminLayout>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return queryClient;
};

const topLevelKeys = () => [...document.querySelectorAll('.ant-menu-root > li[data-menu-id]')]
  .map((item) => item.getAttribute('data-menu-id').replace(/^.*?(\/[^/].*)$/, '$1'));

const lapsedItem = () => document.querySelector('[data-menu-id$="/lapsed-customers"]');

beforeEach(() => {
  vi.clearAllMocks();
  mockAuth.hasPermission.mockReturnValue(true);
  adminService.getReorderReminderSummary.mockResolvedValue({ counts: { follow_up: 4, reminding: 9 } });
});

afterEach(() => {
  vi.useRealTimers();
});

it('puts Lapsed customers right after Users, badged with the follow-up count', async () => {
  renderLayout();

  const item = lapsedItem();
  expect(item).toHaveTextContent('Lapsed customers');
  const keys = topLevelKeys();
  expect(keys.indexOf('/lapsed-customers')).toBe(keys.indexOf('/users') + 1);
  await waitFor(() => expect(item.querySelector('.ant-badge-count')).toHaveAttribute('title', '4'));
  expect(adminService.getReorderReminderSummary).toHaveBeenCalledTimes(1);
});

it('draws no item and never asks for the summary without can_manage_lapsed_customers', async () => {
  mockAuth.hasPermission.mockImplementation((permission) => permission !== 'can_manage_lapsed_customers');
  renderLayout();

  await new Promise((resolve) => { setTimeout(resolve, 0); });
  expect(lapsedItem()).toBeNull();
  expect(document.querySelector('[data-menu-id$="/users"]')).toBeTruthy();
  expect(adminService.getReorderReminderSummary).not.toHaveBeenCalled();
});

it('serves /lapsed-customers to a holder of the flag and sends anyone else to the dashboard', async () => {
  const { unmount } = render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter initialEntries={['/lapsed-customers']}><App /><LocationProbe /></MemoryRouter>
    </QueryClientProvider>,
  );
  expect(await screen.findByText('lapsed-customers-page')).toBeInTheDocument();
  unmount();

  mockAuth.hasPermission.mockImplementation((permission) => permission !== 'can_manage_lapsed_customers');
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter initialEntries={['/lapsed-customers']}><App /><LocationProbe /></MemoryRouter>
    </QueryClientProvider>,
  );
  expect(await screen.findByText('dashboard-page')).toBeInTheDocument();
  await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/dashboard'));
});

it('reads the summary under the page\'s shared key and polls it every 60 seconds, and not sooner', async () => {
  vi.useFakeTimers();
  const queryClient = renderLayout();
  const summary = adminService.getReorderReminderSummary;

  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  expect(summary).toHaveBeenCalledTimes(1);
  expect(queryClient.getQueryCache().find({ queryKey: LAPSED_CUSTOMERS_SUMMARY_KEY })).toBeDefined();

  await act(async () => { await vi.advanceTimersByTimeAsync(59_999); });
  expect(summary).toHaveBeenCalledTimes(1);

  await act(async () => { await vi.advanceTimersByTimeAsync(1); });
  expect(summary).toHaveBeenCalledTimes(2);

  await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
  expect(summary).toHaveBeenCalledTimes(3);
});

it('hides the badge, and keeps the menu, when the summary read fails', async () => {
  adminService.getReorderReminderSummary.mockRejectedValue(new Error('summary unavailable'));
  const queryClient = renderLayout();

  await waitFor(() => expect(queryClient.getQueryState(LAPSED_CUSTOMERS_SUMMARY_KEY)?.status).toBe('error'));
  const item = lapsedItem();
  expect(item).toHaveTextContent('Lapsed customers');
  expect(item.querySelector('.ant-badge-count')).toBeNull();
  expect(document.querySelector('[data-menu-id$="/users"]')).toBeTruthy();
  expect(document.querySelector('[data-menu-id$="/orders"]')).toBeTruthy();
  expect(screen.getByText('content')).toBeInTheDocument();
});
