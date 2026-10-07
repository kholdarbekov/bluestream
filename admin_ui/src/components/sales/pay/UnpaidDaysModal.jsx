import React, { useState } from 'react';
import { Checkbox, Input, Modal, Space } from 'antd';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import salesPayService from '../../../services/salesPayService';
import { PayErrorAlert, isHandledPayError } from './payErrors';
import { dateLabel, monthLabel } from './payFormat';

// The published day statuses an admin can move between (§6.2 item 3): a worked day can be made
// unpaid, an unpaid one worked again. The list is the statement's own `days`.
const TOGGLABLE = ['worked', 'unpaid'];

/** A10: the agent's unpaid days for the month, replaced as a whole. */
const UnpaidDaysModal = ({ statement, agentId, onClose }) => {
  const { t } = useTranslation(['sales_agents', 'common']);
  const queryClient = useQueryClient();
  const notes = new Map(statement.inputs.unpaid_days.map((day) => [day.date, day.note || '']));
  const candidates = statement.days.filter((day) => TOGGLABLE.includes(day.day_status));
  const [picked, setPicked] = useState(() => new Map(
    candidates.map((day) => [day.date, { checked: day.day_status === 'unpaid', note: notes.get(day.date) || '' }]),
  ));
  const [refusal, setRefusal] = useState(null);
  const mutation = useMutation({
    mutationFn: () => salesPayService.setUnpaidDays(
      statement.month,
      agentId,
      candidates
        .filter((day) => picked.get(day.date).checked)
        .map((day) => ({ date: day.date, note: picked.get(day.date).note.trim() || null })),
    ),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['salesPay'] });
      onClose();
    },
    onError: (error) => setRefusal(isHandledPayError(error) ? error : null),
  });
  const update = (date, patch) => setPicked((current) => new Map(current).set(date, { ...current.get(date), ...patch }));

  return (
    <Modal
      open
      title={t('sales_agents:pay.days.unpaid_title', { defaultValue: 'Unpaid days in {{month}}', month: monthLabel(statement.month) })}
      okText={t('sales_agents:pay.form.save', 'Save')}
      confirmLoading={mutation.isPending}
      onOk={() => { setRefusal(null); mutation.mutate(); }}
      onCancel={onClose}
    >
      <Space direction="vertical" style={{ width: '100%' }}>
        {/* A closed-month re-freeze can answer TERMS_MISSING (`details.agents`); name the agent
            from the statement already on screen, never the raw id. */}
        <PayErrorAlert error={refusal} agentNames={new Map([[agentId, statement.agent.name]])} />
        {candidates.map((day) => (
          <Space key={day.date}>
            <Checkbox checked={picked.get(day.date).checked} onChange={(event) => update(day.date, { checked: event.target.checked })}>
              {dateLabel(day.date)}
            </Checkbox>
            <Input
              size="small"
              maxLength={100}
              aria-label={`${t('sales_agents:pay.form.note', 'Note')} ${dateLabel(day.date)}`}
              value={picked.get(day.date).note}
              disabled={!picked.get(day.date).checked}
              onChange={(event) => update(day.date, { note: event.target.value })}
            />
          </Space>
        ))}
      </Space>
    </Modal>
  );
};

export default UnpaidDaysModal;
