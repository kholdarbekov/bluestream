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
  id: 201,
  payment_id: null,
  order_number: 'ORD-REWARD-201',
  user_id: 42,
  status: 'confirmed',
  payment_method: 'cash',
  payment_provider: null,
  payment_status: 'completed',
  fiscalization_status: 'not_required',
  consume_marking_codes: false,
  total_amount: 36000,
  amount_collected: 36000,
  outstanding_amount: 0,
  customer_name: 'Buyer Reward',
  customer_email: 'reward@example.com',
  customer_phone: '+998901112233',
  created_at: '2026-06-15T10:00:00+00:00',
  has_loyalty_reward: true,
  payment_timeline: { timeline: [] },
  marking_code_summary: { events: {}, codes_by_order_item: {} },
  payment_transactions: [],
  click_callback_history: [],
  fiscalization_audit_trail: [],
  marking_code_activity: [],
  fiscalization: null,
  ...overrides,
});

const listOrderBase = {
  id: 201,
  order_number: 'ORD-REWARD-201',
  status: 'confirmed',
  payment_method: 'cash',
  payment_status: 'completed',
  total_amount: 36000,
  customer_name: 'Buyer Reward',
  customer_email: 'reward@example.com',
  customer_phone: '+998901112233',
  created_at: '2026-06-15T10:00:00+00:00',
};

