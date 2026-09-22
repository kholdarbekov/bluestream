import React from 'react';
import { render, screen, fireEvent, within, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { message } from 'antd';
import dayjs from 'dayjs';

import Outlets from '../../pages/Outlets';
import salesService from '../../services/salesService';
import staffService from '../../services/staffService';
import api from '../../services/api';

vi.mock('../../services/salesService');
vi.mock('../../services/staffService');
vi.mock('../../services/api', () => ({ __esModule: true, default: { get: vi.fn() } }));
vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key, opts) => (typeof opts === 'string' ? opts : opts?.defaultValue) || key }),
}));
vi.mock('antd', async () => {
  const actual = await vi.importActual('antd');
  return { ...actual, message: { success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn(), loading: vi.fn(), destroy: vi.fn(), open: vi.fn() } };
});

// Pinned by tests/unit/test_admin_ui_payload_fixture_contracts.py against serialize_outlet().
const OUTLET_ROW_KEYS = new Set([
  'id', 'name', 'outlet_type', 'channel', 'stage', 'class', 'cadence_days_override', 'user_id', 'address_id',
  'latitude', 'longitude', 'address_text', 'district', 'assigned_agent_user_id', 'assigned_agent_name',
  'onboarded_by_user_id', 'next_visit_due_at', 'agent_next_visit_at', 'last_visit_at', 'last_order_at',
  'opening_hours', 'preferred_visit_window', 'delivery_window_start', 'delivery_window_end', 'payment_terms',
  'legal_form', 'tax_id', 'preferred_language', 'storefront_photo_path', 'competitor_note', 'status_warning',
  'dedupe_candidates', 'activation_requested_at', 'approved_at', 'approved_by_user_id', 'rejected_reason',
  'lost_reason', 'lost_note', 'notes', 'contacts', 'created_at', 'updated_at',
]);

const OUTLET = Object.fromEntries([...OUTLET_ROW_KEYS].map((k) => [k, null]));
Object.assign(OUTLET, {
  id: 5, name: 'Bahor market', outlet_type: 'grocery_store', stage: 'activation_requested', class: 'B',
  latitude: 41.3111, longitude: 69.2797, address_text: 'Chilonzor 5', district: 'chilanzar',
  assigned_agent_user_id: 41, assigned_agent_name: 'Sardor Alimov', dedupe_candidates: [{ kind: 'customer', name: 'Bahor (Olim)', phone: '+998901112266', distance_m: 40, reason: 'name_nearby' }],
  contacts: [{ id: 1, name: 'Olim aka', phone: '+998901112266', role: 'owner', is_primary: true, presence_window: null }],
  payment_terms: 'cash', preferred_language: 'uz', created_at: '2026-09-01T00:00:00Z', updated_at: '2026-09-01T00:00:00Z',
  delivery_window_start: '09:30', delivery_window_end: '18:45',
});

const STAGE_HISTORY = [{ id: 1, from_stage: null, to_stage: 'prospect', reason_code: 'created', note: null, actor_user_id: 41, created_at: '2026-09-01T00:00:00Z' }];

const createWrapper = () => {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }) => (
    <MemoryRouter initialEntries={['/outlets']}><QueryClientProvider client={queryClient}>{children}</QueryClientProvider></MemoryRouter>
  );
};

const openDrawer = async () => {
  fireEvent.click(await screen.findByText('Bahor market'));
  const drawer = await screen.findByRole('dialog');
  // The drawer opens EMPTY and fills in when `getOutlet` resolves, so waiting on the dialog alone
  // races the detail query. The tab strip is the first thing the detail payload renders.
  await within(drawer).findByText('Overview');
  return drawer;
};

const lastModal = () => {
  const modals = document.querySelectorAll('.ant-modal-content');
  return modals[modals.length - 1];
};

const pickOption = async (combobox, title) => {
  fireEvent.mouseDown(combobox);
  fireEvent.click(await screen.findByTitle(title));
};

// Dispatch.test.jsx:77's convention for a date the page computes from "today".
const day = (offset) => dayjs().subtract(offset, 'day').format('YYYY-MM-DD');

