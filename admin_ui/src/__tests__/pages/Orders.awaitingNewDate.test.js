/**
 * Orders page: a failed delivery waits for a new date
 * (docs/superpowers/specs/2026-09-25-failed-delivery-awaits-new-date-design.md §3.4, §3.6;
 * F6, F8, F18, F19).
 *
 * Driven through the page an admin uses, down to the HTTP call. Only the axios instance
 * (services/api) is mocked. The real adminService builds every request, so each assertion reads
 * the URL, the body and the request config the backend receives. Every flag here is a field the
 * backend publishes (`awaiting_new_date`, `closing_reason`), rendered as it comes. The page works
 * none of them out.
 */
import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { message } from 'antd';
import dayjs from 'dayjs';

import Orders from '../../pages/Orders';
import api from '../../services/api';
import { formatDateTimeShort } from '../../utils/dateUtils';

vi.mock('../../services/api', () => ({
  __esModule: true,
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
  getCookie: vi.fn(),
}));
vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    // The positional English fallback PLUS i18next's `{{token}}` interpolation, so the page reads
    // back what an admin sees, never `{{number}}` or `{{reason}}`.
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
const TAG = 'Delivery failed: needs a new date';
const NOTICE = "This order's delivery failed and is waiting for a new date.";
const REASON_LABEL = 'Reason (internal, not shown to the customer)';
const NOTE_LABEL = 'Note to the customer (optional)';
// The refusals the page explains itself, named on every status PUT so api.js does not toast them
// as well: one refusal, one message. The interceptor side is pinned in services/api.test.js.
const HANDLED = { handledErrorCodes: ['ORDER_AWAITING_NEW_DATE', 'ADMIN_REASON_REQUIRED', 'ADMIN_REASON_TOO_LONG'] };

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
// ...and its `reason_required_statuses`, from order_service.ADMIN_REASON_REQUIRED_STATUSES: the
// statuses whose PUT must carry a reason.
const REASON_REQUIRED = ['cancelled', 'returned'];

// A list row: serialize_order_admin + order_schedule_fields(detail=False).
const ROW = {
  id: 321,
  order_number: 'ORD-321',
  user_id: 77,
  status: 'out_for_delivery',
  payment_method: 'cash',
  payment_status: 'pending',
  total_amount: 18000,
  customer_name: 'Ali Buyer',
  customer_email: 'ali@example.com',
  customer_phone: '+998901234500',
  created_at: '2026-09-20T10:00:00+00:00',
  items_summary: [],
  items_count: 0,
  delivery_date: TODAY,
  delivery_window: { start: '09:00', end: '12:00', kind: 'between', label: '09:00-12:00' },
  awaiting_release: false,
  release_at: null,
  can_reschedule: true,
  reschedule_block_code: null,
  awaiting_new_date: false,
};
// The same order after its delivery failed. Its status is unchanged (F1), and the backend flags it.
const AWAITING = { awaiting_new_date: true };
const FAILED_DELIVERY = { id: 55, status: 'failed', tracking_number: 'TRK-55' };

// GET /admin/orders/<id>: the row plus the detail-only blocks the page and the modal read.
const detailOf = (overrides = {}) => ({
  ...ROW,
  payment_timeline: { timeline: [] },
  marking_code_summary: { events: {}, codes_by_order_item: {} },
  delivery: { id: 55, status: 'in_transit', tracking_number: 'TRK-55' },
  closing_reason: null,
  reschedule_notifies_customer: true,
  reschedule_customer_channel: 'telegram',
  reschedule_driver_losing_stop: null,
  reschedule_min_date: TODAY,
  reschedule_max_date: dayjs().add(15, 'day').format('YYYY-MM-DD'),
  ...overrides,
});

// The backend, by URL. An unexpected read fails loudly instead of rendering nothing.
const renderPage = ({ rows = [ROW], detail = detailOf(), reasonRequired = REASON_REQUIRED } = {}) => {
  api.get.mockImplementation(async (url) => {
    if (url === '/orders/statuses') {
      return { data: { data: { statuses: STATUSES, transitions: TRANSITIONS, reason_required_statuses: reasonRequired } } };
    }
    if (url === '/admin/orders') return { data: { success: true, data: { items: rows }, meta: { total: rows.length } } };
    if (url === `/admin/orders/${detail.id}`) return { data: { success: true, data: { order: detail } } };
    if (url === `/admin/orders/${detail.id}/edit-history`) return { data: { success: true, data: { entries: [] } } };
    throw new Error(`unexpected GET ${url}`);
  });
  api.put.mockResolvedValue({ data: { success: true, message: 'Order status updated', data: {} } });
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={queryClient}><Orders /></QueryClientProvider>);
};

