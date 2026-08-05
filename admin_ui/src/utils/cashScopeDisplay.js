// Display helpers for scope-aware cash attribution (Phase 2c, spec §9).
//
// The money engine is scope-aware: one cash collection can settle a coworker's
// or a linked sibling's order. The admin surfaces therefore have to say WHOSE
// money paid WHOSE debt, and under which scope — otherwise a workplace
// collection is indistinguishable from personal cash.

const SCOPE_PERSONAL = 'personal';
const SCOPE_PLACE = 'place';
const SCOPE_CLUSTER = 'cluster';

/**
 * Human label for a scoped allocation / collection event.
 *
 * Accepts anything carrying the admin-arm keys `scope_type`,
 * `scope_group_label` (place events) and the dual audit stamps
 * `source_customer_id` -> `beneficiary_user_id`. Returns '' for personal or
 * legacy rows so unlinked + ungrouped customers render exactly as today.
 *
 * @param {object|null} allocation timeline entry, session event, or allocation row
 * @param {(key: string, fallback: string) => string} t translation fn
 * @returns {string}
 */
export function describeAllocationScope(allocation, t) {
  const scope = allocation?.scope_type;
  if (!scope || scope === SCOPE_PERSONAL) return '';
  const parts = [];
  if (scope === SCOPE_PLACE) {
    const label = allocation.scope_group_label
      || t('ui.orders.scope_place', 'Place collection');
    parts.push(`🏢 ${label}`);
  } else if (scope === SCOPE_CLUSTER) {
    parts.push(t('ui.orders.scope_cluster', 'Linked-accounts collection'));
  }
  if (allocation.source_customer_id != null && allocation.beneficiary_user_id != null
      && allocation.source_customer_id !== allocation.beneficiary_user_id) {
    parts.push(`#${allocation.source_customer_id} → #${allocation.beneficiary_user_id}`);
  }
  return parts.join(' · ');
}

/**
 * True when any entry of an order payment timeline was funded by a
 * place-scoped collection — drives the collected-cash modal's scope copy.
 *
 * @param {Array|undefined|null} timeline
 * @returns {boolean}
 */
export function hasPlaceScopedAllocation(timeline) {
  return (timeline || []).some((entry) => entry?.scope_type === SCOPE_PLACE);
}

// Every code `OrderCashEditService` can emit (see
// business_app/services/order_cash_edit_service.py). Anything not listed here
// is surfaced verbatim rather than swallowed.
// A Map, not an object literal: the lookup key comes straight off the wire.
const WARNING_COPY = new Map([
  ['delivery_timestamp_missing', [
    'ui.orders.cash_warning_no_delivery_timestamp',
    'No delivery timestamp on this order — the correction window is treated as unlimited',
  ]],
  ['collected_below_order_total', [
    'ui.orders.cash_warning_below_total',
    'The order will not be fully paid — loyalty may need manual review',
  ]],
  ['order_already_settled_by_other_source', [
    'ui.orders.cash_warning_settled_elsewhere',
    'This order is already paid from another source (card transfer or prepaid credit), '
      + 'so nothing applies to it and the whole amount becomes customer credit',
  ]],
  ['customer_has_other_unpaid_cod_orders', [
    'ui.orders.cash_warning_spill',
    "Extra cash settles the scope's oldest unpaid order first — that can be a linked "
      + "account's or a coworker's debt, so the per-order figures are approximate",
  ]],
  ['surplus_credited_to_customer', [
    'ui.orders.cash_warning_surplus',
    "Surplus becomes the customer's prepaid credit (shared across linked accounts)",
  ]],
  ['correction_pushes_cod_over_cap', [
    'ui.orders.cash_warning_cap',
    'This correction puts the customer (or their workplace) back at the COD debt cap — '
      + 'they will not be able to order cash-on-delivery until it is paid down',
  ]],
]);

/**
 * Translate one collected-cash-edit warning.
 *
 * Backend warnings are NOT bare codes: they are `"<code>: text"` or
 * `"<code> - text"` strings. Splitting off the leading token is load-bearing —
 * an exact-key lookup on the whole string misses every real warning and renders
 * raw English. Unknown codes fall through to the raw string on purpose: a
 * warning the UI has not learned yet must still reach the admin.
 *
 * @param {string|null|undefined} warning
 * @param {(key: string, fallback: string) => string} t
 * @returns {string}
 */
export function describeCashEditWarning(warning, t) {
  if (!warning) return '';
  const code = String(warning).split(/[:\s]/, 1)[0];
  const entry = WARNING_COPY.get(code);
  if (!entry) return warning; // unknown codes surface verbatim — never swallowed
  return t(entry[0], entry[1]);
}
