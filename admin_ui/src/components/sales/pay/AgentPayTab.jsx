import React, { useState } from 'react';
import {
  Alert, Button, DatePicker, Descriptions, Form, Input, Modal, Select, Space, Spin, Table, Tag,
} from 'antd';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import dayjs from 'dayjs';
import MoneyInput from '../../common/MoneyInput';
import salesPayService from '../../../services/salesPayService';
import { useAuthStore } from '../../../stores/authStore';
import { extractApiErrorMessage } from '../../../utils/apiError';
import AdjustmentModal from './AdjustmentModal';
import { PayErrorAlert, isHandledPayError } from './payErrors';
import { dateLabel, monthLabel, signedMoney } from './payFormat';

// One door per write: A18 (employment) and A17 (new terms). Each keeps its own refusal inline.
const usePayWrite = (mutationFn, onDone) => {
  const queryClient = useQueryClient();
  const [refusal, setRefusal] = useState(null);
  const mutation = useMutation({
    mutationFn,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['salesPay'] });
      setRefusal(null);
      onDone();
    },
    onError: (error) => setRefusal(isHandledPayError(error) ? error : null),
  });
  return { mutation, refusal, clear: () => setRefusal(null) };
};

const EmploymentModal = ({ open, agentUserId, employment, onClose }) => {
  const { t } = useTranslation(['sales_agents', 'common']);
  const [form] = Form.useForm();
  const { mutation, refusal, clear } = usePayWrite(
    ({ start, end }) => salesPayService.setEmployment(agentUserId, {
      start: start.format('YYYY-MM-DD'),
      end: end ? end.format('YYYY-MM-DD') : null,
    }),
    onClose,
  );
  const close = () => { clear(); onClose(); };
  return (
    <Modal
      open={open}
      title={t('sales_agents:pay.terms.employment_title', 'Employment dates')}
      okText={t('sales_agents:pay.form.save', 'Save')}
      confirmLoading={mutation.isPending}
      onOk={() => form.submit()}
      onCancel={close}
      destroyOnHidden
    >
      <Space direction="vertical" style={{ width: '100%' }}>
        {/* A18 names EVERY month the change would reach (§4.10); `payErrorText` lists them. */}
        <PayErrorAlert error={refusal} />
        <Form
          form={form}
          layout="vertical"
          preserve={false}
          initialValues={{
            start: employment.start ? dayjs(employment.start) : undefined,
            end: employment.end ? dayjs(employment.end) : undefined,
          }}
          onFinish={(values) => { clear(); mutation.mutate(values); }}
        >
          <Form.Item name="start" label={t('sales_agents:pay.form.start', 'Employment start')} rules={[{ required: true, message: t('sales_agents:pay.form.required', 'Required') }]}>
            <DatePicker style={{ width: '100%' }} format="DD.MM.YYYY" />
          </Form.Item>
          <Form.Item name="end" label={t('sales_agents:pay.form.end', 'Employment end')}>
            <DatePicker style={{ width: '100%' }} format="DD.MM.YYYY" allowClear />
          </Form.Item>
        </Form>
      </Space>
    </Modal>
  );
};

const TermsModal = ({ open, agentUserId, plans, editableFromMonth, onClose }) => {
  const { t } = useTranslation(['sales_agents', 'common']);
  const [form] = Form.useForm();
  const { mutation, refusal, clear } = usePayWrite(
    ({ effective_month: month, base_salary: baseSalary, plan_id: planId, note }) => salesPayService.addAgentTerms(agentUserId, {
      effective_month: month.format('YYYY-MM'),
      base_salary: baseSalary,
      plan_id: planId,
      note: note || null,
    }),
    onClose,
  );
  const close = () => { clear(); onClose(); };
  return (
    <Modal
      open={open}
      title={t('sales_agents:pay.terms.new', 'New terms')}
      okText={t('sales_agents:pay.form.save', 'Save')}
      confirmLoading={mutation.isPending}
      onOk={() => form.submit()}
      onCancel={close}
      destroyOnHidden
    >
      <Space direction="vertical" style={{ width: '100%' }}>
        <PayErrorAlert error={refusal} />
        <Form form={form} layout="vertical" preserve={false} onFinish={(values) => { clear(); mutation.mutate(values); }}>
          <Form.Item name="effective_month" label={t('sales_agents:pay.form.effective_month', 'Effective month')} rules={[{ required: true, message: t('sales_agents:pay.form.required', 'Required') }]}>
            {/* The guard's own answer (A16 `editable_from_month`); nothing before it is offered. */}
            <DatePicker picker="month" format="MM.YYYY" style={{ width: '100%' }} disabledDate={(value) => value.format('YYYY-MM') < editableFromMonth} />
          </Form.Item>
          <Form.Item name="base_salary" label={t('sales_agents:pay.form.base_salary', 'Base salary')} rules={[{ required: true, message: t('sales_agents:pay.form.amount_required', 'Enter an amount') }]}>
            <MoneyInput />
          </Form.Item>
          <Form.Item name="plan_id" label={t('sales_agents:pay.form.plan', 'Plan')} rules={[{ required: true, message: t('sales_agents:pay.form.required', 'Required') }]}>
            <Select options={plans.map((plan) => ({ value: plan.id, label: plan.name }))} />
          </Form.Item>
          <Form.Item name="note" label={t('sales_agents:pay.form.note', 'Note')}>
            <Input maxLength={500} />
          </Form.Item>
        </Form>
      </Space>
    </Modal>
  );
};

