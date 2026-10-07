import React from 'react';
import { render, screen, fireEvent, within, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { message, Modal } from 'antd';

import SalesCompensation from '../../pages/SalesCompensation';
import salesPayService from '../../services/salesPayService';
import staffService from '../../services/staffService';
import adminService from '../../services/adminService';

vi.mock('../../services/salesPayService', async () => {
  const actual = await vi.importActual('../../services/salesPayService');
  const methods = Object.getOwnPropertyNames(Object.getPrototypeOf(actual.default)).filter((name) => name !== 'constructor');
  return { ...actual, default: Object.fromEntries(methods.map((name) => [name, vi.fn()])) };
});
vi.mock('../../services/staffService');
vi.mock('../../services/adminService');
// Rows whose key the page builds from a published value (`pay.flag.${key}`, a refusal code): this
// mock has no seed, so the few sentences a test reads are seeded here, in their English wording.
const mockSeeded = {
  'sales_agents:pay.error.sales_pay_terms_missing': 'Set pay terms first for: {{agents}}.',
  'sales_agents:pay.flag.dedupe_forced': 'forced duplicate',
  'sales_agents:pay.flag.nearby_other_accounts': '{{count}} nearby orders by other accounts',
  'sales_agents:pay.flag.all_orders_placed_by_onboarder': 'all orders placed by the agent',
  'sales_agents:pay.flag.delivered_by_onboarder': 'delivered by the agent',
  'sales_agents:pay.flag.self_approved_orders': '{{count}} extra orders approved by the agent themselves',
  'sales_agents:pay.adjustment_source.carry_forward': 'Carried shortfall',
  'sales_agents:pay.self_decided.input.penalty': 'Penalty',
  'sales_agents:pay.self_decided.action.confirmed': 'confirmed',
  'sales_agents:pay.self_decided.input.unpaid_day': 'Unpaid day',
  'sales_agents:pay.self_decided.action.set': 'set',
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
vi.mock('../../stores/authStore', () => ({ useAuthStore: () => mockAuth }));

// ----- A1 / A2 fixtures (spec §5.2 shapes; figures from Illustration B, §1.2) -----
const periodRow = (overrides) => ({
  month: '2026-10', status: 'closed', is_shadow: false, start_date: '2026-10-01', end_date: '2026-10-31',
  is_estimate: false, agent_count: 3, totals: { base: 3200000.0, variable: 210000.0, total: 3560000.0 },
  next_actions: ['recalculate', 'approve'], closable_from: '2026-11-01T02:00:00+00:00',
  closed_at: '2026-11-02T05:00:00+00:00', approved_at: null, paid_on: null, last_synced_at: '2026-11-02T04:59:00+00:00',
  ...overrides,
});
const PERIODS = {
  started: true,
  startable_month: null,
  items: [
    periodRow({
      month: '2026-11', status: 'open', is_estimate: true, agent_count: 3, next_actions: ['recalculate'],
      totals: { base: 7800000.0, variable: 1350000.0, total: 9050000.0 }, closable_from: '2026-12-01T02:00:00+00:00',
      closed_at: null,
    }),
    periodRow({}),
  ],
  statuses: ['open', 'closed', 'approved', 'paid'],
  actions: ['close', 'recalculate', 'approve', 'mark_paid'],
  pending_penalty_count: 2,
  as_of: '2026-11-06T02:00:00+00:00',
};

const AZIZ = {
  agent_user_id: 41, agent_name: 'Aziz Karimov', self_decided: false, statement_id: 88, revision: 1,
  worked_days: 28, working_days: 30, base_salary: 3000000.0, base_amount: 2800000.0, commission: 850000.0,
  late_commission_after_gate: -20000.0, compliance_pct: 75.5, visits_due: 212, visits_counted: 160, gate_multiplier: 0.8,
  gated_commission: 660000.0, new_outlets: 200000.0, adjustments: 50000.0, penalties: 150000.0, variable: 760000.0,
  carry_in: 0.0, gross_total: 3560000.0, total: 3560000.0, carry_out: 0.0, owed: 0.0, late_lines: 1, review_flags: 1,
};
// A shortfall carried into November, decided in part by the agent about himself (D-Q11).
const BOBUR = {
  ...AZIZ, agent_user_id: 42, agent_name: 'Bobur Toshev', self_decided: true, statement_id: 89, worked_days: 2,
  base_amount: 200000.0, commission: 0.0, late_commission_after_gate: 0.0, compliance_pct: null, visits_due: 0,
  visits_counted: 0, gate_multiplier: 1.0, gated_commission: 0.0, new_outlets: 0.0, adjustments: 0.0,
  penalties: 300000.0, variable: -300000.0, gross_total: -100000.0, total: 0.0, carry_out: -100000.0, owed: 0.0,
  late_lines: 0, review_flags: 0,
};
// The same kind of shortfall for a leaver: owed, not carried (I-28).
const KARIM = {
  ...BOBUR, agent_user_id: 43, agent_name: 'Karim Olimov', self_decided: false, statement_id: 90,
  penalties: 250000.0, variable: -250000.0, gross_total: -50000.0, carry_out: 0.0, owed: 50000.0,
};
// Bobur's November: Base 3,000,000 · After discipline 450,000 · Carried in −100,000 · Total 3,350,000.
const BOBUR_NOVEMBER = {
  ...AZIZ, agent_user_id: 42, agent_name: 'Bobur Toshev', statement_id: 95, worked_days: 30, working_days: 30,
  base_salary: 3000000.0, base_amount: 3000000.0, commission: 450000.0, late_commission_after_gate: 0.0,
  compliance_pct: 90.0, visits_due: 200, visits_counted: 180, gate_multiplier: 1.0, gated_commission: 450000.0,
  new_outlets: 0.0, adjustments: 0.0, penalties: 0.0, variable: 450000.0, carry_in: -100000.0,
  gross_total: 3350000.0, total: 3350000.0, carry_out: 0.0, owed: 0.0, late_lines: 0, review_flags: 0,
};

const detail = (overrides) => ({
  ...periodRow({}),
  holidays: [{ date: '2026-10-01', note: 'Teachers day' }],
  calendar: {
    working_days: 30,
    days: [
      { date: '2026-10-01', weekday: 3, is_working_day: false, holiday_note: 'Teachers day' },
      { date: '2026-10-02', weekday: 4, is_working_day: true, holiday_note: null },
      { date: '2026-10-04', weekday: 6, is_working_day: true, holiday_note: null },
      // Published as not working with no holiday (only a narrowed week does that): never offered.
      { date: '2026-10-11', weekday: 6, is_working_day: false, holiday_note: null },
    ],
  },
  agents: [AZIZ, BOBUR, KARIM],
  unconfigured_agents: [],
  sync: { last_synced_at: '2026-11-02T04:59:00+00:00', stats: { credited: 3, failed: [], conflicts: 0, skipped_no_terms: [] } },
  can_edit_inputs: true,
  closed_by: { id: 1, name: 'Admin User' }, approved_by: null, paid_by: null,
  as_of: null,
  ...overrides,
});

// ----- A8 / A22 fixtures -----
const penaltyRow = (overrides) => ({
  id: 17, agent: { user_id: 41, name: 'Aziz Karimov' },
  type: { id: 2, names: { en: 'Missed planned visits', uz: "Rejali tashriflar o'tkazib yuborildi", ru: 'Пропущены плановые визиты' } },
  incident_date: '2026-10-14', reason: 'Skipped the Chilonzor route', evidence: 'Route log for 14.10',
  status: 'proposed', origin: 'proposal', proposed_by: { id: 7, name: 'Manager M.' },
  proposed_at: '2026-10-15T06:00:00+00:00', decided_at: null,
  amount: null, default_amount: 50000.0, posting_month: null, is_late: false, target_month: '2026-10', target_is_late: false,
  decided_by: null, decision_note: null, cancelled_at: null, cancel_reason: null,
  self_decided: false, can_confirm: true, can_reject: true, can_cancel: false,
  ...overrides,
});

const STATEMENT = {
  month: '2026-10', status: 'closed', is_estimate: false, is_shadow: false, revision: 1, can_edit_inputs: true,
  self_decided: true,
  self_decisions: [
    { input: 'penalty', id: 17, action: 'confirmed', at: '2026-10-09T06:10:00+00:00', date: null },
    { input: 'unpaid_day', id: 3, action: 'set', at: '2026-10-15T06:00:00+00:00', date: '2026-10-14' },
  ],
  agent: { user_id: 41, name: 'Aziz Karimov', phone: '+998901234574' },
  plan: {
    plan_id: 1, plan_name: 'Standard', version_id: 3, version_no: 1, effective_month: '2026-10',
    gate_bands: [{ min_pct: 80.0, multiplier: 1.0 }, { min_pct: 60.0, multiplier: 0.8 }, { min_pct: 0.0, multiplier: 0.5 }],
    gate_min_due: 20,
  },
  inputs: {
    base_salary: 3000000.0, employment_start: '2026-06-01', employment_end: null, working_days: 30, worked_days: 28,
    holidays: [{ date: '2026-10-01', note: 'Teachers day' }], unpaid_days: [{ date: '2026-10-14', note: 'sick' }],
    short_visit_seconds: 60, geofence_radius_m: 250,
  },
  summary: {
    base_amount: 2800000.0,
    // §5.2 (D-BANDS): the own month's products with their tiers, and the products a late group moved.
    commission: {
      gross: 850000.0, orders: 31, after_gate: 680000.0,
      products: [
        {
          product_id: 3, product_name: { en: '19L', uz: '19L', ru: '19 л' }, uses_default_tiers: false, units: 500, total: 600000.0,
          tiers: [
            { from_unit: 1, to_unit: 300, units: 300, mode: 'per_unit', value: 1000.0, net: 6000000.0, amount_full: 300000.0, amount: 300000.0 },
            { from_unit: 301, to_unit: null, units: 200, mode: 'per_unit', value: 1500.0, net: 4000000.0, amount_full: 300000.0, amount: 300000.0 },
          ],
          next_tier: null,
        },
        {
          product_id: 9, product_name: { en: 'Juice 1 L', uz: 'Sharbat 1 L', ru: 'Сок 1 л' }, uses_default_tiers: true, units: 500, total: 250000.0,
          tiers: [{ from_unit: 1, to_unit: null, units: 500, mode: 'percent', value: 2.0, net: 12500000.0, amount_full: 250000.0, amount: 250000.0 }],
          next_tier: null,
        },
      ],
    },
    late: [{
      earned_month: '2026-09', plan_version_id: 2, gross: -20000.0, multiplier: 1.0, source: 'statement', after_gate: -20000.0,
      counted: true, products: [{
        product_id: 3, product_name: { en: '19L', uz: '19L', ru: '19 л' }, units_before: 410, units: 400,
        total_before: 615000.0, total: 595000.0, change: -20000.0, tiers_before: [], tiers: [],
      }],
    }],
    gate: { compliance_pct: 75.5, visits_due: 212, visits_counted: 160, multiplier: 0.8, rule: 'band', band: { min_pct: 60.0, multiplier: 0.8 }, provisional: false },
    gated_commission: 660000.0, new_outlets: { amount: 200000.0, count: 2 },
    adjustments: 50000.0, penalties: 150000.0, variable: 760000.0,
    carry_in: { amount: -100000.0, from_month: '2026-09', source: 'carry_forward' },
    gross_total: 3460000.0, total: 3460000.0, carry_out: 0.0, owed: 0.0,
  },
  days: [
    { date: '2026-10-02', weekday: 4, day_status: 'worked', plan_source: 'snapshot', legacy: false, due: 9, counted: 7, not_counted: { not_visited: 1, short: 1 } },
    { date: '2026-10-03', weekday: 5, day_status: 'worked', plan_source: 'snapshot', legacy: true, due: 8, counted: 8, not_counted: {} },
    { date: '2026-10-14', weekday: 2, day_status: 'unpaid', plan_source: 'none', legacy: false, due: null, counted: 0, not_counted: {} },
  ],
  day_statuses: ['worked', 'non_working', 'holiday', 'unpaid', 'not_employed'],
  not_counted_reasons: ['not_visited', 'not_completed', 'no_checkin', 'skipped', 'out_of_range', 'no_location', 'short'],
  new_outlets: [{
    outlet_id: 77, outlet_name: 'Oasis market', status: 'qualified', activation_reason: 'approved',
    onboarded_at: '2026-09-20T06:00:00+00:00', window_start: '2026-09-22T06:00:00+00:00', window_end: '2026-11-21T06:00:00+00:00', window_days: 60,
    window_opened_by: { order_id: 405, order_number: 'TG_000405_26', order_source: 'telegram', delivered_instant: '2026-09-22T06:00:00+00:00', is_paid: true },
    rule: 'orders_with_total', amount: 100000.0, is_late: false,
    review_flags: { dedupe_forced: true, nearby_other_accounts: 3, all_orders_placed_by_onboarder: false, delivered_by_onboarder: true, self_approved_orders: 2 },
    orders: [{ order_id: 412, order_number: 'SA_000412_26', order_source: 'sales_agent', earned_instant: '2026-10-12T07:31:00+00:00', total: 240000.0, staff_approved: true }],
  }],
  penalties: [penaltyRow({ status: 'confirmed', amount: 150000.0, posting_month: '2026-10', can_confirm: false, can_reject: false, can_cancel: true })],
  adjustments: [
    { id: 5, amount: 50000.0, reason: 'Stock count bonus', source: 'admin', carried_from_month: null, posting_month: '2026-10', created_at: '2026-10-20T06:00:00+00:00', created_by: { id: 1, name: 'Admin User' } },
    { id: 6, amount: -100000.0, reason: 'SYSTEM carry 2026-09 stmt 71', source: 'carry_forward', carried_from_month: '2026-09', posting_month: '2026-10', created_at: '2026-10-02T02:00:00+00:00', created_by: { id: 1, name: 'Admin User' } },
  ],
  line_kinds: ['commission_credit', 'commission_reversal', 'commission_difference', 'new_outlet_bonus'],
  line_counts: { commission_credit: 31, commission_reversal: 1, commission_difference: 0, new_outlet_bonus: 2 },
  as_of: null,
};

// ----- A12 / A14 fixtures (Task 4's pinned echo) -----
const PLAN = {
  id: 1, name: 'Standard',
  version_in_force: { id: 3, version_no: 1, effective_month: '2026-10' },
  timeline: [{ from_month: '2026-10', version_id: 3, version_no: 1 }],
  versions: [{
    id: 3, version_no: 1, effective_month: '2026-10', applies_from: '2026-10', applies_until: null, replaced_by_version_no: null,
    created_at: '2026-09-28T06:00:00+00:00', created_by: { id: 1, name: 'Admin User' }, note: 'launch',
  }],
};
const DEFAULT_CONFIG = {
  gate_bands: [{ min_pct: 80.0, multiplier: 1.0 }, { min_pct: 60.0, multiplier: 0.8 }, { min_pct: 0.0, multiplier: 0.5 }],
  gate_min_visits_due: 20, bonus_window_days: 60, bonus_min_orders_with_total: 2,
  bonus_min_orders_any_amount: 5, bonus_prior_customer_lookback_days: 180,
};
const PLANS = {
  items: [PLAN], rate_modes: ['per_unit', 'percent'], bonus_rules: ['orders_with_total', 'orders_any'],
  editable_from_month: '2026-11', current_month: '2026-11', default_config: DEFAULT_CONFIG,
};
const VERSION_DETAIL = {
  id: 3, plan_id: 1, plan_name: 'Standard', version_no: 1, effective_month: '2026-10',
  default_tiers: [{ from_unit: 1, to_unit: null, mode: 'percent', value: 3.0 }],
  rates: [
    {
      product_id: 3, product_name: 'Pure Water 19L', product_is_active: true,
      tiers: [{ from_unit: 1, to_unit: 300, mode: 'per_unit', value: 1000.0 }, { from_unit: 301, to_unit: null, mode: 'per_unit', value: 1500.0 }],
    },
    {
      product_id: 5, product_name: 'Water 10L', product_is_active: false,
      tiers: [{ from_unit: 1, to_unit: null, mode: 'percent', value: 2.5 }],
    },
  ],
  gate_bands: DEFAULT_CONFIG.gate_bands, gate_min_visits_due: 20,
  new_outlet_bonus: {
    amount: 150000.0, window_days: 60, prior_customer_lookback_days: 180,
    min_orders_with_total: 2, min_combined_total: 300000.0, min_orders_any_amount: 5,
  },
  note: 'launch', created_at: '2026-09-28T06:00:00+00:00', created_by: { id: 1, name: 'Admin User' },
};
const PENALTY_TYPE = {
  id: 2, names: { en: 'Missed planned visits', uz: "Rejali tashriflar o'tkazib yuborildi", ru: 'Пропущены плановые визиты' },
  default_amount: 50000.0, is_active: true,
};

const LocationProbe = () => {
  const location = useLocation();
  return <div data-testid="agents-page">{JSON.stringify(location.state)}</div>;
};

const renderPage = (entry = '/sales/compensation') => {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  const invalidate = vi.spyOn(queryClient, 'invalidateQueries');
  render(
    <MemoryRouter initialEntries={[entry]}>
      <QueryClientProvider client={queryClient}>
        <Routes>
          <Route path="/sales/compensation" element={<SalesCompensation />} />
          <Route path="/sales/agents" element={<LocationProbe />} />
        </Routes>
      </QueryClientProvider>
    </MemoryRouter>,
  );
  return { invalidate };
};

const refusal = (status, code, details) => ({
  response: { status, data: { success: false, message: `${code} (backend sentence)`, error_code: code, details } },
});

beforeEach(() => {
  vi.clearAllMocks();
  mockAuth.hasPermission.mockReturnValue(true);
  salesPayService.getPeriods.mockResolvedValue(PERIODS);
  salesPayService.getPeriod.mockResolvedValue(detail({}));
  salesPayService.getStatement.mockResolvedValue(STATEMENT);
  salesPayService.getStatementLines.mockResolvedValue({ items: [], meta: { page: 1, per_page: 20, total: 0 }, kind: null, line_kinds: STATEMENT.line_kinds, as_of: null });
  salesPayService.getPlans.mockResolvedValue(PLANS);
  salesPayService.getPlanVersion.mockResolvedValue({ version: VERSION_DETAIL });
  salesPayService.getPenaltyTypes.mockResolvedValue({ items: [PENALTY_TYPE] });
  salesPayService.getPenalties.mockResolvedValue({ items: [penaltyRow({ self_decided: true })], meta: { page: 1, per_page: 20, total: 1 }, statuses: ['proposed', 'confirmed', 'rejected', 'cancelled'] });
  ['startPay', 'closePeriod', 'recalculatePeriod', 'approvePeriod', 'markPeriodPaid', 'setHolidays'].forEach((name) => salesPayService[name].mockResolvedValue(detail({})));
  salesPayService.createPlanVersion.mockResolvedValue({ version: VERSION_DETAIL });
  salesPayService.confirmPenalty.mockResolvedValue({ penalty: penaltyRow({ status: 'confirmed' }) });
  salesPayService.rejectPenalty.mockResolvedValue({ penalty: penaltyRow({ status: 'rejected' }) });
  salesPayService.updatePenaltyType.mockResolvedValue({ type: { ...PENALTY_TYPE, is_active: false } });
  salesPayService.createPenaltyType.mockResolvedValue({ type: { ...PENALTY_TYPE, id: 3 } });
  staffService.getSalesAgents.mockResolvedValue({ data: { data: { items: [{ user_id: 41, full_name: 'Aziz Karimov' }, { user_id: 42, full_name: 'Bobur Toshev' }] }, meta: { total: 2 } } });
  adminService.getProducts.mockResolvedValue({ success: true, data: { items: [{ id: 3, name: 'Pure Water 19L', is_active: true }, { id: 5, name: 'Water 10L', is_active: false }] } });
});

// Modal.confirm portals outside Testing Library's root (the Delivery.test.js precedent). An
// undismissed one (the shadow-month approve test below inspects it without clicking) would
// leak into the next test.
afterEach(() => {
  Modal.destroyAll();
});

// ---------------------------------------------------------------- page, tabs, start

it('gives an admin four tabs, the pending badge on Penalties, and no export anywhere', async () => {
  renderPage();

  expect(await screen.findByText('Compensation')).toBeInTheDocument();
  ['Months', 'Plans', 'Penalty types'].forEach((name) => expect(screen.getByRole('tab', { name })).toBeInTheDocument());
  // The Title renders before A1 resolves (T12-R2: the page no longer gates on it, so a cold
  // deep link into Plans/Penalties/Types never waits on the Months-only periods query); the
  // badge itself still starts at 0 and flips once `pending_penalty_count` arrives.
  await waitFor(() => expect(screen.getByRole('tab', { name: /Penalties/ })).toHaveTextContent('2'));
  expect(screen.queryByRole('button', { name: /export/i })).toBeNull();
});

it('before pay starts, "Start with" posts A0 for the published month as real pay unless the trial box is ticked', async () => {
  salesPayService.getPeriods.mockResolvedValue({ ...PERIODS, started: false, startable_month: '2026-10', items: [], pending_penalty_count: 0 });
  renderPage();

  expect(await screen.findByText('Pay tracking has not started.')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Start with 10.2026' }));
  const dialog = await screen.findByRole('dialog');
  expect(within(dialog).getByText('Tick only if this first month should be a dry run: numbers are shown for review, and pay is made the old way.')).toBeInTheDocument();
  expect(within(dialog).getByRole('checkbox', { name: 'Trial month (optional)' })).not.toBeChecked();
  fireEvent.click(within(dialog).getByRole('button', { name: 'Start' }));

  await waitFor(() => expect(salesPayService.startPay).toHaveBeenCalledWith({ month: '2026-10', isShadow: false }));
});

it('a ticked trial box starts the first month as a trial month', async () => {
  salesPayService.getPeriods.mockResolvedValue({ ...PERIODS, started: false, startable_month: '2026-10', items: [], pending_penalty_count: 0 });
  renderPage();

  fireEvent.click(await screen.findByRole('button', { name: 'Start with 10.2026' }));
  const dialog = await screen.findByRole('dialog');
  fireEvent.click(within(dialog).getByRole('checkbox', { name: 'Trial month (optional)' }));
  expect(within(dialog).getByRole('checkbox', { name: 'Trial month (optional)' })).toBeChecked();
  fireEvent.click(within(dialog).getByRole('button', { name: 'Start' }));

  await waitFor(() => expect(salesPayService.startPay).toHaveBeenCalledWith({ month: '2026-10', isShadow: true }));
});

it('lists A1 months newest first, tags an estimate, and opens a month on click', async () => {
  renderPage();

  const november = (await screen.findByText('11.2026')).closest('tr');
  expect(november).toHaveTextContent('estimate');
  expect(november).toHaveTextContent('9,050,000');
  const october = screen.getByText('10.2026').closest('tr');
  expect(october).not.toHaveTextContent('estimate');
  expect(october).toHaveTextContent('3,560,000');

  fireEvent.click(october);

  await waitFor(() => expect(salesPayService.getPeriod).toHaveBeenCalledWith('2026-10'));
  expect(await screen.findByText('Aziz Karimov')).toBeInTheDocument();
});

// ---------------------------------------------------------------- month view

const ACTION_LABELS = ['Close', 'Recalculate', 'Sync now', 'Approve', 'Mark paid'];

it.each([
  ['open', ['recalculate'], ['Sync now']],
  ['open', ['close', 'recalculate'], ['Close', 'Sync now']],
  ['closed', ['recalculate', 'approve'], ['Recalculate', 'Approve']],
  ['approved', ['mark_paid'], ['Mark paid']],
  ['paid', [], []],
])('a %s month with next_actions %j draws exactly %j', async (status, nextActions, drawn) => {
  salesPayService.getPeriod.mockResolvedValue(detail({ status, next_actions: nextActions, is_estimate: status === 'open' }));
  renderPage('/sales/compensation?month=2026-10');
  await screen.findByText('Aziz Karimov');

  ACTION_LABELS.forEach((label) => {
    expect(Boolean(screen.queryByRole('button', { name: label }))).toBe(drawn.includes(label));
  });
});

it('reads each agent row from its published fields', async () => {
  renderPage('/sales/compensation?month=2026-10');

  const aziz = (await screen.findByText('Aziz Karimov')).closest('tr');
  ['28/30', '2,800,000', '850,000', '75.5% (160/212) → ×0.8', '−20,000', '660,000', '200,000', '−150,000', '3,560,000', 'review']
    .forEach((text) => expect(aziz).toHaveTextContent(text));
  expect(aziz).not.toHaveTextContent('Self-decided');

  // D-Q11: the tag is drawn from `self_decided` alone.
  const bobur = screen.getByText('Bobur Toshev').closest('tr');
  expect(bobur).toHaveTextContent('Self-decided');
  expect(bobur).toHaveTextContent('100,000 will be deducted next month (the total cannot go below 0)');
  expect(bobur).not.toHaveTextContent('Owed by the agent');

  const karim = screen.getByText('Karim Olimov').closest('tr');
  expect(karim).toHaveTextContent('Owed by the agent: 50,000');
  expect(karim).not.toHaveTextContent('will be deducted next month');
});

it('prints a carried-in shortfall with a minus and leaves the column blank at zero', async () => {
  salesPayService.getPeriod.mockResolvedValue(detail({ month: '2026-11', status: 'open', is_estimate: true, next_actions: ['recalculate'], agents: [AZIZ, BOBUR_NOVEMBER] }));
  renderPage('/sales/compensation?month=2026-11');

  const bobur = (await screen.findByText('Bobur Toshev')).closest('tr');
  const cells = within(bobur).getAllByRole('cell').map((cell) => cell.textContent);
  // Agent · Days · Base · Commission · Plan vs fact · Late · After discipline · New outlets ·
  // Adjustments · Penalties · Carried in · Total
  expect(cells[2]).toBe('3,000,000');
  expect(cells[6]).toBe('450,000');
  expect(cells[10]).toBe('−100,000');
  expect(cells[11]).toContain('3,350,000');
  const aziz = within(screen.getByText('Aziz Karimov').closest('tr')).getAllByRole('cell').map((cell) => cell.textContent);
  expect(aziz[10]).toBe('');
});

it('asks before approving, with the pending proposals and the self-decided agents named', async () => {
  renderPage('/sales/compensation?month=2026-10');
  await screen.findByText('Aziz Karimov');

  fireEvent.click(screen.getByRole('button', { name: 'Approve' }));
  const dialog = await screen.findByRole('dialog');
  expect(dialog).toHaveTextContent('Approve 10.2026?');
  expect(dialog).toHaveTextContent('Statements are locked permanently and each agent receives their final statement in the staff bot.');
  expect(dialog).toHaveTextContent('2 penalty proposals are still pending. If confirmed later, they land in the next open month as late.');
  expect(dialog).toHaveTextContent('Statements with decisions admins made about their own pay: Bobur Toshev.');
  fireEvent.click(within(dialog).getByRole('button', { name: 'Approve' }));

  await waitFor(() => expect(salesPayService.approvePeriod).toHaveBeenCalledWith('2026-10'));
});

it('warns a shadow month that its trial proposals die with the approval', async () => {
  salesPayService.getPeriods.mockResolvedValue({ ...PERIODS, items: [periodRow({ is_shadow: true })] });
  salesPayService.getPeriod.mockResolvedValue(detail({ is_shadow: true }));
  renderPage('/sales/compensation?month=2026-10');
  await screen.findByText('Aziz Karimov');
  expect(screen.getByText('Trial month: numbers are shown, pay is made the old way')).toBeInTheDocument();

  fireEvent.click(screen.getByRole('button', { name: 'Approve' }));
  const dialog = await screen.findByRole('dialog');

  expect(dialog).toHaveTextContent('2 penalty proposals are still pending. Proposals for incidents in this trial month can no longer be confirmed once it is approved; confirm or reject them first.');
  expect(dialog).not.toHaveTextContent('they land in the next open month as late');
});

it('explains a TERMS_MISSING refusal once, inline, naming the agent, and never with message.error', async () => {
  salesPayService.getPeriod.mockResolvedValue(detail({
    status: 'open', is_estimate: true, next_actions: ['close', 'recalculate'],
    unconfigured_agents: [{ agent_user_id: 44, agent_name: 'Dilshod Rahimov' }],
  }));
  salesPayService.closePeriod.mockRejectedValue(refusal(409, 'SALES_PAY_TERMS_MISSING', { agents: [44] }));
  renderPage('/sales/compensation?month=2026-10');
  await screen.findByText('Aziz Karimov');

  fireEvent.click(screen.getByRole('button', { name: 'Close' }));
  const dialog = await screen.findByRole('dialog');
  expect(dialog).toHaveTextContent('Close 10.2026?');
  expect(dialog).toHaveTextContent('The numbers are frozen into statements. You can still add penalties or adjustments and recalculate before approving.');
  fireEvent.click(within(dialog).getByRole('button', { name: 'Close' }));

  const alerts = await screen.findAllByTestId('pay-error');
  expect(alerts).toHaveLength(1);
  expect(alerts[0]).toHaveTextContent('Set pay terms first for: Dilshod Rahimov.');
  expect(message.error).not.toHaveBeenCalled();
});

it('lists unconfigured agents with a way into their Pay tab', async () => {
  salesPayService.getPeriod.mockResolvedValue(detail({
    status: 'open', is_estimate: true, next_actions: ['recalculate'], as_of: '2026-11-06T02:00:00+00:00',
    unconfigured_agents: [{ agent_user_id: 44, agent_name: 'Dilshod Rahimov' }],
  }));
  renderPage('/sales/compensation?month=2026-10');
  await screen.findByText('Aziz Karimov');
  expect(screen.getByText(/^Estimate as of 2026-11-0/)).toBeInTheDocument();
  expect(screen.getByText(/^Can be closed from 2026-11-01/)).toBeInTheDocument();

  fireEvent.click(screen.getByRole('button', { name: 'Set pay terms' }));

  expect(await screen.findByTestId('agents-page')).toHaveTextContent('{"payAgent":{"user_id":44,"full_name":"Dilshod Rahimov"}}');
});

it('warns when orders could not be synced', async () => {
  salesPayService.getPeriod.mockResolvedValue(detail({
    status: 'open', is_estimate: true, next_actions: ['recalculate'],
    sync: { last_synced_at: '2026-11-02T04:59:00+00:00', stats: { credited: 3, failed: [{ agent_id: 41, order_id: 501, error: 'RuntimeError' }, { agent_id: 41, order_id: 502, error: 'RuntimeError' }], conflicts: 0, skipped_no_terms: [503] } },
  }));
  renderPage('/sales/compensation?month=2026-10');

  expect(await screen.findByText('3 orders could not be synced. Close will be refused until this is fixed.')).toBeInTheDocument();
});

it('marks a month paid on the date the admin picks, today by default', async () => {
  salesPayService.getPeriod.mockResolvedValue(detail({ status: 'approved', next_actions: ['mark_paid'], can_edit_inputs: false }));
  renderPage('/sales/compensation?month=2026-10');
  await screen.findByText('Aziz Karimov');
  expect(screen.queryByRole('button', { name: 'Holidays' })).toBeNull();

  fireEvent.click(screen.getByRole('button', { name: 'Mark paid' }));
  const dialog = await screen.findByRole('dialog');
  fireEvent.click(within(dialog).getByRole('button', { name: 'Mark paid' }));

  const today = new Date();
  const iso = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, '0')}-${String(today.getDate()).padStart(2, '0')}`;
  await waitFor(() => expect(salesPayService.markPeriodPaid).toHaveBeenCalledWith('2026-10', iso));
});

it('saves the whole holiday list from the published calendar', async () => {
  renderPage('/sales/compensation?month=2026-10');
  await screen.findByText('Aziz Karimov');

  fireEvent.click(screen.getByRole('button', { name: 'Holidays' }));
  const dialog = await screen.findByRole('dialog');
  // The candidates are the published working days plus the current holidays: Sunday the 4th is a
  // working day (C2 v5), so it is offered; the 11th is published as not working with no holiday
  // note, so it is not. The client decides nothing about weekdays.
  expect(within(dialog).getByRole('checkbox', { name: '01.10.2026' })).toBeChecked();
  expect(within(dialog).getByRole('checkbox', { name: '04.10.2026' })).not.toBeChecked();
  expect(within(dialog).queryByRole('checkbox', { name: '11.10.2026' })).toBeNull();
  fireEvent.click(within(dialog).getByRole('checkbox', { name: '04.10.2026' }));
  fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }));

  await waitFor(() => expect(salesPayService.setHolidays).toHaveBeenCalledWith('2026-10', [
    { date: '2026-10-01', note: 'Teachers day' },
    { date: '2026-10-04', note: null },
  ]));
});

// ---------------------------------------------------------------- statement drawer

it('opens the statement drawer with every tab the drill-down needs', async () => {
  renderPage('/sales/compensation?month=2026-10');
  fireEvent.click(await screen.findByText('Aziz Karimov'));

  await waitFor(() => expect(salesPayService.getStatement).toHaveBeenCalledWith('2026-10', 41));
  const drawer = await screen.findByRole('dialog');

  // D-Q11: the warning lists the published self-decisions.
  expect(await within(drawer).findByText('This statement includes decisions Aziz Karimov made about their own pay:')).toBeInTheDocument();
  expect(drawer).toHaveTextContent('Penalty · confirmed');
  expect(drawer).toHaveTextContent('Unpaid day · set · 14.10.2026');
  // Summary first, with the month's products and the late group's (D-BANDS).
  expect(within(drawer).getByText('Gross')).toBeInTheDocument();
  expect(drawer).toHaveTextContent('19L: 500 units · 600,000');
  expect(drawer).toHaveTextContent('1–300: 300 × 1,000 = 300,000');
  expect(drawer).toHaveTextContent('19L: 410 → 400 units, 615,000 → 595,000 (−20,000)');
  expect(within(drawer).queryByRole('button', { name: /export/i })).toBeNull();

  fireEvent.click(within(drawer).getByRole('tab', { name: 'Orders' }));
  await waitFor(() => expect(salesPayService.getStatementLines).toHaveBeenCalledWith('2026-10', 41, { kind: undefined, page: 1, perPage: 20 }));

  fireEvent.click(within(drawer).getByRole('tab', { name: 'Days' }));
  const legacyRow = (await within(drawer).findByText('2026-10-03')).closest('tr');
  expect(legacyRow).toHaveTextContent('n/a (before frozen plans)');
  expect(drawer).toHaveTextContent('Due 212 · Counted 160');
  expect(within(drawer).getByRole('button', { name: 'Unpaid days' })).toBeInTheDocument();

  fireEvent.click(within(drawer).getByRole('tab', { name: 'New outlets' }));
  const outlet = (await within(drawer).findByText('Oasis market')).closest('tr');
  expect(outlet).toHaveTextContent('forced duplicate');
  expect(outlet).toHaveTextContent('3 nearby orders by other accounts');
  expect(outlet).toHaveTextContent('delivered by the agent');
  expect(outlet).toHaveTextContent('2 extra orders approved by the agent themselves');
  expect(outlet).not.toHaveTextContent('all orders placed by the agent');
  fireEvent.click(within(outlet).getByRole('button', { name: /expand row/i }));
  expect(outlet.nextElementSibling).toHaveTextContent('SA_000412_26');
  expect(outlet.nextElementSibling).toHaveTextContent('approved extra order');

  fireEvent.click(within(drawer).getByRole('tab', { name: 'Penalties & adjustments' }));
  const carry = (await within(drawer).findByText('Carried shortfall')).closest('tr');
  expect(carry).toHaveTextContent('Shortfall from 09.2026');
  expect(carry).not.toHaveTextContent('SYSTEM carry');
  expect(within(drawer).getByRole('button', { name: 'Add penalty' })).toBeInTheDocument();
  expect(within(drawer).getByRole('button', { name: 'Add adjustment' })).toBeInTheDocument();
  expect(within(drawer).getByRole('button', { name: 'Cancel penalty' })).toBeInTheDocument();
});

it('offers a worked Sunday as an unpaid day and never a holiday (C2 v5, I-38)', async () => {
  const [second, third, unpaid] = STATEMENT.days;
  salesPayService.getStatement.mockResolvedValue({
    ...STATEMENT,
    days: [
      { date: '2026-10-01', weekday: 3, day_status: 'holiday', plan_source: 'none', legacy: false, due: null, counted: 0, not_counted: {} },
      second,
      third,
      { date: '2026-10-04', weekday: 6, day_status: 'worked', plan_source: 'none', legacy: false, due: null, counted: 0, not_counted: {} },
      unpaid,
    ],
  });
  renderPage('/sales/compensation?month=2026-10');
  fireEvent.click(await screen.findByText('Aziz Karimov'));
  const drawer = await screen.findByRole('dialog');
  fireEvent.click(await within(drawer).findByRole('tab', { name: 'Days' }));
  fireEvent.click(await within(drawer).findByRole('button', { name: 'Unpaid days' }));

  const sunday = await screen.findByRole('checkbox', { name: '04.10.2026' });
  expect(sunday).not.toBeChecked();
  expect(screen.getByRole('checkbox', { name: '14.10.2026' })).toBeChecked();
  expect(screen.queryByRole('checkbox', { name: '01.10.2026' })).toBeNull();
  fireEvent.click(sunday);
  fireEvent.click(screen.getAllByRole('button', { name: 'Save' }).at(-1));

  await waitFor(() => expect(salesPayService.setUnpaidDays).toHaveBeenCalledWith('2026-10', 41, [
    { date: '2026-10-04', note: null },
    { date: '2026-10-14', note: 'sick' },
  ]));
});

// Hovers the Oasis market window and waits for its tooltip, which always carries the first
// delivery that opened the window.
const hoverOutletWindow = async () => {
  renderPage('/sales/compensation?month=2026-10');
  fireEvent.click(await screen.findByText('Aziz Karimov'));
  const drawer = await screen.findByRole('dialog');
  // `findByRole`, not `getByRole`: the dialog role is on the Drawer shell, present before the
  // statement query resolves and its tabs mount.
  fireEvent.click(await within(drawer).findByRole('tab', { name: 'New outlets' }));
  const outlet = (await within(drawer).findByText('Oasis market')).closest('tr');
  fireEvent.mouseEnter(within(outlet).getByText('22.09.2026 – 21.11.2026'));
  await screen.findByText(/^Window opened by the first delivery: TG_000405_26/);
};

it('names the window length A8 published for the outlet check', async () => {
  await hoverOutletWindow();

  expect(screen.getByText('The 60-day window starts at the first delivery after onboarding, paid or not. Only orders delivered and paid inside it count.')).toBeInTheDocument();
});

it('draws no window hint for a check with no plan version (window_days null)', async () => {
  salesPayService.getStatement.mockResolvedValue({ ...STATEMENT, new_outlets: [{ ...STATEMENT.new_outlets[0], window_days: null }] });
  await hoverOutletWindow();

  expect(screen.queryByText(/-day window starts at the first delivery/)).toBeNull();
});

// ---------------------------------------------------------------- penalties, types, plans

it('lists proposed penalties and confirms one at its default amount, saying where it lands', async () => {
  renderPage('/sales/compensation?tab=penalties');

  const row = (await screen.findByText('Skipped the Chilonzor route')).closest('tr');
  expect(salesPayService.getPenalties).toHaveBeenCalledWith({ status: 'proposed', agentId: undefined, month: undefined, page: 1, perPage: 20 });
  expect(row).toHaveTextContent('Missed planned visits');
  expect(row).toHaveTextContent('Self-decided');

  fireEvent.click(within(row).getByRole('button', { name: 'Confirm' }));
  const dialog = await screen.findByRole('dialog');
  expect(within(dialog).getByTestId('penalty-target')).toHaveTextContent('Will count in 10.2026');
  expect(within(dialog).getByTestId('penalty-target')).not.toHaveTextContent('(late)');
  expect(within(dialog).getByLabelText('Amount')).toHaveValue('50,000');
  fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }));

  await waitFor(() => expect(salesPayService.confirmPenalty).toHaveBeenCalledWith(17, { amount: 50000 }));
});

// T12-R1: A24 can still answer TERMS_MISSING (pay_penalty_service.py:258, `details.agents`
// carries the agent's id) even though `can_confirm` already checked month routing — the modal
// must name the agent from data it already has (the row's own `agent`), never print the raw id.
it('names the agent, not their id, in a TERMS_MISSING refusal on confirm (A24)', async () => {
  salesPayService.confirmPenalty.mockRejectedValue(refusal(409, 'SALES_PAY_TERMS_MISSING', { agents: [41], month: '2026-11' }));
  renderPage('/sales/compensation?tab=penalties');

  const row = (await screen.findByText('Skipped the Chilonzor route')).closest('tr');
  fireEvent.click(within(row).getByRole('button', { name: 'Confirm' }));
  const dialog = await screen.findByRole('dialog');
  fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }));

  const alerts = await screen.findAllByTestId('pay-error');
  expect(alerts).toHaveLength(1);
  expect(alerts[0]).toHaveTextContent('Set pay terms first for: Aziz Karimov.');
  expect(alerts[0]).not.toHaveTextContent('#41');
  expect(message.error).not.toHaveBeenCalled();
});

it('rejects a proposal with an admin-only note', async () => {
  renderPage('/sales/compensation?tab=penalties');
  const row = (await screen.findByText('Skipped the Chilonzor route')).closest('tr');

  fireEvent.click(within(row).getByRole('button', { name: 'Reject' }));
  const dialog = await screen.findByRole('dialog');
  expect(within(dialog).getByText('Visible to admins only.')).toBeInTheDocument();
  fireEvent.change(within(dialog).getByLabelText('Note'), { target: { value: 'Not his route' } });
  fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }));

  await waitFor(() => expect(salesPayService.rejectPenalty).toHaveBeenCalledWith(17, 'Not his route'));
});

it('switches a penalty type off and creates a new one with three names', async () => {
  const { invalidate } = renderPage('/sales/compensation?tab=types');

  const row = (await screen.findByText('Missed planned visits')).closest('tr');
  expect(row).toHaveTextContent('50,000');
  fireEvent.click(within(row).getByRole('switch'));
  await waitFor(() => expect(salesPayService.updatePenaltyType).toHaveBeenCalledWith(2, { is_active: false }));
  await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: ['salesPay'] }));

  // Regex, not an exact string: `@ant-design/icons` puts the glyph's own name ("plus") into the
  // button's accessible name ahead of the visible text, the same reason the Penalties tab badge
  // above is matched with a regex rather than an exact string.
  fireEvent.click(screen.getByRole('button', { name: /New type/ }));
  const dialog = await screen.findByRole('dialog');
  expect(within(dialog).getByText('Names are shown to managers and agents.')).toBeInTheDocument();
  fireEvent.change(within(dialog).getByLabelText('Name (English)'), { target: { value: 'Late start' } });
  fireEvent.change(within(dialog).getByLabelText('Name (Uzbek)'), { target: { value: 'Kech boshlash' } });
  fireEvent.change(within(dialog).getByLabelText('Name (Russian)'), { target: { value: 'Поздний старт' } });
  fireEvent.change(within(dialog).getByLabelText('Default amount'), { target: { value: '70 000' } });
  fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }));

  await waitFor(() => expect(salesPayService.createPenaltyType).toHaveBeenCalledWith({
    names: { en: 'Late start', uz: 'Kech boshlash', ru: 'Поздний старт' }, default_amount: 70000,
  }));
});

it('shows the empty plans state', async () => {
  salesPayService.getPlans.mockResolvedValue({ ...PLANS, items: [] });
  renderPage('/sales/compensation?tab=plans');

  expect(await screen.findByText('No pay plans yet. Create one to set commission tiers.')).toBeInTheDocument();
});

it('posts a new version pre-filled from the version in force, from the first editable month', async () => {
  renderPage('/sales/compensation?tab=plans');

  const row = (await screen.findByText('Standard')).closest('tr');
  expect(row).toHaveTextContent('10.2026');
  fireEvent.click(within(row).getByRole('button', { name: 'New version' }));
  await waitFor(() => expect(salesPayService.getPlanVersion).toHaveBeenCalledWith(1, 3));
  const dialog = await screen.findByRole('dialog');
  // An inactive product keeps its rate, labelled as such (T-PLAN-3).
  expect(await within(dialog).findByTitle('Water 10L (inactive)')).toBeInTheDocument();
  fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }));

  await waitFor(() => expect(salesPayService.createPlanVersion).toHaveBeenCalledWith(1, {
    effective_month: '2026-11',
    default_tiers: [{ from_unit: 1, mode: 'percent', value: 3 }],
    rates: [
      { product_id: 3, tiers: [{ from_unit: 1, mode: 'per_unit', value: 1000 }, { from_unit: 301, mode: 'per_unit', value: 1500 }] },
      { product_id: 5, tiers: [{ from_unit: 1, mode: 'percent', value: 2.5 }] },
    ],
    gate_bands: [{ min_pct: 80, multiplier: 1 }, { min_pct: 60, multiplier: 0.8 }, { min_pct: 0, multiplier: 0.5 }],
    gate_min_visits_due: 20,
    new_outlet_bonus: {
      amount: 150000, window_days: 60, prior_customer_lookback_days: 180,
      min_orders_with_total: 2, min_combined_total: 300000, min_orders_any_amount: 5,
    },
    note: null,
  }));
});

it('fires no pay read for a viewer without can_manage_sales_pay', async () => {
  mockAuth.hasPermission.mockReturnValue(false);
  renderPage();

  await new Promise((resolve) => { setTimeout(resolve, 0); });
  Object.values(salesPayService).forEach((fn) => expect(fn).not.toHaveBeenCalled());
});
