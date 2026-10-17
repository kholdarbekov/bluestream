/**
 * The Lapsed customers worklist (reorder-reminders spec §9.1), driven down to the HTTP call: only
 * the axios instance is mocked, so each assertion reads the URL, params and body the backend gets.
 * Every flag the page draws from (`can_resolve`, `can_turn_off_reminders`, `pending_close`,
 * `contact_restriction`, the vocabularies) is the backend's, rendered as it comes.
 *
 * The fixtures are shaped like the backend's JSON: `to_wire` types (ISO instants with an offset,
 * ISO dates, money as numbers), `null` where the backend publishes null, and the RESOLVED reminder
 * language (`recipient.reminder_language`). tests/unit/test_lapsed_customers_ui_contract.py holds
 * `WORKLIST_ROW_KEYS` to the backend's own `ROW_KEYS` tuple, and the vocabularies below to its
 * constants.
 */
import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { message } from 'antd';

import LapsedCustomers from '../../pages/LapsedCustomers';
import api from '../../services/api';

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
    // Each call reads back its fallback with i18next's `{{token}}` interpolation; dynamic keys
    // fall back to the raw published code.
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
    message: { success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn(), loading: vi.fn(), destroy: vi.fn(), open: vi.fn() },
  };
});

const SUMMARY_URL = '/admin/reorder-reminders/summary';
const EPISODES_URL = '/admin/reorder-reminders/episodes';

// Every key `ReorderReminderWorklist` publishes on a row (worklist.ROW_KEYS).
const WORKLIST_ROW_KEYS = new Set([
  'episode_id', 'status', 'is_dry_run', 'recipient', 'phones', 'last_delivery_at', 'days_since',
  'usual_gap_days', 'gap_source', 'threshold_days', 'due_on', 'days_overdue', 'step',
  'next_action_on', 'reminders_sent', 'last_channel', 'last_reminder_at', 'escalation_flag',
  'escalated_at', 'suspended_reason', 'suspended_at', 'resolved_at', 'outcome', 'outcome_note',
  'resolved_by', 'history', 'reply_excerpt', 'stuck_orders', 'setting', 'contact_restriction',
  'places', 'outlets', 'bottles_held', 'open_debt', 'lifetime_deliveries', 'can_resolve',
  'can_turn_off_reminders', 'pending_close',
]);
// The row's list fields are always lists on the wire; the rest default to null, as the backend does.
const worklistRow = (overrides) => {
  const row = Object.assign(
    Object.fromEntries([...WORKLIST_ROW_KEYS].map((key) => [key, null])),
    {
      phones: [], history: [], stuck_orders: [], places: [], outlets: [],
      can_resolve: false, can_turn_off_reminders: false, pending_close: false,
    },
    overrides,
  );
  Object.keys(row).forEach((key) => { if (!WORKLIST_ROW_KEYS.has(key)) throw new Error(`fixture key ${key} is not published`); });
  return row;
};

// Published by summary(): the vocabularies are the backend's tuples, in the backend's order.
const OUTCOMES = ['reordered', 'away', 'moved', 'other_supplier', 'price', 'service_issue', 'unreachable', 'do_not_contact', 'other'];
const FLAGS = ['no_response', 'no_channel', 'stopped', 'blocked', 'snoozed', 'replied', 'stuck_order', 'long_lapsed', 'outlet_dormant'];
const summaryFor = (mode, overrides = {}) => ({
  mode,
  // Published by summary() (Task 20b); the page never re-derives it from `mode` (master amendment 7).
  row_set_is_dry_run: mode === 'dry_run',
  settings: [
    { key: 'fallback_days', value: 30 }, { key: 'ratio', value: 1.5 }, { key: 'min_deliveries', value: 3 },
    { key: 'max_per_episode', value: 2 }, { key: 'gap_days', value: 7 }, { key: 'snooze_days', value: 7 },
    { key: 'escalate_after_days', value: 7 }, { key: 'fresh_days', value: 14 },
    { key: 'live_order_grace_days', value: 3 }, { key: 'local_time', value: '10:00' },
  ],
  // Only the tabs the badge counts are published; paused rows are not counted.
  counts: { follow_up: 2, reminding: 1 },
  metrics_30d: {
    lapses_started: 12,
    reminders_sent: { telegram: 9, email: 3 },
    reordered_after_reminder: 5,
    place_resupplied: 1,
    reordered_before_contact: 2,
    stuck_order_delivered: 1,
    escalated_by_flag: Object.fromEntries(FLAGS.map((flag) => [flag, flag === 'no_response' ? 4 : 0])),
    switched_off_by_customers: 1,
    blocks_after_reminder: 0,
  },
  outcomes: OUTCOMES,
  flags: FLAGS,
  tabs: ['follow_up', 'reminding', 'paused', 'resolved'],
  ...overrides,
});

