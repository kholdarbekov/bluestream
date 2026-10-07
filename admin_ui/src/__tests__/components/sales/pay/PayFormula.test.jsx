import React from 'react';
import { render, screen, fireEvent, within } from '@testing-library/react';

import PayFormula from '../../../../components/sales/pay/PayFormula';

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

// A8 `summary.commission.products` (§5.2, D-BANDS). Illustration B's October: 19L on its own two
// tiers, juice on the default 2%. Σ tier amounts = the product's total; Σ totals = gross 850,000.
const NAME_19L = { en: '19L', uz: '19L', ru: '19 л' };
const WATER_19L = {
  product_id: 3, product_name: NAME_19L, uses_default_tiers: false, units: 500, total: 600000.0,
  tiers: [
    { from_unit: 1, to_unit: 300, units: 300, mode: 'per_unit', value: 1000.0, net: 6000000.0, amount_full: 300000.0, amount: 300000.0 },
    { from_unit: 301, to_unit: null, units: 200, mode: 'per_unit', value: 1500.0, net: 4000000.0, amount_full: 300000.0, amount: 300000.0 },
  ],
  next_tier: null,
};
const JUICE = {
  product_id: 9, product_name: { en: 'Juice 1 L', uz: 'Sharbat 1 L', ru: 'Сок 1 л' }, uses_default_tiers: true, units: 500, total: 250000.0,
  tiers: [{ from_unit: 1, to_unit: null, units: 500, mode: 'percent', value: 2.0, net: 12500000.0, amount_full: 250000.0, amount: 250000.0 }],
  next_tier: null,
};
// September's late group (a shape sample: Illustration B fixes only the −20,000; one order of
// the 400 units brought in less money, so 400 × 1,500 = 600,000 counts 595,000).
const SEPTEMBER_19L = {
  product_id: 3, product_name: NAME_19L, units_before: 410, units: 400, total_before: 615000.0, total: 595000.0, change: -20000.0,
  tiers_before: [{ from_unit: 1, to_unit: null, units: 410, mode: 'per_unit', value: 1500.0, net: 8200000.0, amount_full: 615000.0, amount: 615000.0 }],
  tiers: [{ from_unit: 1, to_unit: null, units: 400, mode: 'per_unit', value: 1500.0, net: 8000000.0, amount_full: 600000.0, amount: 595000.0 }],
};
// The carry case's November (Example A's single 19L tier at 1,500, spec E.3 M3): 300 × 1,500.
const NOVEMBER_19L = {
  product_id: 3, product_name: NAME_19L, uses_default_tiers: false, units: 300, total: 450000.0,
  tiers: [{ from_unit: 1, to_unit: null, units: 300, mode: 'per_unit', value: 1500.0, net: 6000000.0, amount_full: 450000.0, amount: 450000.0 }],
  next_tier: null,
};

// Illustration B of spec §1.2, as the frozen, closed statement A8 publishes (§5.2).
const BANDS = [{ min_pct: 80.0, multiplier: 1.0 }, { min_pct: 60.0, multiplier: 0.8 }, { min_pct: 0.0, multiplier: 0.5 }];
const statement = (summary, inputs = {}) => ({
  month: '2026-10', status: 'closed', is_estimate: false, is_shadow: false, revision: 1, can_edit_inputs: true,
  self_decided: false, self_decisions: [],
  agent: { user_id: 41, name: 'Aziz K.', phone: '+998901234574' },
  plan: {
    plan_id: 1, plan_name: 'Standard', version_id: 3, version_no: 1, effective_month: '2026-10',
    gate_bands: BANDS, gate_min_due: 20,
  },
  inputs: {
    base_salary: 3000000.0, employment_start: '2026-06-01', employment_end: null, working_days: 30, worked_days: 28,
    holidays: [{ date: '2026-10-01', note: 'Teachers day' }],
    unpaid_days: [{ date: '2026-10-14', note: 'sick' }, { date: '2026-10-15', note: 'sick' }],
    short_visit_seconds: 60, geofence_radius_m: 250,
    ...inputs,
  },
  summary: {
    base_amount: 2800000.0,
    commission: { gross: 850000.0, orders: 31, after_gate: 680000.0, products: [WATER_19L, JUICE] },
    late: [{
      earned_month: '2026-09', plan_version_id: 2, gross: -20000.0, multiplier: 1.0, source: 'statement', after_gate: -20000.0,
      counted: true, products: [SEPTEMBER_19L],
    }],
    gate: {
      compliance_pct: 75.5, visits_due: 212, visits_counted: 160, multiplier: 0.8,
      rule: 'band', band: { min_pct: 60.0, multiplier: 0.8 }, provisional: false,
    },
    gated_commission: 660000.0,
    new_outlets: { amount: 200000.0, count: 2 },
    adjustments: 50000.0, penalties: 150000.0, variable: 760000.0,
    carry_in: { amount: 0.0, from_month: null, source: null },
    gross_total: 3560000.0, total: 3560000.0, carry_out: 0.0, owed: 0.0,
    ...summary,
  },
  days: [], day_statuses: [], not_counted_reasons: [], new_outlets: [], penalties: [], adjustments: [],
  line_kinds: [], line_counts: {},
});

