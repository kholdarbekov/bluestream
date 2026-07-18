import React from 'react';
import { act, render, screen, waitFor } from '@testing-library/react';
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
      queries: {
        retry: false,
      },
    },
  });

  return ({ children }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
};

describe('Orders personal card payment flow', () => {
  beforeEach(() => {
    vi.clearAllMocks();

    api.get.mockResolvedValue({
      data: {
        data: {
          statuses: [
            { value: 'pending', label: 'Pending' },
            { value: 'confirmed', label: 'Confirmed' },
            { value: 'delivered', label: 'Delivered' },
          ],
        },
      },
    });

    adminService.getOrders.mockResolvedValue({
      data: {
        items: [
          {
            id: 456,
            order_number: 'ORD-TEST-456',
            user_id: 77,
            status: 'confirmed',
            payment_method: 'cash',
            payment_status: 'partially_paid',
            total_amount: 18000,
            outstanding_amount: 13000,
            customer_name: 'Ali Buyer',
            customer_email: 'ali@example.com',
            customer_phone: '+998901234500',
            created_at: '2026-03-11T10:00:00+00:00',
            items_summary: [],
            items_count: 0,
          },
        ],
      },
      meta: {
        total: 1,
      },
    });

    adminService.getOrderDetails.mockResolvedValue({
      success: true,
      data: {
        order: {
          id: 456,
          order_number: 'ORD-TEST-456',
          user_id: 77,
          status: 'confirmed',
          payment_method: 'cash',
          payment_status: 'partially_paid',
          total_amount: 18000,
          amount_collected: 5000,
          outstanding_amount: 13000,
          customer_name: 'Ali Buyer',
          customer_email: 'ali@example.com',
          customer_phone: '+998901234500',
          created_at: '2026-03-11T10:00:00+00:00',
          items: [],
        },
      },
    });

    adminService.recordStaffCashCollection.mockResolvedValue({
      data: {
        cash_collection_event: {
          id: 901,
          source: 'personal_card_transfer',
        },
      },
    });
  });

  it('shows Record Personal Card Payment button for delivered Click order with cancelled payment', async () => {
    // Arrange: override getOrders + getOrderDetails with a DELIVERED Click order
    // whose payment was timeout-cancelled (the canonical prod scenario: order 547)
    adminService.getOrders.mockResolvedValue({
      data: {
        items: [
          {
            id: 547,
            order_number: 'TG_000178_26',
            user_id: 99,
            status: 'delivered',
            payment_method: 'click',
            payment_status: 'cancelled',
            total_amount: 36000,
            outstanding_amount: 36000,
            customer_name: 'Test Customer',
            customer_email: 'test@example.com',
            customer_phone: '+998901234567',
            created_at: '2026-06-20T10:00:00+00:00',
            items_summary: [],
            items_count: 1,
          },
        ],
      },
      meta: { total: 1 },
    });

    adminService.getOrderDetails.mockResolvedValue({
      success: true,
      data: {
        order: {
          id: 547,
          order_number: 'TG_000178_26',
          user_id: 99,
          status: 'delivered',
          payment_method: 'click',
          payment_status: 'cancelled',
          total_amount: 36000,
          amount_collected: 0,
          outstanding_amount: 36000,
          customer_name: 'Test Customer',
          customer_email: 'test@example.com',
          customer_phone: '+998901234567',
          created_at: '2026-06-20T10:00:00+00:00',
          items: [],
        },
      },
    });

    const user = userEvent.setup();
    render(<Orders />, { wrapper: createWrapper() });

    await waitFor(() => {
      expect(adminService.getOrders).toHaveBeenCalled();
    });

    await user.click(await screen.findByText(/view_details|View Details/i));

    await waitFor(() => {
      expect(adminService.getOrderDetails).toHaveBeenCalledWith(547);
    });

    // The button must be visible for cancelled electronic payment
    expect(
      await screen.findByText(/record_personal_card_payment|Record Personal Card Payment/i)
    ).toBeInTheDocument();
  });

  it('submits personal card payment from order details and refreshes details', async () => {
    const user = userEvent.setup();
    render(<Orders />, { wrapper: createWrapper() });

    await waitFor(() => {
      expect(adminService.getOrders).toHaveBeenCalled();
    });

    await user.click(await screen.findByText(/view_details|View Details/i));

    await waitFor(() => {
      expect(adminService.getOrderDetails).toHaveBeenCalledWith(456);
    });

    await user.click(await screen.findByText(/record_personal_card_payment|Record Personal Card Payment/i));

    const amountInput = screen.getByRole('spinbutton');
    await user.clear(amountInput);
    await user.type(amountInput, '12000');
    await user.type(
      screen.getByPlaceholderText('Example: Customer transferred to owner personal card'),
      'Customer paid owner personal card',
    );
    await user.click(screen.getByRole('button', { name: 'OK' }));

    await waitFor(() => {
      expect(adminService.recordStaffCashCollection).toHaveBeenCalledTimes(1);
    });

    expect(adminService.recordStaffCashCollection).toHaveBeenCalledWith({
      customer_id: 77,
      order_id: 456,
      amount: '12000',
      notes: 'Customer paid owner personal card',
      source: 'personal_card_transfer',
      proof_data: { channel: 'admin_ui_orders' },
    });

    await waitFor(() => {
      expect(adminService.getOrderDetails).toHaveBeenCalledTimes(2);
    });
  });

  it('previews the surplus spilling onto another delivered debt before the admin confirms', async () => {
    // The admin's only on-screen anchor used to be THIS order's outstanding, so a
    // transfer covering an older debt too gave no signal. The preview must name the
    // other order and the amount going to it.
    adminService.previewPersonalCardTransfer.mockResolvedValue({
      data: {
        order_id: 456,
        order_number: 'ORD-TEST-456',
        amount: 100000,
        applied_to_order: 13000,
        order_outstanding_before: 13000,
        order_outstanding_after: 0,
        applied_to_other_debts: 87000,
        remaining_as_credit: 0,
        spill_allocations: [
          {
            order_id: 321,
            order_number: 'ORD-OLD-DEBT',
            amount: 87000,
            outstanding_before: 90000,
            outstanding_after: 3000,
          },
        ],
        warnings: [],
      },
    });

    const user = userEvent.setup();
    render(<Orders />, { wrapper: createWrapper() });

    await waitFor(() => {
      expect(adminService.getOrders).toHaveBeenCalled();
    });
    await user.click(await screen.findByText(/view_details|View Details/i));
    await waitFor(() => {
      expect(adminService.getOrderDetails).toHaveBeenCalledWith(456);
    });
    await user.click(await screen.findByText(/record_personal_card_payment|Record Personal Card Payment/i));

    // The amount field opens pre-filled with this order's outstanding.
    await waitFor(
      () => {
        expect(adminService.previewPersonalCardTransfer).toHaveBeenCalledWith(456, { amount: 13000 });
      },
      { timeout: 3000 },
    );

    const amountInput = screen.getByRole('spinbutton');
    await user.clear(amountInput);
    await user.type(amountInput, '100000');

    // Debounced: typing 6 characters must still produce one request for the final
    // amount, as a number — not one per keystroke.
    await waitFor(
      () => {
        expect(adminService.previewPersonalCardTransfer).toHaveBeenCalledWith(456, { amount: 100000 });
      },
      { timeout: 3000 },
    );
    expect(adminService.previewPersonalCardTransfer).toHaveBeenCalledTimes(2);

    expect(await screen.findByText('Where this payment will go')).toBeInTheDocument();
    expect(await screen.findByText('ORD-OLD-DEBT')).toBeInTheDocument();
  });

  it('drops an in-flight preview when the amount is cleared', async () => {
    // Otherwise a slow response repaints an allocation breakdown for an amount the
    // admin has already erased.
    let releasePreview;
    adminService.previewPersonalCardTransfer.mockImplementation(
      () =>
        new Promise((resolve) => {
          releasePreview = () =>
            resolve({
              data: {
                order_id: 456,
                order_number: 'ORD-TEST-456',
                amount: 100000,
                applied_to_order: 13000,
                order_outstanding_before: 13000,
                order_outstanding_after: 0,
                applied_to_other_debts: 87000,
                remaining_as_credit: 0,
                spill_allocations: [
                  {
                    order_id: 321,
                    order_number: 'ORD-STALE-DEBT',
                    amount: 87000,
                    outstanding_before: 90000,
                    outstanding_after: 3000,
                  },
                ],
                warnings: [],
              },
            });
        }),
    );

    const user = userEvent.setup();
    render(<Orders />, { wrapper: createWrapper() });

    await waitFor(() => {
      expect(adminService.getOrders).toHaveBeenCalled();
    });
    await user.click(await screen.findByText(/view_details|View Details/i));
    await waitFor(() => {
      expect(adminService.getOrderDetails).toHaveBeenCalledWith(456);
    });
    await user.click(await screen.findByText(/record_personal_card_payment|Record Personal Card Payment/i));

    const amountInput = screen.getByRole('spinbutton');
    await user.clear(amountInput);
    await user.type(amountInput, '100000');

    await waitFor(
      () => {
        expect(releasePreview).toBeDefined();
      },
      { timeout: 3000 },
    );

    // Erase the amount while the request is still in flight, then let it land and
    // give React a real chance to paint it — asserting absence immediately would
    // pass whether or not the response is discarded.
    await user.clear(amountInput);
    await act(async () => {
      releasePreview();
      await new Promise((resolve) => setTimeout(resolve, 50));
    });

    expect(screen.queryByText('ORD-STALE-DEBT')).not.toBeInTheDocument();
  });

  it('warns that surplus with no other debt to absorb it becomes customer credit', async () => {
    adminService.previewPersonalCardTransfer.mockResolvedValue({
      data: {
        order_id: 456,
        order_number: 'ORD-TEST-456',
        amount: 20000,
        applied_to_order: 13000,
        order_outstanding_before: 13000,
        order_outstanding_after: 0,
        applied_to_other_debts: 0,
        remaining_as_credit: 7000,
        spill_allocations: [],
        warnings: ['surplus_becomes_customer_credit'],
      },
    });

    const user = userEvent.setup();
    render(<Orders />, { wrapper: createWrapper() });

    await waitFor(() => {
      expect(adminService.getOrders).toHaveBeenCalled();
    });
    await user.click(await screen.findByText(/view_details|View Details/i));
    await waitFor(() => {
      expect(adminService.getOrderDetails).toHaveBeenCalledWith(456);
    });
    await user.click(await screen.findByText(/record_personal_card_payment|Record Personal Card Payment/i));

    const amountInput = screen.getByRole('spinbutton');
    await user.clear(amountInput);
    await user.type(amountInput, '20000');

    await waitFor(
      () => {
        expect(adminService.previewPersonalCardTransfer).toHaveBeenCalledWith(456, { amount: 20000 });
      },
      { timeout: 3000 },
    );

    expect(await screen.findByText('Left as customer credit')).toBeInTheDocument();
  });

  it('shows the personal card action for a subscription-generated COD order', async () => {
    // Regression guard: Order.subscription_id/is_subscription_order (Task 3)
    // must not affect the personal-card gate, which reads payment_method only.
    adminService.getOrderDetails.mockResolvedValue({
      success: true,
      data: {
        order: {
          id: 456,
          order_number: 'ORD-TEST-456',
          user_id: 77,
          status: 'confirmed',
          payment_method: 'cash',
          payment_status: 'partially_paid',
          total_amount: 18000,
          amount_collected: 5000,
          outstanding_amount: 13000,
          customer_name: 'Ali Buyer',
          customer_email: 'ali@example.com',
          customer_phone: '+998901234500',
          created_at: '2026-03-11T10:00:00+00:00',
          items: [],
          is_subscription_order: true,
          subscription_id: 7,
        },
      },
    });

    const user = userEvent.setup();
    render(<Orders />, { wrapper: createWrapper() });

    await waitFor(() => {
      expect(adminService.getOrders).toHaveBeenCalled();
    });

    await user.click(await screen.findByText(/view_details|View Details/i));

    await waitFor(() => {
      expect(adminService.getOrderDetails).toHaveBeenCalledWith(456);
    });

    expect(
      await screen.findByText(/record_personal_card_payment|Record Personal Card Payment/i)
    ).toBeInTheDocument();
  });

  it('tags a subscription-generated order in the detail modal', async () => {
    adminService.getOrderDetails.mockResolvedValue({
      success: true,
      data: {
        order: {
          id: 456,
          order_number: 'ORD-TEST-456',
          user_id: 77,
          status: 'confirmed',
          payment_method: 'cash',
          payment_status: 'partially_paid',
          total_amount: 18000,
          amount_collected: 5000,
          outstanding_amount: 13000,
          customer_name: 'Ali Buyer',
          customer_email: 'ali@example.com',
          customer_phone: '+998901234500',
          created_at: '2026-03-11T10:00:00+00:00',
          items: [],
          is_subscription_order: true,
          subscription_id: 7,
        },
      },
    });

    const user = userEvent.setup();
    render(<Orders />, { wrapper: createWrapper() });

    await waitFor(() => {
      expect(adminService.getOrders).toHaveBeenCalled();
    });

    await user.click(await screen.findByText(/view_details|View Details/i));

    await waitFor(() => {
      expect(adminService.getOrderDetails).toHaveBeenCalledWith(456);
    });

    expect(await screen.findByText(/subscription #7/i)).toBeInTheDocument();
  });

  it('labels an ordinary (non-subscription) order as One-off in the detail modal', async () => {
    adminService.getOrderDetails.mockResolvedValue({
      success: true,
      data: {
        order: {
          id: 456,
          order_number: 'ORD-TEST-456',
          user_id: 77,
          status: 'confirmed',
          payment_method: 'cash',
          payment_status: 'partially_paid',
          total_amount: 18000,
          amount_collected: 5000,
          outstanding_amount: 13000,
          customer_name: 'Ordinary Buyer',
          customer_email: 'ord@example.com',
          customer_phone: '+998901234501',
          created_at: '2026-03-11T10:00:00+00:00',
          items: [],
          is_subscription_order: false,
          subscription_id: null,
        },
      },
    });

    const user = userEvent.setup();
    render(<Orders />, { wrapper: createWrapper() });

    await waitFor(() => {
      expect(adminService.getOrders).toHaveBeenCalled();
    });

    await user.click(await screen.findByText(/view_details|View Details/i));

    await waitFor(() => {
      expect(adminService.getOrderDetails).toHaveBeenCalledWith(456);
    });

    expect(await screen.findByText('One-off')).toBeInTheDocument();
    expect(screen.queryByText(/subscription #/i)).not.toBeInTheDocument();
  });
});
