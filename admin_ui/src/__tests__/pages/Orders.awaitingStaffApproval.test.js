/**
 * Orders page: an agent's same-day order held for a manager's approval (C14,
 * docs/superpowers/specs/2026-09-28-sales-agent-compensation-design.md §6.6 and §10.6).
 *
 * Driven through the page an admin uses; only the axios instance (services/api) is mocked. The
 * flag is the backend's own `awaiting_staff_approval` (serialize_order_admin), rendered as it
 * comes. The held order is confirmed only from the approval queue, so the status form does not
 * offer `confirmed` for it; every other order is unchanged.
 */
import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import dayjs from 'dayjs';

import Orders from '../../pages/Orders';
import api from '../../services/api';
import { useAuthStore } from '../../stores/authStore';

vi.mock('../../services/api', () => ({
  __esModule: true,
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
  getCookie: vi.fn(),
}));
vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key, fallback, options) => {
      const text = typeof fallback === 'string' ? fallback : key;
      const values = options || {};
      return text.replace(/\{\{(\w+)\}\}/g, (_, token) => (
        values[token] !== undefined ? String(values[token]) : `{{${token}}}`
      ));
    },
  }),
}));
vi.mock('antd', async () => {
  const actual = await vi.importActual('antd');
  return {
    ...actual,
    // Row actions as plain buttons: the pattern every Orders page test uses.
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
    message: { success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn(), loading: vi.fn(), destroy: vi.fn(), open: vi.fn() },
  };
});

const TODAY = dayjs().format('YYYY-MM-DD');
const TAG = 'Awaiting manager approval';
const QUEUE_LINK = 'Open order approvals';

// GET /orders/statuses: every order status, and shared/status_transitions.ORDER_STATUS_TRANSITIONS.
const STATUSES = [
  { value: 'pending', label: 'Pending' },
  { value: 'confirmed', label: 'Confirmed' },
  { value: 'preparing', label: 'Preparing' },
  { value: 'out_for_delivery', label: 'Out for delivery' },
  { value: 'delivered', label: 'Delivered' },
  { value: 'cancelled', label: 'Cancelled' },
  { value: 'returned', label: 'Returned' },
];
const TRANSITIONS = {
  pending: ['confirmed', 'cancelled'],
  confirmed: ['preparing', 'delivered', 'cancelled'],
  preparing: ['out_for_delivery', 'cancelled'],
  out_for_delivery: ['delivered', 'returned', 'cancelled'],
  delivered: [],
  cancelled: [],
  returned: ['pending'],
};
const REASON_REQUIRED = ['cancelled', 'returned'];

// A list row: serialize_order_admin + order_schedule_fields(detail=False), for an agent's order.
const ROW = {
  id: 321,
  order_number: 'SA_000321_26',
  user_id: 77,
  status: 'pending',
  payment_method: 'cash',
  payment_status: 'pending',
  total_amount: 240000,
  customer_name: 'Oasis market',
  customer_email: null,
  customer_phone: '+998901234500',
  created_at: '2026-10-14T09:12:40+00:00',
  items_summary: [],
  items_count: 0,
  delivery_date: TODAY,
  delivery_window: null,
  awaiting_release: false,
  release_at: null,
  can_reschedule: true,
  reschedule_block_code: null,
  awaiting_new_date: false,
  awaiting_staff_approval: false,
};
// The agent's second order at that outlet today, held (C14): its status is still `pending`.
const HELD = { awaiting_staff_approval: true };

// GET /admin/orders/<id>: the row plus the detail-only blocks the page reads.
const detailOf = (overrides = {}) => ({
  ...ROW,
  payment_timeline: { timeline: [] },
  marking_code_summary: { events: {}, codes_by_order_item: {} },
  delivery: null,
  closing_reason: null,
  reschedule_notifies_customer: true,
  reschedule_customer_channel: 'telegram',
  reschedule_driver_losing_stop: null,
  reschedule_min_date: TODAY,
  reschedule_max_date: dayjs().add(15, 'day').format('YYYY-MM-DD'),
  ...overrides,
});

