/**
 * Create Order prices the basket on the server (plan 2026-11-16, Task 56): the lines, the courtesy split, the dated
 * line and the total are the server's own fields, the total shown goes back as `expected_total`, and a 409 redraws
 * the panel from the quote it carries. The panel computes no money.
 */
import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import Orders, { orderPricingPayload } from '../../pages/Orders';
import adminService from '../../services/adminService';
import api from '../../services/api';

vi.mock('../../services/adminService');
vi.mock('../../services/api', () => ({
  __esModule: true,
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
  getCookie: vi.fn(),
}));
vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key, fallback, opts) => String(typeof fallback === 'string' ? fallback : key)
      .replace(/\{\{(\w+)\}\}/g, (_, name) => String(opts?.[name] ?? '')),
  }),
}));

// tests/integration/test_admin_order_quote_ui_contract.py diffs each Set with the real route's answer.
const QUOTE_KEYS = new Set([
  'items', 'payment_method', 'subtotal', 'delivery_fee', 'discount_amount', 'tier_discount', 'total_amount',
  'price_courtesy', 'courtesy_status',
]);
const QUOTE_ITEM_KEYS = new Set([
  'product_id', 'product_name', 'quantity', 'unit_price', 'discount_amount', 'courtesy_quantity',
  'courtesy_unit_price', 'regular_quantity', 'total_price', 'is_contract_price', 'upcoming_price',
]);

const checked = (name, value, keys) => {
  const wrong = Object.keys(value).filter((key) => !keys.has(key));
  const missing = [...keys].filter((key) => !(key in value));
  if (wrong.length || missing.length) throw new Error(`${name}: extra ${wrong} missing ${missing}`);
  return value;
};

const quoteItem = (overrides = {}) => checked('QUOTE_ITEM', {
  product_id: 3,
  product_name: 'Water 19L',
  quantity: 4,
  unit_price: 20000,
  discount_amount: 0,
  courtesy_quantity: 2,
  courtesy_unit_price: 18000,
  regular_quantity: 2,
  total_price: 76000,
  is_contract_price: false,
  upcoming_price: null,
  ...overrides,
}, QUOTE_ITEM_KEYS);

const quote = (overrides = {}) => checked('QUOTE', {
  items: [quoteItem()],
  payment_method: 'cash',
  subtotal: 76000,
  delivery_fee: 0,
  discount_amount: 0,
  tier_discount: 0,
  total_amount: 76000,
  price_courtesy: { applied: true, expires_on: '2027-02-14' },
  courtesy_status: {
    state: 'available',
    expires_on: '2027-02-14',
    caps: [{ product_id: 3, product_name: 'Water 19L', cap: 2 }],
    used_order_id: null,
    used_order_number: null,
  },
  ...overrides,
}, QUOTE_KEYS);

const NEW_QUOTE = quote({
  items: [quoteItem({
    courtesy_quantity: null, courtesy_unit_price: null, regular_quantity: 4, total_price: 80000,
  })],
  subtotal: 80000,
  total_amount: 80000,
  price_courtesy: null,
  courtesy_status: {
    state: 'used',
    expires_on: '2027-02-14',
    caps: [],
    used_order_id: 42,
    used_order_number: 'ORD-2027-0042',
  },
});

const CUSTOMER = { id: 77, first_name: 'Ali', last_name: 'Buyer', phone: '+998901234500' };
const ADDRESS = { id: 9, title: 'Home', full_address: 'Chilonzor 5', is_default: true };
const PRODUCT = { id: 3, name: 'Water 19L', price: 20000 };

const createWrapper = () => {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }) => <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
};

beforeEach(() => {
  vi.clearAllMocks();
  api.get.mockImplementation(async () => ({ data: { data: {} } }));
  adminService.getOrders.mockResolvedValue({ data: { items: [] }, meta: { total: 0 } });
  adminService.getProducts.mockResolvedValue({ data: { items: [PRODUCT] } });
  adminService.getUsers.mockResolvedValue({ data: { items: [CUSTOMER] }, meta: { has_next: false } });
  adminService.getUserDetails.mockResolvedValue({ data: { user: CUSTOMER } });
  adminService.getUserAddresses.mockResolvedValue({ data: { addresses: [ADDRESS] } });
  adminService.getUserPaymentMethods.mockResolvedValue({
    data: { available_methods: [{ method: 'cash', name: 'Cash', is_default: true }], payment_restrictions: null },
  });
  adminService.createOrderForUser.mockResolvedValue({ success: true, data: {} });
  adminService.quoteAdminOrder.mockResolvedValue(quote());
});

// Opens Create Order and fills customer, address and one product line; returns the dialog.
const fillBasket = async () => {
  render(<Orders />, { wrapper: createWrapper() });
  fireEvent.click(screen.getAllByRole('button', { name: /create order/i })[0]);
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
  return dialog;
};

const submit = (dialog) => fireEvent.click(within(dialog).getByRole('button', { name: /create order/i }));

it('prices the basket for the chosen customer and prints the server lines and total', async () => {
  const dialog = await fillBasket();

  expect(await within(dialog).findByText('2 × 18,000 UZS + 2 × 20,000 UZS')).toBeInTheDocument();
  expect(adminService.quoteAdminOrder).toHaveBeenCalledWith({
    user_id: 77,
    delivery_address_id: 9,
    payment_method: 'cash',
    items: [{ product_id: 3, quantity: 1 }],
  });
  expect(within(dialog).getAllByText('76,000 UZS').length).toBeGreaterThanOrEqual(1);
  expect(within(dialog).getByText(/Old price kept for this customer until Feb 14, 2027: up to 2 × Water 19L/)).toBeInTheDocument();
  expect(within(dialog).getByText('The old price is kept on this order.')).toBeInTheDocument();
});