/**
 * The SalesAgents drawer's Pay tab (§6.2 "Agent terms"): employment (A18), the outstanding
 * balance and its "Record repayment" door (Q15), and the effective-dated terms (A17). Every
 * figure and month comes from A16; the repayment posts into the published `nets_in`.
 */
const AgentPayTab = ({ agentUserId, active }) => {
  const { t } = useTranslation(['sales_agents', 'common']);
  const canManagePay = useAuthStore().hasPermission('can_manage_sales_pay');
  const [employmentOpen, setEmploymentOpen] = useState(false);
  const [termsOpen, setTermsOpen] = useState(false);
  const [repaymentOpen, setRepaymentOpen] = useState(false);

  const termsQuery = useQuery({
    queryKey: ['salesPay', 'agentTerms', agentUserId],
    queryFn: () => salesPayService.getAgentTerms(agentUserId),
    enabled: canManagePay && Boolean(active) && Boolean(agentUserId),
  });

  if (termsQuery.isError) {
    return <Alert type="error" showIcon message={extractApiErrorMessage(termsQuery.error, t('ui.common.error_occurred', 'An error occurred'))} />;
  }
  if (!termsQuery.data) return <Spin />;

  const { agent, employment, owed_to_date: owed, terms, term_in_force: inForce, plans, editable_from_month: editableFrom } = termsQuery.data;
  // Newest first: display order only, by the published month.
  const rows = [...terms].sort((a, b) => b.effective_month.localeCompare(a.effective_month) || b.id - a.id);

  const columns = [
    {
      title: t('sales_agents:pay.col.effective_month', 'Effective month'),
      dataIndex: 'effective_month',
      key: 'effective_month',
      render: (value, row) => (
        <Space size={4}>
          {monthLabel(value)}
          {inForce && row.id === inForce.id ? <Tag color="green">{t('sales_agents:pay.terms.in_force', 'In force')}</Tag> : null}
        </Space>
      ),
    },
    { title: t('sales_agents:pay.col.base_salary', 'Base salary'), dataIndex: 'base_salary', key: 'base_salary', render: (value) => signedMoney(value) },
    { title: t('sales_agents:pay.col.plan', 'Plan'), key: 'plan', render: (_, row) => row.plan?.name || '—' },
    { title: t('sales_agents:pay.col.note', 'Note'), dataIndex: 'note', key: 'note', render: (value) => value || '—' },
    { title: t('sales_agents:pay.col.created_by', 'Created by'), key: 'created_by', render: (_, row) => row.created_by?.name || '—' },
  ];

  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <Descriptions
        column={1}
        bordered
        size="small"
        title={t('sales_agents:pay.terms.employment', 'Employment')}
        extra={<Button onClick={() => setEmploymentOpen(true)}>{t('sales_agents:pay.terms.edit', 'Edit')}</Button>}
      >
        <Descriptions.Item label={t('sales_agents:pay.form.start', 'Employment start')}>
          {employment.start ? dateLabel(employment.start) : t('sales_agents:pay.terms.not_set', 'Not set')}
        </Descriptions.Item>
        <Descriptions.Item label={t('sales_agents:pay.form.end', 'Employment end')}>
          {employment.end ? dateLabel(employment.end) : '—'}
        </Descriptions.Item>
      </Descriptions>

      {Number(owed.amount) > 0 ? (
        <Space wrap>
          <Tag color="red">
            {t('sales_agents:pay.terms.owed_to_date', { defaultValue: 'Outstanding balance owed by the agent: {{amount}} (statement {{month}})', amount: signedMoney(owed.amount), month: monthLabel(owed.month) })}
          </Tag>
          {owed.nets_in ? (
            <Button onClick={() => setRepaymentOpen(true)}>{t('sales_agents:pay.terms.record_repayment', 'Record repayment')}</Button>
          ) : null}
        </Space>
      ) : null}

      {employment.start ? (
        <>
          <Button type="primary" onClick={() => setTermsOpen(true)}>{t('sales_agents:pay.terms.new', 'New terms')}</Button>
          <Table rowKey="id" size="small" pagination={false} columns={columns} dataSource={rows} />
        </>
      ) : (
        <Alert type="warning" showIcon message={t('sales_agents:pay.hint.employment_start_required', 'Set the employment start date before adding pay terms')} />
      )}

      <EmploymentModal open={employmentOpen} agentUserId={agentUserId} employment={employment} onClose={() => setEmploymentOpen(false)} />
      <TermsModal open={termsOpen} agentUserId={agentUserId} plans={plans} editableFromMonth={editableFrom} onClose={() => setTermsOpen(false)} />
      {owed.nets_in ? (
        <AdjustmentModal open={repaymentOpen} month={owed.nets_in} agentId={agentUserId} agentName={agent.name} mode="repayment" onClose={() => setRepaymentOpen(false)} />
      ) : null}
    </Space>
  );
};

export default AgentPayTab;
