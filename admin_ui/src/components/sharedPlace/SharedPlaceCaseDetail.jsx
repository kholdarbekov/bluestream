import React, { useState } from 'react';
import { Link } from 'react-router-dom';
import { Alert, Button, Descriptions, Input, Radio, Space, Spin, Tag, Typography, message } from 'antd';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import adminService from '../../services/adminService';
import LocationBubble from '../support/LocationBubble';
import LinkAccountsConfirm from '../LinkAccountsConfirm';
import PlaceGroupConfirmModal from '../PlaceGroupConfirmModal';
import { placeGroupErrorMessage } from '../placeGroupCopy';
import { invalidatePlaceGroupQueries } from '../placeGroupQueries';
import SharedPlaceAdjustConfirm from './SharedPlaceAdjustConfirm';
import { apiErrorCode } from '../../utils/apiError';
import { formatLocaleDateTime } from '../../utils/dateUtils';
import { SHARED_PLACE_CASES_KEY, TRYOUT_LINK_PARAM } from '../../utils/constants';

const { Text, Title } = Typography;

/** The refusals this screen explains inline; the interceptor stays quiet for them (api.js). */
export const RESOLVE_HANDLED_CODES = [
  'SHARED_PLACE_NOT_VERIFIED',
  'SHARED_PLACE_RESOLVE_INVALID',
  'SHARED_PLACE_RESOLUTION_CONFLICT',
];

const REFUSAL_COPY = new Map([
  [
    'SHARED_PLACE_NOT_VERIFIED',
    [
      'ui.users.shared_place_reports.refusal_not_verified',
      'The grouping or link this choice needs is not in place now. Nothing was recorded.',
    ],
  ],
  [
    'SHARED_PLACE_RESOLVE_INVALID',
    [
      'ui.users.shared_place_reports.refusal_invalid',
      'This choice cannot be recorded as entered. Nothing was recorded; check the numbers and try again.',
    ],
  ],
  [
    'SHARED_PLACE_RESOLUTION_CONFLICT',
    [
      'ui.users.shared_place_reports.refusal_conflict',
      'Another outcome is already recorded for this case. Nothing was changed.',
    ],
  ],
]);

// The six choices (the backend's CHOICE_KINDS), in the order `choices.kinds` lists them.
const CHOICE_COPY = new Map([
  ['shared_place', ['ui.users.shared_place_reports.choice_shared_place', 'Shared place — different people, one place']],
  ['same_home', ['ui.users.shared_place_reports.choice_same_home', 'Same person, same home']],
  ['other_place', ['ui.users.shared_place_reports.choice_other_place', 'Same person, a different place']],
  ['own_bottles', ['ui.users.shared_place_reports.choice_own_bottles', "Customer's own bottles"]],
  ['wrong_count', ['ui.users.shared_place_reports.choice_wrong_count', 'Wrong count']],
  ['close', ['ui.users.shared_place_reports.choice_close', 'Close, no action']],
]);

export const choiceText = (t, kind) => {
  const copy = CHOICE_COPY.get(kind);
  return copy ? t(copy[0], copy[1]) : kind;
};

/**
 * What the driver did, in one short phrase (the list's column, and the detail when nothing was picked).
 * `answerKind` is the backend's published `answer_kind`: `no_candidates` is the note-only screen's "none"
 * (nothing was offered), which is never "None of these" (spec §4.4).
 */
export const driverSaidText = (t, driverStatus, answerKind) => {
  if (answerKind === 'picked') return t('ui.users.shared_place_reports.driver_picked_short', 'Picked a place');
  if (answerKind === 'none') return t('ui.users.shared_place_reports.driver_none', 'None of these');
  if (answerKind === 'no_candidates') return t('ui.users.shared_place_reports.driver_no_candidates', 'No nearby places');
  if (answerKind === 'skipped') return t('ui.users.shared_place_reports.driver_skipped', 'Skipped');
  if (driverStatus === 'not_asked') return t('ui.users.shared_place_reports.driver_not_asked', 'Entered by an admin');
  if (driverStatus === 'awaiting') return t('ui.users.shared_place_reports.driver_awaiting', 'Waiting for the answer');
  return t('ui.users.shared_place_reports.driver_no_answer', "Didn't answer");
};

