/**
 * Orders page -> Change address (docs/superpowers/specs/2026-10-18-admin-order-address-change-design.md).
 *
 * Driven through the page an admin uses. The detail footer opens ChangeAddressModal, which reads
 * GET /admin/orders/<id>/address-options and PATCHes /admin/orders/<id>/delivery-address through
 * adminService. Only the service layer is mocked: every flag, option, reason and branch below is
 * a field the backend publishes, rendered as it comes.
 */
import React from 'react';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { message } from 'antd';
import { MemoryRouter } from 'react-router-dom';

import Orders from '../../pages/Orders';
import adminService from '../../services/adminService';
import api from '../../services/api';
import { CHANGE_ADDRESS_INVALIDATED_KEYS } from '../../components/orders/ChangeAddressModal';

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

const HANDLED = {
  handledErrorCodes: ['ORDER_ADDRESS_LOCKED', 'DELIVERY_ADDRESS_LOCKED', 'ORDER_AWAITING_STAFF_APPROVAL',
    'INVALID_DELIVERY_ADDRESS', 'ADDRESS_CURRENT', 'ADDRESS_NO_PIN', 'ADDRESS_OUTSIDE_ZONE', 'COD_DEBT_LIMIT_REACHED',
    'SALES_PAY_SELF_DECISION'],
};
const KEYS = [['orders'], ['dispatchSnapshot'], ['dispatchSnapshotOverlay'], ['dispatchRouteGeometry'],
  ['deliveries'], ['customerMapPins'], ['dashboard']];

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
  can_reschedule: false,
};

const HOME = { id: 901, title: 'Home', full_address: 'Chilonzor 5', is_default: true };
const BRANCH_A = { outlet_id: 1, outlet_name: 'Branch A', agent_id: 10, agent_name: 'Alisher' };
const BRANCH_B = { outlet_id: 2, outlet_name: 'Branch B', agent_id: 11, agent_name: 'Bekzod' };

const detailOf = (overrides = {}) => ({
  ...ROW,
  payment_timeline: { timeline: [] },
  marking_code_summary: { events: {}, codes_by_order_item: {} },
  delivery: { id: 55, status: 'assigned', tracking_number: 'TRK-55' },
  delivery_address: HOME,
  can_change_address: true,
  change_address_block_code: null,
  change_address_driver: null,
  ...overrides,
});

const OPTIONS = [
  { ...HOME, selectable: false, disabled_reason: 'CURRENT', branch: BRANCH_A, agent_differs: false },
  { id: 902, title: 'Office', full_address: 'Yunusobod 7', is_default: false, selectable: true,
    disabled_reason: null, branch: BRANCH_B, agent_differs: true },
  { id: 903, title: null, full_address: 'Sergeli 3', is_default: false, selectable: false,
    disabled_reason: 'NO_PIN', branch: null, agent_differs: false },
];
const optionsData = (overrides = {}) => ({
  success: true,
  data: { options: OPTIONS, current_address_id: 901, current_branch: BRANCH_A, driver: { id: 5, name: 'Jasur' }, ...overrides },
});

const mount = ({ detail = {}, options = optionsData(), history = [] } = {}) => {
  api.get.mockResolvedValue({ data: { data: { statuses: [{ value: 'confirmed', label: 'Confirmed' }], transitions: {} } } });
  adminService.getOrders.mockResolvedValue({ data: { items: [ROW] }, meta: { total: 1 } });
  adminService.getOrderDetails.mockResolvedValue({ success: true, data: { order: detailOf(detail) } });
  adminService.getOrderEditHistory.mockResolvedValue({ success: true, data: { entries: history } });
  adminService.getOrderAddressOptions.mockResolvedValue(options);
  adminService.changeOrderAddress.mockResolvedValue({ success: true, data: { order: ROW } });
};

const createWrapper = (queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })) =>
  ({ children }) => (
    <QueryClientProvider client={queryClient}><MemoryRouter>{children}</MemoryRouter></QueryClientProvider>
  );

const apiError = (status, errorCode, extra = {}) => Object.assign(new Error(`Request failed with status code ${status}`), {
  response: { status, data: { success: false, message: 'x', errors: ['x'], data: { error_code: errorCode, ...extra } } },
});

const flush = () => act(() => new Promise((resolve) => { setTimeout(resolve, 0); }));

// Both dialogs share antd's test-id aria-labelledby under vitest, so the role+name query cannot
// tell them apart; the modal is found by its title.
const changeAddressDialog = async () => (await screen.findByText('Change delivery address')).closest('[role="dialog"]');

