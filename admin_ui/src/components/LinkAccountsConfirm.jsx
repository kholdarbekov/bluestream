import React, { useState } from 'react';
import { Modal, Input, Space, Typography, message } from 'antd';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import adminService from '../services/adminService';
import { SHARED_PLACE_CASES_KEY } from '../utils/constants';

const { TextArea } = Input;
const { Text } = Typography;

const errorMessageOf = (error, fallback) => error?.response?.data?.message || fallback;

/**
 * The "link this account, with a reason" confirmation, shared by the linked
 * accounts panel and the shared-place case screen so there is ONE link flow
 * with ONE reason rule. A link can settle a shared-place case, so success also
 * refreshes the case queue.
 *
 * `onLinked(result, reason)` fires after the link is made and the queries are
 * invalidated; the host decides what to close or clear.
 */
export default function LinkAccountsConfirm({
  userId,
  candidate,
  open,
  onClose,
  onLinked,
  defaultReason = '',
}) {
  const { t } = useTranslation('users');
  const queryClient = useQueryClient();

  // Re-seeded on every open (adjusting state while rendering, as the place
  // group dialog does) so a second candidate never inherits the first reason.
  const [reason, setReason] = useState(defaultReason);
  const [openedFor, setOpenedFor] = useState(null);
  const openKey = open ? `${candidate?.id}` : null;
  if (openKey !== openedFor) {
    setOpenedFor(openKey);
    if (open) {
      setReason(defaultReason);
    }
  }

  const linkMutation = useMutation({
    mutationFn: ({ secondaryUserId, linkReason }) =>
      adminService.linkAccounts(userId, secondaryUserId, linkReason),
    onSuccess: (result, { linkReason }) => {
      message.success(t('ui.users.linked_accounts.link_success', 'Accounts linked'));
      queryClient.invalidateQueries({ queryKey: ['linkedAccounts', userId] });
      queryClient.invalidateQueries({ queryKey: ['linkSuggestions', userId] });
      queryClient.invalidateQueries({ queryKey: SHARED_PLACE_CASES_KEY });
      onLinked?.(result, linkReason);
    },
    onError: (error) => {
      message.error(errorMessageOf(error, t('ui.users.linked_accounts.action_failed', 'Action failed')));
    },
  });

  const trimmed = reason.trim();

  return (
    <Modal
      title={t('ui.users.linked_accounts.link_title', 'Link this account?')}
      open={open}
      onOk={() => {
        if (trimmed && candidate) {
          linkMutation.mutate({ secondaryUserId: candidate.id, linkReason: trimmed });
        }
      }}
      onCancel={onClose}
      confirmLoading={linkMutation.isPending}
      okText={t('ui.common.confirm', 'Confirm')}
      cancelText={t('ui.common.cancel', 'Cancel')}
      okButtonProps={{ disabled: !trimmed }}
      destroyOnClose
    >
      <Space direction="vertical" size={10} style={{ width: '100%' }}>
        {candidate && (
          <Text>
            {candidate.first_name} {candidate.last_name} {candidate.phone ? `— ${candidate.phone}` : ''}
          </Text>
        )}
        <TextArea
          rows={3}
          value={reason}
          onChange={(event) => setReason(event.target.value)}
          placeholder={t('ui.users.linked_accounts.reason_placeholder', 'Enter reason')}
          maxLength={500}
        />
      </Space>
    </Modal>
  );
}