const fullName = (person) => [person?.first_name, person?.last_name].filter(Boolean).join(' ');

/** One address as PlaceGroupConfirmModal's picker lists it (suggestionMemberToOption). */
const modalMember = (addressId, owner, title, fullAddress) => ({
  address_id: addressId,
  first_name: owner?.first_name,
  last_name: owner?.last_name,
  phone: owner?.phone,
  title,
  full_address: fullAddress,
});

const memberLine = (owner, title, fullAddress) =>
  [[fullName(owner), owner?.phone].filter(Boolean).join(' · '), title || fullAddress].filter(Boolean).join(' — ');

/**
 * One shared-place case (spec 2026-11-10 §4.5; §9 binds): what happened, what the
 * driver said, where, the live facts, and the six choices.
 *
 * Every number and every choice comes from the detail payload; the screen
 * re-derives no rule. The server verifies every outcome against live state, so
 * the screen only orders the existing writes (link → group → rename/note) and
 * then records the decision with one resolve call.
 *
 * The A/B/C target is one pick: a member of an offered place, a search hit, or
 * (for B and C, whose target is a person) an account already linked.
 */
export default function SharedPlaceCaseDetail({ caseId, onViewUser }) {
  const { t } = useTranslation('users');
  const queryClient = useQueryClient();

  const detailQuery = useQuery({
    queryKey: [...SHARED_PLACE_CASES_KEY, 'detail', caseId],
    queryFn: () => adminService.getSharedPlaceCase(caseId),
    enabled: caseId != null,
  });
  const detail = detailQuery.data?.case;

  const [pickedKey, setPickedKey] = useState(null);
  const [searchTerm, setSearchTerm] = useState('');
  const [refusal, setRefusal] = useState(null);
  // The link step of B and C: who to link, and what runs once linked.
  const [linkFlow, setLinkFlow] = useState(null);
  // The grouping step of A and B: PlaceGroupConfirmModal's action, plus what to resolve after it.
  const [groupFlow, setGroupFlow] = useState(null);
  const [preparing, setPreparing] = useState(false);
  // The case-owned confirmation: { mode, targetUserId, reason }.
  const [confirm, setConfirm] = useState(null);

  const searchQuery = useQuery({
    queryKey: ['sharedPlaceAddressSearch', searchTerm],
    queryFn: () => adminService.searchAddresses(searchTerm, false),
    enabled: searchTerm.length >= 2,
  });
  const searchHits = searchQuery.data?.data?.addresses || [];

  const resolveMutation = useMutation({
    mutationFn: (payload) =>
      adminService.resolveSharedPlaceCase(caseId, payload, { handledErrorCodes: RESOLVE_HANDLED_CODES }),
    onSuccess: () => {
      message.success(t('ui.users.shared_place_reports.resolved', 'Case resolved'));
      setConfirm(null);
      setRefusal(null);
    },
    onError: (error) => {
      // Any other failure is toasted by the interceptor and leaves the dialog open for a retry.
      const code = apiErrorCode(error);
      if (RESOLVE_HANDLED_CODES.includes(code)) {
        setRefusal(code);
        setConfirm(null);
      }
    },
    // The list, the badge and this case all read under the one prefix.
    onSettled: () => queryClient.invalidateQueries({ queryKey: SHARED_PLACE_CASES_KEY }),
  });

  const groupMutation = useMutation({
    mutationFn: async ({ flow, values }) => {
      if (flow.action.kind === 'create') {
        await adminService.createPlaceGroup(values.addressIds, values.label, values.reason, values.merge, {
          notes: values.notes,
        });
        return;
      }
      // A retry after a failed rename: the add already landed, and adding again would be refused as "already
      // grouped" (PLACE_GROUP_ADDRESS_ALREADY_GROUPED), so the label and the note would never be saved.
      const added = flow.added || detailQuery.data?.case?.address?.place_group?.id === flow.groupId;
      if (!added) {
        await adminService.addPlaceGroupAddresses(flow.groupId, values.addressIds, values.reason, values.merge);
        setGroupFlow((current) => (current === flow ? { ...current, added: true } : current));
      }
      const labelChanged = (values.label || '') !== flow.existingLabel;
      const noteChanged = values.notes !== flow.existingNote;
      if (labelChanged || noteChanged) {
        // `null` keeps the label (rename_place_group), so a note alone never touches it.
        await adminService.renamePlaceGroup(flow.groupId, labelChanged ? values.label : null, values.reason, {
          notes: values.notes,
        });
      }
    },
    onSuccess: (_, { flow, values }) => {
      setGroupFlow(null);
      resolveMutation.mutate({
        kind: flow.resolveKind,
        ...(flow.resolveKind === 'same_home' ? { target_user_id: flow.targetUserId } : {}),
        ...(flow.targetAddressId != null ? { target_address_id: flow.targetAddressId } : {}),
        reason: values.reason,
      });
    },
    onError: (error) => {
      message.error(placeGroupErrorMessage(error, t, t('ui.users.place_groups.action_failed', 'Action failed')));
    },
    // An add that landed before a failed rename is still a change every place view must see.
    onSettled: () => invalidatePlaceGroupQueries(queryClient),
  });

  if (detailQuery.isLoading) {
    return <Spin />;
  }
  if (!detail) {
    return <Alert type="error" message={t('ui.users.shared_place_reports.load_failed', 'Could not load this case.')} />;
  }

  const { address, customer, choices } = detail;
  const kinds = choices?.kinds || [];
  const open = kinds.length > 0;
  const grouped = Boolean(choices?.grouping_done);
  const unnamed = t('ui.users.shared_place_reports.unnamed_place', 'Unnamed place');
  const groupLabel = address.place_group ? address.place_group.label || unnamed : null;
  const driverNote = detail.answer?.note ?? '';
  const linkedAccounts = detail.facts?.linked_accounts || [];
  const pickedCandidate = detail.answer?.kind === 'picked'
    ? (detail.candidates || []).find((candidate) => candidate.index === detail.answer.picked_index)
    : null;

  // ---- the one target pick ---------------------------------------------------
  const options = new Map();
  (detail.candidates || []).forEach((candidate) => {
    if (!candidate.live?.exists) return;
    const { live } = candidate;
    const members = live.members.map((m) => modalMember(m.address_id, m.owner, m.title, m.full_address));
    live.members.forEach((member) => {
      options.set(`c:${member.address_id}`, {
        place: { addressId: member.address_id, groupId: live.place_group_id, members },
        user: member.owner,
      });
    });
  });
  searchHits.forEach((hit) => {
    options.set(`s:${hit.address_id}`, {
      place: {
        addressId: hit.address_id,
        groupId: hit.address_group_id,
        members: [modalMember(hit.address_id, hit.owner, hit.title, hit.full_address)],
      },
      user: hit.owner,
    });
  });
  linkedAccounts.forEach((account) => options.set(`u:${account.id}`, { place: null, user: account }));

  // Pre-picked from the driver's answer: the picked place's member named as the driver saw it.
  const pickedMember = pickedCandidate?.live?.exists
    ? pickedCandidate.live.members.find((m) => m.owner?.first_name === pickedCandidate.first_name)
      || pickedCandidate.live.members[0]
    : null;
  const defaultKey = pickedMember ? `c:${pickedMember.address_id}` : null;
  const selectedKey = options.has(pickedKey) ? pickedKey : defaultKey;
  const selected = options.get(selectedKey) || null;
  const targetPlace = selected?.place || null;
  // Linking needs two accounts: the customer's own address names no one to link.
  const targetUser = selected?.user && selected.user.id !== customer.id ? selected.user : null;
  const claimTargetUserId = detail.claim?.payload?.target_user_id;
  const isLinked = (userId) => linkedAccounts.some((account) => account.id === userId);

  // ---- the flows -------------------------------------------------------------
  const openConfirm = (mode, { targetUserId, reason = '' } = {}) => {
    setConfirm({ mode, targetUserId, reason });
  };

  /** B and C: link the target unless it already is, then go on with the link step's reason. */
  const withLink = (user, proceed) => {
    if (isLinked(user.id)) {
      proceed({ reason: '', linkedNow: false });
    } else {
      setLinkFlow({ user, proceed });
    }
  };

  const onLinked = (_result, linkReason) => {
    const flow = linkFlow;
    setLinkFlow(null);
    flow?.proceed({ reason: linkReason || '', linkedNow: true });
  };

  /** A and B: open the grouping dialog, pre-filled; on `add`, from the place's current label and note. */
  const startGrouping = async ({ resolveKind, place, targetUserId, reason = '' }) => {
    const caseMember = modalMember(address.id, customer, address.title, address.full_address);
    if (place.groupId == null) {
      setGroupFlow({
        resolveKind,
        targetUserId,
        targetAddressId: place.addressId,
        action: {
          kind: 'create',
          payload: {
            addressIds: [address.id, ...place.members.map((m) => m.address_id)],
            members: [caseMember, ...place.members],
            notes: driverNote,
            reason,
          },
        },
      });
      return;
    }
    setPreparing(true);
    try {
      const group = (await adminService.getPlaceGroup(place.groupId))?.data;
      if (!group) throw new Error('missing place group');
      const existingLabel = group.label || '';
      const existingNote = (group.notes || '').trim();
      setGroupFlow({
        resolveKind,
        targetUserId,
        groupId: place.groupId,
        existingLabel,
        existingNote,
        action: {
          kind: 'add',
          payload: {
            groupId: place.groupId,
            addressIds: [address.id],
            members: [caseMember],
            currentLabel: existingLabel,
            // The place's own note wins; the driver's fills a place that has none.
            notes: existingNote || driverNote,
            reason,
          },
        },
      });
    } catch (error) {
      message.error(placeGroupErrorMessage(error, t, t('ui.users.place_groups.load_failed', 'Could not load this place group.')));
    } finally {
      setPreparing(false);
    }
  };

  const onChoice = (kind) => {
    setRefusal(null);
    if (kind === 'shared_place') {
      if (grouped) openConfirm('shared_place');
      else startGrouping({ resolveKind: 'shared_place', place: targetPlace });
    } else if (kind === 'same_home') {
      withLink(targetUser, ({ reason }) => {
        if (grouped) openConfirm('same_home', { targetUserId: targetUser.id, reason });
        else startGrouping({ resolveKind: 'same_home', place: targetPlace, targetUserId: targetUser.id, reason });
      });
    } else if (kind === 'other_place') {
      if (claimTargetUserId != null) {
        openConfirm('other_place', { targetUserId: claimTargetUserId });
        return;
      }
      const user = targetUser;
      withLink(user, async ({ reason, linkedNow }) => {
        // The move sources are the linked cluster's places, so they exist only once the link does.
        if (linkedNow) await detailQuery.refetch();
        openConfirm('other_place', { targetUserId: user.id, reason });
      });
    } else {
      openConfirm(kind);
    }
  };

  const choiceEnabled = (kind) => {
    if (kind === 'shared_place') return grouped || targetPlace != null;
    if (kind === 'same_home') return targetUser != null && (grouped || targetPlace != null);
    if (kind === 'other_place') return claimTargetUserId != null || targetUser != null;
    return true;
  };

  const choiceButtonText = (kind) => {
    if (grouped && kind === 'shared_place') {
      return t('ui.users.shared_place_reports.confirm_shared_place', "Confirm: shared place '{{label}}'", { label: groupLabel });
    }
    if (grouped && kind === 'same_home') {
      return t('ui.users.shared_place_reports.confirm_same_home', 'Confirm: same person, same home');
    }
    return choiceText(t, kind);
  };

  const busy = preparing || groupMutation.isPending || resolveMutation.isPending;

  // ---- rendering -------------------------------------------------------------
  const targetOption = (key, text) => (open ? <Radio value={key}>{text}</Radio> : <Text>{text}</Text>);

  const driverLine = () => {
    if (pickedCandidate) {
      return t('ui.users.shared_place_reports.driver_picked', 'Picked: {{name}} · {{empties}} empties · {{metres}} m', {
        name: pickedCandidate.first_name,
        empties: pickedCandidate.empties,
        metres: pickedCandidate.distance_m,
      });
    }
    return driverSaidText(t, detail.driver_status, detail.answer?.kind);
  };

  const emptyReasonText = () => {
    if (detail.empty_reason === 'no_pin') {
      return t('ui.users.shared_place_reports.empty_no_pin', 'This address has no map pin, so no nearby places were looked up.');
    }
    if (detail.empty_reason === 'none_within_radius') {
      return t('ui.users.shared_place_reports.empty_none_within_radius', 'No other customer keeps empties close to this address.');
    }
    if (detail.empty_reason === 'none_eligible') {
      return t('ui.users.shared_place_reports.empty_none_eligible', 'The places nearby belong to business, shop or staff accounts, which are never offered.');
    }
    return t('ui.users.shared_place_reports.empty_none', 'No places were offered.');
  };

  const liveLine = (live) => {
    if (!live.exists) {
      return <Tag>{t('ui.users.shared_place_reports.candidate_gone', 'No longer exists')}</Tag>;
    }
    if (live.place_group_id != null) {
      return (
        <Text type="secondary">
          {t('ui.users.shared_place_reports.candidate_now_place', "Now the place '{{label}}' · {{empties}} empties", {
            label: live.label || unnamed,
            empties: live.empties,
          })}
        </Text>
      );
    }
    return (
      <Text type="secondary">
        {t('ui.users.shared_place_reports.candidate_now', 'Now {{empties}} empties', { empties: live.empties })}
      </Text>
    );
  };

  const hitText = (hit) => {
    const where = memberLine(hit.owner, hit.title, hit.full_address);
    if (hit.address_group_id == null) return where;
    const place = t('ui.users.shared_place_reports.hit_place', "The place '{{label}}'", {
      label: hit.address_group_label || unnamed,
    });
    return `${place} — ${where}`;
  };

  const facts = [];
  if (linkedAccounts.length) {
    facts.push(
      <Space key="linked" direction="vertical" size={2}>
        <Text>
          {t('ui.users.shared_place_reports.fact_linked', 'Already linked to {{names}}', {
            names: linkedAccounts.map(fullName).join(', '),
          })}
        </Text>
        {open && linkedAccounts.map((account) => (
          <React.Fragment key={account.id}>{targetOption(`u:${account.id}`, fullName(account))}</React.Fragment>
        ))}
      </Space>
    );
  }
  if (detail.facts?.referred_by) {
    facts.push(
      <Text key="referred">
        {t('ui.users.shared_place_reports.fact_referred', 'Referred by {{name}}', {
          name: fullName(detail.facts.referred_by),
        })}
      </Text>
    );
  }
  if (groupLabel != null) {
    facts.push(
      <Text key="grouped">
        {t('ui.users.shared_place_reports.fact_grouped', "This address has since been grouped into '{{label}}'", {
          label: groupLabel,
        })}
      </Text>
    );
  }
  // Every converted try-out that handed trial bottles over, settled ones included (0 out): settling it is the
  // admin's first step, and the hint must still be there when they come back (spec §9: then D, not Close).
  if ((detail.tryouts || []).length) {
    facts.push(
      <Space key="tryouts" direction="vertical" size={2}>
        {detail.tryouts.map((tryout) => (
          <Space key={tryout.tryout_id} size={6}>
            <Link to={`/tryouts?${TRYOUT_LINK_PARAM}=${tryout.tryout_id}`}>{tryout.tryout_number}</Link>
            {tryout.outstanding_bottles_total > 0 && (
              <Text>
                {t('ui.users.shared_place_reports.tryout_outstanding', '{{n}} trial bottles still out', {
                  n: tryout.outstanding_bottles_total,
                })}
              </Text>
            )}
          </Space>
        ))}
        {open && (
          <Text type="secondary">
            {t(
              'ui.users.shared_place_reports.tryout_hint',
              'Settle the trial bottles on the try-out, then choose "Customer\'s own bottles" (+{{n}}) so this address isn\'t short.',
              { n: detail.returned_qty }
            )}
          </Text>
        )}
      </Space>
    );
  }

  const confirmTitle = confirm
    ? (confirm.mode === 'shared_place' || confirm.mode === 'same_home') && grouped
      ? choiceButtonText(confirm.mode)
      : choiceText(t, confirm.mode)
    : null;

  const resolution = detail.resolution;

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <section>
        <Title level={5}>{t('ui.users.shared_place_reports.what_happened', 'What happened')}</Title>
        <Descriptions column={1} size="small">
          <Descriptions.Item label={t('ui.users.shared_place_reports.order', 'Order')}>
            {detail.order.order_number}
          </Descriptions.Item>
          <Descriptions.Item label={t('ui.users.shared_place_reports.customer', 'Customer')}>
            <Space size={6} wrap>
              <Button type="link" size="small" style={{ padding: 0 }} onClick={() => onViewUser?.(customer.id)}>
                {fullName(customer)}
              </Button>
              {customer.phone && <Text type="secondary">{customer.phone}</Text>}
            </Space>
          </Descriptions.Item>
          <Descriptions.Item label={t('ui.users.shared_place_reports.delivered', 'Delivered')}>
            {detail.driver
              ? t('ui.users.shared_place_reports.delivered_by', '{{when}} by {{driver}}', {
                when: formatLocaleDateTime(detail.created_at),
                driver: detail.driver.first_name,
              })
              : t('ui.users.shared_place_reports.delivered_by_admin', '{{when}}, entered by an admin', {
                when: formatLocaleDateTime(detail.created_at),
              })}
          </Descriptions.Item>
        </Descriptions>
        <Text>
          {t(
            'ui.users.shared_place_reports.counts',
            'got {{delivered}} full, handed back {{returned}} empties; this address had {{before}} on record before',
            { delivered: detail.delivered_qty, returned: detail.returned_qty, before: detail.place_balance_before }
          )}
        </Text>
      </section>

      <section>
        <Title level={5}>{t('ui.users.shared_place_reports.driver_said', 'What the driver said')}</Title>
        <Space direction="vertical" size={4}>
          <Text strong>{driverLine()}</Text>
          {driverNote && (
            <Space size={6} align="start">
              <Text type="secondary">{t('ui.users.shared_place_reports.driver_note', "Driver's note")}</Text>
              <Text>{driverNote}</Text>
            </Space>
          )}
        </Space>
      </section>

      <Radio.Group value={selectedKey} onChange={(event) => setPickedKey(event.target.value)} style={{ width: '100%' }}>
        <Space direction="vertical" size={16} style={{ width: '100%' }}>
          <section>
            <Title level={5}>{t('ui.users.shared_place_reports.where', 'Where')}</Title>
            <Space direction="vertical" size={8} style={{ width: '100%' }}>
              <Text>
                {[address.title, address.full_address].filter(Boolean).join(' — ')}
                {address.apartment_number
                  ? ` · ${t('ui.users.shared_place_reports.apartment', 'Apartment {{number}}', { number: address.apartment_number })}`
                  : ''}
              </Text>
              <LocationBubble message={{ latitude: address.latitude, longitude: address.longitude }} />
              {(detail.candidates || []).length ? (
                <Space direction="vertical" size={8} style={{ width: '100%' }}>
                  <Text type="secondary">{t('ui.users.shared_place_reports.offered', 'Places offered to the driver')}</Text>
                  {detail.candidates.map((candidate) => (
                    <Space key={candidate.index} direction="vertical" size={2}>
                      <Text>
                        {t('ui.users.shared_place_reports.candidate', '{{name}} · {{empties}} empties · {{metres}} m', {
                          name: candidate.first_name,
                          empties: candidate.empties,
                          metres: candidate.distance_m,
                        })}
                      </Text>
                      {liveLine(candidate.live)}
                      {candidate.live?.exists && candidate.live.members.map((member) => (
                        <React.Fragment key={member.address_id}>
                          {targetOption(`c:${member.address_id}`, memberLine(member.owner, member.title, member.full_address))}
                        </React.Fragment>
                      ))}
                    </Space>
                  ))}
                </Space>
              ) : (
                <Text type="secondary">{emptyReasonText()}</Text>
              )}
              {open && (
                <Space direction="vertical" size={4} style={{ width: '100%' }}>
                  <Text type="secondary">{t('ui.users.shared_place_reports.search', 'Another place')}</Text>
                  <Input.Search
                    allowClear
                    placeholder={t('ui.users.shared_place_reports.search_placeholder', 'Find an address by phone, name or address')}
                    onSearch={(value) => setSearchTerm((value || '').trim())}
                    loading={searchQuery.isFetching}
                  />
                  {searchHits.map((hit) => (
                    <React.Fragment key={hit.address_id}>{targetOption(`s:${hit.address_id}`, hitText(hit))}</React.Fragment>
                  ))}
                  {searchQuery.isSuccess && !searchHits.length && (
                    <Text type="secondary">{t('ui.users.shared_place_reports.search_none', 'No addresses found')}</Text>
                  )}
                </Space>
              )}
            </Space>
          </section>

          {facts.length > 0 && (
            <section>
              <Title level={5}>{t('ui.users.shared_place_reports.facts', 'Read now')}</Title>
              <Space direction="vertical" size={6}>{facts}</Space>
            </section>
          )}
        </Space>
      </Radio.Group>

      <section>
        <Title level={5}>{t('ui.users.shared_place_reports.decision', 'Decision')}</Title>
        <Space direction="vertical" size={8} style={{ width: '100%' }}>
          {refusal && REFUSAL_COPY.has(refusal) && (
            <Alert
              type="error"
              showIcon
              closable
              onClose={() => setRefusal(null)}
              message={t(REFUSAL_COPY.get(refusal)[0], REFUSAL_COPY.get(refusal)[1])}
            />
          )}
          {open ? (
            <>
              <Space wrap>
                {kinds.map((kind) => (
                  <Button key={kind} disabled={busy || !choiceEnabled(kind)} onClick={() => onChoice(kind)}>
                    {choiceButtonText(kind)}
                  </Button>
                ))}
              </Space>
              {kinds.some((kind) => !choiceEnabled(kind)) && (
                <Text type="secondary">
                  {t('ui.users.shared_place_reports.needs_target', 'Pick a place or another account above to use the greyed-out choices.')}
                </Text>
              )}
            </>
          ) : resolution && (
            <Descriptions column={1} size="small">
              <Descriptions.Item label={t('ui.users.shared_place_reports.outcome', 'Outcome')}>
                {choiceText(t, resolution.kind)}
              </Descriptions.Item>
              <Descriptions.Item label={t('ui.users.shared_place_reports.reason', 'Reason')}>
                {resolution.reason}
              </Descriptions.Item>
              <Descriptions.Item label={t('ui.users.shared_place_reports.decided', 'Decided')}>
                {resolution.decided_by
                  ? t('ui.users.shared_place_reports.decided_by', '{{name}}, {{when}}', {
                    name: resolution.decided_by.first_name,
                    when: formatLocaleDateTime(resolution.decided_at),
                  })
                  : formatLocaleDateTime(resolution.decided_at)}
              </Descriptions.Item>
            </Descriptions>
          )}
        </Space>
      </section>

      <LinkAccountsConfirm
        userId={customer.id}
        candidate={linkFlow?.user}
        open={linkFlow != null}
        onClose={() => setLinkFlow(null)}
        onLinked={onLinked}
      />
      <PlaceGroupConfirmModal
        action={groupFlow?.action || null}
        pending={groupMutation.isPending}
        onConfirm={(values) => groupMutation.mutate({ flow: groupFlow, values })}
        onCancel={() => setGroupFlow(null)}
      />
      {confirm && (
        <SharedPlaceAdjustConfirm
          mode={confirm.mode}
          title={confirmTitle}
          detail={detail}
          groupLabel={groupLabel}
          targetUserId={confirm.targetUserId}
          defaultReason={confirm.reason}
          pending={resolveMutation.isPending}
          onSubmit={(payload) => resolveMutation.mutate(payload)}
          onCancel={() => setConfirm(null)}
        />
      )}
    </Space>
  );
}
