import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
  Button,
  Checkbox,
  Input,
  InputNumber,
  List,
  Modal,
  Select,
  Space,
  Statistic,
  Typography,
  message,
} from 'antd';
import { useTranslation } from 'react-i18next';
import dayjs from 'dayjs';
import adminService from '../services/adminService';
import { placeGroupErrorMessage } from './placeGroupCopy';

const { Text } = Typography;
const { TextArea } = Input;

/** One label shape for every address the picker can show (search hit or suggestion member). */
const addressOptionLabel = ({ firstName, lastName, phone, title, fullAddress }) => {
  const who = [firstName, lastName].filter(Boolean).join(' ');
  const where = title || fullAddress;
  return [[who, phone].filter(Boolean).join(' · '), where].filter(Boolean).join(' — ');
};

const searchHitToOption = (hit) => ({
  value: hit.address_id,
  label: addressOptionLabel({
    firstName: hit.owner?.first_name,
    lastName: hit.owner?.last_name,
    phone: hit.owner?.phone,
    title: hit.title,
    fullAddress: hit.full_address,
  }),
});

const suggestionMemberToOption = (member) => ({
  value: member.address_id,
  label: addressOptionLabel({
    firstName: member.first_name,
    lastName: member.last_name,
    phone: member.phone,
    title: member.title,
    fullAddress: member.full_address,
  }),
});

/** Ledger quantities are signed; a bare "4" beside a "-4" reads as the same move. */
const signedQuantity = (quantity) => {
  const value = Number(quantity || 0);
  return value > 0 ? `+${value}` : `${value}`;
};

const numberOrZero = (value) => (Number.isFinite(Number(value)) ? Number(value) : 0);

/**
 * The figure the place will actually HOLD once this merge commits —
 * `stored_balance - excluded_total`, which the backend measures the
 * `resultingBalance` override against (`_apply_merge_review`).
 *
 * NOT `resulting_balance`: that one is `computed_balance - excluded_total`, the
 * LEDGER's story. The two agree on every place whose ledger explains its stored
 * figure, and diverge on exactly the places this review exists for — dev address
 * 24 holds 20.00 with zero ledger rows, so its `resulting_balance` is 0 while
 * the place ends up holding 20. Pre-filling the override with 0 there would
 * offer the admin a number that is not what happens if they accept it.
 */
const projectedBalance = (preview) =>
  numberOrZero(preview?.projected_place_balance ?? preview?.resulting_balance);

/**
 * The ONE confirm flow for every place-group write, and the ONE place the
 * mandatory `reason` is enforced in the admin UI.
 *
 * EXTRACTED from PlaceGroupPanel.jsx (spec 9's per-customer panel) so the
 * estate-wide "Grouped Addresses" tab can route "Group as same place" through
 * the same dialog instead of growing a second copy of it — two copies is how a
 * blank-reason grouping ships from the screen nobody re-reviewed. Behaviour is
 * unchanged: the picker, the label, the split, the merge review and
 * `confirmDisabled` all moved together, byte for byte.
 *
 * Every piece of dialog-local state lives here; the caller owns only WHICH
 * action is pending and WHAT to do when it is confirmed:
 *
 *   action  - null (closed) or { kind, payload }
 *             create : { addressIds?, members? }        pre-picked + pre-listed
 *             add    : { groupId, addressIds?, members? }
 *             remove : { groupId, addressId, placeBalance, suggestedBottlesLeaving }
 *             dismiss: { addressIdA, addressIdB }
 *   pending - the caller's mutation is in flight
 *   onConfirm({ addressIds, label, reason, bottlesLeaving, merge })
 *   onCancel()
 */