// A dialog found by text it renders, not by its accessible name. Under NODE_ENV=test, rc-util's
// useId answers the constant 'test-id', so with two modals open both carry the first one's title.
const dialogWithText = async (text) => (await screen.findByText(text)).closest('[role="dialog"]');

const clickRowAction = async (name) => {
  const action = await screen.findByRole('button', { name });
  // Cancel is enabled once GET /orders/statuses has published the transitions.
  await waitFor(() => expect(action).toBeEnabled());
  fireEvent.click(action);
};

const pickStatus = async (modal, label) => {
  fireEvent.mouseDown(modal.querySelector('.ant-select-selector'));
  const option = () => document.querySelector(`.ant-select-dropdown .ant-select-item-option[title="${label}"]`);
  await waitFor(() => expect(option()).toBeTruthy());
  fireEvent.click(option());
};

// What axios puts on the wire. JSON drops an undefined key, so an absent `notes` is really absent.
const sentBody = (call = 0) => JSON.parse(JSON.stringify(api.put.mock.calls[call][1]));

// An axios rejection shaped like validation_error_response: the service's English sentence in
// `errors[0]`, the fence code in `data.error_code`.
const apiError = (sentence, errorCode) => Object.assign(new Error('Request failed with status code 400'), {
  response: {
    status: 400,
    data: { success: false, message: 'Validation failed', errors: [sentence], data: { error_code: errorCode } },
  },
});

beforeEach(() => {
  vi.clearAllMocks();
});

describe('the list (F8)', () => {
  it('flags only the rows the backend marks awaiting_new_date', async () => {
    renderPage({ rows: [{ ...ROW, ...AWAITING }, { ...ROW, id: 322, order_number: 'ORD-322' }] });

    const flagged = (await screen.findByText('ORD-321')).closest('tr');
    expect(within(flagged).getByText(TAG)).toBeInTheDocument();
    expect(within(screen.getByText('ORD-322').closest('tr')).queryByText(TAG)).toBeNull();
  });

  it('the Delivery failed toggle asks for delivery_failed=true from page 1, and drops it when off', async () => {
    renderPage();
    await screen.findByText('ORD-321');
    const lastListParams = () => api.get.mock.calls.filter(([url]) => url === '/admin/orders').at(-1)[1].params;
    expect(lastListParams()).not.toHaveProperty('delivery_failed');

    fireEvent.click(screen.getByRole('switch', { name: 'Delivery failed' }));
    await waitFor(() => expect(lastListParams()).toMatchObject({ delivery_failed: 'true', page: 1 }));

    fireEvent.click(screen.getByRole('switch', { name: 'Delivery failed' }));
    await waitFor(() => expect(lastListParams()).not.toHaveProperty('delivery_failed'));
  });
});

describe('the order detail (F8, F19)', () => {
  it('flags the failed delivery beside its status, and shows no closing reason while the order is live', async () => {
    renderPage({ rows: [{ ...ROW, ...AWAITING }], detail: detailOf({ ...AWAITING, delivery: FAILED_DELIVERY }) });
    await clickRowAction('View Details');
    const detail = await dialogWithText('Order Details - ORD-321');

    const tag = await within(detail).findByText(TAG);
    // Same cell as the delivery's own status tag.
    expect(within(tag.closest('td')).getByText('failed')).toBeInTheDocument();
    expect(within(detail).queryByText(/Internal reason/)).toBeNull();
  });

  it('shows the closing reason next to the status: the reason, what it closed, who and when', async () => {
    const CHANGED_AT = '2026-09-25T09:03:00+00:00';
    renderPage({
      rows: [{ ...ROW, status: 'cancelled', can_reschedule: false }],
      detail: detailOf({
        status: 'cancelled',
        can_reschedule: false,
        delivery: { id: 55, status: 'cancelled', tracking_number: 'TRK-55' },
        closing_reason: {
          status: 'cancelled',
          reason: 'Customer moved away',
          changed_by_name: 'Aziz Karimov',
          changed_at: CHANGED_AT,
        },
      }),
    });
    await clickRowAction('View Details');
    const detail = await dialogWithText('Order Details - ORD-321');

    const reason = await within(detail).findByText('Internal reason: Customer moved away');
    expect(
      within(reason.closest('td')).getByText(`cancelled · Aziz Karimov · ${formatDateTimeShort(CHANGED_AT)}`),
    ).toBeInTheDocument();
  });
});

