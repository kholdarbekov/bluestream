import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import Orders from '../../pages/Orders';
import adminService from '../../services/adminService';
import api from '../../services/api';

vi.mock('../../services/adminService');
vi.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
    delete: vi.fn(),
  },
  getCookie: vi.fn(),
}));
vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key, fallback) => fallback || key,
  }),
}));

vi.mock('../../components/common/PermissionGuard', async () => {
  const actual = await vi.importActual('../../components/common/PermissionGuard');
  return {
    ...actual,
    usePermissions: vi.fn(() => ({
      isAdmin: () => true,
      isManager: () => false,
      isOperator: () => false,
      hasPermission: () => true,
      canManageOrders: () => true,
    })),
  };
});

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
            <button key={item.key} onClick={item.onClick} type="button" disabled={item.disabled}>
              {typeof item.label === 'string' ? item.label : item.key}
            </button>
          ))}
      </div>
    ),
  };
});

vi.setConfig({ testTimeout: 10000 });

const createWrapper = () => {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return ({ children }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
};

function setupMocksForOrder(order) {
  vi.clearAllMocks();

  api.get.mockResolvedValue({
    data: {
      data: {
        statuses: [
          { value: 'pending', label: 'Pending' },
          { value: 'delivered', label: 'Delivered' },
        ],
      },
    },
  });

  adminService.getOrders.mockResolvedValue({
    data: { items: [order] },
    meta: { total: 1 },
  });
  adminService.getOrderDetails.mockResolvedValue({
    success: true,
    data: { order: { ...order, items: [] } },
  });
  adminService.getOrderEditHistory.mockResolvedValue({
    success: true,
    data: { entries: [] },
  });
  adminService.getProducts.mockResolvedValue({ data: { items: [] } });
}

const BASE_ORDER = {
  user_id: 88,
  status: 'delivered',
  customer_name: 'Receivable Customer',
  customer_email: 'test@example.com',
  customer_phone: '+998901234567',
  created_at: '2026-08-07T10:00:00+00:00',
  items_summary: [],
  items_count: 3,
  is_collected_cash_editable: false,
  collected_cash_edit_window_remaining_hours: null,
  collected_cash_event_amount: null,
};

async function openOrderDetail(order) {
  setupMocksForOrder(order);
  const user = userEvent.setup();
  render(<Orders />, { wrapper: createWrapper() });

  await waitFor(() => {
    expect(adminService.getOrders).toHaveBeenCalled();
  });
  await user.click(await screen.findByText(/view_details|View Details/i));
  await waitFor(() => {
    expect(adminService.getOrderDetails).toHaveBeenCalledWith(order.id);
  });
}

/**
 * Prod order 961: a Click order paid for 2 bottles, edited at the door to add a
 * 3rd. `payment_status` becomes `partially_paid`, which the old gate
 * (`['pending','cancelled','failed'].includes(payment_status)`) excluded — so
 * the ONE admin affordance that records a settlement was hidden for exactly the
 * case the order-edit cascade tells the admin to use it for.
 *
 * Plan: docs/superpowers/plans/2026-08-08-open-receivable-ssot.md (Task 11)
 */
describe('Record Personal Card Payment button visibility', () => {
  it('shows for a delivered click order that still owes money', async () => {
    await openOrderDetail({
      ...BASE_ORDER,
      id: 961,
      order_number: 'AD_000961_26',
      payment_method: 'click',
      payment_status: 'partially_paid',
      total_amount: 90000,
      amount_collected: 60000,
      outstanding_amount: 30000,
    });

    expect(
      await screen.findByText(/Record Personal Card Payment/i)
    ).toBeInTheDocument();
  });

  it('hides for a fully settled click order', async () => {
    await openOrderDetail({
      ...BASE_ORDER,
      id: 962,
      order_number: 'AD_000962_26',
      payment_method: 'click',
      payment_status: 'completed',
      total_amount: 90000,
      amount_collected: 90000,
      outstanding_amount: 0,
    });

    expect(
      screen.queryByText(/Record Personal Card Payment/i)
    ).not.toBeInTheDocument();
  });

  it('still shows for a cash order with nothing outstanding', async () => {
    await openOrderDetail({
      ...BASE_ORDER,
      id: 963,
      order_number: 'AD_000963_26',
      payment_method: 'cash',
      payment_status: 'completed',
      total_amount: 90000,
      amount_collected: 90000,
      outstanding_amount: 0,
    });

    expect(
      await screen.findByText(/Record Personal Card Payment/i)
    ).toBeInTheDocument();
  });

  it('still shows for a pending click order', async () => {
    await openOrderDetail({
      ...BASE_ORDER,
      id: 964,
      order_number: 'AD_000964_26',
      payment_method: 'click',
      payment_status: 'pending',
      total_amount: 36000,
      amount_collected: 0,
      outstanding_amount: 36000,
    });

    expect(
      await screen.findByText(/Record Personal Card Payment/i)
    ).toBeInTheDocument();
  });
});
