import React, { useMemo, useState } from 'react';
import {
  Card,
  List,
  Tag,
  Button,
  Space,
  Modal,
  Input,
  message,
  Divider,
  Typography,
} from 'antd';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import adminService from '../services/adminService';

const { Text } = Typography;
const { TextArea } = Input;

const errorMessageOf = (error, fallback) => error?.response?.data?.message || fallback;

/**
 * Reusable "confirm this action, with a reason" modal. Parameterized so that
 * link / unlink / dismiss all reuse the same piece rather than each rolling
 * their own Modal + Input.TextArea plumbing.
 */
const ActionModal = ({
  open,
  title,
  description,
  showReasonField,
  reasonValue,
  onReasonChange,
  reasonPlaceholder,
  onOk,
  onCancel,
  confirmLoading,
  okText,
  cancelText,
}) => (
  <Modal
    title={title}
    open={open}
    onOk={onOk}
    onCancel={onCancel}
    confirmLoading={confirmLoading}
    okText={okText}
    cancelText={cancelText}
    okButtonProps={{ disabled: showReasonField && !reasonValue?.trim() }}
    destroyOnClose
  >
    <Space direction="vertical" size={10} style={{ width: '100%' }}>
      {description && <div>{description}</div>}
      {showReasonField && (
        <TextArea
          rows={3}
          value={reasonValue}
          onChange={(event) => onReasonChange(event.target.value)}
          placeholder={reasonPlaceholder}
          maxLength={500}
        />
      )}
    </Space>
  </Modal>
);

