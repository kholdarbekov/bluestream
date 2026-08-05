import React, { useMemo, useState } from 'react';
import {
  Card,
  List,
  Tag,
  Button,
  Space,
  Statistic,
  message,
  Divider,
  Typography,
} from 'antd';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import dayjs from 'dayjs';
import adminService from '../services/adminService';
import PlaceGroupConfirmModal from './PlaceGroupConfirmModal';
import { placeGroupErrorMessage, placeGroupEventText } from './placeGroupCopy';

const { Text } = Typography;

/** Group-scoped audit reasons are stored as "[group 7] ..." — that prefix is an internal scope key. */
const displayReason = (reason) => (reason || '').replace(/^\[group \d+\]\s*/, '');

/**
 * Place-group management (Phase 2c, spec 9): the WHERE axis. Place groups are
 * ownerless and may span customers, so this panel is deliberately independent
 * from identity linking (LinkedAccountsPanel keeps the WHO axis only).
 *
 * All four lifecycle operations live here — create (from a suggestion OR from
 * a cross-user address search), add a member, remove a member, dismiss a
 * suggestion — because the co-location engine only surfaces addresses within
 * the configured proximity radius; everything else (a coworker parked 60 m
 * away, a new hire joining an existing office) is reachable only via the
 * picker.
 *
 * The confirm dialog itself — the picker, the label, the split, the merge
 * review and the mandatory `reason` — lives in `PlaceGroupConfirmModal`, shared
 * with the estate-wide "Grouped Addresses" tab so there is ONE confirm flow
 * with ONE reason rule rather than two copies drifting apart.
 */
