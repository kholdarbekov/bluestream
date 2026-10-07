import React from 'react';
import { render, screen, fireEvent, within, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import PayPlansTab from '../../../../components/sales/pay/PayPlansTab';
import salesPayService from '../../../../services/salesPayService';

vi.mock('../../../../services/salesPayService', async () => {
  const actual = await vi.importActual('../../../../services/salesPayService');
  const methods = Object.getOwnPropertyNames(Object.getPrototypeOf(actual.default)).filter((name) => name !== 'constructor');
  return { ...actual, default: Object.fromEntries(methods.map((name) => [name, vi.fn()])) };
});
vi.mock('../../../../services/adminService');
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

const historyRow = (overrides) => ({
  created_at: '2026-09-28T06:00:00+00:00', created_by: { id: 1, name: 'Admin User' }, note: null, ...overrides,
});
// A12 (§5.2) as `_coverage` publishes it: each version's months and the plan's timeline, newest
// version first. v4 is replaced within September by v5, v5 gives way to v3 in October, v3 runs
// to November and v6 from December on: the four shapes "Applies to" words.
const PLAN = {
  id: 1, name: 'Standard',
  version_in_force: { id: 13, version_no: 3, effective_month: '2026-10' },
  timeline: [
    { from_month: '2026-09', version_id: 15, version_no: 5 },
    { from_month: '2026-10', version_id: 13, version_no: 3 },
    { from_month: '2026-12', version_id: 16, version_no: 6 },
  ],
  versions: [
    historyRow({ id: 16, version_no: 6, effective_month: '2026-12', applies_from: '2026-12', applies_until: null, replaced_by_version_no: null }),
    historyRow({
      id: 15, version_no: 5, effective_month: '2026-09', applies_from: '2026-09', applies_until: '2026-09', replaced_by_version_no: null, note: '10 L tiers',
    }),
    historyRow({ id: 14, version_no: 4, effective_month: '2026-09', applies_from: null, applies_until: null, replaced_by_version_no: 5 }),
    historyRow({ id: 13, version_no: 3, effective_month: '2026-10', applies_from: '2026-10', applies_until: '2026-11', replaced_by_version_no: null }),
  ],
};
const PLANS = {
  items: [PLAN], rate_modes: ['per_unit', 'percent'], bonus_rules: ['orders_with_total', 'orders_any'],
  editable_from_month: '2026-09', current_month: '2026-10',
  default_config: {
    gate_bands: [{ min_pct: 80.0, multiplier: 1.0 }, { min_pct: 60.0, multiplier: 0.8 }, { min_pct: 0.0, multiplier: 0.5 }],
    gate_min_visits_due: 20, bonus_window_days: 60, bonus_min_orders_with_total: 2,
    bonus_min_orders_any_amount: 5, bonus_prior_customer_lookback_days: 180,
  },
};
// A14 `VersionDetail` for v5: the owner's 10 L tiers on an inactive product.
const V5_DETAIL = {
  id: 15, plan_id: 1, plan_name: 'Standard', version_no: 5, effective_month: '2026-09',
  default_tiers: [{ from_unit: 1, to_unit: null, mode: 'percent', value: 3.0 }],
  rates: [
    {
      product_id: 3, product_name: 'Pure Water 19L', product_is_active: true,
      tiers: [{ from_unit: 1, to_unit: null, mode: 'per_unit', value: 1000.0 }],
    },
    {
      product_id: 5, product_name: 'Water 10L', product_is_active: false,
      tiers: [
        { from_unit: 1, to_unit: 500, mode: 'per_unit', value: 300.0 },
        { from_unit: 501, to_unit: 1000, mode: 'per_unit', value: 400.0 },
        { from_unit: 1001, to_unit: null, mode: 'per_unit', value: 500.0 },
      ],
    },
  ],
  gate_bands: [{ min_pct: 80.0, multiplier: 1.0 }, { min_pct: 60.0, multiplier: 0.8 }, { min_pct: 0.0, multiplier: 0.5 }],
  gate_min_visits_due: 20,
  new_outlet_bonus: {
    amount: 150000.0, window_days: 60, prior_customer_lookback_days: 180,
    min_orders_with_total: 2, min_combined_total: 300000.0, min_orders_any_amount: 7,
  },
  note: '10 L tiers', created_at: '2026-09-28T06:00:00+00:00', created_by: { id: 1, name: 'Admin User' },
};

const renderTab = () => {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <PayPlansTab active />
    </QueryClientProvider>,
  );
};
// Expands the plan into its version history and returns the history table's body rows, in order.
const openHistory = async () => {
  fireEvent.click(await screen.findByRole('button', { name: 'Expand row' }));
  const history = (await screen.findByText('10 L tiers')).closest('table');
  return within(history).getAllByRole('row').slice(1);
};
const versionRow = (rows, versionNo) => rows.find((row) => row.cells[0].textContent === String(versionNo));
// A history row's cell under the column headed `header` (undefined when there is no such column).
const cellUnder = (row, header) => {
  const table = row.closest('table');
  const index = within(table).getAllByRole('columnheader').findIndex((cell) => cell.textContent === header);
  return row.cells[index]?.textContent;
};
// A small table's body rows, each as its cells' text.
const bodyRows = (scope) => within(scope).getAllByRole('row').slice(1)
  .map((row) => within(row).getAllByRole('cell').map((cell) => cell.textContent));
