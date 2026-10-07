import React from 'react';
import { render, screen, fireEvent, within, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import PayLinesTable from '../../../../components/sales/pay/PayLinesTable';
import salesPayService from '../../../../services/salesPayService';

vi.mock('../../../../services/salesPayService', async () => {
  const actual = await vi.importActual('../../../../services/salesPayService');
  const methods = Object.getOwnPropertyNames(Object.getPrototypeOf(actual.default)).filter((name) => name !== 'constructor');
  return { ...actual, default: Object.fromEntries(methods.map((name) => [name, vi.fn()])) };
});
vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key, opts) => {
      const text = (typeof opts === 'string' ? opts : opts?.defaultValue) || key;
      return opts && typeof opts === 'object'
        ? text.replace(/\{\{(\w+)\}\}/g, (_, name) => String(opts[name] ?? ''))
        : text;
    },
    i18n: { language: 'en' },
  }),
}));
const mockAuth = { hasPermission: vi.fn(() => true) };
vi.mock('../../../../stores/authStore', () => ({ useAuthStore: () => mockAuth }));

const LINE_KINDS = ['commission_credit', 'commission_reversal', 'commission_difference', 'new_outlet_bonus'];
const SETTLED_HINT = "Commission follows the money received and the units still on the order, never above the order's full share at its tiers.";

// A9 `AdminOrderRow` (§5.2, v5): one row per order, its ledger lines of this period as `events`.
// Every money figure below is distinct, so a label that read the wrong field would print a number
// that appears nowhere else. `follows_money` is the backend's published D-Q3 display rule
// (`pay_rules.follows_money`, ruling T14-R1); every event fixture carries it, as A9 sends it.
const event = (overrides) => ({
  id: 9001, kind: 'commission_credit', cause: null, is_recredit: false, follows_money: false, occurred_at: '2026-11-12T07:31:00+00:00',
  date: '2026-11-12', received: 240000.0, received_applied: 240000.0,
  ...overrides,
});
const orderRow = (overrides) => ({
  row: 'order', order_id: 412, order_number: 'SA_000412_26', outlet_name: 'Oasis market',
  earned_month: '2026-11', earned_date: '2026-11-12', date: '2026-11-12',
  is_late: false, counted: true, tier_shift: false,
  amount: 18000.0, share: 18000.0, share_before: null,
  products: [{
    product_id: 3, product_name: '19L', units: 12, net: 240000.0,
    tiers: [{ from_unit: 301, to_unit: null, units: 12, mode: 'per_unit', value: 1500.0, share: 18000.0 }],
    tiers_before: null,
  }],
  items: [{ product_id: 3, product_name: '19L', quantity: 12, unit_price: 20000.0, net: 240000.0 }],
  money: { received_ref: 240000.0, received: 240000.0, received_applied: 240000.0 },
  units_level: [{ product_id: 3, product_name: '19L', at_credit: 12, on_order: 12, counted: 12 }],
  events: [event({})],
  ...overrides,
});

