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
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
  getCookie: vi.fn(),
}));
vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key, fallback) => fallback || key }),
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

vi.setConfig({ testTimeout: 15000 });

const createWrapper = () => {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }) => <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
};

/**
 * A COD order for a tiered member that also carries a subscription rate and a
 * redeemed reward. All three discounts stack, and the admin has to be able to
 * see WHICH one produced the gap between 36 000 and 32 280 — otherwise the only
 * way to audit a "wrong" total is to read the database.
 */
const STACKED_ORDER = {
  id: 1042,
  user_id: 115,
  order_number: 'ORD-TIER-BREAKDOWN',
  status: 'pending',
  customer_name: 'Tiered Customer',
  customer_email: 'tiered@example.com',
  customer_phone: '+998901234567',
  created_at: '2026-08-27T05:10:05+00:00',
  items_summary: [],
  items_count: 2,
  is_collected_cash_editable: false,
  collected_cash_edit_window_remaining_hours: null,
  collected_cash_event_amount: null,
  payment_method: 'cash',
  payment_status: 'pending',
  subtotal: 36000,
  discount_amount: 1000,
  loyalty_discount: 2000,
  tier_discount: 720,
  delivery_fee: 0,
  total_amount: 32280,
  amount_collected: 0,
  outstanding_amount: 32280,
  reserved_prepayment_amount: 0,
  net_outstanding_amount: 32280,
};

const PLAIN_ORDER = {
  ...STACKED_ORDER,
  id: 1043,
  order_number: 'ORD-PLAIN',
  subtotal: 36000,
  discount_amount: 0,
  loyalty_discount: 0,
  tier_discount: 0,
  delivery_fee: 0,
  total_amount: 36000,
  outstanding_amount: 36000,
  net_outstanding_amount: 36000,
};

function setupMocksForOrder(order) {
  vi.clearAllMocks();
  api.get.mockResolvedValue({
    data: { data: { statuses: [{ value: 'pending', label: 'Pending' }] } },
  });
  adminService.getOrders.mockResolvedValue({ data: { items: [order] }, meta: { total: 1 } });
  adminService.getOrderDetails.mockResolvedValue({
    success: true,
    data: { order: { ...order, items: [] } },
  });
  adminService.getOrderEditHistory.mockResolvedValue({ success: true, data: { entries: [] } });
  adminService.getProducts.mockResolvedValue({ data: { items: [] } });
}

async function openOrderDetail(order) {
  setupMocksForOrder(order);
  const user = userEvent.setup();
  render(<Orders />, { wrapper: createWrapper() });
  await waitFor(() => expect(adminService.getOrders).toHaveBeenCalled());
  await user.click(await screen.findByText(/view_details|View Details/i));
  await waitFor(() => expect(adminService.getOrderDetails).toHaveBeenCalledWith(order.id));
  return user;
}

describe('Order detail money breakdown', () => {
  it('shows every money line, each labelled by what produced it', async () => {
    await openOrderDetail(STACKED_ORDER);

    expect(await screen.findByText(/Money Breakdown/i)).toBeInTheDocument();
    expect(await screen.findByText(/^Subtotal$/i)).toBeInTheDocument();
    expect(await screen.findByText(/36,000 UZS/)).toBeInTheDocument();
    expect(await screen.findByText(/Subscription discount/i)).toBeInTheDocument();
    expect(await screen.findByText(/1,000 UZS/)).toBeInTheDocument();
    expect(await screen.findByText(/Reward discount/i)).toBeInTheDocument();
    expect(await screen.findByText(/2,000 UZS/)).toBeInTheDocument();
    expect(await screen.findByText(/Tier discount/i)).toBeInTheDocument();
    expect(await screen.findByText(/720 UZS/)).toBeInTheDocument();
    expect(await screen.findByText(/Delivery fee/i)).toBeInTheDocument();
  });

  it('hides the discount rows an order does not have', async () => {
    await openOrderDetail(PLAIN_ORDER);

    expect(await screen.findByText(/Money Breakdown/i)).toBeInTheDocument();
    expect(screen.queryByText(/Tier discount/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/Reward discount/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/Subscription discount/i)).not.toBeInTheDocument();
  });
});