export default function PlaceGroupPanel({ user }) {
  const { t } = useTranslation('users');
  const queryClient = useQueryClient();
  const userId = user?.id;

  // action: { kind: 'create' | 'add' | 'remove' | 'dismiss', payload }
  const [action, setAction] = useState(null);

  const addressesQuery = useQuery({
    queryKey: ['placeGroupAddresses', userId],
    queryFn: () => adminService.getUserAddresses(userId),
    enabled: !!userId,
  });
  const addresses = useMemo(
    () => addressesQuery.data?.data?.addresses || [],
    [addressesQuery.data]
  );
  const groupIds = useMemo(
    () => [...new Set(addresses.map((a) => a.address_group_id).filter((id) => id != null))],
    [addresses]
  );

  const groupsQuery = useQuery({
    queryKey: ['placeGroups', userId, groupIds.join(',')],
    queryFn: async () => {
      const details = await Promise.all(groupIds.map((id) => adminService.getPlaceGroup(id)));
      return details.map((detail) => detail?.data).filter(Boolean);
    },
    enabled: groupIds.length > 0,
  });
  const groups = groupsQuery.data || [];

  /**
   * Co-location suggestions are OPT-IN, not fire-on-open.
   *
   * `get_place_group_suggestions` clusters the FULL ungrouped estate on every
   * call, uncached (`customer_link_service.py`), and it must: connected
   * components are transitively unbounded, so narrowing the pool by a bounding
   * box around the anchor truncates a chain and makes this path disagree with
   * `dismiss_place_suggestion` about a point's membership — which silently
   * voids the admin's dismissal (plan E19, pinned by
   * `tests/unit/test_place_group_suggestions.py`). The cost is therefore
   * structural and cannot be optimised away from here; it can only be stopped
   * from being billed unasked. The user drawer mounts this panel for EVERY
   * customer an admin opens, so the auto-firing query charged one full-estate
   * clustering pass per drawer open — overwhelmingly for admins who opened the
   * drawer to read a phone number and never looked at this section.
   *
   * Anchored to `userId` rather than a plain boolean because the drawer REUSES
   * this component instance when the admin switches customer: a boolean would
   * carry the previous customer's consent over and re-fire without a click.
   */
  const [suggestionsRequestedFor, setSuggestionsRequestedFor] = useState(null);
  const suggestionsRequested = !!userId && suggestionsRequestedFor === userId;

  const suggestionsQuery = useQuery({
    queryKey: ['placeGroupSuggestions', userId],
    queryFn: () => adminService.getPlaceGroupSuggestions(userId),
    enabled: suggestionsRequested,
  });
  // Gated on the request too, not just on `enabled`: react-query keeps serving
  // a cached result for this key, so a customer revisited after a switch would
  // otherwise render stale suggestions nobody asked for on this visit.
  const suggestions = suggestionsRequested
    ? suggestionsQuery.data?.data?.suggestions || []
    : [];

  const invalidatePlaceData = () => {
    queryClient.invalidateQueries({ queryKey: ['placeGroupAddresses', userId] });
    queryClient.invalidateQueries({ queryKey: ['placeGroups', userId] });
    queryClient.invalidateQueries({ queryKey: ['placeGroupSuggestions', userId] });
  };

  const closeAction = () => setAction(null);

  const onMutationSuccess = (successKey, successFallback) => () => {
    message.success(t(successKey, successFallback));
    closeAction();
    invalidatePlaceData();
  };

  const onMutationError = (error) => {
    message.error(
      placeGroupErrorMessage(error, t, t('ui.users.place_groups.action_failed', 'Action failed'))
    );
  };

  const createMutation = useMutation({
    mutationFn: ({ addressIds, groupLabel, actionReason, merge }) =>
      adminService.createPlaceGroup(addressIds, groupLabel, actionReason, merge),
    onSuccess: onMutationSuccess('ui.users.place_groups.create_success', 'Place group created'),
    onError: onMutationError,
  });

  const addMutation = useMutation({
    mutationFn: ({ groupId, addressIds, actionReason, merge }) =>
      adminService.addPlaceGroupAddresses(groupId, addressIds, actionReason, merge),
    onSuccess: onMutationSuccess('ui.users.place_groups.add_success', 'Address added to place group'),
    onError: onMutationError,
  });

  const removeMutation = useMutation({
    mutationFn: ({ groupId, addressId, actionReason, splitBottles }) =>
      adminService.removePlaceGroupAddress(groupId, addressId, actionReason, splitBottles),
    onSuccess: onMutationSuccess(
      'ui.users.place_groups.remove_success',
      'Address removed from place group'
    ),
    onError: onMutationError,
  });

  const dismissMutation = useMutation({
    mutationFn: ({ addressIdA, addressIdB, actionReason }) =>
      adminService.dismissPlaceGroupSuggestion(addressIdA, addressIdB, actionReason),
    onSuccess: onMutationSuccess('ui.users.place_groups.dismiss_success', 'Suggestion dismissed'),
    onError: onMutationError,
  });

  if (!userId) {
    return null;
  }

  const startCreateFromSuggestion = (suggestion) => {
    // The dialog pre-fills its picker from these, so the admin sees exactly
    // what will be grouped and can still widen the set before confirming.
    setAction({
      kind: 'create',
      payload: { addressIds: suggestion.address_ids, members: suggestion.members },
    });
  };

  const startManualCreate = () => setAction({ kind: 'create', payload: {} });

  const startAdd = (groupId) => setAction({ kind: 'add', payload: { groupId } });

  /**
   * The member object is what carries `suggested_bottles_leaving` — the
   * backend's pre-fill for the split, derived from THIS address's own attributed
   * entries at the place and already capped at what the place holds. Without it
   * every removal defaulted to 0 and the bottles silently stayed behind.
   */
  const startRemove = (group, member) => {
    setAction({
      kind: 'remove',
      payload: {
        groupId: group?.place_group_id,
        addressId: member?.address_id,
        placeBalance: group?.place_balance,
        suggestedBottlesLeaving: member?.suggested_bottles_leaving,
      },
    });
  };

  const startDismiss = (suggestion) => {
    const [first, second] = suggestion.members;
    setAction({
      kind: 'dismiss',
      payload: {
        addressIdA: first?.address_id,
        addressIdB: (second || first)?.address_id,
      },
    });
  };

  const handleConfirm = ({ addressIds, label, reason, bottlesLeaving, merge }) => {
    if (action.kind === 'create') {
      createMutation.mutate({
        addressIds,
        groupLabel: label,
        actionReason: reason,
        merge,
      });
    } else if (action.kind === 'add') {
      addMutation.mutate({
        groupId: action.payload.groupId,
        addressIds,
        actionReason: reason,
        merge,
      });
    } else if (action.kind === 'remove') {
      removeMutation.mutate({
        groupId: action.payload.groupId,
        addressId: action.payload.addressId,
        actionReason: reason,
        splitBottles: bottlesLeaving,
      });
    } else if (action.kind === 'dismiss') {
      dismissMutation.mutate({
        addressIdA: action.payload.addressIdA,
        addressIdB: action.payload.addressIdB,
        actionReason: reason,
      });
    }
  };

  const modalIsPending =
    (action?.kind === 'create' && createMutation.isPending) ||
    (action?.kind === 'add' && addMutation.isPending) ||
    (action?.kind === 'remove' && removeMutation.isPending) ||
    (action?.kind === 'dismiss' && dismissMutation.isPending);

  return (
    <Card
      title={t('ui.users.place_groups.title', 'Place groups (same physical place)')}
      size="small"
      style={{ marginBottom: 12 }}
    >
      {/* Section 1: the groups this customer's addresses belong to */}
      {groups.length === 0 ? (
        <Text type="secondary">
          {t('ui.users.place_groups.no_groups', 'No place groups for this customer yet')}
        </Text>
      ) : (
        groups.map((group) => (
          <div key={group.place_group_id} style={{ marginBottom: 12 }}>
            <Space wrap align="start">
              <Tag color="geekblue">
                {group.label ||
                  `${t('ui.users.place_groups.unnamed', 'Place')} #${group.place_group_id}`}
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
              <Button size="small" onClick={() => startAdd(group.place_group_id)}>
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
                    <Button
                      key="remove"
                      size="small"
                      danger
                      onClick={() => startRemove(group, member)}
                    >
                      {t('ui.users.place_groups.remove', 'Remove')}
                    </Button>,
                  ]}
                >
                  <Space direction="vertical" size={0}>
                    <Text>
                      {[member.owner?.first_name, member.owner?.last_name]
                        .filter(Boolean)
                        .join(' ')}{' '}
                      · {member.owner?.phone}
                    </Text>
                    {/* No per-member bottle count: the place holds ONE pool
                        (the statistic above) and it cannot be sliced per
                        coworker — `members` carries no balance at all. */}
                    <Text type="secondary">
                      {member.address_title || member.full_address}
                    </Text>
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
          </div>
        ))
      )}

      <div style={{ marginTop: 8 }}>
        <Button size="small" type="dashed" onClick={startManualCreate}>
          {t('ui.users.place_groups.create_action', 'New place group')}
        </Button>
      </div>

      <Divider style={{ margin: '12px 0' }} />

      {/* Section 2: co-location suggestions */}
      <Text strong>{t('ui.users.place_groups.suggestions_title', 'Possible same place')}</Text>
      {!suggestionsRequested ? (
        <div style={{ marginTop: 8 }}>
          <Button size="small" onClick={() => setSuggestionsRequestedFor(userId)}>
            {t('ui.users.place_groups.find_suggestions', 'Find possible same-place matches')}
          </Button>
        </div>
      ) : (
        <List
          size="small"
          loading={suggestionsQuery.isFetching}
          dataSource={suggestions}
          locale={{ emptyText: t('ui.users.place_groups.no_suggestions', 'No suggestions') }}
          renderItem={(suggestion) => (
            <List.Item
              key={suggestion.address_ids.join('-')}
              actions={[
                <Button
                  key="group"
                  size="small"
                  type="primary"
                  onClick={() => startCreateFromSuggestion(suggestion)}
                >
                  {t('ui.users.place_groups.group_action', 'Group as same place')}
                </Button>,
                <Button key="dismiss" size="small" onClick={() => startDismiss(suggestion)}>
                  {t('ui.users.place_groups.dismiss_action', 'Not the same place')}
                </Button>,
              ]}
            >
              <Space direction="vertical" size={0}>
                <Text>
                  {suggestion.members
                    .map((member) => [member.first_name, member.last_name].filter(Boolean).join(' '))
                    .join(' · ')}
                </Text>
                <Text type="secondary">
                  {suggestion.members[0]?.full_address} ·{' '}
                  {t('ui.users.place_groups.distinct_customers', 'customers')}:{' '}
                  {suggestion.distinct_customer_count}
                </Text>
              </Space>
            </List.Item>
          )}
        />
      )}

      <PlaceGroupConfirmModal
        action={action}
        pending={modalIsPending}
        onConfirm={handleConfirm}
        onCancel={closeAction}
      />
    </Card>
  );
}