const LONG_LAPSED = worklistRow({
  episode_id: 501,
  status: 'escalated',
  is_dry_run: false,
  recipient: { user_id: 77, name: 'Alisher Mijoz', reminder_language: 'ru' },
  phones: [
    { user_id: 77, name: 'Alisher Mijoz', phone: '+998901110001', is_recipient: true, blocked_bot: false, blocked_since: null },
    { user_id: 78, name: 'Alisher Ikkinchi', phone: '+998937771122', is_recipient: false, blocked_bot: true, blocked_since: '2026-10-01T08:00:00+00:00' },
  ],
  last_delivery_at: '2026-08-01T07:00:00+00:00',
  days_since: 75,
  usual_gap_days: null,
  gap_source: 'preset',
  threshold_days: 30,
  due_on: '2026-09-01',
  days_overdue: 44,
  step: 0,
  reminders_sent: 0,
  escalation_flag: 'long_lapsed',
  escalated_at: '2026-10-14T05:00:00+00:00',
  history: [{ kind: 'escalated', step: null, channel: null, detail: 'long_lapsed', at: '2026-10-14T05:00:00+00:00' }],
  setting: { enabled: false, changed_by_kind: 'customer', changed_via: 'telegram_button', changed_at: '2026-10-10T06:00:00+00:00', reason: null, state: 'customer_off' },
  contact_restriction: { kind: 'stopped', since: '2026-10-10T06:00:00+00:00', via: 'telegram_button' },
  places: [{
    group_id: 4,
    label: 'Office 12',
    address: '12 Amir Temur, Tashkent',
    members: [{ user_id: 90, name: 'Bobur Mijoz', phone: '+998901110003', last_delivery_at: '2026-07-20T07:00:00+00:00' }],
  }],
  bottles_held: 3,
  open_debt: 0,
  lifetime_deliveries: 2,
  can_resolve: true,
});
const STUCK = worklistRow({
  episode_id: 502,
  status: 'escalated',
  is_dry_run: false,
  recipient: { user_id: 80, name: 'Dilshod Mijoz', reminder_language: 'uz' },
  phones: [{ user_id: 80, name: 'Dilshod Mijoz', phone: '+998901110020', is_recipient: true, blocked_bot: false, blocked_since: null }],
  last_delivery_at: '2026-08-20T07:00:00+00:00',
  days_since: 56,
  gap_source: 'preset',
  threshold_days: 30,
  due_on: '2026-09-20',
  days_overdue: 25,
  step: 0,
  reminders_sent: 0,
  escalation_flag: 'stuck_order',
  stuck_orders: [{ order_id: 9001, order_number: 'TG_009001_26', status: 'confirmed', age_days: 25, awaiting_new_date: false }],
  setting: { enabled: true, changed_by_kind: null, changed_via: null, changed_at: null, reason: null, state: 'on' },
  bottles_held: 0,
  open_debt: 0,
  lifetime_deliveries: 1,
  can_resolve: true,
});
const REMINDING = worklistRow({
  episode_id: 503,
  status: 'open',
  is_dry_run: false,
  recipient: { user_id: 81, name: 'Feruza Mijoz', reminder_language: 'uz' },
  phones: [{ user_id: 81, name: 'Feruza Mijoz', phone: '+998901110030', is_recipient: true, blocked_bot: false, blocked_since: null }],
  last_delivery_at: '2026-09-01T07:00:00+00:00',
  days_since: 44,
  usual_gap_days: 14,
  gap_source: 'personal',
  threshold_days: 21,
  due_on: '2026-09-23',
  days_overdue: 22,
  step: 1,
  next_action_on: '2026-10-18',
  reminders_sent: 1,
  last_channel: 'telegram',
  last_reminder_at: '2026-10-11T05:10:00+00:00',
  history: [{ kind: 'reminder_sent', step: 1, channel: 'telegram', detail: null, at: '2026-10-11T05:10:00+00:00' }],
  setting: { enabled: true, changed_by_kind: null, changed_via: null, changed_at: null, reason: null, state: 'on' },
  bottles_held: 2.5,
  open_debt: 25000,
  lifetime_deliveries: 6,
  can_turn_off_reminders: true,
});
const PAUSED = worklistRow({
  episode_id: 504,
  status: 'suspended',
  is_dry_run: false,
  recipient: { user_id: 82, name: 'Parvina Mijoz', reminder_language: 'en' },
  phones: [{ user_id: 82, name: 'Parvina Mijoz', phone: '+998901110040', is_recipient: true, blocked_bot: false, blocked_since: null }],
  last_delivery_at: '2026-08-01T07:00:00+00:00',
  days_since: 75,
  gap_source: 'preset',
  threshold_days: 30,
  due_on: '2026-09-01',
  days_overdue: 44,
  step: 2,
  reminders_sent: 2,
  escalation_flag: 'no_response',
  escalated_at: '2026-10-01T05:00:00+00:00',
  suspended_reason: 'subscription',
  suspended_at: '2026-10-05T05:00:00+00:00',
  setting: { enabled: true, changed_by_kind: null, changed_via: null, changed_at: null, reason: null, state: 'on' },
  bottles_held: 0,
  open_debt: 0,
  lifetime_deliveries: 4,
  can_resolve: true,
});

let summaryMode = 'on';
let summaryOverrides = {};
let totalOverride = null;
const pages = {};

const renderPage = () => render(
  <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <LapsedCustomers />
  </QueryClientProvider>,
);

const callsTo = (url) => api.get.mock.calls.filter(([called]) => called === url);

beforeEach(() => {
  vi.clearAllMocks();
  summaryMode = 'on';
  summaryOverrides = {};
  totalOverride = null;
  Object.assign(pages, { follow_up: [LONG_LAPSED, STUCK], reminding: [REMINDING], paused: [PAUSED], resolved: [] });
  mockAuth.hasPermission.mockReturnValue(true);
  api.get.mockImplementation((url, config) => {
    if (url === SUMMARY_URL) {
      return Promise.resolve({ data: { success: true, data: summaryFor(summaryMode, summaryOverrides) } });
    }
    if (url === EPISODES_URL) {
      const { tab, flag, search } = config.params;
      let items = pages[tab].filter((row) => !flag || row.escalation_flag === flag);
      if (search) items = items.filter((row) => row.phones.some((phone) => phone.phone.includes(search)));
      return Promise.resolve({
        data: { success: true, data: { items, meta: { page: config.params.page, per_page: 20, total: totalOverride ?? items.length, pages: 1, has_next: false, has_prev: false } } },
      });
    }
    return Promise.reject(new Error(`unexpected GET ${url}`));
  });
  api.post.mockResolvedValue({ data: { success: true, data: { episode: { episode_id: 501, status: 'resolved' } } } });
  api.put.mockResolvedValue({ data: { success: true, data: { notification_settings: {} } } });
});

const rowFor = (name) => screen.getByText(name).closest('tr');

