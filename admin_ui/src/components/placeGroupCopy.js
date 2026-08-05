import { extractApiErrorMessage } from '../utils/apiError';

/**
 * Shared place-group copy: the fence-code map, the audit-event labels and the
 * two pure readers over them.
 *
 * EXTRACTED VERBATIM from PlaceGroupPanel.jsx (which now imports them), so the
 * per-customer panel and the estate-wide "Grouped Addresses" tab speak with one
 * vocabulary instead of two drifting copies. Pure data plus two pure functions
 * — no behaviour lives here.
 */

/**
 * Machine-readable fence codes the place-group endpoints return inside
 * `data.error_code`. The envelope's `message` is ALWAYS the generic
 * "Validation failed", so branching on the code (and falling back to
 * `errors[0]`) is the only way an admin learns why an action was rejected.
 */
export const PLACE_GROUP_ERROR_MESSAGES = new Map([
  [
    'PLACE_GROUP_GROCERY_MEMBER',
    [
      'ui.users.place_groups.error_grocery_member',
      'Grocery-store accounts cannot be part of a place group.',
    ],
  ],
  [
    'PLACE_GROUP_ENTITY_MEMBER',
    [
      'ui.users.place_groups.error_entity_member',
      'Business (entity) accounts cannot be part of a place group.',
    ],
  ],
  [
    'PLACE_GROUP_ADDRESS_ALREADY_GROUPED',
    [
      'ui.users.place_groups.error_already_grouped',
      'That address is already in another place group. Remove it from that group first.',
    ],
  ],
  [
    'PLACE_GROUP_NOT_FOUND',
    [
      'ui.users.place_groups.error_group_not_found',
      'This place group no longer exists. Refresh and try again.',
    ],
  ],
  [
    'CUSTOMER_LINK_ADDRESS_NOT_FOUND',
    [
      'ui.users.place_groups.error_address_not_found',
      'One of the selected addresses no longer exists.',
    ],
  ],
  [
    // Spec 7.1: out of range is REJECTED, never clamped, so this message is the
    // only thing that tells the admin their number was thrown away.
    'PLACE_SPLIT_INVALID',
    [
      'ui.users.place_groups.error_place_split_invalid',
      'Bottles leaving must be between 0 and the place total.',
    ],
  ],
  [
    'MERGE_PREVIEW_STALE',
    [
      'ui.users.place_groups.error_merge_preview_stale',
      'The bottle history changed while you were reviewing it. Reload the preview and try again.',
    ],
  ],
  [
    'MERGE_EXCLUSION_NOT_ELIGIBLE',
    [
      'ui.users.place_groups.error_merge_exclusion',
      'One of the excluded entries is not part of this merge.',
    ],
  ],
  [
    'MERGE_REASON_REQUIRED',
    [
      'ui.users.place_groups.error_merge_reason',
      'A reason is required to exclude entries or override the balance.',
    ],
  ],
  [
    // Reachable in one call: the route guard is `len(address_ids) < 2` while the
    // service guard is `len(set(address_ids)) < 2`, so a DUPLICATE address id
    // passes the route and trips the service.
    'PLACE_GROUP_MIN_ADDRESSES',
    [
      'ui.users.place_groups.error_min_addresses',
      'A place group needs at least two different addresses.',
    ],
  ],
  [
    // Currently masked by every route's own blank-reason guard; mapped so that
    // removing that guard shows translated copy instead of raw English.
    'PLACE_GROUP_REASON_REQUIRED',
    [
      'ui.users.place_groups.error_reason_required',
      'A reason is required for this change.',
    ],
  ],
]);

/**
 * `CustomerLinkEvent.event_type` -> [translation key, English fallback].
 *
 * The place-group history is the admin's only record of who changed a shared
 * place and why; rendering `event.event_type` raw showed every admin the same
 * English snake_case regardless of language. Keys are literal (not built with
 * a template literal) so the seed script and the JSX stay verifiably in step.
 */
export const PLACE_GROUP_EVENT_LABELS = new Map([
  [
    'create_place_group',
    ['ui.users.place_groups.event.create_place_group', 'Place group created'],
  ],
  [
    'add_to_place_group',
    ['ui.users.place_groups.event.add_to_place_group', 'Address added to the place group'],
  ],
  [
    'remove_from_place_group',
    ['ui.users.place_groups.event.remove_from_place_group', 'Address removed from the place group'],
  ],
  [
    'dismiss_place_suggestion',
    ['ui.users.place_groups.event.dismiss_place_suggestion', 'Same-place suggestion dismissed'],
  ],
  // The three PRE-PLACE link events render through the same list.
  ['link', ['ui.users.place_groups.event.link', 'Accounts linked']],
  ['unlink', ['ui.users.place_groups.event.unlink', 'Accounts unlinked']],
  ['dismiss', ['ui.users.place_groups.event.dismiss', 'Marked as different customers']],
]);

/** Translated audit label, degrading to the raw identifier for an unknown type. */
export const placeGroupEventText = (eventType, t) => {
  const known = eventType ? PLACE_GROUP_EVENT_LABELS.get(eventType) : null;
  return known ? t(known[0], known[1]) : eventType;
};

export const placeGroupErrorMessage = (error, t, fallback) => {
  const code = error?.response?.data?.data?.error_code;
  const known = code ? PLACE_GROUP_ERROR_MESSAGES.get(code) : null;
  if (known) {
    return t(known[0], known[1]);
  }
  // extractApiErrorMessage prefers `errors[0]` over the generic `message`.
  return extractApiErrorMessage(error, fallback);
};
