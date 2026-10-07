/**
 * Order approvals queue (C14): docs/superpowers/specs/2026-09-28-sales-agent-compensation-design.md
 * §6.7, §5.7 and §10.6.
 *
 * Driven through the page a manager uses, down to the HTTP call. Only the axios instance
 * (services/api) is mocked: the real salesService builds every request, so each assertion reads
 * the URL, the body and the request config the backend receives. Every flag the page draws from
 * (`can_decide`, `self_decided`, the published `statuses`) is the backend's, rendered as it comes.
 * The api.js side of "no toast" (a named code is not toasted) is pinned in services/api.test.js.
 */
import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { message } from 'antd';

import SalesOrderApprovals from '../../pages/SalesOrderApprovals';
import api from '../../services/api';
import { ORDER_APPROVAL_HANDLED_CODES } from '../../services/salesService';
import { formatDate, formatDateTimeShort } from '../../utils/dateUtils';

vi.mock('../../services/api', () => ({
  __esModule: true,
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
  getCookie: vi.fn(),
}));
const mockAuth = { hasPermission: vi.fn(() => true) };
vi.mock('../../stores/authStore', () => ({ useAuthStore: () => mockAuth }));
vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    i18n: { language: 'en' },
    // A refusal's copy is looked up by its key, so the key is echoed: it proves which seeded row
    // the page asked for (the rows are pinned by test_admin_ui_payload_fixture_contracts.py). Every
    // other call reads back its English fallback with i18next's `{{token}}` interpolation.
    t: (key, fallback, options) => {
      if (key.includes('.error.')) return key;
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
    message: { success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn(), loading: vi.fn(), destroy: vi.fn(), open: vi.fn() },
  };
});

const HANDLED = { handledErrorCodes: ORDER_APPROVAL_HANDLED_CODES };
const STATUSES = ['pending', 'approved', 'rejected', 'cancelled'];

// Every key `serialize_agent_order_approval` publishes. test_admin_ui_payload_fixture_contracts.py
// holds this set to the backend's own pinned key set (APPROVAL_ROW_KEYS, T-HOLD-11), so a key the
// page reads but the route never sends is `null` here, not silently undefined.
const APPROVAL_ROW_KEYS = new Set([
  'order_id', 'status', 'requested_at', 'decided_at', 'agent', 'outlet', 'visit_id', 'order',
  'earlier_orders', 'decided_by', 'reason', 'self_decided', 'can_decide',
]);
const approvalRow = (overrides) => Object.assign(
  Object.fromEntries([...APPROVAL_ROW_KEYS].map((key) => [key, null])),
  { earlier_orders: [], self_decided: false, can_decide: false },
  overrides,
);

