import React from 'react';
import { Alert } from 'antd';
import { useTranslation } from 'react-i18next';
import { PAY_HANDLED_CODES } from '../../../services/salesPayService';
import { apiErrorCode, extractApiErrorMessage } from '../../../utils/apiError';
import { instant, monthLabel } from './payFormat';

const payErrorDetails = (error) => error?.response?.data?.details ?? error?.response?.data?.data?.details ?? {};

export const isHandledPayError = (error) => PAY_HANDLED_CODES.includes(apiErrorCode(error));

const joined = (value) => (Array.isArray(value) ? value.join(', ') : (value ?? ''));

/**
 * D-BANDS (§6.2): where in the editor a SALES_PAY_PLAN_INVALID points. `details.tier` is the
 * tier's 1-based position and `details.product_id` the product whose schedule holds it, named as
 * the editor lists it. Nothing for a refusal with neither a tier nor a product.
 */
const tierPosition = (t, details, productNames) => {
  const where = [];
  if (details.tier) where.push(t('sales_agents:pay.error.tier_position', { defaultValue: 'tier {{tier}}', tier: details.tier }));
  if (details.product_id) where.push(productNames.get(details.product_id) || `#${details.product_id}`);
  return where.length ? ` — ${where.join(' · ')}` : '';
};

/**
 * §6.5: the admin's own sentence for a refusal the request named, with the refusal's `details`
 * interpolated; the backend's message is the default until the row is seeded. `agentNames` is a
 * Map from a `details.agents` id to the name the screen already shows (TERMS_MISSING "naming
 * the agent"); `productNames` is the plan editor's Map from a product id to its option label.
 * `month` covers `details.month` and A18's `details.months` (§4.10).
 */
export const payErrorText = (t, error, { agentNames = new Map(), productNames = new Map() } = {}) => {
  const code = apiErrorCode(error);
  const details = payErrorDetails(error);
  if (code === 'SALES_PAY_SYNC_INCOMPLETE' && details.reason === 'concurrent') {
    // Final-review M3: the sync lost only races, so there is no order to name; "()" told nobody why.
    return t('sales_agents:pay.error.sales_pay_sync_incomplete_concurrent', 'Another sync was running at the same moment. Nothing was frozen; try again.');
  }
  const months = details.month ? [details.month] : (details.months || []);
  const sentence = t(`sales_agents:pay.error.${String(code).toLowerCase()}`, {
    defaultValue: extractApiErrorMessage(error),
    month: months.map(monthLabel).join(', '),
    agents: (details.agents || []).map((id) => agentNames.get(id) || `#${id}`).join(', '),
    orders: joined(details.orders),
    closable_from: details.closable_from ? instant(details.closable_from) : '',
    field: details.field ?? '',
    date: details.date ?? '',
    reason: details.reason ?? '',
    min_length: details.min_length ?? '',
    max_length: details.max_length ?? '',
  });
  return `${sentence}${tierPosition(t, details, productNames)}`;
};

// The one inline refusal a pay modal or view draws. Nothing for an error it did not name: the
// interceptor has already toasted that one.
export const PayErrorAlert = ({ error, agentNames, productNames }) => {
  const { t } = useTranslation(['sales_agents', 'common']);
  if (!error || !isHandledPayError(error)) return null;
  return <Alert type="error" showIcon data-testid="pay-error" message={payErrorText(t, error, { agentNames, productNames })} />;
};
