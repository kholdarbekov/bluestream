import React from 'react';
import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, useLocation } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import AdminLayout from '../../components/layout/AdminLayout';
import adminService from '../../services/adminService';
import { SHARED_PLACE_OPEN_COUNT_KEY } from '../../utils/constants';

const mockAuth = {
  isAuthenticated: true,
  isLoading: false,
  initialize: vi.fn().mockResolvedValue(undefined),
  hasPermission: vi.fn(() => true),
  getUserRole: vi.fn(() => 'admin'),
  user: { first_name: 'Ada', last_name: 'Admin', role: 'admin' },
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
    listSharedPlaceCases: vi.fn(),
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

const LocationProbe = () => {
  const location = useLocation();
  return <div data-testid="location">{`${location.pathname}${location.search}`}</div>;
};

const renderLayout = () => {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={['/dashboard']}>
        <AdminLayout><div>content</div></AdminLayout>
        <LocationProbe />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return queryClient;
};

const usersItem = () => document.querySelector('[data-menu-id$="/users"]');

// The list read's own shape (review.py list_cases): the badge reads only open_count.
const openPage = (openCount) => ({
  cases: [],
  open_count: openCount,
  page: 1,
  per_page: 1,
  total: openCount,
  statuses: ['awaiting_driver', 'awaiting_admin', 'resolved', 'closed'],
});

beforeEach(() => {
  vi.clearAllMocks();
  mockAuth.hasPermission.mockReturnValue(true);
  adminService.listSharedPlaceCases.mockResolvedValue(openPage(4));
});

afterEach(() => {
  vi.useRealTimers();
});

it('badges the Users item with the open case count, read once under the shared key', async () => {
  const queryClient = renderLayout();

  const item = usersItem();
  expect(item).toHaveTextContent('ui.nav.users');
  await waitFor(() => expect(item.querySelector('.ant-badge-count')).toHaveAttribute('title', '4'));
  expect(adminService.listSharedPlaceCases.mock.calls).toEqual([[{ status: 'open', perPage: 1 }]]);
  expect(queryClient.getQueryData(SHARED_PLACE_OPEN_COUNT_KEY)).toEqual(openPage(4));
});

it('without can_review_shared_places: no badge and no fetch, and the item stays', async () => {
  mockAuth.hasPermission.mockImplementation((permission) => permission !== 'can_review_shared_places');
  renderLayout();

  await new Promise((resolve) => { setTimeout(resolve, 0); });
  const item = usersItem();
  expect(item).toHaveTextContent('ui.nav.users');
  expect(item.querySelector('.ant-badge')).toBeNull();
  expect(adminService.listSharedPlaceCases).not.toHaveBeenCalled();
});

it('a badge click opens the Driver reports tab; the menu key stays /users', async () => {
  const user = userEvent.setup();
  renderLayout();

  const item = usersItem();
  const badge = await waitFor(() => {
    const count = item.querySelector('.ant-badge-count');
    expect(count).not.toBeNull();
    return count;
  });
  await user.click(badge);

  expect(screen.getByTestId('location')).toHaveTextContent('/users?tab=driver-reports');
  expect(item.getAttribute('data-menu-id')).toMatch(/\/users$/);
});

it('a click on the Users label still opens the plain /users page', async () => {
  const user = userEvent.setup();
  renderLayout();

  await user.click(screen.getByText('ui.nav.users'));
  expect(screen.getByTestId('location')).toHaveTextContent(/^\/users$/);
});

it('polls the open count every 60 seconds, and not sooner', async () => {
  vi.useFakeTimers();
  renderLayout();
  const list = adminService.listSharedPlaceCases;

  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  expect(list).toHaveBeenCalledTimes(1);

  await act(async () => { await vi.advanceTimersByTimeAsync(59_999); });
  expect(list).toHaveBeenCalledTimes(1);

  await act(async () => { await vi.advanceTimersByTimeAsync(1); });
  expect(list).toHaveBeenCalledTimes(2);

  await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
  expect(list).toHaveBeenCalledTimes(3);
});
