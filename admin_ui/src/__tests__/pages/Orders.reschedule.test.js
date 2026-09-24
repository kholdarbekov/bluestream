/**
 * Orders page → Reschedule (docs/superpowers/specs/2026-09-23-admin-order-reschedule-design.md §6).
 *
 * Driven through the page an admin uses. The row action and the detail footer open
 * RescheduleOrderModal. The modal re-reads GET /admin/orders/<id> and PATCHes
 * /admin/orders/<id>/schedule through adminService. Only the service layer is mocked: every
 * gate, banner and bound below is a field the backend publishes (Task 8), rendered as it comes.
 */
import React from 'react';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider, onlineManager } from '@tanstack/react-query';
import { message } from 'antd';
import dayjs from 'dayjs';

import Orders from '../../pages/Orders';
import adminService from '../../services/adminService';
import api from '../../services/api';
import { formatDate, formatDateTimeShort } from '../../utils/dateUtils';

vi.mock('../../services/adminService');
vi.mock('../../services/api', () => ({
  __esModule: true,
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
  getCookie: vi.fn(),
}));
vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    // The positional English fallback PLUS i18next's `{{token}}` interpolation. Without it the
    // driver banner and the window label would read `{{name}}` back out of the page and pass on
    // a string no admin ever sees.
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
const day = (offset) => dayjs().add(offset, 'day').format('YYYY-MM-DD');
const MORNING = { start: '09:00', end: '12:00', kind: 'between', label: '09:00-12:00' };
const EVENING = { start: '18:00', end: '21:00', kind: 'between', label: '18:00-21:00' };
const ANYTIME = { start: null, end: null, kind: 'anytime', label: 'anytime' };
// Windows no preset matches, so the form opens on Custom with the stored edges.
const CUSTOM_BETWEEN = { start: '10:30', end: '14:00', kind: 'between', label: '10:30-14:00' };
const CUSTOM_UNTIL = { start: null, end: '11:00', kind: 'until', label: 'until 11:00' };
const REASON_PLACEHOLDER = 'Staff only — never shown to the customer';
// The refusals the modal explains itself, named on every PATCH so api.js does not toast them as
// well: one refusal, one message. The interceptor side is pinned in services/api.test.js.
const HANDLED = {
  handledErrorCodes: [
    'ORDER_NOT_RESCHEDULABLE',
    'DELIVERY_NOT_RESCHEDULABLE',
    'DELIVERY_DATE_REQUIRED',
    'ORDER_RESCHEDULE_PAST_CONTRACT_END',
    'ORDER_RESCHEDULE_REASON_TOO_LONG',
  ],
};

// A list row: serialize_order_admin + get_reschedule_metadata(detail=False).
const ROW = {
  id: 321,
  order_number: 'ORD-321',
  user_id: 77,
  status: 'confirmed',
  payment_method: 'cash',
  payment_status: 'pending',
  total_amount: 18000,
  customer_name: 'Ali Buyer',
  customer_email: 'ali@example.com',
  customer_phone: '+998901234500',
  created_at: '2026-09-20T10:00:00+00:00',
  items_summary: [],
  item_count: 0,
  delivery_date: day(1),
  delivery_window: MORNING,
  awaiting_release: false,
  release_at: null,
  can_reschedule: true,
  reschedule_block_code: null,
};

// GET /admin/orders/<id>: the same schedule block, the `delivery` block the endpoint already
// publishes, and get_reschedule_metadata(detail=True).
const detailOf = (overrides = {}) => ({
  ...ROW,
  payment_timeline: { timeline: [] },
  marking_code_summary: { events: {}, codes_by_order_item: {} },
  delivery: { id: 55, status: 'assigned', tracking_number: 'TRK-55' },
  reschedule_notifies_customer: false,
  reschedule_customer_channel: 'telegram',
  reschedule_driver_losing_stop: { id: 41, name: 'Sardor Alimov' },
  reschedule_min_date: TODAY,
  reschedule_max_date: day(15),
  ...overrides,
});