const ILLUSTRATION_B = statement({});

// A shortfall that stays with the company (employed next month): base 200,000 + variable
// −300,000 = gross −100,000; paid 0; −100,000 carried forward (C1).
const SHORTFALL = statement({
  commission: { gross: 0.0, orders: 0, after_gate: 0.0, products: [] }, late: [], gated_commission: 0.0,
  new_outlets: { amount: 0.0, count: 0 }, adjustments: 0.0, penalties: 300000.0, variable: -300000.0,
  base_amount: 200000.0, gross_total: -100000.0, total: 0.0, carry_out: -100000.0, owed: 0.0,
});

// The same shortfall for a leaver (employment ends in the month): owed, not carried (I-28).
const OWED = statement({
  commission: { gross: 0.0, orders: 0, after_gate: 0.0, products: [] }, late: [], gated_commission: 0.0,
  new_outlets: { amount: 0.0, count: 0 }, adjustments: 0.0, penalties: 250000.0, variable: -250000.0,
  base_amount: 200000.0, gross_total: -50000.0, total: 0.0, carry_out: 0.0, owed: 50000.0,
});

// November after the October shortfall: Base 3,000,000 · After discipline 450,000 ·
// Carried in −100,000 · Total 3,350,000 (§10.6).
const NOVEMBER = statement({
  commission: { gross: 450000.0, orders: 20, after_gate: 450000.0, products: [NOVEMBER_19L] }, late: [], gated_commission: 450000.0,
  new_outlets: { amount: 0.0, count: 0 }, adjustments: 0.0, penalties: 0.0, variable: 450000.0,
  base_amount: 3000000.0, carry_in: { amount: -100000.0, from_month: '2026-10', source: 'carry_forward' },
  gross_total: 3350000.0, total: 3350000.0,
});

// December netting the leaver's outstanding balance (Q15, I-28).
const OWED_IN = statement({
  late: [], carry_in: { amount: -50000.0, from_month: '2026-11', source: 'owed' },
  gross_total: 3510000.0, total: 3510000.0,
});

const row = (label) => screen.getByText(label, { selector: 'th, th *' }).closest('tr');

it('prints every step of Illustration B from the published fields', () => {
  render(<PayFormula statement={ILLUSTRATION_B} onOpenTab={vi.fn()} />);

  expect(row('Plan')).toHaveTextContent('Standard · version 1 from 10.2026');
  expect(row('Plan')).toHaveTextContent('≥80% ×1 · ≥60% ×0.8 · ≥0% ×0.5');
  expect(row('Base')).toHaveTextContent('3,000,000 × 28 / 30 = 2,800,000');
  expect(row('Base')).toHaveTextContent('Holiday 01.10.2026');
  expect(row('Base')).toHaveTextContent('Unpaid 14.10.2026');
  expect(row('Commission')).toHaveTextContent('31 orders: 850,000');
  expect(row('Plan vs fact')).toHaveTextContent('160 / 212 = 75.5% → band ≥60% → ×0.8');
  expect(row('Commission after discipline')).toHaveTextContent('680,000');
  expect(row('Late correction')).toHaveTextContent('from 09.2026: −20,000 × 1 = −20,000');
  expect(row('New outlets')).toHaveTextContent('2 = 200,000');
  expect(row('Adjustments')).toHaveTextContent('+50,000');
  expect(row('Penalties')).toHaveTextContent('−150,000');
  expect(row('Variable')).toHaveTextContent('680,000 − 20,000 + 200,000 + 50,000 − 150,000 = 760,000');
  expect(row('Gross')).toHaveTextContent('2,800,000 + 760,000 + 0 = 3,560,000');
  expect(row('Total')).toHaveTextContent('max(0, 3,560,000) = 3,560,000');
  expect(screen.getByText('Short visit under 60 s, radius 250 m (frozen with this statement)')).toBeInTheDocument();

  // No carry-in, no shortfall: none of the three conditional lines is drawn.
  expect(screen.queryByText(/Shortfall from/)).toBeNull();
  expect(screen.queryByText(/Owed from/)).toBeNull();
  expect(screen.queryByText(/will be deducted next month/)).toBeNull();
  expect(screen.queryByText(/Owed by the agent/)).toBeNull();
});

