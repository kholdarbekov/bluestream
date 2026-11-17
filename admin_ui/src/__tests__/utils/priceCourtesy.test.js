import { courtesyLineParts, priceChangedQuote } from '../../utils/priceCourtesy';

const LINE = {
  quantity: 4,
  unit_price: 20000,
  courtesy_quantity: 2,
  courtesy_unit_price: 18000,
  regular_quantity: 2,
};

describe('courtesyLineParts', () => {
  it('splits a courtesy line into the old-price part and the regular part, as the server sent them', () => {
    expect(courtesyLineParts(LINE)).toEqual([
      { quantity: 2, unitPrice: 18000 },
      { quantity: 2, unitPrice: 20000 },
    ]);
  });

  it('leaves out the regular part when the courtesy covers every unit', () => {
    expect(courtesyLineParts({ ...LINE, quantity: 2, regular_quantity: 0 })).toEqual([
      { quantity: 2, unitPrice: 18000 },
    ]);
  });

  it('is one part for a line without a courtesy', () => {
    expect(
      courtesyLineParts({ quantity: 3, unit_price: 20000, courtesy_quantity: null, courtesy_unit_price: null, regular_quantity: 3 }),
    ).toEqual([{ quantity: 3, unitPrice: 20000 }]);
    expect(courtesyLineParts({ quantity: 3, unit_price: 20000 })).toEqual([{ quantity: 3, unitPrice: 20000 }]);
  });
});

describe('priceChangedQuote', () => {
  const QUOTE = { total_amount: 80000 };
  const refusal = (status, body) => ({ response: { status, data: body } });

  it('returns the quote of a price_changed 409', () => {
    const error = refusal(409, { error_code: 'STATE_CHANGED', details: { reason: 'price_changed', quote: QUOTE } });
    expect(priceChangedQuote(error)).toBe(QUOTE);
  });

  it('is null for another reason of the same code', () => {
    const error = refusal(409, { error_code: 'STATE_CHANGED', details: { reason: 'something_else', quote: QUOTE } });
    expect(priceChangedQuote(error)).toBeNull();
  });

  it('is null for a 400 and for an error without a body', () => {
    expect(priceChangedQuote(refusal(400, { error_code: 'VALIDATION_ERROR', details: {} }))).toBeNull();
    expect(priceChangedQuote(new Error('network'))).toBeNull();
    expect(priceChangedQuote(undefined)).toBeNull();
  });
});
