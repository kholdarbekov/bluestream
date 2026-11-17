import React from 'react';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Modal } from 'antd';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import PriceChanges from '../../pages/PriceChanges';
import adminService from '../../services/adminService';

vi.mock('../../services/adminService', () => ({
  __esModule: true,
  default: {
    listPriceChanges: vi.fn(),
    getPriceChange: vi.fn(),
    listPriceChangeUnreachable: vi.fn(),
    listPriceChangeExceptions: vi.fn(),
    getProducts: vi.fn(),
    createPriceChange: vi.fn(),
    updatePriceChange: vi.fn(),
    cancelPriceChange: vi.fn(),
  },
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key, fallback, opts) => String(typeof fallback === 'string' ? fallback : key)
      .replace(/\{\{(\w+)\}\}/g, (_, name) => String(opts?.[name] ?? '')),
  }),
}));

// The product fields the form reads. tests/integration/test_price_change_admin_ui_contract.py checks this Set is a
// subset of the admin product row, so the fixture cannot carry a field the real list does not publish.
const PRODUCT_ROW_KEYS = new Set(['id', 'name', 'catalogue_unit_price', 'min_order_quantity']);
const product = (overrides = {}) => {
  const value = { id: 3, name: 'Water 18.9 L', catalogue_unit_price: 18000, min_order_quantity: 2, ...overrides };
  const extra = Object.keys(value).filter((key) => !PRODUCT_ROW_KEYS.has(key));
  if (extra.length) throw new Error(`product: extra ${extra}`);
  return value;
};

// The body create and edit send. The contract test builds a body from these Sets and posts it to the real route.
const PAYLOAD_KEYS = new Set(['announce_on', 'change_on', 'reminder_lead_days', 'courtesy_days', 'items']);
const PAYLOAD_ITEM_KEYS = new Set(['product_id', 'new_price', 'courtesy_cap']);
const sameKeys = (keys, value) => expect(new Set(Object.keys(value))).toEqual(keys);

const ALLOWED = { allowed: true, block_reason: null };
const HIDDEN = { allowed: false, block_reason: null };
const row = (overrides = {}) => ({
  id: 7, status: 'scheduled', stage: 'scheduled', announce_on: '2027-01-04', change_on: '2027-02-01',
  reminder_on: '2027-01-25', reminder_lead_days: 7, courtesy_days: 14, courtesy_last_day: '2027-02-14',
  price_valid_until: '2027-01-31', effective_at: '2027-01-31T19:00:00+00:00',
  courtesy_ends_at: '2027-02-14T19:00:00+00:00', announcement_started_at: null, reminder_started_at: null,
  applied_at: null, cancelled_at: null, created_by_id: 3, created_by_name: 'Aziza Manager', cancelled_by_id: null,
  cancelled_by_name: null, created_at: '2026-12-20T05:00:00+00:00',
  allowed_actions: { edit: ALLOWED, cancel: ALLOWED },
  items: [{
    product_id: 3, product_name: 'Water 18.9 L', current_price: 18000, new_price: 20000, old_price: null,
    courtesy_cap: 2, min_order_quantity: 2,
  }],
  ...overrides,
});

const list = (create, ...items) => adminService.listPriceChanges.mockResolvedValue({
  items, allowed_actions: { create },
});

const renderPage = () => render(
  <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <PriceChanges />
  </QueryClientProvider>,
);

beforeEach(() => {
  vi.clearAllMocks();
  adminService.getProducts.mockResolvedValue({ data: { items: [product(), product({ id: 4, name: 'Water 10 L', catalogue_unit_price: 11000, min_order_quantity: 1 })] } });
  adminService.listPriceChangeUnreachable.mockResolvedValue({ items: [], meta: { page: 1, per_page: 20, total: 0 } });
  adminService.listPriceChangeExceptions.mockResolvedValue([]);
});

// `Modal.confirm` mounts outside the render tree: close what a test left open.
afterEach(() => Modal.destroyAll());

const pickProduct = async (user, dialog, label) => {
  await user.click(within(dialog).getByRole('combobox', { name: 'Product' }));
  await user.click(await screen.findByTitle(label));
};

