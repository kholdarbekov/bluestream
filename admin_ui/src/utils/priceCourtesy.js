import { apiErrorCode } from './apiError';

// One order line as the server priced it -> the parts it is printed in. A line that took the price-change courtesy is
// two parts (the old-price units, then the rest), and the server publishes both quantities so nothing here subtracts.
export const courtesyLineParts = (line) => {
  if (line.courtesy_quantity > 0) {
    const parts = [{ quantity: line.courtesy_quantity, unitPrice: line.courtesy_unit_price }];
    if (line.regular_quantity > 0) parts.push({ quantity: line.regular_quantity, unitPrice: line.unit_price });
    return parts;
  }
  return [{ quantity: line.quantity, unitPrice: line.unit_price }];
};

// The 409 an order create answers when the total the screen showed no longer holds: the quote the screen must show
// now, or null for any other refusal. `details` sits at the top level of the staff envelope's body.
export const priceChangedQuote = (error) => {
  if (apiErrorCode(error) !== 'STATE_CHANGED') return null;
  const details = error?.response?.data?.details;
  return details?.reason === 'price_changed' ? details.quote ?? null : null;
};