export default function LinkedAccountsPanel({ user }) {
  const { t } = useTranslation('users');
  const queryClient = useQueryClient();
  const userId = user?.id;

  // action: { kind: 'unlink' | 'link' | 'dismiss', payload }
  // "Same physical place" is deliberately NOT here: place groups are ownerless
  // and may span customers, so they live in PlaceGroupPanel (the WHERE axis).
  // This panel is the WHO axis only.
  const [action, setAction] = useState(null);
  const [reason, setReason] = useState('');

  const [manualSearch, setManualSearch] = useState('');
  const [manualResults, setManualResults] = useState([]);
  const [manualSearching, setManualSearching] = useState(false);

  const linkedAccountsQuery = useQuery({
    queryKey: ['linkedAccounts', userId],
    queryFn: () => adminService.getLinkedAccounts(userId),
    enabled: !!userId,
  });

  const suggestionsQuery = useQuery({
    queryKey: ['linkSuggestions', userId],
    queryFn: () => adminService.getLinkSuggestions(userId),
    enabled: !!userId,
  });

  const members = linkedAccountsQuery.data?.data?.members || [];
  const primaryUserId = linkedAccountsQuery.data?.data?.primary_user_id;
  const suggestions = suggestionsQuery.data?.data?.suggestions || [];

  const memberIds = useMemo(() => members.map((m) => m.id), [members]);

  const closeAction = () => {
    setAction(null);
    setReason('');
  };

  const invalidateLinkedData = () => {
    queryClient.invalidateQueries({ queryKey: ['linkedAccounts', userId] });
    queryClient.invalidateQueries({ queryKey: ['linkSuggestions', userId] });
  };

  const unlinkMutation = useMutation({
    mutationFn: ({ memberId, reason: unlinkReason }) =>
      adminService.unlinkAccount(memberId, unlinkReason),
    onSuccess: (result) => {
      const nonTerminalOrders = result?.data?.non_terminal_orders || [];
      if (nonTerminalOrders.length > 0) {
        message.warning(
          t(
            'ui.users.linked_accounts.unlink_non_terminal_warning',
            `${nonTerminalOrders.length} in-flight order(s) to check`
          )
        );
      }
      message.success(t('ui.users.linked_accounts.unlink_success', 'Account unlinked'));
      closeAction();
      invalidateLinkedData();
    },
    onError: (error) => {
      message.error(errorMessageOf(error, t('ui.users.linked_accounts.action_failed', 'Action failed')));
    },
  });

  const linkMutation = useMutation({
    mutationFn: ({ secondaryUserId, reason: linkReason }) =>
      adminService.linkAccounts(userId, secondaryUserId, linkReason),
    onSuccess: () => {
      message.success(t('ui.users.linked_accounts.link_success', 'Accounts linked'));
      closeAction();
      setManualResults([]);
      setManualSearch('');
      invalidateLinkedData();
    },
    onError: (error) => {
      message.error(errorMessageOf(error, t('ui.users.linked_accounts.action_failed', 'Action failed')));
    },
  });

  const dismissMutation = useMutation({
    mutationFn: (suggestionUserId) => adminService.dismissCustomerLink(userId, suggestionUserId),
    onSuccess: () => {
      message.success(t('ui.users.linked_accounts.dismiss_success', 'Marked as different customers'));
      closeAction();
      queryClient.invalidateQueries({ queryKey: ['linkSuggestions', userId] });
    },
    onError: (error) => {
      message.error(errorMessageOf(error, t('ui.users.linked_accounts.action_failed', 'Action failed')));
    },
  });

  if (!userId) {
    return null;
  }

  const handleManualSearch = async () => {
    const query = manualSearch.trim();
    if (!query) {
      return;
    }
    setManualSearching(true);
    try {
      const result = await adminService.getUsers({ search: query });
      const items = result?.data?.items || [];
      const excludeIds = new Set([userId, ...memberIds]);
      setManualResults(items.filter((candidate) => !excludeIds.has(candidate.id)));
    } catch (error) {
      message.error(
        errorMessageOf(error, t('ui.users.linked_accounts.search_failed', 'Search failed'))
      );
    } finally {
      setManualSearching(false);
    }
  };

  const handleConfirm = () => {
    if (!action) {
      return;
    }
    if (action.kind === 'unlink') {
      unlinkMutation.mutate({ memberId: action.member.id, reason: reason.trim() });
    } else if (action.kind === 'link') {
      linkMutation.mutate({ secondaryUserId: action.candidate.id, reason: reason.trim() });
    } else if (action.kind === 'dismiss') {
      dismissMutation.mutate(action.suggestionUserId);
    }
  };

  const modalIsPending =
    (action?.kind === 'unlink' && unlinkMutation.isPending) ||
    (action?.kind === 'link' && linkMutation.isPending) ||
    (action?.kind === 'dismiss' && dismissMutation.isPending);

  return (
    <Card
      title={t('ui.users.linked_accounts', 'Linked accounts')}
      size="small"
      style={{ marginBottom: 12 }}
    >
      {/* Section 1: members */}
      <div>
        {members.length <= 1 ? (
          <span>{t('ui.users.no_linked_accounts', 'Not linked to any other account')}</span>
        ) : (
          <List
            size="small"
            dataSource={members}
            renderItem={(member) => (
              <List.Item
                key={member.id}
                actions={[
                  <Button
                    key="unlink"
                    size="small"
                    danger
                    loading={
                      unlinkMutation.isPending &&
                      action?.kind === 'unlink' &&
                      action.member.id === member.id
                    }
                    onClick={() => {
                      setAction({ kind: 'unlink', member });
                      setReason('');
                    }}
                  >
                    {t('ui.users.linked_accounts.unlink', 'Unlink')}
                  </Button>,
                ]}
              >
                <span>
                  {member.first_name} {member.last_name} — {member.phone}
                </span>
                {member.id === primaryUserId && (
                  <Tag color="blue" style={{ marginLeft: 8 }}>
                    {t('ui.users.primary', 'primary')}
                  </Tag>
                )}
              </List.Item>
            )}
          />
        )}
      </div>

      <Divider style={{ margin: '12px 0' }} />

      {/* Section 2: geo suggestions */}
      <div>
        <Text strong>
          {t('ui.users.linked_accounts.suggestions_title', 'Possible same customer')}
        </Text>
        {suggestions.length === 0 ? (
          <div style={{ marginTop: 8 }}>
            <Text type="secondary">
              {t('ui.users.linked_accounts.no_suggestions', 'No suggestions')}
            </Text>
          </div>
        ) : (
          <List
            size="small"
            dataSource={suggestions}
            renderItem={(suggestion) => (
              <List.Item
                key={suggestion.user_id}
                actions={[
                  <Button
                    key="link"
                    size="small"
                    type="primary"
                    onClick={() => {
                      setAction({ kind: 'link', candidate: { id: suggestion.user_id, ...suggestion } });
                      setReason('');
                    }}
                  >
                    {t('ui.users.linked_accounts.link', 'Link')}
                  </Button>,
                  <Button
                    key="dismiss"
                    size="small"
                    loading={
                      dismissMutation.isPending &&
                      action?.kind === 'dismiss' &&
                      action.suggestionUserId === suggestion.user_id
                    }
                    onClick={() => setAction({ kind: 'dismiss', suggestionUserId: suggestion.user_id, name: `${suggestion.first_name} ${suggestion.last_name}` })}
                  >
                    {t('ui.users.linked_accounts.not_same_person', 'Not the same person')}
                  </Button>,
                ]}
              >
                <div>
                  <div>
                    {suggestion.first_name} {suggestion.last_name} — {suggestion.phone}
                  </div>
                  <div>
                    <Text type="secondary">
                      {t(
                        'ui.users.linked_accounts.suggestion_evidence',
                        `${suggestion.min_distance_km} km away · ${suggestion.shared_geo_customer_count} shared address(es) · score ${suggestion.score}`
                      )}
                    </Text>
                  </div>
                </div>
              </List.Item>
            )}
          />
        )}
      </div>

      <Divider style={{ margin: '12px 0' }} />

      {/* Section 3: manual link */}
      <div>
        <Text strong>
          {t('ui.users.linked_accounts.manual_title', 'Link another account')}
        </Text>
        <Space style={{ marginTop: 8, marginBottom: 8 }}>
          <Input
            value={manualSearch}
            onChange={(event) => setManualSearch(event.target.value)}
            placeholder={t('ui.users.linked_accounts.search_placeholder', 'Phone or user ID')}
            onPressEnter={handleManualSearch}
          />
          <Button onClick={handleManualSearch} loading={manualSearching}>
            {t('ui.users.linked_accounts.find', 'Find')}
          </Button>
        </Space>
        {manualResults.length > 0 && (
          <List
            size="small"
            dataSource={manualResults}
            renderItem={(candidate) => (
              <List.Item
                key={candidate.id}
                actions={[
                  <Button
                    key="link"
                    size="small"
                    type="primary"
                    onClick={() => {
                      setAction({ kind: 'link', candidate });
                      setReason('');
                    }}
                  >
                    {t('ui.users.linked_accounts.link', 'Link')}
                  </Button>,
                ]}
              >
                {candidate.first_name} {candidate.last_name} — {candidate.phone}
              </List.Item>
            )}
          />
        )}
      </div>

      <ActionModal
        open={!!action}
        title={
          action?.kind === 'unlink'
            ? t(
                'ui.users.linked_accounts.unlink_title',
                `Unlink ${action ? `${action.member.first_name} ${action.member.last_name}` : ''} from this customer?`
              )
            : action?.kind === 'link'
              ? t('ui.users.linked_accounts.link_title', 'Link this account?')
              : t('ui.users.linked_accounts.dismiss_title', `Mark ${action?.name || ''} as a different person?`)
        }
        showReasonField={action?.kind !== 'dismiss'}
        reasonValue={reason}
        onReasonChange={setReason}
        reasonPlaceholder={t('ui.users.linked_accounts.reason_placeholder', 'Enter reason')}
        onOk={handleConfirm}
        onCancel={closeAction}
        confirmLoading={modalIsPending}
        okText={t('ui.common.confirm', 'Confirm')}
        cancelText={t('ui.common.cancel', 'Cancel')}
      />
    </Card>
  );
}
