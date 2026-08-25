/**
 * B3 — the admin's two "Open Payment Link" affordances must ask the BACKEND
 * whether a link is payable, not infer it from "we stored a link once".
 *
 * Both `Orders.js:1479` and `:1953` were gated on `selectedOrder.payment_link`
 * truthiness alone. `payment_link` is an ARCHIVAL column: it survives
 * cancellation, delivery, settlement in cash at the door and conversion of the
 * rail. So the admin was offered a live pay link for orders that cannot be paid
 * — and, under the 2026-08-24 policy, was given no way to tell that apart from
 * a case-B order whose link really is still live.
 *
 * The backend now publishes `payable_payment_link`: the stored URL, non-null
 * ONLY when `order_is_payable_online` says following it would work. Gate and
 * href are one value, so a button aimed at a dead link is not writable. The raw
 * `payment_link` stays published so the audit row can still distinguish
 * "issued, now dead" from "never issued" — which is exactly why one boolean
 * could not serve both questions.
 */
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

const PAYABLE_URL = 'https://my.click.uz/services/pay?id=1&t=CASE-B';
const DEAD_URL = 'https://my.click.uz/services/pay?id=1&t=DEAD';

const createWrapper = () => {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }) => <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
};

const baseRow = {
  id: 321,
  order_number: 'ORD-B3-321',
  user_id: 77,
  total_amount: 18000,
  customer_name: 'Ali Buyer',
  customer_email: 'ali@example.com',
  customer_phone: '+998901234500',
  created_at: '2026-08-24T10:00:00+00:00',
  items_summary: [],
  item_count: 0,
  payment_timeline: { timeline: [] },
  marking_code_summary: { events: {}, codes_by_order_item: {} },
};

function mountWithOrder(order) {
  api.get.mockResolvedValue({
    data: { data: { statuses: [{ value: 'pending', label: 'Pending' }] } },
  });
  adminService.getOrders.mockResolvedValue({
    data: { items: [{ ...baseRow, ...order }] },
    meta: { total: 1 },
  });
  adminService.getOrderDetails.mockResolvedValue({
    success: true,
    data: { order: { ...baseRow, ...order } },
  });
  adminService.getOrderEditHistory.mockResolvedValue({ success: true, data: { entries: [] } });
}

async function openDetailModal() {
  const user = userEvent.setup();
  render(<Orders />, { wrapper: createWrapper() });
  await waitFor(() => expect(adminService.getOrders).toHaveBeenCalled());
  await user.click(await screen.findByText(/view_details|View Details/i));
  await waitFor(() => expect(adminService.getOrderDetails).toHaveBeenCalledWith(321));
  return user;
}

describe('Orders payment-link affordances follow the published payability', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('offers both links for a case-B order — delivered, unpaid, still payable', async () => {
    mountWithOrder({
      status: 'delivered',
      payment_method: 'click',
      payment_status: 'pending',
      outstanding_amount: 18000,
      payment_link: PAYABLE_URL,
      is_payable: true,
      payable_payment_link: PAYABLE_URL,
    });

    await openDetailModal();

    const anchor = await screen.findByText('Open payment link');
    expect(anchor.closest('a')).toHaveAttribute('href', PAYABLE_URL);

    const button = await screen.findByText('Open Payment Link');
    expect(button.closest('a')).toHaveAttribute('href', PAYABLE_URL);
  });

  it('withholds both links for a cancelled order that still carries a stored link', async () => {
    mountWithOrder({
      status: 'cancelled',
      payment_method: 'click',
      payment_status: 'cancelled',
      outstanding_amount: 0,
      payment_link: DEAD_URL,
      is_payable: false,
      payable_payment_link: null,
    });

    await openDetailModal();

    expect(screen.queryByText('Open payment link')).not.toBeInTheDocument();
    expect(screen.queryByText('Open Payment Link')).not.toBeInTheDocument();
    // The link is never rendered as a target, in either place.
    expect(document.querySelector(`a[href="${DEAD_URL}"]`)).toBeNull();
  });

  it('still says a link was ISSUED, so a dead link is distinguishable from none', async () => {
    mountWithOrder({
      status: 'cancelled',
      payment_method: 'click',
      payment_status: 'cancelled',
      outstanding_amount: 0,
      payment_link: DEAD_URL,
      is_payable: false,
      payable_payment_link: null,
    });

    await openDetailModal();

    expect(await screen.findByText('Link issued, no longer payable')).toBeInTheDocument();
  });

  it('shows a plain dash when no link was ever issued', async () => {
    mountWithOrder({
      status: 'pending',
      payment_method: 'cash',
      payment_status: 'pending',
      outstanding_amount: 18000,
      payment_link: null,
      is_payable: false,
      payable_payment_link: null,
    });

    await openDetailModal();

    expect(screen.queryByText('Link issued, no longer payable')).not.toBeInTheDocument();
    expect(screen.queryByText('Open payment link')).not.toBeInTheDocument();
  });
});