it('opens on Needs follow-up and renders every published field of a long-lapsed row', async () => {
  renderPage();

  expect(await screen.findByText('Alisher Mijoz')).toBeInTheDocument();
  expect(api.get).toHaveBeenCalledWith(EPISODES_URL, {
    params: { tab: 'follow_up', page: 1, per_page: 20 },
  });
  const row = rowFor('Alisher Mijoz');
  expect(row).toHaveTextContent('+998937771122');
  expect(within(row).getByText('blocked bot')).toBeInTheDocument();
  expect(row).toHaveTextContent('Customer switched reorder reminders off on Oct 10, 2026 via telegram_button — service follow-up only.');
  expect(row).toHaveTextContent('Shared place: Office 12 · 12 Amir Temur, Tashkent');
  expect(row).toHaveTextContent('Bobur Mijoz · +998901110003 · last delivery Jul 20, 2026');
  expect(row).toHaveTextContent('Last delivery Aug 01, 2026 · 75 days ago');
  expect(row).toHaveTextContent('Preset · threshold 30 days');
  expect(row).toHaveTextContent('44 days overdue (due Sep 01, 2026)');
  expect(row).toHaveTextContent('2 deliveries · 3 bottles held · debt 0 UZS');
  expect(row).toHaveTextContent('Reminders off · customer · telegram_button · Oct 10, 2026');
  expect(within(row).getByText('long_lapsed')).toBeInTheDocument();
  expect(rowFor('Dilshod Mijoz')).toHaveTextContent('Order TG_009001_26 · confirmed · 25 days old');
  expect(screen.getByText('follow_up (2)')).toBeInTheDocument();
  expect(screen.getByText('Call customers only between 09:00 and 18:00.')).toBeInTheDocument();
});

it('names a shared place by its label and its address, or by whichever it has', async () => {
  const placeRow = (episodeId, name, place) => ({
    ...STUCK,
    episode_id: episodeId,
    recipient: { user_id: episodeId, name, reminder_language: 'uz' },
    places: [{ group_id: episodeId, members: [], ...place }],
  });
  pages.follow_up = [
    placeRow(601, 'Both Mijoz', { label: 'Office 12', address: '12 Amir Temur, Tashkent' }),
    placeRow(602, 'Label Mijoz', { label: 'Warehouse', address: null }),
    placeRow(603, 'Address Mijoz', { label: null, address: '5 Navoi, Tashkent' }),
    placeRow(604, 'Neither Mijoz', { label: null, address: null }),
  ];
  renderPage();

  await screen.findByText('Both Mijoz');

  expect(rowFor('Both Mijoz')).toHaveTextContent('Shared place: Office 12 · 12 Amir Temur, Tashkent');
  expect(rowFor('Label Mijoz')).toHaveTextContent('Shared place: Warehouse');
  expect(rowFor('Address Mijoz')).toHaveTextContent('Shared place: 5 Navoi, Tashkent');
  expect(rowFor('Neither Mijoz')).toHaveTextContent('Shared place: #604');
});

it('prints a fractional bottle balance and the open debt as published', async () => {
  renderPage();
  await screen.findByText('Alisher Mijoz');

  fireEvent.click(screen.getByText('reminding (1)'));

  expect(await screen.findByText('Feruza Mijoz')).toBeInTheDocument();
  expect(rowFor('Feruza Mijoz')).toHaveTextContent('6 deliveries · 2.5 bottles held · debt 25,000 UZS');
});

it('shows the reminder language the backend publishes, as published', async () => {
  // Final review B1/E1: the backend resolves the language once (`reminder_language`); the page
  // derives nothing from a stored value, so it shows exactly the published code, or no tag at all.
  pages.follow_up = [
    LONG_LAPSED, // published "ru"
    { ...STUCK, episode_id: 509, recipient: { ...STUCK.recipient, reminder_language: 'en' } },
    { ...STUCK, episode_id: 510, recipient: { user_id: 91, name: 'Zafar Mijoz', reminder_language: 'uz' } },
    { ...STUCK, episode_id: 511, recipient: { user_id: 92, name: 'Nargiza Mijoz', reminder_language: null } },
  ];
  renderPage();

  await screen.findByText('Alisher Mijoz');
  const languageOf = (name) => within(rowFor(name)).queryByTitle('Reminder language')?.textContent ?? null;

  expect(languageOf('Alisher Mijoz')).toBe('RU');
  expect(languageOf('Dilshod Mijoz')).toBe('EN');
  expect(languageOf('Zafar Mijoz')).toBe('UZ');
  expect(languageOf('Nargiza Mijoz')).toBeNull();
});

it('marks the setting line as off from the published state, for a customer-off and an admin-off alike', async () => {
  pages.follow_up = [
    LONG_LAPSED, // state customer_off
    STUCK, // state on
    {
      ...STUCK,
      episode_id: 512,
      recipient: { user_id: 93, name: 'Excluded Mijoz', reminder_language: 'uz' },
      setting: {
        enabled: false, changed_by_kind: 'admin', changed_via: 'admin_ui', changed_at: '2026-10-12T06:00:00+00:00',
        reason: 'Moved', state: 'admin_off',
      },
    },
  ];
  renderPage();

  await screen.findByText('Alisher Mijoz');
  const settingLine = (name, text) => within(rowFor(name)).getByText(text);

  expect(settingLine('Alisher Mijoz', 'Reminders off · customer · telegram_button · Oct 10, 2026')).toHaveClass(
    'ant-typography-danger'
  );
  expect(settingLine('Excluded Mijoz', 'Reminders off · admin · admin_ui · Oct 12, 2026')).toHaveClass(
    'ant-typography-danger'
  );
  expect(settingLine('Dilshod Mijoz', 'Reminders on · never changed')).not.toHaveClass('ant-typography-danger');
});

