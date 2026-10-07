import React, { useState } from 'react';
import { Alert, Form, Input, Modal, Space } from 'antd';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import salesPayService from '../../../services/salesPayService';
import { PayErrorAlert, isHandledPayError } from './payErrors';

/**
 * A25 / A26: reject a proposal or cancel a confirmed penalty, with a note. The note is an
 * admin's free text and may quote a figure, so it is never sent to a manager (§5.3).
 */
const PenaltyNoteModal = ({ open, kind, penalty, onClose }) => {
  const { t } = useTranslation(['sales_agents', 'common']);
  const queryClient = useQueryClient();
  const [form] = Form.useForm();
  const [refusal, setRefusal] = useState(null);
  const close = () => { setRefusal(null); onClose(); };

  const mutation = useMutation({
    mutationFn: ({ note }) => (kind === 'reject'
      ? salesPayService.rejectPenalty(penalty.id, note)
      : salesPayService.cancelPenalty(penalty.id, note)),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['salesPay'] });
      close();
    },
    onError: (error) => setRefusal(isHandledPayError(error) ? error : null),
  });

  return (
    <Modal
      open={open}
      title={kind === 'reject'
        ? t('sales_agents:pay.penalties.reject_title', 'Reject penalty')
        : t('sales_agents:pay.penalties.cancel_title', 'Cancel penalty')}
      okText={t('sales_agents:pay.form.save', 'Save')}
      confirmLoading={mutation.isPending}
      onOk={() => form.submit()}
      onCancel={close}
      destroyOnHidden
    >
      <Space direction="vertical" style={{ width: '100%' }}>
        <Alert type="info" showIcon message={t('sales_agents:pay.hint.admin_only_note', 'Visible to admins only.')} />
        <PayErrorAlert error={refusal} />
        <Form form={form} layout="vertical" preserve={false} onFinish={(values) => { setRefusal(null); mutation.mutate(values); }}>
          <Form.Item
            name="note"
            label={t('sales_agents:pay.form.note', 'Note')}
            rules={[{ required: true, whitespace: true, message: t('sales_agents:pay.form.required', 'Required') }]}
          >
            <Input.TextArea rows={3} maxLength={500} />
          </Form.Item>
        </Form>
      </Space>
    </Modal>
  );
};

export default PenaltyNoteModal;
