import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { BrowserRouter } from 'react-router-dom';

import Users from '../../pages/Users';
import adminService from '../../services/adminService';
import staffService from '../../services/staffService';

vi.mock('../../services/adminService');
vi.mock('../../services/staffService');

vi.mock('antd', async () => {
  const actual = await vi.importActual('antd');
  return {
    ...actual,
    Dropdown: ({ menu, children }) => (
      <div>
        {children}
        {menu?.items
          ?.filter((item) => item && item.type !== 'divider' && item.onClick)
          .map((item) => (
            <button
              key={item.key}
              disabled={item.disabled}
              onClick={item.onClick}
              type="button"
            >
              {typeof item.label === 'string' ? item.label : item.key}
            </button>
          ))}
      </div>
    ),
  };
});

vi.mock('../../components/AddressMapPicker', () => ({
  default: () => <div data-testid="address-map-picker" />,
}));

vi.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    get: vi.fn(),
  },
}));

vi.mock('../../hooks/useResponsive', () => ({
  default: () => ({
    isMobileDevice: false,
    isTabletDevice: false,
    isTouchDevice: false,
    getFontSize: (mobile, _tablet, desktop) => desktop || mobile,
  }),
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key, fallback) => fallback || key,
  }),
}));

const createWrapper = () => {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
      },
    },
  });

  return ({ children }) => (
    <BrowserRouter>
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    </BrowserRouter>
  );
};

// Detailed behavior lives in the component tests that exercise the extracted
// components directly — LinkedAccountsPanel.test.jsx (identity linking) and
// PlaceGroupPanel.test.jsx (same-physical-place groups). This file keeps thin
// smoke tests confirming Users.js still wires BOTH panels into the details
// modal for the selected user.
describe('Users linked-accounts panel', () => {
  beforeEach(() => {
    vi.clearAllMocks();

    adminService.getUsers.mockResolvedValue({
      data: {
        items: [
          {
            id: 11,
            first_name: 'Alice',
            last_name: 'Tester',
            email: 'alice@example.com',
            phone: '+998901234567',
            status: 'active',
            role: 'customer',
            user_type: 'individual',
            telegram_id: '998901234567',
            is_bot_active: true,
            created_at: '2026-03-01T10:00:00+00:00',
            last_login: '2026-03-02T10:00:00+00:00',
          },
        ],
      },
      meta: {
        total: 1,
        page: 1,
        per_page: 20,
      },
    });

    adminService.getUserAddresses.mockResolvedValue({
      data: {
        addresses: [],
      },
    });

    adminService.getUserNotificationSettings.mockResolvedValue({
      data: {
        notification_settings: {
          delivery_telegram_status_updates_enabled: true,
          delivery_telegram_status_updates_source: 'default',
          telegram_connected: true,
          bot_active: true,
          updated_at: null,
        },
      },
    });

    adminService.getUserCart.mockResolvedValue({ data: null });

    adminService.getLinkedAccounts.mockResolvedValue({
      data: {
        canonical_customer_id: 5,
        primary_user_id: 11,
        members: [
          { id: 11, first_name: 'Alice', last_name: 'Tester', phone: '+998900000001' },
          { id: 12, first_name: 'Alice2', last_name: 'Tester2', phone: '+998900000002' },
        ],
      },
    });

    staffService.getCustomerCodStatement.mockResolvedValue({
      data: {
        data: {
          active_cod_debt_count: 0,
          total_outstanding_amount: 0,
          cod_restricted: false,
          items: [],
        },
      },
    });

    adminService.getPlaceGroupSuggestions.mockResolvedValue({
      data: { suggestions: [] },
    });

    staffService.getCustomerPrepaymentHistory.mockResolvedValue({
      data: {
        data: null,
      },
    });
  });

  it('loads and shows linked members when the details modal opens', async () => {
    const user = userEvent.setup();

    render(<Users />, { wrapper: createWrapper() });

    await waitFor(() => {
      expect(adminService.getUsers).toHaveBeenCalled();
    });

    await screen.findByText('alice@example.com');
    await user.click(screen.getByText('ui.users.view_details'));

    await waitFor(() => {
      expect(adminService.getLinkedAccounts).toHaveBeenCalledWith(11);
    });

    expect(await screen.findByText(/\+998900000002/)).toBeInTheDocument();
  });

  it('shows a "not linked" message when the account has no linked members', async () => {
    adminService.getLinkedAccounts.mockResolvedValue({
      data: {
        canonical_customer_id: 5,
        primary_user_id: 11,
        members: [
          { id: 11, first_name: 'Alice', last_name: 'Tester', phone: '+998900000001' },
        ],
      },
    });

    const user = userEvent.setup();

    render(<Users />, { wrapper: createWrapper() });

    await screen.findByText('alice@example.com');
    await user.click(screen.getByText('ui.users.view_details'));

    await waitFor(() => {
      expect(adminService.getLinkedAccounts).toHaveBeenCalledWith(11);
    });

    expect(
      await screen.findByText('Not linked to any other account')
    ).toBeInTheDocument();
  });

  it('also wires the place-group panel into the details modal', async () => {
    const user = userEvent.setup();

    render(<Users />, { wrapper: createWrapper() });

    await screen.findByText('alice@example.com');
    await user.click(screen.getByText('ui.users.view_details'));

    await waitFor(() => {
      expect(adminService.getUserAddresses).toHaveBeenCalledWith(11);
    });

    expect(
      await screen.findByText('Place groups (same physical place)')
    ).toBeInTheDocument();

    // Opening the drawer must NOT trigger the co-location scan. It clusters the
    // full ungrouped estate on every call, uncached, and deliberately cannot be
    // narrowed (a bbox truncates transitive components and voids dismissals —
    // plan E19), so it is opt-in behind an explicit button. This drawer is
    // opened for every customer an admin looks at, for any reason.
    expect(adminService.getPlaceGroupSuggestions).not.toHaveBeenCalled();

    await user.click(
      await screen.findByRole('button', { name: /Find possible same-place matches/ })
    );
    await waitFor(() => {
      expect(adminService.getPlaceGroupSuggestions).toHaveBeenCalledWith(11);
    });
  });
});
