// Pure transform from antd form values to an admin subscription API payload.
// Kept separate from the page so it can be unit-tested deterministically.

const toIso = (d) => (d && typeof d.toISOString === 'function' ? d.toISOString() : undefined);

export function buildSubscriptionPayload(values, { isEdit } = {}) {
  const base = {
    name: values.name,
    description: values.description,
    billing_cycle: values.billing_cycle,
    delivery_frequency: values.delivery_frequency,
    delivery_day_of_week: values.delivery_day_of_week ?? null,
    delivery_day_of_month: values.delivery_day_of_month ?? null,
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
    payload.next_billing_date = toIso(values.next_billing_date);
    payload.last_billing_date = toIso(values.last_billing_date);
  }
  return payload;
}
