import React, { useState } from 'react';
import { Button, Card, Input, Space, Table, Tag, Typography, message } from 'antd';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import adminService from '../services/adminService';
import PlaceGroupConfirmModal from './PlaceGroupConfirmModal';
import { placeGroupErrorMessage } from './placeGroupCopy';
import { formatMoney } from '../utils/formatMoney';

const { Text } = Typography;

const PER_PAGE = 20;
const SUGGESTION_LIMIT = 20;

/**
 * The estate-wide "Grouped Addresses" tab (plan E, A1.3).
 *
 * `PlaceGroupPanel` answers "what does THIS customer share?" from inside the
 * user-details drawer, so an admin had to already suspect someone before they
 * could look. This is the other door: every place group that exists, with the
 * exposure it carries — the COD debt it OWES and the bottles it HOLDS, both,
 * because grouping pools the two alike — beside the co-located candidates the
 * engine has found across the whole estate.
 *
 * 🔴 READ-ONLY except through the shared confirm flow. A suggestion is NOT a
 * grouping: every candidate needs an explicit admin confirmation carrying a
 * free-text `reason`, entered in `PlaceGroupConfirmModal` — the SAME dialog the
 * per-customer panel uses, with the same mandatory-`reason` rule. There is
 * deliberately no bulk "accept all" control and no auto-grouping (spec §2.1
 * lists seven distinct ways that fails dangerously), and grouping two addresses
 * has money consequences the moment place-scoped COD collection is on.
 */
