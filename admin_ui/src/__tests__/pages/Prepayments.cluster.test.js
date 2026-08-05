import React from 'react';
import { render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';

import Prepayments from '../../pages/Prepayments';
import staffService from '../../services/staffService';

vi.mock('../../services/staffService');

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key, fallback) => fallback || key,
  }),
}));

// antd's static `message` mounts a real portal outside act() and leaks across
// tests — stub it.
vi.mock('antd', async () => {
  const actual = await vi.importActual('antd');
  return {
    ...actual,
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

const createWrapper = (initialEntries = ['/staff/prepayments']) => {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return ({ children }) => (
    <MemoryRouter initialEntries={initialEntries}>
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    </MemoryRouter>
  );
};

// Shape produced by CashCollectionService.list_customers_with_prepayment_balance
// AFTER plan 2b task 5: linked accounts arrive collapsed into ONE row carrying
// the summed balance plus `member_user_ids`; unlinked rows pass through.
const CUSTOMERS = {
  data: {
    data: {
      items: [
        {
          id: 42,
          first_name: 'Cluster',
          last_name: 'Head',
          phone: '+998901111111',
          role: 'customer',
          available_prepayment_balance: 150000,
          last_collection_at: '2026-07-20T09:00:00+00:00',
          member_user_ids: [42, 43],
        },
        {
          id: 77,
          first_name: 'Solo',
          last_name: 'Customer',
          phone: '+998902222222',
          role: 'customer',
          available_prepayment_balance: 30000,
          last_collection_at: '2026-07-19T09:00:00+00:00',
          member_user_ids: [77],
        },
      ],
      total: 2,
    },
  },
};

const HISTORY = {
  data: {
    data: {
      customer_id: 43,
      first_name: 'Cluster',
      last_name: 'Sibling',
      available_prepayment_balance: 150000,
      lifetime_collected: 400000,
      lifetime_applied: 250000,
      cluster_member_ids: [42, 43],
      events: [],
    },
  },
};

beforeEach(() => {
  vi.clearAllMocks();
  staffService.listCustomersWithPrepaymentBalance.mockResolvedValue(CUSTOMERS);
  staffService.getCustomerPrepaymentHistory.mockResolvedValue(HISTORY);
});

describe('Prepayments page with cluster-collapsed rows', () => {
  it('renders the summed cluster balance and flags the collapsed row', async () => {
    render(<Prepayments />, { wrapper: createWrapper() });

    await waitFor(() => expect(staffService.listCustomersWithPrepaymentBalance).toHaveBeenCalled());

    const clusterRow = await screen.findByRole('row', { name: /Cluster Head/ });
    // Balance accessor still resolves (no NaN) on the collapsed row.
    expect(clusterRow).toHaveTextContent('150,000 UZS');
    expect(within(clusterRow).getByText('2 linked accounts')).toBeInTheDocument();
  });

  it('leaves an unlinked customer row exactly as before', async () => {
    render(<Prepayments />, { wrapper: createWrapper() });

    const soloRow = await screen.findByRole('row', { name: /Solo Customer/ });
    expect(soloRow).toHaveTextContent('30,000 UZS');
    expect(within(soloRow).queryByText(/linked accounts/)).toBeNull();
  });

  it('deep-links a linked member to the cluster-wide ledger', async () => {
    render(<Prepayments />, {
      wrapper: createWrapper(['/staff/prepayments?customer_id=43']),
    });

    await waitFor(() => {
      expect(staffService.getCustomerPrepaymentHistory).toHaveBeenCalledWith(43, expect.any(Object));
    });

    // The drawer titles from the history payload, not from the collapsed list
    // row (the member id is not a list row id after collapsing).
    expect(await screen.findByText('Cluster Sibling')).toBeInTheDocument();
    expect(await screen.findByText('150,000')).toBeInTheDocument();
  });
});
