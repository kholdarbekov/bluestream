import dayjs from 'dayjs';
import { formatMoney } from '../../../utils/formatMoney';

/**
 * Display helpers for the pay screens. Every value passed in is a field the backend published
 * (§6.2: "the UI does no arithmetic"). These only group digits, draw a sign and reformat a
 * month; none of them adds, subtracts, rounds or picks a month.
 */
export const MINUS = '\u2212';
const DASH = '—';

const toNumber = (value) => (value === null || value === undefined || value === '' ? NaN : Number(value));

// "−100,000" for a negative, "+50,000" when `plus` asks for an explicit sign.
export const signedMoney = (value, { plus = false } = {}) => {
  const n = toNumber(value);
  if (Number.isNaN(n)) return DASH;
  if (n < 0) return `${MINUS}${formatMoney(-n)}`;
  return plus && n > 0 ? `+${formatMoney(n)}` : formatMoney(n);
};

// A figure a column labels as a deduction (Penalties, Carried in): its size behind a U+2212,
// whatever sign the field carries, and blank when it is zero.
export const deduction = (value) => {
  const n = toNumber(value);
  if (Number.isNaN(n) || n === 0) return '';
  return `${MINUS}${formatMoney(Math.abs(n))}`;
};

// A figure's size, for a sentence that already says "deducted" or "owed".
export const magnitude = (value) => {
  const n = toNumber(value);
  return Number.isNaN(n) ? DASH : formatMoney(Math.abs(n));
};

/**
 * "680,000 − 20,000 + 200,000 + 50,000 − 150,000": published terms side by side, each with its
 * own sign; a `deduct` term (penalties, a published magnitude) always reads "−". It sums
 * nothing: the caller prints the published result after it.
 */
export const termsLine = (terms) => terms.map(({ value, deduct = false }, index) => {
  const n = toNumber(value);
  const size = formatMoney(Math.abs(n));
  const negative = deduct || n < 0;
  if (index === 0) return negative ? `${MINUS}${size}` : size;
  return `${negative ? MINUS : '+'} ${size}`;
}).join(' ');

// "2026-11" -> "11.2026", the spec's display form ("statement 11.2026").
export const monthLabel = (month) => {
  if (!month) return DASH;
  const [year, mm] = String(month).split('-');
  return `${mm}.${year}`;
};

// "09.2026 – 11.2026": a span of months, first and last inclusive.
export const monthRange = (t, from, until) => t('sales_agents:pay.plans.applies_range', {
  defaultValue: '{{from}} – {{until}}', from: monthLabel(from), until: monthLabel(until),
});

/**
 * The months a plan version applies to, worded from A12's `applies_from`, `applies_until` and
 * `replaced_by_version_no`: "Replaced by v5", "from 10.2026", "09.2026 only" or a span. The
 * backend decides the months (`_coverage`); the history column and the version view both word
 * them here.
 */
export const appliesToLabel = (t, version) => {
  if (version.replaced_by_version_no) {
    return t('sales_agents:pay.plans.replaced_by', { defaultValue: 'Replaced by v{{version}}', version: version.replaced_by_version_no });
  }
  if (!version.applies_until) {
    return t('sales_agents:pay.plans.applies_from', { defaultValue: 'from {{month}}', month: monthLabel(version.applies_from) });
  }
  if (version.applies_until === version.applies_from) {
    return t('sales_agents:pay.plans.applies_only', { defaultValue: '{{month}} only', month: monthLabel(version.applies_from) });
  }
  return monthRange(t, version.applies_from, version.applies_until);
};

// An `_iso` instant, in the viewer's zone.
export const instant = (value) => (value ? dayjs(value).format('YYYY-MM-DD HH:mm') : DASH);

// A local "YYYY-MM-DD" date as the admin reads it.
export const dateLabel = (value) => (value ? dayjs(value).format('DD.MM.YYYY') : DASH);

// A count (units, a unit number) grouped like money: "1,001". Never an amount.
export const grouped = (value) => formatMoney(value);

/**
 * "301–500" or "1,001+": a tier's published bounds, `to_unit` null on the top tier (I-29). The one
 * string the clients build from bounds; they never derive a bound themselves (V11).
 */
export const tierRange = (fromUnit, toUnit) => (toUnit === null || toUnit === undefined
  ? `${grouped(fromUnit)}+`
  : `${grouped(fromUnit)}–${grouped(toUnit)}`);

// A tier's rate as the formula and the lines print it: "1,500 UZS/unit" or "2.5 %".
export const rateLabel = (t, mode, value) => (mode === 'per_unit'
  ? t('sales_agents:pay.lines.rate_per_unit', { defaultValue: '{{value}} UZS/unit', value: signedMoney(value) })
  : `${value} %`);

/**
 * A product's name as the route published it: A8 sends the frozen snapshot `{en, uz, ru}`, A9 and
 * A14 a string. The admin's language first, then English.
 */
export const productName = (name, language) => {
  if (typeof name === 'string') return name;
  const names = new Map(Object.entries(name || {}));
  return names.get(language) || names.get('en') || DASH;
};