export default function PlaceGroupConfirmModal({ action, pending = false, onConfirm, onCancel }) {
  const { t } = useTranslation('users');

  const [reason, setReason] = useState('');
  const [label, setLabel] = useState('');
  const [pickedIds, setPickedIds] = useState([]);
  const [searchOptions, setSearchOptions] = useState([]);
  // Spec 7.1: how many of the place's bottles leave WITH the departing address.
  // `null` while the field is empty mid-edit; normalised to a number on send.
  const [bottlesLeaving, setBottlesLeaving] = useState(0);

  // Spec 7.4's merge review. `mergeReview` is the CONFIRMED decision that rides
  // along with the join; everything else is the open dialog's working state.
  const [mergeReview, setMergeReview] = useState(null);
  const [mergeOpen, setMergeOpen] = useState(false);
  const [mergePreview, setMergePreview] = useState(null);
  const [mergeLoading, setMergeLoading] = useState(false);
  const [excludedIds, setExcludedIds] = useState([]);
  const [overrideBalance, setOverrideBalance] = useState(null);
  const [overrideTouched, setOverrideTouched] = useState(false);

  // Debounced so a fast typist does not fire one request per keystroke. The
  // pending timer lives in a ref, not in the callback's closure: react-i18next
  // hands back a fresh `t` on every language change, and a closure-held timer
  // id would be lost with the old callback (leaving an uncancellable request
  // in flight).
  const searchTimerRef = useRef(null);
  useEffect(() => () => clearTimeout(searchTimerRef.current), []);

  // A newer merge preview must always win: each exclusion toggle fires its own
  // request and the responses can land out of order, which would otherwise
  // leave the figures describing a set the admin has already changed.
  const previewSeq = useRef(0);

  /**
   * Every open starts from a clean dialog — the reset the panel used to do in
   * `closeAction`. Adjusting state during render (React's documented pattern
   * for "a prop changed") rather than in an effect, so the dialog never paints
   * one frame carrying the previous action's reason.
   */
  const [openedAction, setOpenedAction] = useState(null);
  if (action !== openedAction) {
    setOpenedAction(action);
    if (action) {
      const payload = action.payload || {};
      setReason('');
      setLabel('');
      setPickedIds(payload.addressIds || []);
      // Pre-fill the picker so the admin sees exactly what will be grouped and
      // can still widen the set before confirming.
      setSearchOptions((payload.members || []).map(suggestionMemberToOption));
      // A place at or below zero has nothing to give: its cap is max(0, balance),
      // so any non-zero pre-fill there is a guaranteed PLACE_SPLIT_INVALID.
      setBottlesLeaving(
        numberOrZero(payload.placeBalance) > 0
          ? Math.max(0, numberOrZero(payload.suggestedBottlesLeaving))
          : 0
      );
      setMergeReview(null);
      setMergeOpen(false);
      setMergePreview(null);
      setExcludedIds([]);
      setOverrideBalance(null);
      setOverrideTouched(false);
    }
  }

  const runSearch = useCallback(
    (value) => {
      clearTimeout(searchTimerRef.current);
      const query = (value || '').trim();
      if (query.length < 2) {
        setSearchOptions([]);
        return;
      }
      searchTimerRef.current = setTimeout(async () => {
        try {
          const result = await adminService.searchAddresses(query, true);
          setSearchOptions((result?.data?.addresses || []).map(searchHitToOption));
        } catch (error) {
          setSearchOptions([]);
          message.error(
            placeGroupErrorMessage(
              error,
              t,
              t('ui.users.place_groups.search_failed', 'Address search failed')
            )
          );
        }
      }, 250);
    },
    [t]
  );

  /** The dialog's own working state. Says nothing about the pending decision. */
  const resetMergeDialog = () => {
    setMergeOpen(false);
    setMergePreview(null);
    setExcludedIds([]);
    setOverrideBalance(null);
    setOverrideTouched(false);
  };

  /**
   * Cancel DISCARDS, including a decision confirmed on an earlier pass. Every
   * admin reads it that way, and a review left attached after a cancelled
   * second look would ride its `previewEntryIds` along on the join.
   */
  const cancelMergeReview = () => {
    setMergeReview(null);
    resetMergeDialog();
  };

  // Ids are sorted so the request body is order-stable regardless of the order
  // the admin happened to tick the options in.
  const sortedPickedIds = () => [...pickedIds].sort((a, b) => a - b);

  const loadMergePreview = async (nextExcluded) => {
    const seq = (previewSeq.current += 1);
    setMergeLoading(true);
    try {
      const result = await adminService.getPlaceGroupMergePreview(sortedPickedIds(), {
        groupId: action?.payload?.groupId,
        ...(nextExcluded.length ? { exclude: nextExcluded } : {}),
      });
      if (seq !== previewSeq.current) {
        return null;
      }
      const preview = result?.data || null;
      if (!preview) {
        // A 200 with no envelope body. Returning null silently would just stop
        // the button spinning and leave the admin clicking it.
        message.error(
          t('ui.users.place_groups.merge_preview_failed', 'Could not load the merged history')
        );
        return null;
      }
      setMergePreview(preview);
      return preview;
    } catch (error) {
      if (seq === previewSeq.current) {
        message.error(
          placeGroupErrorMessage(
            error,
            t,
            t('ui.users.place_groups.merge_preview_failed', 'Could not load the merged history')
          )
        );
      }
      return null;
    } finally {
      if (seq === previewSeq.current) {
        setMergeLoading(false);
      }
    }
  };

  const startMergeReview = async () => {
    // Re-opening shows the decision already pending on this join, rather than a
    // blank review while a confirmed one still rides along with the request.
    const alreadyExcluded = mergeReview?.excludedLedgerEntryIds || [];
    const alreadyStated = mergeReview?.resultingBalance;
    setExcludedIds(alreadyExcluded);
    setOverrideTouched(alreadyStated != null);
    const preview = await loadMergePreview(alreadyExcluded);
    if (!preview) {
      // A failed preview must not open an empty review: the admin would be
      // deciding against nothing, and confirming would post an empty entry set.
      return;
    }
    setOverrideBalance(alreadyStated != null ? alreadyStated : projectedBalance(preview));
    setMergeOpen(true);
  };

  const toggleExcluded = async (entryId) => {
    const previous = excludedIds;
    const next = excludedIds.includes(entryId)
      ? excludedIds.filter((id) => id !== entryId)
      : [...excludedIds, entryId].sort((a, b) => a - b);
    setExcludedIds(next);
    // Re-derived by the backend rather than recomputed here: the figures the
    // admin decides against must be the ones the committing call will use.
    const preview = await loadMergePreview(next);
    if (!preview) {
      // The figures on screen still describe `previous`, so the tick has to go
      // back with them — otherwise the checkboxes claim an exclusion that
      // nothing the admin can see has accounted for.
      setExcludedIds(previous);
      return;
    }
    if (!overrideTouched) {
      setOverrideBalance(projectedBalance(preview));
    }
  };

  const confirmMergeReview = () => {
    const review = { excludedLedgerEntryIds: excludedIds };
    // Only when the admin actually changed it. Re-stating the previewed figure
    // is arithmetically harmless (delta 0, no correction row) but writes an
    // audit trail claiming a decision that was never made.
    if (overrideBalance != null && Number(overrideBalance) !== projectedBalance(mergePreview)) {
      review.resultingBalance = Number(overrideBalance);
    }
    // ...and `previewEntryIds` ONLY alongside a real decision.
    // `_validate_merge_review` returns early only when there is no review AND
    // the ids are absent (customer_link_service.py:832), so sending them alone
    // arms the staleness comparison while `_apply_merge_review` still writes
    // nothing at all (:1019). That can only REJECT a join whose outcome the
    // entry set does not enter — and re-clicking OK fails forever, because the
    // ids never change. Looking is not deciding.
    if (review.excludedLedgerEntryIds.length || review.resultingBalance != null) {
      review.previewEntryIds = mergePreview?.entry_ids || [];
    }
    setMergeReview(review);
    resetMergeDialog();
  };

  const handleConfirm = () => {
    const trimmedReason = reason.trim();
    if (!action || !trimmedReason) {
      return;
    }
    onConfirm({
      addressIds: sortedPickedIds(),
      label: label.trim() || null,
      reason: trimmedReason,
      bottlesLeaving: numberOrZero(bottlesLeaving),
      merge: mergeReview || {},
    });
  };

  const confirmDisabled =
    !reason.trim() ||
    (action?.kind === 'create' && pickedIds.length < 2) ||
    (action?.kind === 'add' && pickedIds.length < 1);

  const modalTitle = () => {
    if (action?.kind === 'dismiss') {
      return t('ui.users.place_groups.dismiss_title', 'Not the same place?');
    }
    if (action?.kind === 'remove') {
      return t('ui.users.place_groups.remove_title', 'Remove this address from the place group?');
    }
    if (action?.kind === 'add') {
      return t('ui.users.place_groups.add_title', 'Add an address to this place group?');
    }
    return t('ui.users.place_groups.create_title', 'Group these addresses as one place?');
  };

  return (
    <>
      <Modal
        title={modalTitle()}
        open={!!action}
        onOk={handleConfirm}
        onCancel={onCancel}
        confirmLoading={pending}
        okText={t('ui.common.ok', 'OK')}
        cancelText={t('ui.common.cancel', 'Cancel')}
        okButtonProps={{ disabled: confirmDisabled }}
        footer={(_, { OkBtn, CancelBtn }) => (
          <Space>
            {(action?.kind === 'create' || action?.kind === 'add') && (
              <Button
                onClick={startMergeReview}
                loading={mergeLoading}
                // The same address threshold the join itself needs, so the
                // review can never preview a merge that cannot be committed.
                disabled={action?.kind === 'create' ? pickedIds.length < 2 : pickedIds.length < 1}
              >
                {t('ui.users.place_groups.merge_review_action', 'Review bottle history')}
              </Button>
            )}
            <CancelBtn />
            <OkBtn />
          </Space>
        )}
        destroyOnHidden
      >
        <Space direction="vertical" size={10} style={{ width: '100%' }}>
          {(action?.kind === 'create' || action?.kind === 'add') && (
            <Select
              mode="multiple"
              showSearch
              filterOption={false}
              style={{ width: '100%' }}
              value={pickedIds}
              onSearch={runSearch}
              onChange={(ids) => {
                setPickedIds(ids);
                // A review of a different set of addresses is not a review of
                // this one; keeping it would post `previewEntryIds` from a merge
                // that is no longer the merge being committed.
                setMergeReview(null);
              }}
              options={searchOptions}
              notFoundContent={null}
              placeholder={t(
                'ui.users.place_groups.search_placeholder',
                'Search addresses by phone, name or address'
              )}
            />
          )}
          {action?.kind === 'create' && (
            <Input
              value={label}
              onChange={(event) => setLabel(event.target.value)}
              placeholder={t('ui.users.place_groups.label_placeholder', 'Label (e.g. Acme office)')}
              maxLength={100}
            />
          )}
          {action?.kind === 'remove' && (
            <Space direction="vertical" size={2} style={{ width: '100%' }}>
              <Text>
                {t(
                  'ui.users.place_groups.bottles_leaving_label',
                  'Bottles leaving with this address'
                )}
              </Text>
              <InputNumber
                min={0}
                style={{ width: '100%' }}
                value={bottlesLeaving}
                onChange={setBottlesLeaving}
              />
              <Text type="secondary" style={{ fontSize: 12 }}>
                {t(
                  'ui.users.place_groups.bottles_leaving_hint',
                  "Pre-filled from this address's own entries at this place, capped at the place total. The rest stays with the place."
                )}
              </Text>
            </Space>
          )}
          <TextArea
            rows={3}
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            placeholder={t('ui.users.place_groups.reason_placeholder', 'Reason (required)')}
            maxLength={500}
          />
        </Space>
      </Modal>

      {/* Spec 7.4: the merged bottle history, reviewed BEFORE the join commits.
          Every figure here comes from the backend preview — recomputing them
          locally would let the dialog and the committing call disagree. */}
      <Modal
        title={t('ui.users.place_groups.merge_review_title', 'Review the merged bottle history')}
        open={mergeOpen}
        onOk={confirmMergeReview}
        onCancel={cancelMergeReview}
        okText={t('ui.common.ok', 'OK')}
        cancelText={t('ui.common.cancel', 'Cancel')}
        width={720}
        destroyOnHidden
      >
        <Space direction="vertical" size={10} style={{ width: '100%' }}>
          <List
            size="small"
            loading={mergeLoading}
            dataSource={mergePreview?.entries || []}
            locale={{
              emptyText: t('ui.users.place_groups.merge_empty', 'No bottle history to merge'),
            }}
            renderItem={(entry) => (
              <List.Item key={entry.id}>
                <Checkbox
                  // Named, because the row beside it reads as a history line and
                  // says nothing about what ticking the box does.
                  aria-label={t('ui.users.place_groups.merge_exclude', 'Exclude')}
                  checked={excludedIds.includes(entry.id)}
                  onChange={() => toggleExcluded(entry.id)}
                >
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    {entry.occurred_at ? dayjs(entry.occurred_at).format('YYYY-MM-DD HH:mm') : ''} ·{' '}
                    {entry.user_name} · {entry.event_type} · {signedQuantity(entry.quantity)} ·{' '}
                    {entry.preview_balance_after}
                  </Text>
                </Checkbox>
              </List.Item>
            )}
          />

          <Space wrap align="start">
            <Statistic
              title={t('ui.users.place_groups.merge_computed_balance', 'Combined balance')}
              value={numberOrZero(mergePreview?.computed_balance)}
              valueStyle={{ fontSize: 16 }}
            />
            <Statistic
              title={t('ui.users.place_groups.merge_excluded_total', 'Excluded')}
              value={numberOrZero(mergePreview?.excluded_total)}
              valueStyle={{ fontSize: 16 }}
            />
            <Statistic
              title={t('ui.users.place_groups.merge_resulting_balance', 'Resulting balance')}
              value={numberOrZero(mergePreview?.resulting_balance)}
              valueStyle={{ fontSize: 16 }}
            />
            <Statistic
              title={t('ui.users.place_groups.merge_projected_balance', 'Place will hold')}
              value={projectedBalance(mergePreview)}
              valueStyle={{ fontSize: 16 }}
            />
            {/* Only when the two stories disagree. On a clean place this row
                would be a permanent "drift: 0" the admin learns to ignore. */}
            {numberOrZero(mergePreview?.drift) !== 0 && (
              <Statistic
                title={t('ui.users.place_groups.merge_drift', 'Unexplained drift')}
                value={numberOrZero(mergePreview?.drift)}
                valueStyle={{ fontSize: 16 }}
              />
            )}
          </Space>

          {numberOrZero(mergePreview?.drift) !== 0 && (
            <Text type="secondary" style={{ fontSize: 12 }}>
              {/* Conditional on purpose. With no exclusion and no override the
                  join writes NOTHING (_apply_merge_review returns at :1019) and
                  the difference survives — the copy must not promise a repair
                  the likeliest path does not perform. */}
              {t(
                'ui.users.place_groups.merge_drift_hint',
                'These places hold more (or fewer) bottles than their history explains, which is why the place will hold the figure above rather than the combined history total. Excluding an entry or setting the resulting balance writes that difference into the ledger so both figures agree; joining without a change leaves it in place.'
              )}
            </Text>
          )}

          <Space direction="vertical" size={2} style={{ width: '100%' }}>
            <Text>
              {t(
                'ui.users.place_groups.merge_override_label',
                'Set the resulting balance instead'
              )}
            </Text>
            <InputNumber
              style={{ width: '100%' }}
              value={overrideBalance}
              onChange={(value) => {
                setOverrideBalance(value);
                setOverrideTouched(true);
              }}
            />
          </Space>
        </Space>
      </Modal>
    </>
  );
}