const PENDING = approvalRow({
  order_id: 812,
  status: 'pending',
  requested_at: '2026-10-14T09:12:40+00:00',
  agent: { id: 41, name: 'Aziz Karimov' },
  outlet: { id: 77, name: 'Oasis market' },
  visit_id: 3310,
  order: {
    id: 812,
    order_number: 'SA_000812_26',
    status: 'pending',
    total_amount: 240000.0,
    delivery_date: '2026-10-15',
    delivery_window_start: '09:00',
    delivery_window_end: '12:00',
    payment_method: 'cash',
    items: [
      { product_id: 3, product_name: '19 L', quantity: 12 },
      { product_id: 5, product_name: '5 L', quantity: 4 },
    ],
  },
  earlier_orders: [{
    id: 809,
    order_number: 'SA_000809_26',
    status: 'confirmed',
    total_amount: 120000.0,
    created_at: '2026-10-14T05:40:02+00:00',
    placed_by: { id: 41, name: 'Aziz Karimov' },
  }],
  can_decide: true,
});
// A second held order the viewer may not decide: they placed it or onboarded its outlet (I-24).
// The service answers `can_decide: false`; the page draws its buttons from nothing else.
const NOT_MINE_TO_DECIDE = approvalRow({
  ...PENDING,
  order_id: 815,
  agent: { id: 52, name: 'Nodira Karimova' },
  outlet: { id: 78, name: 'Bahor market' },
  order: {
    ...PENDING.order,
    id: 815,
    order_number: 'SA_000815_26',
    total_amount: 90000.0,
    items: [{ product_id: 3, product_name: '19 L', quantity: 3 }],
  },
  earlier_orders: [{
    id: 811,
    order_number: 'SA_000811_26',
    status: 'cancelled',
    total_amount: 30000.0,
    created_at: '2026-10-14T06:00:00+00:00',
    placed_by: { id: 52, name: 'Nodira Karimova' },
  }],
  can_decide: false,
});
// Decided rows: an admin who was a decision subject (tagged), and an ordinary rejection.
const SELF_APPROVED = approvalRow({
  ...PENDING,
  order_id: 790,
  status: 'approved',
  decided_at: '2026-10-13T07:02:00+00:00',
  order: { ...PENDING.order, id: 790, order_number: 'SA_000790_26', status: 'confirmed' },
  decided_by: { id: 1, name: 'Sabina Admin' },
  self_decided: true,
  can_decide: false,
});
const REJECTED = approvalRow({
  ...PENDING,
  order_id: 791,
  status: 'rejected',
  decided_at: '2026-10-13T07:30:00+00:00',
  order: { ...PENDING.order, id: 791, order_number: 'SA_000791_26', status: 'cancelled' },
  decided_by: { id: 9, name: 'Mansur Manager' },
  reason: 'Duplicate of SA_000789_26',
  can_decide: false,
});

const AGENTS = [{ user_id: 41, full_name: 'Aziz Karimov' }, { user_id: 52, full_name: 'Nodira Karimova' }];

// OA1's `data`: `meta` rides inside it, with the published vocabulary and the badge's count.
const listPage = (items) => ({
  items,
  meta: { page: 1, per_page: 20, total: items.length, pages: 1, has_next: false, has_prev: false },
  statuses: STATUSES,
  pending_count: 2,
});

let queryClient;
// The backend, by URL and by the status asked for. An unexpected read fails loudly.
const mount = (pages = { pending: listPage([PENDING]) }) => {
  api.get.mockImplementation(async (url, config) => {
    if (url === '/admin/sales/order-approvals') {
      const page = pages[config.params.status];
      if (!page) throw new Error(`unexpected status ${config.params.status}`);
      return { data: { success: true, data: page } };
    }
    // `meta` without `has_next` stops fetchAllPages after page 1 (see Visits.test.js).
    if (url === '/admin/staff/sales-agents') return { data: { data: { items: AGENTS }, meta: { total: AGENTS.length } } };
    throw new Error(`unexpected GET ${url}`);
  });
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  vi.spyOn(queryClient, 'invalidateQueries');
  render(<QueryClientProvider client={queryClient}><SalesOrderApprovals /></QueryClientProvider>);
};

const listCalls = () => api.get.mock.calls.filter(([url]) => url === '/admin/sales/order-approvals');
const rowOf = async (orderNumber) => (await screen.findByText(orderNumber)).closest('tr');
// A dialog found by text it renders: under NODE_ENV=test rc-util's useId is a constant, so a
// dialog's accessible name is not reliable (Orders.awaitingNewDate.test.js).
const dialogWithText = async (text) => (await screen.findByText(text)).closest('[role="dialog"]');
const pickStatus = (label) => fireEvent.click(within(document.querySelector('.ant-segmented')).getByText(label));
const sentBody = (call = 0) => JSON.parse(JSON.stringify(api.post.mock.calls[call][1]));

// An axios rejection. A 404/409 carries its code in `data.error_code`; a 403 carries it at the top
// level (§6.5: the page reads both envelopes).
const refusal = (status, code, sentence, { topLevel = false } = {}) => Object.assign(
  new Error(`Request failed with status code ${status}`),
  {
    response: {
      status,
      data: topLevel
        ? { success: false, message: sentence, error_code: code }
        : { success: false, message: sentence, data: { error_code: code, details: { order_id: 812 } } },
    },
  },
);

