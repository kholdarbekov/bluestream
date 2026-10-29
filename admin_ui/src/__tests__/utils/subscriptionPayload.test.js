import fs from 'node:fs';
import path from 'node:path';
import { describe, it, expect } from 'vitest';
import { ANY_TIME, buildSubscriptionPayload, mapDetailToForm } from '../../utils/subscriptionPayload';

// The edit bodies the admin form submits, shared with the endpoint test that PUTs them
// (tests/integration/test_subscription_action_guards.py replays each `body` after checking each `detail` is
// what the admin detail endpoint publishes).
const EDIT_BODIES = JSON.parse(fs.readFileSync(path.resolve(process.cwd(), '..', 'tests/fixtures/admin_subscription_edit_bodies.json'), 'utf8'));

const dateOnly = (s) => ({ format: () => s });

describe('buildSubscriptionPayload', () => {
  it('builds a create payload with user_id and items', () => {
    const values = {
      user_id: 7,
      name: 'Weekly Water',
      description: 'desc',
      frequency: 'weekly',
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
    expect(payload.frequency).toBe('weekly');
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
      frequency: 'weekly',
      delivery_address_id: 3,
      payment_method: 'card',
      auto_payment: false,
      auto_renew: true,
      discount_percentage: 0,
      billing_amount: 99999,
      next_delivery_date: dateOnly('2026-09-01'),
    };
    const payload = buildSubscriptionPayload(values, { isEdit: true });
    expect(payload.override_edit_any_status).toBe(false);
    expect(payload.override_manual_billing_amount).toBe(false);
    expect(payload.override_manual_billing_dates).toBe(false);
    expect(payload).not.toHaveProperty('billing_amount');
    expect(payload).not.toHaveProperty('next_delivery_date');
    // edit payload must NOT carry create-only keys
    expect(payload).not.toHaveProperty('user_id');
    expect(payload).not.toHaveProperty('items');
  });

  it('includes override-gated fields on edit when flags are on', () => {
    const values = {
      name: 'Renamed',
      frequency: 'weekly',
      delivery_address_id: 3,
      payment_method: 'card',
      auto_payment: true,
      auto_renew: true,
      discount_percentage: 0,
      override_manual_billing_amount: true,
      billing_amount: 12345,
      override_manual_billing_dates: true,
      next_delivery_date: dateOnly('2026-09-01'),
    };
    const payload = buildSubscriptionPayload(values, { isEdit: true });
    expect(payload.override_manual_billing_amount).toBe(true);
    expect(payload.billing_amount).toBe(12345);
    expect(payload.override_manual_billing_dates).toBe(true);
    // a calendar DATE, never an instant
    expect(payload.next_delivery_date).toBe('2026-09-01');
    expect(payload).not.toHaveProperty('next_billing_date');
    expect(payload).not.toHaveProperty('last_billing_date');
  });

  const base = {
    name: 'Water', delivery_address_id: 3, payment_method: 'cash', auto_renew: true, user_id: 7,
    delivery_day_of_week: 3, delivery_day_of_month: 15, delivery_time_slot_id: 2,
    items: [{ product_id: 2, quantity: 1 }],
  };

  it.each([
    ['daily', null, null],
    ['weekly', 3, null],
    ['biweekly', 3, null],
    ['monthly', null, 15],
  ])('sends ONE frequency (%s) and only the day that frequency uses', (frequency, weekday, monthDay) => {
    const payload = buildSubscriptionPayload({ ...base, frequency }, { isEdit: false });
    expect(payload.frequency).toBe(frequency);
    expect(payload.delivery_day_of_week).toBe(weekday);
    expect(payload.delivery_day_of_month).toBe(monthDay);
    expect(payload.delivery_time_slot_id).toBe(2);
    ['billing_cycle', 'delivery_frequency', 'next_billing_date', 'last_billing_date'].forEach((old) => {
      expect(payload).not.toHaveProperty(old);
    });
  });
});

describe('Any time (spec 2026-10-27 §6.2)', () => {
  const form = { name: 'W', frequency: 'daily', delivery_address_id: 3, payment_method: 'cash', auto_renew: true };

  it('a stored subscription without a slot opens on Any time, and Any time is sent as an explicit null', () => {
    expect(ANY_TIME).toBe('any');
    expect(mapDetailToForm({ name: 'W', delivery_time_slot_id: null }).delivery_time_slot_id).toBe(ANY_TIME);
    expect(mapDetailToForm({ name: 'W' }).delivery_time_slot_id).toBe(ANY_TIME);
    expect(mapDetailToForm({ name: 'W', delivery_time_slot: { id: 4 } }).delivery_time_slot_id).toBe(4);
    [true, false].forEach((isEdit) => {
      const payload = buildSubscriptionPayload({ ...form, delivery_time_slot_id: ANY_TIME }, { isEdit });
      expect(payload).toHaveProperty('delivery_time_slot_id', null);
      expect(JSON.parse(JSON.stringify(payload)).delivery_time_slot_id).toBeNull();
    });
    expect(buildSubscriptionPayload({ ...form, delivery_time_slot_id: 6 }, { isEdit: true }).delivery_time_slot_id).toBe(6);
  });

  it('a time not chosen is not sent at all', () => {
    [true, false].forEach((isEdit) => {
      const payload = buildSubscriptionPayload({ ...form, delivery_time_slot_id: undefined }, { isEdit });
      expect(JSON.parse(JSON.stringify(payload))).not.toHaveProperty('delivery_time_slot_id');
    });
  });
});

// Re-review tests-G11a: the endpoint test no longer keeps its own copy of "what the Edit form sends". The shipped
// mapDetailToForm and buildSubscriptionPayload turn each fixture detail into the body the form submits, and that
// body is the one the Python test PUTs.
describe('the admin Edit form body (tests/fixtures/admin_subscription_edit_bodies.json)', () => {
  // The keys mapDetailToForm reads from a detail, recorded as it reads them.
  const keysRead = (detail) => {
    const read = new Set();
    mapDetailToForm(new Proxy(detail, { get: (target, key) => { read.add(key); return Reflect.get(target, key); } }));
    return [...read].sort();
  };

  it.each(Object.entries(EDIT_BODIES.details))('detail %s holds exactly the keys the form reads', (_name, detail) => {
    expect(Object.keys(detail).sort()).toEqual(keysRead(detail));
  });

  it.each(EDIT_BODIES.cases)('$name', ({ detail, edit, body }) => {
    const values = { ...mapDetailToForm(EDIT_BODIES.details[detail]), ...edit };
    // What reaches the wire: axios JSON-encodes the payload, which drops the undefined keys.
    expect(JSON.parse(JSON.stringify(buildSubscriptionPayload(values, { isEdit: true })))).toEqual(body);
  });
});
