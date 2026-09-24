/**
 * Create Order's delivery-date picker reads its range from the backend, fetched as the modal
 * opens (docs/superpowers/specs/2026-09-23-admin-order-reschedule-design.md §6).
 *
 * It used to compute the range from the browser clock and a JS copy of
 * MAX_SCHEDULE_HORIZON_DAYS. Now it has a query of its own (`['schedule-bounds']`,
 * `staleTime: 0`, enabled only while the modal is open). It never reads the page's
 * `['order-statuses']` entry, which is cached for a day, so a tab left open overnight
 * cannot offer yesterday.
 */
import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import dayjs from 'dayjs';

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
  useTranslation: () => ({ t: (key, fallback) => (typeof fallback === 'string' ? fallback : key) }),
}));

const day = (offset) => dayjs().add(offset, 'day').format('YYYY-MM-DD');
// antd titles every calendar cell with its YYYY-MM-DD date.
const cell = (isoDate) => document.querySelector(`.ant-picker-dropdown td[title="${isoDate}"]`);

const CUSTOMER = { id: 77, first_name: 'Ali', last_name: 'Buyer', phone: '+998901234500' };
const ADDRESS = { id: 9, title: 'Home', full_address: 'Chilonzor 5', is_default: true };
const PRODUCT = { id: 3, name: 'Water 19L', price: 18000 };

// What GET /orders/statuses answers with at the moment it is called.
let bounds;
const statusesCalls = () => api.get.mock.calls.filter(([url]) => url === '/orders/statuses').length;

const createWrapper = () => {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }) => <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
};

// The page's button comes first in the DOM; the open modal's submit button has the same name.
const openCreateOrder = () => fireEvent.click(screen.getAllByRole('button', { name: /create order/i })[0]);

const openDatePopup = async () => {
  const input = screen.getByPlaceholderText('Deliver as soon as possible');
  // Disabled until THIS open's bounds have landed.
  await waitFor(() => expect(input).toBeEnabled());
  fireEvent.mouseDown(input);
  fireEvent.click(input);
  await waitFor(() => expect(document.querySelector('.ant-picker-dropdown td[title]')).not.toBeNull());
};

beforeEach(() => {
  vi.clearAllMocks();
  bounds = { min: day(0), max: day(15) };
  api.get.mockImplementation(async (url) => {
    if (url !== '/orders/statuses') return { data: { data: {} } };
    return {
      data: {
        data: {
          statuses: [{ value: 'pending', label: 'Pending' }],
          transitions: {},
          schedule_min_date: bounds.min,
          schedule_max_date: bounds.max,
        },
      },
    };
  });
  adminService.getOrders.mockResolvedValue({ data: { items: [] }, meta: { total: 0 } });
  adminService.getProducts.mockResolvedValue({ data: { items: [PRODUCT] } });
});

it("reads the date range from a fetch made as Create Order opens, not from the page's day-long cache", async () => {
  // What the page's ['order-statuses'] entry holds from mount: a range the calendar has since
  // moved past.
  bounds = { min: day(-1), max: day(14) };
  render(<Orders />, { wrapper: createWrapper() });
  await waitFor(() => expect(statusesCalls()).toBe(1));

  bounds = { min: day(1), max: day(3) };
  openCreateOrder();
  await waitFor(() => expect(statusesCalls()).toBe(2));
  await openDatePopup();

  // The backend's range: not the browser's today, not the 15-day constant the JS used to carry,
  // and not the stale range the page cached at mount.
  expect(cell(day(0))).toHaveClass('ant-picker-cell-disabled');
  expect(cell(day(1))).not.toHaveClass('ant-picker-cell-disabled');
  expect(cell(day(3))).not.toHaveClass('ant-picker-cell-disabled');
  expect(cell(day(4))).toHaveClass('ant-picker-cell-disabled');
});

it('re-reads the range on every open, so a tab left open overnight never offers yesterday', async () => {
  render(<Orders />, { wrapper: createWrapper() });
  await waitFor(() => expect(statusesCalls()).toBe(1));
  openCreateOrder();
  await waitFor(() => expect(statusesCalls()).toBe(2));
  const dialog = await screen.findByRole('dialog', { name: 'Create Order' });
  fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));

  // "Tomorrow" arrives while the tab stays open: the backend's first bookable day moves on.
  bounds = { min: day(2), max: day(15) };
  openCreateOrder();
  await waitFor(() => expect(statusesCalls()).toBe(3));
  await openDatePopup();

  expect(cell(day(1))).toHaveClass('ant-picker-cell-disabled');
  expect(cell(day(2))).not.toHaveClass('ant-picker-cell-disabled');
});

it('sends the picked day and window with the new order', async () => {
  adminService.getUsers.mockResolvedValue({ data: { items: [CUSTOMER] }, meta: { has_next: false } });
  adminService.getUserDetails.mockResolvedValue({ data: { user: CUSTOMER } });
  adminService.getUserAddresses.mockResolvedValue({ data: { addresses: [ADDRESS] } });
  adminService.getUserPaymentMethods.mockResolvedValue({
    data: { available_methods: [{ method: 'cash', name: 'Cash', is_default: true }], payment_restrictions: null },
  });
  adminService.createOrderForUser.mockResolvedValue({ success: true, data: {} });
  render(<Orders />, { wrapper: createWrapper() });
  openCreateOrder();
  const dialog = await screen.findByRole('dialog', { name: 'Create Order' });

  const customer = within(dialog).getByText('Search customer by name or phone').closest('.ant-select');
  fireEvent.mouseDown(customer.querySelector('.ant-select-selector'));
  fireEvent.change(customer.querySelector('input'), { target: { value: 'Ali' } });
  fireEvent.click(await screen.findByTitle('Ali Buyer - +998901234500'));

  const address = (await within(dialog).findByText('Select an address')).closest('.ant-select');
  fireEvent.mouseDown(address.querySelector('.ant-select-selector'));
  fireEvent.click(await screen.findByText('Home: Chilonzor 5 (Default)'));

  const product = within(dialog).getByText('Select product').closest('.ant-select');
  fireEvent.mouseDown(product.querySelector('.ant-select-selector'));
  fireEvent.click(await screen.findByText(/^Water 19L - /));

  await openDatePopup();
  fireEvent.click(cell(day(2)));
  fireEvent.click(within(dialog).getByRole('radio', { name: 'Evening' }));
  fireEvent.click(within(dialog).getByRole('button', { name: /create order/i }));

  // The extracted picker still writes the same four fields into Create Order's own form.
  await waitFor(() => expect(adminService.createOrderForUser).toHaveBeenCalledWith({
    user_id: 77,
    delivery_address_id: 9,
    payment_method: 'cash',
    delivery_notes: '',
    consume_marking_codes: false,
    items: [{ product_id: 3, quantity: 1 }],
    delivery_date: day(2),
    delivery_window_start: '18:00',
    delivery_window_end: '21:00',
  }));
});
