import React from 'react';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { message } from 'antd';

import DriverExpenses from '../../pages/DriverExpenses';
import driverExpenseService from '../../services/driverExpenseService';
import staffService from '../../services/staffService';

vi.mock('../../services/driverExpenseService', () => ({
  __esModule: true,
  default: {
    list: vi.fn(),
    get: vi.fn(),
    preview: vi.fn(),
    approve: vi.fn(),
    reject: vi.fn(),
    undo: vi.fn(),
    getReceiptBlob: vi.fn(),
  },
}));
vi.mock('../../services/staffService', () => ({
  __esModule: true,
  default: { getDeliveryPersons: vi.fn() },
}));
vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key, fallback, opts) => String(typeof fallback === 'string' ? fallback : key)
      .replace(/\{\{(\w+)\}\}/g, (_, name) => String(opts?.[name] ?? '')),
  }),
}));

// The keys `DriverExpenseReview.row` publishes (tests/integration/test_driver_expense_admin_read_api.py::ROW_KEYS).
const ROW_KEYS = new Set([
  'id', 'driver_user_id', 'driver_name', 'driver_cash_session_id', 'category', 'amount', 'approved_amount', 'note',
  'has_receipt', 'latitude', 'longitude', 'status', 'submitted_at', 'decided_at', 'decided_by_user_id',
  'decided_by_name', 'decision_reason', 'self_decided', 'withdrawn_at', 'can_approve', 'can_reject', 'can_undo',
  'decide_block_code', 'approve_block_code', 'undo_block_code', 'approve_max_amount',
]);
const only = (keys, value) => {
  const extra = Object.keys(value).filter((key) => !keys.has(key));
  const missing = [...keys].filter((key) => !(key in value));
  if (extra.length || missing.length) throw new Error(`row: extra ${extra}, missing ${missing}`);
  return value;
};

const expense = (overrides = {}) => only(ROW_KEYS, {
  id: 7, driver_user_id: 31, driver_name: 'Second Driver', driver_cash_session_id: 124, category: 'fuel',
  amount: 150000, approved_amount: null, note: 'Full tank', has_receipt: false, latitude: null, longitude: null,
  status: 'pending', submitted_at: '2026-11-19T07:00:00+00:00', decided_at: null, decided_by_user_id: null,
  decided_by_name: null, decision_reason: null, self_decided: false, withdrawn_at: null,
  can_approve: true, can_reject: true, can_undo: false, decide_block_code: null, approve_block_code: null,
  undo_block_code: 'not_approved',
  approve_max_amount: 150000, ...overrides,
});

const EFFECT = {
  session_id: 124, on_hand_before: 420000, on_hand_after: 270000, hand_in_now_before: 0, hand_in_now_after: 0,
  will_close: false,
};

const listBody = (items, extra = {}) => ({
  items, page: 1, per_page: 20, total: items.length, pending_count: items.length,
  totals_by_category: [{ category: 'fuel', approved_total: 0 }], reason_min_length: 8, ...extra,
});
const detailBody = (row, extra = {}) => ({
  expense: row,
  warnings: { possible_duplicates: [], recent_closed_trips: [] },
  reason_min_length: 8,
  ...extra,
});

const renderPage = (url = '/delivery/expenses') => render(
  <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter initialEntries={[url]}>
      <DriverExpenses />
    </MemoryRouter>
  </QueryClientProvider>,
);

const openDialog = async (row) => {
  fireEvent.click(await screen.findByRole('button', { name: 'Review' }));
  return within(await screen.findByRole('dialog'));
};

beforeEach(() => {
  vi.clearAllMocks();
  staffService.getDeliveryPersons.mockResolvedValue({ data: { data: { items: [] } } });
  driverExpenseService.preview.mockResolvedValue({ effect: EFFECT, blocking_codes: [], approve_max_amount: 150000 });
  globalThis.URL.createObjectURL = vi.fn(() => 'blob:receipt');
  globalThis.URL.revokeObjectURL = vi.fn();
});