it('prints a negative variable with a minus and says where the shortfall goes', () => {
  render(<PayFormula statement={SHORTFALL} onOpenTab={vi.fn()} />);

  expect(row('Variable')).toHaveTextContent('0 + 0 + 0 − 300,000 = −300,000');
  expect(row('Gross')).toHaveTextContent('200,000 − 300,000 + 0 = −100,000');
  expect(row('Total')).toHaveTextContent('max(0, −100,000) = 0');
  expect(row('Total')).toHaveTextContent('100,000 will be deducted next month (the total cannot go below 0)');
  expect(screen.queryByText(/Owed by the agent/)).toBeNull();
});

it('names an owed balance only when the statement publishes one', () => {
  render(<PayFormula statement={OWED} onOpenTab={vi.fn()} />);

  expect(row('Total')).toHaveTextContent('max(0, −50,000) = 0');
  expect(row('Total')).toHaveTextContent(
    "Owed by the agent: 50,000 (employment does not continue into the next month; the agent's next statement deducts it)",
  );
  expect(screen.queryByText(/will be deducted next month/)).toBeNull();
});

it('draws the carried-in row only for a negative carry-in, labelled by its source', () => {
  const { unmount } = render(<PayFormula statement={NOVEMBER} onOpenTab={vi.fn()} />);
  expect(row('Shortfall from 10.2026')).toHaveTextContent('−100,000');
  expect(row('Gross')).toHaveTextContent('3,000,000 + 450,000 − 100,000 = 3,350,000');
  expect(row('Total')).toHaveTextContent('max(0, 3,350,000) = 3,350,000');
  unmount();

  render(<PayFormula statement={OWED_IN} onOpenTab={vi.fn()} />);
  expect(row('Owed from 11.2026')).toHaveTextContent('−50,000');
  expect(screen.queryByText(/Shortfall from/)).toBeNull();
});

it('reads a gate below the minimum as "no reduction"', () => {
  const statementBelowMin = statement({
    gate: {
      compliance_pct: 50.0, visits_due: 12, visits_counted: 6, multiplier: 1.0,
      rule: 'below_min_due', band: null, provisional: false,
    },
  });
  render(<PayFormula statement={statementBelowMin} onOpenTab={vi.fn()} />);

  expect(row('Plan vs fact')).toHaveTextContent('fewer than 20 visits due, no reduction');
  expect(row('Plan vs fact')).not.toHaveTextContent('band');
});

it('marks a late group whose discipline is this month\'s', () => {
  const current = statement({
    late: [{ earned_month: '2026-09', gross: -20000.0, multiplier: 0.8, source: 'current', after_gate: -16000.0, counted: true, products: [SEPTEMBER_19L] }],
  });
  render(<PayFormula statement={current} onOpenTab={vi.fn()} />);

  expect(row('Late correction')).toHaveTextContent(
    "from 09.2026: −20,000 × 0.8 = −16,000 (this month's discipline, no statement for 09.2026)",
  );
});

it('links commission, plan vs fact, new outlets and penalties to their tabs', () => {
  const onOpenTab = vi.fn();
  render(<PayFormula statement={ILLUSTRATION_B} onOpenTab={onOpenTab} />);

  fireEvent.click(screen.getByRole('button', { name: '31 orders: 850,000' }));
  fireEvent.click(screen.getByRole('button', { name: /160 \/ 212/ }));
  fireEvent.click(screen.getByRole('button', { name: '2 = 200,000' }));
  fireEvent.click(screen.getByRole('button', { name: '−150,000' }));

  expect(onOpenTab.mock.calls).toEqual([['orders'], ['days'], ['new_outlets'], ['penalties']]);
});

// ---- D-BANDS: products and tiers under the Commission row (§6.2, §10.6) ----

// Illustration C's October estimate at T-TIER-5 (C paid 50%): A 300 / B 250 / C 150 × 19L on
// T-TIER-1's schedule (1,000 to 500; 1,500 from 501; 2,000 from 1,001). The 501–1,000 tier holds
// B's 50 and C's 150 units: 300,000 at full money, 75,000 + 225,000 × 0.5 = 187,500 counted.
// Total 500,000 + 187,500 = 687,500; the next tier starts 1,001 − 700 = 301 units away.
const OCTOBER_ESTIMATE = {
  ...statement({
    commission: {
      gross: 687500.0, orders: 3, after_gate: 687500.0,
      products: [{
        product_id: 3, product_name: NAME_19L, uses_default_tiers: false, units: 700, total: 687500.0,
        tiers: [
          { from_unit: 1, to_unit: 500, units: 500, mode: 'per_unit', value: 1000.0, net: 10000000.0, amount_full: 500000.0, amount: 500000.0 },
          { from_unit: 501, to_unit: 1000, units: 200, mode: 'per_unit', value: 1500.0, net: 4000000.0, amount_full: 300000.0, amount: 187500.0 },
        ],
        next_tier: { from_unit: 1001, units_to_go: 301, mode: 'per_unit', value: 2000.0 },
      }],
    },
    late: [],
  }),
  status: 'open',
  is_estimate: true,
};

