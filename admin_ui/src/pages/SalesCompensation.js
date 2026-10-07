import React from 'react';
import { Badge, Space, Tabs, Tag, Typography } from 'antd';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { useSearchParams } from 'react-router-dom';
import salesPayService from '../services/salesPayService';
import { useAuthStore } from '../stores/authStore';
import PayMonthsTab from '../components/sales/pay/PayMonthsTab';
import PayMonthView from '../components/sales/pay/PayMonthView';
import PayPlansTab from '../components/sales/pay/PayPlansTab';
import PenaltiesTab from '../components/sales/pay/PenaltiesTab';
import PenaltyTypesTab from '../components/sales/pay/PenaltyTypesTab';

const { Title } = Typography;
const TABS = ['months', 'plans', 'penalties', 'types'];

/**
 * /sales/compensation (spec §6.2), admins only: the route sits behind
 * `PermissionGuard permission="can_manage_sales_pay"`, and every query below is still gated on
 * the same flag (C29), so nothing is fetched if the page is ever rendered without it.
 * `?tab=` and `?month=` live in the URL (the Visits.js pattern), so a month view is linkable.
 */
const SalesCompensation = () => {
  const { t } = useTranslation(['sales_agents', 'common']);
  const canManagePay = useAuthStore().hasPermission('can_manage_sales_pay');
  const [searchParams, setSearchParams] = useSearchParams();
  const tab = TABS.includes(searchParams.get('tab')) ? searchParams.get('tab') : 'months';
  const month = searchParams.get('month');

  // The same key and fetcher as the nav badge (AdminLayout), so both read one cache entry.
  const periodsQuery = useQuery({
    queryKey: ['salesPay', 'periods'],
    queryFn: () => salesPayService.getPeriods(),
    enabled: canManagePay,
    staleTime: 60_000,
  });
  const periods = periodsQuery.data;
  const pending = periods?.pending_penalty_count || 0;
  const viewed = month ? (periods?.items || []).find((row) => row.month === month) : null;

  const items = [
    {
      key: 'months',
      label: t('sales_agents:pay.tab.months', 'Months'),
      children: month ? (
        <PayMonthView month={month} pendingPenaltyCount={pending} onBack={() => setSearchParams({ tab: 'months' })} />
      ) : (
        <PayMonthsTab periodsQuery={periodsQuery} onOpenMonth={(value) => setSearchParams({ tab: 'months', month: value })} />
      ),
    },
    { key: 'plans', label: t('sales_agents:pay.tab.plans', 'Plans'), children: <PayPlansTab active={tab === 'plans'} /> },
    {
      key: 'penalties',
      label: (
        <Space size={4}>
          {t('sales_agents:pay.tab.penalties', 'Penalties')}
          <Badge count={pending} size="small" />
        </Space>
      ),
      children: <PenaltiesTab active={tab === 'penalties'} />,
    },
    { key: 'types', label: t('sales_agents:pay.tab.types', 'Penalty types'), children: <PenaltyTypesTab /> },
  ];

  return (
    <div style={{ padding: 24 }}>
      <Space align="center" wrap style={{ marginBottom: 16 }}>
        <Title level={3} style={{ margin: 0 }}>{t('sales_agents:pay.title', 'Compensation')}</Title>
        {viewed?.is_shadow ? (
          <Tag color="purple">{t('sales_agents:pay.alert.shadow', 'Trial month: numbers are shown, pay is made the old way')}</Tag>
        ) : null}
      </Space>
      <Tabs activeKey={tab} onChange={(key) => setSearchParams({ tab: key })} items={items} />
    </div>
  );
};

export default SalesCompensation;
