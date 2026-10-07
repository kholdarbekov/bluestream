import React, { useState } from 'react';
import { Checkbox, Modal, Space, Typography } from 'antd';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import salesPayService from '../../../services/salesPayService';
import { PayErrorAlert, isHandledPayError } from './payErrors';
import { monthLabel } from './payFormat';

const { Text } = Typography;

// A0 (§6.2): the first month is real pay unless the admin ticks the optional trial box (C13,
// opt-in since 2026-10-07). `month` is A1's published `startable_month`.
const StartPayModal = ({ month, onClose }) => {
  const { t } = useTranslation(['sales_agents', 'common']);
  const queryClient = useQueryClient();
  const [isShadow, setIsShadow] = useState(false);
  const [refusal, setRefusal] = useState(null);
  const mutation = useMutation({
    mutationFn: () => salesPayService.startPay({ month, isShadow }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['salesPay'] });
      onClose();
    },
    onError: (error) => setRefusal(isHandledPayError(error) ? error : null),
  });
  return (
    <Modal
      open
      title={t('sales_agents:pay.start.title', 'Start pay tracking')}
      okText={t('sales_agents:pay.start.confirm', 'Start')}
      confirmLoading={mutation.isPending}
      onOk={() => { setRefusal(null); mutation.mutate(); }}
      onCancel={onClose}
    >
      <Space direction="vertical" style={{ width: '100%' }}>
        <PayErrorAlert error={refusal} />
        <Text strong>{monthLabel(month)}</Text>
        <Checkbox checked={isShadow} onChange={(event) => setIsShadow(event.target.checked)}>
          {t('sales_agents:pay.start.shadow', 'Trial month (optional)')}
        </Checkbox>
        <Text type="secondary">{t('sales_agents:pay.start.shadow_hint', 'Tick only if this first month should be a dry run: numbers are shown for review, and pay is made the old way.')}</Text>
      </Space>
    </Modal>
  );
};

export default StartPayModal;