// Illustration C (b), November: A (300 units) reversed after October closed at ×0.8. October's
// 700 units paid 500 × 1,000 + 200 × 1,500 = 800,000; the 400 left pay 400 × 1,000 = 400,000.
const NOVEMBER_LATE = statement({
  late: [{
    earned_month: '2026-10', plan_version_id: 3, gross: -400000.0, multiplier: 0.8, source: 'statement', after_gate: -320000.0,
    counted: true, products: [{
      product_id: 3, product_name: NAME_19L, units_before: 700, units: 400, total_before: 800000.0, total: 400000.0, change: -400000.0,
      tiers_before: [
        { from_unit: 1, to_unit: 500, units: 500, mode: 'per_unit', value: 1000.0, net: 10000000.0, amount_full: 500000.0, amount: 500000.0 },
        { from_unit: 501, to_unit: 1000, units: 200, mode: 'per_unit', value: 1500.0, net: 4000000.0, amount_full: 300000.0, amount: 300000.0 },
      ],
      tiers: [{ from_unit: 1, to_unit: 500, units: 400, mode: 'per_unit', value: 1000.0, net: 8000000.0, amount_full: 400000.0, amount: 400000.0 }],
    }],
  }],
});

it('prints each product and its tiers from the published fields', () => {
  render(<PayFormula statement={ILLUSTRATION_B} onOpenTab={vi.fn()} />);

  const water = within(screen.getByTestId('pay-product-3'));
  expect(water.getByText('19L: 500 units · 600,000')).toBeInTheDocument();
  expect(water.getByText('1–300: 300 × 1,000 = 300,000')).toBeInTheDocument();
  expect(water.getByText('301+: 200 × 1,500 = 300,000')).toBeInTheDocument();
  expect(water.queryByText('default tiers')).toBeNull();

  const juice = within(screen.getByTestId('pay-product-9'));
  expect(juice.getByText('Juice 1 L: 500 units · 250,000')).toBeInTheDocument();
  expect(juice.getByText('default tiers')).toBeInTheDocument();
  expect(juice.getByText('1+: 2% of 12,500,000 = 250,000')).toBeInTheDocument();

  // Both products sit under the Commission row. Every tier paid in full, and a closed statement
  // publishes no next tier, so neither line is drawn.
  expect(row('Commission')).toHaveTextContent('31 orders: 850,000');
  expect(row('Commission')).toHaveTextContent('Juice 1 L: 500 units · 250,000');
  expect(screen.queryByText(/less money received/)).toBeNull();
  expect(screen.queryByText(/Next tier/)).toBeNull();
});

it('adds "counted" only to a tier whose amount fell and names the next tier of an open estimate', () => {
  render(<PayFormula statement={OCTOBER_ESTIMATE} onOpenTab={vi.fn()} />);

  const water = within(screen.getByTestId('pay-product-3'));
  expect(water.getByText('19L: 700 units · 687,500')).toBeInTheDocument();
  // The whole text of the first tier's line: it paid in full, so nothing follows its formula.
  expect(water.getByText('1–500: 500 × 1,000 = 500,000')).toBeInTheDocument();
  expect(water.getByText('501–1,000: 200 × 1,500 = 300,000 · counted 187,500 (less money received)')).toBeInTheDocument();
  expect(water.getByText('Next tier from unit 1,001 (2,000 UZS/unit): 301 more units')).toBeInTheDocument();
});

it('lists the products a late group moved, with their units and totals before and after', () => {
  render(<PayFormula statement={NOVEMBER_LATE} onOpenTab={vi.fn()} />);

  expect(row('Late correction')).toHaveTextContent('from 10.2026: −400,000 × 0.8 = −320,000');
  expect(row('Late correction')).toHaveTextContent('19L: 700 → 400 units, 800,000 → 400,000 (−400,000)');
});

// V5-T5-R1: a trial/shadow month's discipline is not counted (I-16), so a late group earned in it
// gates to 0 with no history behind the 0. The already-seeded shadow tag explains it; no new copy.
it('tags a shadow-month late group as not counted, explaining its × 1.0 = 0', () => {
  const shadowLate = statement({
    late: [{
      earned_month: '2026-09', plan_version_id: 2, gross: -20000.0, multiplier: 1.0, source: 'statement', after_gate: 0.0,
      counted: false, products: [SEPTEMBER_19L],
    }],
  });
  render(<PayFormula statement={shadowLate} onOpenTab={vi.fn()} />);

  expect(row('Late correction')).toHaveTextContent('from 09.2026: −20,000 × 1 = 0');
  expect(within(row('Late correction')).getByText('not counted (trial month)')).toBeInTheDocument();
});