it('sends the total it showed as expected_total', async () => {
  const dialog = await fillBasket();
  await within(dialog).findByText('2 × 18,000 UZS + 2 × 20,000 UZS');

  submit(dialog);

  await waitFor(() => expect(adminService.createOrderForUser).toHaveBeenCalledTimes(1));
  const [body, options] = adminService.createOrderForUser.mock.calls[0];
  expect(body.expected_total).toBe(76000);
  expect(options).toEqual({ handledErrorCodes: ['STATE_CHANGED'] });
});

it('redraws from the 409 quote with a calm note and sends the new total next time', async () => {
  adminService.createOrderForUser.mockRejectedValueOnce({
    response: {
      status: 409,
      data: { error_code: 'STATE_CHANGED', details: { reason: 'price_changed', quote: NEW_QUOTE } },
    },
  });
  const dialog = await fillBasket();
  await within(dialog).findByText('2 × 18,000 UZS + 2 × 20,000 UZS');

  submit(dialog);

  expect(await within(dialog).findByText(/^The price changed since this screen was drawn/)).toBeInTheDocument();
  expect(screen.getByRole('dialog', { name: 'Create Order' })).toBeInTheDocument();
  expect(within(dialog).getAllByText('80,000 UZS').length).toBeGreaterThanOrEqual(1);
  expect(within(dialog).getByText('Already used on order ORD-2027-0042')).toBeInTheDocument();
  expect(adminService.quoteAdminOrder).toHaveBeenCalledTimes(1);

  // The button is busy until the refused create has settled.
  await waitFor(() => expect(within(dialog).getByRole('button', { name: /create order/i })).not.toHaveClass('ant-btn-loading'));
  submit(dialog);
  await waitFor(() => expect(adminService.createOrderForUser).toHaveBeenCalledTimes(2));
  expect(adminService.createOrderForUser.mock.calls[1][0].expected_total).toBe(80000);
});

it('shows the dated line during the notice period', async () => {
  adminService.quoteAdminOrder.mockResolvedValue(quote({
    items: [quoteItem({
      quantity: 2, courtesy_quantity: null, courtesy_unit_price: null, regular_quantity: 2, unit_price: 18000,
      total_price: 36000, upcoming_price: { effective_date: '2027-02-01', new_price: 20000 },
    })],
    subtotal: 36000,
    total_amount: 36000,
    price_courtesy: null,
  }));
  const dialog = await fillBasket();

  expect(await within(dialog).findByText('From Feb 01, 2027: 20,000 UZS')).toBeInTheDocument();
});

it('keeps Create disabled while the basket is being priced', async () => {
  let release;
  adminService.quoteAdminOrder.mockImplementation(() => new Promise((resolve) => { release = resolve; }));
  const dialog = await fillBasket();

  expect(await within(dialog).findByText('Pricing…')).toBeInTheDocument();
  expect(within(dialog).getByRole('button', { name: /create order/i })).toBeDisabled();

  release(quote());
  await waitFor(() => expect(within(dialog).getByRole('button', { name: /create order/i })).toBeEnabled());
});

it('a quote refusal shows the backend sentence and sends no expected_total', async () => {
  adminService.quoteAdminOrder.mockRejectedValue({
    response: {
      status: 400,
      data: { errors: ['Minimum order is 4 bottles.'], data: { error_code: 'ORDER_ITEM_BELOW_MIN_QUANTITY' } },
    },
  });
  const dialog = await fillBasket();

  expect(await within(dialog).findByText('Could not price this basket')).toBeInTheDocument();
  expect(within(dialog).getByText('Minimum order is 4 bottles.')).toBeInTheDocument();

  submit(dialog);
  await waitFor(() => expect(adminService.createOrderForUser).toHaveBeenCalledTimes(1));
  expect(adminService.createOrderForUser.mock.calls[0][0]).not.toHaveProperty('expected_total');
});

describe('orderPricingPayload', () => {
  const ROW = { product_id: 3, quantity: 2, extra: 'x' };

  it('reduces the rows to product and quantity and defaults the method to cash', () => {
    expect(orderPricingPayload({ user_id: 7, delivery_address_id: 9, items: [ROW] })).toEqual({
      user_id: 7,
      delivery_address_id: 9,
      payment_method: 'cash',
      items: [{ product_id: 3, quantity: 2 }],
    });
  });

  it('keeps a chosen payment method', () => {
    expect(orderPricingPayload({ user_id: 7, payment_method: 'click', items: [ROW] }).payment_method).toBe('click');
  });

  it.each([
    ['no customer', { items: [ROW] }],
    ['no rows', { user_id: 7, items: [] }],
    ['no items', { user_id: 7 }],
    ['a row without a product', { user_id: 7, items: [{ quantity: 1 }] }],
    ['a row without a quantity', { user_id: 7, items: [{ product_id: 3 }] }],
  ])('is null for %s', (_name, input) => {
    expect(orderPricingPayload(input)).toBeNull();
  });
});