const fillCreateForm = async (user, dialog) => {
  await user.type(within(dialog).getByLabelText('Announcement date'), '2027-01-04{enter}');
  await user.type(within(dialog).getByLabelText('New prices from'), '2027-02-01{enter}');
  await pickProduct(user, dialog, 'Water 18.9 L — 18,000 UZS');
  await user.type(within(dialog).getByLabelText('New price'), '20000');
};

describe('the form', () => {
  it('creates a change with exactly the payload the API takes, defaults included', async () => {
    const user = userEvent.setup();
    list(ALLOWED);
    adminService.createPriceChange.mockResolvedValue(row());
    renderPage();

    await user.click(await screen.findByRole('button', { name: 'New price change' }));
    const dialog = await screen.findByRole('dialog');
    await fillCreateForm(user, dialog);
    // the cap is prefilled with the product's minimum order quantity
    expect(within(dialog).getByLabelText('Old-price units per order')).toHaveValue('2');
    expect(within(dialog).getByLabelText('Reminder days before the change')).toHaveValue('7');
    expect(within(dialog).getByLabelText('Old-price days after the change')).toHaveValue('14');
    await user.click(within(dialog).getByRole('button', { name: 'Save' }));

    await waitFor(() => expect(adminService.createPriceChange).toHaveBeenCalledTimes(1));
    expect(adminService.createPriceChange).toHaveBeenCalledWith({
      announce_on: '2027-01-04', change_on: '2027-02-01', reminder_lead_days: 7, courtesy_days: 14,
      items: [{ product_id: 3, new_price: 20000, courtesy_cap: 2 }],
    });
    sameKeys(PAYLOAD_KEYS, adminService.createPriceChange.mock.calls[0][0]);
    sameKeys(PAYLOAD_ITEM_KEYS, adminService.createPriceChange.mock.calls[0][0].items[0]);
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  });

  it('shows the refusal the backend sends and keeps the form open', async () => {
    const user = userEvent.setup();
    list(ALLOWED);
    const sentence = 'The courtesy cap of Water 18.9 L is below its minimum order quantity.';
    adminService.createPriceChange.mockRejectedValue({
      response: { data: { success: false, error_code: 'PRICE_CHANGE_CAP_BELOW_MIN_ORDER', message: sentence } },
    });
    renderPage();

    await user.click(await screen.findByRole('button', { name: 'New price change' }));
    const dialog = await screen.findByRole('dialog');
    await fillCreateForm(user, dialog);
    await user.click(within(dialog).getByRole('button', { name: 'Save' }));

    expect(await within(dialog).findByText(sentence)).toBeInTheDocument();
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(within(dialog).getByLabelText('New price')).toHaveValue('20,000');
  });

  it('checks only that the required fields are filled', async () => {
    const user = userEvent.setup();
    list(ALLOWED);
    renderPage();

    await user.click(await screen.findByRole('button', { name: 'New price change' }));
    const dialog = await screen.findByRole('dialog');
    await user.click(within(dialog).getByRole('button', { name: 'Save' }));

    expect(await within(dialog).findAllByText('Required')).not.toHaveLength(0);
    expect(adminService.createPriceChange).not.toHaveBeenCalled();
  });

  it('edits a scheduled change and sends the same payload shape', async () => {
    const user = userEvent.setup();
    list(ALLOWED, row());
    adminService.updatePriceChange.mockResolvedValue(row());
    renderPage();

    await user.click(await screen.findByRole('button', { name: 'Edit' }));
    const dialog = await screen.findByRole('dialog');
    expect(await within(dialog).findByLabelText('Announcement date')).toHaveValue('2027-01-04');
    expect(within(dialog).getByLabelText('New prices from')).toHaveValue('2027-02-01');
    const price = await within(dialog).findByLabelText('New price');
    await waitFor(() => expect(price).toHaveValue('20,000'));
    await user.clear(price);
    await user.type(price, '21000');
    await user.click(within(dialog).getByRole('button', { name: 'Save' }));

    await waitFor(() => expect(adminService.updatePriceChange).toHaveBeenCalledTimes(1));
    expect(adminService.updatePriceChange).toHaveBeenCalledWith(7, {
      announce_on: '2027-01-04', change_on: '2027-02-01', reminder_lead_days: 7, courtesy_days: 14,
      items: [{ product_id: 3, new_price: 21000, courtesy_cap: 2 }],
    });
    expect(adminService.createPriceChange).not.toHaveBeenCalled();
  });
});