it('lists do_not_contact first for a restricted row and posts the chosen outcome', async () => {
  renderPage();
  await screen.findByText('Alisher Mijoz');

  fireEvent.click(within(rowFor('Alisher Mijoz')).getByRole('button', { name: 'Record outcome' }));
  const dialog = await screen.findByRole('dialog');
  expect(within(dialog).getByText('Customer switched reorder reminders off on Oct 10, 2026 via telegram_button — service follow-up only.')).toBeInTheDocument();
  const options = within(dialog).getAllByRole('radio').map((radio) => radio.closest('label').textContent);
  expect(options[0]).toBe('do_not_contact');
  expect(options).toHaveLength(9);

  fireEvent.click(within(dialog).getByRole('radio', { name: 'do_not_contact' }));
  fireEvent.change(within(dialog).getByLabelText('Note (optional)'), { target: { value: 'Asked not to be called' } });
  fireEvent.click(within(dialog).getByRole('button', { name: 'Record outcome' }));

  await waitFor(() => expect(api.post).toHaveBeenCalledWith(
    '/admin/reorder-reminders/episodes/501/resolve',
    { outcome: 'do_not_contact', note: 'Asked not to be called' },
    { handledErrorCodes: ['REORDER_REMINDER_NOT_RESOLVABLE'] },
  ));
  await waitFor(() => expect(message.success).toHaveBeenCalledWith('Outcome saved'));
});

it('keeps the published outcome order for an unrestricted row', async () => {
  renderPage();
  await screen.findByText('Dilshod Mijoz');

  fireEvent.click(within(rowFor('Dilshod Mijoz')).getByRole('button', { name: 'Record outcome' }));
  const dialog = await screen.findByRole('dialog');

  expect(within(dialog).getAllByRole('radio').map((radio) => radio.closest('label').textContent)).toEqual(OUTCOMES);
});

it('offers exactly the outcomes the summary publishes', async () => {
  summaryOverrides = { outcomes: ['reordered', 'price', 'do_not_contact', 'other'] };
  renderPage();
  await screen.findByText('Dilshod Mijoz');

  fireEvent.click(within(rowFor('Dilshod Mijoz')).getByRole('button', { name: 'Record outcome' }));
  const dialog = await screen.findByRole('dialog');

  expect(within(dialog).getAllByRole('radio').map((radio) => radio.closest('label').textContent))
    .toEqual(['reordered', 'price', 'do_not_contact', 'other']);
});

it('explains that outcomes are unavailable when the summary failed, and cannot record', async () => {
  const unavailable = 'The list of outcomes is not available. Close this window and reload the page to try again.';
  const summaryFailure = { response: { status: 500, data: { success: false, message: 'Server error' } } };
  api.get.mockImplementation((url, config) => {
    if (url === SUMMARY_URL) return Promise.reject(summaryFailure);
    const items = pages[config.params.tab];
    return Promise.resolve({ data: { success: true, data: { items, meta: { page: 1, per_page: 20, total: items.length, pages: 1, has_next: false, has_prev: false } } } });
  });
  renderPage();
  await screen.findByText('Dilshod Mijoz');

  fireEvent.click(within(rowFor('Dilshod Mijoz')).getByRole('button', { name: 'Record outcome' }));
  const dialog = await screen.findByRole('dialog');

  expect(within(dialog).getByText(unavailable)).toBeInTheDocument();
  expect(within(dialog).queryAllByRole('radio')).toHaveLength(0);
  expect(within(dialog).getByRole('button', { name: 'Record outcome' })).toBeDisabled();
  expect(api.post).not.toHaveBeenCalled();
});

it('explains that outcomes are unavailable while the summary is still loading', async () => {
  const unavailable = 'The list of outcomes is not available. Close this window and reload the page to try again.';
  let releaseSummary;
  api.get.mockImplementation((url, config) => {
    if (url === SUMMARY_URL) {
      return new Promise((resolve) => { releaseSummary = () => resolve({ data: { success: true, data: summaryFor('on') } }); });
    }
    const items = pages[config.params.tab];
    return Promise.resolve({ data: { success: true, data: { items, meta: { page: 1, per_page: 20, total: items.length, pages: 1, has_next: false, has_prev: false } } } });
  });
  renderPage();
  await screen.findByText('Dilshod Mijoz');

  fireEvent.click(within(rowFor('Dilshod Mijoz')).getByRole('button', { name: 'Record outcome' }));
  const dialog = await screen.findByRole('dialog');
  expect(within(dialog).getByText(unavailable)).toBeInTheDocument();
  expect(within(dialog).getByRole('button', { name: 'Record outcome' })).toBeDisabled();

  releaseSummary();
  await waitFor(() => expect(within(dialog).getAllByRole('radio')).toHaveLength(9));
  expect(within(dialog).queryByText(unavailable)).toBeNull();
});

it('cannot record an outcome until one is chosen, and sends no note when none was typed', async () => {
  renderPage();
  await screen.findByText('Dilshod Mijoz');

  fireEvent.click(within(rowFor('Dilshod Mijoz')).getByRole('button', { name: 'Record outcome' }));
  const dialog = await screen.findByRole('dialog');
  const confirm = within(dialog).getByRole('button', { name: 'Record outcome' });
  expect(confirm).toBeDisabled();
  expect(within(dialog).getByLabelText('Note (optional)')).toHaveValue('');

  fireEvent.click(within(dialog).getByRole('radio', { name: 'away' }));
  fireEvent.change(within(dialog).getByLabelText('Note (optional)'), { target: { value: '   ' } });
  expect(confirm).toBeEnabled();
  fireEvent.click(confirm);

  await waitFor(() => expect(api.post).toHaveBeenCalledTimes(1));
  const [url, body] = api.post.mock.calls[0];
  expect(url).toBe('/admin/reorder-reminders/episodes/502/resolve');
  expect(body).toEqual({ outcome: 'away' });
  expect(Object.keys(body)).toEqual(['outcome']);
});

