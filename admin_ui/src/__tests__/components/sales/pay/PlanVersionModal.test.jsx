import React from 'react';
import { render, screen, fireEvent, within, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { message } from 'antd';
import dayjs from 'dayjs';

import PlanVersionModal, { versionPayload } from '../../../../components/sales/pay/PlanVersionModal';
import salesPayService from '../../../../services/salesPayService';
import adminService from '../../../../services/adminService';

vi.mock('../../../../services/salesPayService', async () => {
  const actual = await vi.importActual('../../../../services/salesPayService');
  const methods = Object.getOwnPropertyNames(Object.getPrototypeOf(actual.default)).filter((name) => name !== 'constructor');
  return { ...actual, default: Object.fromEntries(methods.map((name) => [name, vi.fn()])) };
});
vi.mock('../../../../services/adminService');
// The refusal's sentence as seeded (scripts/seed_ui_sales_translations.py), so the test reads what
// an admin reads, with `details` interpolated.
const mockSeeded = {
  'sales_agents:pay.error.sales_pay_plan_invalid': 'The plan is not valid: {{field}} ({{reason}}).',
};
vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key, opts) => {
      const text = mockSeeded[key] || (typeof opts === 'string' ? opts : opts?.defaultValue) || key;
      return opts && typeof opts === 'object'
        ? text.replace(/\{\{(\w+)\}\}/g, (_, name) => String(opts[name] ?? ''))
        : text;
    },
    i18n: { language: 'en' },
  }),
}));
vi.mock('antd', async () => {
  const actual = await vi.importActual('antd');
  return { ...actual, message: { success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn(), loading: vi.fn(), destroy: vi.fn(), open: vi.fn() } };
});
const mockAuth = { hasPermission: vi.fn(() => true) };
vi.mock('../../../../stores/authStore', () => ({ useAuthStore: () => mockAuth }));

const BANDS = [{ min_pct: 80.0, multiplier: 1.0 }, { min_pct: 60.0, multiplier: 0.8 }, { min_pct: 0.0, multiplier: 0.5 }];
const POSTED_BANDS = [{ min_pct: 80, multiplier: 1 }, { min_pct: 60, multiplier: 0.8 }, { min_pct: 0, multiplier: 0.5 }];
// A12 (§5.2, v5): `default_config` carries no rate key; tiers have no default (§4.10).
const CONFIG = {
  items: [], rate_modes: ['per_unit', 'percent'], bonus_rules: ['orders_with_total', 'orders_any'],
  editable_from_month: '2026-11', current_month: '2026-11',
  default_config: {
    gate_bands: BANDS, gate_min_visits_due: 20, bonus_window_days: 60, bonus_min_orders_with_total: 2,
    bonus_min_orders_any_amount: 5, bonus_prior_customer_lookback_days: 180,
  },
};
const PLAN = {
  id: 1, name: 'Standard', version_in_force: { id: 3, version_no: 1, effective_month: '2026-10' },
  timeline: [{ from_month: '2026-10', version_id: 3, version_no: 1 }], versions: [],
};
// The dev shape behind the 2026-10-07 report: September's pay period is still open, so A12's first
// editable month is earlier than the current one, and September's version (v5) gives way to v3
// from October.
const SEPTEMBER_OPEN = { ...CONFIG, editable_from_month: '2026-09', current_month: '2026-10' };
const SHADOWING_PLAN = {
  ...PLAN,
  version_in_force: { id: 3, version_no: 3, effective_month: '2026-10' },
  timeline: [{ from_month: '2026-09', version_id: 5, version_no: 5 }, { from_month: '2026-10', version_id: 3, version_no: 3 }],
};
// A14 `VersionDetail` (Task 2's pinned echo): ascending tiers, each with its derived `to_unit`.
const VERSION_DETAIL = {
  id: 3, plan_id: 1, plan_name: 'Standard', version_no: 1, effective_month: '2026-10',
  default_tiers: [{ from_unit: 1, to_unit: null, mode: 'percent', value: 3.0 }],
  rates: [{
    product_id: 3, product_name: 'Pure Water 19L', product_is_active: true,
    tiers: [{ from_unit: 1, to_unit: 300, mode: 'per_unit', value: 1000.0 }, { from_unit: 301, to_unit: null, mode: 'per_unit', value: 1500.0 }],
  }],
  gate_bands: BANDS, gate_min_visits_due: 20,
  new_outlet_bonus: {
    amount: 150000.0, window_days: 60, prior_customer_lookback_days: 180,
    min_orders_with_total: 2, min_combined_total: 300000.0, min_orders_any_amount: 5,
  },
  note: 'launch', created_at: '2026-09-28T06:00:00+00:00', created_by: { id: 1, name: 'Admin User' },
};
const POSTED_BONUS = {
  amount: 150000, window_days: 60, prior_customer_lookback_days: 180,
  min_orders_with_total: 2, min_combined_total: 300000, min_orders_any_amount: 5,
};

