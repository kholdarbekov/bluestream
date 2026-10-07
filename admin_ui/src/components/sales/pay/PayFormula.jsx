import React from 'react';
import { Button, Descriptions, Space, Tag, Typography } from 'antd';
import { useTranslation } from 'react-i18next';
import {
  dateLabel, deduction, grouped, magnitude, monthLabel, productName, rateLabel, signedMoney, termsLine, tierRange,
} from './payFormat';

const { Text } = Typography;

/**
 * Where a shortfall goes (C1, I-28), from the statement's own `carry_out` and `owed`. One
 * component for both places that say it: the Total row here and the month view's agents table.
 */
export const ShortfallNote = ({ carryOut, owed }) => {
  const { t } = useTranslation(['sales_agents', 'common']);
  if (Number(carryOut) < 0) {
    return <Tag color="red">{t('sales_agents:pay.formula.carry_note', { defaultValue: '{{amount}} will be deducted next month (the total cannot go below 0)', amount: magnitude(carryOut) })}</Tag>;
  }
  if (Number(owed) > 0) {
    return <Tag color="red">{t('sales_agents:pay.formula.owed_note', { defaultValue: "Owed by the agent: {{amount}} (employment does not continue into the next month; the agent's next statement deducts it)", amount: magnitude(owed) })}</Tag>;
  }
  return null;
};

/**
 * One tier of a product's month (§6.2): "1–300: 300 × 1,000 = 300,000" or "1+: 2% of 12,500,000
 * = 250,000", always at the tier's full-money figure, then "counted …" when an order of the tier
 * brought in less money (D-Q3). Every number is a published field of the tier.
 */
const tierLine = (t, tier) => {
  const range = tierRange(tier.from_unit, tier.to_unit);
  const line = tier.mode === 'per_unit'
    ? t('sales_agents:pay.formula.tier_per_unit', { defaultValue: '{{range}}: {{units}} × {{value}} = {{amount}}', range, units: grouped(tier.units), value: signedMoney(tier.value), amount: signedMoney(tier.amount_full) })
    : t('sales_agents:pay.formula.tier_percent', { defaultValue: '{{range}}: {{value}}% of {{net}} = {{amount}}', range, value: tier.value, net: signedMoney(tier.net), amount: signedMoney(tier.amount_full) });
  if (Number(tier.amount) === Number(tier.amount_full)) return line;
  return `${line} · ${t('sales_agents:pay.formula.tier_scaled', { defaultValue: 'counted {{amount}} (less money received)', amount: signedMoney(tier.amount) })}`;
};

/**
 * One product of the month's commission (§6.2, D-BANDS): its units and total, one line per tier
 * holding units, and the next tier while the month is open (the backend publishes `next_tier`
 * only on an open estimate, so its presence is the whole rule here).
 */
const CommissionProduct = ({ product }) => {
  const { t, i18n } = useTranslation(['sales_agents', 'common']);
  const next = product.next_tier;
  return (
    <div data-testid={`pay-product-${product.product_id}`}>
      <Space size={4} wrap>
        <Text>
          {t('sales_agents:pay.formula.product_units', { defaultValue: '{{product}}: {{units}} units · {{amount}}', product: productName(product.product_name, i18n.language), units: grouped(product.units), amount: signedMoney(product.total) })}
        </Text>
        {product.uses_default_tiers ? <Tag>{t('sales_agents:pay.formula.default_tiers_tag', 'default tiers')}</Tag> : null}
      </Space>
      {product.tiers.map((tier) => (
        <div key={tier.from_unit}><Text type="secondary">{tierLine(t, tier)}</Text></div>
      ))}
      {next ? (
        <div>
          <Text type="secondary">
            {t('sales_agents:pay.formula.next_tier', { defaultValue: 'Next tier from unit {{from_unit}} ({{rate}}): {{count}} more units', from_unit: grouped(next.from_unit), rate: rateLabel(t, next.mode, next.value), count: grouped(next.units_to_go) })}
          </Text>
        </div>
      ) : null}
    </div>
  );
};

