import React, { useState } from 'react';
import { Navigate } from 'react-router-dom';
import { Alert, Button, Card, Select, Space, Table, Tag, Typography, message } from 'antd';
import { PlusOutlined } from '@ant-design/icons';
import { keepPreviousData, useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import salesPayService from '../services/salesPayService';
import staffService from '../services/staffService';
import PenaltyFormModal, { penaltyTypeName } from '../components/sales/pay/PenaltyFormModal';
import { useAuthStore } from '../stores/authStore';
import { fetchAllPages } from '../utils/pagination';
import { BULK_LOAD_PAGE_SIZE, DEFAULT_PAGE_SIZE } from '../utils/constants';
import { extractApiErrorMessage } from '../utils/apiError';
import { formatDate, formatDateTimeShort } from '../utils/dateUtils';

const { Title, Text } = Typography;

/**
 * Penalty proposals: the manager's page (spec §6.3, §5.3).
 *
 * A manager proposes a penalty with a reason and evidence; an administrator confirms or rejects it
 * on the Compensation page, so an administrator is sent there. Nothing on this page is money:
 * M1 publishes no amount, default amount, posting month or admin note (C11), and the propose form
 * has no amount field. Whether a proposal is allowed (self-proposal, a locked trial month) is the
 * backend's call, explained inline by the form (§6.5); the pickers are not pre-filtered.
 */
const PenaltyProposals = () => {
  const { t, i18n } = useTranslation(['sales_agents', 'common']);
  const { hasPermission } = useAuthStore();
  const canManagePay = hasPermission('can_manage_sales_pay');

  const [status, setStatus] = useState();
  const [agentId, setAgentId] = useState();
  const [page, setPage] = useState(1);
  const [proposing, setProposing] = useState(false);

  const proposalsQuery = useQuery({
    queryKey: ['penaltyProposals', status, agentId, page],
    queryFn: () => salesPayService.getPenaltyProposals({ status, agentId, page, perPage: DEFAULT_PAGE_SIZE }),
    // An administrator never reads the manager route: they land on the full Penalties tab below.
    enabled: !canManagePay,
    placeholderData: keepPreviousData,
  });
  const listing = proposalsQuery.data;

  // Same query key as Outlets.js and Visits.js, so react-query serves every page from one fetch.
  const { data: agentsData } = useQuery({
    queryKey: ['salesAgentOptions'],
    queryFn: () => fetchAllPages(
      (agentsPage) => staffService.getSalesAgents({ page: agentsPage, per_page: BULK_LOAD_PAGE_SIZE }),
      (resp) => resp?.data?.data?.items || [],
      BULK_LOAD_PAGE_SIZE,
    ),
    staleTime: 60_000,
    enabled: !canManagePay,
  });
  const agents = agentsData || [];

  if (canManagePay) {
    return <Navigate to="/sales/compensation?tab=penalties" replace />;
  }

  const resetToFirstPage = (setter) => (value) => {
    setter(value);
    setPage(1);
  };

  const columns = [
    {
      title: t('sales_agents:pay.proposals.col.incident_date', 'Incident date'),
      dataIndex: 'incident_date',
      key: 'incident_date',
      render: (value) => formatDate(value),
    },
    {
      title: t('sales_agents:pay.proposals.col.agent', 'Agent'),
      key: 'agent',
      render: (_, row) => row.agent?.name || '—',
    },
    {
      title: t('sales_agents:pay.proposals.col.type', 'Type'),
      key: 'type',
      render: (_, row) => penaltyTypeName(row.type, i18n?.language),
    },
    {
      title: t('sales_agents:pay.proposals.col.reason', 'Reason'),
      dataIndex: 'reason',
      key: 'reason',
      render: (value) => <Text style={{ whiteSpace: 'pre-wrap' }}>{value}</Text>,
    },
    {
      title: t('sales_agents:pay.proposals.col.evidence', 'Evidence'),
      dataIndex: 'evidence',
      key: 'evidence',
      render: (value) => <Text style={{ whiteSpace: 'pre-wrap' }}>{value}</Text>,
    },
    {
      title: t('sales_agents:pay.proposals.col.proposed_by', 'Proposed by'),
      key: 'proposed_by',
      render: (_, row) => row.proposed_by?.name || '—',
    },
    {
      title: t('sales_agents:pay.proposals.col.status', 'Status'),
      dataIndex: 'status',
      key: 'status',
      render: (value) => <Tag>{t(`sales_agents:pay.penalty_status.${value}`, value)}</Tag>,
    },
    {
      title: t('sales_agents:pay.proposals.col.decided_at', 'Decided at'),
      dataIndex: 'decided_at',
      key: 'decided_at',
      render: (value) => (value ? formatDateTimeShort(value) : '—'),
    },
  ];

  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <Space style={{ width: '100%', justifyContent: 'space-between' }} wrap>
        <Title level={3} style={{ margin: 0 }}>{t('sales_agents:pay.proposals.title', 'Penalty proposals')}</Title>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setProposing(true)}>
          {t('sales_agents:pay.proposals.propose', 'Propose penalty')}
        </Button>
      </Space>

      <Card size="small">
        <Space wrap>
          <Select
            data-testid="penalty-proposals-status"
            allowClear
            style={{ minWidth: 180 }}
            placeholder={t('sales_agents:pay.proposals.col.status', 'Status')}
            value={status}
            options={(listing?.statuses || []).map((value) => ({
              value,
              label: t(`sales_agents:pay.penalty_status.${value}`, value),
            }))}
            onChange={resetToFirstPage(setStatus)}
          />
          <Select
            data-testid="penalty-proposals-agent"
            allowClear
            showSearch
            optionFilterProp="label"
            style={{ minWidth: 220 }}
            placeholder={t('sales_agents:agent', 'Agent')}
            value={agentId}
            options={agents.map((agent) => ({ value: agent.user_id, label: agent.full_name }))}
            onChange={resetToFirstPage(setAgentId)}
          />
        </Space>
      </Card>

      {proposalsQuery.isError ? (
        <Alert
          type="error"
          showIcon
          message={extractApiErrorMessage(proposalsQuery.error, t('ui.common.error_occurred', 'An error occurred'))}
        />
      ) : null}

      <Table
        rowKey="id"
        columns={columns}
        dataSource={listing?.items || []}
        loading={proposalsQuery.isFetching}
        locale={{ emptyText: t('sales_agents:pay.empty.proposals', 'No penalty proposals yet.') }}
        pagination={{
          current: page,
          pageSize: DEFAULT_PAGE_SIZE,
          total: listing?.meta?.total || 0,
          showSizeChanger: false,
          onChange: setPage,
        }}
      />

      {/* Mounted only while open, so every proposal starts from an empty form. The types are M1's:
          active only, names only (a manager never reads A19). The modal invalidates
          ['penaltyProposals'], calls onSaved and closes; the success toast is this page's. */}
      {proposing ? (
        <PenaltyFormModal
          open
          mode="propose"
          types={listing?.types || []}
          onSaved={() => message.success(t('sales_agents:pay.proposals.sent', 'Proposal sent to an administrator'))}
          onClose={() => setProposing(false)}
        />
      ) : null}
    </Space>
  );
};

export default PenaltyProposals;
