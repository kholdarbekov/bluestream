import { formatMoney } from '../../utils/formatMoney';

describe('formatMoney', () => {
  it('formats whole numbers with thousands grouping', () => {
    expect(formatMoney(12000)).toBe('12,000');
    expect(formatMoney(1234567)).toBe('1,234,567');
  });

  it('formats small integers without grouping', () => {
    expect(formatMoney(500)).toBe('500');
    expect(formatMoney(0)).toBe('0');
  });

  it('rounds decimal amounts to whole UZS by default', () => {
    expect(formatMoney(12345.67)).toBe('12,346');
    expect(formatMoney(99.99)).toBe('100');
  });

  it('supports an explicit decimals option', () => {
    expect(formatMoney(12345.678, { decimals: 2 })).toBe('12,345.68');
  });

  it('is null/undefined-safe and never calls toFixed on a missing value', () => {
    expect(formatMoney(null)).toBe('—');
    expect(formatMoney(undefined)).toBe('—');
  });

  it('returns the fallback for NaN input', () => {
    expect(formatMoney('not-a-number')).toBe('—');
    expect(formatMoney(NaN)).toBe('—');
  });

  it('supports a custom fallback', () => {
    expect(formatMoney(null, { fallback: '0' })).toBe('0');
  });
});
