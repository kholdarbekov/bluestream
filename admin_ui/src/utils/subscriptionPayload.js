// Pure transform from antd form values to an admin subscription API payload.
// Kept separate from the page so it can be unit-tested deterministically.
//
// ONE `frequency` drives a subscription (spec 2026-10-23). The delivery day is a weekday (0 = Monday,
// the backend's convention) for weekly and biweekly, a day of the month for monthly, and nothing for
// daily: only the field that frequency uses is sent, the other is cleared.

const WEEKDAY_FREQUENCIES = ['weekly', 'biweekly'];
const MONTH_DAY_FREQUENCIES = ['monthly'];

export const usesWeekday = (frequency) => WEEKDAY_FREQUENCIES.includes(frequency);
export const usesDayOfMonth = (frequency) => MONTH_DAY_FREQUENCIES.includes(frequency);

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
    delivery_time_slot_id: values.delivery_time_slot_id ?? null,
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
