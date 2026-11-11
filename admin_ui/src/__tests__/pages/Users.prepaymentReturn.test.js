import React from 'react';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { BrowserRouter } from 'react-router-dom';

import Users from '../../pages/Users';
import adminService from '../../services/adminService';
import { PAYOUT_HANDLED_ERROR_CODES } from '../../utils/cashScopeDisplay';
import staffService from '../../services/staffService';

vi.mock('../../services/adminService');
vi.mock('../../services/staffService');

vi.mock('../../components/AddressMapPicker', () => ({
  default: () => <div data-testid="address-map-picker" />,
}));

vi.mock('../../services/api', () => ({
  __esModule: true,
  default: { get: vi.fn() },
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
    t: (key, fallback, options) => {
      const text = typeof fallback === 'string' ? fallback : key;
      return text.replace(/\{\{(\w+)\}\}/g, (_, name) => String(options?.[name] ?? ''));
    },
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
            <button key={item.key} disabled={item.disabled} onClick={item.onClick} type="button">
              {typeof item.label === 'string' ? item.label : item.key}
            </button>
          ))}
      </div>
    ),
    message: {
      success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn(), loading: vi.fn(), destroy: vi.fn(), open: vi.fn(),
    },
  };
});

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

const historyWith = (extra) => ({
  data: {
    data: {
      customer_id: 11,
      available_prepayment_balance: 50000,
      events: [],
      payouts: [],
      can_return_prepayment: true,
      ...extra,
    },
  },
});

const wrapper = ({ children }) => (
  <BrowserRouter>
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      {children}
    </QueryClientProvider>
  </BrowserRouter>
);

const openDetails = async (user) => {
  render(<Users />, { wrapper });
  await screen.findByText('alice@example.com');
  await user.click(screen.getByText('ui.users.view_details'));
};

beforeEach(() => {
  vi.clearAllMocks();
  adminService.getUsers.mockResolvedValue({ data: { items: [baseUser] }, meta: { total: 1, page: 1, per_page: 20 } });
  adminService.getUserAddresses.mockResolvedValue({ data: { addresses: [] } });
  adminService.getUserNotificationSettings.mockResolvedValue({
    data: { notification_settings: { telegram_connected: true, bot_active: true } },
  });
  staffService.getCustomerCodStatement.mockResolvedValue({ data: { data: { items: [] } } });
  staffService.getDeliveryPersons.mockResolvedValue({ data: { data: { items: [] } } });
});

describe('Users prepayment card: returning prepayment', () => {
  it('hides Return prepayment when can_return_prepayment is false', async () => {
    staffService.getCustomerPrepaymentHistory.mockResolvedValue(historyWith({ can_return_prepayment: false }));
    await openDetails(userEvent.setup());
    await waitFor(() => expect(staffService.getCustomerPrepaymentHistory).toHaveBeenCalled());
    await screen.findByText('View full ledger', { exact: false });
    expect(screen.queryByRole('button', { name: 'Return prepayment' })).toBeNull();
  });

  it('shows Return prepayment beside the ledger link and renders returned rows', async () => {
    staffService.getCustomerPrepaymentHistory.mockResolvedValue(historyWith({
      payouts: [{
        id: 9, customer_id: 11, amount: 20000, paid_by: 'office', paid_at: '2026-11-10T09:00:00+05:00',
        reason: 'Customer moved away', voided_at: null, can_undo: true, undo_block_code: null, debits: [],
        card_click_amount: 0, revenue_counted_amount: 0,
      }],
    }));
    await openDetails(userEvent.setup());
    expect(await screen.findByRole('button', { name: 'Return prepayment' })).toBeInTheDocument();
    const rows = await screen.findByTestId('payout-rows');
    expect(within(rows).getByRole('button', { name: 'Undo' })).toBeInTheDocument();
  });

  it('refetches the history and the COD statement after an undo', async () => {
    staffService.getCustomerPrepaymentHistory.mockResolvedValue(historyWith({
      payouts: [{
        id: 9, customer_id: 11, amount: 20000, paid_by: 'office', paid_at: '2026-11-10T09:00:00+05:00',
        reason: 'Customer moved away', voided_at: null, can_undo: true, undo_block_code: null, debits: [],
        card_click_amount: 0, revenue_counted_amount: 0,
      }],
    }));
    staffService.undoPrepaymentPayout.mockResolvedValue({ data: { data: {} } });
    const user = userEvent.setup();
    await openDetails(user);
    const rows = await screen.findByTestId('payout-rows');
    await user.click(within(rows).getByRole('button', { name: 'Undo' }));
    await user.type(await screen.findByLabelText('Reason'), 'Recorded twice');
    const historyCalls = staffService.getCustomerPrepaymentHistory.mock.calls.length;
    const statementCalls = staffService.getCustomerCodStatement.mock.calls.length;
    await user.click(within(screen.getByText('Undo this return').closest('.ant-modal-content')).getByRole('button', { name: 'Undo' }));
    await waitFor(() => expect(staffService.undoPrepaymentPayout).toHaveBeenCalledWith(
      9, { reason: 'Recorded twice' }, { handledErrorCodes: PAYOUT_HANDLED_ERROR_CODES },
    ));
    await waitFor(() => {
      expect(staffService.getCustomerPrepaymentHistory.mock.calls.length).toBeGreaterThan(historyCalls);
      expect(staffService.getCustomerCodStatement.mock.calls.length).toBeGreaterThan(statementCalls);
    });
  });

  it('tags a drained event Returned', async () => {
    staffService.getCustomerPrepaymentHistory.mockResolvedValue(historyWith({
      events: [{
        id: 1, occurred_at: '2026-11-01T09:00:00+00:00', source: 'delivery_completion', amount: 30000,
        unapplied_amount: 0, returned_amount: 30000, order_number: 'AD_5', notes: null, allocations: [],
      }, {
        id: 2, occurred_at: '2026-11-02T09:00:00+00:00', source: 'delivery_completion', amount: 5000,
        unapplied_amount: 5000, returned_amount: 0, order_number: 'AD_6', notes: null, allocations: [],
      }],
    }));
    await openDetails(userEvent.setup());
    const drained = (await screen.findByText('AD_5')).closest('tr');
    expect(within(drained).getByText('Returned')).toBeInTheDocument();
    const untouched = (await screen.findByText('AD_6')).closest('tr');
    expect(within(untouched).queryByText('Returned')).toBeNull();
  });
});