it('sends the note trimmed and never caps what the admin types (the backend strips, then caps)', async () => {
  renderPage();
  await screen.findByText('Dilshod Mijoz');

  fireEvent.click(within(rowFor('Dilshod Mijoz')).getByRole('button', { name: 'Record outcome' }));
  const dialog = await screen.findByRole('dialog');
  // 2000 real characters plus padding: 2010 typed, which a raw maxLength={2000} would cut.
  const padded = `  ${'x'.repeat(2000)}        `;
  const textarea = within(dialog).getByLabelText('Note (optional)');
  fireEvent.change(textarea, { target: { value: padded } });
  expect(textarea).not.toHaveAttribute('maxlength');
  expect(textarea).toHaveValue(padded);

  fireEvent.click(within(dialog).getByRole('radio', { name: 'other' }));
  fireEvent.click(within(dialog).getByRole('button', { name: 'Record outcome' }));

  await waitFor(() => expect(api.post).toHaveBeenCalledTimes(1));
  expect(api.post.mock.calls[0][1]).toEqual({ outcome: 'other', note: 'x'.repeat(2000) });
});

it('refreshes the rows and the summary after an outcome is saved', async () => {
  renderPage();
  await screen.findByText('Dilshod Mijoz');
  expect(callsTo(EPISODES_URL)).toHaveLength(1);
  expect(callsTo(SUMMARY_URL)).toHaveLength(1);

  fireEvent.click(within(rowFor('Dilshod Mijoz')).getByRole('button', { name: 'Record outcome' }));
  const dialog = await screen.findByRole('dialog');
  fireEvent.click(within(dialog).getByRole('radio', { name: 'moved' }));
  fireEvent.click(within(dialog).getByRole('button', { name: 'Record outcome' }));

  await waitFor(() => expect(callsTo(EPISODES_URL)).toHaveLength(2));
  expect(callsTo(SUMMARY_URL)).toHaveLength(2);
});

it('explains a lapse someone else already handled, closes the modal and refreshes', async () => {
  api.post.mockRejectedValueOnce({
    response: {
      status: 400,
      data: {
        success: false,
        error: 'VALIDATION_ERROR',
        error_code: 'REORDER_REMINDER_NOT_RESOLVABLE',
        message: 'Only a lapse waiting for follow-up can be resolved',
      },
    },
  });
  renderPage();
  await screen.findByText('Dilshod Mijoz');

  fireEvent.click(within(rowFor('Dilshod Mijoz')).getByRole('button', { name: 'Record outcome' }));
  const dialog = await screen.findByRole('dialog');
  fireEvent.click(within(dialog).getByRole('radio', { name: 'away' }));
  fireEvent.click(within(dialog).getByRole('button', { name: 'Record outcome' }));

  await waitFor(() => expect(message.warning).toHaveBeenCalledWith(
    'This lapse was already handled by someone else. The list has been refreshed.',
  ));
  expect(message.success).not.toHaveBeenCalled();
  await waitFor(() => expect(callsTo(EPISODES_URL)).toHaveLength(2));
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
});

it('refreshes when the lapse no longer exists (404)', async () => {
  api.post.mockRejectedValueOnce({
    response: { status: 404, data: { success: false, error: 'NOT_FOUND', message: 'Lapse not found' } },
  });
  renderPage();
  await screen.findByText('Dilshod Mijoz');

  fireEvent.click(within(rowFor('Dilshod Mijoz')).getByRole('button', { name: 'Record outcome' }));
  const dialog = await screen.findByRole('dialog');
  fireEvent.click(within(dialog).getByRole('radio', { name: 'away' }));
  fireEvent.click(within(dialog).getByRole('button', { name: 'Record outcome' }));

  await waitFor(() => expect(callsTo(EPISODES_URL)).toHaveLength(2));
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  expect(message.success).not.toHaveBeenCalled();
});

it('keeps the modal and the typed note when the backend refuses the request itself', async () => {
  api.post.mockRejectedValueOnce({
    response: { status: 400, data: { success: false, message: 'Validation failed', errors: ['note: String should have at most 2000 characters'] } },
  });
  renderPage();
  await screen.findByText('Dilshod Mijoz');

  fireEvent.click(within(rowFor('Dilshod Mijoz')).getByRole('button', { name: 'Record outcome' }));
  const dialog = await screen.findByRole('dialog');
  fireEvent.click(within(dialog).getByRole('radio', { name: 'away' }));
  fireEvent.change(within(dialog).getByLabelText('Note (optional)'), { target: { value: 'still here' } });
  fireEvent.click(within(dialog).getByRole('button', { name: 'Record outcome' }));

  await waitFor(() => expect(api.post).toHaveBeenCalledTimes(1));
  // api.js toasts the refusal; the page adds nothing and keeps what the admin typed.
  await waitFor(() => expect(within(dialog).getByRole('button', { name: 'Record outcome' })).toBeEnabled());
  expect(screen.getByRole('dialog')).toBeInTheDocument();
  expect(within(dialog).getByLabelText('Note (optional)')).toHaveValue('still here');
  expect(message.warning).not.toHaveBeenCalled();
  expect(message.error).not.toHaveBeenCalled();
  expect(callsTo(EPISODES_URL)).toHaveLength(1);
});

it('filters the Paused tab by a flag chip too, since a paused row that had escalated keeps its flag', async () => {
  renderPage();
  await screen.findByText('Alisher Mijoz');

  fireEvent.click(screen.getByText('paused'));
  await screen.findByText('Parvina Mijoz');
  fireEvent.click(within(screen.getByTestId('lapsed-flag-chips')).getByText('no_response'));

  await waitFor(() => expect(api.get).toHaveBeenLastCalledWith(EPISODES_URL, {
    params: { tab: 'paused', page: 1, per_page: 20, flag: 'no_response' },
  }));
  expect(await screen.findByText('Parvina Mijoz')).toBeInTheDocument();
  fireEvent.click(within(screen.getByTestId('lapsed-flag-chips')).getByText('stuck_order'));
  await waitFor(() => expect(api.get).toHaveBeenLastCalledWith(EPISODES_URL, {
    params: { tab: 'paused', page: 1, per_page: 20, flag: 'stuck_order' },
  }));
  await waitFor(() => expect(screen.queryByText('Parvina Mijoz')).toBeNull());
});

