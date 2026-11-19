// The Driver expenses page's labels. Each key is a literal `t('ui.driver_expenses.…', 'English')` call, so the seed
// test (tests/unit/test_driver_expense_ui_translation_seed.py) can read the fallback and hold the seeded English to it.
// Each lookup is a Map, not an object literal: the key comes straight off the wire.

export const EXPENSE_STATUSES = ['pending', 'approved', 'rejected', 'withdrawn'];
export const EXPENSE_CATEGORIES = [
  'fuel', 'new_bottles', 'caps', 'labels', 'other_supplies', 'vehicle', 'parking_wash', 'other',
];

export const statusLabel = (t, status) => new Map([
  ['pending', t('ui.driver_expenses.status.pending', 'Waiting')],
  ['approved', t('ui.driver_expenses.status.approved', 'Approved')],
  ['rejected', t('ui.driver_expenses.status.rejected', 'Rejected')],
  ['withdrawn', t('ui.driver_expenses.status.withdrawn', 'Withdrawn')],
  ['all', t('ui.driver_expenses.status.all', 'All')],
]).get(status) ?? status;

export const categoryLabel = (t, category) => new Map([
  ['fuel', t('ui.driver_expenses.category.fuel', 'Fuel')],
  ['new_bottles', t('ui.driver_expenses.category.new_bottles', 'New bottles')],
  ['caps', t('ui.driver_expenses.category.caps', 'Caps')],
  ['labels', t('ui.driver_expenses.category.labels', 'Labels')],
  ['other_supplies', t('ui.driver_expenses.category.other_supplies', 'Other supplies')],
  ['vehicle', t('ui.driver_expenses.category.vehicle', 'Car repair')],
  ['parking_wash', t('ui.driver_expenses.category.parking_wash', 'Parking/wash')],
  ['other', t('ui.driver_expenses.category.other', 'Other')],
]).get(category) ?? category;

// The check result of one of the driver's last closed trips (`warnings.recent_closed_trips[].status`): a trip the
// office has not counted yet must not read as checked clean, and a shortfall still blocking the driver must not read as
// written off.
export const tripStatusLabel = (t, status) => new Map([
  ['submitted', t('ui.driver_expenses.trip_status.submitted', 'Not checked yet')],
  ['verified', t('ui.driver_expenses.trip_status.verified', 'Checked')],
  ['mismatch', t('ui.driver_expenses.trip_status.mismatch', 'Mismatch, not resolved')],
  ['resolved', t('ui.driver_expenses.trip_status.resolved', 'Written off')],
  ['force_closed', t('ui.driver_expenses.trip_status.force_closed', 'Force-closed')],
]).get(status) ?? status;

// The copy of a decision or undo block code the backend publishes (`decide_block_code`, `undo_block_code`,
// `blocking_codes`, a refusal's `details.block_code`).
export const blockLabel = (t, code) => new Map([
  ['not_pending', t('ui.driver_expenses.block.not_pending', 'Already decided')],
  ['trip_not_active', t('ui.driver_expenses.block.trip_not_active', 'The trip is no longer active')],
  ['amount_above_claim', t('ui.driver_expenses.block.amount_above_claim', 'More than the claim')],
  ['amount_above_trip_cash', t('ui.driver_expenses.block.amount_above_trip_cash', "More than the driver's trip cash")],
  ['self_decision', t('ui.driver_expenses.block.self_decision', 'You cannot decide your own expense')],
  ['not_approved', t('ui.driver_expenses.block.not_approved', 'Not approved')],
  ['session_checked', t('ui.driver_expenses.block.session_checked', 'The trip is already closed or checked')],
  ['cash_already_handed_in', t(
    'ui.driver_expenses.block.cash_already_handed_in',
    'The driver already handed in the amount this approval freed',
  )],
]).get(code) ?? code;
