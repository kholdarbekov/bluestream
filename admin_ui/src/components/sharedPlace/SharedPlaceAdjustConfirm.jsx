import React, { useState } from 'react';
import { Alert, Form, Input, InputNumber, Modal, Select, Typography } from 'antd';
import { useTranslation } from 'react-i18next';

const { Text } = Typography;
const { TextArea } = Input;

/**
 * The move source the driver pointed at: the picked candidate's place, when it
 * is one of `move_sources` now. Matched by place identity, never by name.
 */
const pickedSourceId = (detail) => {
  if (detail.answer?.kind !== 'picked') return undefined;
  const picked = (detail.candidates || []).find((c) => c.index === detail.answer.picked_index);
  if (!picked?.live?.exists) return undefined;
  const { live } = picked;
  const match = (detail.move_sources || []).find(
    (source) =>
      (live.place_group_id != null && source.place_group_id === live.place_group_id) ||
      (live.members || []).some((member) => member.address_id === source.address_id)
  );
  return match?.address_id;
};

/**
 * The case-owned confirmation for every choice that resolves without one of the
 * existing dialogs: the three bottle adjustments (C "other_place", D
 * "own_bottles", E "wrong_count"), Close, and the A/B confirmations once the
 * grouping is already done. Spec §4.5: each one shows what will happen, the
 * fields of that choice and the mandatory reason.
 *
 * Mounted only while open, so every open starts from the payload's figures.
 * The resolve payload is built from the VALIDATED values in `onFinish`, never
 * from `form.getFieldsValue()`.
 *
 * While a claim of this kind is stored, the fields are pre-filled from
 * `claim.payload` and locked, so a retry repeats it exactly (any other payload
 * is a SHARED_PLACE_RESOLUTION_CONFLICT).
 *
 * Every number comes from the case detail: nothing here re-derives a rule.
 */