describe('cancelling', () => {
  it('cancels an announced change after a confirm that says customers were told', async () => {
    const user = userEvent.setup();
    list(HIDDEN, row({ status: 'announced', stage: 'announced', announcement_started_at: '2027-01-04T04:00:00+00:00' }));
    adminService.cancelPriceChange.mockResolvedValue(row({ status: 'cancelled', stage: 'cancelled' }));
    renderPage();

    await user.click(await screen.findByRole('button', { name: 'Cancel change' }));
    const confirm = await screen.findByRole('dialog');
    expect(within(confirm).getByText(
      'Customers were already told about this change. Cancelling it sends them nothing, and prices stay as they are.',
    )).toBeInTheDocument();
    expect(adminService.cancelPriceChange).not.toHaveBeenCalled();
    await user.click(within(confirm).getByRole('button', { name: 'Cancel change' }));

    await waitFor(() => expect(adminService.cancelPriceChange).toHaveBeenCalledWith(7));
  });

  it('tells a scheduled change\'s admin that nobody has been told yet', async () => {
    const user = userEvent.setup();
    list(HIDDEN, row());
    renderPage();

    await user.click(await screen.findByRole('button', { name: 'Cancel change' }));

    expect(await within(await screen.findByRole('dialog')).findByText(
      'Prices stay as they are. No customer has been told yet.',
    )).toBeInTheDocument();
    expect(adminService.cancelPriceChange).not.toHaveBeenCalled();
  });

  it('offers the same two buttons in the drawer', async () => {
    const user = userEvent.setup();
    const scheduled = row();
    list(HIDDEN, scheduled);
    adminService.getPriceChange.mockResolvedValue({
      price_change: scheduled,
      counts: {
        announcement: { telegram: 0, email: 0, none: 0, queued: 0 },
        reminder: { telegram: 0, email: 0, none: 0, queued: 0 },
        eligible: 0, courtesy_used: 0, exceptions: 0,
      },
    });
    renderPage();

    await user.click(await screen.findByRole('button', { name: 'Details' }));
    const drawer = await screen.findByRole('dialog');

    expect(await within(drawer).findByRole('button', { name: 'Edit' })).toBeInTheDocument();
    expect(within(drawer).getByRole('button', { name: 'Cancel change' })).toBeInTheDocument();
  });
});

describe('the buttons follow the published actions', () => {
  it('hides a button the role cannot use and disables one the state refuses, with the reason', async () => {
    const user = userEvent.setup();
    const reason = 'Only one price change can be pending at a time.';
    const locked = 'This change can no longer be edited.';
    list(
      { allowed: false, block_reason: reason },
      row({ allowed_actions: { edit: { allowed: false, block_reason: locked }, cancel: HIDDEN } }),
    );
    renderPage();

    const create = await screen.findByRole('button', { name: 'New price change' });
    expect(create).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Edit' })).toBeDisabled();
    expect(screen.queryByRole('button', { name: 'Cancel change' })).toBeNull();

    await user.hover(create.parentElement);
    expect(await screen.findByText(reason)).toBeInTheDocument();
    await user.hover(screen.getByRole('button', { name: 'Edit' }).parentElement);
    expect(await screen.findByText(locked)).toBeInTheDocument();
  });

  it('an operator sees the page read-only', async () => {
    list(HIDDEN, row({ allowed_actions: { edit: HIDDEN, cancel: HIDDEN } }));
    renderPage();

    expect(await screen.findByRole('button', { name: 'Details' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'New price change' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Edit' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Cancel change' })).toBeNull();
    expect(adminService.getProducts).not.toHaveBeenCalled();
  });
});
