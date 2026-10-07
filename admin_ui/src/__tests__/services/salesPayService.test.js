import salesPayService, { PAY_HANDLED_CODES } from '../../services/salesPayService';
import api from '../../services/api';

vi.mock('../../services/api', () => ({
  __esModule: true,
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}));

const envelope = (data) => ({ data: { success: true, data } });
const HANDLED = { handledErrorCodes: PAY_HANDLED_CODES };

// §4.16's eighteen SALES_PAY_* codes, spelled out here as the ruling, not read back from the
// module under test. The Python pin (tests/unit/test_admin_ui_payload_fixture_contracts.py)
// holds the same list against the literal raise sites.
const EIGHTEEN = [
  'SALES_PAY_MONTH_INVALID', 'SALES_PAY_NOT_STARTED', 'SALES_PAY_ALREADY_STARTED', 'SALES_PAY_MONTH_LOCKED',
  'SALES_PAY_NOT_FOUND', 'SALES_PAY_STATE_INVALID', 'SALES_PAY_PERIOD_NOT_ENDED', 'SALES_PAY_PREVIOUS_PERIOD_OPEN',
  'SALES_PAY_PREVIOUS_PERIOD_NOT_APPROVED', 'SALES_PAY_SYNC_INCOMPLETE', 'SALES_PAY_TERMS_MISSING',
  'SALES_PAY_PLAN_INVALID', 'SALES_PAY_TERMS_INVALID', 'SALES_PAY_DATE_INVALID', 'SALES_PAY_AMOUNT_INVALID',
  'SALES_PAY_REASON_REQUIRED', 'SALES_PAY_PENALTY_TYPE_INACTIVE', 'SALES_PAY_SELF_DECISION',
];

beforeEach(() => {
  vi.clearAllMocks();
  ['get', 'post', 'put', 'patch'].forEach((verb) => api[verb].mockResolvedValue(envelope({ echoed: verb })));
});

it('names exactly the eighteen pay refusals a pay modal explains itself', () => {
  expect([...PAY_HANDLED_CODES].sort()).toEqual([...EIGHTEEN].sort());
});

// [label, call, verb, url, body-or-params]. Reads take a params object (or none); mutations take
// a body AND the handled codes, so a refusal is explained in the modal that sent it, once.
const READS = [
  ['A1 periods', () => salesPayService.getPeriods(), '/admin/sales/pay/periods', undefined],
  ['A2 one month', () => salesPayService.getPeriod('2026-10'), '/admin/sales/pay/periods/2026-10', undefined],
  ['A8 statement', () => salesPayService.getStatement('2026-10', 41), '/admin/sales/pay/periods/2026-10/agents/41', undefined],
  [
    'A9 lines',
    () => salesPayService.getStatementLines('2026-10', 41, { kind: 'commission_difference', page: 2, perPage: 20 }),
    '/admin/sales/pay/periods/2026-10/agents/41/lines',
    { params: { kind: 'commission_difference', page: 2, per_page: 20 } },
  ],
  ['A12 plans', () => salesPayService.getPlans(), '/admin/sales/pay/plans', undefined],
  ['A14 version', () => salesPayService.getPlanVersion(3, 7), '/admin/sales/pay/plans/3/versions/7', undefined],
  ['A16 terms', () => salesPayService.getAgentTerms(41), '/admin/sales/pay/agents/41/terms', undefined],
  ['A19 types', () => salesPayService.getPenaltyTypes(), '/admin/sales/pay/penalty-types', undefined],
  [
    'A22 penalties',
    () => salesPayService.getPenalties({ status: 'proposed', agentId: 41, month: '2026-10', page: 1, perPage: 20 }),
    '/admin/sales/pay/penalties',
    { params: { status: 'proposed', agent_id: 41, month: '2026-10', page: 1, per_page: 20 } },
  ],
  [
    'M1 proposals',
    () => salesPayService.getPenaltyProposals({ status: 'proposed', agentId: 77, page: 3, perPage: 20 }),
    '/admin/sales/penalty-proposals',
    { params: { status: 'proposed', agent_id: 77, page: 3, per_page: 20 } },
  ],
];

it.each(READS)('%s reads its route and unwraps data', async (_label, call, url, config) => {
  const result = await call();

  const expectedArgs = config === undefined ? [url] : [url, config];
  expect(api.get).toHaveBeenCalledTimes(1);
  expect(api.get.mock.calls[0]).toEqual(expectedArgs);
  expect(result).toEqual({ echoed: 'get' });
});

const VERSION = {
  effective_month: '2026-11',
  default_tiers: [{ from_unit: 1, mode: 'percent', value: 3 }],
  rates: [{ product_id: 3, tiers: [{ from_unit: 1, mode: 'per_unit', value: 1000 }, { from_unit: 301, mode: 'per_unit', value: 1500 }] }],
  gate_bands: [{ min_pct: 80, multiplier: 1 }, { min_pct: 0, multiplier: 0.5 }],
  gate_min_visits_due: 20,
  new_outlet_bonus: {
    amount: 100000, window_days: 60, prior_customer_lookback_days: 180,
    min_orders_with_total: 2, min_combined_total: 300000, min_orders_any_amount: 5,
  },
  note: 'November rates',
};
const PENALTY = {
  agent_user_id: 41, penalty_type_id: 2, incident_date: '2026-10-14', reason: 'Skipped the route', evidence: 'Route log',
};

