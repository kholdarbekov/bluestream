/**
 * Money formatting utilities for consistent UZS amount display.
 *
 * The API returns raw numeric UZS amounts (no minor currency unit in
 * practice), so the admin UI's convention is thousands-grouped integers
 * (e.g. "12,000") rather than fixed 2-decimal currency strings. These
 * helpers are null/undefined/NaN-safe so callers never need to guard
 * before calling `.toFixed`/`.toLocaleString` on a possibly-missing value.
 */

const DEFAULT_FALLBACK = '—'; // em dash, matches existing ad-hoc renders

export const formatMoney = (amount, { decimals = 0, fallback = DEFAULT_FALLBACK } = {}) => {
  const num = Number(amount);
  if (amount === null || amount === undefined || Number.isNaN(num)) return fallback;
  return num.toLocaleString('en-US', {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
};

export const formatMoneyUZS = (amount, options = {}) => {
  const formatted = formatMoney(amount, options);
  const fallback = options.fallback ?? DEFAULT_FALLBACK;
  if (formatted === fallback) return formatted;
  return `${formatted} UZS`;
};
