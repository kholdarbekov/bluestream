import { useMemo } from 'react';
import { Alert, Button, Form, Input, Modal, Space, Spin, message } from 'antd';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import dayjs from 'dayjs';
import customParseFormat from 'dayjs/plugin/customParseFormat';

import adminService from '../../services/adminService';
import { extractApiErrorMessage } from '../../utils/apiError';
import AsyncButton from '../common/AsyncButton';
import DeliverySchedulePicker from './DeliverySchedulePicker';
import { buildSchedulePayload, presetFromWindow } from './deliverySchedule';

dayjs.extend(customParseFormat);

const WINDOW_FORMAT = 'HH:mm';
// The reason lands in `DeliveryStatusHistory.reason` (R9), a String(100). The service refuses a
// longer one with ORDER_RESCHEDULE_REASON_TOO_LONG and never truncates it (R24), so the field
// never takes more.
const REASON_MAX_LENGTH = 100;

// Every cached screen a reschedule makes wrong. It moves the order's date, can unassign the
// driver and drop the stop from a route, and changes the delivery's status.
const INVALIDATED_QUERY_KEYS = [
  ['orders'],
  ['dispatchSnapshot'],
  ['dispatchSnapshotOverlay'],
  ['dispatchRouteGeometry'],
  ['deliveries'],
  ['dashboard'],
];

/**
 * Fence code -> [translation key, English fallback].
 *
 * PATCH /admin/orders/<id>/schedule returns these in `data.error_code`, and the detail read
 * publishes the first two as `reschedule_block_code`. The response's `errors[0]` is the service's
 * English sentence. The modal names every code here in the request's `handledErrorCodes`, so the
 * api.js interceptor does not toast it, and shows the admin's own language inline instead: one
 * refusal, one message. A Map for the same reason as placeGroupCopy.js: the lookup key comes off
 * the wire.
 */
export const RESCHEDULE_ERROR_MESSAGES = new Map([
  [
    'ORDER_NOT_RESCHEDULABLE',
    [
      'ui.orders.reschedule_error.ORDER_NOT_RESCHEDULABLE',
      'This order is delivered, cancelled or returned, so it can no longer be rescheduled.',
    ],
  ],
  [
    'DELIVERY_NOT_RESCHEDULABLE',
    [
      'ui.orders.reschedule_error.DELIVERY_NOT_RESCHEDULABLE',
      "This order's delivery is already delivered, cancelled or returned, so it can't be rescheduled.",
    ],
  ],
  [
    // Unreachable from this modal, which always sends a date. Mapped so a caller that ever
    // does not gets translated copy instead of the raw English.
    'DELIVERY_DATE_REQUIRED',
    [
      'ui.orders.reschedule_error.DELIVERY_DATE_REQUIRED',
      'Pick a delivery date: this order has already been released to drivers.',
    ],
  ],
  [
    'ORDER_RESCHEDULE_PAST_CONTRACT_END',
    [
      'ui.orders.reschedule_error.ORDER_RESCHEDULE_PAST_CONTRACT_END',
      "The customer's contract ends before that date. Pick an earlier date.",
    ],
  ],
  [
    // R24. Unreachable from this modal: the reason field stops at REASON_MAX_LENGTH and the
    // payload is trimmed. Mapped so the server's English sentence never reaches the admin.
    'ORDER_RESCHEDULE_REASON_TOO_LONG',
    [
      'ui.orders.reschedule_error.ORDER_RESCHEDULE_REASON_TOO_LONG',
      'The reason can be at most 100 characters.',
    ],
  ],
]);

/** Translated copy for a known fence code, else null. */
export const rescheduleErrorText = (code, t) => {
  const known = code ? RESCHEDULE_ERROR_MESSAGES.get(code) : null;
  return known ? t(known[0], known[1]) : null;
};

// The day the form opens on.
// - An overdue order opens on the first day the backend allows, so a past date can never be
//   submitted (Review Focus 2).
// - An undated order, or one dated past the upper bound, opens EMPTY. Seeding a day there would
//   turn an untouched Save into a real reschedule that unassigns the driver (Review Focus 1).
const seedDate = (order) => {
  if (!order.delivery_date) return null;
  const current = dayjs(order.delivery_date);
  const min = dayjs(order.reschedule_min_date);
  if (current.isBefore(min, 'day')) return min;
  if (current.isAfter(dayjs(order.reschedule_max_date), 'day')) return null;
  return current;
};