const openDetail = async (queryClient) => {
  render(<Orders />, { wrapper: createWrapper(queryClient) });
  fireEvent.click(await screen.findByRole('button', { name: 'View Details' }));
  return screen.findByRole('dialog', { name: 'Order Details - ORD-321' });
};

const openModal = async (queryClient) => {
  const detailModal = await openDetail(queryClient);
  fireEvent.click(await within(detailModal).findByRole('button', { name: /change address/i }));
  const modal = await changeAddressDialog();
  await within(modal).findByRole('button', { name: 'Save' });
  return modal;
};

const dropdownOptions = () => [...document.querySelectorAll('.ant-select-dropdown .ant-select-item-option')];
const openSelect = (modal) => fireEvent.mouseDown(modal.querySelector('.ant-select-selector'));
const pick = async (modal, text) => {
  openSelect(modal);
  await waitFor(() => expect(dropdownOptions().length).toBeGreaterThan(0));
  fireEvent.click(dropdownOptions().find((o) => o.getAttribute('title') === text));
};

beforeEach(() => {
  vi.clearAllMocks();
});

it('shows the delivery address and offers Change address only when the order publishes can_change_address', async () => {
  mount();
  const detailModal = await openDetail();
  expect(await within(detailModal).findByText('Home: Chilonzor 5 (Default)')).toBeInTheDocument();
  expect(within(detailModal).getByRole('button', { name: /change address/i })).toBeInTheDocument();
});

it('hides Change address when the order does not publish can_change_address', async () => {
  mount({ detail: { can_change_address: false, change_address_block_code: 'ORDER_ADDRESS_LOCKED' } });
  const detailModal = await openDetail();
  await within(detailModal).findByText('Home: Chilonzor 5 (Default)');
  expect(within(detailModal).queryByRole('button', { name: /change address/i })).toBeNull();
});

it('lists every saved address and greys the ones the backend refuses, with its reason', async () => {
  mount();
  const modal = await openModal();
  openSelect(modal);
  await waitFor(() => expect(dropdownOptions()).toHaveLength(3));
  const byTitle = (title) => dropdownOptions().find((o) => o.getAttribute('title') === title);
  expect(byTitle('Home: Chilonzor 5 (Default) — Current address')).toHaveClass('ant-select-item-option-disabled');
  expect(byTitle('Sergeli 3 — No map pin')).toHaveClass('ant-select-item-option-disabled');
  expect(byTitle('Office: Yunusobod 7')).not.toHaveClass('ant-select-item-option-disabled');
});

it('warns when the picked branch belongs to another agent and still lets the admin save', async () => {
  mount();
  const modal = await openModal();
  const save = within(modal).getByRole('button', { name: 'Save' });
  expect(save).toBeDisabled();
  await pick(modal, 'Office: Yunusobod 7');
  expect(await within(modal).findByText(
    'This order will count for Branch B (agent Bekzod) instead of Branch A (agent Alisher). Commission stays with the agent who placed the order.',
  )).toBeInTheDocument();
  expect(save).toBeEnabled();
});

it('names the driver who will be told', async () => {
  mount();
  const modal = await openModal();
  expect(within(modal).getByText('Driver Jasur will get the new address in the staff bot.')).toBeInTheDocument();
});

it('explains when nothing can be picked and keeps Save disabled', async () => {
  mount({ options: optionsData({ options: [OPTIONS[0], OPTIONS[2]], driver: null }) });
  const modal = await openModal();
  expect(within(modal).getByText(
    'No other saved address can be picked. Add one on the Users page, then come back.',
  )).toBeInTheDocument();
  expect(within(modal).getByRole('button', { name: 'Save' })).toBeDisabled();
});

it('saves the picked address with the trimmed note, refreshes detail and history and invalidates every surface', async () => {
  mount();
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const invalidate = vi.spyOn(queryClient, 'invalidateQueries');
  const modal = await openModal(queryClient);
  await pick(modal, 'Office: Yunusobod 7');
  fireEvent.change(within(modal).getByRole('textbox'), { target: { value: '  Customer called  ' } });
  fireEvent.click(within(modal).getByRole('button', { name: 'Save' }));

  await waitFor(() => expect(adminService.changeOrderAddress).toHaveBeenCalledWith(
    321, { address_id: 902, note: 'Customer called' }, HANDLED,
  ));
  expect(adminService.getOrderAddressOptions).toHaveBeenCalledWith(321, HANDLED);
  await waitFor(() => expect(message.success).toHaveBeenCalledWith('Delivery address changed'));
  KEYS.forEach((queryKey) => expect(invalidate).toHaveBeenCalledWith({ queryKey }));
  expect(CHANGE_ADDRESS_INVALIDATED_KEYS).toEqual(KEYS);
  await waitFor(() => expect(adminService.getOrderDetails).toHaveBeenCalledTimes(2));
  await waitFor(() => expect(adminService.getOrderEditHistory).toHaveBeenCalledTimes(2));
  await waitFor(() => expect(screen.queryByText('Change delivery address')).toBeNull());
});

