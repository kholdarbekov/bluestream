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
    defaultOptions: {
      queries: { retry: false },
    },
  });
  return ({ children }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
};

// A minimal "full order details" stub used in all detail-modal tests.
const makeDetailOrder = (overrides = {}) => ({
  id: 301,
  payment_id: null,
  order_number: 'ORD-COURTESY-301',
  user_id: 42,
  status: 'confirmed',
  payment_method: 'cash',
  payment_provider: null,
  payment_status: 'pending',
  fiscalization_status: 'not_required',
  consume_marking_codes: false,
  total_amount: 76000,
  customer_name: 'Buyer Courtesy',
  customer_email: 'courtesy@example.com',
  customer_phone: '+998901112233',
  created_at: '2026-06-15T10:00:00+00:00',
  payment_timeline: { timeline: [] },
  marking_code_summary: { events: {}, codes_by_order_item: {} },
  payment_transactions: [],
  click_callback_history: [],
  fiscalization_audit_trail: [],
  marking_code_activity: [],
  fiscalization: null,
  ...overrides,
});

// The detail's items as GET /admin/orders/<id> publishes them (tests/integration/test_admin_order_detail_courtesy_split.py).
const COURTESY_ITEM = {
  id: 10, product_id: 2, product_name: '19 litrlik suv', quantity: 4, unit_price: 20000, discount_amount: 4000,
  courtesy_quantity: 2, courtesy_unit_price: 18000, regular_quantity: 2, total_price: 76000, is_reward: false,
};
const PLAIN_ITEM = {
  id: 11, product_id: 3, product_name: '10 litrlik suv', quantity: 3, unit_price: 12000, discount_amount: 0,
  courtesy_quantity: null, courtesy_unit_price: null, regular_quantity: null, total_price: 36000, is_reward: false,
};

describe('Orders page: a courtesy line in the detail', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.get.mockResolvedValue({ data: { data: { statuses: [{ value: 'confirmed', label: 'Confirmed' }] } } });
    adminService.getOrders.mockResolvedValue({
      data: {
        items: [{
          id: 301, order_number: 'ORD-COURTESY-301', status: 'confirmed', payment_method: 'cash',
          payment_status: 'pending', total_amount: 76000, customer_name: 'Buyer Courtesy',
          customer_email: 'courtesy@example.com', customer_phone: '+998901112233',
          created_at: '2026-06-15T10:00:00+00:00', items_summary: [], items_count: 0,
        }],
      },
      meta: { total: 1 },
    });
  });

  it('prints the split from the published fields, and a plain line as before', async () => {
    adminService.getOrderDetails.mockResolvedValue({
      success: true,
      data: { order: makeDetailOrder({ items: [COURTESY_ITEM, PLAIN_ITEM] }) },
    });

    const user = userEvent.setup();
    render(<Orders />, { wrapper: createWrapper() });
    await waitFor(() => expect(adminService.getOrders).toHaveBeenCalled());
    await user.click(await screen.findByText(/View Details/i));
    await waitFor(() => expect(adminService.getOrderDetails).toHaveBeenCalledWith(301));

    const unitCell = await screen.findByText(/2 × 18[\s\u00a0,.]000 UZS \+ 2 × 20[\s\u00a0,.]000 UZS/);
    expect(unitCell).toBeInTheDocument();
    expect(screen.queryByText(/^20[\s\u00a0,.]000 UZS$/)).not.toBeInTheDocument();
    expect(screen.getByText(/^12[\s\u00a0,.]000 UZS$/)).toBeInTheDocument();
  });
});