const mount = ({ row = {}, detail = {} } = {}) => {
  api.get.mockResolvedValue({ data: { data: { statuses: [{ value: 'confirmed', label: 'Confirmed' }], transitions: {} } } });
  adminService.getOrders.mockResolvedValue({ data: { items: [{ ...ROW, ...row }] }, meta: { total: 1 } });
  adminService.getOrderDetails.mockResolvedValue({ success: true, data: { order: detailOf({ ...row, ...detail }) } });
  adminService.getOrderEditHistory.mockResolvedValue({ success: true, data: { entries: [] } });
  adminService.rescheduleOrder.mockResolvedValue({ success: true, data: { order: detailOf({ ...row, ...detail }) } });
};

const createWrapper = (queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })) =>
  ({ children }) => <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;

const openRescheduleFromRow = async () => {
  render(<Orders />, { wrapper: createWrapper() });
  fireEvent.click(await screen.findByRole('button', { name: 'Reschedule' }));
  const modal = await screen.findByRole('dialog', { name: 'Reschedule delivery' });
  // The form mounts only once the fresh detail read has landed.
  await within(modal).findByRole('button', { name: 'Save' });
  return modal;
};

// An axios rejection shaped like validation_error_response. `errors[0]` is the service's English
// sentence; the fence code rides in `data.error_code`. The service layer is mocked here, so no
// interceptor runs in this file.
const apiError = (errors, errorCode) => Object.assign(new Error('Request failed with status code 400'), {
  response: {
    status: 400,
    data: {
      success: false,
      message: 'Validation failed',
      errors,
      ...(errorCode ? { data: { error_code: errorCode } } : {}),
    },
  },
});

// Lets every pending promise settle (antd's validateFields, a mutation), so a check that nothing
// happened proves it did not happen, not merely that it has not happened YET.
const flush = () => act(() => new Promise((resolve) => { setTimeout(resolve, 0); }));

const pickEveningAndSave = async (modal) => {
  fireEvent.click(within(modal).getByRole('radio', { name: 'Evening' }));
  const save = within(modal).getByRole('button', { name: 'Save' });
  await waitFor(() => expect(save).toBeEnabled());
  fireEvent.click(save);
};

beforeEach(() => {
  vi.clearAllMocks();
});

it('offers Reschedule only on rows the backend marks can_reschedule', async () => {
  mount();
  adminService.getOrders.mockResolvedValue({
    data: {
      items: [
        ROW,
        { ...ROW, id: 322, order_number: 'ORD-322', status: 'delivered', can_reschedule: false, reschedule_block_code: 'ORDER_NOT_RESCHEDULABLE' },
      ],
    },
    meta: { total: 2 },
  });
  render(<Orders />, { wrapper: createWrapper() });
  await screen.findByText('ORD-322');

  const offered = screen.getAllByRole('button', { name: 'Reschedule' });
  expect(offered).toHaveLength(1);
  fireEvent.click(offered[0]);
  // The one on offer belongs to ORD-321: the modal re-reads that order, never the delivered one.
  await waitFor(() => expect(adminService.getOrderDetails).toHaveBeenCalledWith(321));
  await flush();
  expect(adminService.getOrderDetails).not.toHaveBeenCalledWith(322);
});

it.each([
  [true, 1],
  [false, 0],
])('shows the detail-footer Reschedule when the detail says can_reschedule=%s', async (canReschedule, count) => {
  mount({ row: { can_reschedule: canReschedule }, detail: { can_reschedule: canReschedule } });
  render(<Orders />, { wrapper: createWrapper() });
  fireEvent.click(await screen.findByRole('button', { name: 'View Details' }));
  const detailModal = await screen.findByRole('dialog', { name: 'Order Details - ORD-321' });

  // The rows the detail modal gained: date · window, and the delivery's own status.
  expect(await within(detailModal).findByText('assigned')).toBeInTheDocument();
  expect(within(detailModal).getByText(`${formatDate(day(1))} · 09:00–12:00`)).toBeInTheDocument();
  expect(within(detailModal).queryAllByRole('button', { name: /reschedule/i })).toHaveLength(count);
});