it('draws the buttons from the backend flags', async () => {
  const row = expense({ can_approve: false, decide_block_code: 'self_decision', approve_block_code: 'self_decision', can_reject: false });
  driverExpenseService.list.mockResolvedValue(listBody([row]));
  driverExpenseService.get.mockResolvedValue(detailBody(row));
  renderPage();

  const dialog = await openDialog(row);

  const approve = dialog.getByRole('button', { name: 'Approve' });
  expect(approve).toBeDisabled();
  expect(dialog.getByRole('button', { name: 'Reject' })).toBeDisabled();
  expect(dialog.queryByRole('button', { name: 'Undo approval' })).toBeNull();
  await userEvent.hover(approve.parentElement);
  expect(await screen.findByText('You cannot decide your own expense')).toBeInTheDocument();
  expect(driverExpenseService.preview).not.toHaveBeenCalled();
});

it('explains a claim the trip cash cannot cover with the published block code', async () => {
  const row = expense({ can_approve: false, approve_block_code: 'amount_above_trip_cash', approve_max_amount: 0 });
  driverExpenseService.list.mockResolvedValue(listBody([row]));
  driverExpenseService.get.mockResolvedValue(detailBody(row));
  renderPage();

  const dialog = await openDialog(row);
  await userEvent.hover(dialog.getByRole('button', { name: 'Approve' }).parentElement);

  expect(await screen.findByText("More than the driver's trip cash")).toBeInTheDocument();
});

it('does not blame the trip cash on a Reject held back only by a short reason', async () => {
  const row = expense({ can_approve: false, approve_block_code: 'amount_above_trip_cash', approve_max_amount: 0 });
  driverExpenseService.list.mockResolvedValue(listBody([row]));
  driverExpenseService.get.mockResolvedValue(detailBody(row));
  renderPage();

  const dialog = await openDialog(row);
  const reject = dialog.getByRole('button', { name: 'Reject' });
  expect(reject).toBeDisabled();
  await userEvent.hover(reject.parentElement);

  expect(screen.queryByText("More than the driver's trip cash")).toBeNull();
  fireEvent.change(dialog.getByRole('textbox', { name: 'Reason' }), { target: { value: 'Not a business expense' } });
  expect(dialog.getByRole('button', { name: 'Reject' })).toBeEnabled();
});

it('approves with the typed amount and shows the preview', async () => {
  const row = expense();
  driverExpenseService.list.mockResolvedValue(listBody([row]));
  driverExpenseService.get.mockResolvedValue(detailBody(row));
  driverExpenseService.approve.mockResolvedValue({});
  renderPage();

  const dialog = await openDialog(row);

  const input = dialog.getByRole('spinbutton', { name: 'Amount to approve' });
  expect(input).toHaveAttribute('aria-valuemax', '150000');
  expect(dialog.getByText('At most 150,000')).toBeInTheDocument();
  expect(await dialog.findByText('Trip #124: on hand 420,000 → 270,000 · Hand in now 0 → 0')).toBeInTheDocument();
  expect(driverExpenseService.preview).toHaveBeenLastCalledWith(7, 150000);

  driverExpenseService.preview.mockResolvedValue({
    effect: { ...EFFECT, on_hand_after: 300000, hand_in_now_after: 30000, will_close: true },
    blocking_codes: [],
    approve_max_amount: 150000,
  });
  fireEvent.change(input, { target: { value: '120000' } });
  expect(await dialog.findByText(/Hand in now 0 → 30,000 · the trip closes/)).toBeInTheDocument();
  expect(driverExpenseService.preview).toHaveBeenLastCalledWith(7, 120000);

  fireEvent.click(dialog.getByRole('button', { name: 'Approve' }));
  await waitFor(() => expect(driverExpenseService.approve).toHaveBeenCalledWith(7, 120000));
});

// A closed trip as `review._recent_closed_trips` publishes it.
const closedTrip = (sessionId, status, verifiedVariance) => ({
  session_id: sessionId, status, verified_variance: verifiedVariance, session_ended_at: '2026-11-18T15:00:00+00:00',
});

