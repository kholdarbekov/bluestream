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

describe('Orders fiscalization operations', () => {
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

    adminService.getOrders.mockResolvedValue({
      data: {
        items: [
          {
            id: 321,
            order_number: 'ORD-FISCAL-321',
            status: 'confirmed',
            payment_method: 'click',
            payment_provider: 'click',
            payment_status: 'completed',
            total_amount: 18000,
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
          id: 321,
          payment_id: 9001,
          order_number: 'ORD-FISCAL-321',
          user_id: 77,
          status: 'confirmed',
          payment_method: 'click',
          payment_provider: 'click',
          payment_status: 'completed',
          fiscalization_status: 'failed',
          total_amount: 18000,
          amount_collected: 18000,
          outstanding_amount: 0,
          customer_name: 'Ali Buyer',
          customer_email: 'ali@example.com',
          customer_phone: '+998901234500',
          created_at: '2026-03-11T10:00:00+00:00',
          items: [],
          payment_timeline: { timeline: [] },
          marking_code_summary: { events: { reserved: 1 }, codes_by_order_item: { 501: ['MARK-001'] } },
          payment_transactions: [
            {
              id: 1,
              transaction_type: 'refund',
              status: 'completed',
              success: true,
              provider_transaction_id: 'click-txn-1',
              failure_reason: null,
              notes: 'Operator refund',
              created_at: '2026-03-11T10:15:00+00:00',
            },
          ],
          click_callback_history: [
            {
              stage: 'complete',
              received_at: '2026-03-11T10:05:00+00:00',
              response: { error: 0, error_note: 'Success' },
            },
          ],
          fiscalization_audit_trail: [
            {
              action: 'payment_fiscalization_failed',
              success: false,
              error_message: 'Temporary provider error',
              occurred_at: '2026-03-11T10:06:00+00:00',
              additional_data: { provider_receipt_id: null },
            },
          ],
          marking_code_activity: [
            {
              id: 11,
              action: 'released',
              code: 'MARK-001',
              order_item_id: 501,
              notes: 'click_fiscalization_failed',
              occurred_at: '2026-03-11T10:07:00+00:00',
              event_metadata: { reason: 'click_fiscalization_failed' },
            },
          ],
          fiscalization: {
            status: 'failed',
            failure_reason: 'Temporary provider error',
          },
        },
      },
    });

    adminService.retryPaymentFiscalization.mockResolvedValue({
      success: true,
      data: {
        fiscalization: { status: 'processing' },
      },
    });
  });

  it('retries failed fiscalization from the order detail modal', async () => {
    const user = userEvent.setup();
    render(<Orders />, { wrapper: createWrapper() });

    await waitFor(() => {
      expect(adminService.getOrders).toHaveBeenCalled();
    });

    await user.click(await screen.findByText(/view_details|View Details/i));

    await waitFor(() => {
      expect(adminService.getOrderDetails).toHaveBeenCalledWith(321);
    });

    await user.click(await screen.findByText(/retry_fiscalization|Retry Fiscalization/i));

    await waitFor(() => {
      expect(adminService.retryPaymentFiscalization).toHaveBeenCalledWith(9001);
    });

    expect(screen.getByText(/Fiscalization Audit Trail/i)).toBeInTheDocument();
    expect(screen.getByText(/Payment Transactions/i)).toBeInTheDocument();
    expect(screen.getByText(/Click Callback History/i)).toBeInTheDocument();
    expect(screen.getByText(/Marking-Code Activity/i)).toBeInTheDocument();
    expect(screen.getAllByText('MARK-001').length).toBeGreaterThan(0);
  });
});
