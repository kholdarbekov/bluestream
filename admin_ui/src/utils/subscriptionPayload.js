// Pure transforms between the admin subscription API and the antd form: a published admin detail into form
// values (`mapDetailToForm`), and form values into the payload the form submits (`buildSubscriptionPayload`).
// Kept separate from the page so they can be unit-tested deterministically, and so the endpoint test replays
// the body they produce (tests/fixtures/admin_subscription_edit_bodies.json) instead of a copy of them.
import dayjs from 'dayjs';

// ONE `frequency` drives a subscription (spec 2026-10-23). The delivery day is a weekday (0 = Monday,
// the backend's convention) for weekly and biweekly, a day of the month for monthly, and nothing for
// daily: only the field that frequency uses is sent, the other is cleared.

const WEEKDAY_FREQUENCIES = ['weekly', 'biweekly'];
const MONTH_DAY_FREQUENCIES = ['monthly'];

export const usesWeekday = (frequency) => WEEKDAY_FREQUENCIES.includes(frequency);
export const usesDayOfMonth = (frequency) => MONTH_DAY_FREQUENCIES.includes(frequency);

// The slot a published admin detail names: its id, or the nested slot's for an older payload.
export const detailSlotId = (d) => d.delivery_time_slot_id ?? d.delivery_time_slot?.id;

// "Any time" is a choice of its own (spec 2026-10-27 §6.2), never an empty field: the form holds this value for a
// subscription with no slot, and the payload sends it as an explicit null, which the backend reads as "clear the slot".
export const ANY_TIME = 'any';

export const mapDetailToForm = (d) => ({
  name: d.name,
  description: d.description,
  frequency: d.frequency,
  delivery_day_of_week: d.delivery_day_of_week,
  delivery_day_of_month: d.delivery_day_of_month,
  delivery_time_slot_id: detailSlotId(d) ?? ANY_TIME,
  delivery_address_id: d.delivery_address_id,
  payment_method: d.payment_method,
  auto_renew: d.auto_renew,
  discount_percentage: d.discount_percentage,
  loyalty_points_multiplier: d.loyalty_points_multiplier,
  start_date: d.start_date ? dayjs(d.start_date) : null,
  end_date: d.end_date ? dayjs(d.end_date) : null,
  billing_amount: d.billing_amount,
  next_delivery_date: d.next_delivery_date ? dayjs(d.next_delivery_date) : null,
  override_edit_any_status: false,
  override_manual_billing_amount: false,
  override_manual_billing_dates: false,
});

const toIso = (d) => (d && typeof d.toISOString === 'function' ? d.toISOString() : undefined);
// The next delivery is a calendar DATE, never an instant.
const toDate = (d) => (d && typeof d.format === 'function' ? d.format('YYYY-MM-DD') : undefined);

export function buildSubscriptionPayload(values, { isEdit } = {}) {
  const base = {
    name: values.name,
    description: values.description,
    frequency: values.frequency,
    delivery_day_of_week: usesWeekday(values.frequency) ? (values.delivery_day_of_week ?? null) : null,
    delivery_day_of_month: usesDayOfMonth(values.frequency) ? (values.delivery_day_of_month ?? null) : null,
    // Any time -> null; a slot id as chosen; a time not chosen (undefined) is left out of the body.
    delivery_time_slot_id: values.delivery_time_slot_id === ANY_TIME ? null : values.delivery_time_slot_id,
    delivery_address_id: values.delivery_address_id,
    payment_method: values.payment_method,
    auto_payment: values.auto_payment,
    auto_renew: values.auto_renew,
    discount_percentage: values.discount_percentage ?? 0,
    loyalty_points_multiplier: values.loyalty_points_multiplier ?? null,
    start_date: toIso(values.start_date),
    end_date: toIso(values.end_date),
  };

  if (!isEdit) {
    return {
      ...base,
      user_id: values.user_id,
      items: (values.items || []).map((it) => ({
        product_id: it.product_id,
        quantity: it.quantity,
        ...(it.special_instructions ? { special_instructions: it.special_instructions } : {}),
      })),
    };
  }

  const payload = {
    ...base,
    override_edit_any_status: !!values.override_edit_any_status,
    override_manual_billing_amount: !!values.override_manual_billing_amount,
    override_manual_billing_dates: !!values.override_manual_billing_dates,
  };
  if (values.override_manual_billing_amount) {
    payload.billing_amount = values.billing_amount;
  }
  if (values.override_manual_billing_dates) {
    payload.next_delivery_date = toDate(values.next_delivery_date);
  }
  return payload;
}