it('shows the duplicate and recent-trip warnings the backend publishes, each trip with its check result', async () => {
  const row = expense();
  driverExpenseService.list.mockResolvedValue(listBody([row]));
  driverExpenseService.get.mockResolvedValue(detailBody(row, {
    warnings: {
      possible_duplicates: [{ id: 5, amount: 150000, status: 'approved', submitted_at: '2026-11-19T05:00:00+00:00' }],
      // A trip the office has not counted yet publishes 0, which must not read as a clean check.
      recent_closed_trips: [closedTrip(121, 'submitted', 0), closedTrip(120, 'resolved', -150000)],
    },
  }));
  renderPage();

  const dialog = await openDialog(row);

  expect(dialog.getByText('Same driver, same amount, same day: #5')).toBeInTheDocument();
  expect(dialog.getByText("The driver's last closed trips")).toBeInTheDocument();
  expect(dialog.getByText(/^#121 · /)).toHaveTextContent(/ · Not checked yet$/);
  expect(dialog.getByText(/^#120 · /)).toHaveTextContent(/ · Written off · -150,000$/);
});

it.each([
  ['verified', -5000, / · Checked · -5,000$/],
  ['mismatch', -150000, / · Mismatch, not resolved · -150,000$/],
  ['force_closed', 0, / · Force-closed · 0$/],
])('names a %s closed trip beside its variance', async (status, variance, shown) => {
  const row = expense();
  driverExpenseService.list.mockResolvedValue(listBody([row]));
  driverExpenseService.get.mockResolvedValue(detailBody(row, {
    warnings: { possible_duplicates: [], recent_closed_trips: [closedTrip(120, status, variance)] },
  }));
  renderPage();

  const dialog = await openDialog(row);

  expect(dialog.getByText(/^#120 · /)).toHaveTextContent(shown);
});

it('needs the published reason length to reject and undo', async () => {
  const pending = expense();
  driverExpenseService.list.mockResolvedValue(listBody([pending]));
  driverExpenseService.get.mockResolvedValue(detailBody(pending));
  driverExpenseService.reject.mockResolvedValue({});
  driverExpenseService.undo.mockResolvedValue({});
  const first = renderPage();

  let dialog = await openDialog(pending);
  expect(dialog.getByText('At least 8 characters')).toBeInTheDocument();
  const reject = dialog.getByRole('button', { name: 'Reject' });
  const reason = dialog.getByRole('textbox', { name: 'Reason' });
  fireEvent.change(reason, { target: { value: 'abcdefg' } });
  expect(reject).toBeDisabled();
  fireEvent.change(reason, { target: { value: 'Not a business expense' } });
  expect(reject).toBeEnabled();
  fireEvent.click(reject);
  await waitFor(() => expect(driverExpenseService.reject).toHaveBeenCalledWith(7, 'Not a business expense'));
  first.unmount();

  const approved = expense({
    status: 'approved', approved_amount: 150000, can_approve: false, can_reject: false, can_undo: true,
    decide_block_code: 'not_pending', approve_block_code: 'not_pending', undo_block_code: null,
  });
  driverExpenseService.list.mockResolvedValue(listBody([approved]));
  driverExpenseService.get.mockResolvedValue(detailBody(approved));
  renderPage();

  dialog = await openDialog(approved);
  const undo = dialog.getByRole('button', { name: 'Undo approval' });
  fireEvent.change(dialog.getByRole('textbox', { name: 'Reason' }), { target: { value: 'abcdefg' } });
  expect(undo).toBeDisabled();
  fireEvent.change(dialog.getByRole('textbox', { name: 'Reason' }), { target: { value: 'Not a business expense' } });
  expect(undo).toBeEnabled();
  fireEvent.click(undo);
  await waitFor(() => expect(driverExpenseService.undo).toHaveBeenCalledWith(7, 'Not a business expense'));
});

it('offers Undo only where can_undo, and explains a refusal', async () => {
  const approved = expense({
    status: 'approved', approved_amount: 150000, can_approve: false, can_reject: false, can_undo: true,
    decide_block_code: 'not_pending', approve_block_code: 'not_pending', undo_block_code: null,
  });
  driverExpenseService.list.mockResolvedValue(listBody([approved], { reason_min_length: 8 }));
  driverExpenseService.get.mockResolvedValue(detailBody(approved));
  driverExpenseService.undo.mockRejectedValue({
    response: {
      status: 409,
      data: {
        error_code: 'DRIVER_EXPENSE_DECISION_BLOCKED',
        details: { block_code: 'cash_already_handed_in' },
      },
    },
  });
  const toast = vi.spyOn(message, 'error');
  renderPage('/delivery/expenses?id=7');

  const dialog = within(await screen.findByRole('dialog'));
  expect(dialog.queryByRole('button', { name: 'Approve' })).toBeNull();
  fireEvent.change(dialog.getByRole('textbox', { name: 'Reason' }), { target: { value: 'Not a business expense' } });
  fireEvent.click(dialog.getByRole('button', { name: 'Undo approval' }));

  expect(await dialog.findByText('The driver already handed in the amount this approval freed')).toBeInTheDocument();
  expect(toast).not.toHaveBeenCalled();
});

it('draws Photo unavailable when the receipt 404s', async () => {
  const row = expense({ has_receipt: true });
  driverExpenseService.list.mockResolvedValue(listBody([row]));
  driverExpenseService.get.mockResolvedValue(detailBody(row));
  driverExpenseService.getReceiptBlob.mockRejectedValue({ response: { status: 404 } });
  renderPage();

  const dialog = await openDialog(row);

  expect(await dialog.findByText('Photo unavailable')).toBeInTheDocument();
  expect(driverExpenseService.getReceiptBlob).toHaveBeenCalledWith(7);
});

it('opens the claim named in ?id= even when the list does not show it', async () => {
  driverExpenseService.list.mockResolvedValue(listBody([]));
  driverExpenseService.get.mockResolvedValue(detailBody(expense({ id: 42 })));
  renderPage('/delivery/expenses?id=42');

  const dialog = within(await screen.findByRole('dialog'));

  expect(driverExpenseService.get).toHaveBeenCalledWith('42');
  expect(dialog.getByText(/^#42 · Second Driver · Fuel$/)).toBeInTheDocument();
});

// Open one filter Select and pick the option with this label from its own dropdown.
const pick = async (select, label) => {
  fireEvent.mouseDown(select.querySelector('.ant-select-selector'));
  const option = () => document.querySelector(`.ant-select-dropdown .ant-select-item-option[title="${label}"]`);
  await waitFor(() => expect(option()).toBeTruthy());
  fireEvent.click(option());
};
const filterSelect = (placeholder) => screen
  .getByText(placeholder, { selector: '.ant-select-selection-placeholder' })
  .closest('.ant-select');

it('lists the waiting claims by default and sends every filter the person picks', async () => {
  // The period picker opens on the current month: pin it to November 2026 (only Date is faked).
  vi.useFakeTimers({ toFake: ['Date'] });
  vi.setSystemTime(new Date(2026, 10, 19, 10, 0));
  try {
    driverExpenseService.list.mockResolvedValue(listBody([expense()]));
    // `id` is the delivery_persons row, `user_id` the driver's users row: the list filters by the users id.
    staffService.getDeliveryPersons.mockResolvedValue({
      data: { data: { items: [{ id: 3, user_id: 31, full_name: 'Second Driver' }] } },
    });
    renderPage();

    expect(await screen.findByText('Full tank')).toBeInTheDocument();
    expect(driverExpenseService.list).toHaveBeenLastCalledWith({
      status: 'pending', driverId: undefined, category: undefined, startDate: undefined, endDate: undefined,
      page: 1, perPage: 20,
    });

    await pick(screen.getByRole('combobox', { name: 'Status' }).closest('.ant-select'), 'All');
    await pick(filterSelect('Driver'), 'Second Driver');
    await pick(filterSelect('Category'), 'Fuel');

    const startInput = document.querySelector('.ant-picker-input input');
    fireEvent.mouseDown(startInput);
    fireEvent.click(startInput);
    const cell = (isoDate) => document.querySelector(`.ant-picker-dropdown td[title="${isoDate}"]`);
    await waitFor(() => expect(cell('2026-11-05')).not.toBeNull());
    fireEvent.click(cell('2026-11-05'));
    await waitFor(() => expect(cell('2026-11-20')).not.toBeNull());
    fireEvent.click(cell('2026-11-20'));

    await waitFor(() => expect(driverExpenseService.list).toHaveBeenLastCalledWith({
      status: 'all', driverId: 31, category: 'fuel', startDate: '2026-11-05', endDate: '2026-11-20',
      page: 1, perPage: 20,
    }));
  } finally {
    vi.useRealTimers();
  }
});
