import React, { useState } from 'react';
import { Alert, Button, Descriptions, Input, InputNumber, Modal, Space, Tooltip, Typography } from 'antd';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';

import driverExpenseService from '../../services/driverExpenseService';
import { apiErrorCode } from '../../utils/apiError';
import { formatDateTime } from '../../utils/dateUtils';
import { formatMoney } from '../../utils/formatMoney';
import ExpenseReceiptThumb from './ExpenseReceiptThumb';
import { blockLabel, categoryLabel, tripStatusLabel } from './expenseCopy';

const { Text } = Typography;

// The refusals this dialog explains itself (`DRIVER_EXPENSE_HANDLED_CODES`): the code a decision was refused with, as
// the `block.<code>` it maps to. Anything else api.js already toasted.
const refusalBlockCode = (error) => {
  const code = apiErrorCode(error);
  if (code === 'DRIVER_EXPENSE_DECISION_BLOCKED') return error.response.data?.details?.block_code ?? null;
  if (code === 'DRIVER_EXPENSE_SELF_DECISION') return 'self_decision';
  return null;
};

const Warnings = ({ warnings }) => {
  const { t } = useTranslation();
  const duplicates = warnings?.possible_duplicates || [];
  const trips = warnings?.recent_closed_trips || [];
  return (
    <>
      {duplicates.length > 0 && (
        <Alert
          type="warning"
          showIcon
          message={t('ui.driver_expenses.warning_duplicate', 'Same driver, same amount, same day: {{ids}}', {
            ids: duplicates.map((other) => `#${other.id}`).join(', '),
          })}
        />
      )}
      {trips.length > 0 && (
        <Alert
          type="info"
          showIcon
          message={t('ui.driver_expenses.warning_recent_trips', "The driver's last closed trips")}
          description={trips.map((trip) => {
            // A trip not counted yet has no variance: its published 0 is not a clean check, so it is not drawn.
            const parts = [`#${trip.session_id}`, formatDateTime(trip.session_ended_at), tripStatusLabel(t, trip.status)];
            if (trip.status !== 'submitted') parts.push(formatMoney(trip.verified_variance));
            return <div key={trip.session_id}>{parts.join(' · ')}</div>;
          })}
        />
      )}
    </>
  );
};

// The preview line the backend computes for the typed amount: what the approval does to the trip.
const EffectLine = ({ effect }) => {
  const { t } = useTranslation();
  const line = t(
    'ui.driver_expenses.effect',
    'Trip #{{session}}: on hand {{before}} → {{after}} · Hand in now {{handBefore}} → {{handAfter}}',
    {
      session: effect.session_id,
      before: formatMoney(effect.on_hand_before),
      after: formatMoney(effect.on_hand_after),
      handBefore: formatMoney(effect.hand_in_now_before),
      handAfter: formatMoney(effect.hand_in_now_after),
    },
  );
  return (
    <Text>
      {line}
      {effect.will_close && ` · ${t('ui.driver_expenses.effect_closes', 'the trip closes')}`}
    </Text>
  );
};