it('keeps Save disabled and sends nothing while the date and window are unchanged', async () => {
  // Review Focus 1: an accidental Save would unassign Sardor and could message the customer.
  mount();
  const modal = await openRescheduleFromRow();
  const save = within(modal).getByRole('button', { name: 'Save' });
  expect(save).toBeDisabled();

  // A reason on its own is not a new schedule.
  fireEvent.change(within(modal).getByPlaceholderText(REASON_PLACEHOLDER), { target: { value: 'Checking' } });
  expect(save).toBeDisabled();

  // Changing the window and changing it back is still no change.
  fireEvent.click(within(modal).getByRole('radio', { name: 'Evening' }));
  await waitFor(() => expect(save).toBeEnabled());
  fireEvent.click(within(modal).getByRole('radio', { name: 'Morning' }));
  await waitFor(() => expect(save).toBeDisabled());

  fireEvent.click(save);
  await flush();
  expect(adminService.rescheduleOrder).not.toHaveBeenCalled();
});

it('sends a changed window with the date key always present, plus the trimmed reason', async () => {
  mount();
  const modal = await openRescheduleFromRow();
  fireEvent.change(within(modal).getByPlaceholderText(REASON_PLACEHOLDER), {
    target: { value: '  Customer asked for the evening  ' },
  });
  await pickEveningAndSave(modal);

  await waitFor(() => expect(adminService.rescheduleOrder).toHaveBeenCalledWith(321, {
    delivery_date: day(1),
    delivery_window_start: '18:00',
    delivery_window_end: '21:00',
    reason: 'Customer asked for the evening',
  }, HANDLED));
});

it('opens an overdue order on the first allowed day and never submits a past date', async () => {
  // Review Focus 2: failed two days ago, still on its old date.
  mount({
    row: { delivery_date: day(-2) },
    detail: { delivery: { id: 55, status: 'failed', tracking_number: 'TRK-55' }, reschedule_driver_losing_stop: null },
  });
  const modal = await openRescheduleFromRow();

  expect(modal.querySelector('.ant-picker input')).toHaveValue(TODAY);
  const save = within(modal).getByRole('button', { name: 'Save' });
  await waitFor(() => expect(save).toBeEnabled());
  fireEvent.click(save);

  await waitFor(() => expect(adminService.rescheduleOrder).toHaveBeenCalledWith(321, {
    delivery_date: TODAY,
    delivery_window_start: '09:00',
    delivery_window_end: '12:00',
  }, HANDLED));
});

it("opens an undated order's date empty, so Save waits for a real choice", async () => {
  // Seeding "today" here would turn an untouched Save into a real reschedule.
  mount({ row: { delivery_date: null, delivery_window: ANYTIME } });
  const modal = await openRescheduleFromRow();

  expect(modal.querySelector('.ant-picker input')).toHaveValue('');
  expect(within(modal).getByRole('button', { name: 'Save' })).toBeDisabled();
  // A window with no day is still not a schedule.
  fireEvent.click(within(modal).getByRole('radio', { name: 'Evening' }));
  await flush();
  expect(within(modal).getByRole('button', { name: 'Save' })).toBeDisabled();
});

// Review Focus 1, per stored shape: an untouched form must describe exactly the stored schedule.
// Save stays disabled, and a submit that bypasses the button (Enter in a field) sends nothing.
it.each([
  ['a preset window', {}, day(1)],
  ['a custom between window', { delivery_window: CUSTOM_BETWEEN }, day(1)],
  ['a custom until window', { delivery_window: CUSTOM_UNTIL }, day(1)],
  ['anytime on a set day', { delivery_window: ANYTIME }, day(1)],
  // Past the last allowed day: the date opens empty rather than on a day the admin never picked.
  ['a date past reschedule_max_date', { delivery_date: day(20) }, ''],
])('an untouched form over %s sends nothing, even submitted directly', async (_label, row, openDate) => {
  mount({ row });
  const modal = await openRescheduleFromRow();

  expect(modal.querySelector('.ant-picker input')).toHaveValue(openDate);
  const save = within(modal).getByRole('button', { name: 'Save' });
  expect(save).toBeDisabled();

  fireEvent.click(save);
  fireEvent.submit(modal.querySelector('form'));
  await flush();
  expect(adminService.rescheduleOrder).not.toHaveBeenCalled();
});

