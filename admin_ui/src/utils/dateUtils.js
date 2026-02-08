/**
 * Date formatting utilities for consistent timezone display.
 *
 * All server timestamps are stored in UTC. These helpers convert to
 * the configured display timezone offset before formatting so the
 * admin UI shows the correct local time regardless of the browser's timezone.
 *
 * The offset is set via REACT_APP_TIMEZONE_OFFSET env var (from .env).
 */
import moment from 'moment';

const TIMEZONE_OFFSET = process.env.REACT_APP_TIMEZONE_OFFSET || '+05:00';

/**
 * Convert a date value to a moment in the Tashkent timezone.
 */
export const toTashkent = (date) => moment(date).utcOffset(TIMEZONE_OFFSET);

/**
 * Format a date for display (date only).
 * Default format: "MMM DD, YYYY"
 */
export const formatDate = (date, format = 'MMM DD, YYYY') => {
  if (!date) return '-';
  return toTashkent(date).format(format);
};

/**
 * Format a date-time for display.
 * Default format: "MMM DD, YYYY HH:mm"
 */
export const formatDateTime = (date, format = 'MMM DD, YYYY HH:mm') => {
  if (!date) return '-';
  return toTashkent(date).format(format);
};

/**
 * Format a date as a short ISO-style string (YYYY-MM-DD HH:mm).
 */
export const formatDateTimeShort = (date) => {
  if (!date) return '-';
  return toTashkent(date).format('YYYY-MM-DD HH:mm');
};

/**
 * Format a date as a locale-style date string (DD/MM/YYYY).
 * Replacement for new Date(date).toLocaleDateString().
 */
export const formatLocalDate = (date) => {
  if (!date) return '-';
  return toTashkent(date).format('DD/MM/YYYY');
};

/**
 * Format a date as a locale-style date+time string (DD/MM/YYYY, HH:mm).
 * Replacement for new Date(date).toLocaleString().
 */
export const formatLocaleDateTime = (date) => {
  if (!date) return '-';
  return toTashkent(date).format('DD/MM/YYYY, HH:mm');
};

/**
 * Get the current time in Tashkent as a formatted string.
 */
export const nowTashkent = (format = 'DD/MM/YYYY, HH:mm') => {
  return moment().utcOffset(TIMEZONE_OFFSET).format(format);
};
