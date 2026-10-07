import React, { useState } from 'react';
import { DatePicker, Descriptions, Form, Input, Modal, Select, Space, Typography } from 'antd';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import MoneyInput from '../../common/MoneyInput';
import salesPayService from '../../../services/salesPayService';
import staffService from '../../../services/staffService';
import { useAuthStore } from '../../../stores/authStore';
import { fetchAllPages } from '../../../utils/pagination';
import { BULK_LOAD_PAGE_SIZE } from '../../../utils/constants';
import { PayErrorAlert, isHandledPayError } from './payErrors';
import { monthLabel, signedMoney } from './payFormat';

const { Text } = Typography;

// A type's name in the admin's language; the three names are all required (A20), so `en` is
// only ever the fallback for a UI language the backend does not seed.
export const penaltyTypeName = (type, language) => {
  const names = new Map(Object.entries(type?.names || {}));
  return names.get(language) || names.get('en') || '—';
};

/**
 * One form, three doors (§6.2, §6.3):
 * - `create`: A23, the admin's penalty, confirmed at once; `amount` optional (the type's default).
 * - `confirm`: A24 on a proposal; the amount starts at the published `default_amount` and the
 *   modal says where it lands from the published `target_month` / `target_is_late`.
 * - `propose`: M2 for a manager (Task 13's page). No amount field exists in this mode; the types
 *   come from M1's `types` prop, because a manager has no `['salesPay','penaltyTypes']` query.
 */
