import React from 'react';
import {
  Alert, Descriptions, Divider, Drawer, Space, Spin, Table, Tag, Typography,
} from 'antd';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import salesPayService from '../../../services/salesPayService';
import { useAuthStore } from '../../../stores/authStore';
import { extractApiErrorMessage } from '../../../utils/apiError';
import {
  appliesToLabel, monthLabel, productName, rateLabel, signedMoney, tierRange,
} from './payFormat';

const { Text } = Typography;

/**
 * One plan version, read-only (§6.2 Plans tab "View"): A14's `VersionDetail` in the editor's
 * order, each tier as its published range and rate. The months it applies to come from its A12
 * history row (`version`), worded as the history column words them.
 */
const PlanVersionView = ({ plan, version, onClose }) => {
  const { t, i18n } = useTranslation(['sales_agents', 'common']);
  const canManagePay = useAuthStore().hasPermission('can_manage_sales_pay');
  const versionQuery = useQuery({
    queryKey: ['salesPay', 'planVersion', plan.id, version.id],
    queryFn: () => salesPayService.getPlanVersion(plan.id, version.id),
    enabled: canManagePay,
  });
  const detail = versionQuery.data?.version;

  const tierColumns = [
    { title: t('sales_agents:pay.col.tier', 'Tier'), key: 'tier', render: (_, tier) => tierRange(tier.from_unit, tier.to_unit) },
    { title: t('sales_agents:pay.col.rate', 'Rate'), key: 'rate', render: (_, tier) => rateLabel(t, tier.mode, tier.value) },
  ];
  const bandColumns = [
    { title: t('sales_agents:pay.form.min_pct', 'From %'), dataIndex: 'min_pct', key: 'min_pct' },
    { title: t('sales_agents:pay.form.multiplier', 'Multiplier'), dataIndex: 'multiplier', key: 'multiplier' },
  ];
  const tiersTable = (tiers) => <Table rowKey="from_unit" size="small" pagination={false} columns={tierColumns} dataSource={tiers} />;

  let body = <Spin />;
  if (versionQuery.isError) {
    body = <Alert type="error" showIcon message={extractApiErrorMessage(versionQuery.error, t('ui.common.error_occurred', 'An error occurred'))} />;
  } else if (detail) {
    const bonus = detail.new_outlet_bonus;
    body = (
      <>
        <Divider orientation="left">{t('sales_agents:pay.form.default_tiers', 'Default tiers')}</Divider>
        <div data-testid="view-default-tiers">{tiersTable(detail.default_tiers)}</div>

        <Divider orientation="left">{t('sales_agents:pay.form.product_tiers', 'Product tiers')}</Divider>
        <Space direction="vertical" style={{ width: '100%' }}>
          {detail.rates.length ? detail.rates.map((rate) => (
            <div key={rate.product_id} data-testid={`view-product-${rate.product_id}`}>
              <Space size={4}>
                <Text strong>{productName(rate.product_name, i18n.language)}</Text>
                {rate.product_is_active ? null : <Tag>{t('sales_agents:pay.form.inactive_product', '(inactive)')}</Tag>}
              </Space>
              {tiersTable(rate.tiers)}
            </div>
          )) : <Text type="secondary">—</Text>}
        </Space>

        <Divider orientation="left">{t('sales_agents:pay.form.gate_bands', 'Plan-vs-fact bands')}</Divider>
        <Space direction="vertical" style={{ width: '100%' }}>
          <div data-testid="view-bands">
            <Table rowKey="min_pct" size="small" pagination={false} columns={bandColumns} dataSource={detail.gate_bands} />
          </div>
          <Descriptions column={1} bordered size="small" data-testid="view-gate">
            <Descriptions.Item label={t('sales_agents:pay.form.min_visits_due', 'Min visits due')}>{detail.gate_min_visits_due}</Descriptions.Item>
          </Descriptions>
        </Space>

        <Divider orientation="left">{t('sales_agents:pay.form.bonus_amount', 'New-outlet bonus')}</Divider>
        <Descriptions column={1} bordered size="small" data-testid="view-bonus">
          <Descriptions.Item label={t('sales_agents:pay.form.bonus_amount', 'New-outlet bonus')}>{signedMoney(bonus.amount)}</Descriptions.Item>
          <Descriptions.Item label={t('sales_agents:pay.form.bonus_window_days', 'Window (days)')}>{bonus.window_days}</Descriptions.Item>
          <Descriptions.Item label={t('sales_agents:pay.form.bonus_lookback_days', 'Prior-customer lookback (days)')}>{bonus.prior_customer_lookback_days}</Descriptions.Item>
          <Descriptions.Item label={t('sales_agents:pay.form.bonus_min_orders_with_total', 'Orders with total')}>{bonus.min_orders_with_total}</Descriptions.Item>
          <Descriptions.Item label={t('sales_agents:pay.form.bonus_min_combined_total', 'Min combined total')}>{signedMoney(bonus.min_combined_total)}</Descriptions.Item>
          <Descriptions.Item label={t('sales_agents:pay.form.bonus_min_orders_any', 'Orders of any amount')}>{bonus.min_orders_any_amount}</Descriptions.Item>
        </Descriptions>

        <Divider />
        <Descriptions column={1} bordered size="small" data-testid="view-note">
          <Descriptions.Item label={t('sales_agents:pay.form.note', 'Note')}>{detail.note || '—'}</Descriptions.Item>
        </Descriptions>
      </>
    );
  }

  return (
    <Drawer
      open
      width={600}
      onClose={onClose}
      title={t('sales_agents:pay.plans.view_title', { defaultValue: '{{plan}} · version {{version}}', plan: plan.name, version: version.version_no })}
    >
      <Descriptions column={1} bordered size="small" data-testid="view-months">
        <Descriptions.Item label={t('sales_agents:pay.col.effective_month', 'Effective month')}>{monthLabel(version.effective_month)}</Descriptions.Item>
        <Descriptions.Item label={t('sales_agents:pay.col.applies_to', 'Applies to')}>{appliesToLabel(t, version)}</Descriptions.Item>
      </Descriptions>
      {body}
    </Drawer>
  );
};

export default PlanVersionView;
