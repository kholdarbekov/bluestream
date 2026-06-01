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

// ─── UI timing (ms) ─────────────────────────────────────────────────────────
export const UI_DELAYS = {
  TOAST_AUTO_DISMISS: 5000,
  REDIRECT_AFTER_ACTION: 2000,
  STATUS_MESSAGE_CLEAR: 3000,
};
