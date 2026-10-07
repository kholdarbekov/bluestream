import React from 'react';
import { InputNumber } from 'antd';
import { formatMoney } from '../../utils/formatMoney';

// Whole UZS, as typed by a person: digits grouped by spaces, commas, no-break spaces or dots
// ("150.000", the ru/uz habit), an optional ".00", or a spreadsheet's "1.5e6". The character check is flat on purpose (no nested
// quantifier: `security/detect-unsafe-regex`); `Number` then decides what the characters mean.
const LEADING_MINUS = /^[-\u2212]/;
const GROUP_SEPARATORS = /[\s,\u00a0\u202f]/g;
const AMOUNT_CHARS = /^[\d.eE+]+$/;

/**
 * Text -> the whole-UZS number string antd stores, or '' when the text is no amount.
 *
 * The sign is the point (Review Focus 5). With `allowNegative` a leading "-" or U+2212 is kept,
 * so "-50 000" posts -50000. Without it a leading minus is REFUSED: the parser answers '' (no
 * value), because dropping the minus would turn a typed deduction into the positive amount the
 * admin never meant. A fraction is refused too, never rounded: pay amounts are whole UZS.
 */
export const parseMoneyInput = (text, allowNegative = false) => {
  const raw = String(text ?? '').trim();
  const negative = LEADING_MINUS.test(raw);
  if (negative && !allowNegative) return '';
  let body = raw.replace(LEADING_MINUS, '').replace(GROUP_SEPARATORS, '');
  if (!AMOUNT_CHARS.test(body)) return '';
  const parts = body.split('.');
  // "150.000" / "1.500.000": dot-grouped thousands (ru/uz habit), never a fraction of a sum.
  // Only exact groups of three after a 1-3 digit head that does not start with 0; anything else
  // ("150.00", "150.5", "0.500") is a decimal point, and a fraction is refused below.
  if (parts.length > 1 && !/[eE]/.test(body) && parts[0].length >= 1 && parts[0].length <= 3
      && parts[0][0] !== '0' && parts.slice(1).every((p) => p.length === 3)) body = parts.join('');
  const amount = Number(body);
  if (!Number.isInteger(amount)) return '';
  return String(negative ? -amount : amount);
};

/**
 * The stored number -> "-50,000". While the admin is typing the text is left exactly as typed:
 * reformatting "1.5e" mid-keystroke would wipe it (antd re-runs the formatter on every change).
 */
export const formatMoneyInput = (value, info) => {
  if (info?.userTyping) return info.input;
  if (value === null || value === undefined || value === '') return '';
  return formatMoney(value);
};

/**
 * The admin UI's money input (spec §6.2). An antd `InputNumber` with `precision={0}`, a
 * thousands formatter and the parser above; negative only with `allowNegative` (an adjustment),
 * never for a salary, a penalty, a type's default or a recorded repayment.
 */
const MoneyInput = ({ value, onChange, allowNegative = false, ...inputNumberProps }) => (
  <InputNumber
    style={{ width: '100%' }}
    {...inputNumberProps}
    value={value}
    onChange={onChange}
    precision={0}
    min={allowNegative ? undefined : 0}
    formatter={formatMoneyInput}
    parser={(text) => parseMoneyInput(text, allowNegative)}
  />
);

export default MoneyInput;
