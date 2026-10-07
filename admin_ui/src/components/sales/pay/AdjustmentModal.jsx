import React, { useState } from 'react';
import { Alert, Form, Input, Modal, Space } from 'antd';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import MoneyInput from '../../common/MoneyInput';
import salesPayService from '../../../services/salesPayService';
import { PayErrorAlert, isHandledPayError } from './payErrors';
import { monthLabel } from './payFormat';

/**
 * A11 in both of its doors (§6.2).
 * - `signed`: "Add adjustment" in the drawer; a signed `MoneyInput`, so a deduction keeps its minus.
 * - `repayment`: "Record repayment" on the Pay tab (Q15); no `allowNegative`, so a minus is refused
 *   before submit, and `month` is the published `nets_in` the caller passes, never computed here.
 */
const AdjustmentModal = ({ open, month, agentId, agentName, mode, onClose }) => {
  const { t } = useTranslation(['sales_agents', 'common']);
  const queryClient = useQueryClient();
  const [form] = Form.useForm();
  const [refusal, setRefusal] = useState(null);
  const repayment = mode === 'repayment';
  // `preserve={false}` + `destroyOnHidden`: every opening starts from an empty form.
  const close = () => { setRefusal(null); onClose(); };

  const mutation = useMutation({
    mutationFn: ({ amount, reason }) => salesPayService.createAdjustment(month, agentId, { amount, reason }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['salesPay'] });
      close();
    },
    onError: (error) => setRefusal(isHandledPayError(error) ? error : null),
  });

  return (
    <Modal
      open={open}
      title={repayment
        ? t('sales_agents:pay.terms.record_repayment', 'Record repayment')
        : t('sales_agents:pay.adjustments.add', 'Add adjustment')}
      okText={t('sales_agents:pay.form.save', 'Save')}
      confirmLoading={mutation.isPending}
      onOk={() => form.submit()}
      onCancel={close}
      destroyOnHidden
    >
      <Space direction="vertical" style={{ width: '100%' }}>
        <Alert
          type="info"
          showIcon
          message={repayment
            ? t('sales_agents:pay.hint.repayment', { defaultValue: 'Money the agent repaid outside the system. It is added as an adjustment in {{month}}, which deducts it from the balance.', month: monthLabel(month) })
            : t('sales_agents:pay.hint.adjustment_sign', 'Use − for a deduction. To undo an adjustment, add the opposite amount with a reason.')}
        />
        {/* A closed-month re-freeze can also answer TERMS_MISSING (`details.agents`); name the
            agent from what the caller already has (§6.2), never the raw id. */}
        <PayErrorAlert error={refusal} agentNames={new Map(agentName != null ? [[agentId, agentName]] : [])} />
        <Form form={form} layout="vertical" preserve={false} onFinish={(values) => { setRefusal(null); mutation.mutate(values); }}>
          <Form.Item
            name="amount"
            label={t('sales_agents:pay.form.amount', 'Amount')}
            rules={[{ required: true, message: t('sales_agents:pay.form.amount_required', 'Enter an amount') }]}
          >
            <MoneyInput allowNegative={!repayment} />
          </Form.Item>
          <Form.Item
            name="reason"
            label={t('sales_agents:pay.form.reason', 'Reason')}
            rules={[{ required: true, whitespace: true, message: t('sales_agents:pay.form.reason_required', 'Enter a reason') }]}
          >
            <Input.TextArea rows={3} maxLength={300} />
          </Form.Item>
        </Form>
      </Space>
    </Modal>
  );
};

export default AdjustmentModal;