beforeEach(() => {
  vi.clearAllMocks();
  mockAuth.hasPermission.mockReturnValue(true);
});

describe('the queue', () => {
  it('opens on the pending orders, and a status or agent change asks again from page 1', async () => {
    mount({ pending: listPage([PENDING]), approved: listPage([SELF_APPROVED]) });
    await rowOf('SA_000812_26');

    // toEqual ignores the unset `agent_id`, which axios drops from the wire anyway.
    expect(listCalls()[0][1]).toEqual({ params: { status: 'pending', page: 1, per_page: 20 } });
    expect(listCalls()[0][1].params.agent_id).toBeUndefined();
    // The options are the published `statuses`, in the backend's order; `pending` is selected.
    expect([...document.querySelectorAll('.ant-segmented-item-label')].map((node) => node.textContent)).toEqual(STATUSES);
    expect(document.querySelector('.ant-segmented-item-selected')).toHaveTextContent('pending');

    pickStatus('approved');
    await rowOf('SA_000790_26');
    expect(listCalls().at(-1)[1].params).toMatchObject({ status: 'approved', page: 1 });

    const agentFilter = screen.getByTestId('order-approvals-agent');
    fireEvent.mouseDown(agentFilter.querySelector('.ant-select-selector'));
    const option = () => document.querySelector('.ant-select-dropdown .ant-select-item-option[title="Nodira Karimova"]');
    await waitFor(() => expect(option()).toBeTruthy());
    fireEvent.click(option());
    await waitFor(() => expect(listCalls().at(-1)[1].params).toMatchObject({ status: 'approved', agent_id: 52, page: 1 }));
  });

  it('shows what the manager decides on: the order, its items and money, the earlier orders, the agent and the outlet', async () => {
    mount();
    const row = await rowOf('SA_000812_26');

    expect(within(row).getByText(formatDateTimeShort('2026-10-14T09:12:40+00:00'))).toBeInTheDocument();
    expect(within(row).getByText('Aziz Karimov')).toBeInTheDocument();
    expect(within(row).getByText('Oasis market')).toBeInTheDocument();
    expect(within(row).getByText('19 L × 12')).toBeInTheDocument();
    expect(within(row).getByText('5 L × 4')).toBeInTheDocument();
    expect(within(row).getByText('240,000 UZS')).toBeInTheDocument();
    // The Orders page's payment-method label key, with the raw method as its fallback.
    expect(within(row).getByText('cash')).toBeInTheDocument();
    expect(within(row).getByText(`${formatDate('2026-10-15')} · 09:00–12:00`)).toBeInTheDocument();
    // The earlier order of the day, with its LIVE status as the Orders page colours it.
    const earlier = within(row).getByText('SA_000809_26').closest('.ant-tag');
    expect(earlier).toHaveClass('ant-tag-blue');
    expect(earlier).toHaveAttribute('title', 'confirmed');
    expect(within(row).getByText('pending')).toBeInTheDocument();
  });

  it('says so when nothing is waiting', async () => {
    mount({ pending: listPage([]) });

    expect(await screen.findByText('No orders are waiting for approval.')).toBeInTheDocument();
  });

  it('draws Approve and Reject only where the backend says the viewer may decide', async () => {
    mount({ pending: listPage([PENDING, NOT_MINE_TO_DECIDE]) });
    const mine = await rowOf('SA_000812_26');
    const notMine = await rowOf('SA_000815_26');

    expect(within(mine).getByRole('button', { name: 'Approve' })).toBeInTheDocument();
    expect(within(mine).getByRole('button', { name: 'Reject' })).toBeInTheDocument();
    expect(within(notMine).queryByRole('button', { name: 'Approve' })).toBeNull();
    expect(within(notMine).queryByRole('button', { name: 'Reject' })).toBeNull();
    const cancelledEarlier = within(notMine).getByText('SA_000811_26').closest('.ant-tag');
    expect(cancelledEarlier).toHaveClass('ant-tag-red');
  });

  it('shows who decided, tags a self-decision, and keeps the reason', async () => {
    mount({ pending: listPage([PENDING]), approved: listPage([SELF_APPROVED]), rejected: listPage([REJECTED]) });
    await rowOf('SA_000812_26');

    pickStatus('approved');
    const approved = await rowOf('SA_000790_26');
    expect(within(approved).getByText('Self-decided')).toBeInTheDocument();
    expect(within(approved).getByText('by Sabina Admin')).toBeInTheDocument();
    expect(within(approved).queryByRole('button')).toBeNull();

    pickStatus('rejected');
    const rejected = await rowOf('SA_000791_26');
    expect(within(rejected).getByText('by Mansur Manager')).toBeInTheDocument();
    expect(within(rejected).getByText('Duplicate of SA_000789_26')).toBeInTheDocument();
    expect(within(rejected).queryByText('Self-decided')).toBeNull();
  });

  it('has no pay column and no pay field', async () => {
    mount();
    await rowOf('SA_000812_26');

    expect([...document.querySelectorAll('.ant-table-thead th')].map((th) => th.textContent)).toEqual([
      'Placed', 'Agent', 'Outlet', 'Order', 'Total', 'Payment', 'Delivery', 'Earlier today', 'Status', '',
    ]);
    // C11 / T-HOLD-11: the queue shows order data only.
    expect(document.body.textContent).not.toMatch(/commission|bonus|salary|estimate|penalt|\brate\b/i);
  });
});

