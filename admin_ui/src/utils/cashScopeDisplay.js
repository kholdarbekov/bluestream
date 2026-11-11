import { apiErrorCode, extractApiErrorMessages } from './apiError';

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
  ['event_funds_prepayment_return', [
    'ui.prepayments.correction_blocked_by_return',
    'A prepayment return drew on this collection. Undo the return first.',
  ]],
  ['correction_pushes_cod_over_cap', [
    'ui.orders.cash_warning_cap',
    'This correction puts the customer (or their workplace) back at the COD debt cap — '
      + 'they will not be able to order cash-on-delivery until it is paid down',
  ]],
]);

// Every blocking code the prepayment return preview can emit (Tasks 2, 4 and 7 of
// business_app/services/prepayment_payout_service.py).
export const PAYOUT_BLOCK_COPY = new Map([
  ['no_free_balance', [
    'ui.prepayments.block_no_free_balance',
    'There is no free prepayment to return. Credit held on pending orders is released by cancelling them.',
  ]],
  ['amount_exceeds_free_balance', [
    'ui.prepayments.block_amount_exceeds_free_balance',
    'That is more than the free prepayment balance.',
  ]],
  ['target_not_below_free_balance', [
    'ui.prepayments.block_target_not_below_free_balance',
    'The balance after the return must be lower than the free balance now.',
  ]],
  ['driver_not_eligible', [
    'ui.prepayments.block_driver_not_eligible',
    'That person is not an active driver.',
  ]],
  ['no_active_session', [
    'ui.prepayments.block_no_active_session',
    'This driver has no current trip to take the cash from.',
  ]],
  ['amount_exceeds_driver_cash', [
    'ui.prepayments.block_amount_exceeds_driver_cash',
    "The driver's current trip does not hold that much cash.",
  ]],
  ['paid_before_current_trip', [
    'ui.prepayments.block_paid_before_current_trip',
    "The paid-at time is before the driver's current trip started. Use Driver, already settled instead.",
  ]],
  ['ambiguous_contract_mirror', [
    'ui.prepayments.block_ambiguous_contract_mirror',
    'This credit sits on more than one contract, so it cannot be returned automatically.',
  ]],
  ['units_contract_mirror', [
    'ui.prepayments.block_units_contract_mirror',
    'This credit funds prepaid contract bottles, so it cannot be returned as cash.',
  ]],
  ['contract_account_missing', [
    'ui.prepayments.block_contract_account_missing',
    'The contract account for this credit is missing, so it cannot be returned.',
  ]],
]);

// Why a recorded return cannot be undone (`undo_block_code` on a history payout row).
export const UNDO_BLOCK_COPY = new Map([
  ['already_undone', [
    'ui.prepayments.undo_block_already_undone',
    'This return has already been undone.',
  ]],
  ['funding_event_voided', [
    'ui.prepayments.undo_block_funding_event_voided',
    'A collection this return drew on was voided, so it cannot be undone.',
  ]],
  ['session_checked', [
    'ui.prepayments.undo_block_session_checked',
    "The driver's trip is closed, so this return can no longer be undone.",
  ]],
]);

/**
 * Translate one coded reason: a `"<code>: text"` / `"<code> - text"` string, or a bare code.
 *
 * Splitting off the leading token is load-bearing: an exact-key lookup on the whole string misses every
 * real backend string and renders raw English. Unknown codes fall through to the raw string on purpose:
 * a reason the UI has not learned yet must still reach the admin.
 *
 * @param {string|null|undefined} reason
 * @param {Map<string, [string, string]>} copyMap code -> [i18n key, English fallback]
 * @param {(key: string, fallback: string) => string} t
 * @returns {string}
 */
export function describeCodedReason(reason, copyMap, t) {
  if (!reason) return '';
  const code = String(reason).split(/[:\s]/, 1)[0];
  const entry = copyMap.get(code);
  if (!entry) return reason;
  return t(entry[0], entry[1]);
}

/**
 * Translate one collected-cash-edit warning or blocking reason.
 *
 * @param {string|null|undefined} warning
 * @param {(key: string, fallback: string) => string} t
 * @returns {string}
 */
export function describeCashEditWarning(warning, t) {
  return describeCodedReason(warning, WARNING_COPY, t);
}

