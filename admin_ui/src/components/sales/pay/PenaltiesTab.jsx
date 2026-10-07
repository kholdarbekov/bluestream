import React, { useState } from 'react';
import {
  Alert, Button, DatePicker, Popover, Select, Space, Table, Tag, Typography,
} from 'antd';
import { PlusOutlined } from '@ant-design/icons';
import { keepPreviousData, useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import salesPayService from '../../../services/salesPayService';
import staffService from '../../../services/staffService';
import { useAuthStore } from '../../../stores/authStore';
import { extractApiErrorMessage } from '../../../utils/apiError';
import { fetchAllPages } from '../../../utils/pagination';
import { BULK_LOAD_PAGE_SIZE, DEFAULT_PAGE_SIZE } from '../../../utils/constants';
import PenaltyFormModal, { penaltyTypeName } from './PenaltyFormModal';
import PenaltyNoteModal from './PenaltyNoteModal';
import { deduction, instant, monthLabel } from './payFormat';

const { Text } = Typography;

/**
 * The Penalties tab (§6.2): A22 filtered by status (proposals first), agent and posting month.
 * Every action is drawn from the row's own `can_*` flags; the confirm modal shows the published
 * `target_month`, so the admin sees where a penalty lands before confirming it.
 */
const PenaltiesTab = ({ active }) => {
  const { t, i18n } = useTranslation(['sales_agents', 'common']);
  const canManagePay = useAuthStore().hasPermission('can_manage_sales_pay');
  const [status, setStatus] = useState('proposed');
  const [agentId, setAgentId] = useState();
  const [month, setMonth] = useState();
  const [page, setPage] = useState(1);
  const [confirming, setConfirming] = useState(null);
  const [noting, setNoting] = useState(null);
  const [createOpen, setCreateOpen] = useState(false);

  const penaltiesQuery = useQuery({
    queryKey: ['salesPay', 'penalties', status, agentId, month, page],
    queryFn: () => salesPayService.getPenalties({ status, agentId, month, page, perPage: DEFAULT_PAGE_SIZE }),
    enabled: canManagePay && Boolean(active),
    placeholderData: keepPreviousData,
  });
  const agentsQuery = useQuery({
    queryKey: ['salesAgentOptions'],
    queryFn: () => fetchAllPages(
      (p) => staffService.getSalesAgents({ page: p, per_page: BULK_LOAD_PAGE_SIZE }),
      (resp) => resp?.data?.data?.items || [],
      BULK_LOAD_PAGE_SIZE,
    ),
    staleTime: 60_000,
    enabled: Boolean(active),
  });
  const data = penaltiesQuery.data;
  const narrow = (setter) => (value) => { setter(value); setPage(1); };

  const columns = [
    { title: t('sales_agents:pay.col.incident_date', 'Incident date'), dataIndex: 'incident_date', key: 'incident_date' },
    {
      title: t('sales_agents:pay.col.agent', 'Agent'),
      key: 'agent',
      render: (_, row) => (
        <Space size={4} wrap>
          {row.agent?.name}
          {row.self_decided ? <Tag color="orange">{t('sales_agents:pay.self_decided.tag', 'Self-decided')}</Tag> : null}
        </Space>
      ),
    },
    { title: t('sales_agents:pay.col.type', 'Type'), key: 'type', render: (_, row) => penaltyTypeName(row.type, i18n.language) },
    { title: t('sales_agents:pay.col.reason', 'Reason'), dataIndex: 'reason', key: 'reason' },
    {
      title: t('sales_agents:pay.col.evidence', 'Evidence'),
      dataIndex: 'evidence',
      key: 'evidence',
      render: (value) => (
        <Popover content={<div style={{ maxWidth: 360, whiteSpace: 'pre-wrap' }}>{value}</div>}>
          <Text ellipsis style={{ maxWidth: 160 }}>{value}</Text>
        </Popover>
      ),
    },
    { title: t('sales_agents:pay.col.origin', 'Origin'), dataIndex: 'origin', key: 'origin', render: (value) => t(`sales_agents:pay.penalty_origin.${value}`, value) },
    { title: t('sales_agents:pay.col.proposed_by', 'Proposed by'), key: 'proposed_by', render: (_, row) => `${row.proposed_by?.name || '—'} · ${instant(row.proposed_at)}` },
    { title: t('sales_agents:pay.col.status', 'Status'), dataIndex: 'status', key: 'status', render: (value) => <Tag>{t(`sales_agents:pay.penalty_status.${value}`, value)}</Tag> },
    { title: t('sales_agents:pay.col.amount', 'Amount'), dataIndex: 'amount', key: 'amount', render: (value) => (value == null ? '—' : deduction(value)) },
    {
      title: t('sales_agents:pay.col.posting_month', 'Month'),
      key: 'posting_month',
      render: (_, row) => (
        <Space size={4}>
          {row.posting_month ? monthLabel(row.posting_month) : '—'}
          {row.is_late ? <Tag color="orange">{t('sales_agents:pay.lines.late', 'late')}</Tag> : null}
        </Space>
      ),
    },
    {
      title: t('sales_agents:pay.col.actions', 'Actions'),
      key: 'actions',
      render: (_, row) => (
        <Space size={4} wrap>
          {row.can_confirm ? <Button size="small" type="primary" onClick={() => setConfirming(row)}>{t('sales_agents:pay.penalties.confirm', 'Confirm')}</Button> : null}
          {row.can_reject ? <Button size="small" onClick={() => setNoting({ kind: 'reject', penalty: row })}>{t('sales_agents:pay.penalties.reject', 'Reject')}</Button> : null}
          {row.can_cancel ? <Button size="small" onClick={() => setNoting({ kind: 'cancel', penalty: row })}>{t('sales_agents:pay.penalties.cancel_title', 'Cancel penalty')}</Button> : null}
        </Space>
      ),
    },
  ];

  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      {penaltiesQuery.isError ? (
        <Alert type="error" showIcon message={extractApiErrorMessage(penaltiesQuery.error, t('ui.common.error_occurred', 'An error occurred'))} />
      ) : null}
      <Space wrap>
        <Select
          allowClear
          style={{ width: 180 }}
          placeholder={t('sales_agents:pay.col.status', 'Status')}
          value={status}
          onChange={narrow(setStatus)}
          options={(data?.statuses || []).map((value) => ({ value, label: t(`sales_agents:pay.penalty_status.${value}`, value) }))}
        />
        <Select
          allowClear
          showSearch
          optionFilterProp="label"
          style={{ width: 220 }}
          placeholder={t('sales_agents:pay.col.agent', 'Agent')}
          value={agentId}
          onChange={narrow(setAgentId)}
          options={(agentsQuery.data || []).map((agent) => ({ value: agent.user_id, label: agent.full_name }))}
        />
        <DatePicker
          picker="month"
          format="MM.YYYY"
          placeholder={t('sales_agents:pay.col.posting_month', 'Month')}
          onChange={(value) => narrow(setMonth)(value ? value.format('YYYY-MM') : undefined)}
        />
        <Button icon={<PlusOutlined />} onClick={() => setCreateOpen(true)}>{t('sales_agents:pay.penalties.add', 'Add penalty')}</Button>
      </Space>
      <Table
        rowKey="id"
        size="small"
        columns={columns}
        dataSource={data?.items || []}
        loading={penaltiesQuery.isLoading}
        scroll={{ x: 1300 }}
        pagination={{ current: page, pageSize: DEFAULT_PAGE_SIZE, total: data?.meta?.total || 0, showSizeChanger: false, onChange: setPage }}
      />
      <PenaltyFormModal open={Boolean(confirming)} mode="confirm" penalty={confirming} onClose={() => setConfirming(null)} />
      <PenaltyFormModal open={createOpen} mode="create" onClose={() => setCreateOpen(false)} />
      <PenaltyNoteModal open={Boolean(noting)} kind={noting?.kind} penalty={noting?.penalty} onClose={() => setNoting(null)} />
    </Space>
  );
};

export default PenaltiesTab;