// A bordered Descriptions' items, label -> value.
const described = (scope) => Object.fromEntries(within(scope).getAllByRole('row')
  .map((row) => [row.querySelector('th').textContent, row.querySelector('td').textContent]));

beforeEach(() => {
  vi.clearAllMocks();
  mockAuth.hasPermission.mockReturnValue(true);
  salesPayService.getPlans.mockResolvedValue(PLANS);
  salesPayService.getPlanVersion.mockResolvedValue({ version: V5_DETAIL });
});

it('words the months each version applies to from the published fields', async () => {
  renderTab();
  const rows = await openHistory();

  expect(rows.map((row) => row.cells[0].textContent)).toStrictEqual(['6', '5', '4', '3']);
  expect(rows.map((row) => cellUnder(row, 'Applies to'))).toStrictEqual([
    'from 12.2026', '09.2026 only', 'Replaced by v5', '10.2026 – 11.2026',
  ]);
  expect(rows.map((row) => cellUnder(row, 'Effective month'))).toStrictEqual(['12.2026', '09.2026', '09.2026', '10.2026']);
});

it('views a version read-only: its months, every schedule, the bands, the bonus and the note', async () => {
  renderTab();
  const rows = await openHistory();

  fireEvent.click(within(versionRow(rows, 5)).getByRole('button', { name: 'View' }));

  await waitFor(() => expect(salesPayService.getPlanVersion).toHaveBeenCalledWith(1, 15));
  const drawer = await screen.findByRole('dialog');
  expect(within(drawer).getByText('Standard · version 5')).toBeInTheDocument();
  await within(drawer).findByTestId('view-default-tiers');
  expect(described(within(drawer).getByTestId('view-months'))).toStrictEqual({
    'Effective month': '09.2026', 'Applies to': '09.2026 only',
  });
  expect(bodyRows(within(drawer).getByTestId('view-default-tiers'))).toStrictEqual([['1+', '3 %']]);
  const water19 = within(drawer).getByTestId('view-product-3');
  expect(within(water19).getByText('Pure Water 19L')).toBeInTheDocument();
  expect(within(water19).queryByText('(inactive)')).toBeNull();
  expect(bodyRows(water19)).toStrictEqual([['1+', '1,000 UZS/unit']]);
  const water10 = within(drawer).getByTestId('view-product-5');
  expect(within(water10).getByText('Water 10L')).toBeInTheDocument();
  expect(within(water10).getByText('(inactive)')).toBeInTheDocument();
  expect(bodyRows(water10)).toStrictEqual([
    ['1–500', '300 UZS/unit'], ['501–1,000', '400 UZS/unit'], ['1,001+', '500 UZS/unit'],
  ]);
  const bands = within(drawer).getByTestId('view-bands');
  expect(within(bands).getAllByRole('columnheader').map((cell) => cell.textContent)).toStrictEqual(['From %', 'Multiplier']);
  expect(bodyRows(bands)).toStrictEqual([['80', '1'], ['60', '0.8'], ['0', '0.5']]);
  expect(described(within(drawer).getByTestId('view-gate'))).toStrictEqual({ 'Min visits due': '20' });
  expect(described(within(drawer).getByTestId('view-bonus'))).toStrictEqual({
    'New-outlet bonus': '150,000',
    'Window (days)': '60',
    'Prior-customer lookback (days)': '180',
    'Orders with total': '2',
    'Min combined total': '300,000',
    'Orders of any amount': '7',
  });
  expect(described(within(drawer).getByTestId('view-note'))).toStrictEqual({ Note: '10 L tiers' });
  // Read-only: nothing in the view can be typed into or saved.
  expect(within(drawer).queryByRole('textbox')).toBeNull();
  expect(within(drawer).queryByRole('button', { name: 'Save' })).toBeNull();
});

it('fires no plan read for a viewer without can_manage_sales_pay', async () => {
  mockAuth.hasPermission.mockReturnValue(false);
  renderTab();

  await new Promise((resolve) => { setTimeout(resolve, 0); });
  expect(salesPayService.getPlans).not.toHaveBeenCalled();
  expect(salesPayService.getPlanVersion).not.toHaveBeenCalled();
});
