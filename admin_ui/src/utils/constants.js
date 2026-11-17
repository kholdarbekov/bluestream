/**
 * Single source of truth for admin-UI-internal constants.
 *
 * These are presentation/transport constants for the admin dashboard itself
 * (pagination sizes, polling cadences, currency label). Business values that
 * originate in the backend (min order amount, delivery fee, loyalty rules) are
 * NOT defined here — they come from API responses / backend config.
 *
 * Import these instead of inlining literals so a change lives in one place.
 */

// ─── Pagination ─────────────────────────────────────────────────────────
// Backend caps per_page at 100 (business_app/utils/constants.py::MAX_PAGE_SIZE).
// Never request more than MAX_PAGE_SIZE in a single call — paginate instead.
export const DEFAULT_PAGE_SIZE = 20; // default list page size, mirrors backend DEFAULT_PAGE_SIZE
export const MAX_PAGE_SIZE = 100; // hard backend cap
export const EXPORT_PAGE_SIZE = 100; // per-request size when looping to export all rows
export const BULK_LOAD_PAGE_SIZE = 100; // per-request size when looping to fully populate a dropdown

// ─── Currency ─────────────────────────────────────────────────────────────
export const CURRENCY = 'UZS';

// ─── Polling / refetch cadences (ms) ───────────────────────────────────────
// Named so intentional differences are explicit rather than magic numbers.
export const POLL_INTERVALS = {
  REALTIME: 10000, // 10s — fast monitoring views
  FAST: 15000, // 15s
  STANDARD: 30000, // 30s — default live dashboards / maps
  HOURLY: 3600000, // 1h — slow-changing reference data
};

// ─── Shared react-query keys ──────────────────────────────────────────────
// The Lapsed customers summary (reorder-reminders spec §9.1, D17): the page header, the nav badge
// and the Analytics cards read ONE cache entry, and the page's writes invalidate its prefix.
export const LAPSED_CUSTOMERS_SUMMARY_KEY = ['lapsedCustomers', 'summary'];

// Price changes (price-change spec §5.2): every price-change query key starts with this prefix, so a write
// invalidates the whole family (list, detail, unreachable, exceptions) with one call.
export const PRICE_CHANGES_KEY = ['priceChanges'];

// Shared-place cases (the admin review queue): the nav badge reads the open
// count, and every place-group / link write invalidates the whole prefix.
export const SHARED_PLACE_CASES_KEY = ['sharedPlaceCases'];
export const SHARED_PLACE_OPEN_COUNT_KEY = ['sharedPlaceCases', 'openCount'];

// ─── Deep links ─────────────────────────────────────────────────────────────
// /tryouts?tryout_id=<id> opens that try-out's drawer (Tryouts.js reads it; the
// shared-place case screen links to it, where the Adjust Bottles tool lives).
export const TRYOUT_LINK_PARAM = 'tryout_id';

// ─── UI timing (ms) ─────────────────────────────────────────────────────────
export const UI_DELAYS = {
  TOAST_AUTO_DISMISS: 5000,
  REDIRECT_AFTER_ACTION: 2000,
  STATUS_MESSAGE_CLEAR: 3000,
};