describe('Orders page — loyalty reward display', () => {
  beforeEach(() => {
    vi.clearAllMocks();

    api.get.mockResolvedValue({
      data: {
        data: {
          statuses: [
            { value: 'pending', label: 'Pending' },
            { value: 'confirmed', label: 'Confirmed' },
          ],
        },
      },
    });
  });

  describe('List table — reward badge and merged items', () => {
    it('shows 🎁 Reward badge in list Alerts column when has_loyalty_reward is true', async () => {
      adminService.getOrders.mockResolvedValue({
        data: {
          items: [
            {
              ...listOrderBase,
              has_loyalty_reward: true,
              items_summary: [],
              items_count: 0,
            },
          ],
        },
        meta: { total: 1 },
      });

      render(<Orders />, { wrapper: createWrapper() });

      await waitFor(() => expect(adminService.getOrders).toHaveBeenCalled());

      // The Reward badge should appear in the list row (may be multiple in DOM, that's fine)
      const rewardBadges = await screen.findAllByText(/Reward/);
      expect(rewardBadges.length).toBeGreaterThan(0);
    });

    it('does NOT show reward badge when has_loyalty_reward is false', async () => {
      adminService.getOrders.mockResolvedValue({
        data: {
          items: [
            {
              ...listOrderBase,
              has_loyalty_reward: false,
              items_summary: [],
              items_count: 0,
            },
          ],
        },
        meta: { total: 1 },
      });

      render(<Orders />, { wrapper: createWrapper() });

      await waitFor(() => expect(adminService.getOrders).toHaveBeenCalled());
      await screen.findByText('ORD-REWARD-201');

      // No reward tag should appear for non-reward orders
      expect(screen.queryAllByText(/🎁 Reward/).length).toBe(0);
    });

    it('shows merged items line with free annotation in list when reward shares product', async () => {
      adminService.getOrders.mockResolvedValue({
        data: {
          items: [
            {
              ...listOrderBase,
              has_loyalty_reward: true,
              items_summary: [
                { product_id: 2, product_name: '19 litrlik suv', quantity: 2, unit_price: 18000, total_price: 36000, is_reward: false },
                { product_id: 2, product_name: '19 litrlik suv', quantity: 1, unit_price: 0, total_price: 0, is_reward: true },
              ],
              items_count: 2,
            },
          ],
        },
        meta: { total: 1 },
      });

      render(<Orders />, { wrapper: createWrapper() });

      await waitFor(() => expect(adminService.getOrders).toHaveBeenCalled());

      // Should show merged line with "+1 free" and no duplicate product line
      const freeAnnotation = await screen.findByText(/\(\+1 free/);
      expect(freeAnnotation).toBeInTheDocument();

      // Only ONE row for "19 litrlik suv" should exist in the items column
      const productRows = screen.queryAllByText(/19 litrlik suv/);
      expect(productRows.length).toBe(1);
    });
  });

  describe('Detail modal — additive merge in Order Items table', () => {
    beforeEach(() => {
      adminService.getOrders.mockResolvedValue({
        data: {
          items: [
            {
              ...listOrderBase,
              has_loyalty_reward: true,
              items_summary: [],
              items_count: 0,
            },
          ],
        },
        meta: { total: 1 },
      });
    });

    it('shows a single merged row with (+1 free) and Reward tag when reward shares product with purchased line', async () => {
      adminService.getOrderDetails.mockResolvedValue({
        success: true,
        data: {
          order: makeDetailOrder({
            items: [
              { id: 10, product_id: 2, product_name: '19 litrlik suv', quantity: 2, unit_price: 18000, total_price: 36000, is_reward: false },
              { id: 11, product_id: 2, product_name: '19 litrlik suv', quantity: 1, unit_price: 0, total_price: 0, is_reward: true },
            ],
          }),
        },
      });

      const user = userEvent.setup();
      render(<Orders />, { wrapper: createWrapper() });

      await waitFor(() => expect(adminService.getOrders).toHaveBeenCalled());
      await user.click(await screen.findByText(/View Details/i));
      await waitFor(() => expect(adminService.getOrderDetails).toHaveBeenCalledWith(201));

      // Only ONE row for "19 litrlik suv" should appear in the items table
      const productRows = screen.queryAllByText('19 litrlik suv');
      expect(productRows.length).toBe(1);

      // The Qty cell should show the free annotation
      expect(screen.getByText(/\+1 free/)).toBeInTheDocument();

      // A Reward tag should appear in the items table
      const rewardTags = screen.queryAllByText(/Reward/);
      expect(rewardTags.length).toBeGreaterThan(0);
    });

    it('shows 🎁 Reward badge in modal status area when has_loyalty_reward is true', async () => {
      adminService.getOrderDetails.mockResolvedValue({
        success: true,
        data: {
          order: makeDetailOrder({
            items: [],
          }),
        },
      });

      const user = userEvent.setup();
      render(<Orders />, { wrapper: createWrapper() });

      await waitFor(() => expect(adminService.getOrders).toHaveBeenCalled());
      await user.click(await screen.findByText(/View Details/i));
      await waitFor(() => expect(adminService.getOrderDetails).toHaveBeenCalledWith(201));

      // Should find Reward badge in the modal
      const rewardTags = screen.queryAllByText(/Reward/);
      expect(rewardTags.length).toBeGreaterThan(0);
    });

    it('shows standalone Free row for reward product with no matching paid line', async () => {
      adminService.getOrderDetails.mockResolvedValue({
        success: true,
        data: {
          order: makeDetailOrder({
            has_loyalty_reward: true,
            total_amount: 18000,
            items: [
              { id: 20, product_id: 5, product_name: 'Bonus Suv', quantity: 2, unit_price: 9000, total_price: 18000, is_reward: false },
              { id: 21, product_id: 7, product_name: 'Gift Bottle', quantity: 1, unit_price: 0, total_price: 0, is_reward: true },
            ],
          }),
        },
      });

      const user = userEvent.setup();
      render(<Orders />, { wrapper: createWrapper() });

      await waitFor(() => expect(adminService.getOrders).toHaveBeenCalled());
      await user.click(await screen.findByText(/View Details/i));
      await waitFor(() => expect(adminService.getOrderDetails).toHaveBeenCalledWith(201));

      // Gift Bottle should appear as a standalone Free row
      expect(await screen.findByText('Gift Bottle')).toBeInTheDocument();
      // The standalone row should show "Free" for price columns
      const freeTexts = screen.queryAllByText('Free');
      expect(freeTexts.length).toBeGreaterThan(0);
    });
  });
});
