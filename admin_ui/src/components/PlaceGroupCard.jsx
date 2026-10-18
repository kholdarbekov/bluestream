import React, { useState } from 'react';
import { List, Tag, Button, Space, Statistic, message, Typography, Spin, Alert } from 'antd';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import dayjs from 'dayjs';
import adminService from '../services/adminService';
import PlaceGroupConfirmModal from './PlaceGroupConfirmModal';
import { placeGroupErrorMessage, placeGroupEventText } from './placeGroupCopy';
import { invalidatePlaceGroupQueries } from './placeGroupQueries';

const { Text } = Typography;

/** Group-scoped audit reasons are stored as "[group 7] ..." — that prefix is an internal scope key. */
const displayReason = (reason) => (reason || '').replace(/^\[group \d+\]\s*/, '');

/**
 * One place group: its pool, COD exposure, members and history, with the add
 * and remove actions. Rendered by the per-customer panel today and shared so
 * every surface edits a group through the same card and the same confirm flow.
 *
 * `onChanged` fires after every successful mutation (once the shared queries
 * are invalidated). `onDissolved` fires instead of the normal success toast
 * when a removal left the group with one address and the backend dissolved it,
 * so a host showing the group (e.g. a drawer) can close it.
 */
export default function PlaceGroupCard({ groupId, onChanged, onDissolved }) {
  const { t } = useTranslation('users');
  const queryClient = useQueryClient();

  // action: { kind: 'add' | 'remove' | 'rename', payload }
  const [action, setAction] = useState(null);

  const groupQuery = useQuery({
    queryKey: ['placeGroup', groupId],
    queryFn: () => adminService.getPlaceGroup(groupId),
    enabled: groupId != null,
  });
  const group = groupQuery.data?.data;

  const closeAction = () => setAction(null);

  const onMutationSuccess = (successKey, successFallback) => () => {
    message.success(t(successKey, successFallback));
    closeAction();
    invalidatePlaceGroupQueries(queryClient);
    onChanged?.();
  };

  const onMutationError = (error) => {
    message.error(
      placeGroupErrorMessage(error, t, t('ui.users.place_groups.action_failed', 'Action failed'))
    );
  };

  const addMutation = useMutation({
    mutationFn: ({ addressIds, actionReason, merge }) =>
      adminService.addPlaceGroupAddresses(groupId, addressIds, actionReason, merge),
    onSuccess: onMutationSuccess('ui.users.place_groups.add_success', 'Address added to place group'),
    onError: onMutationError,
  });

  const afterRemove = onMutationSuccess(
    'ui.users.place_groups.remove_success',
    'Address removed from place group'
  );
  const afterDissolvingRemove = onMutationSuccess(
    'ui.users.place_groups.remove_dissolved_success',
    'Address removed; the place group was dissolved'
  );

  const removeMutation = useMutation({
    mutationFn: ({ addressId, actionReason, splitBottles }) =>
      adminService.removePlaceGroupAddress(groupId, addressId, actionReason, splitBottles),
    onSuccess: (response) => {
      if (response?.data?.dissolved) {
        afterDissolvingRemove();
        onDissolved?.();
      } else {
        afterRemove();
      }
    },
    onError: onMutationError,
  });

  const renameMutation = useMutation({
    mutationFn: ({ label, actionReason }) =>
      adminService.renamePlaceGroup(groupId, label, actionReason),
    onSuccess: onMutationSuccess('ui.users.place_groups.rename_success', 'Place group renamed'),
    onError: onMutationError,
  });

  const startRename = () =>
    setAction({ kind: 'rename', payload: { groupId, currentLabel: group?.label || '' } });

  const startAdd = () => setAction({ kind: 'add', payload: { groupId } });

  /**
   * The member object is what carries `suggested_bottles_leaving` — the
   * backend's pre-fill for the split, derived from THIS address's own attributed
   * entries at the place and already capped at what the place holds. Without it
   * every removal defaulted to 0 and the bottles silently stayed behind.
   */
  const startRemove = (member) => {
    setAction({
      kind: 'remove',
      payload: {
        groupId,
        addressId: member?.address_id,
        placeBalance: group?.place_balance,
        suggestedBottlesLeaving: member?.suggested_bottles_leaving,
        removalDissolvesGroup: Boolean(group?.removal_dissolves_group),
      },
    });
  };

  const handleConfirm = ({ addressIds, label, reason, bottlesLeaving, merge }) => {
    if (action.kind === 'rename') {
      renameMutation.mutate({ label, actionReason: reason });
    } else if (action.kind === 'add') {
      addMutation.mutate({ addressIds, actionReason: reason, merge });
    } else if (action.kind === 'remove') {
      removeMutation.mutate({
        addressId: action.payload.addressId,
        actionReason: reason,
        splitBottles: bottlesLeaving,
      });
    }
  };

  const modalIsPending =
    (action?.kind === 'add' && addMutation.isPending) ||
    (action?.kind === 'remove' && removeMutation.isPending) ||
    (action?.kind === 'rename' && renameMutation.isPending);

  if (groupQuery.isError) {
    return (
      <Alert
        type="error"
        showIcon
        message={t('ui.users.place_groups.load_failed', 'Could not load this place group.')}
      />
    );
  }

  if (!group) {
    // Loading shows a spinner; a disabled query (no group id) stays empty.
    return groupQuery.isLoading ? <Spin /> : null;
  }

  return (
    <div style={{ marginBottom: 12 }}>
      <Space wrap align="start">
        <Tag color="geekblue">
          {group.label || `${t('ui.users.place_groups.unnamed', 'Place')} #${group.place_group_id}`}
        </Tag>
        <Statistic
          title={t('ui.users.place_groups.union_balance', 'Bottles at this place')}
          value={group.place_balance ?? 0}
          valueStyle={{ fontSize: 16 }}
        />
        <Statistic
          title={t('ui.users.place_groups.place_cod_total', 'Place COD debt')}
          value={group.cod?.total_outstanding_amount ?? 0}
          valueStyle={{ fontSize: 16 }}
        />
        <Statistic
          title={t('ui.users.place_groups.place_cod_count', 'Unpaid COD orders')}
          value={group.cod?.active_cod_debt_count ?? 0}
          valueStyle={{ fontSize: 16 }}
        />
        <Button size="small" onClick={startRename}>
          {t('ui.users.place_groups.rename_action', 'Rename')}
        </Button>
        <Button size="small" onClick={startAdd}>
          {t('ui.users.place_groups.add_action', 'Add address')}
        </Button>
      </Space>

      <List
        size="small"
        dataSource={group.members || []}
        locale={{
          emptyText: t('ui.users.place_groups.no_members', 'No addresses in this place group'),
        }}
        renderItem={(member) => (
          <List.Item
            key={member.address_id}
            actions={[
              <Button key="remove" size="small" danger onClick={() => startRemove(member)}>
                {t('ui.users.place_groups.remove', 'Remove')}
              </Button>,
            ]}
          >
            <Space direction="vertical" size={0}>
              <Text>
                {[member.owner?.first_name, member.owner?.last_name].filter(Boolean).join(' ')} ·{' '}
                {member.owner?.phone}
              </Text>
              {/* No per-member bottle count: the place holds ONE pool
                  (the statistic above) and it cannot be sliced per
                  coworker — `members` carries no balance at all. */}
              <Text type="secondary">{member.address_title || member.full_address}</Text>
            </Space>
          </List.Item>
        )}
      />

      <List
        size="small"
        header={
          <Text type="secondary">
            {t('ui.users.place_groups.audit_title', 'Place group history')}
          </Text>
        }
        dataSource={group.events || []}
        locale={{
          emptyText: t('ui.users.place_groups.no_events', 'No changes recorded yet'),
        }}
        renderItem={(event) => (
          <List.Item key={event.id}>
            <Text type="secondary" style={{ fontSize: 12 }}>
              {event.created_at ? dayjs(event.created_at).format('YYYY-MM-DD HH:mm') : ''} ·{' '}
              {placeGroupEventText(event.event_type, t)} · {displayReason(event.reason)}
            </Text>
          </List.Item>
        )}
      />

      <PlaceGroupConfirmModal
        action={action}
        pending={modalIsPending}
        onConfirm={handleConfirm}
        onCancel={closeAction}
      />
    </div>
  );
}