it('offers no flag chips on Being reminded, whose rows have not escalated', async () => {
  renderPage();
  await screen.findByText('Alisher Mijoz');
  expect(screen.getByTestId('lapsed-flag-chips')).toBeInTheDocument();

  fireEvent.click(screen.getByText('reminding (1)'));
  await screen.findByText('Feruza Mijoz');

  expect(screen.queryByTestId('lapsed-flag-chips')).toBeNull();
});

it('filters Needs follow-up by a flag chip', async () => {
  renderPage();
  await screen.findByText('Alisher Mijoz');

  fireEvent.click(within(screen.getByTestId('lapsed-flag-chips')).getByText('stuck_order'));

  await waitFor(() => expect(api.get).toHaveBeenCalledWith(EPISODES_URL, {
    params: { tab: 'follow_up', page: 1, per_page: 20, flag: 'stuck_order' },
  }));
  await waitFor(() => expect(screen.queryByText('Alisher Mijoz')).toBeNull());
  expect(screen.getByText('Dilshod Mijoz')).toBeInTheDocument();
});

it('drops the flag filter when the chip is clicked again', async () => {
  renderPage();
  await screen.findByText('Alisher Mijoz');
  const chips = screen.getByTestId('lapsed-flag-chips');

  fireEvent.click(within(chips).getByText('stuck_order'));
  await waitFor(() => expect(screen.queryByText('Alisher Mijoz')).toBeNull());
  fireEvent.click(within(chips).getByText('stuck_order'));

  expect(await screen.findByText('Alisher Mijoz')).toBeInTheDocument();
  expect(screen.getByText('Dilshod Mijoz')).toBeInTheDocument();
});

it('searches by the typed text, trimmed, and sends nothing once it is cleared', async () => {
  renderPage();
  await screen.findByText('Alisher Mijoz');

  const search = screen.getByPlaceholderText('Search by name or phone');
  fireEvent.change(search, { target: { value: '  +998901110020  ' } });
  fireEvent.keyDown(search, { key: 'Enter', code: 'Enter', keyCode: 13, charCode: 13 });

  await waitFor(() => expect(api.get).toHaveBeenCalledWith(EPISODES_URL, {
    params: { tab: 'follow_up', page: 1, per_page: 20, search: '+998901110020' },
  }));
  await waitFor(() => expect(screen.queryByText('Alisher Mijoz')).toBeNull());
  expect(screen.getByText('Dilshod Mijoz')).toBeInTheDocument();

  // The clear icon is what an admin clicks to drop the search.
  fireEvent.click(screen.getByRole('button', { name: 'close-circle' }));

  expect(await screen.findByText('Alisher Mijoz')).toBeInTheDocument();
  expect(screen.getByText('Dilshod Mijoz')).toBeInTheDocument();
  expect(api.get.mock.calls.some(([, config]) => config?.params?.search === '')).toBe(false);
});

it('starts every tab with no flag filter and on the first page', async () => {
  totalOverride = 45;
  renderPage();
  await screen.findByText('Alisher Mijoz');

  fireEvent.click(within(screen.getByTestId('lapsed-flag-chips')).getByText('stuck_order'));
  await waitFor(() => expect(api.get).toHaveBeenCalledWith(EPISODES_URL, {
    params: { tab: 'follow_up', page: 1, per_page: 20, flag: 'stuck_order' },
  }));
  fireEvent.click(screen.getByTitle('2'));
  await waitFor(() => expect(api.get).toHaveBeenCalledWith(EPISODES_URL, {
    params: { tab: 'follow_up', page: 2, per_page: 20, flag: 'stuck_order' },
  }));
  fireEvent.click(screen.getByText('resolved'));

  await waitFor(() => expect(api.get).toHaveBeenLastCalledWith(EPISODES_URL, {
    params: { tab: 'resolved', page: 1, per_page: 20 },
  }));
});

it('pages through the rows 20 at a time and returns to page 1 when the filter changes', async () => {
  totalOverride = 45;
  renderPage();
  await screen.findByText('Alisher Mijoz');

  fireEvent.click(screen.getByTitle('2'));
  await waitFor(() => expect(api.get).toHaveBeenCalledWith(EPISODES_URL, {
    params: { tab: 'follow_up', page: 2, per_page: 20 },
  }));
  fireEvent.click(within(screen.getByTestId('lapsed-flag-chips')).getByText('stuck_order'));

  await waitFor(() => expect(api.get).toHaveBeenLastCalledWith(EPISODES_URL, {
    params: { tab: 'follow_up', page: 1, per_page: 20, flag: 'stuck_order' },
  }));
});

it('reads the summary once, however many tabs are opened', async () => {
  renderPage();
  await screen.findByText('Alisher Mijoz');

  fireEvent.click(screen.getByText('reminding (1)'));
  await screen.findByText('Feruza Mijoz');
  fireEvent.click(screen.getByText('paused'));
  await screen.findByText('Parvina Mijoz');
  fireEvent.click(screen.getByText('resolved'));
  await waitFor(() => expect(api.get).toHaveBeenCalledWith(EPISODES_URL, { params: { tab: 'resolved', page: 1, per_page: 20 } }));

  expect(callsTo(SUMMARY_URL)).toHaveLength(1);
  expect(callsTo(EPISODES_URL).map(([, config]) => config.params.tab)).toEqual(['follow_up', 'reminding', 'paused', 'resolved']);
});

