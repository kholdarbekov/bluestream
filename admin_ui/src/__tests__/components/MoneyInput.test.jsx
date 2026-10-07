import React, { useState } from 'react';
import { render, screen, fireEvent } from '@testing-library/react';

import MoneyInput, { formatMoneyInput, parseMoneyInput } from '../../components/common/MoneyInput';

// Review Focus 5: an admin types a DEDUCTION. A digits-only parser keeps "50000" out of
// "-50 000" and silently turns a deduction into a bonus, so every spelling an admin really
// produces is pinned: a space-grouped minus, the U+2212 minus a phone keyboard or a pasted
// statement carries, a pasted amount with kopeks, and scientific notation from a spreadsheet.
describe('parseMoneyInput', () => {
  it.each([
    ['-50 000', true, '-50000'],
    ['−50,000', true, '-50000'],
    ['50 000.00', true, '50000'],
    ['1.5e6', true, '1500000'],
    ['3,000,000', false, '3000000'],
    ['50\u00a0000', false, '50000'],
  ])('%j (allowNegative=%s) parses to %j', (text, allowNegative, expected) => {
    expect(parseMoneyInput(text, allowNegative)).toBe(expected);
  });

  // Final-review M1: on a ru/uz keyboard "150.000" means 150,000, never 150 (1000x too small),
  // and "1.500.000" is a million and a half, not NaN. A dot followed by anything but exact
  // three-digit groups is still a decimal point, and a fraction is still refused.
  it.each([
    ['150.000', false, '150000'],
    ['1.500.000', false, '1500000'],
    ['-50.000', true, '-50000'],
    ['150.00', false, '150'],
    ['150.5', false, ''],
    ['1234.567', false, ''],
    ['0.500', false, ''],
    ['1.5e6', false, '1500000'],
  ])('dot-grouped %j (allowNegative=%s) parses to %j', (text, allowNegative, expected) => {
    expect(parseMoneyInput(text, allowNegative)).toBe(expected);
  });

  // Without `allowNegative` a minus is REFUSED (no value), never dropped: dropping it would
  // turn "-50 000" into a 50,000 repayment the admin did not type.
  it.each(['-50 000', '−50,000', '-1'])('refuses %j without allowNegative instead of flipping its sign', (text) => {
    expect(parseMoneyInput(text, false)).toBe('');
  });

  // Whole UZS only (pay amounts pass the `_whole` CHECK): a fraction is refused, not rounded.
  it.each(['1.5', '12abc', '', '   ', '--5', '1e'])('refuses %j', (text) => {
    expect(parseMoneyInput(text, true)).toBe('');
  });
});

describe('formatMoneyInput', () => {
  it('groups thousands and keeps the sign', () => {
    expect(formatMoneyInput(-50000)).toBe('-50,000');
    expect(formatMoneyInput('3000000')).toBe('3,000,000');
    expect(formatMoneyInput(0)).toBe('0');
    expect(formatMoneyInput(null)).toBe('');
    expect(formatMoneyInput(undefined)).toBe('');
  });

  it('leaves the text alone while the admin is still typing', () => {
    expect(formatMoneyInput(1, { userTyping: true, input: '1.5e' })).toBe('1.5e');
  });
});

const Harness = ({ allowNegative = false, onValue }) => {
  const [value, setValue] = useState(null);
  return (
    <MoneyInput
      aria-label="amount"
      value={value}
      allowNegative={allowNegative}
      onChange={(next) => { setValue(next); onValue(next); }}
    />
  );
};

describe('MoneyInput', () => {
  it.each([
    ['-50 000', -50000],
    ['−50,000', -50000],
    ['50 000.00', 50000],
    ['1.5e6', 1500000],
  ])('a signed input reports %j as %d', (typed, expected) => {
    const onValue = vi.fn();
    render(<Harness allowNegative onValue={onValue} />);

    fireEvent.change(screen.getByLabelText('amount'), { target: { value: typed } });

    expect(onValue).toHaveBeenLastCalledWith(expected);
  });

  it('an unsigned input never reports a negative, and never the flipped positive', () => {
    const onValue = vi.fn();
    render(<Harness onValue={onValue} />);

    fireEvent.change(screen.getByLabelText('amount'), { target: { value: '-50 000' } });

    expect(onValue.mock.calls.every(([value]) => value === null || value >= 0)).toBe(true);
    expect(onValue).not.toHaveBeenCalledWith(50000);
  });

  it('shows the value grouped once the admin leaves the field', () => {
    render(<Harness allowNegative onValue={vi.fn()} />);
    const input = screen.getByLabelText('amount');

    fireEvent.change(input, { target: { value: '-50000' } });
    fireEvent.blur(input);

    expect(input).toHaveValue('-50,000');
  });
});