const PenaltyFormModal = ({ open, mode, penalty, agentId, types, onSaved, onClose }) => {
  const { t, i18n } = useTranslation(['sales_agents', 'common']);
  const queryClient = useQueryClient();
  const canManagePay = useAuthStore().hasPermission('can_manage_sales_pay');
  const [form] = Form.useForm();
  const [refusal, setRefusal] = useState(null);

  const typesQuery = useQuery({
    queryKey: ['salesPay', 'penaltyTypes'],
    queryFn: () => salesPayService.getPenaltyTypes(),
    enabled: canManagePay && open && mode === 'create',
  });
  const agentsQuery = useQuery({
    queryKey: ['salesAgentOptions'],
    queryFn: () => fetchAllPages(
      (page) => staffService.getSalesAgents({ page, per_page: BULK_LOAD_PAGE_SIZE }),
      (resp) => resp?.data?.data?.items || [],
      BULK_LOAD_PAGE_SIZE,
    ),
    staleTime: 60_000,
    enabled: open && mode !== 'confirm',
  });

  const close = () => { setRefusal(null); onClose(); };

  const typeOptions = (mode === 'propose' ? (types || []) : (typesQuery.data?.items || []).filter((type) => type.is_active))
    .map((type) => ({ value: type.id, label: penaltyTypeName(type, i18n.language) }));
  const agentOptions = (agentsQuery.data || []).map((agent) => ({ value: agent.user_id, label: agent.full_name }));

  const mutation = useMutation({
    mutationFn: (values) => {
      if (mode === 'confirm') return salesPayService.confirmPenalty(penalty.id, { amount: values.amount });
      const payload = {
        agent_user_id: values.agent_user_id,
        penalty_type_id: values.penalty_type_id,
        incident_date: values.incident_date.format('YYYY-MM-DD'),
        reason: values.reason,
        evidence: values.evidence,
      };
      if (mode === 'propose') return salesPayService.proposePenalty(payload);
      return salesPayService.createPenalty(
        values.amount === null || values.amount === undefined ? payload : { ...payload, amount: values.amount },
      );
    },
    onSuccess: (result) => {
      queryClient.invalidateQueries({ queryKey: mode === 'propose' ? ['penaltyProposals'] : ['salesPay'] });
      if (onSaved) onSaved(result);
      close();
    },
    onError: (error) => setRefusal(isHandledPayError(error) ? error : null),
  });

  const titles = new Map([
    ['create', t('sales_agents:pay.penalties.add', 'Add penalty')],
    ['confirm', t('sales_agents:pay.penalties.confirm_title', 'Confirm penalty')],
    ['propose', t('sales_agents:pay.penalties.propose_title', 'Propose penalty')],
  ]);

  return (
    <Modal
      open={open}
      title={titles.get(mode)}
      okText={t('sales_agents:pay.form.save', 'Save')}
      confirmLoading={mutation.isPending}
      onOk={() => form.submit()}
      onCancel={close}
      destroyOnHidden
    >
      <Space direction="vertical" style={{ width: '100%' }}>
        {/* A24 can still answer TERMS_MISSING (`details.agents`) even though `can_confirm`
            already checked month routing; name the row's own agent, never the raw id. */}
        <PayErrorAlert error={refusal} agentNames={new Map(penalty?.agent ? [[penalty.agent.user_id, penalty.agent.name]] : [])} />
        {mode === 'confirm' && penalty ? (
          <Descriptions column={1} size="small" bordered>
            <Descriptions.Item label={t('sales_agents:pay.col.agent', 'Agent')}>{penalty.agent?.name}</Descriptions.Item>
            <Descriptions.Item label={t('sales_agents:pay.col.type', 'Type')}>{penaltyTypeName(penalty.type, i18n.language)}</Descriptions.Item>
            <Descriptions.Item label={t('sales_agents:pay.col.incident_date', 'Incident date')}>{penalty.incident_date}</Descriptions.Item>
            <Descriptions.Item label={t('sales_agents:pay.col.reason', 'Reason')}>{penalty.reason}</Descriptions.Item>
          </Descriptions>
        ) : null}
        <Form
          form={form}
          layout="vertical"
          preserve={false}
          // Remounted on every opening (`destroyOnHidden`), so these apply each time.
          initialValues={mode === 'confirm' ? { amount: penalty?.default_amount ?? null } : { agent_user_id: agentId ?? undefined }}
          onFinish={(values) => { setRefusal(null); mutation.mutate(values); }}
        >
          {mode !== 'confirm' ? (
            <>
              <Form.Item name="agent_user_id" label={t('sales_agents:pay.form.agent', 'Agent')} rules={[{ required: true, message: t('sales_agents:pay.form.required', 'Required') }]}>
                <Select showSearch optionFilterProp="label" options={agentOptions} loading={agentsQuery.isLoading} />
              </Form.Item>
              <Form.Item name="penalty_type_id" label={t('sales_agents:pay.form.type', 'Type')} rules={[{ required: true, message: t('sales_agents:pay.form.required', 'Required') }]}>
                <Select options={typeOptions} loading={typesQuery.isLoading} />
              </Form.Item>
              <Form.Item name="incident_date" label={t('sales_agents:pay.form.incident_date', 'Incident date')} rules={[{ required: true, message: t('sales_agents:pay.form.required', 'Required') }]}>
                <DatePicker style={{ width: '100%' }} format="DD.MM.YYYY" />
              </Form.Item>
              <Form.Item name="reason" label={t('sales_agents:pay.form.reason', 'Reason')} rules={[{ required: true, whitespace: true, message: t('sales_agents:pay.form.reason_required', 'Enter a reason') }]}>
                <Input.TextArea rows={2} maxLength={500} />
              </Form.Item>
              <Form.Item name="evidence" label={t('sales_agents:pay.form.evidence', 'Evidence')} rules={[{ required: true, whitespace: true, message: t('sales_agents:pay.form.required', 'Required') }]}>
                <Input.TextArea rows={3} maxLength={2000} />
              </Form.Item>
            </>
          ) : null}
          {mode === 'create' ? (
            <Form.Item name="amount" label={t('sales_agents:pay.form.amount', 'Amount')} extra={t('sales_agents:pay.form.amount_default_hint', "Leave empty to use the type's default amount.")}>
              <MoneyInput />
            </Form.Item>
          ) : null}
          {mode === 'confirm' ? (
            <>
              <Form.Item name="amount" label={t('sales_agents:pay.form.amount', 'Amount')} rules={[{ required: true, message: t('sales_agents:pay.form.amount_required', 'Enter an amount') }]}>
                <MoneyInput />
              </Form.Item>
              <Text data-testid="penalty-target">
                {t('sales_agents:pay.form.will_count_in', { defaultValue: 'Will count in {{month}}', month: monthLabel(penalty?.target_month) })}
                {penalty?.target_is_late ? ` ${t('sales_agents:pay.form.late', '(late)')}` : ''}
              </Text>
              <div><Text type="secondary">{`${t('sales_agents:pay.col.default_amount', 'Default amount')}: ${signedMoney(penalty?.default_amount)}`}</Text></div>
            </>
          ) : null}
        </Form>
      </Space>
    </Modal>
  );
};

export default PenaltyFormModal;