beforeEach(() => {
  vi.clearAllMocks();
  salesService.getOutlets.mockResolvedValue({ items: [OUTLET], total: 1, page: 1, per_page: 20, summary: { prospect: 0, activation_requested: 1, active: 0 } });
  salesService.getOutlet.mockResolvedValue({ outlet: { ...OUTLET, open_receivable: 0, bottle_balance: 0, last_orders: [] }, stage_history: STAGE_HISTORY });
  salesService.approveOutlet.mockResolvedValue({ outlet: { ...OUTLET, stage: 'active' } });
  salesService.updateOutlet.mockResolvedValue({ outlet: { ...OUTLET, district: 'yunusabad' } });
  salesService.bulkAssign.mockResolvedValue({ updated: 3 });
  salesService.importExistingCustomers.mockResolvedValue({ created: 2 });
  salesService.rejectOutlet.mockResolvedValue({ outlet: { ...OUTLET, stage: 'prospect' } });
  salesService.assignOutlet.mockResolvedValue({ outlet: { ...OUTLET, assigned_agent_user_id: 77 } });
  salesService.markLost.mockResolvedValue({ outlet: { ...OUTLET, stage: 'lost', lost_reason: 'closed' } });
  // Two agents with distinct ids: an assign assertion must not be satisfiable by echoing back the
  // agent (41) the outlet already carries.
  staffService.getSalesAgents.mockResolvedValue({ data: { data: { items: [{ user_id: 41, full_name: 'Sardor Alimov' }, { user_id: 77, full_name: 'Nodira Karimova' }] }, meta: { total: 2 } } });
  // The district pickers are fed from the geo-config KEYS, the same list SalesAgents.js uses.
  api.get.mockResolvedValue({ data: { success: true, data: { districts: [{ key: 'chilanzar', name: 'Chilanzar' }, { key: 'yunusabad', name: 'Yunusabad' }] } } });
  salesService.getVisits.mockResolvedValue({ visits: [], meta: { page: 1, per_page: 20, total: 0, pages: 0, has_next: false, has_prev: false }, start_date: day(89), end_date: day(0) });
});

it('lists outlets with stage tags and the per-stage summary', async () => {
  render(<Outlets />, { wrapper: createWrapper() });
  const row = await screen.findByRole('row', { name: /Bahor market/ });
  expect(row).toHaveTextContent('activation_requested');
  expect(row).toHaveTextContent('Sardor Alimov');
  expect(screen.getByText('Awaiting activation')).toBeInTheDocument();
  expect(salesService.getOutlets).toHaveBeenCalledWith({ page: 1, per_page: 20, search: undefined, stage: undefined, class: undefined, outlet_type: undefined, agent_user_id: undefined, district: undefined, unvisited_days: undefined });
});

it('opens the drawer and approves with the dedupe candidates visible', async () => {
  render(<Outlets />, { wrapper: createWrapper() });
  const drawer = await openDrawer();
  expect(within(drawer).getByText(/Bahor \(Olim\)/)).toBeInTheDocument();
  fireEvent.click(within(drawer).getByRole('button', { name: /^approve$/i }));
  await waitFor(() => expect(salesService.approveOutlet).toHaveBeenCalledWith(5, null));
});

it('imports existing customers from the toolbar', async () => {
  render(<Outlets />, { wrapper: createWrapper() });
  await screen.findByText('Bahor market');
  fireEvent.click(screen.getByRole('button', { name: /import existing customers/i }));
  // Creating an outlet per customer estate-wide is confirmed, never one stray click.
  fireEvent.click(await screen.findByRole('button', { name: /^ok$/i }));
  await waitFor(() => expect(salesService.importExistingCustomers).toHaveBeenCalledTimes(1));
});

// antd's `bordered` Descriptions lays each pair out as <th class="...-item-label"> followed by its
// <td class="...-item-content">, so the label element's next sibling IS the value cell.
const descValue = (drawer, label) => {
  const cell = within(drawer).getByText(label).closest('.ant-descriptions-item-label');
  return cell.nextElementSibling.textContent;
};

it('shows a dash, not a zero, for money that does not apply to a prospect', async () => {
  // `OutletService.card` answers NULL for a figure that has no subject: no customer account means
  // no wallet, no address row means no bottle ledger. Rendering `?? 0` turned both into a 0, which
  // reads as a settled bill and an empty crate on a store that has never ordered.
  salesService.getOutlet.mockResolvedValue({ outlet: { ...OUTLET, open_receivable: null, bottle_balance: null, last_orders: [] }, stage_history: STAGE_HISTORY });
  render(<Outlets />, { wrapper: createWrapper() });
  const drawer = await openDrawer();

  expect(descValue(drawer, 'Owes')).toBe('—');
  expect(descValue(drawer, 'Bottles at outlet')).toBe('—');
});