const refusal = (details) => ({
  response: { status: 400, data: { success: false, message: 'SALES_PAY_PLAN_INVALID (backend sentence)', error_code: 'SALES_PAY_PLAN_INVALID', details } },
});

const renderModal = (props) => {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  const onClose = vi.fn();
  render(
    <QueryClientProvider client={queryClient}>
      <PlanVersionModal config={CONFIG} onClose={onClose} {...props} />
    </QueryClientProvider>,
  );
  return { onClose };
};
// "New version": the form mounts once A14 has answered.
const openVersion = async () => {
  renderModal({ mode: 'version', plan: PLAN });
  const dialog = await screen.findByRole('dialog');
  await within(dialog).findByTestId('default-tiers');
  return dialog;
};
const block = (dialog, testId, index = 0) => within(within(dialog).getAllByTestId(testId)[index]);
const tierRows = (scope) => scope.getAllByTestId('tier-row');
const type = (input, text) => fireEvent.change(input, { target: { value: text } });
const save = (dialog) => fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }));
// The fields a new plan needs before it saves: its name, the default tier's rate and the bonus.
const fillNewPlan = (dialog) => {
  type(within(dialog).getByLabelText('Plan name'), 'Standard');
  type(block(dialog, 'default-tiers').getByLabelText('Value'), '1 500');
  type(within(dialog).getByLabelText('New-outlet bonus'), '100 000');
  type(within(dialog).getByLabelText('Min combined total'), '300 000');
};
// Opens the effective-month picker and clicks a month: rc-picker titles each month cell "YYYY-MM".
const pickMonth = async (dialog, month) => {
  const input = within(dialog).getByLabelText('Effective month');
  fireEvent.mouseDown(input);
  fireEvent.click(input);
  await waitFor(() => expect(document.querySelector(`.ant-picker-dropdown td[title="${month}"]`)).not.toBeNull());
  fireEvent.click(document.querySelector(`.ant-picker-dropdown td[title="${month}"]`));
};

beforeEach(() => {
  vi.clearAllMocks();
  mockAuth.hasPermission.mockReturnValue(true);
  salesPayService.getPlanVersion.mockResolvedValue({ version: VERSION_DETAIL });
  salesPayService.createPlan.mockResolvedValue({ plan: PLAN });
  salesPayService.createPlanVersion.mockResolvedValue({ version: VERSION_DETAIL });
  adminService.getProducts.mockResolvedValue({
    success: true,
    data: { items: [{ id: 3, name: 'Pure Water 19L', is_active: true }, { id: 5, name: 'Water 10L', is_active: false }, { id: 9, name: 'Juice 1 L', is_active: true }] },
  });
});

it('creates a plan with a default tier and a product block, posting exactly the tier shape', async () => {
  const { onClose } = renderModal({ mode: 'plan', plan: null });
  const dialog = await screen.findByRole('dialog');

  // A new schedule starts with one tier from unit 1, in the first published mode, its rate empty.
  const defaults = block(dialog, 'default-tiers');
  expect(tierRows(defaults)).toHaveLength(1);
  expect(defaults.getByLabelText('From unit')).toHaveValue('1');
  type(within(dialog).getByLabelText('Plan name'), 'Standard');
  type(defaults.getByLabelText('Value'), '1 500');

  fireEvent.click(within(dialog).getByRole('button', { name: /Add product/ }));
  const juice = block(dialog, 'product-tiers');
  fireEvent.mouseDown(juice.getByRole('combobox', { name: 'Product' }));
  fireEvent.click(await screen.findByTitle('Juice 1 L'));
  expect(tierRows(juice)).toHaveLength(1);
  expect(juice.getByLabelText('From unit')).toHaveValue('1');
  type(juice.getByLabelText('Value'), '2 000');
  type(within(dialog).getByLabelText('New-outlet bonus'), '100 000');
  type(within(dialog).getByLabelText('Min combined total'), '300 000');
  save(dialog);

  await waitFor(() => expect(salesPayService.createPlan).toHaveBeenCalledTimes(1));
  expect(salesPayService.createPlan.mock.calls[0]).toStrictEqual([{
    name: 'Standard',
    version: {
      effective_month: '2026-11',
      default_tiers: [{ from_unit: 1, mode: 'per_unit', value: 1500 }],
      rates: [{ product_id: 9, tiers: [{ from_unit: 1, mode: 'per_unit', value: 2000 }] }],
      gate_bands: POSTED_BANDS,
      gate_min_visits_due: 20,
      new_outlet_bonus: { ...POSTED_BONUS, amount: 100000 },
      note: null,
    },
  }]);
  await waitFor(() => expect(onClose).toHaveBeenCalled());
});