export default function SharedPlaceAdjustConfirm({
  mode,
  title,
  detail,
  groupLabel = null,
  targetUserId,
  defaultReason = '',
  pending = false,
  onSubmit,
  onCancel,
}) {
  const { t } = useTranslation('users');
  const [form] = Form.useForm();

  const claimed = detail.claim?.kind === mode ? detail.claim.payload : null;
  const sources = detail.move_sources || [];
  const isMove = mode === 'other_place';
  const moveAllowed = Boolean(detail.choices?.move_allowed);
  const claimedSourceId = claimed?.source_address_id;
  // A claimed source that holds nothing now is no longer listed; it stays choosable so the retry repeats.
  const sourceOptions = [
    ...sources.map((source) => ({
      value: source.address_id,
      label: t('ui.users.shared_place_reports.move_source_option', '{{source}} · {{k}} empties', {
        source: source.label || source.owner_first_name || t('ui.users.shared_place_reports.place_number', 'Place #{{id}}', { id: source.address_id }),
        k: source.empties,
      }),
    })),
    ...(claimedSourceId != null && !sources.some((source) => source.address_id === claimedSourceId)
      ? [{ value: claimedSourceId, label: t('ui.users.shared_place_reports.place_number', 'Place #{{id}}', { id: claimedSourceId }) }]
      : []),
  ];
  const moveFieldsShown = isMove && moveAllowed && sourceOptions.length > 0;

  const initialValues = {
    reason: defaultReason,
    ...(isMove ? { source_address_id: claimedSourceId ?? pickedSourceId(detail), move_count: claimed?.move_count ?? null } : {}),
    ...(mode === 'wrong_count' ? { actual_returned: claimed?.actual_returned ?? null } : {}),
  };

  // Mirrors the Select (Form.Item still owns its value) so the facts line follows the chosen source.
  const [chosenSourceId, setChosenSourceId] = useState(initialValues.source_address_id);
  const chosenSource = sources.find((source) => source.address_id === chosenSourceId);

  const handleFinish = (values) => {
    const payload = { kind: mode };
    if (mode === 'same_home' || isMove) {
      payload.target_user_id = targetUserId;
    }
    if (mode === 'wrong_count') {
      payload.actual_returned = values.actual_returned;
    }
    // Empty means link only: the count and its source travel together or not at all.
    if (moveFieldsShown && values.move_count != null) {
      payload.move_count = values.move_count;
      payload.source_address_id = values.source_address_id;
    }
    payload.reason = values.reason.trim();
    onSubmit(payload);
  };

  const atPlace = (hereKey, hereFallback, placeKey, placeFallback, vars) =>
    groupLabel != null ? t(placeKey, placeFallback, { ...vars, label: groupLabel }) : t(hereKey, hereFallback, vars);

  const body = () => {
    if (mode === 'own_bottles') {
      return (
        <Text>
          {atPlace(
            'ui.users.shared_place_reports.own_bottles_here',
            '+{{n}} at this address',
            'ui.users.shared_place_reports.own_bottles_place',
            "+{{n}} at the place '{{label}}'",
            { n: detail.returned_qty }
          )}
        </Text>
      );
    }
    if (mode === 'wrong_count') {
      // The server books `returned_qty - actual_returned` at the place: the number asked for is the empties
      // actually handed back, never the place's balance.
      return (
        <Form.Item
          name="actual_returned"
          label={t(
            'ui.users.shared_place_reports.wrong_count_question',
            'The driver typed {{typed}} empties handed back. How many were actually handed back?',
            { typed: detail.returned_qty }
          )}
          extra={atPlace(
            'ui.users.shared_place_reports.wrong_count_booked_here',
            'The difference is booked at this address.',
            'ui.users.shared_place_reports.wrong_count_booked_place',
            "The difference is booked at the place '{{label}}'.",
            {}
          )}
          rules={[
            {
              required: true,
              message: t('ui.users.shared_place_reports.actual_returned_required', 'Enter the number of empties actually handed back'),
            },
            {
              // A rule, not `min`: a clamp would turn a typed -1 into a real 0 and book it.
              validator(_, value) {
                // A stored claim is repeated as it is: the server never re-checks it.
                if (value == null || claimed != null) return Promise.resolve();
                if (value < 0) {
                  return Promise.reject(new Error(t('ui.users.shared_place_reports.actual_returned_min', 'Enter 0 or more')));
                }
                if (value === detail.returned_qty) {
                  return Promise.reject(new Error(t(
                    'ui.users.shared_place_reports.actual_returned_same',
                    'That is the number the driver typed. Enter the number actually handed back.'
                  )));
                }
                return Promise.resolve();
              },
            },
          ]}
        >
          <InputNumber precision={0} style={{ width: '100%' }} disabled={claimed != null} />
        </Form.Item>
      );
    }
    if (mode === 'close') {
      return <Text>{t('ui.users.shared_place_reports.close_text', 'The case is closed and nothing else changes.')}</Text>;
    }
    if (mode === 'shared_place' || mode === 'same_home') {
      return <Text>{t('ui.users.shared_place_reports.grouped_text', 'The grouping is already done; this records the decision.')}</Text>;
    }
    // C: the facts next to the count, then the source and the count (empty = link only).
    return (
      <>
        <Text>
          {chosenSource
            ? t(
              'ui.users.shared_place_reports.move_facts',
              'handed back {{returned}} today; this address now shows {{now}}; {{source}} shows {{k}}',
              {
                returned: detail.returned_qty,
                now: detail.address.place_empties_now,
                source: chosenSource.label || chosenSource.owner_first_name || t('ui.users.shared_place_reports.place_number', 'Place #{{id}}', { id: chosenSource.address_id }),
                k: chosenSource.empties,
              }
            )
            : t(
              'ui.users.shared_place_reports.move_facts_here',
              'handed back {{returned}} today; this address now shows {{now}}',
              { returned: detail.returned_qty, now: detail.address.place_empties_now }
            )}
        </Text>
        {!moveAllowed && (
          <Text type="secondary">
            {t('ui.users.shared_place_reports.move_not_allowed', 'This address is already in a place group, so no empties can move here.')}
          </Text>
        )}
        {moveAllowed && !moveFieldsShown && (
          <Text type="secondary">
            {t('ui.users.shared_place_reports.move_none', 'No other place of the linked accounts holds empties, so nothing can move.')}
          </Text>
        )}
        {moveFieldsShown && (
          <>
            <Form.Item
              name="source_address_id"
              label={t('ui.users.shared_place_reports.move_source', 'Move empties from')}
              dependencies={['move_count']}
              rules={[({ getFieldValue }) => ({
                validator(_, value) {
                  if (getFieldValue('move_count') != null && value == null) {
                    return Promise.reject(new Error(t('ui.users.shared_place_reports.move_source_required', 'Choose the place the empties move from')));
                  }
                  return Promise.resolve();
                },
              })]}
            >
              <Select options={sourceOptions} allowClear disabled={claimed != null} onChange={setChosenSourceId} />
            </Form.Item>
            <Form.Item
              name="move_count"
              dependencies={['source_address_id']}
              rules={[({ getFieldValue }) => ({
                validator(_, value) {
                  // A stored claim is repeated as it is: the server never re-checks it.
                  if (value == null || claimed != null) return Promise.resolve();
                  const source = sources.find((s) => s.address_id === getFieldValue('source_address_id'));
                  if (source && (value < 1 || value > source.empties)) {
                    return Promise.reject(new Error(t('ui.users.shared_place_reports.move_count_range', 'Enter a number from 1 to {{k}}', { k: source.empties })));
                  }
                  return Promise.resolve();
                },
              })]}
            >
              <InputNumber
                precision={0}
                style={{ width: '100%' }}
                disabled={claimed != null}
                placeholder={t('ui.users.shared_place_reports.move_count_placeholder', 'Empties to move (leave empty to link only)')}
              />
            </Form.Item>
          </>
        )}
      </>
    );
  };

  return (
    <Modal
      open
      title={title}
      onOk={() => form.submit()}
      onCancel={onCancel}
      confirmLoading={pending}
      okText={t('ui.common.confirm', 'Confirm')}
      cancelText={t('ui.common.cancel', 'Cancel')}
      destroyOnHidden
    >
      <Form form={form} layout="vertical" initialValues={initialValues} onFinish={handleFinish}>
        {claimed && (
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 12 }}
            message={t('ui.users.shared_place_reports.claim_hint', 'This outcome was started earlier. Confirm it again to finish it.')}
          />
        )}
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8, marginBottom: 12 }}>{body()}</div>
        <Form.Item
          name="reason"
          rules={[{
            required: true,
            whitespace: true,
            message: t('ui.users.shared_place_reports.reason_required', 'Enter a reason'),
          }]}
        >
          <TextArea
            rows={3}
            maxLength={500}
            placeholder={t('ui.users.shared_place_reports.reason_placeholder', 'Reason (required)')}
          />
        </Form.Item>
      </Form>
    </Modal>
  );
}
