import React from 'react';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
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
  },
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key, fallback, opts) => String(typeof fallback === 'string' ? fallback : key)
      .replace(/\{\{(\w+)\}\}/g, (_, name) => String(opts?.[name] ?? '')),
  }),
}));

// The exact keys the real routes publish: tests/integration/test_price_change_admin_ui_contract.py diffs each Set
// against the HTTP response, so a fixture cannot carry a field the backend does not send.
const PRICE_CHANGE_ROW_KEYS = new Set([
  'id', 'status', 'stage', 'announce_on', 'change_on', 'reminder_on', 'reminder_lead_days', 'courtesy_days',
  'courtesy_last_day', 'price_valid_until', 'effective_at', 'courtesy_ends_at', 'announcement_started_at',
  'reminder_started_at', 'applied_at', 'cancelled_at', 'created_by_id', 'created_by_name', 'cancelled_by_id',
  'cancelled_by_name', 'created_at', 'allowed_actions', 'items',
]);
const PRICE_CHANGE_ITEM_KEYS = new Set([
  'product_id', 'product_name', 'current_price', 'new_price', 'old_price', 'courtesy_cap', 'min_order_quantity',
]);
const PRICE_CHANGE_ACTION_KEYS = new Set(['edit', 'cancel']);
const PRICE_CHANGE_LIST_ACTION_KEYS = new Set(['create']);
const ACTION_KEYS = new Set(['allowed', 'block_reason']);
const PRICE_CHANGE_COUNT_KEYS = new Set(['announcement', 'reminder', 'eligible', 'courtesy_used', 'exceptions']);
const NOTICE_COUNT_KEYS = new Set(['telegram', 'email', 'none', 'queued']);
const UNREACHABLE_ROW_KEYS = new Set(['user_id', 'name', 'phone']);
const EXCEPTION_ROW_KEYS = new Set([
  'id', 'user_id', 'customer_name', 'customer_phone', 'reason', 'granted_by_id', 'granted_by_name', 'granted_at',
  'used_order_id', 'used_order_number', 'used_at',
]);

const only = (keys, value, label) => {
  const extra = Object.keys(value).filter((key) => !keys.has(key));
  const missing = [...keys].filter((key) => !(key in value));
  if (extra.length || missing.length) throw new Error(`${label}: extra ${extra}, missing ${missing}`);
  return value;
};

const ALLOWED = { allowed: true, block_reason: null };
const actions = () => only(PRICE_CHANGE_ACTION_KEYS, { edit: only(ACTION_KEYS, { ...ALLOWED }, 'action'), cancel: only(ACTION_KEYS, { ...ALLOWED }, 'action') }, 'actions');

const item = (overrides = {}) => only(PRICE_CHANGE_ITEM_KEYS, {
  product_id: 1, product_name: 'Water 18.9 L', current_price: 18000, new_price: 20000, old_price: null,
  courtesy_cap: 2, min_order_quantity: 2, ...overrides,
}, 'item');

const row = (overrides = {}) => only(PRICE_CHANGE_ROW_KEYS, {
  id: 7, status: 'announced', stage: 'announced', announce_on: '2027-01-04', change_on: '2027-02-01',
  reminder_on: '2027-01-25', reminder_lead_days: 7, courtesy_days: 14, courtesy_last_day: '2027-02-14',
  price_valid_until: '2027-01-31', effective_at: '2027-01-31T19:00:00+00:00', courtesy_ends_at: '2027-02-14T19:00:00+00:00',
  announcement_started_at: '2027-01-04T04:00:00+00:00', reminder_started_at: null, applied_at: null,
  cancelled_at: null, created_by_id: 3, created_by_name: 'Aziza Manager', cancelled_by_id: null,
  cancelled_by_name: null, created_at: '2026-12-20T05:00:00+00:00',
  allowed_actions: actions(),
  items: [item()], ...overrides,
}, 'row');

const notices = (telegram, email, none, queued) => only(NOTICE_COUNT_KEYS, { telegram, email, none, queued }, 'notices');
const counts = (overrides = {}) => only(PRICE_CHANGE_COUNT_KEYS, {
  announcement: notices(120, 14, 9, 0), reminder: notices(101, 11, 7, 3), eligible: 95, courtesy_used: 12,
  exceptions: 2, ...overrides,
}, 'counts');