// The own month's first credit: 12 units at 301+ (1,500), 18,000.
const FIRST_CREDIT = orderRow({});
// An October order partly refunded in November: the units stay, the money falls (I-32).
// 6 × 1,500 = 9,000 at full money; 9,000 × 90,000 / 120,000 = 6,750 now; −2,250 in November.
// Its product name is A8's snapshot shape, read in the admin's language.
const REDUCED = orderRow({
  order_id: 398, order_number: 'SA_000398_26', earned_month: '2026-10', earned_date: '2026-10-08', date: '2026-11-05', is_late: true,
  amount: -2250.0, share: 6750.0, share_before: 9000.0,
  products: [{
    product_id: 3, product_name: { en: '19L', uz: '19L', ru: '19 л' }, units: 6, net: 120000.0,
    tiers: [{ from_unit: 1, to_unit: 500, units: 6, mode: 'per_unit', value: 1500.0, share: 6750.0 }],
    tiers_before: [{ from_unit: 1, to_unit: 500, units: 6, mode: 'per_unit', value: 1500.0, share: 9000.0 }],
  }],
  items: [{ product_id: 3, product_name: '19L', quantity: 6, unit_price: 20000.0, net: 120000.0 }],
  money: { received_ref: 120000.0, received: 90000.0, received_applied: 90000.0 },
  units_level: [{ product_id: 3, product_name: '19L', at_credit: 6, on_order: 6, counted: 6 }],
  events: [event({
    id: 9212, kind: 'commission_difference', cause: 'received_reduced', follows_money: true,
    occurred_at: '2026-11-05T06:00:00+00:00', date: '2026-11-05', received: 90000.0, received_applied: 90000.0,
  })],
});
// Illustration C (b), November's drill-down (§5.2 sample): A left October's count, so B's 250
// units fell from 301–550 into 1–250. Its share moved without an event of its own (I-31):
// 200 × 1,000 + 50 × 1,500 = 275,000 before, 250 × 1,000 = 250,000 now.
const TIER_SHIFT = orderRow({
  order_id: 502, order_number: 'SA_000502_26', outlet_name: 'Baraka', earned_month: '2026-10', date: null, is_late: true, tier_shift: true,
  amount: -25000.0, share: 250000.0, share_before: 275000.0,
  products: [{
    product_id: 3, product_name: '19L', units: 250, net: 5000000.0,
    tiers: [{ from_unit: 1, to_unit: 500, units: 250, mode: 'per_unit', value: 1000.0, share: 250000.0 }],
    tiers_before: [
      { from_unit: 1, to_unit: 500, units: 200, mode: 'per_unit', value: 1000.0, share: 200000.0 },
      { from_unit: 501, to_unit: 1000, units: 50, mode: 'per_unit', value: 1500.0, share: 75000.0 },
    ],
  }],
  items: [{ product_id: 3, product_name: '19L', quantity: 250, unit_price: 20000.0, net: 5000000.0 }],
  money: { received_ref: 5000000.0, received: 5000000.0, received_applied: 5000000.0 },
  units_level: [{ product_id: 3, product_name: '19L', at_credit: 250, on_order: 250, counted: 250 }],
  events: [],
});
// T-TIER-20's removal (OQ-B3): 299 of the order's 300 bottles taken off after its credit; the
// juice on the same order kept its 4 units, so only 19L draws a units line. 300,000 → 1,000.
const UNITS_REDUCED = orderRow({
  order_id: 501, order_number: 'SA_000501_26', earned_month: '2026-10', date: '2026-11-05', is_late: true,
  amount: -299000.0, share: 1000.0, share_before: 300000.0,
  products: [{
    product_id: 3, product_name: '19L', units: 1, net: 20000.0,
    tiers: [{ from_unit: 1, to_unit: 500, units: 1, mode: 'per_unit', value: 1000.0, share: 1000.0 }],
    tiers_before: [{ from_unit: 1, to_unit: 500, units: 300, mode: 'per_unit', value: 1000.0, share: 300000.0 }],
  }],
  items: [
    { product_id: 3, product_name: '19L', quantity: 300, unit_price: 20000.0, net: 6000000.0 },
    { product_id: 9, product_name: 'Juice 1 L', quantity: 4, unit_price: 15000.0, net: 60000.0 },
  ],
  money: { received_ref: 6060000.0, received: 80000.0, received_applied: 80000.0 },
  units_level: [
    { product_id: 3, product_name: '19L', at_credit: 300, on_order: 1, counted: 1 },
    { product_id: 9, product_name: 'Juice 1 L', at_credit: 4, on_order: 4, counted: 4 },
  ],
  events: [event({
    id: 9230, kind: 'commission_difference', cause: 'units_reduced', follows_money: true,
    occurred_at: '2026-11-05T09:00:00+00:00', date: '2026-11-05', received: 80000.0, received_applied: 80000.0,
  })],
});
// An order no longer delivered: the money left and its units left the count (`counted` null).
const REVERSAL = orderRow({
  order_id: 402, order_number: 'SA_000402_26', earned_month: '2026-10', date: '2026-11-03', is_late: true,
  amount: -7000.0, share: 0.0, share_before: 7000.0,
  products: [{
    product_id: 3, product_name: '19L', units: 7, net: 140000.0, tiers: [],
    tiers_before: [{ from_unit: 1, to_unit: 500, units: 7, mode: 'per_unit', value: 1000.0, share: 7000.0 }],
  }],
  items: [{ product_id: 3, product_name: '19L', quantity: 7, unit_price: 20000.0, net: 140000.0 }],
  money: { received_ref: 140000.0, received: 0.0, received_applied: null },
  units_level: [{ product_id: 3, product_name: '19L', at_credit: 7, on_order: 7, counted: null }],
  events: [event({
    id: 9215, kind: 'commission_reversal', cause: 'not_delivered', follows_money: false,
    occurred_at: '2026-11-03T06:00:00+00:00', date: '2026-11-03', received: 0.0, received_applied: null,
  })],
});
const SHADOW = orderRow({ order_id: 403, order_number: 'SA_000403_26', earned_month: '2026-09', is_late: true, counted: false });
// `AdminBonusRow` (§5.2): v4's bonus line plus `row: "bonus"`.
const BONUS = {
  row: 'bonus', id: 9301, kind: 'new_outlet_bonus', cause: null, is_recredit: false, follows_money: false,
  earned_month: '2026-11', posted_month: '2026-11', is_late: false, counted: true, order_id: null, order_number: null,
  outlet_name: 'Nur market', occurred_at: '2026-11-20T06:00:00+00:00', date: '2026-11-20', amount: 100000.0, money: null, items: [],
};

