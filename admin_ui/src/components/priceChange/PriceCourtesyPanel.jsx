import React, { useState } from 'react';
import { Card, Input, Modal, Space, Typography, message } from 'antd';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';

import adminService from '../../services/adminService';
import { extractApiErrorMessages } from '../../utils/apiError';
import { PRICE_CHANGES_KEY } from '../../utils/constants';
import { ActionButton } from './PriceChangeFormModal';
import PriceCourtesyStatus from './PriceCourtesyStatus';

const { Text } = Typography;
const { TextArea } = Input;

// The customer's old-price order after a price change: what they keep, and the manager's one-time exception. The
// button follows the published `allowed_actions.grant`; the panel never re-derives a role or a window.
const PriceCourtesyPanel = ({ user }) => {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const userId = user?.id;
  const queryKey = ['priceCourtesy', userId];
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState('');

  const { data } = useQuery({
    queryKey,
    queryFn: () => adminService.getCustomerPriceCourtesy(userId),
    enabled: userId !== undefined && userId !== null,
  });

  const grantMutation = useMutation({
    mutationFn: (text) => adminService.grantCustomerPriceCourtesy(userId, text),
    onSuccess: (response) => {
      queryClient.setQueryData(queryKey, response);
      queryClient.invalidateQueries({ queryKey: PRICE_CHANGES_KEY });
      message.success(t('ui.price_courtesy.granted', 'Old-price order given'));
      setOpen(false);
      setReason('');
    },
    onError: (error) => {
      message.error(extractApiErrorMessages(
        error, t('ui.price_courtesy.grant_failed', 'Could not give the old-price order'),
      )[0]);
      // The refusal usually means the status moved (given already, window closed): read it again.
      queryClient.invalidateQueries({ queryKey });
    },
  });

  const status = data?.courtesy_status;
  const grant = data?.allowed_actions?.grant;
  if (!status) return null;
  const grantHidden = !grant || (!grant.allowed && !grant.block_reason);
  if (status.state === 'none' && grantHidden) return null;

  return (
    <Card size="small" title={t('ui.price_courtesy.title', 'Old-price order')} style={{ marginBottom: 16 }}>
      <Space direction="vertical" size={8} style={{ width: '100%' }}>
        {status.state === 'none'
          ? <Text type="secondary">{t('ui.price_courtesy.none', 'No old-price order for this customer')}</Text>
          : <PriceCourtesyStatus status={status} />}
        <ActionButton action={grant} onClick={() => setOpen(true)}>
          {t('ui.price_courtesy.grant', 'Give the old-price order')}
        </ActionButton>
      </Space>
      <Modal
        title={t('ui.price_courtesy.grant', 'Give the old-price order')}
        open={open}
        onOk={() => grantMutation.mutate(reason.trim())}
        onCancel={() => {
          setOpen(false);
          setReason('');
        }}
        confirmLoading={grantMutation.isPending}
        okButtonProps={{ disabled: !reason.trim() }}
        destroyOnClose
      >
        <Space direction="vertical" size={10} style={{ width: '100%' }}>
          <div>
            {t('ui.price_courtesy.grant_body', 'One order at the old price, with the same limits and until the same day as everyone else. It works in every channel. Each customer can be given it once.')}
          </div>
          <Text strong>{t('ui.price_courtesy.reason', 'Reason')}</Text>
          <TextArea
            rows={3}
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            placeholder={t('ui.price_courtesy.reason_placeholder', 'Why this customer gets it')}
            maxLength={500}
          />
        </Space>
      </Modal>
    </Card>
  );
};

export default PriceCourtesyPanel;
