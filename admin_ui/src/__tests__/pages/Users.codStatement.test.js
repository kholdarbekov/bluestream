import React from 'react';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { BrowserRouter } from 'react-router-dom';

import Users from '../../pages/Users';
import adminService from '../../services/adminService';
import staffService from '../../services/staffService';

vi.mock('../../services/adminService');
vi.mock('../../services/staffService');

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
    message: {
      success: vi.fn(),
      error: vi.fn(),
      info: vi.fn(),
      warning: vi.fn(),
      loading: vi.fn(),
      destroy: vi.fn(),
      open: vi.fn(),
    },
  };
});

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

const baseUser = {
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
};

const setupBaseMocks = () => {
  vi.clearAllMocks();

  adminService.getUsers.mockResolvedValue({
    data: { items: [baseUser] },
    meta: { total: 1, page: 1, per_page: 20 },
  });

  adminService.getUserAddresses.mockResolvedValue({ data: { addresses: [] } });
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
  adminService.updateUserStatus.mockResolvedValue({});
  adminService.createUser.mockResolvedValue({});
  adminService.updateUser.mockResolvedValue({});
  adminService.createUserAddress.mockResolvedValue({});
  adminService.updateUserAddress.mockResolvedValue({});
  adminService.deleteUserAddress.mockResolvedValue({});
  adminService.unlockUserAccount.mockResolvedValue({});

  staffService.getCustomerPrepaymentHistory = vi.fn().mockResolvedValue({
    data: { data: { events: [] } },
  });
};

const openUserDetails = async (user) => {
  render(<Users />, { wrapper: createWrapper() });
  await waitFor(() => {
    expect(adminService.getUsers).toHaveBeenCalled();
  });
  await screen.findByText('alice@example.com');
  await user.click(screen.getByText('ui.users.view_details'));
};

