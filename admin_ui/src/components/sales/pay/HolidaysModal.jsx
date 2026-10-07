import React, { useState } from 'react';
import { Checkbox, Input, Modal, Space } from 'antd';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import salesPayService from '../../../services/salesPayService';
import { PayErrorAlert, isHandledPayError } from './payErrors';
import { dateLabel, monthLabel } from './payFormat';

/**
 * A7 (§6.2): one checkbox per published working day, plus the month's current holidays so they
 * can be unticked, each with a note. It saves the full list; the UI does no date arithmetic.
 */
const HolidaysModal = ({ detail, onClose }) => {
  const { t } = useTranslation(['sales_agents', 'common']);
  const queryClient = useQueryClient();
  const notes = new Map(detail.holidays.map((day) => [day.date, day.note || '']));
  const candidates = detail.calendar.days.filter((day) => day.is_working_day || notes.has(day.date));
  const [picked, setPicked] = useState(() => new Map(
    candidates.map((day) => [day.date, { checked: notes.has(day.date), note: notes.get(day.date) || '' }]),
  ));
  const [refusal, setRefusal] = useState(null);
  const mutation = useMutation({
    mutationFn: () => salesPayService.setHolidays(
      detail.month,
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
      title={t('sales_agents:pay.holidays.title', { defaultValue: 'Holidays in {{month}}', month: monthLabel(detail.month) })}
      okText={t('sales_agents:pay.form.save', 'Save')}
      confirmLoading={mutation.isPending}
      onOk={() => { setRefusal(null); mutation.mutate(); }}
      onCancel={onClose}
    >
      <Space direction="vertical" style={{ width: '100%' }}>
        <PayErrorAlert error={refusal} />
        {candidates.map((day) => (
          <Space key={day.date} style={{ width: '100%' }}>
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

export default HolidaysModal;
