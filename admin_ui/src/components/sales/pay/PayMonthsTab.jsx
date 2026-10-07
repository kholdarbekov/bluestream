import React, { useState } from 'react';
import { Alert, Button, Empty, Table, Tag } from 'antd';
import { useTranslation } from 'react-i18next';
import { extractApiErrorMessage } from '../../../utils/apiError';
import StartPayModal from './StartPayModal';
import { instant, monthLabel, signedMoney } from './payFormat';

// Status colours are display only; the words are `pay.status.*` (pinned to the tuple).
export const STATUS_COLORS = new Map([['open', 'blue'], ['closed', 'orange'], ['approved', 'green'], ['paid', 'default']]);

/**
 * The Months tab (§6.2): before start, one "Start with {month}" door; after start, A1's rows,
 * newest first as published. A row opens its month view (`?month=`).
 */
const PayMonthsTab = ({ periodsQuery, onOpenMonth }) => {
  const { t } = useTranslation(['sales_agents', 'common']);
  const [startOpen, setStartOpen] = useState(false);
  const periods = periodsQuery.data;

  if (periodsQuery.isError) {
    return <Alert type="error" showIcon message={extractApiErrorMessage(periodsQuery.error, t('ui.common.error_occurred', 'An error occurred'))} />;
  }
  if (periods && !periods.started) {
    return (
      <>
        <Empty description={t('sales_agents:pay.start.empty', 'Pay tracking has not started.')}>
          <Button type="primary" onClick={() => setStartOpen(true)}>
            {t('sales_agents:pay.start.button', { defaultValue: 'Start with {{month}}', month: monthLabel(periods.startable_month) })}
          </Button>
        </Empty>
        {startOpen ? <StartPayModal month={periods.startable_month} onClose={() => setStartOpen(false)} /> : null}
      </>
    );
  }

  const columns = [
    { title: t('sales_agents:pay.col.month', 'Month'), dataIndex: 'month', key: 'month', render: monthLabel },
    {
      title: t('sales_agents:pay.col.status', 'Status'),
      dataIndex: 'status',
      key: 'status',
      render: (value) => <Tag color={STATUS_COLORS.get(value)}>{t(`sales_agents:pay.status.${value}`, value)}</Tag>,
    },
    { title: t('sales_agents:pay.col.agents', 'Agents'), dataIndex: 'agent_count', key: 'agent_count' },
    { title: t('sales_agents:pay.col.base', 'Base'), key: 'base', render: (_, row) => signedMoney(row.totals.base) },
    { title: t('sales_agents:pay.col.variable', 'Variable'), key: 'variable', render: (_, row) => signedMoney(row.totals.variable) },
    {
      title: t('sales_agents:pay.col.total', 'Total'),
      key: 'total',
      render: (_, row) => (
        <>
          {signedMoney(row.totals.total)}
          {row.is_estimate ? <Tag style={{ marginLeft: 8 }}>{t('sales_agents:pay.col.estimate', 'estimate')}</Tag> : null}
        </>
      ),
    },
    { title: t('sales_agents:pay.col.last_sync', 'Last sync'), dataIndex: 'last_synced_at', key: 'last_synced_at', render: instant },
  ];

  return (
    <Table
      rowKey="month"
      size="small"
      columns={columns}
      dataSource={periods?.items || []}
      loading={periodsQuery.isLoading}
      pagination={false}
      onRow={(row) => ({ onClick: () => onOpenMonth(row.month), style: { cursor: 'pointer' } })}
    />
  );
};

export default PayMonthsTab;