it('posts a new version pre-filled from A14 without to_unit, showing a range only while its rows match what A14 published', async () => {
  const dialog = await openVersion();

  // A14's published ranges sit beside the rows they came with, read-only, on open.
  const water = block(dialog, 'product-tiers');
  expect(within(tierRows(water)[0]).getByText('1–300')).toBeInTheDocument();
  expect(within(tierRows(water)[1]).getByText('301+')).toBeInTheDocument();
  const defaults = block(dialog, 'default-tiers');
  expect(within(tierRows(defaults)[0]).getByText('1+')).toBeInTheDocument();

  // V5-T5-R2: an added row changes this schedule's from_unit list, so EVERY range in it hides —
  // the screen never shows a bound that no longer matches what it is editing (§6.2, D-BANDS).
  fireEvent.click(defaults.getByRole('button', { name: /Add tier/ }));
  let added = within(tierRows(defaults)[1]);
  type(added.getByLabelText('From unit'), '501');
  type(added.getByLabelText('Value'), '2');
  expect(within(dialog).queryByText('1+')).toBeNull();
  expect(within(dialog).queryByText(/^501(\+|–)/)).toBeNull();
  // A different schedule (the product's) is untouched and keeps its own ranges.
  expect(within(tierRows(water)[0]).getByText('1–300')).toBeInTheDocument();

  // Removing the added row returns the rows to exactly the pre-filled set: the range is back.
  fireEvent.click(within(tierRows(defaults)[1]).getByLabelText('Remove'));
  expect(within(tierRows(defaults)[0]).getByText('1+')).toBeInTheDocument();

  // Re-adding it (the edit this admin will actually submit) hides it again.
  fireEvent.click(defaults.getByRole('button', { name: /Add tier/ }));
  added = within(tierRows(defaults)[1]);
  type(added.getByLabelText('From unit'), '501');
  type(added.getByLabelText('Value'), '2');
  expect(within(dialog).queryByText('1+')).toBeNull();

  // Editing an existing row's from_unit hides its schedule's ranges too, and typing it back to
  // the pre-filled value restores them.
  type(within(tierRows(water)[1]).getByLabelText('From unit'), '401');
  expect(within(dialog).queryByText('1–300')).toBeNull();
  expect(within(dialog).queryByText(/^401(\+|–)/)).toBeNull();
  type(within(tierRows(water)[1]).getByLabelText('From unit'), '301');
  expect(within(tierRows(water)[0]).getByText('1–300')).toBeInTheDocument();
  expect(within(tierRows(water)[1]).getByText('301+')).toBeInTheDocument();

  save(dialog);

  await waitFor(() => expect(salesPayService.createPlanVersion).toHaveBeenCalledTimes(1));
  const [planId, body] = salesPayService.createPlanVersion.mock.calls[0];
  expect(planId).toBe(1);
  // "Add tier" copied the previous row's mode (percent).
  expect(body.default_tiers).toStrictEqual([{ from_unit: 1, mode: 'percent', value: 3 }, { from_unit: 501, mode: 'percent', value: 2 }]);
  expect(body.rates).toStrictEqual([
    { product_id: 3, tiers: [{ from_unit: 1, mode: 'per_unit', value: 1000 }, { from_unit: 301, mode: 'per_unit', value: 1500 }] },
  ]);
  expect(JSON.stringify(body)).not.toMatch(/to_unit|published_range|default_rate/);
});

