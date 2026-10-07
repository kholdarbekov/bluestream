import React, { useState } from 'react';
import { Button, Space, Table, Tag, Typography } from 'antd';
import { useTranslation } from 'react-i18next';
import AdjustmentModal from './AdjustmentModal';
import PenaltyFormModal, { penaltyTypeName } from './PenaltyFormModal';
import PenaltyNoteModal from './PenaltyNoteModal';
import { deduction, instant, monthLabel, signedMoney } from './payFormat';

const { Title } = Typography;

/**
 * The drawer's last tab (§6.2 item 5). A carry row is labelled by its source and
 * `carried_from_month`, never by its stored system reason (§4.9); a netted owed balance is not
 * an adjustment and shows only as the Summary's Carried-in row.
 */
const PayPenaltiesAdjustments = ({ statement, agentId }) => {
  const { t, i18n } = useTranslation(['sales_agents', 'common']);
  const [penaltyOpen, setPenaltyOpen] = useState(false);
  const [adjustmentOpen, setAdjustmentOpen] = useState(false);
  const [cancelling, setCancelling] = useState(null);

  const penaltyColumns = [
    { title: t('sales_agents:pay.col.incident_date', 'Incident date'), dataIndex: 'incident_date', key: 'incident_date' },
    { title: t('sales_agents:pay.col.type', 'Type'), key: 'type', render: (_, row) => penaltyTypeName(row.type, i18n.language) },
    { title: t('sales_agents:pay.col.reason', 'Reason'), dataIndex: 'reason', key: 'reason' },
    { title: t('sales_agents:pay.col.status', 'Status'), dataIndex: 'status', key: 'status', render: (value) => <Tag>{t(`sales_agents:pay.penalty_status.${value}`, value)}</Tag> },
    { title: t('sales_agents:pay.col.amount', 'Amount'), dataIndex: 'amount', key: 'amount', render: (value) => (value == null ? '—' : deduction(value)) },
    {
      title: '',
      key: 'actions',
      render: (_, row) => (row.can_cancel
        ? <Button size="small" onClick={() => setCancelling(row)}>{t('sales_agents:pay.penalties.cancel_title', 'Cancel penalty')}</Button>
        : null),
    },
  ];

  const adjustmentColumns = [
    { title: t('sales_agents:pay.col.date', 'Date'), dataIndex: 'created_at', key: 'created_at', render: instant },
    { title: t('sales_agents:pay.col.source', 'Source'), dataIndex: 'source', key: 'source', render: (value) => <Tag>{t(`sales_agents:pay.adjustment_source.${value}`, value)}</Tag> },
    { title: t('sales_agents:pay.col.amount', 'Amount'), dataIndex: 'amount', key: 'amount', render: (value) => signedMoney(value, { plus: true }) },
    {
      title: t('sales_agents:pay.col.reason', 'Reason'),
      key: 'reason',
      render: (_, row) => (row.source === 'carry_forward'
        ? t('sales_agents:pay.formula.carry_in', { defaultValue: 'Shortfall from {{month}}', month: monthLabel(row.carried_from_month) })
        : row.reason),
    },
    { title: t('sales_agents:pay.col.created_by', 'Created by'), key: 'created_by', render: (_, row) => row.created_by?.name || '—' },
  ];

  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      {statement.can_edit_inputs ? (
        <Space>
          <Button onClick={() => setPenaltyOpen(true)}>{t('sales_agents:pay.penalties.add', 'Add penalty')}</Button>
          <Button onClick={() => setAdjustmentOpen(true)}>{t('sales_agents:pay.adjustments.add', 'Add adjustment')}</Button>
        </Space>
      ) : null}
      <Title level={5}>{t('sales_agents:pay.penalties.title', 'Penalties')}</Title>
      <Table rowKey="id" size="small" pagination={false} columns={penaltyColumns} dataSource={statement.penalties} />
      <Title level={5}>{t('sales_agents:pay.adjustments.title', 'Adjustments')}</Title>
      <Table rowKey="id" size="small" pagination={false} columns={adjustmentColumns} dataSource={statement.adjustments} />

      <PenaltyFormModal open={penaltyOpen} mode="create" agentId={agentId} onClose={() => setPenaltyOpen(false)} />
      <AdjustmentModal open={adjustmentOpen} month={statement.month} agentId={agentId} agentName={statement.agent.name} mode="signed" onClose={() => setAdjustmentOpen(false)} />
      <PenaltyNoteModal open={Boolean(cancelling)} kind="cancel" penalty={cancelling} onClose={() => setCancelling(null)} />
    </Space>
  );
};

export default PayPenaltiesAdjustments;
