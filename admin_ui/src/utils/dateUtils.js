/**
 * Date formatting utilities for consistent timezone display.
 *
 * All server timestamps are stored in UTC. These helpers convert to
 * the configured display timezone offset before formatting so the
 * admin UI shows the correct local time regardless of the browser's timezone.
 *
 * The offset is set via VITE_TIMEZONE_OFFSET env var (from .env).
 */
import dayjs from 'dayjs';
import utc from 'dayjs/plugin/utc';
import customParseFormat from 'dayjs/plugin/customParseFormat';

dayjs.extend(utc);
dayjs.extend(customParseFormat);

const TIMEZONE_OFFSET = import.meta.env.VITE_TIMEZONE_OFFSET || '+05:00';

// Parse "+05:00" / "-03:30" into total minutes.
const parseOffsetMinutes = (offset) => {
  const match = /^([+-])(\d{2}):(\d{2})$/.exec(offset);
  if (!match) return 0;
  const sign = match[1] === '-' ? -1 : 1;
  return sign * (parseInt(match[2], 10) * 60 + parseInt(match[3], 10));
};

const OFFSET_MINUTES = parseOffsetMinutes(TIMEZONE_OFFSET);

export const toTashkent = (date) => dayjs(date).utcOffset(OFFSET_MINUTES);

export const formatDate = (date, format = 'MMM DD, YYYY') => {
  if (!date) return '-';
  return toTashkent(date).format(format);
};

export const formatDateTime = (date, format = 'MMM DD, YYYY HH:mm') => {
  if (!date) return '-';
  return toTashkent(date).format(format);
};

export const formatDateTimeShort = (date) => {
  if (!date) return '-';
  return toTashkent(date).format('YYYY-MM-DD HH:mm');
};

export const formatLocalDate = (date) => {
  if (!date) return '-';
  return toTashkent(date).format('DD/MM/YYYY');
};

export const formatLocaleDateTime = (date) => {
  if (!date) return '-';
  return toTashkent(date).format('DD/MM/YYYY, HH:mm');
};

export const formatDateTimeSeconds = (date) => {
  if (!date) return '-';
  return toTashkent(date).format('DD-MM-YYYY HH:mm:ss');
};

export const nowTashkent = (format = 'DD/MM/YYYY, HH:mm') => {
  return dayjs().utcOffset(OFFSET_MINUTES).format(format);
};