// Statuses a payout preview's warning lines can carry -> [key, English fallback]: the pending orders that hold
// credit, and the live subscriptions that may rely on it.
export const PAYOUT_ORDER_STATUS_COPY = new Map([
  ['pending', ['ui.prepayments.order_status_pending', 'Pending']],
  ['confirmed', ['ui.prepayments.order_status_confirmed', 'Confirmed']],
  ['preparing', ['ui.prepayments.order_status_preparing', 'Preparing']],
  ['out_for_delivery', ['ui.prepayments.order_status_out_for_delivery', 'Out for delivery']],
]);

export const PAYOUT_SUBSCRIPTION_STATUS_COPY = new Map([
  ['active', ['ui.prepayments.sub_status_active', 'Active']],
  ['paused', ['ui.prepayments.sub_status_paused', 'Paused']],
]);

const PAYOUT_ERR_AMOUNT = ['ui.prepayments.error_amount', 'Enter the amount to return, or the balance to leave.'];
const PAYOUT_ERR_REASON = [
  'ui.prepayments.error_reason',
  'The reason is required: at least 5 characters, and not too long.',
];
const PAYOUT_ERR_PAYER = [
  'ui.prepayments.error_payer',
  'Check who paid: the office names no driver, and a driver payment needs one.',
];
const PAYOUT_ERR_REQUEST = [
  'ui.prepayments.error_request',
  'The request was not understood. Reload the page and try again.',
];

// Refusal codes of the three payout routes (preview, apply, undo) -> [key, English fallback].
// PAYOUT_BLOCKED and PAYOUT_UNDO_BLOCKED carry coded reasons and are translated per reason instead.
export const PAYOUT_ERROR_COPY = new Map([
  ['PAYOUT_AMOUNT_INVALID', PAYOUT_ERR_AMOUNT],
  ['PAYOUT_AMOUNT_OR_TARGET', PAYOUT_ERR_AMOUNT],
  ['PAYOUT_PAID_AT_INVALID', ['ui.prepayments.error_paid_at', 'Paid at must be a past date and time.']],
  ['PAYOUT_REASON_REQUIRED', PAYOUT_ERR_REASON],
  ['PAYOUT_REASON_TOO_LONG', PAYOUT_ERR_REASON],
  ['PAYOUT_PAID_BY_INVALID', PAYOUT_ERR_PAYER],
  ['PAYOUT_DRIVER_NOT_ALLOWED', PAYOUT_ERR_PAYER],
  ['PAYOUT_DRIVER_REQUIRED', PAYOUT_ERR_PAYER],
  ['PAYOUT_IDEMPOTENCY_KEY_REUSED', [
    'ui.prepayments.error_key_reused',
    'This return was already recorded with different values. Close this window and start a new return.',
  ]],
  ['PAYOUT_IDEMPOTENCY_KEY_REQUIRED', PAYOUT_ERR_REQUEST],
  ['PAYOUT_BODY_INVALID', PAYOUT_ERR_REQUEST],
]);

const BLOCKED_CODES = new Map([['PAYOUT_BLOCKED', PAYOUT_BLOCK_COPY], ['PAYOUT_UNDO_BLOCKED', UNDO_BLOCK_COPY]]);

/** Every code a payout request explains itself: passed as the request's `handledErrorCodes`. */
export const PAYOUT_HANDLED_ERROR_CODES = [...PAYOUT_ERROR_COPY.keys(), ...BLOCKED_CODES.keys()];

/**
 * The admin-language text of a payout refusal, or null for a failure api.js has already toasted
 * (it only stays silent for PAYOUT_HANDLED_ERROR_CODES).
 *
 * @param {object} error the rejected axios error
 * @param {(key: string, fallback: string) => string} t
 * @returns {string|null}
 */
export function payoutErrorText(error, t) {
  const code = apiErrorCode(error);
  if (BLOCKED_CODES.has(code)) {
    const copy = BLOCKED_CODES.get(code);
    // "code: text; code: text": split only where the next part starts with a code token
    return extractApiErrorMessages(error)[0]
      .split(/;\s+(?=[a-z_]+:\s)/)
      .map((reason) => describeCodedReason(reason, copy, t))
      .join(' ');
  }
  const known = PAYOUT_ERROR_COPY.get(code);
  return known ? t(known[0], known[1]) : null;
}