// One dialog for one claim: the claim, and Approve / Reject / Undo drawn from the flags the backend published on it.
// The page derives nothing: the buttons come from `can_*`, the amount limit from `approve_max_amount`, a disabled
// button's tooltip from the block code, the reason length from `reasonMinLength`.
const DecideExpenseModal = ({ detail, reasonMinLength, onClose }) => {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { expense, warnings } = detail;
  const [amount, setAmount] = useState(expense.amount);
  const [reason, setReason] = useState('');
  const [refusal, setRefusal] = useState(null);

  const previewQuery = useQuery({
    queryKey: ['driverExpenses', 'preview', expense.id, amount],
    queryFn: () => driverExpenseService.preview(expense.id, amount),
    enabled: expense.can_approve && amount > 0,
  });
  const preview = previewQuery.data;
  const previewBlock = preview?.blocking_codes?.[0] ?? null;

  const decision = useMutation({
    mutationFn: ({ send }) => send(),
    onSuccess: onClose,
    onError: (error) => setRefusal(refusalBlockCode(error)),
    // A refused decision also means the row was stale: reread it, the badge and the list.
    onSettled: () => queryClient.invalidateQueries({ queryKey: ['driverExpenses'] }),
  });

  const reasonReady = reason.trim().length >= reasonMinLength;
  const action = (key, label, { type, enabled, blockCode, send }) => (
    // A disabled button emits no mouse events, so the tooltip hangs on a span around it.
    <Tooltip key={key} title={enabled || !blockCode ? null : blockLabel(t, blockCode)}>
      <span>
        <Button
          type={type}
          danger={key === 'reject'}
          disabled={!enabled}
          loading={decision.isPending && decision.variables?.key === key}
          onClick={() => { setRefusal(null); decision.mutate({ key, send }); }}
        >
          {label}
        </Button>
      </span>
    </Tooltip>
  );

  const isPending = expense.status === 'pending';
  const buttons = [];
  if (isPending) {
    buttons.push(action('approve', t('ui.driver_expenses.approve', 'Approve'), {
      type: 'primary',
      enabled: expense.can_approve && amount > 0 && !decision.isPending && !previewBlock,
      blockCode: expense.approve_block_code,
      send: () => driverExpenseService.approve(expense.id, amount),
    }));
    buttons.push(action('reject', t('ui.driver_expenses.reject', 'Reject'), {
      enabled: expense.can_reject && reasonReady && !decision.isPending,
      blockCode: expense.decide_block_code,
      send: () => driverExpenseService.reject(expense.id, reason.trim()),
    }));
  }
  if (expense.can_undo || expense.status === 'approved') {
    buttons.push(action('undo', t('ui.driver_expenses.undo', 'Undo approval'), {
      enabled: expense.can_undo && reasonReady && !decision.isPending,
      blockCode: expense.undo_block_code,
      send: () => driverExpenseService.undo(expense.id, reason.trim()),
    }));
  }

  const location = expense.latitude != null && expense.longitude != null
    ? `${expense.latitude}, ${expense.longitude}`
    : t('ui.driver_expenses.no_location', 'No location');

  return (
    <Modal
      open
      onCancel={onClose}
      title={`#${expense.id} · ${expense.driver_name} · ${categoryLabel(t, expense.category)}`}
      footer={<Space>{buttons}</Space>}
      width={640}
    >
      <Space direction="vertical" size="middle" style={{ width: '100%' }}>
        <Descriptions size="small" column={1} bordered>
          <Descriptions.Item label={t('ui.driver_expenses.col_claimed', 'Claimed')}>{formatMoney(expense.amount)}</Descriptions.Item>
          <Descriptions.Item label={t('ui.driver_expenses.col_recorded', 'Recorded')}>{formatDateTime(expense.submitted_at)}</Descriptions.Item>
          <Descriptions.Item label={t('ui.driver_expenses.col_note', 'Note')}>{expense.note || '—'}</Descriptions.Item>
          <Descriptions.Item label={t('ui.driver_expenses.location', 'Location')}>{location}</Descriptions.Item>
        </Descriptions>
        {expense.has_receipt && <ExpenseReceiptThumb expenseId={expense.id} />}
        <Warnings warnings={warnings} />
        {isPending && (
          <Space direction="vertical" size={4} style={{ width: '100%' }}>
            <Text strong>{t('ui.driver_expenses.amount_to_approve', 'Amount to approve')}</Text>
            <InputNumber
              aria-label={t('ui.driver_expenses.amount_to_approve', 'Amount to approve')}
              min={0}
              max={expense.approve_max_amount}
              value={amount}
              onChange={setAmount}
              style={{ width: 200 }}
            />
            <Text type="secondary">
              {t('ui.driver_expenses.max_hint', 'At most {{amount}}', { amount: formatMoney(expense.approve_max_amount) })}
            </Text>
            {preview?.effect && <EffectLine effect={preview.effect} />}
            {previewBlock && <Alert type="error" showIcon message={blockLabel(t, previewBlock)} />}
          </Space>
        )}
        {(isPending || expense.status === 'approved') && (
          <Space direction="vertical" size={4} style={{ width: '100%' }}>
            <Text strong>{t('ui.driver_expenses.reason', 'Reason')}</Text>
            <Input.TextArea
              aria-label={t('ui.driver_expenses.reason', 'Reason')}
              rows={2}
              value={reason}
              onChange={(event) => setReason(event.target.value)}
            />
            <Text type="secondary">
              {t('ui.driver_expenses.reason_min', 'At least {{min}} characters', { min: reasonMinLength })}
            </Text>
          </Space>
        )}
        {refusal && <Alert type="error" showIcon message={blockLabel(t, refusal)} />}
      </Space>
    </Modal>
  );
};

export default DecideExpenseModal;