describe('a read that changes while the modal is open', () => {
  // Another admin moves the order to the evening of day(2) while this one looks at it.
  const MOVED = { delivery_date: day(2), delivery_window: EVENING };

  afterEach(() => { onlineManager.setOnline(true); });

  it('is not re-read when the network comes back', async () => {
    mount();
    const modal = await openRescheduleFromRow();
    adminService.getOrderDetails.mockResolvedValue({ success: true, data: { order: detailOf(MOVED) } });

    act(() => { onlineManager.setOnline(false); });
    act(() => { onlineManager.setOnline(true); });
    await flush();

    // Only the read the modal opened with.
    expect(adminService.getOrderDetails).toHaveBeenCalledTimes(1);
    expect(within(modal).getByRole('button', { name: 'Save' })).toBeDisabled();
  });

  it('re-seeds the form from any refetch, so an untouched Save cannot revert the other change', async () => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    mount();
    render(<Orders />, { wrapper: createWrapper(queryClient) });
    fireEvent.click(await screen.findByRole('button', { name: 'Reschedule' }));
    const modal = await screen.findByRole('dialog', { name: 'Reschedule delivery' });
    await within(modal).findByRole('button', { name: 'Save' });
    adminService.getOrderDetails.mockResolvedValue({ success: true, data: { order: detailOf(MOVED) } });

    // Whatever might trigger one later: an invalidation of every query.
    await act(() => queryClient.invalidateQueries());
    await waitFor(() => expect(adminService.getOrderDetails).toHaveBeenCalledTimes(2));
    await flush();

    expect(within(modal).getByRole('button', { name: 'Save' })).toBeDisabled();
    expect(modal.querySelector('.ant-picker input')).toHaveValue(day(2));
    fireEvent.submit(modal.querySelector('form'));
    await flush();
    expect(adminService.rescheduleOrder).not.toHaveBeenCalled();
  });
});

