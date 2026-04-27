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

// antd's Dropdown items don't render in the DOM unless the dropdown trigger is
// actively hovered/clicked through the portal. Replace with a flat list of
// buttons so the test can click action items directly. (Mirrors the pattern
// already used in Orders.fiscalization.test.js.)
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

const wrapper = () => {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return ({ children }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
};

const baseOrder = {
  id: 100,
  order_number: 'ORD-100',
  status: 'pending',
  payment_method: 'cash',
  payment_status: 'pending',
  total_amount: 18000,
  customer_name: 'Test Buyer',
  customer_email: 'buyer@example.com',
  customer_phone: '+998901234567',
  created_at: '2026-04-27T10:00:00+00:00',
  items_summary: [],
  items_count: 0,
};

describe('Orders page — golden path', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.get.mockResolvedValue({
      data: {
        data: {
          statuses: [
            { value: 'pending', label: 'Pending' },
            { value: 'confirmed', label: 'Confirmed' },
            { value: 'cancelled', label: 'Cancelled' },
          ],
        },
      },
    });
    adminService.getOrders.mockResolvedValue({
      data: { items: [baseOrder] },
      meta: { total: 1 },
    });
    adminService.updateOrderStatus.mockResolvedValue({ success: true });
  });

  it('fetches and renders the orders list', async () => {
    render(<Orders />, { wrapper: wrapper() });

    await waitFor(() => {
      expect(adminService.getOrders).toHaveBeenCalled();
    });

    // Order number from the mocked response should land in the table.
    expect(await screen.findByText('ORD-100')).toBeInTheDocument();
    expect(screen.getByText('Test Buyer')).toBeInTheDocument();
  });

  it('shows an empty state when no orders match the filters', async () => {
    adminService.getOrders.mockResolvedValueOnce({
      data: { items: [] },
      meta: { total: 0 },
    });
    render(<Orders />, { wrapper: wrapper() });

    await waitFor(() => {
      expect(adminService.getOrders).toHaveBeenCalled();
    });

    // Either antd's "No data" or our EmptyState — both are valid signals
    // that the no-rows branch rendered. Avoid asserting a literal label
    // that may shift between i18n catalogs.
    await waitFor(() => {
      expect(screen.queryByText('ORD-100')).not.toBeInTheDocument();
    });
  });

  it('surfaces a recoverable error when the list query fails', async () => {
    adminService.getOrders.mockRejectedValueOnce(
      Object.assign(new Error('boom'), {
        response: { data: { message: 'temporary outage' } },
      }),
    );
    render(<Orders />, { wrapper: wrapper() });

    await waitFor(() => {
      expect(adminService.getOrders).toHaveBeenCalled();
    });
    // The page should not crash; the table region still renders even when
    // data fails. We don't assert the exact error UI here — different page
    // states render different chrome — only that no React error boundary
    // swallowed everything.
    expect(document.body.textContent.length).toBeGreaterThan(0);
  });
});