it('still shows a real zero balance for an outlet that has an account', async () => {
  // The other half of the same rule: 0 is a FIGURE for an activated outlet, and must not be
  // swallowed by the null check.
  salesService.getOutlet.mockResolvedValue({ outlet: { ...OUTLET, open_receivable: 0, bottle_balance: 0, last_orders: [] }, stage_history: STAGE_HISTORY });
  render(<Outlets />, { wrapper: createWrapper() });
  const drawer = await openDrawer();

  expect(descValue(drawer, 'Owes')).toBe('0 UZS');
  expect(descValue(drawer, 'Bottles at outlet')).toBe('0');
});

it('repairs a NULL district from the drawer', async () => {
  // The state the outlet CREATE path deliberately produces: a geocoder hint that named no
  // district is stored as NULL rather than blocking a finished walk-in, on the promise that an
  // admin fixes it HERE. Until this select existed the promise was unkeepable, and the outlet
  // stayed invisible to every district filter and to bulk assignment.
  salesService.getOutlet.mockResolvedValue({ outlet: { ...OUTLET, district: null, open_receivable: 0, bottle_balance: 0, last_orders: [] }, stage_history: STAGE_HISTORY });
  render(<Outlets />, { wrapper: createWrapper() });
  const drawer = await openDrawer();

  await pickOption(within(drawer).getByTestId('outlet-district-select').querySelector('.ant-select-selector'), 'Yunusabad');

  // The KEY is what gets stored, never the display name the admin actually saw.
  await waitFor(() => expect(salesService.updateOutlet).toHaveBeenCalledWith(5, { district: 'yunusabad' }));
});

it('bulk-assigns a district that no row on the current page carries', async () => {
  // Fed from geo-config, not from the districts present in the loaded rows: the whole point of
  // bulk assignment is reaching a district the admin is NOT already looking at.
  render(<Outlets />, { wrapper: createWrapper() });
  await screen.findByText('Bahor market');
  fireEvent.click(screen.getByRole('button', { name: /bulk assign by district/i }));
  const modal = await screen.findByRole('dialog');

  await pickOption(within(modal).getByTestId('bulk-district-select').querySelector('.ant-select-selector'), 'Yunusabad');
  await pickOption(within(modal).getByTestId('bulk-agent-select').querySelector('.ant-select-selector'), 'Sardor Alimov');
  fireEvent.click(within(modal).getByRole('button', { name: /save/i }));

  await waitFor(() => expect(salesService.bulkAssign).toHaveBeenCalledWith('yunusabad', 41));
});

it('rejects an activation request with the reason the modal collected', async () => {
  render(<Outlets />, { wrapper: createWrapper() });
  const drawer = await openDrawer();
  fireEvent.click(within(drawer).getByRole('button', { name: /^reject$/i }));

  const modal = await waitFor(() => { expect(lastModal()).toBeTruthy(); return lastModal(); });
  fireEvent.change(within(modal).getByRole('textbox'), { target: { value: 'Owner refused a contract' } });
  fireEvent.click(within(modal).getByRole('button', { name: /save/i }));

  // `reason` is required by RejectPayload (min_length=1) — the prose the admin typed has to reach
  // the body, not a stage name or an id.
  await waitFor(() => expect(salesService.rejectOutlet).toHaveBeenCalledWith(5, 'Owner refused a contract'));
});

it('assigns a different agent from the drawer', async () => {
  render(<Outlets />, { wrapper: createWrapper() });
  const drawer = await openDrawer();

  await pickOption(within(drawer).getByTestId('outlet-agent-select').querySelector('.ant-select-selector'), 'Nodira Karimova');

  // (outletId, agentUserId) — both numbers, deliberately different, so a swapped argument fails.
  await waitFor(() => expect(salesService.assignOutlet).toHaveBeenCalledWith(5, 77));
});

