import React, { useState } from 'react';
import { DatePicker, Modal, Space, Typography } from 'antd';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import dayjs from 'dayjs';
import salesPayService from '../../../services/salesPayService';
import { PayErrorAlert, isHandledPayError } from './payErrors';
import { monthLabel } from './payFormat';

const { Text } = Typography;

// A6: the pay date defaults to today; a future date is the backend's refusal (DATE_INVALID).
const MarkPaidModal = ({ month, onClose }) => {
  const { t } = useTranslation(['sales_agents', 'common']);
  const queryClient = useQueryClient();
  const [paidOn, setPaidOn] = useState(dayjs());
  const [refusal, setRefusal] = useState(null);
  const mutation = useMutation({
    mutationFn: () => salesPayService.markPeriodPaid(month, paidOn.format('YYYY-MM-DD')),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['salesPay'] });
      onClose();
    },
    onError: (error) => setRefusal(isHandledPayError(error) ? error : null),
  });
  return (
    <Modal
      open
      title={t('sales_agents:pay.confirm.mark_paid.title', { defaultValue: 'Mark {{month}} as paid', month: monthLabel(month) })}
      okText={t('sales_agents:pay.action.mark_paid', 'Mark paid')}
      okButtonProps={{ disabled: !paidOn }}
      confirmLoading={mutation.isPending}
      onOk={() => { setRefusal(null); mutation.mutate(); }}
      onCancel={onClose}
    >
      <Space direction="vertical" style={{ width: '100%' }}>
        <PayErrorAlert error={refusal} />
        <Text>{t('sales_agents:pay.confirm.mark_paid.body', 'Pay date')}</Text>
        <DatePicker value={paidOn} onChange={setPaidOn} format="DD.MM.YYYY" allowClear={false} />
      </Space>
    </Modal>
  );
};

export default MarkPaidModal;
