import React, { useState } from 'react';
import { Alert, Form, Input, Modal, Space } from 'antd';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import MoneyInput from '../../common/MoneyInput';
import salesPayService from '../../../services/salesPayService';
import { PayErrorAlert, isHandledPayError } from './payErrors';

/** A20 / A21: all three names are required, because agents read the type in their own language. */
const PenaltyTypeModal = ({ type, onClose }) => {
  const { t } = useTranslation(['sales_agents', 'common']);
  const queryClient = useQueryClient();
  const [form] = Form.useForm();
  const [refusal, setRefusal] = useState(null);
  const nameRule = [{ required: true, whitespace: true, message: t('sales_agents:pay.form.required', 'Required') }];
  const mutation = useMutation({
    mutationFn: ({ names, default_amount: defaultAmount }) => (type
      ? salesPayService.updatePenaltyType(type.id, { names, default_amount: defaultAmount })
      : salesPayService.createPenaltyType({ names, default_amount: defaultAmount })),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['salesPay'] });
      onClose();
    },
    onError: (error) => setRefusal(isHandledPayError(error) ? error : null),
  });
  return (
    <Modal
      open
      title={type ? t('sales_agents:pay.types.edit_title', 'Edit penalty type') : t('sales_agents:pay.types.create_title', 'New penalty type')}
      okText={t('sales_agents:pay.form.save', 'Save')}
      confirmLoading={mutation.isPending}
      onOk={() => form.submit()}
      onCancel={onClose}
    >
      <Space direction="vertical" style={{ width: '100%' }}>
        <Alert type="info" showIcon message={t('sales_agents:pay.hint.type_names_visible', 'Names are shown to managers and agents.')} />
        <PayErrorAlert error={refusal} />
        <Form
          form={form}
          layout="vertical"
          initialValues={type ? { names: type.names, default_amount: type.default_amount } : undefined}
          onFinish={(values) => { setRefusal(null); mutation.mutate(values); }}
        >
          <Form.Item name={['names', 'en']} label={t('sales_agents:pay.form.name_en', 'Name (English)')} rules={nameRule}>
            <Input maxLength={100} />
          </Form.Item>
          <Form.Item name={['names', 'uz']} label={t('sales_agents:pay.form.name_uz', 'Name (Uzbek)')} rules={nameRule}>
            <Input maxLength={100} />
          </Form.Item>
          <Form.Item name={['names', 'ru']} label={t('sales_agents:pay.form.name_ru', 'Name (Russian)')} rules={nameRule}>
            <Input maxLength={100} />
          </Form.Item>
          <Form.Item name="default_amount" label={t('sales_agents:pay.col.default_amount', 'Default amount')} rules={[{ required: true, message: t('sales_agents:pay.form.amount_required', 'Enter an amount') }]}>
            <MoneyInput />
          </Form.Item>
        </Form>
      </Space>
    </Modal>
  );
};

export default PenaltyTypeModal;