const page = (items) => ({
  items, meta: { page: 1, per_page: 20, total: items.length, pages: 1, has_next: false, has_prev: false },
  kind: null, line_kinds: LINE_KINDS, as_of: '2026-11-06T02:00:00+00:00',
});

const renderTable = () => {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <PayLinesTable month="2026-11" agentId={41} lineKinds={LINE_KINDS} active />
    </QueryClientProvider>,
  );
};

const expand = async (orderNumber) => {
  const tableRow = (await screen.findByText(orderNumber)).closest('tr');
  fireEvent.click(within(tableRow).getByRole('button', { name: /expand row/i }));
  return tableRow.nextElementSibling;
};
// Every body row of the tables inside `element`, as its cells' text (header rows have no cells).
const cellsOf = (element) => within(element).getAllByRole('row')
  .map((tr) => within(tr).queryAllByRole('cell').map((cell) => cell.textContent))
  .filter((cells) => cells.length > 0);

beforeEach(() => {
  vi.clearAllMocks();
  mockAuth.hasPermission.mockReturnValue(true);
  salesPayService.getStatementLines.mockResolvedValue(
    page([FIRST_CREDIT, REDUCED, TIER_SHIFT, UNITS_REDUCED, REVERSAL, SHADOW, BONUS]),
  );
});

it('reads A9 for the drawer\'s month and agent, one page at a time', async () => {
  renderTable();

  await screen.findByText('SA_000412_26');
  expect(salesPayService.getStatementLines).toHaveBeenCalledWith('2026-11', 41, { kind: undefined, page: 1, perPage: 20 });
  expect(screen.getByText('SA_000398_26').closest('tr')).toHaveTextContent('late · for 10.2026');
  expect(screen.getByText('SA_000403_26').closest('tr')).toHaveTextContent('not counted (trial month)');
  expect(screen.getByText('SA_000412_26').closest('tr')).not.toHaveTextContent('late');
});

it('prints an order\'s units and signed amount, and share_before → share on a late row', async () => {
  renderTable();

  const own = (await screen.findByText('SA_000412_26')).closest('tr');
  expect(own).toHaveTextContent('2026-11-12');
  expect(own).toHaveTextContent('19L × 12');
  expect(own).toHaveTextContent('18,000');
  expect(own).toHaveTextContent('commission_credit');
  expect(own).not.toHaveTextContent('→');

  const late = screen.getByText('SA_000398_26').closest('tr');
  expect(late).toHaveTextContent('19L × 6');
  expect(late).toHaveTextContent('−2,250');
  expect(late).toHaveTextContent('9,000 → 6,750');
  expect(late).toHaveTextContent('commission_difference');
  expect(late).toHaveTextContent('received_reduced');
});

it('marks a tier shift with its tag and hint, and no date', async () => {
  renderTable();

  const shifted = (await screen.findByText('SA_000502_26')).closest('tr');
  expect(shifted.textContent).not.toMatch(/\d{4}-\d{2}-\d{2}/);
  expect(shifted).toHaveTextContent('−25,000');
  expect(shifted).toHaveTextContent('275,000 → 250,000');
  fireEvent.mouseEnter(within(shifted).getByText('Tier change'));
  expect(await screen.findByText(/^Another order of that month left or joined the count/)).toBeInTheDocument();
});

it('expands a tier shift into its segments now and before', async () => {
  renderTable();
  const expanded = await expand('SA_000502_26');

  expect(within(expanded).getByText('Before')).toBeInTheDocument();
  // Tier · Units · Rate · Share · Before. The tier the order left keeps its row, blank now.
  expect(cellsOf(expanded)).toContainEqual(['1–500', '250', '1,000 UZS/unit', '250,000', '200,000']);
  expect(cellsOf(expanded)).toContainEqual(['501–1,000', '', '1,500 UZS/unit', '', '75,000']);
});