const initialValuesFor = (order) => {
  const { preset, start, end } = presetFromWindow(order.delivery_window);
  return {
    delivery_date: seedDate(order),
    window_preset: preset,
    // Seeded whatever the preset, so switching to Custom starts from the stored edges.
    window_start: start ? dayjs(start, WINDOW_FORMAT) : null,
    window_end: end ? dayjs(end, WINDOW_FORMAT) : null,
    reason: '',
  };
};

// The schedule the form describes, in the PATCH body's shape; null until a day is picked.
// `delivery_date` is always a key: the endpoint reads an absent key as a caller bug, never as
// "leave the date alone".
const scheduleFromValues = (values) => {
  if (!values?.delivery_date) return null;
  return buildSchedulePayload({
    preset: values.window_preset,
    date: values.delivery_date.format('YYYY-MM-DD'),
    start: values.window_start ? values.window_start.format(WINDOW_FORMAT) : null,
    end: values.window_end ? values.window_end.format(WINDOW_FORMAT) : null,
  });
};

const sameSchedule = (order, schedule) =>
  schedule.delivery_date === (order.delivery_date ?? null)
  && schedule.delivery_window_start === (order.delivery_window?.start ?? null)
  && schedule.delivery_window_end === (order.delivery_window?.end ?? null);

// What the customer will hear, exactly as the backend decided it (R13/R14). The modal never
// promises a notice the backend has no channel for (Review Focus 5).
const customerNotice = (order, t) => {
  if (!order.reschedule_notifies_customer) return null;
  if (order.reschedule_customer_channel === 'telegram') {
    return {
      type: 'info',
      text: t('ui.orders.reschedule_notifies_customer_telegram', 'The customer will be notified of the new date in Telegram.'),
    };
  }
  if (order.reschedule_customer_channel === 'email') {
    return {
      type: 'info',
      text: t('ui.orders.reschedule_notifies_customer_email', 'The customer will be notified of the new date by email.'),
    };
  }
  return {
    type: 'warning',
    text: t('ui.orders.reschedule_customer_unreachable', "The customer can't be notified automatically — tell them yourself."),
  };
};

const RescheduleForm = ({ order, onClose, onRescheduled }) => {
  const { t } = useTranslation('orders');
  const queryClient = useQueryClient();
  const [form] = Form.useForm();
  const initialValues = useMemo(() => initialValuesFor(order), [order]);

  const schedule = scheduleFromValues({
    delivery_date: Form.useWatch('delivery_date', form),
    window_preset: Form.useWatch('window_preset', form),
    window_start: Form.useWatch('window_start', form),
    window_end: Form.useWatch('window_end', form),
  });
  const canSave = Boolean(schedule) && !sameSchedule(order, schedule);

  const mutation = useMutation({
    mutationFn: (payload) => adminService.rescheduleOrder(order.id, payload, {
      handledErrorCodes: [...RESCHEDULE_ERROR_MESSAGES.keys()],
    }),
    onSuccess: (response) => {
      message.success(t('ui.orders.reschedule_success', 'Delivery rescheduled'));
      INVALIDATED_QUERY_KEYS.forEach((queryKey) => queryClient.invalidateQueries({ queryKey }));
      onRescheduled?.(response?.data?.order);
      onClose();
    },
  });

  // Never message.error. A mapped code was named in handledErrorCodes, so api.js stayed silent
  // and this inline copy is its one message. Anything else api.js has toasted; the server's
  // sentence stays beside the form so the admin can act on it. Read off the mutation rather than
  // copied into state from onError: React Query runs onError before it leaves `isPending`, so a
  // copy rendered beside a Save that was still spinning. A new submit clears it.
  const errorText = mutation.error
    ? rescheduleErrorText(mutation.error.response?.data?.data?.error_code, t)
      || extractApiErrorMessage(mutation.error, t('ui.common.error_occurred', 'An error occurred'))
    : null;

  const submit = (values) => {
    const payload = scheduleFromValues(values);
    // The disabled Save is not the only way to submit a form (Enter in a field). An unchanged
    // schedule is never sent: it would still unassign the driver and could message the customer.
    if (!payload || sameSchedule(order, payload)) return;
    const reason = (values.reason || '').trim();
    mutation.mutate(reason ? { ...payload, reason } : payload);
  };

  const notice = customerNotice(order, t);
  const driver = order.reschedule_driver_losing_stop;

  return (
    <Form form={form} layout="vertical" initialValues={initialValues} onFinish={submit}>
      {notice ? <Alert type={notice.type} showIcon message={notice.text} style={{ marginBottom: 12 }} /> : null}
      {driver ? (
        <Alert
          type="warning"
          showIcon
          message={t('ui.orders.reschedule_driver_loses_stop', 'Driver {{name}} will lose this stop.', { name: driver.name })}
          style={{ marginBottom: 12 }}
        />
      ) : null}
      {errorText ? <Alert type="error" showIcon message={errorText} style={{ marginBottom: 12 }} /> : null}

      <DeliverySchedulePicker
        minDate={order.reschedule_min_date}
        maxDate={order.reschedule_max_date}
        allowClear={false}
      />

      <Form.Item name="reason" label={t('ui.orders.reschedule_reason', 'Reason (optional)')}>
        <Input.TextArea
          rows={2}
          maxLength={REASON_MAX_LENGTH}
          showCount
          placeholder={t('ui.orders.reschedule_reason_placeholder', 'Staff only — never shown to the customer')}
        />
      </Form.Item>

      <Form.Item style={{ marginBottom: 0, textAlign: 'right' }}>
        <Space>
          <Button onClick={onClose}>{t('ui.common.cancel', 'Cancel')}</Button>
          <AsyncButton type="primary" htmlType="submit" disabled={!canSave} loading={mutation.isPending}>
            {t('ui.common.save', 'Save')}
          </AsyncButton>
        </Space>
      </Form.Item>
    </Form>
  );
};