it('resolves a suspended row that had escalated from the Paused tab', async () => {
  renderPage();
  await screen.findByText('Alisher Mijoz');

  fireEvent.click(screen.getByText('paused'));
  expect(await screen.findByText('Parvina Mijoz')).toBeInTheDocument();
  expect(rowFor('Parvina Mijoz')).toHaveTextContent('subscription');
  fireEvent.click(within(rowFor('Parvina Mijoz')).getByRole('button', { name: 'Record outcome' }));
  const dialog = await screen.findByRole('dialog');
  fireEvent.click(within(dialog).getByRole('radio', { name: 'moved' }));
  fireEvent.click(within(dialog).getByRole('button', { name: 'Record outcome' }));

  await waitFor(() => expect(api.post).toHaveBeenCalledWith(
    '/admin/reorder-reminders/episodes/504/resolve',
    { outcome: 'moved' },
    { handledErrorCodes: ['REORDER_REMINDER_NOT_RESOLVABLE'] },
  ));
});

it('shows what was recorded on a resolved row and offers no action on it', async () => {
  pages.resolved = [worklistRow({
    ...STUCK,
    episode_id: 520,
    status: 'resolved',
    outcome: 'price',
    outcome_note: 'Found a cheaper supplier',
    resolved_at: '2026-10-15T09:00:00+00:00',
    resolved_by: { user_id: 3, name: 'Manager Aziza' },
    can_resolve: false,
  })];
  renderPage();
  await screen.findByText('Alisher Mijoz');

  fireEvent.click(screen.getByText('resolved'));

  // The previous tab's rows stay up until the new page arrives, so wait for the recorded outcome.
  await waitFor(() => expect(rowFor('Dilshod Mijoz')).toHaveTextContent('Outcome: price'));
  const row = rowFor('Dilshod Mijoz');
  expect(row).toHaveTextContent('Found a cheaper supplier');
  expect(within(row).queryByRole('button')).toBeNull();
});

it('turns reminders off for a Being reminded row through the partial settings PUT', async () => {
  renderPage();
  await screen.findByText('Alisher Mijoz');

  fireEvent.click(screen.getByText('reminding (1)'));
  expect(await screen.findByText('Feruza Mijoz')).toBeInTheDocument();
  expect(within(rowFor('Feruza Mijoz')).queryByRole('button', { name: 'Record outcome' })).toBeNull();
  fireEvent.click(within(rowFor('Feruza Mijoz')).getByRole('button', { name: 'Turn off reminders' }));
  const dialog = await screen.findByRole('dialog');
  fireEvent.click(within(dialog).getByRole('button', { name: 'Turn off reminders' }));
  expect(message.error).toHaveBeenCalledWith('Reason is required');
  expect(api.put).not.toHaveBeenCalled();

  fireEvent.change(within(dialog).getByLabelText('Reason'), { target: { value: '  Customer moved to Samarkand  ' } });
  fireEvent.click(within(dialog).getByRole('button', { name: 'Turn off reminders' }));

  await waitFor(() => expect(api.put).toHaveBeenCalledWith('/admin/users/81/notification-settings', {
    reorder_reminders_enabled: false,
    reason: 'Customer moved to Samarkand',
  }));
  const [, body] = api.put.mock.calls[0];
  expect(Object.keys(body).sort()).toEqual(['reason', 'reorder_reminders_enabled']);
  await waitFor(() => expect(message.success).toHaveBeenCalledWith('Reminders turned off'));
});

it('refuses a whitespace-only turn-off reason', async () => {
  renderPage();
  await screen.findByText('Alisher Mijoz');
  fireEvent.click(screen.getByText('reminding (1)'));
  await screen.findByText('Feruza Mijoz');

  fireEvent.click(within(rowFor('Feruza Mijoz')).getByRole('button', { name: 'Turn off reminders' }));
  const dialog = await screen.findByRole('dialog');
  fireEvent.change(within(dialog).getByLabelText('Reason'), { target: { value: '     ' } });
  fireEvent.click(within(dialog).getByRole('button', { name: 'Turn off reminders' }));

  expect(message.error).toHaveBeenCalledWith('Reason is required');
  expect(api.put).not.toHaveBeenCalled();
});

it('offers Turn off only where the backend publishes it', async () => {
  pages.reminding = [
    REMINDING,
    { ...REMINDING, episode_id: 530, recipient: { user_id: 95, name: 'Off Allaqachon', reminder_language: 'uz' }, can_turn_off_reminders: false },
  ];
  renderPage();
  await screen.findByText('Alisher Mijoz');

  fireEvent.click(screen.getByText('reminding (1)'));
  await screen.findByText('Feruza Mijoz');

  expect(within(rowFor('Feruza Mijoz')).getByRole('button', { name: 'Turn off reminders' })).toBeInTheDocument();
  expect(within(rowFor('Off Allaqachon')).queryByRole('button', { name: 'Turn off reminders' })).toBeNull();
});

it('shows the dry-run banner and offers no Turn off on a simulated row', async () => {
  summaryMode = 'dry_run';
  pages.reminding = [{ ...REMINDING, is_dry_run: true, can_turn_off_reminders: false }];
  renderPage();

  expect(await screen.findByText('Dry-run — simulated rows, discarded at go-live.')).toBeInTheDocument();
  fireEvent.click(await screen.findByText('reminding (1)'));
  expect(await screen.findByText('Feruza Mijoz')).toBeInTheDocument();
  expect(within(rowFor('Feruza Mijoz')).getByText('Simulated')).toBeInTheDocument();
  expect(within(rowFor('Feruza Mijoz')).queryByRole('button', { name: 'Turn off reminders' })).toBeNull();
  expect(within(screen.getByTestId('lapsed-metrics')).getByText('Reminders simulated · Telegram')).toBeInTheDocument();
});