it('explains a cash correction by the money, with the settled hint and no units line', async () => {
  renderTable();
  const expanded = await expand('SA_000398_26');

  expect(expanded).toHaveTextContent('Money received 90,000 (at credit 120,000) · counted 90,000');
  expect(expanded).toHaveTextContent(SETTLED_HINT);
  expect(expanded).not.toHaveTextContent('on the order (at credit');
  // The segments with the share before, the frozen first-credit item, and this period's event.
  expect(cellsOf(expanded)).toContainEqual(['1–500', '6', '1,500 UZS/unit', '6,750', '9,000']);
  expect(cellsOf(expanded)).toContainEqual(['19L', '6', '120,000']);
  expect(expanded).toHaveTextContent('2026-11-05 · commission_difference · received_reduced');
});

it('draws the units line only for a product whose counted units fell', async () => {
  renderTable();
  const expanded = await expand('SA_000501_26');

  expect(expanded).toHaveTextContent('19L: 1 on the order (at credit 300) · counted 1');
  expect(expanded).not.toHaveTextContent('Juice 1 L: 4 on the order');
  expect(expanded).toHaveTextContent(SETTLED_HINT);
  expect(expanded).toHaveTextContent('2026-11-05 · commission_difference · units_reduced');
});

it('explains a reversal by the money that left, with no units line for its uncounted units', async () => {
  renderTable();
  const expanded = await expand('SA_000402_26');

  expect(expanded).toHaveTextContent("Money received 140,000 → 0: the order's units leave the month's count");
  expect(expanded).toHaveTextContent('Money received 0 (at credit 140,000) · counted —');
  expect(expanded).not.toHaveTextContent('on the order (at credit');
  expect(expanded).not.toHaveTextContent('Commission follows the money received');
});

it('shows a first credit\'s money and segments, and no settled hint', async () => {
  renderTable();
  const expanded = await expand('SA_000412_26');

  expect(expanded).toHaveTextContent('Money received 240,000 (at credit 240,000) · counted 240,000');
  expect(cellsOf(expanded)).toContainEqual(['301+', '12', '1,500 UZS/unit', '18,000']);
  expect(within(expanded).queryByText('Before')).toBeNull();
  expect(expanded).not.toHaveTextContent('Commission follows the money received');
  expect(expanded).not.toHaveTextContent('→');
});

it('draws the settled hint on the published follows_money, never on the cause', async () => {
  // A cause the page has never heard of that follows the money (the backend says so), and a
  // reduction the backend says does not: the hint obeys the field both times.
  const NEW_CAUSE = orderRow({
    order_id: 406, order_number: 'SA_000406_26',
    events: [event({ id: 9219, kind: 'commission_difference', cause: 'received_rebooked', follows_money: true })],
  });
  const NOT_FOLLOWING = orderRow({
    order_id: 407, order_number: 'SA_000407_26',
    events: [event({ id: 9220, kind: 'commission_difference', cause: 'received_reduced', follows_money: false })],
  });
  salesPayService.getStatementLines.mockResolvedValue(page([NEW_CAUSE, NOT_FOLLOWING]));
  renderTable();

  expect(await expand('SA_000406_26')).toHaveTextContent(SETTLED_HINT);
  expect(await expand('SA_000407_26')).not.toHaveTextContent('Commission follows the money received');
});

it('reads a bonus row\'s kind and amount and does not expand it', async () => {
  renderTable();

  const bonus = (await screen.findByText('Nur market')).closest('tr');
  expect(bonus).toHaveTextContent('2026-11-20');
  expect(bonus).toHaveTextContent('new_outlet_bonus');
  expect(bonus).toHaveTextContent('100,000');
  // antd keeps a hidden spacer where the expand button would be; no admin can reach it.
  expect(within(bonus).queryByRole('button', { name: /expand row/i })).toBeNull();
});

it('filters by a kind the route published', async () => {
  renderTable();
  await screen.findByText('SA_000398_26');

  // The option label is `t('pay.kind.<kind>', kind)`: this mock has no seed, so it reads the
  // published value itself — which is the point: the options are the route's `line_kinds`.
  fireEvent.mouseDown(screen.getByRole('combobox'));
  fireEvent.click(await screen.findByTitle('commission_reversal'));

  await waitFor(() => expect(salesPayService.getStatementLines).toHaveBeenLastCalledWith(
    '2026-11', 41, { kind: 'commission_reversal', page: 1, perPage: 20 },
  ));
});

it('fires nothing for a viewer without the pay permission', async () => {
  mockAuth.hasPermission.mockReturnValue(false);
  renderTable();

  await new Promise((resolve) => { setTimeout(resolve, 0); });
  expect(salesPayService.getStatementLines).not.toHaveBeenCalled();
});
