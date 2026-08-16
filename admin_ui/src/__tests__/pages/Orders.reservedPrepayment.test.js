import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import Orders, { collectableOutstanding } from '../../pages/Orders';
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

vi.setConfig({ testTimeout: 15000 });

const createWrapper = () => {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return ({ children }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
};

/**
 * Prod order AD_000630_26: a 90 000 COD order carrying a 5 000 prepaid
 * reservation. The modal quoted the GROSS 90 000 while the driver's screen and
 * the customer's COD statement quoted the net 85 000 — and because the Record
 * Personal Card Payment button pre-fills from this modal, the gross invited a
 * transfer that consumed the reservation's slice and destroyed the credit.
 */
const RESERVED_ORDER = {
  id: 1028,
  user_id: 115,
  order_number: 'AD_000630_26',
  status: 'out_for_delivery',
  customer_name: 'Prepaid Customer',
  customer_email: 'test@example.com',
  customer_phone: '+998901234567',
  created_at: '2026-08-14T05:10:05+00:00',
  items_summary: [],
  items_count: 3,
  is_collected_cash_editable: false,
  collected_cash_edit_window_remaining_hours: null,
  collected_cash_event_amount: null,
  payment_method: 'cash',
  payment_status: 'pending',
  total_amount: 90000,
  amount_collected: 0,
  outstanding_amount: 90000,
  reserved_prepayment_amount: 5000,
  net_outstanding_amount: 85000,
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
  adminService.previewPersonalCardTransfer.mockResolvedValue({
    success: true,
    data: {
      applied_to_order: 85000,
      order_outstanding_before: 85000,
      order_outstanding_after: 0,
      spill_allocations: [],
      remaining_as_credit: 0,
      warnings: [],
    },
  });
}

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
  return user;
}

describe('collectableOutstanding', () => {
  it('prefers the net figure when the backend supplies one', () => {
    expect(collectableOutstanding(RESERVED_ORDER)).toBe(85000);
  });

  it('falls back to the gross for payloads without the field', () => {
    expect(collectableOutstanding({ outstanding_amount: 36000 })).toBe(36000);
  });

  it('is zero for a missing order', () => {
    expect(collectableOutstanding(undefined)).toBe(0);
  });
});

describe('Order detail modal with a prepaid reservation', () => {
  it('shows what the prepayment covers and what is left to collect', async () => {
    await openOrderDetail(RESERVED_ORDER);

    expect(await screen.findByText(/Covered by prepayment/i)).toBeInTheDocument();
    expect(await screen.findByText(/Left to collect/i)).toBeInTheDocument();
    expect(await screen.findByText(/85,000 UZS/)).toBeInTheDocument();
  });

  it('pre-fills the personal card transfer with the net, not the gross', async () => {
    const user = await openOrderDetail(RESERVED_ORDER);

    await user.click(await screen.findByText(/Record Personal Card Payment/i));

    await waitFor(() => {
      expect(adminService.previewPersonalCardTransfer).toHaveBeenCalledWith(
        RESERVED_ORDER.id,
        { amount: 85000 },
      );
    });
  });
});