const RescheduleBody = ({ orderId, onClose, onRescheduled }) => {
  const { t } = useTranslation('orders');
  // Read fresh on every open, never from a cache. The banners and the bounds must describe the
  // delivery as it is now: a driver may have picked it up since the list loaded.
  const detail = useQuery({
    queryKey: ['order-reschedule', orderId],
    queryFn: () => adminService.getOrderDetails(orderId),
    staleTime: 0,
    gcTime: 0,
    // Read once per open. antd applies `initialValues` only at mount, so a refetch would swap
    // `order` under the old form values, and an untouched Save would revert whoever changed it.
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  });

  if (!detail.isFetchedAfterMount) {
    return <div style={{ textAlign: 'center', padding: 24 }}><Spin /></div>;
  }
  const order = detail.data?.data?.order;
  if (detail.isError || !order) {
    return (
      <Alert
        type="error"
        showIcon
        message={extractApiErrorMessage(detail.error, t('ui.common.error_occurred', 'An error occurred'))}
      />
    );
  }
  if (!order.can_reschedule) {
    return (
      <Alert
        type="error"
        showIcon
        message={rescheduleErrorText(order.reschedule_block_code, t) || t('ui.common.error_occurred', 'An error occurred')}
      />
    );
  }
  // Keyed on the read: a refetch nothing here asks for (an invalidation elsewhere) remounts the
  // form, seeded from the same order it is compared against.
  return <RescheduleForm key={detail.dataUpdatedAt} order={order} onClose={onClose} onRescheduled={onRescheduled} />;
};

/**
 * Move a not-yet-delivered order to another day and window (spec §6, R1–R17). Opened from the
 * Orders row action and the detail footer, both gated on the published `can_reschedule`.
 *
 * The body renders only while `open`. It unmounts on close, and with `gcTime: 0` its detail
 * query goes with it, so the next open can never seed the form from an older read.
 */
const RescheduleOrderModal = ({ orderId, open, onClose, onRescheduled }) => {
  const { t } = useTranslation('orders');
  return (
    <Modal
      title={t('ui.orders.reschedule_title', 'Reschedule delivery')}
      open={open}
      onCancel={onClose}
      footer={null}
      width={560}
    >
      {open && orderId != null ? (
        <RescheduleBody orderId={orderId} onClose={onClose} onRescheduled={onRescheduled} />
      ) : null}
    </Modal>
  );
};

export default RescheduleOrderModal;
