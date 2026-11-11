import React from 'react';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';

import Prepayments from '../../pages/Prepayments';
import staffService from '../../services/staffService';
import { PAYOUT_HANDLED_ERROR_CODES } from '../../utils/cashScopeDisplay';

vi.mock('../../services/staffService');

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
    message: {
      success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn(), loading: vi.fn(), destroy: vi.fn(), open: vi.fn(),
    },
  };
});

const CUSTOMERS = {
  data: {
    data: {
      items: [{
        id: 43, first_name: 'Ali', last_name: 'Karimov', phone: '+998901111111', role: 'customer',
        available_prepayment_balance: 50000, last_collection_at: '2026-07-20T09:00:00+00:00', member_user_ids: [43],
      }],
      total: 1,
    },
  },
};

const payout = (overrides) => ({
  id: 9,
  customer_id: 43,
  amount: 20000,
  paid_by: 'office',
  paid_at: '2026-11-10T09:00:00+05:00',
  reason: 'Customer moved away',
  actor_name: 'Admin One',
  voided_at: null,
  void_reason: null,
  card_click_amount: 0,
  revenue_counted_amount: 0,
  can_undo: true,
  undo_block_code: null,
  debits: [],
  ...overrides,
});

const history = (overrides = {}) => ({
  data: {
    data: {
      customer_id: 43,
      first_name: 'Ali',
      last_name: 'Karimov',
      available_prepayment_balance: 50000,
      lifetime_collected: 400000,
      lifetime_applied: 250000,
      lifetime_returned: 30000,
      can_return_prepayment: true,
      cluster_member_ids: [43],
      payouts: [
        payout({ id: 9 }),
        payout({
          id: 8, amount: 10000, voided_at: '2026-11-10T10:00:00+05:00', void_reason: 'Recorded twice',
          can_undo: false, undo_block_code: 'already_undone',
        }),
      ],
      events: [
        {
          id: 1, occurred_at: '2026-11-01T09:00:00+00:00', source: 'delivery_completion', amount: 30000, unapplied_amount: 0,
          returned_amount: 30000, order_number: 'AD_5', notes: null, allocations: [],
        },
        {
          id: 2, occurred_at: '2026-11-02T09:00:00+00:00', source: 'delivery_completion', amount: 5000, unapplied_amount: 5000,
          returned_amount: 0, order_number: 'AD_6', notes: null, allocations: [],
        },
      ],
      ...overrides,
    },
  },
});

let queryClient;
const renderPage = () => {
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MemoryRouter initialEntries={['/staff/prepayments?customer_id=43']}>
      <QueryClientProvider client={queryClient}><Prepayments /></QueryClientProvider>
    </MemoryRouter>,
  );
};

beforeEach(() => {
  vi.clearAllMocks();
  staffService.listCustomersWithPrepaymentBalance.mockResolvedValue(CUSTOMERS);
  staffService.getCustomerPrepaymentHistory.mockResolvedValue(history());
  staffService.getDeliveryPersons.mockResolvedValue({ data: { data: { items: [] } } });
});

describe('Prepayments page: returning prepayment', () => {
  it('hides Return prepayment when can_return_prepayment is false', async () => {
    staffService.getCustomerPrepaymentHistory.mockResolvedValue(history({ can_return_prepayment: false }));
    renderPage();
    await screen.findByText('Lifetime returned');
    expect(screen.queryByRole('button', { name: 'Return prepayment' })).toBeNull();
  });

  it('shows the button, the lifetime_returned statistic and opens the modal', async () => {
    const user = userEvent.setup();
    renderPage();
    const stat = (await screen.findByText('Lifetime returned')).closest('.ant-statistic');
    expect(stat).toHaveTextContent('30,000');
    await user.click(screen.getByRole('button', { name: 'Return prepayment' }));
    expect(await screen.findByRole('button', { name: 'Preview' })).toBeInTheDocument();
  });

  it('renders returned rows and offers Undo only where can_undo', async () => {
    renderPage();
    const rows = await screen.findByTestId('payout-rows');
    expect(within(rows).getAllByRole('button', { name: 'Undo' })).toHaveLength(1);
    expect(within(rows).getByText('Undone')).toBeInTheDocument();
    expect(within(rows).getByText('Recorded twice')).toBeInTheDocument();
    const undoneRow = within(rows).getByText('Undone').closest('tr');
    expect(within(undoneRow).queryByRole('button', { name: 'Undo' })).toBeNull();
  });

  it('posts the exact undo body and refreshes the history and the list', async () => {
    staffService.undoPrepaymentPayout.mockResolvedValue({ data: { data: {} } });
    const user = userEvent.setup();
    renderPage();
    const rows = await screen.findByTestId('payout-rows');
    await user.click(within(rows).getByRole('button', { name: 'Undo' }));
    await user.type(await screen.findByLabelText('Reason'), 'Recorded twice');
    const historyCalls = staffService.getCustomerPrepaymentHistory.mock.calls.length;
    const customerCalls = staffService.listCustomersWithPrepaymentBalance.mock.calls.length;
    const dialog = screen.getByText('Undo this return').closest('.ant-modal-content');
    await user.click(within(dialog).getByRole('button', { name: 'Undo' }));
    await waitFor(() => expect(staffService.undoPrepaymentPayout).toHaveBeenCalledWith(
      9, { reason: 'Recorded twice' }, { handledErrorCodes: PAYOUT_HANDLED_ERROR_CODES },
    ));
    await waitFor(() => {
      expect(staffService.getCustomerPrepaymentHistory.mock.calls.length).toBeGreaterThan(historyCalls);
      expect(staffService.listCustomersWithPrepaymentBalance.mock.calls.length).toBeGreaterThan(customerCalls);
    });
  });

  it('tags a drained event Returned', async () => {
    renderPage();
    const drained = (await screen.findByText('AD_5')).closest('tr');
    expect(within(drained).getByText('Returned')).toBeInTheDocument();
    const untouched = screen.getByText('AD_6').closest('tr');
    expect(within(untouched).queryByText('Returned')).toBeNull();
  });

  it('offers no Undo, and no misleading reason, where can_undo is false only because of the viewer', async () => {
    staffService.getCustomerPrepaymentHistory.mockResolvedValue(history({
      can_return_prepayment: false,
      payouts: [payout({ id: 9, can_undo: false, undo_block_code: null })],
    }));
    renderPage();
    const rows = await screen.findByTestId('payout-rows');
    expect(within(rows).queryByRole('button', { name: 'Undo' })).toBeNull();
    expect(within(rows).queryByRole('img', { name: /info-circle/ })).toBeNull();
    expect(within(rows).queryByText('Undone')).toBeNull();
  });

  it('opens a fresh form at the balance of each open', async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole('button', { name: 'Return prepayment' }));
    expect(await screen.findByRole('spinbutton')).toHaveValue('50,000');
    await user.click(screen.getByRole('button', { name: 'Cancel' }));
    await waitFor(() => expect(screen.queryByRole('spinbutton')).toBeNull());

    staffService.getCustomerPrepaymentHistory.mockResolvedValue(history({ available_prepayment_balance: 20000 }));
    await queryClient.invalidateQueries();
    await waitFor(() => expect(staffService.getCustomerPrepaymentHistory).toHaveBeenCalledTimes(2));
    await user.click(await screen.findByRole('button', { name: 'Return prepayment' }));
    await waitFor(() => expect(screen.getByRole('spinbutton')).toHaveValue('20,000'));
  });
});