// The backend, by URL. An unexpected read fails loudly instead of rendering nothing.
const renderPage = ({ rows = [ROW], detail = detailOf() } = {}) => {
  api.get.mockImplementation(async (url) => {
    if (url === '/orders/statuses') {
      return { data: { data: { statuses: STATUSES, transitions: TRANSITIONS, reason_required_statuses: REASON_REQUIRED } } };
    }
    if (url === '/admin/orders') return { data: { success: true, data: { items: rows }, meta: { total: rows.length } } };
    if (url === `/admin/orders/${detail.id}`) return { data: { success: true, data: { order: detail } } };
    if (url === `/admin/orders/${detail.id}/edit-history`) return { data: { success: true, data: { entries: [] } } };
    throw new Error(`unexpected GET ${url}`);
  });
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  // The queue link is a router Link; the app renders the page inside its router.
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter><Orders /></MemoryRouter>
    </QueryClientProvider>,
  );
};

// The flag comes from the real auth store, the way usePermissions() reads it.
const grant = (permissions) => useAuthStore.setState({ permissions });

const dialogWithText = async (text) => (await screen.findByText(text)).closest('[role="dialog"]');

const clickRowAction = async (name) => {
  const action = await screen.findByRole('button', { name });
  await waitFor(() => expect(action).toBeEnabled());
  fireEvent.click(action);
};

// The option titles of the status Select, once GET /orders/statuses has published them.
const statusOptions = async (modal) => {
  fireEvent.mouseDown(modal.querySelector('.ant-select-selector'));
  const options = () => [...document.querySelectorAll('.ant-select-dropdown .ant-select-item-option')];
  await waitFor(() => expect(options().length).toBeGreaterThan(0));
  return options().map((option) => option.getAttribute('title'));
};

beforeEach(() => {
  vi.clearAllMocks();
  grant({});
});

afterAll(() => {
  grant({});
});

describe('the list (C14)', () => {
  it('tags only the held rows, beside their status, with the way to the queue for a reviewer', async () => {
    grant({ can_review_agent_orders: true });
    renderPage({ rows: [{ ...ROW, ...HELD }, { ...ROW, id: 322, order_number: 'SA_000322_26' }] });

    const held = (await screen.findByText('SA_000321_26')).closest('tr');
    const tag = within(held).getByText(TAG);
    // Same cell as the order's own status tag.
    expect(within(tag.closest('td')).getByText('pending')).toBeInTheDocument();
    expect(within(held).getByRole('link', { name: QUEUE_LINK })).toHaveAttribute('href', '/sales/order-approvals');

    const other = screen.getByText('SA_000322_26').closest('tr');
    expect(within(other).queryByText(TAG)).toBeNull();
    expect(within(other).queryByRole('link', { name: QUEUE_LINK })).toBeNull();
  });

  it('shows the tag but no queue link to a user who cannot review agent orders', async () => {
    renderPage({ rows: [{ ...ROW, ...HELD }] });

    const held = (await screen.findByText('SA_000321_26')).closest('tr');
    expect(within(held).getByText(TAG)).toBeInTheDocument();
    expect(within(held).queryByRole('link', { name: QUEUE_LINK })).toBeNull();
  });
});

describe('the order detail (C14)', () => {
  it('flags the held order beside its status', async () => {
    grant({ can_review_agent_orders: true });
    renderPage({ rows: [{ ...ROW, ...HELD }], detail: detailOf(HELD) });
    await clickRowAction('View Details');
    const detail = await dialogWithText('Order Details - SA_000321_26');

    const tag = await within(detail).findByText(TAG);
    expect(within(tag.closest('td')).getByText('pending')).toBeInTheDocument();
    expect(within(tag.closest('td')).getByRole('link', { name: QUEUE_LINK })).toHaveAttribute('href', '/sales/order-approvals');
  });
});

describe('Update Status (C14)', () => {
  it('offers no Confirmed for a held order', async () => {
    renderPage({ rows: [{ ...ROW, ...HELD }], detail: detailOf(HELD) });
    await clickRowAction('Update Status');
    const modal = await dialogWithText('Update Order Status - SA_000321_26');

    expect(await statusOptions(modal)).toEqual(['Cancelled']);
  });

  it('still offers Confirmed for a pending order that is not held', async () => {
    renderPage();
    await clickRowAction('Update Status');
    const modal = await dialogWithText('Update Order Status - SA_000321_26');

    expect(await statusOptions(modal)).toEqual(['Confirmed', 'Cancelled']);
  });
});
