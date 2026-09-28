import { Alert, Button, Form, Input, Modal, Space } from 'antd';
import { useTranslation } from 'react-i18next';

import AsyncButton from '../common/AsyncButton';

// The reason lands in OrderStatusHistory.reason, a String(100). The backend refuses a longer one
// with ADMIN_REASON_TOO_LONG and never truncates it (F6), so the field never takes more.
const ADMIN_REASON_MAX_LENGTH = 100;

/**
 * The backend's refusals of a missing or oversized admin reason (F6): `data.error_code` ->
 * [translation key, English fallback]. The Orders page's status change and the Delivery page's
 * Returned both get them, and both name these codes in the request's `handledErrorCodes`, so
 * api.js stays silent and the page shows the refusal once, in the same words. A Map because the
 * lookup key comes off the wire (security/detect-object-injection).
 */
export const ADMIN_REASON_ERROR_MESSAGES = new Map([
  [
    // The reason field is required and trimmed, so only a request that bypasses it gets here.
    'ADMIN_REASON_REQUIRED',
    [
      'ui.orders.status_error.ADMIN_REASON_REQUIRED',
      'Enter a reason for cancelling or returning this order.',
    ],
  ],
  [
    // The field stops at the column length. Mapped so the server's English never reaches the admin.
    'ADMIN_REASON_TOO_LONG',
    [
      'ui.orders.status_error.ADMIN_REASON_TOO_LONG',
      'The reason can be at most 100 characters.',
    ],
  ],
]);

/**
 * The internal reason every admin cancel or return must give (F6), as a field of the ENCLOSING
 * antd Form. It is stored in the history `reason` column, which only admins read. The note beside
 * it is what the customer sees.
 *
 * `whitespace`: the backend strips the reason and refuses a blank one (ADMIN_REASON_REQUIRED).
 *
 * The rule and the length cap are written here only. The Delivery page renders this field too
 * (F6), under its own `ui.delivery.return_reason_*` copy, so it passes `label` and
 * `requiredMessage`. Without them the field shows the Orders copy.
 */
export const AdminReasonField = ({ label, requiredMessage }) => {
  const { t } = useTranslation('orders');
  return (
    <Form.Item
      name="reason"
      label={label ?? t('ui.orders.reason_internal_label', 'Reason (internal, not shown to the customer)')}
      rules={[{
        required: true,
        whitespace: true,
        message: requiredMessage ?? t('ui.orders.reason_required', 'Reason is required'),
      }]}
    >
      <Input.TextArea rows={2} maxLength={ADMIN_REASON_MAX_LENGTH} showCount />
    </Form.Item>
  );
};

/**
 * F6: the first thing an admin reads before ending or moving on an order whose delivery failed.
 * Its button is the primary action: the re-date the order is actually waiting for. It is shown
 * only when the backend publishes `awaiting_new_date` (F8); nothing here works that out.
 */
export const AwaitingNewDateNotice = ({ onRescheduleInstead }) => {
  const { t } = useTranslation('orders');
  return (
    <Alert
      type="warning"
      showIcon
      style={{ marginBottom: 12 }}
      message={t('ui.orders.awaiting_new_date_notice', "This order's delivery failed and is waiting for a new date.")}
      action={(
        <Button type="primary" size="small" onClick={onRescheduleInstead}>
          {t('ui.orders.reschedule_instead', 'Reschedule instead')}
        </Button>
      )}
    />
  );
};

/**
 * Confirms an admin cancel or return (F6). It is opened from the row menu's Cancel, and from the
 * Update Status modal when that modal moves the order to Cancelled or Returned.
 *
 * `request` is `{ order, status, reason?, notes? }`.
 * - The row menu passes no reason, so the dialog asks for one.
 * - The Update Status modal passes the reason its own field holds.
 * Either way, `onConfirm` gets the trimmed reason. The reason is internal and is never sent as the
 * customer-visible `notes`.
 *
 * The page mounts it per open (`{closeRequest ? <CloseOrderDialog … /> : null}`), never as an
 * always-mounted `open` toggle:
 * - every open starts with an empty reason;
 * - its portal is appended to <body> after every modal already open. Sibling antd modals share
 *   one z-index, so the portal appended last is drawn on top. An always-mounted modal keeps the
 *   portal position of its first open, and would open hidden under an Update Status modal that
 *   was first opened after it.
 */
const CloseOrderDialog = ({ request, loading, onConfirm, onRescheduleInstead, onClose }) => {
  const { t } = useTranslation('orders');
  const { order, status } = request;
  const askReason = request.reason == null;
  const returning = status === 'returned';
  const awaiting = Boolean(order.awaiting_new_date);

  return (
    <Modal
      title={returning
        ? t('ui.orders.return_order_title', 'Mark order as returned')
        : t('ui.orders.cancel_order_title', 'Cancel order')}
      open
      onCancel={onClose}
      footer={null}
    >
      <Form
        name="close_order"
        layout="vertical"
        onFinish={(values) => onConfirm((askReason ? values.reason : request.reason).trim())}
      >
        {awaiting ? <AwaitingNewDateNotice onRescheduleInstead={onRescheduleInstead} /> : null}
        <p>
          {returning
            ? t('ui.orders.return_order_confirm', 'Mark order {{number}} as returned?', { number: order.order_number })
            : `${t('ui.orders.cancel_order_confirm', 'Cancel order')} ${order.order_number}?`}
        </p>
        {askReason ? <AdminReasonField /> : null}
        <Form.Item style={{ marginBottom: 0, textAlign: 'right' }}>
          <Space>
            <Button onClick={onClose}>{t('ui.orders.close', 'Close')}</Button>
            {/* Never the primary action while the order waits for a new date: Reschedule instead is. */}
            <AsyncButton danger type={awaiting ? 'default' : 'primary'} htmlType="submit" loading={loading}>
              {returning ? t('ui.orders.mark_returned', 'Mark as returned') : t('ui.orders.cancel_order', 'Cancel Order')}
            </AsyncButton>
          </Space>
        </Form.Item>
      </Form>
    </Modal>
  );
};

export default CloseOrderDialog;