// A product a late group moved (§6.2, I-34): its units and total at the cut before → now.
const LateProduct = ({ product }) => {
  const { t, i18n } = useTranslation(['sales_agents', 'common']);
  return (
    <div>
      <Text type="secondary">
        {t('sales_agents:pay.formula.late_product', { defaultValue: '{{product}}: {{units_before}} → {{units}} units, {{total_before}} → {{total}} ({{change}})', product: productName(product.product_name, i18n.language), units_before: grouped(product.units_before), units: grouped(product.units), total_before: signedMoney(product.total_before), total: signedMoney(product.total), change: signedMoney(product.change, { plus: true }) })}
      </Text>
    </div>
  );
};

/**
 * The statement drawer's Summary tab (§6.2 item 1): one row per formula step, every figure a
 * published field of A8. The expression lines print the published terms beside the published
 * result; nothing here adds, multiplies or floors.
 */
const PayFormula = ({ statement, onOpenTab }) => {
  const { t } = useTranslation(['sales_agents', 'common']);
  const { plan, inputs, summary } = statement;
  const { commission, gate, carry_in: carryIn } = summary;
  const link = (tab, text) => (
    <Button type="link" size="small" style={{ padding: 0 }} onClick={() => onOpenTab(tab)}>{text}</Button>
  );
  const gateText = gate.rule === 'below_min_due'
    ? t('sales_agents:pay.formula.gate_below_min', { defaultValue: 'fewer than {{min_due}} visits due, no reduction', min_due: plan.gate_min_due })
    : `${gate.visits_counted} / ${gate.visits_due} = ${gate.compliance_pct}% → ${t('sales_agents:pay.formula.gate_band', { defaultValue: 'band ≥{{min_pct}}%', min_pct: gate.band?.min_pct })} → ×${gate.multiplier}`;
  const variableTerms = [
    { value: commission.after_gate },
    ...summary.late.map((group) => ({ value: group.after_gate })),
    { value: summary.new_outlets.amount },
    { value: summary.adjustments },
    { value: summary.penalties, deduct: true },
  ];
  const grossTerms = [{ value: summary.base_amount }, { value: summary.variable }, { value: carryIn.amount }];

  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      <Descriptions column={1} bordered size="small" data-testid="pay-formula">
        <Descriptions.Item label={t('sales_agents:pay.formula.plan', 'Plan')}>
          {t('sales_agents:pay.formula.plan_version', { defaultValue: '{{plan}} · version {{version}} from {{month}}', plan: plan.plan_name, version: plan.version_no, month: monthLabel(plan.effective_month) })}
          <div><Text type="secondary">{plan.gate_bands.map((band) => `≥${band.min_pct}% ×${band.multiplier}`).join(' · ')}</Text></div>
        </Descriptions.Item>
        <Descriptions.Item label={t('sales_agents:pay.formula.base', 'Base')}>
          {`${signedMoney(inputs.base_salary)} × ${inputs.worked_days} / ${inputs.working_days} = ${signedMoney(summary.base_amount)}`}
          <div>
            {inputs.holidays.map((day) => (
              <Tag key={`holiday-${day.date}`}>{`${t('sales_agents:pay.day_status.holiday', 'Holiday')} ${dateLabel(day.date)}`}</Tag>
            ))}
            {inputs.unpaid_days.map((day) => (
              <Tag key={`unpaid-${day.date}`} color="orange">{`${t('sales_agents:pay.day_status.unpaid', 'Unpaid')} ${dateLabel(day.date)}`}</Tag>
            ))}
          </div>
        </Descriptions.Item>
        <Descriptions.Item label={t('sales_agents:pay.formula.commission', 'Commission')}>
          {link('orders', `${t('sales_agents:pay.formula.orders', { defaultValue: '{{count}} orders', count: commission.orders })}: ${signedMoney(commission.gross)}`)}
          {commission.products.map((product) => <CommissionProduct key={product.product_id} product={product} />)}
        </Descriptions.Item>
        <Descriptions.Item label={t('sales_agents:pay.formula.gate', 'Plan vs fact')}>{link('days', gateText)}</Descriptions.Item>
        <Descriptions.Item label={t('sales_agents:pay.formula.after_gate', 'Commission after discipline')}>
          {signedMoney(commission.after_gate)}
        </Descriptions.Item>
        {summary.late.map((group) => (
          <Descriptions.Item key={`late-${group.earned_month}`} label={t('sales_agents:pay.formula.late', 'Late correction')}>
            {`${t('sales_agents:pay.formula.late_from', { defaultValue: 'from {{month}}', month: monthLabel(group.earned_month) })}: ${signedMoney(group.gross)} × ${group.multiplier} = ${signedMoney(group.after_gate)}`}
            {group.source === 'current'
              ? ` ${t('sales_agents:pay.formula.gate_source_current', { defaultValue: "(this month's discipline, no statement for {{month}})", month: monthLabel(group.earned_month) })}`
              : ''}
            {group.counted === false ? (
              <div><Tag>{t('sales_agents:pay.lines.not_counted_shadow', 'not counted (trial month)')}</Tag></div>
            ) : null}
            {group.products.map((product) => <LateProduct key={product.product_id} product={product} />)}
          </Descriptions.Item>
        ))}
        <Descriptions.Item label={t('sales_agents:pay.formula.new_outlets', 'New outlets')}>
          {link('new_outlets', `${summary.new_outlets.count} = ${signedMoney(summary.new_outlets.amount)}`)}
        </Descriptions.Item>
        <Descriptions.Item label={t('sales_agents:pay.formula.adjustments', 'Adjustments')}>
          {signedMoney(summary.adjustments, { plus: true })}
        </Descriptions.Item>
        <Descriptions.Item label={t('sales_agents:pay.formula.penalties', 'Penalties')}>
          {link('penalties', deduction(summary.penalties) || signedMoney(0))}
        </Descriptions.Item>
        <Descriptions.Item label={t('sales_agents:pay.formula.variable', 'Variable')}>
          {`${termsLine(variableTerms)} = ${signedMoney(summary.variable)}`}
        </Descriptions.Item>
        {Number(carryIn.amount) < 0 && (
          <Descriptions.Item
            label={carryIn.source === 'owed'
              ? t('sales_agents:pay.formula.owed_in', { defaultValue: 'Owed from {{month}}', month: monthLabel(carryIn.from_month) })
              : t('sales_agents:pay.formula.carry_in', { defaultValue: 'Shortfall from {{month}}', month: monthLabel(carryIn.from_month) })}
          >
            {signedMoney(carryIn.amount)}
          </Descriptions.Item>
        )}
        <Descriptions.Item label={t('sales_agents:pay.formula.gross', 'Gross')}>
          {`${termsLine(grossTerms)} = ${signedMoney(summary.gross_total)}`}
        </Descriptions.Item>
        <Descriptions.Item label={t('sales_agents:pay.formula.total', 'Total')}>
          <Space direction="vertical" size={4}>
            <Text strong>{`max(0, ${signedMoney(summary.gross_total)}) = ${signedMoney(summary.total)}`}</Text>
            <ShortfallNote carryOut={summary.carry_out} owed={summary.owed} />
          </Space>
        </Descriptions.Item>
      </Descriptions>
      <Text type="secondary">
        {t('sales_agents:pay.formula.inputs_note', { defaultValue: 'Short visit under {{seconds}} s, radius {{radius}} m (frozen with this statement)', seconds: inputs.short_visit_seconds, radius: inputs.geofence_radius_m })}
      </Text>
    </Space>
  );
};

export default PayFormula;