it.each([
  ['telegram', 'The customer will be notified of the new date in Telegram.'],
  ['email', 'The customer will be notified of the new date by email.'],
])('promises a notice only by the %s channel the backend named', async (channel, promise) => {
  mount({ detail: { reschedule_notifies_customer: true, reschedule_customer_channel: channel } });
  const modal = await openRescheduleFromRow();

  expect(within(modal).getByText(promise)).toBeInTheDocument();
  expect(within(modal).queryByText(/can't be notified/)).toBeNull();
});

it("says the customer can't be notified when the backend has no channel for them", async () => {
  // Review Focus 5: no connected bot and no email. The backend still sends nothing, so the admin
  // must be told to call instead of being promised a message.
  mount({ detail: { reschedule_notifies_customer: true, reschedule_customer_channel: null } });
  const modal = await openRescheduleFromRow();

  expect(within(modal).getByText("The customer can't be notified automatically — tell them yourself.")).toBeInTheDocument();
  expect(within(modal).queryByText(/will be notified/)).toBeNull();
});

it('names the driver who loses the stop, and says nothing of a customer who gets no notice', async () => {
  mount({ detail: { reschedule_notifies_customer: false, reschedule_customer_channel: 'telegram' } });
  const modal = await openRescheduleFromRow();

  expect(within(modal).getByText('Driver Sardor Alimov will lose this stop.')).toBeInTheDocument();
  expect(within(modal).queryByText(/will be notified/)).toBeNull();
  expect(within(modal).queryByText(/can't be notified/)).toBeNull();
});

it("shows a mapped refusal once: inline, in the admin's language, and never as a toast", async () => {
  mount();
  adminService.rescheduleOrder.mockRejectedValue(apiError(
    ['delivery_date 2026-10-09 is after the contract end 2026-09-30'],
    'ORDER_RESCHEDULE_PAST_CONTRACT_END',
  ));
  const modal = await openRescheduleFromRow();
  await pickEveningAndSave(modal);

  expect(await within(modal).findByText("The customer's contract ends before that date. Pick an earlier date.")).toBeInTheDocument();
  // The PATCH named this code, so api.js stays silent on it (services/api.test.js), and the
  // modal adds no toast of its own: the inline copy is the only message.
  expect(adminService.rescheduleOrder).toHaveBeenCalledWith(321, {
    delivery_date: day(1),
    delivery_window_start: '18:00',
    delivery_window_end: '21:00',
  }, HANDLED);
  expect(message.error).not.toHaveBeenCalled();
  expect(message.success).not.toHaveBeenCalled();
  // Still open, so the admin can pick another day.
  expect(within(modal).getByRole('button', { name: 'Save' })).toBeInTheDocument();
});

it('falls back to the server sentence for a refusal that carries no code', async () => {
  mount();
  adminService.rescheduleOrder.mockRejectedValue(apiError(['delivery_window_start must be before delivery_window_end']));
  const modal = await openRescheduleFromRow();
  await pickEveningAndSave(modal);

  // No code, so nothing on the request matched it: api.js shows its usual toast, and the modal
  // keeps the sentence beside the form without a toast of its own.
  expect(await within(modal).findByText('delivery_window_start must be before delivery_window_end')).toBeInTheDocument();
  expect(message.error).not.toHaveBeenCalled();
});

it('explains a refusal the fresh read turned up instead of offering Save', async () => {
  // The row said yes; by the time the modal re-read the order the delivery had been delivered.
  mount({ detail: { can_reschedule: false, reschedule_block_code: 'DELIVERY_NOT_RESCHEDULABLE' } });
  render(<Orders />, { wrapper: createWrapper() });
  fireEvent.click(await screen.findByRole('button', { name: 'Reschedule' }));
  const modal = await screen.findByRole('dialog', { name: 'Reschedule delivery' });

  expect(await within(modal).findByText(
    "This order's delivery is already delivered, cancelled or returned, so it can't be rescheduled.",
  )).toBeInTheDocument();
  expect(within(modal).queryByRole('button', { name: 'Save' })).toBeNull();
});

it('refreshes the open detail modal and every cached delivery screen after a reschedule', async () => {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const invalidate = vi.spyOn(queryClient, 'invalidateQueries');
  const RELEASE_AT = `${day(3)}T04:00:00+00:00`;
  const after = detailOf({
    delivery_date: day(3),
    delivery_window: EVENING,
    awaiting_release: true,
    release_at: RELEASE_AT,
    delivery: { id: 55, status: 'rescheduled', tracking_number: 'TRK-55' },
    reschedule_driver_losing_stop: null,
  });
  mount();
  adminService.rescheduleOrder.mockImplementation(async () => {
    // From here on the backend answers with the rescheduled order.
    adminService.getOrderDetails.mockResolvedValue({ success: true, data: { order: after } });
    return { success: true, data: { order: after } };
  });
  render(<Orders />, { wrapper: createWrapper(queryClient) });

  fireEvent.click(await screen.findByRole('button', { name: 'View Details' }));
  const detailModal = await screen.findByRole('dialog', { name: 'Order Details - ORD-321' });
  expect(await within(detailModal).findByText('assigned')).toBeInTheDocument();
  fireEvent.click(within(detailModal).getByRole('button', { name: /reschedule/i }));
  // Found by its title, not its accessible name: under NODE_ENV=test rc-util's useId answers the
  // constant 'test-id', so with the detail modal open both dialogs are labelled by ITS title.
  const modal = (await screen.findByText('Reschedule delivery')).closest('[role="dialog"]');
  await within(modal).findByRole('button', { name: 'Save' });
  await pickEveningAndSave(modal);

  await waitFor(() => expect(adminService.rescheduleOrder).toHaveBeenCalledWith(321, {
    delivery_date: day(1),
    delivery_window_start: '18:00',
    delivery_window_end: '21:00',
  }, HANDLED));
  // The detail modal re-read the order, like every other detail mutation does.
  expect(await within(detailModal).findByText(`${formatDate(day(3))} · 18:00–21:00`)).toBeInTheDocument();
  expect(within(detailModal).getByText('rescheduled')).toBeInTheDocument();
  expect(within(detailModal).getByText('Scheduled')).toBeInTheDocument();
  expect(within(detailModal).getByText(formatDateTimeShort(RELEASE_AT))).toBeInTheDocument();
  // Opening the detail, the modal's fresh read, and the refresh.
  expect(adminService.getOrderDetails).toHaveBeenCalledTimes(3);

  expect(message.success).toHaveBeenCalledWith('Delivery rescheduled');
  for (const queryKey of [
    ['orders'],
    ['dispatchSnapshot'],
    ['dispatchSnapshotOverlay'],
    ['dispatchRouteGeometry'],
    ['deliveries'],
    ['dashboard'],
  ]) {
    expect(invalidate).toHaveBeenCalledWith({ queryKey });
  }
  // The modal closed; the detail modal stays.
  expect(screen.queryByRole('button', { name: 'Save' })).toBeNull();
});
