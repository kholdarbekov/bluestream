import React, { useState } from 'react';
import { Alert, Button, Space, Table } from 'antd';
import { PlusOutlined } from '@ant-design/icons';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import salesPayService from '../../../services/salesPayService';
import { useAuthStore } from '../../../stores/authStore';
import { extractApiErrorMessage } from '../../../utils/apiError';
import PlanVersionModal from './PlanVersionModal';
import PlanVersionView from './PlanVersionView';
import { appliesToLabel, instant, monthLabel } from './payFormat';

/**
 * The Plans tab (§6.2): A12's plans, each expanding into its version history. A history row
 * shows the months the backend published for its version and opens it read-only.
 */
const PayPlansTab = ({ active }) => {
  const { t } = useTranslation(['sales_agents', 'common']);
  const canManagePay = useAuthStore().hasPermission('can_manage_sales_pay');
  const [editor, setEditor] = useState(null);
  const [viewing, setViewing] = useState(null);
  const plansQuery = useQuery({
    queryKey: ['salesPay', 'plans'],
    queryFn: () => salesPayService.getPlans(),
    enabled: canManagePay && Boolean(active),
  });
  const data = plansQuery.data;

  const versionColumns = (plan) => [
    { title: t('sales_agents:pay.col.version_no', 'No.'), dataIndex: 'version_no', key: 'version_no' },
    { title: t('sales_agents:pay.col.effective_month', 'Effective month'), dataIndex: 'effective_month', key: 'effective_month', render: monthLabel },
    { title: t('sales_agents:pay.col.applies_to', 'Applies to'), key: 'applies_to', render: (_, row) => appliesToLabel(t, row) },
    { title: t('sales_agents:pay.col.created_by', 'Created by'), key: 'created_by', render: (_, row) => `${row.created_by?.name || '—'} · ${instant(row.created_at)}` },
    { title: t('sales_agents:pay.col.note', 'Note'), dataIndex: 'note', key: 'note', render: (value) => value || '—' },
    {
      title: '',
      key: 'actions',
      render: (_, row) => (
        <Button size="small" onClick={() => setViewing({ plan, version: row })}>{t('sales_agents:pay.plans.view', 'View')}</Button>
      ),
    },
  ];
  const columns = [
    { title: t('sales_agents:pay.col.name', 'Name'), dataIndex: 'name', key: 'name' },
    {
      title: t('sales_agents:pay.col.in_force_since', 'In force since'),
      key: 'in_force',
      render: (_, plan) => (plan.version_in_force ? monthLabel(plan.version_in_force.effective_month) : '—'),
    },
    { title: t('sales_agents:pay.col.versions', 'Versions'), key: 'versions', render: (_, plan) => plan.versions.length },
    {
      title: '',
      key: 'actions',
      render: (_, plan) => (
        <Button size="small" disabled={!plan.version_in_force} onClick={() => setEditor({ mode: 'version', plan })}>
          {t('sales_agents:pay.plans.new_version', 'New version')}
        </Button>
      ),
    },
  ];

  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      {plansQuery.isError ? (
        <Alert type="error" showIcon message={extractApiErrorMessage(plansQuery.error, t('ui.common.error_occurred', 'An error occurred'))} />
      ) : null}
      <Button type="primary" icon={<PlusOutlined />} disabled={!data} onClick={() => setEditor({ mode: 'plan', plan: null })}>
        {t('sales_agents:pay.plans.new', 'New plan')}
      </Button>
      <Table
        rowKey="id"
        size="small"
        columns={columns}
        dataSource={data?.items || []}
        loading={plansQuery.isLoading}
        pagination={false}
        locale={{ emptyText: t('sales_agents:pay.empty.plans', 'No pay plans yet. Create one to set commission tiers.') }}
        expandable={{
          expandedRowRender: (plan) => (
            <Table rowKey="id" size="small" pagination={false} columns={versionColumns(plan)} dataSource={plan.versions} />
          ),
        }}
      />
      {editor && data ? <PlanVersionModal mode={editor.mode} plan={editor.plan} config={data} onClose={() => setEditor(null)} /> : null}
      {viewing ? <PlanVersionView plan={viewing.plan} version={viewing.version} onClose={() => setViewing(null)} /> : null}
    </Space>
  );
};

export default PayPlansTab;
