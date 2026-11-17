import React from 'react';
import { useTranslation } from 'react-i18next';

import { formatCalendarDate } from '../../utils/dateUtils';

// One published courtesy STATUS -> one sentence. Nothing here decides a state or a date: it prints the fields the
// server sent. `null` for `none` (the panel says it in its own words) or no status. Reused by the order page (Task 56).
const PriceCourtesyStatus = ({ status }) => {
  const { t } = useTranslation();
  if (!status) return null;
  if (status.state === 'available') {
    const caps = status.caps
      .map((cap) => t('ui.price_courtesy.cap', 'up to {{cap}} × {{product}}', { cap: cap.cap, product: cap.product_name }))
      .join(', ');
    return (
      <span>
        {t('ui.price_courtesy.available', 'Old price kept for this customer until {{date}}: {{caps}}', {
          date: formatCalendarDate(status.expires_on),
          caps,
        })}
      </span>
    );
  }
  if (status.state === 'used') {
    return (
      <span>
        {t('ui.price_courtesy.used', 'Already used on order {{order_number}}', { order_number: status.used_order_number })}
      </span>
    );
  }
  return null;
};

export default PriceCourtesyStatus;