// 2026-10-07: the effective month starts at the later of A12's `current_month` and
// `editable_from_month`, so a version saved without a look at the picker starts this month, not
// in an earlier open month a later version already replaces.
it.each([
  ['plan', null, 'createPlan', ([body]) => body.version],
  ['version', SHADOWING_PLAN, 'createPlanVersion', ([, body]) => body],
])('starts a new %s at the current month while an earlier month is still open', async (mode, plan, method, posted) => {
  renderModal({ mode, plan, config: SEPTEMBER_OPEN });
  const dialog = await screen.findByRole('dialog');
  await within(dialog).findByTestId('default-tiers');

  expect(within(dialog).getByLabelText('Effective month')).toHaveValue('10.2026');
  if (mode === 'plan') fillNewPlan(dialog);
  save(dialog);

  await waitFor(() => expect(salesPayService[method]).toHaveBeenCalledTimes(1));
  expect(posted(salesPayService[method].mock.calls[0]).effective_month).toBe('2026-10');
});

it.each([
  [
    'the month just before the next entry',
    SHADOWING_PLAN.timeline,
    '2026-09',
    'This version will apply only to 09.2026. Version 3 takes over from 10.2026. To change the plan from 10.2026 on, pick 10.2026 or later.',
  ],
  [
    'a span of months before the next entry',
    [{ from_month: '2026-09', version_id: 5, version_no: 5 }, { from_month: '2026-12', version_id: 7, version_no: 7 }],
    '2026-09',
    'This version will apply only to 09.2026 – 11.2026. Version 7 takes over from 12.2026. To change the plan from 12.2026 on, pick 12.2026 or later.',
  ],
  [
    'the first later entry, not the last',
    [...SHADOWING_PLAN.timeline, { from_month: '2026-12', version_id: 7, version_no: 7 }],
    '2026-09',
    'This version will apply only to 09.2026. Version 3 takes over from 10.2026. To change the plan from 10.2026 on, pick 10.2026 or later.',
  ],
  // `null`: the picker is left at its default month (A12's `current_month`, 2026-10).
  [
    'the default month itself, before the picker is touched',
    [{ from_month: '2026-09', version_id: 5, version_no: 5 }, { from_month: '2026-12', version_id: 6, version_no: 6 }],
    null,
    'This version will apply only to 10.2026 – 11.2026. Version 6 takes over from 12.2026. To change the plan from 12.2026 on, pick 12.2026 or later.',
  ],
  [
    'across a year boundary',
    [...SHADOWING_PLAN.timeline, { from_month: '2027-01', version_id: 8, version_no: 8 }],
    '2026-12',
    'This version will apply only to 12.2026. Version 8 takes over from 01.2027. To change the plan from 01.2027 on, pick 01.2027 or later.',
  ],
])('warns under the picker when a later version takes over: %s', async (_label, timeline, picked, sentence) => {
  renderModal({ mode: 'version', plan: { ...SHADOWING_PLAN, timeline }, config: SEPTEMBER_OPEN });
  const dialog = await screen.findByRole('dialog');
  await within(dialog).findByTestId('default-tiers');

  if (picked) await pickMonth(dialog, picked);

  expect(within(dialog).getByTestId('month-shadowed').textContent).toBe(sentence);
  // The warning informs and never blocks: the month in the picker is the month saved.
  save(dialog);
  await waitFor(() => expect(salesPayService.createPlanVersion).toHaveBeenCalledTimes(1));
  expect(salesPayService.createPlanVersion.mock.calls[0][1].effective_month).toBe(picked || SEPTEMBER_OPEN.current_month);
});

it('says nothing at or after the last timeline entry, and posts the month picked', async () => {
  renderModal({ mode: 'version', plan: SHADOWING_PLAN, config: SEPTEMBER_OPEN });
  const dialog = await screen.findByRole('dialog');
  await within(dialog).findByTestId('default-tiers');
  // 10.2026 is v3's own month: nothing takes over after it.
  expect(within(dialog).queryByTestId('month-shadowed')).toBeNull();

  await pickMonth(dialog, '2026-12');

  expect(within(dialog).getByLabelText('Effective month')).toHaveValue('12.2026');
  expect(within(dialog).queryByTestId('month-shadowed')).toBeNull();
  save(dialog);
  await waitFor(() => expect(salesPayService.createPlanVersion).toHaveBeenCalledTimes(1));
  expect(salesPayService.createPlanVersion.mock.calls[0][1].effective_month).toBe('2026-12');
});

it('never warns about a new plan: it has no versions to give way to', async () => {
  renderModal({ mode: 'plan', plan: null, config: SEPTEMBER_OPEN });
  const dialog = await screen.findByRole('dialog');

  await pickMonth(dialog, '2026-09');

  expect(within(dialog).getByLabelText('Effective month')).toHaveValue('09.2026');
  expect(within(dialog).queryByTestId('month-shadowed')).toBeNull();
});

