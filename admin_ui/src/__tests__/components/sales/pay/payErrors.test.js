import { payErrorText } from '../../../../components/sales/pay/payErrors';

// The seeded English row for the orders sentence (scripts/seed_ui_sales_translations.py), then
// i18next's positional default, with `{{token}}` interpolation, so the sentence an admin reads is
// what is asserted, placeholders filled.
const SEEDED = {
  'sales_agents:pay.error.sales_pay_sync_incomplete': 'Some orders could not be synced ({{orders}}). Fix them and try again.',
};
const t = (key, opts) => {
  const options = typeof opts === 'string' ? {} : (opts || {});
  const text = SEEDED[key] || (typeof opts === 'string' ? opts : opts?.defaultValue) || key;
  return text.replace(/\{\{(\w+)\}\}/g, (_, token) => String(options[token] ?? ''));
};

const refusal = (details) => ({
  response: {
    status: 409,
    data: {
      success: false,
      error_code: 'SALES_PAY_SYNC_INCOMPLETE',
      message: 'Some earnings could not be synced; nothing was frozen',
      details,
    },
  },
});

describe('payErrorText: SALES_PAY_SYNC_INCOMPLETE (final-review M3)', () => {
  it('says another sync was running when the close lost only races and names no order', () => {
    const text = payErrorText(t, refusal({ orders: [], agents: [], reason: 'concurrent' }));

    expect(text).toBe('Another sync was running at the same moment. Nothing was frozen; try again.');
  });

  it('names the orders that failed otherwise', () => {
    const text = payErrorText(t, refusal({ orders: [401, 402], agents: [41] }));

    expect(text).toBe('Some orders could not be synced (401, 402). Fix them and try again.');
  });
});

describe('payErrorText: a tier refusal (D-BANDS)', () => {
  const planRefusal = (details) => ({
    response: { status: 400, data: { success: false, error_code: 'SALES_PAY_PLAN_INVALID', message: 'Invalid plan', details } },
  });

  it('names the tier and the product by the name the editor lists', () => {
    const text = payErrorText(
      t,
      planRefusal({ field: 'rates.tiers.value', reason: 'not_whole', tier: 2, product_id: 9 }),
      { productNames: new Map([[9, 'Juice 1 L']]) },
    );

    expect(text).toBe('Invalid plan — tier 2 · Juice 1 L');
  });

  it('falls back to the product id, and adds nothing to a refusal without a tier', () => {
    expect(payErrorText(t, planRefusal({ field: 'rates.tiers.value', reason: 'not_whole', tier: 2, product_id: 9 })))
      .toBe('Invalid plan — tier 2 · #9');
    expect(payErrorText(t, planRefusal({ field: 'gate_bands', reason: 'required' }))).toBe('Invalid plan');
  });
});