it('sends no note key when the note is blank', async () => {
  mount();
  const modal = await openModal();
  await pick(modal, 'Office: Yunusobod 7');
  fireEvent.change(within(modal).getByRole('textbox'), { target: { value: '   ' } });
  fireEvent.click(within(modal).getByRole('button', { name: 'Save' }));
  await waitFor(() => expect(adminService.changeOrderAddress).toHaveBeenCalledWith(321, { address_id: 902 }, HANDLED));
});

it('shows a refusal inline in the admin language', async () => {
  mount();
  adminService.changeOrderAddress.mockRejectedValue(apiError(400, 'ADDRESS_NO_PIN'));
  const modal = await openModal();
  await pick(modal, 'Office: Yunusobod 7');
  fireEvent.click(within(modal).getByRole('button', { name: 'Save' }));
  expect(await within(modal).findByText('No map pin')).toBeInTheDocument();
  await flush();
  expect(message.error).not.toHaveBeenCalled();
});

it('links a held order to the approval queue', async () => {
  mount();
  adminService.getOrderAddressOptions.mockRejectedValue(
    apiError(409, 'ORDER_AWAITING_STAFF_APPROVAL', { details: { path: '/sales/order-approvals' } }),
  );
  const detailModal = await openDetail();
  fireEvent.click(await within(detailModal).findByRole('button', { name: /change address/i }));
  const modal = await changeAddressDialog();
  expect(await within(modal).findByText('Awaiting manager approval')).toBeInTheDocument();
  expect(within(modal).getByRole('link', { name: 'Open order approvals' })).toHaveAttribute('href', '/sales/order-approvals');
});

const changeEntry = (diff) => ({
  id: 1, edited_at: '2026-10-19T09:00:00+00:00', edited_by_user: { name: 'Admin' }, edited_by_user_id: 1,
  reason: 'Customer called', is_post_delivery: false,
  diff: { kind: 'address_change', driver_notified: false, self_decided: false, ...diff },
});
const SNAP_HOME = { id: 901, title: 'Home', full_address: 'Chilonzor 5' };
const SNAP_OFFICE = { id: 902, title: 'Office', full_address: 'Yunusobod 7' };

const expandHistory = async () => {
  const detailModal = await openDetail();
  fireEvent.click(await within(detailModal).findByRole('button', { name: /expand row/i }));
  return detailModal;
};

it('renders an address change in Order Changes', async () => {
  mount({ history: [changeEntry({
    address_before: SNAP_HOME, address_after: SNAP_OFFICE, branch_before: BRANCH_A, branch_after: BRANCH_B,
  })] });
  const detailModal = await expandHistory();
  expect(await within(detailModal).findByText('Address: Home: Chilonzor 5 → Office: Yunusobod 7')).toBeInTheDocument();
  expect(within(detailModal).getByText('Branch: Branch A → Branch B')).toBeInTheDocument();
});

it('renders a legacy change with no old address as a dash', async () => {
  mount({ history: [changeEntry({
    address_before: null, address_after: SNAP_OFFICE, branch_before: null, branch_after: null,
  })] });
  const detailModal = await expandHistory();
  expect(await within(detailModal).findByText('Address: — → Office: Yunusobod 7')).toBeInTheDocument();
  expect(within(detailModal).queryByText(/^Branch:/)).toBeNull();
});

it('shows no branch line when the customer has a single outlet (C4)', async () => {
  mount({ history: [changeEntry({
    address_before: SNAP_HOME, address_after: SNAP_OFFICE, branch_before: BRANCH_A, branch_after: BRANCH_A,
  })] });
  const detailModal = await expandHistory();
  await within(detailModal).findByText('Address: Home: Chilonzor 5 → Office: Yunusobod 7');
  expect(within(detailModal).queryByText(/^Branch:/)).toBeNull();
});

it('shows a branch line when one side has no branch (null is a dash)', async () => {
  mount({ history: [changeEntry({
    address_before: SNAP_HOME, address_after: SNAP_OFFICE, branch_before: null, branch_after: BRANCH_B,
  })] });
  const detailModal = await expandHistory();
  expect(await within(detailModal).findByText('Branch: — → Branch B')).toBeInTheDocument();
});