it('marks an outlet lost with an explicit null note', async () => {
  render(<Outlets />, { wrapper: createWrapper() });
  const drawer = await openDrawer();
  fireEvent.click(within(drawer).getByRole('button', { name: /mark lost/i }));

  const modal = await waitFor(() => { expect(lastModal()).toBeTruthy(); return lastModal(); });
  await pickOption(within(modal).getByTestId('lost-reason-select').querySelector('.ant-select-selector'), 'closed');
  fireEvent.click(within(modal).getByRole('button', { name: /save/i }));

  // The note is optional; an untouched textarea must send null, not undefined (which JSON.stringify
  // drops entirely) and not ''.
  await waitFor(() => expect(salesService.markLost).toHaveBeenCalledWith(5, 'closed', null));
});

it('returns to page 1 when a filter narrows the list', async () => {
  salesService.getOutlets.mockResolvedValue({ items: [OUTLET], total: 60, page: 1, per_page: 20, summary: {} });
  render(<Outlets />, { wrapper: createWrapper() });
  await screen.findByText('Bahor market');

  fireEvent.click(screen.getByTitle('2'));
  await waitFor(() => expect(salesService.getOutlets).toHaveBeenLastCalledWith(expect.objectContaining({ page: 2 })));

  // A narrower filter usually has fewer pages than the one being viewed, so keeping the page
  // number renders an empty table beside a non-zero total. Search, stage and district already
  // reset; class, type, agent and unvisited must too.
  await pickOption(screen.getByTestId('filter-class').querySelector('.ant-select-selector'), 'A');
  await waitFor(() => expect(salesService.getOutlets).toHaveBeenLastCalledWith(expect.objectContaining({ page: 1, class: 'A' })));
});

// --- C24: the outlet's standing delivery window ------------------------------------------
// The window is not decoration: VisitService offers it as the DEFAULT delivery window of every
// order the agent places at this outlet, and only when BOTH edges are set (visit_service.py:571).
// The admin page is its only writer, so both-or-neither has to hold here.

const openWindowModal = async () => {
  const drawer = await openDrawer();
  fireEvent.click(within(drawer).getByRole('button', { name: /edit delivery window/i }));
  return waitFor(() => { expect(lastModal()).toBeTruthy(); return lastModal(); });
};

it('shows the standing delivery window on the drawer overview', async () => {
  render(<Outlets />, { wrapper: createWrapper() });
  const drawer = await openDrawer();

  expect(descValue(drawer, 'Delivery window')).toBe('09:30–18:45');
});

it('shows the stored edge and a dash for the missing one when a window is half stored', async () => {
  // The modal refuses to SAVE a half pair, but nothing refuses to STORE one: the agent PUT and
  // the backend both accept the fields independently (the both-or-neither refusal is backlog),
  // so a row with only a start is reachable. Collapsing it to a bare dash told an operator the
  // outlet had no window, and the modal then opened pre-filled with the 09:30 the overview had
  // just denied — two screens disagreeing about the same two columns.
  salesService.getOutlet.mockResolvedValue({ outlet: { ...OUTLET, delivery_window_end: null, open_receivable: 0, bottle_balance: 0, last_orders: [] }, stage_history: STAGE_HISTORY });
  render(<Outlets />, { wrapper: createWrapper() });
  const drawer = await openDrawer();

  expect(descValue(drawer, 'Delivery window')).toBe('09:30–—');
});

it('shows a dash when the outlet has no window at all', async () => {
  salesService.getOutlet.mockResolvedValue({ outlet: { ...OUTLET, delivery_window_start: null, delivery_window_end: null, open_receivable: 0, bottle_balance: 0, last_orders: [] }, stage_history: STAGE_HISTORY });
  render(<Outlets />, { wrapper: createWrapper() });
  const drawer = await openDrawer();

  expect(descValue(drawer, 'Delivery window')).toBe('—');
});

it('writes the window back as two HH:MM strings', async () => {
  render(<Outlets />, { wrapper: createWrapper() });
  const modal = await openWindowModal();

  // The modal opens ON the stored pair — reopening empty over a window that is really stored is
  // how an admin clears one by accident.
  expect([...modal.querySelectorAll('.ant-picker input')].map((i) => i.value)).toEqual(['09:30', '18:45']);

  fireEvent.click(within(modal).getByRole('button', { name: /save/i }));

  await waitFor(() => expect(salesService.updateOutlet).toHaveBeenCalledWith(5, {
    delivery_window_start: '09:30', delivery_window_end: '18:45',
  }));
});