export default function GroupedAddressesPanel() {
  const { t } = useTranslation('users');
  const queryClient = useQueryClient();

  const [page, setPage] = useState(1);
  const [search, setSearch] = useState('');
  // action: null | { kind: 'create' | 'dismiss', payload } — the shared dialog.
  const [action, setAction] = useState(null);

  const groupsQuery = useQuery({
    queryKey: ['placeGroupsList', page, search],
    queryFn: () => adminService.listPlaceGroups({ page, perPage: PER_PAGE, search }),
  });
  const groups = groupsQuery.data?.data?.items || [];
  const pagination = groupsQuery.data?.data?.pagination || {};

  // The un-anchored clusterer runs over the whole estate on this call, which is
  // why the tab is mounted lazily by Users.js rather than on every page load.
  const suggestionsQuery = useQuery({
    queryKey: ['placeGroupSuggestionsGlobal', SUGGESTION_LIMIT],
    queryFn: () => adminService.getGlobalPlaceGroupSuggestions({ limit: SUGGESTION_LIMIT }),
  });
  const suggestions = suggestionsQuery.data?.data || [];

  const invalidatePlaceData = () => {
    queryClient.invalidateQueries({ queryKey: ['placeGroupsList'] });
    queryClient.invalidateQueries({ queryKey: ['placeGroupSuggestionsGlobal'] });
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

  const dismissMutation = useMutation({
    mutationFn: ({ addressIdA, addressIdB, actionReason }) =>
      adminService.dismissPlaceGroupSuggestion(addressIdA, addressIdB, actionReason),
    onSuccess: onMutationSuccess('ui.users.place_groups.dismiss_success', 'Suggestion dismissed'),
    onError: onMutationError,
  });

  const startCreateFromSuggestion = (suggestion) =>
    setAction({
      kind: 'create',
      payload: { addressIds: suggestion.address_ids, members: suggestion.members },
    });

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

  const handleConfirm = ({ addressIds, label, reason, merge }) => {
    if (action.kind === 'create') {
      createMutation.mutate({
        addressIds,
        groupLabel: label,
        actionReason: reason,
        merge,
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
    (action?.kind === 'dismiss' && dismissMutation.isPending);

  const groupColumns = [
    {
      title: t('ui.users.grouped_addresses.col_place', 'Place'),
      dataIndex: 'label',
      key: 'label',
      render: (label, row) => (
        <Tag color="geekblue">
          {label || `${t('ui.users.place_groups.unnamed', 'Place')} #${row.id}`}
        </Tag>
      ),
    },
    {
      // Distinct address OWNERS, not addresses: one person with two doors at
      // the same office is one customer, and the two figures are reported
      // separately rather than conflated.
      title: t('ui.users.grouped_addresses.col_members', 'Customers at this place'),
      dataIndex: 'member_count',
      key: 'member_count',
    },
    {
      title: t('ui.users.grouped_addresses.col_addresses', 'Addresses'),
      dataIndex: 'address_count',
      key: 'address_count',
    },
    {
      // The exposure BEFORE the action: an admin about to touch a group sees
      // what it is carrying first. A float on the wire (C6) — formatted here,
      // never pre-formatted by the backend.
      title: t('ui.users.grouped_addresses.col_cod_total', 'Open COD debt'),
      dataIndex: 'place_open_cod_debt_total',
      key: 'place_open_cod_debt_total',
      render: (value) => formatMoney(value),
    },
    {
      title: t('ui.users.grouped_addresses.col_cod_count', 'Unpaid COD orders'),
      dataIndex: 'active_cod_debt_count',
      key: 'active_cod_debt_count',
    },
    {
      // The OTHER half of the exposure — what the place HOLDS, beside what it
      // OWES. Grouping pools bottles into one indivisible place balance exactly
      // as it pools COD debt, so a row showing only the money shows half the
      // consequence of the act this tab makes easier.
      //
      // The label is the per-customer panel's own `union_balance` key, reused
      // verbatim rather than re-coined: one vocabulary for one figure, and no
      // new translation key (invariant 3b's byte-for-byte fallback rule makes a
      // second wording for the same number a liability, not a nicety).
      // A quantity, not money — never through `formatMoney`.
      title: t('ui.users.place_groups.union_balance', 'Bottles at this place'),
      dataIndex: 'bottle_exposure',
      key: 'bottle_exposure',
    },
  ];

  const suggestionColumns = [
    {
      title: t('ui.users.grouped_addresses.col_candidate', 'Possible same place'),
      key: 'members',
      render: (_, suggestion) => (
        <Space direction="vertical" size={0}>
          {(suggestion.members || []).map((member) => (
            <Text key={member.address_id}>
              {[member.first_name, member.last_name].filter(Boolean).join(' ')}
            </Text>
          ))}
          <Text type="secondary">{suggestion.members?.[0]?.full_address}</Text>
        </Space>
      ),
    },
    {
      title: t('ui.users.grouped_addresses.col_distinct_customers', 'Distinct customers'),
      dataIndex: 'distinct_customer_count',
      key: 'distinct_customer_count',
    },
    {
      title: t('ui.users.grouped_addresses.col_actions', 'Actions'),
      key: 'actions',
      // Per ROW, always. Anything that acted on more than one candidate at a
      // time would be auto-grouping wearing a button.
      render: (_, suggestion) => (
        <Space>
          <Button size="small" type="primary" onClick={() => startCreateFromSuggestion(suggestion)}>
            {t('ui.users.place_groups.group_action', 'Group as same place')}
          </Button>
          <Button size="small" onClick={() => startDismiss(suggestion)}>
            {t('ui.users.place_groups.dismiss_action', 'Not the same place')}
          </Button>
        </Space>
      ),
    },
  ];

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Card
        size="small"
        title={t('ui.users.grouped_addresses.groups_title', 'Existing place groups')}
        extra={
          <Input.Search
            allowClear
            style={{ width: 260 }}
            placeholder={t('ui.users.grouped_addresses.search_placeholder', 'Search by place label')}
            onSearch={(value) => {
              setSearch((value || '').trim());
              setPage(1);
            }}
          />
        }
      >
        <Table
          rowKey="id"
          size="small"
          loading={groupsQuery.isLoading}
          columns={groupColumns}
          dataSource={groups}
          locale={{ emptyText: t('ui.users.grouped_addresses.no_groups', 'No place groups yet') }}
          scroll={{ x: 'max-content' }}
          pagination={{
            current: pagination.page || page,
            pageSize: pagination.per_page || PER_PAGE,
            total: pagination.total || 0,
            showSizeChanger: false,
            onChange: setPage,
          }}
        />
      </Card>

      <Card
        size="small"
        title={t('ui.users.grouped_addresses.suggestions_title', 'Suggested candidates')}
      >
        <Text type="secondary">
          {t(
            'ui.users.grouped_addresses.no_auto_group_hint',
            'Suggestions are never grouped automatically. Each one needs an admin confirmation with a reason.'
          )}
        </Text>
        <Table
          rowKey={(suggestion) => (suggestion.address_ids || []).join('-')}
          size="small"
          style={{ marginTop: 8 }}
          loading={suggestionsQuery.isLoading}
          columns={suggestionColumns}
          dataSource={suggestions}
          locale={{
            emptyText: t('ui.users.grouped_addresses.no_suggestions', 'No suggested candidates'),
          }}
          scroll={{ x: 'max-content' }}
          pagination={false}
        />
      </Card>

      <PlaceGroupConfirmModal
        action={action}
        pending={modalIsPending}
        onConfirm={handleConfirm}
        onCancel={closeAction}
      />
    </Space>
  );
}