describe('deciding', () => {
  it('Approve confirms first, then posts exactly {} and refreshes the queue, the badge and the orders', async () => {
    mount();
    fireEvent.click(within(await rowOf('SA_000812_26')).getByRole('button', { name: 'Approve' }));
    const dialog = await dialogWithText('Approve order SA_000812_26?');
    expect(within(dialog).getByText('It is confirmed and goes to the drivers.')).toBeInTheDocument();
    expect(api.post).not.toHaveBeenCalled();

    api.post.mockResolvedValue({ data: { success: true, data: { approval: { ...PENDING, status: 'approved', can_decide: false } } } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Approve' }));

    await waitFor(() => expect(message.success).toHaveBeenCalledWith('Order approved'));
    expect(api.post).toHaveBeenCalledTimes(1);
    expect(api.post).toHaveBeenCalledWith('/admin/sales/order-approvals/812/approve', {}, HANDLED);
    expect(sentBody()).toStrictEqual({});
    // `['orderApprovals']` is the list's and the nav badge's shared prefix.
    expect(queryClient.invalidateQueries).toHaveBeenCalledWith({ queryKey: ['orderApprovals'] });
    expect(queryClient.invalidateQueries).toHaveBeenCalledWith({ queryKey: ['orders'] });
    expect(message.error).not.toHaveBeenCalled();
  });

  it('Reject stays disabled until a reason is typed, then posts exactly {reason}', async () => {
    mount();
    fireEvent.click(within(await rowOf('SA_000812_26')).getByRole('button', { name: 'Reject' }));
    const dialog = await dialogWithText('Reject order SA_000812_26');
    expect(within(dialog).getByText('The agent sees this reason. The store is only told that the order was cancelled.')).toBeInTheDocument();
    const reject = within(dialog).getByRole('button', { name: 'Reject' });
    expect(reject).toBeDisabled();

    fireEvent.change(within(dialog).getByLabelText('Reason'), { target: { value: '   ' } });
    expect(reject).toBeDisabled();
    fireEvent.change(within(dialog).getByLabelText('Reason'), { target: { value: 'Duplicate of SA_000809_26' } });
    expect(reject).toBeEnabled();

    api.post.mockResolvedValue({ data: { success: true, data: { approval: { ...PENDING, status: 'rejected', can_decide: false } } } });
    fireEvent.click(reject);

    await waitFor(() => expect(message.success).toHaveBeenCalledWith('Order rejected and cancelled'));
    expect(api.post).toHaveBeenCalledTimes(1);
    expect(api.post).toHaveBeenCalledWith(
      '/admin/sales/order-approvals/812/reject',
      { reason: 'Duplicate of SA_000809_26' },
      HANDLED,
    );
    expect(sentBody()).toStrictEqual({ reason: 'Duplicate of SA_000809_26' });
    expect(queryClient.invalidateQueries).toHaveBeenCalledWith({ queryKey: ['orderApprovals'] });
    expect(queryClient.invalidateQueries).toHaveBeenCalledWith({ queryKey: ['orders'] });
  });

  it('explains a SALES_ORDER_APPROVAL_NOT_PENDING refusal once, inline, with no toast, and refetches', async () => {
    mount();
    fireEvent.click(within(await rowOf('SA_000812_26')).getByRole('button', { name: 'Approve' }));
    const dialog = await dialogWithText('Approve order SA_000812_26?');
    const readsBefore = listCalls().length;
    api.post.mockRejectedValue(refusal(409, 'SALES_ORDER_APPROVAL_NOT_PENDING', 'This order is no longer waiting for approval'));

    fireEvent.click(within(dialog).getByRole('button', { name: 'Approve' }));

    const alert = await within(dialog).findByRole('alert');
    expect(alert).toHaveTextContent('sales_agents:order_approvals.error.sales_order_approval_not_pending');
    expect(dialog.querySelectorAll('.ant-alert-error')).toHaveLength(1);
    expect(message.error).not.toHaveBeenCalled();
    expect(message.success).not.toHaveBeenCalled();
    await waitFor(() => expect(listCalls().length).toBeGreaterThan(readsBefore));
    // The dialog stays open, so the manager reads why nothing happened.
    expect(within(dialog).getByText('It is confirmed and goes to the drivers.')).toBeInTheDocument();
  });

  it('explains a 403 self-decision refusal inline from the top-level envelope', async () => {
    mount();
    fireEvent.click(within(await rowOf('SA_000812_26')).getByRole('button', { name: 'Reject' }));
    const dialog = await dialogWithText('Reject order SA_000812_26');
    fireEvent.change(within(dialog).getByLabelText('Reason'), { target: { value: 'Duplicate' } });
    api.post.mockRejectedValue(refusal(403, 'SALES_PAY_SELF_DECISION', 'You cannot decide about your own pay', { topLevel: true }));

    fireEvent.click(within(dialog).getByRole('button', { name: 'Reject' }));

    const alert = await within(dialog).findByRole('alert');
    expect(alert).toHaveTextContent('sales_agents:order_approvals.error.sales_pay_self_decision');
    expect(dialog.querySelectorAll('.ant-alert-error')).toHaveLength(1);
    expect(message.error).not.toHaveBeenCalled();
  });

  it('leaves a failure it does not explain to the api.js toast', async () => {
    mount();
    fireEvent.click(within(await rowOf('SA_000812_26')).getByRole('button', { name: 'Approve' }));
    const dialog = await dialogWithText('Approve order SA_000812_26?');
    api.post.mockRejectedValue(Object.assign(new Error('Request failed with status code 500'), {
      response: { status: 500, data: { success: false, message: 'Internal error' } },
    }));

    fireEvent.click(within(dialog).getByRole('button', { name: 'Approve' }));

    await waitFor(() => expect(api.post).toHaveBeenCalledTimes(1));
    // api.js toasted it (a 5xx names no code); a page message would be the second one.
    await waitFor(() => expect(within(dialog).getByRole('button', { name: 'Approve' })).toBeEnabled());
    expect(within(dialog).queryByRole('alert')).toBeNull();
    expect(message.error).not.toHaveBeenCalled();
  });
});
