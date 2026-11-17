import React from 'react';
import { Alert, Divider } from 'antd';
import { useTranslation } from 'react-i18next';

import PriceCourtesyStatus from '../priceChange/PriceCourtesyStatus';
import { courtesyLineParts } from '../../utils/priceCourtesy';
import { extractApiErrorMessages } from '../../utils/apiError';
import { formatCalendarDate } from '../../utils/dateUtils';
import { formatMoneyUZS } from '../../utils/formatMoney';

const Row = ({ label, value, strong }) => (
  <div style={{ display: 'flex', justifyContent: 'space-between', fontWeight: strong ? 600 : 400 }}>
    <span>{label}</span>
    <span>{value}</span>
  </div>
);

// The server's quote of the basket being built, printed as sent. Every figure is a published field: the panel
// computes no money, and decides no state.
const OrderQuotePanel = ({ quote, loading, error, priceChanged }) => {
  const { t } = useTranslation();

  return (
    <div style={{ marginBottom: 16 }}>
      {priceChanged ? (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 12 }}
          message={t(
            'ui.order_quote.price_changed',
            'The price changed since this screen was drawn. Check the new total, then create the order again.',
          )}
        />
      ) : null}
      {loading ? <div>{t('ui.order_quote.loading', 'Pricing…')}</div> : null}
      {error ? (
        <Alert
          type="error"
          showIcon
          message={t('ui.order_quote.failed', 'Could not price this basket')}
          description={extractApiErrorMessages(error, '')[0]}
        />
      ) : null}
      {quote ? (
        <>
          {quote.items.map((line) => (
            <div key={line.product_id} style={{ marginBottom: 8 }}>
              <Row label={line.product_name} value={formatMoneyUZS(line.total_price)} />
              <div style={{ fontSize: 12 }}>
                {courtesyLineParts(line)
                  .map((part) => `${part.quantity} × ${formatMoneyUZS(part.unitPrice)}`)
                  .join(' + ')}
              </div>
              {line.upcoming_price ? (
                <div style={{ fontSize: 12, opacity: 0.7 }}>
                  {t('ui.order_quote.upcoming', 'From {{date}}: {{price}}', {
                    date: formatCalendarDate(line.upcoming_price.effective_date),
                    price: formatMoneyUZS(line.upcoming_price.new_price),
                  })}
                </div>
              ) : null}
            </div>
          ))}
          <Divider style={{ margin: '8px 0' }} />
          <Row label={t('ui.order_quote.subtotal', 'Subtotal')} value={formatMoneyUZS(quote.subtotal)} />
          {quote.delivery_fee ? (
            <Row label={t('ui.order_quote.delivery_fee', 'Delivery fee')} value={formatMoneyUZS(quote.delivery_fee)} />
          ) : null}
          {quote.tier_discount ? (
            <Row label={t('ui.order_quote.tier_discount', 'Tier discount')} value={formatMoneyUZS(quote.tier_discount)} />
          ) : null}
          <Row strong label={t('ui.order_quote.total', 'Total')} value={formatMoneyUZS(quote.total_amount)} />
          <div style={{ marginTop: 8 }}>
            <PriceCourtesyStatus status={quote.courtesy_status} />
          </div>
          {quote.price_courtesy ? (
            <div>{t('ui.order_quote.courtesy_applied', 'The old price is kept on this order.')}</div>
          ) : null}
        </>
      ) : null}
    </div>
  );
};

export default OrderQuotePanel;
