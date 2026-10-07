import React, { useState } from 'react';
import { Button, Space, Table, Tag, Typography } from 'antd';
import { useTranslation } from 'react-i18next';
import UnpaidDaysModal from './UnpaidDaysModal';

const { Text } = Typography;

/**
 * The drawer's Days tab (§6.2 item 3): the frozen plan-vs-fact days, one "not counted" column
 * per published reason. The footer is the gate's own published totals, never a column sum.
 */
const PayDaysTable = ({ statement, agentId }) => {
  const { t } = useTranslation(['sales_agents', 'common']);
  const [unpaidOpen, setUnpaidOpen] = useState(false);
  const { gate } = statement.summary;

  const columns = [
    { title: t('sales_agents:pay.col.date', 'Date'), dataIndex: 'date', key: 'date' },
    { title: t('sales_agents:pay.col.weekday', 'Weekday'), dataIndex: 'weekday', key: 'weekday', render: (value) => t(`sales_agents:pay.weekday.${value}`, String(value)) },
    { title: t('sales_agents:pay.col.status', 'Status'), dataIndex: 'day_status', key: 'day_status', render: (value) => <Tag>{t(`sales_agents:pay.day_status.${value}`, value)}</Tag> },
    { title: t('sales_agents:pay.col.due', 'Due'), dataIndex: 'due', key: 'due', render: (value) => (value == null ? '—' : value) },
    { title: t('sales_agents:pay.col.counted', 'Counted'), dataIndex: 'counted', key: 'counted' },
    ...statement.not_counted_reasons.map((reason) => ({
      title: t(`sales_agents:pay.not_counted.${reason}`, reason),
      key: reason,
      render: (_, day) => (day.legacy
        ? <Text type="secondary">{t('sales_agents:pay.days.legacy', 'n/a (before frozen plans)')}</Text>
        : (new Map(Object.entries(day.not_counted || {})).get(reason) || 0)),
    })),
  ];

  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      {statement.can_edit_inputs ? (
        <Button onClick={() => setUnpaidOpen(true)}>{t('sales_agents:pay.days.unpaid_button', 'Unpaid days')}</Button>
      ) : null}
      <Table
        rowKey="date"
        size="small"
        pagination={false}
        scroll={{ x: 1000 }}
        columns={columns}
        dataSource={statement.days}
        footer={() => t('sales_agents:pay.days.footer', { defaultValue: 'Due {{due}} · Counted {{counted}}', due: gate.visits_due, counted: gate.visits_counted })}
      />
      {unpaidOpen ? <UnpaidDaysModal statement={statement} agentId={agentId} onClose={() => setUnpaidOpen(false)} /> : null}
    </Space>
  );
};

export default PayDaysTable;