const unreachable = (user_id, name, phone) => only(UNREACHABLE_ROW_KEYS, { user_id, name, phone }, 'unreachable');
const exception = (overrides = {}) => only(EXCEPTION_ROW_KEYS, {
  id: 1, user_id: 40, customer_name: 'Dilnoza Karimova', customer_phone: '+998901112233',
  reason: 'Cancelled after the switch', granted_by_id: 3, granted_by_name: 'Aziza Manager',
  granted_at: '2027-02-03T07:15:00+00:00', used_order_id: null, used_order_number: null, used_at: null, ...overrides,
}, 'exception');

const renderPage = () => render(
  <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <PriceChanges />
  </QueryClientProvider>,
);

const list = (...items) => adminService.listPriceChanges.mockResolvedValue({
  items, allowed_actions: only(PRICE_CHANGE_LIST_ACTION_KEYS, { create: only(ACTION_KEYS, { ...ALLOWED }, 'action') }, 'list actions'),
});

const openDetail = async (priceChange, countsValue = counts()) => {
  list(priceChange);
  adminService.getPriceChange.mockResolvedValue({ price_change: priceChange, counts: countsValue });
  renderPage();
  fireEvent.click(await screen.findByRole('button', { name: 'Details' }));
  return screen.findByRole('dialog');
};

beforeEach(() => {
  vi.clearAllMocks();
  adminService.listPriceChangeUnreachable.mockResolvedValue({ items: [], meta: { page: 1, per_page: 20, total: 0 } });
  adminService.listPriceChangeExceptions.mockResolvedValue([]);
});

describe('the list', () => {
  it('lists each change with its stage, dates and prices', async () => {
    list(row());
    renderPage();

    expect(await screen.findByText('Water 18.9 L: 18,000 UZS → 20,000 UZS')).toBeInTheDocument();
    expect(screen.getByText('Announced')).toBeInTheDocument();
    expect(screen.getByText('Jan 04, 2027')).toBeInTheDocument();
    expect(screen.getByText('Feb 01, 2027')).toBeInTheDocument();
    expect(screen.getByText(
      'Schedule a price change and tell customers ahead of time. New prices start at 00:00 Tashkent time on the change date.',
    )).toBeInTheDocument();
  });

  it('keeps an applied change\'s history: the old price, never the new one twice', async () => {
    list(row({
      status: 'applied', stage: 'applied', applied_at: '2027-01-31T19:00:30+00:00',
      items: [item({ current_price: 20000, old_price: 18000 })],
    }));
    renderPage();

    expect(await screen.findByText('Water 18.9 L: 18,000 UZS → 20,000 UZS')).toBeInTheDocument();
    expect(screen.queryByText(/20,000 UZS → 20,000 UZS/)).toBeNull();
    expect(screen.getByText('New prices in effect')).toBeInTheDocument();
  });

  it('renders the published stage, never one derived from status', async () => {
    // status says "announced"; the published stage says the old-price window is over.
    list(row({ status: 'applied', stage: 'window_closed' }));
    renderPage();

    expect(await screen.findByText('Old-price window closed')).toBeInTheDocument();
    expect(screen.queryByText('Announced')).toBeNull();
    expect(screen.queryByText('New prices in effect')).toBeNull();
  });

  it('says so when there is no change yet', async () => {
    list();
    renderPage();

    expect(await screen.findByText('No price changes yet')).toBeInTheDocument();
  });
});