describe('the row-menu Cancel (F6)', () => {
  it('asks for the internal reason and sends only the status and that reason', async () => {
    renderPage();
    await clickRowAction('Cancel Order');
    const dialog = await dialogWithText('Cancel order ORD-321?');

    // A blank reason is no reason: no request.
    fireEvent.change(within(dialog).getByLabelText(REASON_LABEL), { target: { value: '   ' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel Order' }));
    expect(await within(dialog).findByText('Reason is required')).toBeInTheDocument();
    expect(api.put).not.toHaveBeenCalled();

    fireEvent.change(within(dialog).getByLabelText(REASON_LABEL), { target: { value: '  Customer moved away  ' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel Order' }));

    await waitFor(() => expect(api.put).toHaveBeenCalledTimes(1));
    expect(api.put.mock.calls[0][0]).toBe('/admin/orders/321/status');
    // No `notes`. The customer's timeline no longer gets a "Cancelled by admin" line in the
    // admin's UI language, and the reason stays internal.
    expect(sentBody()).toStrictEqual({ status: 'cancelled', reason: 'Customer moved away' });
    expect(api.put.mock.calls[0][2]).toStrictEqual(HANDLED);
    expect(message.success).toHaveBeenCalledWith('Order status updated successfully');
    await waitFor(() => expect(screen.queryByText('Cancel order ORD-321?')).toBeNull());
  });

  it('on an order awaiting a new date, leads with Reschedule instead, which opens the re-date and cancels nothing', async () => {
    renderPage({ rows: [{ ...ROW, ...AWAITING }], detail: detailOf({ ...AWAITING, delivery: FAILED_DELIVERY }) });
    await clickRowAction('Cancel Order');
    const dialog = await dialogWithText('Cancel order ORD-321?');

    expect(within(dialog).getByText(NOTICE)).toBeInTheDocument();
    const instead = within(dialog).getByRole('button', { name: 'Reschedule instead' });
    // The way out that keeps the order is the primary action. Cancelling is still there, but it
    // is not the default.
    expect(instead).toHaveClass('ant-btn-primary');
    expect(within(dialog).getByRole('button', { name: 'Cancel Order' })).not.toHaveClass('ant-btn-primary');

    fireEvent.click(instead);
    const reschedule = await dialogWithText('Reschedule delivery');
    await within(reschedule).findByRole('button', { name: 'Save' });
    expect(api.get).toHaveBeenCalledWith('/admin/orders/321');
    expect(screen.queryByText('Cancel order ORD-321?')).toBeNull();
    expect(api.put).not.toHaveBeenCalled();
  });

  it.each([
    ['ADMIN_REASON_REQUIRED', 'Enter a reason for cancelling or returning this order.'],
    ['ADMIN_REASON_TOO_LONG', 'The reason can be at most 100 characters.'],
  ])("explains a %s refusal once, in the admin's language, and stays open", async (code, copy) => {
    renderPage();
    api.put.mockRejectedValue(apiError('server sentence', code));
    await clickRowAction('Cancel Order');
    const dialog = await dialogWithText('Cancel order ORD-321?');
    fireEvent.change(within(dialog).getByLabelText(REASON_LABEL), { target: { value: 'Customer moved away' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel Order' }));

    await waitFor(() => expect(message.error).toHaveBeenCalledTimes(1));
    expect(message.error).toHaveBeenCalledWith(copy);
    expect(api.put.mock.calls[0][2]).toStrictEqual(HANDLED);
    expect(screen.getByText('Cancel order ORD-321?')).toBeInTheDocument();
  });
});

describe('Update Status (F6, F18)', () => {
  it.each(['Cancelled', 'Returned'])('asks for an internal reason for %s, beside the customer-visible note', async (label) => {
    renderPage();
    await clickRowAction('Update Status');
    const modal = await dialogWithText('Update Order Status - ORD-321');
    expect(within(modal).getByText(NOTE_LABEL)).toBeInTheDocument();
    expect(within(modal).queryByText(REASON_LABEL)).toBeNull();

    await pickStatus(modal, label);
    expect(await within(modal).findByText(REASON_LABEL)).toBeInTheDocument();
    expect(within(modal).getByText(NOTE_LABEL)).toBeInTheDocument();
  });

  it('asks for the reason, behind a confirm, only for the statuses the backend publishes', async () => {
    // The rule is the backend's `reason_required_statuses`, never a copy in the page. Published
    // without `returned`, a Return asks no reason and goes out unconfirmed.
    renderPage({ reasonRequired: ['cancelled'] });
    await clickRowAction('Update Status');
    const modal = await dialogWithText('Update Order Status - ORD-321');
    await pickStatus(modal, 'Returned');
    expect(within(modal).queryByText(REASON_LABEL)).toBeNull();
    fireEvent.click(within(modal).getByRole('button', { name: 'Update Status' }));

    await waitFor(() => expect(api.put).toHaveBeenCalledTimes(1));
    expect(sentBody()).toStrictEqual({ status: 'returned', notes: '' });
    expect(screen.queryByText('Mark order ORD-321 as returned?')).toBeNull();
  });

  it('confirms a Return, then sends the internal reason and the customer note apart', async () => {
    renderPage();
    await clickRowAction('Update Status');
    const modal = await dialogWithText('Update Order Status - ORD-321');
    await pickStatus(modal, 'Returned');
    fireEvent.change(await within(modal).findByLabelText(REASON_LABEL), { target: { value: ' Refused at the door ' } });
    fireEvent.change(within(modal).getByLabelText(NOTE_LABEL), {
      target: { value: 'Write to us in the bot to arrange a new delivery' },
    });
    fireEvent.click(within(modal).getByRole('button', { name: 'Update Status' }));

    const confirm = await dialogWithText('Mark order ORD-321 as returned?');
    expect(api.put).not.toHaveBeenCalled();
    // The reason was typed in the modal, so the confirmation does not ask again.
    expect(within(confirm).queryByText(REASON_LABEL)).toBeNull();
    fireEvent.click(within(confirm).getByRole('button', { name: 'Mark as returned' }));

    await waitFor(() => expect(api.put).toHaveBeenCalledTimes(1));
    expect(api.put.mock.calls[0][0]).toBe('/admin/orders/321/status');
    expect(sentBody()).toStrictEqual({
      status: 'returned',
      notes: 'Write to us in the bot to arrange a new delivery',
      reason: 'Refused at the door',
    });
    expect(api.put.mock.calls[0][2]).toStrictEqual(HANDLED);
  });

  it('on an order awaiting a new date, the modal and its Cancel confirmation both lead with Reschedule instead', async () => {
    renderPage({ rows: [{ ...ROW, ...AWAITING }], detail: detailOf({ ...AWAITING, delivery: FAILED_DELIVERY }) });
    await clickRowAction('Update Status');
    const modal = await dialogWithText('Update Order Status - ORD-321');
    expect(within(modal).getByText(NOTICE)).toBeInTheDocument();
    expect(within(modal).getByRole('button', { name: 'Reschedule instead' })).toHaveClass('ant-btn-primary');
    expect(within(modal).getByRole('button', { name: 'Update Status' })).not.toHaveClass('ant-btn-primary');

    await pickStatus(modal, 'Cancelled');
    fireEvent.change(await within(modal).findByLabelText(REASON_LABEL), { target: { value: 'Customer moved away' } });
    fireEvent.click(within(modal).getByRole('button', { name: 'Update Status' }));

    const confirm = await dialogWithText('Cancel order ORD-321?');
    expect(within(confirm).getByText(NOTICE)).toBeInTheDocument();
    expect(within(confirm).getByRole('button', { name: 'Cancel Order' })).not.toHaveClass('ant-btn-primary');
    fireEvent.click(within(confirm).getByRole('button', { name: 'Reschedule instead' }));

    const reschedule = await dialogWithText('Reschedule delivery');
    await within(reschedule).findByRole('button', { name: 'Save' });
    expect(screen.queryByText('Cancel order ORD-321?')).toBeNull();
    expect(api.put).not.toHaveBeenCalled();
  });

  it('asks for the reason on a returned order too, because the backend asks it of every returned save', async () => {
    // Task 8's route wants a reason on every PUT that carries cancelled or returned, changed or
    // not. The modal opens on the order's own status, so it asks from the start. Otherwise the
    // admin would get "Enter a reason…" with no reason field on screen.
    renderPage({ rows: [{ ...ROW, status: 'returned', can_reschedule: false }] });
    api.put.mockRejectedValue(apiError('Cannot change status from returned to returned', 'ORDER_STATUS_TRANSITION_INVALID'));
    await clickRowAction('Update Status');
    const modal = await dialogWithText('Update Order Status - ORD-321');

    fireEvent.change(await within(modal).findByLabelText(REASON_LABEL), { target: { value: 'Refused at the door' } });
    fireEvent.change(within(modal).getByLabelText(NOTE_LABEL), { target: { value: 'We will call you' } });
    fireEvent.click(within(modal).getByRole('button', { name: 'Update Status' }));
    const confirm = await dialogWithText('Mark order ORD-321 as returned?');
    fireEvent.click(within(confirm).getByRole('button', { name: 'Mark as returned' }));

    await waitFor(() => expect(api.put).toHaveBeenCalledTimes(1));
    expect(sentBody()).toStrictEqual({ status: 'returned', notes: 'We will call you', reason: 'Refused at the door' });
    // The reason travelled, so what comes back is the backend's own answer about the move.
    await waitFor(() => expect(message.error).toHaveBeenCalledTimes(1));
    expect(message.error).toHaveBeenCalledWith('Cannot change status from returned to returned');
  });

  it('opens its confirmation on top of the modal, even after an earlier row-menu Cancel was closed', async () => {
    renderPage();
    // The row-menu Cancel, opened and closed without cancelling, before Update Status is ever opened.
    await clickRowAction('Cancel Order');
    const earlier = await dialogWithText('Cancel order ORD-321?');
    fireEvent.click(within(earlier).getByText('Close'));
    await waitFor(() => expect(screen.queryByText('Cancel order ORD-321?')).toBeNull());

    await clickRowAction('Update Status');
    const modal = await dialogWithText('Update Order Status - ORD-321');
    await pickStatus(modal, 'Cancelled');
    fireEvent.change(await within(modal).findByLabelText(REASON_LABEL), { target: { value: 'Customer moved away' } });
    fireEvent.click(within(modal).getByRole('button', { name: 'Update Status' }));

    // Sibling antd modals share one z-index, so the portal appended to <body> last is drawn on
    // top. The confirmation must be that one, or it opens hidden under the modal that asked for it.
    const confirm = await dialogWithText('Cancel order ORD-321?');
    const roots = document.body.querySelectorAll('.ant-modal-root');
    expect(roots[roots.length - 1]).toContainElement(confirm);
    expect(api.put).not.toHaveBeenCalled();
  });

  it("explains a forward move the backend refuses (F18) once, in the admin's language", async () => {
    const confirmedAwaiting = { ...ROW, ...AWAITING, status: 'confirmed' };
    renderPage({ rows: [confirmedAwaiting], detail: detailOf({ ...confirmedAwaiting, delivery: FAILED_DELIVERY }) });
    api.put.mockRejectedValue(apiError(
      "Order ORD-321's delivery failed and is waiting for a new date; reschedule it before moving the order forward",
      'ORDER_AWAITING_NEW_DATE',
    ));
    await clickRowAction('Update Status');
    const modal = await dialogWithText('Update Order Status - ORD-321');
    // Preparing is on offer: the page keeps no list of blocked moves, and the backend decides.
    await pickStatus(modal, 'Preparing');
    fireEvent.click(within(modal).getByRole('button', { name: 'Update Status' }));

    // Not a cancel or a return, so there is no confirmation and no reason.
    await waitFor(() => expect(api.put).toHaveBeenCalledTimes(1));
    expect(sentBody()).toStrictEqual({ status: 'preparing', notes: '' });
    expect(api.put.mock.calls[0][2]).toStrictEqual(HANDLED);
    await waitFor(() => expect(message.error).toHaveBeenCalledTimes(1));
    expect(message.error).toHaveBeenCalledWith(
      "This order's delivery failed and is waiting for a new date. Reschedule it before moving it forward.",
    );
    expect(message.success).not.toHaveBeenCalled();
  });
});