const MUTATIONS = [
  ['A0 start', () => salesPayService.startPay({ month: '2026-10', isShadow: true }), 'post', '/admin/sales/pay/start', { month: '2026-10', is_shadow: true }],
  ['A3 close', () => salesPayService.closePeriod('2026-10'), 'post', '/admin/sales/pay/periods/2026-10/close', {}],
  ['A4 recalculate', () => salesPayService.recalculatePeriod('2026-10'), 'post', '/admin/sales/pay/periods/2026-10/recalculate', {}],
  ['A5 approve', () => salesPayService.approvePeriod('2026-10'), 'post', '/admin/sales/pay/periods/2026-10/approve', {}],
  ['A6 mark paid', () => salesPayService.markPeriodPaid('2026-10', '2026-11-05'), 'post', '/admin/sales/pay/periods/2026-10/mark-paid', { paid_on: '2026-11-05' }],
  [
    'A7 holidays',
    () => salesPayService.setHolidays('2026-10', [{ date: '2026-10-01', note: 'Teachers day' }]),
    'put', '/admin/sales/pay/periods/2026-10/holidays', { days: [{ date: '2026-10-01', note: 'Teachers day' }] },
  ],
  [
    'A10 unpaid days',
    () => salesPayService.setUnpaidDays('2026-10', 41, [{ date: '2026-10-14', note: 'sick' }]),
    'put', '/admin/sales/pay/periods/2026-10/agents/41/unpaid-days', { days: [{ date: '2026-10-14', note: 'sick' }] },
  ],
  // The Record-repayment door (Q15): the month is the published `nets_in`, 2026-12 here.
  [
    'A11 adjustment',
    () => salesPayService.createAdjustment('2026-12', 41, { amount: 50000, reason: 'Repaid offline' }),
    'post', '/admin/sales/pay/periods/2026-12/agents/41/adjustments', { amount: 50000, reason: 'Repaid offline' },
  ],
  ['A13 plan', () => salesPayService.createPlan({ name: 'Standard', version: VERSION }), 'post', '/admin/sales/pay/plans', { name: 'Standard', version: VERSION }],
  ['A15 version', () => salesPayService.createPlanVersion(3, VERSION), 'post', '/admin/sales/pay/plans/3/versions', VERSION],
  [
    'A17 terms',
    () => salesPayService.addAgentTerms(41, { effective_month: '2026-11', base_salary: 3000000, plan_id: 3, note: null }),
    'post', '/admin/sales/pay/agents/41/terms', { effective_month: '2026-11', base_salary: 3000000, plan_id: 3, note: null },
  ],
  [
    'A18 employment',
    () => salesPayService.setEmployment(41, { start: '2026-06-01', end: null }),
    'put', '/admin/sales/pay/agents/41/employment', { start: '2026-06-01', end: null },
  ],
  [
    'A20 type',
    () => salesPayService.createPenaltyType({ names: { en: 'Late', uz: 'Kechikish', ru: 'Опоздание' }, default_amount: 50000 }),
    'post', '/admin/sales/pay/penalty-types', { names: { en: 'Late', uz: 'Kechikish', ru: 'Опоздание' }, default_amount: 50000 },
  ],
  ['A21 type update', () => salesPayService.updatePenaltyType(2, { is_active: false }), 'patch', '/admin/sales/pay/penalty-types/2', { is_active: false }],
  ['A23 penalty', () => salesPayService.createPenalty({ ...PENALTY, amount: 70000 }), 'post', '/admin/sales/pay/penalties', { ...PENALTY, amount: 70000 }],
  ['A24 confirm', () => salesPayService.confirmPenalty(17, { amount: 60000 }), 'post', '/admin/sales/pay/penalties/17/confirm', { amount: 60000 }],
  ['A25 reject', () => salesPayService.rejectPenalty(17, 'Not his route'), 'post', '/admin/sales/pay/penalties/17/reject', { note: 'Not his route' }],
  ['A26 cancel', () => salesPayService.cancelPenalty(17, 'Entered twice'), 'post', '/admin/sales/pay/penalties/17/cancel', { note: 'Entered twice' }],
  ['M2 propose', () => salesPayService.proposePenalty(PENALTY), 'post', '/admin/sales/penalty-proposals', PENALTY],
];

it.each(MUTATIONS)('%s posts exactly its body and names the pay refusals', async (_label, call, verb, url, body) => {
  const result = await call();

  expect(api[verb]).toHaveBeenCalledTimes(1);
  expect(api[verb].mock.calls[0]).toEqual([url, body, HANDLED]);
  expect(result).toEqual({ echoed: verb });
});

it('passes the plan body through as the very object the modal built (D-BANDS)', async () => {
  // The modal's `versionPayload` is the one place the tier shape is built (§6.4); the service
  // rebuilds nothing, so a v4 key can never reappear on the way out.
  await salesPayService.createPlanVersion(3, VERSION);
  await salesPayService.createPlan({ name: 'Standard', version: VERSION });

  expect(api.post.mock.calls[0][1]).toBe(VERSION);
  expect(api.post.mock.calls[1][1].version).toBe(VERSION);
});

it('A0 sends is_shadow false when the trial box is left unticked (the default since 2026-10-07)', async () => {
  await salesPayService.startPay({ month: '2026-10', isShadow: false });

  expect(api.post.mock.calls).toEqual([['/admin/sales/pay/start', { month: '2026-10', is_shadow: false }, HANDLED]]);
});

it('covers every method the service has, once', () => {
  const methods = Object.getOwnPropertyNames(Object.getPrototypeOf(salesPayService)).filter((name) => name !== 'constructor');
  // One row per route (C29): 10 reads + 19 mutations. A method added without a row fails here.
  expect(methods).toHaveLength(READS.length + MUTATIONS.length);
});