describe('the detail', () => {
  it('opens the detail with the timeline, the products and every published count', async () => {
    const drawer = await openDetail(row({ status: 'announced', stage: 'reminded', reminder_started_at: '2027-01-25T04:00:00+00:00' }));
    const within_ = within(drawer);

    expect(await within_.findByText('Reminder sent')).toBeInTheDocument();
    const active = drawer.querySelector('.ant-steps-item-process');
    expect(active).toHaveTextContent('New prices');
    expect(drawer.querySelectorAll('.ant-steps-item-finish')).toHaveLength(2);

    expect(within_.getByText('Done Jan 04, 2027 09:00')).toBeInTheDocument();
    expect(within_.getByText('Jan 25, 2027')).toBeInTheDocument();
    expect(within_.getByText('Feb 14, 2027')).toBeInTheDocument();

    const products = within_.getByRole('region', { name: 'Products' });
    expect(within(products).getByText('Water 18.9 L')).toBeInTheDocument();
    expect(within(products).getByText('18,000 UZS')).toBeInTheDocument();
    expect(within(products).getByText('20,000 UZS')).toBeInTheDocument();

    const notice = within_.getByRole('region', { name: 'Notices' });
    const announcement = within(notice).getByText('Announcement').closest('tr');
    expect([...announcement.querySelectorAll('td')].map((cell) => cell.textContent)).toEqual(['Announcement', '120', '14', '9', '0']);
    const reminder = within(notice).getByText('Reminder').closest('tr');
    expect([...reminder.querySelectorAll('td')].map((cell) => cell.textContent)).toEqual(['Reminder', '101', '11', '7', '3']);
    expect(within_.getByText('Customers who keep their price').nextSibling).toHaveTextContent('95');
    expect(within_.getByText('Orders that used it').nextSibling).toHaveTextContent('12');
    expect(within_.getByText('Exceptions granted').nextSibling).toHaveTextContent('2');
    expect(adminService.getPriceChange).toHaveBeenCalledWith(7);
  });

  it('pages through the customers we could not reach', async () => {
    adminService.listPriceChangeUnreachable.mockImplementation(async (_id, { page }) => ({
      items: [page === 1 ? unreachable(40, 'Dilnoza Karimova', '+998901112233') : unreachable(41, 'Bobur Aliyev', '+998909998877')],
      meta: { page, per_page: 20, total: 21, pages: 2, has_next: page === 1, has_prev: page === 2 },
    }));
    const drawer = await openDetail(row());

    expect(await within(drawer).findByText('Dilnoza Karimova')).toBeInTheDocument();
    expect(adminService.listPriceChangeUnreachable).toHaveBeenCalledWith(7, { page: 1, perPage: 20 });
    expect(within(drawer).getByText('Call them between 09:00 and 18:00, or ask the driver to tell them at the door.')).toBeInTheDocument();

    fireEvent.click(within(drawer).getByTitle('2'));

    expect(await within(drawer).findByText('Bobur Aliyev')).toBeInTheDocument();
    expect(adminService.listPriceChangeUnreachable).toHaveBeenLastCalledWith(7, { page: 2, perPage: 20 });
  });

  it('lists manager exceptions with who, when and why', async () => {
    adminService.listPriceChangeExceptions.mockResolvedValue([
      exception(),
      exception({ id: 2, customer_name: 'Bobur Aliyev', used_order_id: 9, used_order_number: 'TG_000009_27' }),
    ]);
    const drawer = await openDetail(row());

    const first = (await within(drawer).findByText('Dilnoza Karimova')).closest('tr');
    expect(first).toHaveTextContent('+998901112233');
    expect(first).toHaveTextContent('Cancelled after the switch');
    expect(first).toHaveTextContent('Aziza Manager');
    expect(first).toHaveTextContent('Feb 03, 2027 12:15');
    expect([...first.querySelectorAll('td')].pop()).toHaveTextContent('-');
    expect(within(drawer).getByText('Bobur Aliyev').closest('tr')).toHaveTextContent('TG_000009_27');
    expect(adminService.listPriceChangeExceptions).toHaveBeenCalledWith(7);
  });

  it('a cancelled change says who cancelled it', async () => {
    const drawer = await openDetail(row({
      status: 'cancelled', stage: 'cancelled', cancelled_by_id: 4, cancelled_by_name: 'Rustam Admin',
      cancelled_at: '2027-01-10T10:30:00+00:00',
    }));

    expect(await within(drawer).findByText(/Rustam Admin/)).toBeInTheDocument();
    expect(within(drawer).getByText(/Jan 10, 2027 15:30/)).toBeInTheDocument();
    expect(drawer.querySelector('.ant-steps-item-error')).toBeTruthy();
    await waitFor(() => expect(adminService.getPriceChange).toHaveBeenCalled());
  });
});