it('says a row already reordered closes at the next run, and keeps it listed and resolvable', async () => {
  pages.follow_up = [
    { ...LONG_LAPSED, pending_close: true },
    STUCK,
    { ...PAUSED, episode_id: 540, recipient: { user_id: 96, name: 'Locked Mijoz', reminder_language: 'uz' }, pending_close: true, can_resolve: false },
  ];
  renderPage();

  await screen.findByText('Alisher Mijoz');
  const closing = 'Already reordered — closes at the next run';

  expect(rowFor('Alisher Mijoz')).toHaveTextContent(closing);
  // Resolve availability is the published `can_resolve`, whatever `pending_close` says.
  expect(within(rowFor('Alisher Mijoz')).getByRole('button', { name: 'Record outcome' })).toBeInTheDocument();
  expect(rowFor('Locked Mijoz')).toHaveTextContent(closing);
  expect(within(rowFor('Locked Mijoz')).queryByRole('button', { name: 'Record outcome' })).toBeNull();
  expect(rowFor('Dilshod Mijoz')).not.toHaveTextContent(closing);
});

it('renders a row whose outlets, reply excerpt and customer facts are null', async () => {
  pages.follow_up = [worklistRow({
    ...STUCK,
    episode_id: 550,
    recipient: { user_id: 97, name: null, reminder_language: null },
    escalation_flag: 'outlet_dormant',
    stuck_orders: [],
    outlets: null,
    reply_excerpt: null,
    setting: null,
    contact_restriction: null,
    last_delivery_at: '2026-08-20T07:00:00+00:00',
  })];
  renderPage();

  const nameless = await screen.findByText('#97');
  const row = nameless.closest('tr');
  expect(row).toHaveTextContent('Outlet details are unavailable');
  expect(row).not.toHaveTextContent('Customer replied');
  expect(within(row).getByRole('button', { name: 'Record outcome' })).toBeInTheDocument();
});

it('names each dormant outlet with its agent, or none', async () => {
  pages.follow_up = [worklistRow({
    ...STUCK,
    episode_id: 551,
    escalation_flag: 'outlet_dormant',
    stuck_orders: [],
    outlets: [
      { outlet_id: 31, name: 'Kiosk Chilonzor', dormant_since: '2026-09-01T00:00:00+00:00', agent: { user_id: 12, name: 'Agent Sherzod' } },
      { outlet_id: 32, name: 'Kiosk Yunusobod', dormant_since: null, agent: null },
    ],
  })];
  renderPage();

  const row = (await screen.findByText('Dilshod Mijoz')).closest('tr');

  expect(row).toHaveTextContent('Outlet Kiosk Chilonzor · dormant since Sep 01, 2026 · agent Agent Sherzod');
  expect(row).toHaveTextContent('Outlet Kiosk Yunusobod · dormant since — · agent none');
  expect(row).not.toHaveTextContent('Outlet details are unavailable');
});

it('quotes the customer reply and writes each history line from the published event', async () => {
  pages.follow_up = [worklistRow({
    ...STUCK,
    episode_id: 552,
    escalation_flag: 'replied',
    stuck_orders: [],
    reply_excerpt: 'Please call me tomorrow',
    history: [
      { kind: 'reminder_sent', step: 1, channel: 'email', detail: null, at: '2026-10-11T05:10:00+00:00' },
      { kind: 'reminder_failed', step: 2, channel: 'telegram', detail: 'telegram_502', at: '2026-10-12T05:10:00+00:00' },
      { kind: 'escalated', step: null, channel: null, detail: 'replied', at: '2026-10-13T05:10:00+00:00' },
    ],
    reminders_sent: 1,
    step: 2,
  })];
  renderPage();

  const row = (await screen.findByText('Dilshod Mijoz')).closest('tr');

  expect(row).toHaveTextContent('Customer replied: “Please call me tomorrow”');
  expect(row).toHaveTextContent('Oct 11, 2026 · reminder_sent · email');
  expect(row).toHaveTextContent('Oct 12, 2026 · reminder_failed · telegram · telegram_502');
  expect(row).toHaveTextContent('Oct 13, 2026 · escalated · replied');
  expect(row).toHaveTextContent('Reminders on · never changed');
});

it('labels the sent metric from the published row set, not from the mode', async () => {
  summaryMode = 'on';
  renderPage();

  const metrics = await screen.findByTestId('lapsed-metrics');
  expect(within(metrics).getByText('Reminders sent · Telegram')).toBeInTheDocument();
  expect(within(metrics).getByText('Reminders sent · Email')).toBeInTheDocument();
  expect(within(metrics).queryByText(/Reminders simulated/)).toBeNull();
});

it('labels the sent metric from row_set_is_dry_run even when the mode says on', async () => {
  summaryMode = 'on';
  summaryOverrides = { row_set_is_dry_run: true };
  renderPage();

  const metrics = await screen.findByTestId('lapsed-metrics');
  expect(within(metrics).getByText('Reminders simulated · Telegram')).toBeInTheDocument();
});

it('shows the off banner and the 30-day metrics', async () => {
  summaryMode = 'off';
  renderPage();

  expect(await screen.findByText('Off — no reminders are sent.')).toBeInTheDocument();
  const metrics = screen.getByTestId('lapsed-metrics');
  expect(within(metrics).getByText('Lapses started')).toBeInTheDocument();
  expect(metrics).toHaveTextContent('12');
  expect(metrics).toHaveTextContent('no_response: 4');
  expect(within(screen.getByTestId('lapsed-settings')).getByText('10:00')).toBeInTheDocument();
});

it('shows no mode banner when reminders are on', async () => {
  renderPage();
  await screen.findByTestId('lapsed-metrics');

  expect(screen.queryByText('Off — no reminders are sent.')).toBeNull();
  expect(screen.queryByText('Dry-run — simulated rows, discarded at go-live.')).toBeNull();
});

it('makes no request for an admin without the permission', async () => {
  mockAuth.hasPermission.mockReturnValue(false);
  renderPage();

  expect(await screen.findByText('Lapsed customers')).toBeInTheDocument();
  expect(api.get).not.toHaveBeenCalled();
  expect(mockAuth.hasPermission).toHaveBeenCalledWith('can_manage_lapsed_customers');
});