it('clears a stored window with two explicit nulls', async () => {
  render(<Outlets />, { wrapper: createWrapper() });
  const modal = await openWindowModal();
  const clearIcons = () => modal.querySelectorAll('.ant-picker-clear');

  expect(clearIcons()).toHaveLength(2);
  fireEvent.click(clearIcons()[0]);
  await waitFor(() => expect(clearIcons()).toHaveLength(1));
  fireEvent.click(clearIcons()[0]);
  await waitFor(() => expect(clearIcons()).toHaveLength(0));

  fireEvent.click(within(modal).getByRole('button', { name: /save/i }));

  // NULLs, not omitted keys: `_validated_payload` uses exclude_unset, so a field the body never
  // mentions is left alone and an "emptied" modal would toast success having written nothing.
  await waitFor(() => expect(salesService.updateOutlet).toHaveBeenCalledWith(5, {
    delivery_window_start: null, delivery_window_end: null,
  }));
});

it('refuses half a window without touching the API', async () => {
  render(<Outlets />, { wrapper: createWrapper() });
  const modal = await openWindowModal();

  // Clear the END only. One edge alone is a window the order path silently ignores, so it is
  // refused where the admin can still see both fields.
  fireEvent.click(modal.querySelectorAll('.ant-picker-clear')[1]);
  await waitFor(() => expect(modal.querySelectorAll('.ant-picker-clear')).toHaveLength(1));
  fireEvent.click(within(modal).getByRole('button', { name: /save/i }));

  await waitFor(() => expect(message.error).toHaveBeenCalledWith('Set both the start and the end, or clear both.'));
  expect(salesService.updateOutlet).not.toHaveBeenCalled();
});



it('lists the outlet\'s recent visits in the drawer, from the same route the Visits page uses', async () => {
  salesService.getVisits.mockResolvedValue({
    visits: [{
      id: 501, outlet_id: 5, agent_user_id: 41, status: 'completed', planned: true,
      started_at: '2026-09-14T04:05:00+00:00', outcome: 'order_placed', in_radius: true,
      checkin_skipped: false, distance_m: 12.4, outlet_name: 'Bahor market',
      agent_name: 'Sardor Alimov', order_number: 'SA-000101',
    }],
    meta: { page: 1, per_page: 20, total: 1, pages: 1, has_next: false, has_prev: false },
    start_date: day(89), end_date: day(0),
  });
  render(<Outlets />, { wrapper: createWrapper() });
  const drawer = await openDrawer();

  // Nothing is fetched until the tab is opened: a drawer that always ran a 90-day visits query
  // would pay for it on every single outlet click.
  expect(salesService.getVisits).not.toHaveBeenCalled();
  fireEvent.click(within(drawer).getByRole('tab', { name: 'Visits' }));

  // Scoped to the visit ROW: antd keeps the Overview pane mounted behind the active tab, and its
  // agent Select already reads 'Sardor Alimov', so a drawer-wide text query matches twice.
  const visitRow = (await within(drawer).findByText('SA-000101')).closest('tr');
  expect(visitRow).toHaveTextContent('Sardor Alimov');
  // `outlet_id` is the whole point of the spec's drawer tab: ONE visits route serves both the
  // page and this drawer. The 90-day window sits inside the backend's 92-day cap (R12).
  await waitFor(() => expect(salesService.getVisits).toHaveBeenCalledWith({
    outlet_id: 5, start_date: day(89), end_date: day(0), page: 1, per_page: 20,
  }));
  // The caption says what the tab actually shows. It is ONE unpaged page of the same route, so
  // "the last 90 days" alone was a claim about the field the tab cannot support: an outlet with
  // 40 visits shows 20 and looks half as busy as it is.
  expect(within(drawer).getByText('The latest 20 visits in the last 90 days.')).toBeInTheDocument();
});

it('shows the house error alert when the drawer visits query fails, never an empty table', async () => {
  // The same defect Task 9's fix round repaired on the page this tab was copied from: with only
  // `isLoading` and `data` read, a failure paints antd's "No data" directly under a caption
  // asserting what the last 90 days held — an assertion about the field made out of a failed
  // request, which a supervisor reads as "nobody has been to this shop".
  salesService.getVisits.mockRejectedValue({ response: { data: { message: 'Date range is invalid' } } });
  render(<Outlets />, { wrapper: createWrapper() });
  const drawer = await openDrawer();
  fireEvent.click(within(drawer).getByRole('tab', { name: 'Visits' }));

  expect(await within(drawer).findByText('Date range is invalid')).toBeInTheDocument();
});