describe('Users page COD statement enrichment', () => {
  beforeEach(() => {
    setupBaseMocks();
  });

  it('renders Reserved and Net outstanding columns plus enriched summary when API returns new fields', async () => {
    const user = userEvent.setup();

    staffService.getCustomerCodStatement.mockResolvedValue({
      data: {
        data: {
          active_cod_debt_count: 2,
          cod_restricted: false,
          gross_outstanding_amount: 50000,
          total_outstanding_amount: 50000,
          reserved_prepayment_total: 20000,
          net_outstanding_amount: 30000,
          unreserved_prepayment_balance: 5000,
          available_prepayment_balance: 5000,
          items: [
            {
              payment_id: 1,
              order_id: 111,
              order_number: 'ORD-111',
              status: 'pending',
              amount: 40000,
              amount_collected: 10000,
              outstanding_amount: 30000,
              reserved_prepayment_amount: 20000,
              net_outstanding_amount: 10000,
            },
            {
              payment_id: 2,
              order_id: 222,
              order_number: 'ORD-222',
              status: 'pending',
              amount: 20000,
              amount_collected: 0,
              outstanding_amount: 20000,
              reserved_prepayment_amount: 0,
              net_outstanding_amount: 20000,
            },
          ],
        },
      },
    });

    await openUserDetails(user);

    await waitFor(() => {
      expect(staffService.getCustomerCodStatement).toHaveBeenCalledWith(11);
    });

    // Summary row 2 — cash position
    expect(await screen.findByText('Gross outstanding:')).toBeInTheDocument();
    expect(screen.getByText('Reserved from prepayment:')).toBeInTheDocument();
    expect(screen.getByText('Net outstanding:')).toBeInTheDocument();
    expect(screen.getByText('Prepayment balance (unreserved):')).toBeInTheDocument();

    // Column headers
    expect(screen.getByRole('columnheader', { name: 'Reserved' })).toBeInTheDocument();
    expect(screen.getByRole('columnheader', { name: 'Net outstanding' })).toBeInTheDocument();

    // Per-row values — first row has amount 40000, collected 10000, reserved 20000, net 10000
    const rowOne = screen.getByRole('row', { name: /ORD-111/ });
    const cellsOne = within(rowOne).getAllByRole('cell');
    // [0]=order, [1]=status, [2]=amount, [3]=collected, [4]=reserved, [5]=net
    expect(cellsOne[4]).toHaveTextContent('20,000 UZS');
    expect(cellsOne[5]).toHaveTextContent('10,000 UZS');

    // Second row: reserved 0, net 20000
    const rowTwo = screen.getByRole('row', { name: /ORD-222/ });
    const cellsTwo = within(rowTwo).getAllByRole('cell');
    expect(cellsTwo[4]).toHaveTextContent('0 UZS');
    expect(cellsTwo[5]).toHaveTextContent('20,000 UZS');
  });

  it('renders net outstanding as 0 when reserved fully covers a row', async () => {
    const user = userEvent.setup();

    staffService.getCustomerCodStatement.mockResolvedValue({
      data: {
        data: {
          active_cod_debt_count: 1,
          cod_restricted: false,
          gross_outstanding_amount: 15000,
          total_outstanding_amount: 15000,
          reserved_prepayment_total: 15000,
          net_outstanding_amount: 0,
          unreserved_prepayment_balance: 0,
          available_prepayment_balance: 0,
          items: [
            {
              payment_id: 3,
              order_id: 333,
              order_number: 'ORD-333',
              status: 'pending',
              amount: 15000,
              amount_collected: 0,
              outstanding_amount: 15000,
              reserved_prepayment_amount: 15000,
              net_outstanding_amount: 0,
            },
          ],
        },
      },
    });

    await openUserDetails(user);

    const row = await screen.findByRole('row', { name: /ORD-333/ });
    const cells = within(row).getAllByRole('cell');
    // Reserved (idx 4) = 15,000 UZS, Net (idx 5) = 0 UZS
    expect(cells[4]).toHaveTextContent('15,000 UZS');
    expect(cells[5]).toHaveTextContent('0 UZS');

    // Summary row: Net outstanding should also display 0 UZS
    const netLabel = screen.getByText(/Net outstanding:/i);
    const netSummaryCol = netLabel.closest('.ant-col') || netLabel.parentElement;
    expect(netSummaryCol).toHaveTextContent('0 UZS');
  });

  it('falls back gracefully when API has not yet been redeployed (legacy payload)', async () => {
    const user = userEvent.setup();

    staffService.getCustomerCodStatement.mockResolvedValue({
      data: {
        data: {
          active_cod_debt_count: 1,
          cod_restricted: false,
          total_outstanding_amount: 25000,
          available_prepayment_balance: 7000,
          items: [
            {
              payment_id: 4,
              order_id: 444,
              order_number: 'ORD-444',
              status: 'pending',
              amount: 25000,
              amount_collected: 0,
              outstanding_amount: 25000,
            },
          ],
        },
      },
    });

    await openUserDetails(user);

    // Card should still render; new headers should still be present
    expect(await screen.findByRole('columnheader', { name: 'Reserved' })).toBeInTheDocument();
    expect(screen.getByRole('columnheader', { name: 'Net outstanding' })).toBeInTheDocument();

    const row = screen.getByRole('row', { name: /ORD-444/ });
    const cells = within(row).getAllByRole('cell');
    // Reserved (idx 4) defaults to 0 UZS, Net (idx 5) falls back to gross
    expect(cells[4]).toHaveTextContent('0 UZS');
    expect(cells[5]).toHaveTextContent('25,000 UZS');

    // Summary "Net outstanding" should display 0 UZS (not gross) when
    // net_outstanding_amount is missing, to avoid misleading the admin.
    const netLabel = screen.getByText(/Net outstanding:/i);
    const netSummaryCol = netLabel.closest('.ant-col') || netLabel.parentElement;
    expect(netSummaryCol).toHaveTextContent('0 UZS');
  });
});
