import React, { useState } from 'react';
import {
  Alert, Button, Card, Col, Modal, Row, Space, Spin, Statistic, Table, Tag, Tooltip, Typography,
} from 'antd';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router-dom';
import salesPayService from '../../../services/salesPayService';
import { useAuthStore } from '../../../stores/authStore';
import { extractApiErrorMessage } from '../../../utils/apiError';
import HolidaysModal from './HolidaysModal';
import MarkPaidModal from './MarkPaidModal';
import PayStatementDrawer from './PayStatementDrawer';
import { ShortfallNote } from './PayFormula';
import { STATUS_COLORS } from './PayMonthsTab';
import { PayErrorAlert, isHandledPayError } from './payErrors';
import { deduction, instant, monthLabel, signedMoney } from './payFormat';

const { Title, Text } = Typography;

// One label per published action; `recalculate` reads "Sync now" while the month is open,
// because an open month's figures are a live estimate (§6.2).
const actionLabel = (t, action, status) => {
  switch (action) {
    case 'close': return t('sales_agents:pay.action.close', 'Close');
    case 'recalculate': return status === 'open'
      ? t('sales_agents:pay.action.sync_now', 'Sync now')
      : t('sales_agents:pay.action.recalculate', 'Recalculate');
    case 'approve': return t('sales_agents:pay.action.approve', 'Approve');
    case 'mark_paid': return t('sales_agents:pay.action.mark_paid', 'Mark paid');
    default: return action;
  }
};

const runAction = (action, month) => {
  switch (action) {
    case 'close': return salesPayService.closePeriod(month);
    case 'approve': return salesPayService.approvePeriod(month);
    default: return salesPayService.recalculatePeriod(month);
  }
};

/**
 * One month (§6.2 "Month view"): A2's header, alerts, the action buttons drawn ONLY from
 * `next_actions`, the totals and the agents table. A row opens that agent's statement drawer.
 */
