import { describe, it, expect } from 'vitest';
import { buildSubscriptionPayload } from '../../utils/subscriptionPayload';

const isoDate = (s) => ({ toISOString: () => s });

describe('buildSubscriptionPayload', () => {
  it('builds a create payload with user_id and items', () => {
    const values = {
      user_id: 7,
      name: 'Weekly Water',
      description: 'desc',
      billing_cycle: 'monthly',
      delivery_frequency: 'weekly',
      delivery_day_of_week: 1,
      delivery_address_id: 3,
      payment_method: 'cash',
      auto_payment: true,
      auto_renew: true,
      discount_percentage: 10,
      items: [
        { product_id: 2, quantity: 4, special_instructions: 'cold' },
        { product_id: 5, quantity: 1 },
      ],
    };
    const payload = buildSubscriptionPayload(values, { isEdit: false });
    expect(payload.user_id).toBe(7);
    expect(payload.billing_cycle).toBe('monthly');
    expect(payload.delivery_day_of_week).toBe(1);
    expect(payload.discount_percentage).toBe(10);
    expect(payload.items).toEqual([
      { product_id: 2, quantity: 4, special_instructions: 'cold' },
      { product_id: 5, quantity: 1 },
    ]);
    // create payload must NOT carry override flags
    expect(payload).not.toHaveProperty('override_edit_any_status');
    expect(payload).not.toHaveProperty('billing_amount');
  });

  it('omits override-gated fields on edit when flags are off', () => {
    const values = {
      name: 'Renamed',
      billing_cycle: 'monthly',
      delivery_frequency: 'weekly',
      delivery_address_id: 3,
      payment_method: 'card',
      auto_payment: false,
      auto_renew: true,
      discount_percentage: 0,
      billing_amount: 99999,
      next_billing_date: isoDate('2026-09-01T09:00:00.000Z'),
    };
    const payload = buildSubscriptionPayload(values, { isEdit: true });
    expect(payload.override_edit_any_status).toBe(false);
    expect(payload.override_manual_billing_amount).toBe(false);
    expect(payload.override_manual_billing_dates).toBe(false);
    expect(payload).not.toHaveProperty('billing_amount');
    expect(payload).not.toHaveProperty('next_billing_date');
    // edit payload must NOT carry create-only keys
    expect(payload).not.toHaveProperty('user_id');
    expect(payload).not.toHaveProperty('items');
  });

  it('includes override-gated fields on edit when flags are on', () => {
    const values = {
      name: 'Renamed',
      billing_cycle: 'monthly',
      delivery_frequency: 'weekly',
      delivery_address_id: 3,
      payment_method: 'card',
      auto_payment: true,
      auto_renew: true,
      discount_percentage: 0,
      override_manual_billing_amount: true,
      billing_amount: 12345,
      override_manual_billing_dates: true,
      next_billing_date: isoDate('2026-09-01T09:00:00.000Z'),
      last_billing_date: isoDate('2026-08-01T09:00:00.000Z'),
    };
    const payload = buildSubscriptionPayload(values, { isEdit: true });
    expect(payload.override_manual_billing_amount).toBe(true);
    expect(payload.billing_amount).toBe(12345);
    expect(payload.override_manual_billing_dates).toBe(true);
    expect(payload.next_billing_date).toBe('2026-09-01T09:00:00.000Z');
    expect(payload.last_billing_date).toBe('2026-08-01T09:00:00.000Z');
  });
});