// Review Focus 4: tier values typed the way people type them. A percent typed with a ru/uz comma
// is two and a half, never 25; a grouped per-unit value and a grouped first unit are whole numbers.
it.each([
  ['1 500', '2,5'],
  ['1,500', '2.5'],
])('test_review_focus_4: per-unit %j, percent %j and from unit "1 001" post exact numbers', async (perUnit, percent) => {
  const dialog = await openVersion();

  type(block(dialog, 'default-tiers').getByLabelText('Value'), percent);
  const water = block(dialog, 'product-tiers');
  fireEvent.click(water.getByRole('button', { name: /Add tier/ }));
  const added = within(tierRows(water)[2]);
  type(added.getByLabelText('From unit'), '1 001');
  type(added.getByLabelText('Value'), perUnit);
  save(dialog);

  await waitFor(() => expect(salesPayService.createPlanVersion).toHaveBeenCalledTimes(1));
  expect(salesPayService.createPlanVersion.mock.calls[0]).toStrictEqual([1, {
    effective_month: '2026-11',
    default_tiers: [{ from_unit: 1, mode: 'percent', value: 2.5 }],
    rates: [{
      product_id: 3,
      tiers: [
        { from_unit: 1, mode: 'per_unit', value: 1000 },
        { from_unit: 301, mode: 'per_unit', value: 1500 },
        { from_unit: 1001, mode: 'per_unit', value: 1500 },
      ],
    }],
    gate_bands: POSTED_BANDS,
    gate_min_visits_due: 20,
    new_outlet_bonus: POSTED_BONUS,
    note: null,
  }]);
});

it.each([
  [
    'a product tier',
    { field: 'rates.tiers.from_unit', reason: 'not_increasing', tier: 3, product_id: 3 },
    'The plan is not valid: rates.tiers.from_unit (not_increasing). — tier 3 · Pure Water 19L',
  ],
  [
    'a default tier',
    { field: 'default_tiers.from_unit', reason: 'first_not_one', tier: 1 },
    'The plan is not valid: default_tiers.from_unit (first_not_one). — tier 1',
  ],
  // Review Focus 4's refusal half: tier 2 of the 19L schedule, by the name the editor lists.
  [
    'test_review_focus_4: a per-unit value on tier 2',
    { field: 'rates.tiers.value', reason: 'not_whole', tier: 2, product_id: 3 },
    'The plan is not valid: rates.tiers.value (not_whole). — tier 2 · Pure Water 19L',
  ],
  // F1 (final-review v5): a product-schedule refusal with no tier position still names the product.
  [
    'a product schedule with too many tiers',
    { field: 'rates.tiers', reason: 'too_many', product_id: 3 },
    'The plan is not valid: rates.tiers (too_many). — Pure Water 19L',
  ],
])('explains %s inline, naming the tier and its product, with no toast', async (_label, details, sentence) => {
  salesPayService.createPlanVersion.mockRejectedValue(refusal(details));
  const dialog = await openVersion();
  save(dialog);

  const alert = await within(dialog).findByTestId('pay-error');
  expect(alert.textContent).toBe(sentence);
  expect(within(dialog).getAllByTestId('pay-error')).toHaveLength(1);
  expect(message.error).not.toHaveBeenCalled();
});

it('builds the version payload from lower bounds only', () => {
  const values = {
    effective_month: dayjs('2026-11-01'),
    default_tiers: [{ from_unit: 1, mode: 'percent', value: 3, published_range: '1+' }],
    rates: [{ product_id: 3, tiers: [{ from_unit: 1, to_unit: 300, mode: 'per_unit', value: 1000, published_range: '1–300' }] }],
    gate_bands: BANDS,
    gate_min_visits_due: 20,
    new_outlet_bonus: POSTED_BONUS,
    note: '',
  };

  expect(versionPayload(values)).toStrictEqual({
    effective_month: '2026-11',
    default_tiers: [{ from_unit: 1, mode: 'percent', value: 3 }],
    rates: [{ product_id: 3, tiers: [{ from_unit: 1, mode: 'per_unit', value: 1000 }] }],
    gate_bands: POSTED_BANDS,
    gate_min_visits_due: 20,
    new_outlet_bonus: POSTED_BONUS,
    note: null,
  });
});