const PayMonthView = ({ month, pendingPenaltyCount, onBack }) => {
  const { t } = useTranslation(['sales_agents', 'common']);
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const canManagePay = useAuthStore().hasPermission('can_manage_sales_pay');
  const [refusal, setRefusal] = useState(null);
  const [markPaidOpen, setMarkPaidOpen] = useState(false);
  const [holidaysOpen, setHolidaysOpen] = useState(false);
  const [drawerAgentId, setDrawerAgentId] = useState(null);

  const periodQuery = useQuery({
    queryKey: ['salesPay', 'period', month],
    queryFn: () => salesPayService.getPeriod(month),
    enabled: canManagePay && Boolean(month),
  });
  const mutation = useMutation({
    mutationFn: (action) => runAction(action, month),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['salesPay'] }),
    onError: (error) => setRefusal(isHandledPayError(error) ? error : null),
  });
  // The confirm closes either way; a named refusal is then read inline, in this view.
  const perform = (action) => { setRefusal(null); return mutation.mutateAsync(action).catch(() => {}); };

  if (periodQuery.isError) {
    return <Alert type="error" showIcon message={extractApiErrorMessage(periodQuery.error, t('ui.common.error_occurred', 'An error occurred'))} />;
  }
  const detail = periodQuery.data;
  if (!detail) return <Spin />;

  const label = monthLabel(detail.month);
  const agentNames = new Map([
    ...detail.agents.map((row) => [row.agent_user_id, row.agent_name]),
    ...detail.unconfigured_agents.map((row) => [row.agent_user_id, row.agent_name]),
  ]);
  const unsynced = [...(detail.sync.stats?.failed || []), ...(detail.sync.stats?.skipped_no_terms || [])];
  const selfDecidedNames = detail.agents.filter((row) => row.self_decided).map((row) => row.agent_name);

  const confirmClose = () => Modal.confirm({
    title: t('sales_agents:pay.confirm.close.title', { defaultValue: 'Close {{month}}?', month: label }),
    content: t('sales_agents:pay.confirm.close.body', 'The numbers are frozen into statements. You can still add penalties or adjustments and recalculate before approving.'),
    okText: actionLabel(t, 'close', detail.status),
    onOk: () => perform('close'),
  });
  const confirmApprove = () => Modal.confirm({
    title: t('sales_agents:pay.confirm.approve.title', { defaultValue: 'Approve {{month}}?', month: label }),
    content: (
      <Space direction="vertical">
        <Text>{t('sales_agents:pay.confirm.approve.body', 'Statements are locked permanently and each agent receives their final statement in the staff bot.')}</Text>
        {pendingPenaltyCount > 0 ? (
          <Text type="warning">
            {detail.is_shadow
              ? t('sales_agents:pay.confirm.approve.pending_shadow', { defaultValue: '{{n}} penalty proposals are still pending. Proposals for incidents in this trial month can no longer be confirmed once it is approved; confirm or reject them first.', n: pendingPenaltyCount })
              : t('sales_agents:pay.confirm.approve.pending', { defaultValue: '{{n}} penalty proposals are still pending. If confirmed later, they land in the next open month as late.', n: pendingPenaltyCount })}
          </Text>
        ) : null}
        {selfDecidedNames.length > 0 ? (
          <Text type="warning">
            {t('sales_agents:pay.confirm.approve.self_decided', { defaultValue: 'Statements with decisions admins made about their own pay: {{names}}.', names: selfDecidedNames.join(', ') })}
          </Text>
        ) : null}
      </Space>
    ),
    okText: actionLabel(t, 'approve', detail.status),
    onOk: () => perform('approve'),
  });
  const onAction = (action) => {
    if (action === 'close') confirmClose();
    else if (action === 'approve') confirmApprove();
    else if (action === 'mark_paid') setMarkPaidOpen(true);
    else perform(action);
  };

  const columns = [
    {
      title: t('sales_agents:pay.col.agent', 'Agent'),
      key: 'agent',
      render: (_, row) => (
        <Space size={4} wrap>
          {row.agent_name}
          {row.self_decided ? (
            <Tooltip title={t('sales_agents:pay.self_decided.tooltip', 'Includes decisions this person made about their own pay.')}>
              <Tag color="orange">{t('sales_agents:pay.self_decided.tag', 'Self-decided')}</Tag>
            </Tooltip>
          ) : null}
        </Space>
      ),
    },
    { title: t('sales_agents:pay.col.days', 'Days'), key: 'days', render: (_, row) => `${row.worked_days}/${row.working_days}` },
    { title: t('sales_agents:pay.col.base', 'Base'), key: 'base', render: (_, row) => signedMoney(row.base_amount) },
    { title: t('sales_agents:pay.col.commission', 'Commission'), key: 'commission', render: (_, row) => signedMoney(row.commission) },
    {
      title: t('sales_agents:pay.col.plan_vs_fact', 'Plan vs fact'),
      key: 'plan_vs_fact',
      render: (_, row) => (row.compliance_pct == null
        ? '—'
        : `${row.compliance_pct}% (${row.visits_counted}/${row.visits_due}) → ×${row.gate_multiplier}`),
    },
    { title: t('sales_agents:pay.col.late', 'Late'), key: 'late', render: (_, row) => signedMoney(row.late_commission_after_gate) },
    { title: t('sales_agents:pay.col.after_discipline', 'After discipline'), key: 'after_discipline', render: (_, row) => signedMoney(row.gated_commission) },
    { title: t('sales_agents:pay.col.new_outlets', 'New outlets'), key: 'new_outlets', render: (_, row) => signedMoney(row.new_outlets) },
    { title: t('sales_agents:pay.col.adjustments', 'Adjustments'), key: 'adjustments', render: (_, row) => signedMoney(row.adjustments, { plus: true }) },
    { title: t('sales_agents:pay.col.penalties', 'Penalties'), key: 'penalties', render: (_, row) => deduction(row.penalties) },
    { title: t('sales_agents:pay.col.carry_in', 'Carried in'), key: 'carry_in', render: (_, row) => deduction(row.carry_in) },
    {
      title: t('sales_agents:pay.col.total', 'Total'),
      key: 'total',
      render: (_, row) => (
        <Space size={4} wrap>
          <Text strong>{signedMoney(row.total)}</Text>
          <ShortfallNote carryOut={row.carry_out} owed={row.owed} />
          {row.review_flags > 0 ? <Tag color="gold">{t('sales_agents:pay.col.review', 'review')}</Tag> : null}
        </Space>
      ),
    },
  ];

  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <Space wrap align="center">
        <Button onClick={onBack}>{t('sales_agents:pay.back', 'Back to months')}</Button>
        <Title level={4} style={{ margin: 0 }}>{label}</Title>
        <Tag color={STATUS_COLORS.get(detail.status)}>{t(`sales_agents:pay.status.${detail.status}`, detail.status)}</Tag>
        {detail.is_estimate ? (
          <Text type="secondary">{t('sales_agents:pay.alert.estimate_as_of', { defaultValue: 'Estimate as of {{time}}', time: instant(detail.as_of) })}</Text>
        ) : null}
      </Space>

      {detail.unconfigured_agents.length > 0 ? (
        <Alert
          type="info"
          showIcon
          message={t('sales_agents:pay.alert.unconfigured', 'Agents without pay terms:')}
          description={(
            <Space direction="vertical">
              {detail.unconfigured_agents.map((row) => (
                <Space key={row.agent_user_id}>
                  {row.agent_name}
                  <Button
                    size="small"
                    onClick={() => navigate('/sales/agents', { state: { payAgent: { user_id: row.agent_user_id, full_name: row.agent_name } } })}
                  >
                    {t('sales_agents:pay.alert.set_terms', 'Set pay terms')}
                  </Button>
                </Space>
              ))}
            </Space>
          )}
        />
      ) : null}
      {unsynced.length > 0 ? (
        <Alert
          type="warning"
          showIcon
          message={t('sales_agents:pay.alert.sync_failed', { defaultValue: '{{n}} orders could not be synced. Close will be refused until this is fixed.', n: unsynced.length })}
        />
      ) : null}
      <PayErrorAlert error={refusal} agentNames={agentNames} />

      <Space wrap>
        {detail.next_actions.map((action) => (
          <Button
            key={action}
            type={action === 'approve' ? 'primary' : 'default'}
            loading={mutation.isPending && mutation.variables === action}
            onClick={() => onAction(action)}
          >
            {actionLabel(t, action, detail.status)}
          </Button>
        ))}
        {detail.can_edit_inputs ? (
          <Button onClick={() => setHolidaysOpen(true)}>{t('sales_agents:pay.holidays.button', 'Holidays')}</Button>
        ) : null}
        {detail.status === 'open' && !detail.next_actions.includes('close') ? (
          <Text type="secondary">{t('sales_agents:pay.alert.closable_from', { defaultValue: 'Can be closed from {{time}}', time: instant(detail.closable_from) })}</Text>
        ) : null}
      </Space>

      <Row gutter={16}>
        <Col xs={12} md={6}><Card><Statistic title={t('sales_agents:pay.col.agents', 'Agents')} value={detail.agent_count} /></Card></Col>
        <Col xs={12} md={6}><Card><Statistic title={t('sales_agents:pay.col.base', 'Base')} value={signedMoney(detail.totals.base)} /></Card></Col>
        <Col xs={12} md={6}><Card><Statistic title={t('sales_agents:pay.col.variable', 'Variable')} value={signedMoney(detail.totals.variable)} /></Card></Col>
        <Col xs={12} md={6}><Card><Statistic title={t('sales_agents:pay.col.total', 'Total')} value={signedMoney(detail.totals.total)} /></Card></Col>
      </Row>

      <Table
        rowKey="agent_user_id"
        size="small"
        columns={columns}
        dataSource={detail.agents}
        pagination={false}
        scroll={{ x: 1300 }}
        onRow={(row) => ({ onClick: () => setDrawerAgentId(row.agent_user_id), style: { cursor: 'pointer' } })}
      />

      {markPaidOpen ? <MarkPaidModal month={detail.month} onClose={() => setMarkPaidOpen(false)} /> : null}
      {holidaysOpen ? <HolidaysModal detail={detail} onClose={() => setHolidaysOpen(false)} /> : null}
      <PayStatementDrawer month={detail.month} agentId={drawerAgentId} onClose={() => setDrawerAgentId(null)} />
    </Space>
  );
};

export default PayMonthView;
