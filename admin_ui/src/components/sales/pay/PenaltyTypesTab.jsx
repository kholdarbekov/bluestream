import React, { useState } from 'react';
import { Alert, Button, Space, Switch, Table } from 'antd';
import { PlusOutlined } from '@ant-design/icons';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import salesPayService from '../../../services/salesPayService';
import { useAuthStore } from '../../../stores/authStore';
import { extractApiErrorMessage } from '../../../utils/apiError';
import PenaltyTypeModal from './PenaltyTypeModal';
import { penaltyTypeName } from './PenaltyFormModal';
import { PayErrorAlert, isHandledPayError } from './payErrors';
import { signedMoney } from './payFormat';

/** The Penalty types tab (§6.2): A19 with an Active switch (A21); a type is never deleted. */
const PenaltyTypesTab = () => {
  const { t, i18n } = useTranslation(['sales_agents', 'common']);
  const queryClient = useQueryClient();
  const canManagePay = useAuthStore().hasPermission('can_manage_sales_pay');
  const [editing, setEditing] = useState(null);
  const [refusal, setRefusal] = useState(null);

  const typesQuery = useQuery({
    queryKey: ['salesPay', 'penaltyTypes'],
    queryFn: () => salesPayService.getPenaltyTypes(),
    enabled: canManagePay,
  });
  const toggle = useMutation({
    mutationFn: ({ id, isActive }) => salesPayService.updatePenaltyType(id, { is_active: isActive }),
    onSuccess: () => { setRefusal(null); queryClient.invalidateQueries({ queryKey: ['salesPay'] }); },
    onError: (error) => setRefusal(isHandledPayError(error) ? error : null),
  });

  const columns = [
    { title: t('sales_agents:pay.col.name', 'Name'), key: 'name', render: (_, type) => penaltyTypeName(type, i18n.language) },
    { title: t('sales_agents:pay.col.default_amount', 'Default amount'), dataIndex: 'default_amount', key: 'default_amount', render: (value) => signedMoney(value) },
    {
      title: t('sales_agents:pay.col.active', 'Active'),
      key: 'active',
      render: (_, type) => (
        <Switch checked={type.is_active} loading={toggle.isPending && toggle.variables?.id === type.id} onChange={(checked) => toggle.mutate({ id: type.id, isActive: checked })} />
      ),
    },
    { title: '', key: 'edit', render: (_, type) => <Button size="small" onClick={() => setEditing({ type })}>{t('sales_agents:pay.types.edit', 'Edit')}</Button> },
  ];

  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      {typesQuery.isError ? (
        <Alert type="error" showIcon message={extractApiErrorMessage(typesQuery.error, t('ui.common.error_occurred', 'An error occurred'))} />
      ) : null}
      <PayErrorAlert error={refusal} />
      <Button type="primary" icon={<PlusOutlined />} onClick={() => setEditing({ type: null })}>{t('sales_agents:pay.types.new', 'New type')}</Button>
      <Table rowKey="id" size="small" pagination={false} columns={columns} dataSource={typesQuery.data?.items || []} loading={typesQuery.isLoading} />
      {editing ? <PenaltyTypeModal type={editing.type} onClose={() => setEditing(null)} /> : null}
    </Space>
  );
};

export default PenaltyTypesTab;
