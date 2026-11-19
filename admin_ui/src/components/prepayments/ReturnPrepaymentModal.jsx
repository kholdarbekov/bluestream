import React, { useEffect, useRef, useState } from 'react';
import {
  Alert, Button, DatePicker, Descriptions, Form, Input, Modal, Radio, Select, Space, message,
} from 'antd';
import dayjs from 'dayjs';
import { useTranslation } from 'react-i18next';
import staffService from '../../services/staffService';
import MoneyInput from '../common/MoneyInput';
import {
  PAYOUT_BLOCK_COPY, PAYOUT_HANDLED_ERROR_CODES, PAYOUT_ORDER_STATUS_COPY, PAYOUT_SUBSCRIPTION_STATUS_COPY,
  describeCodedReason, payoutErrorText,
} from '../../utils/cashScopeDisplay';
import { formatMoneyUZS } from '../../utils/formatMoney';

const PAID_BY_OFFICE = 'office';
const MODE_AMOUNT = 'amount';
const MODE_TARGET = 'target';

// Named on every payout request, so api.js stays silent and the one message is the translated one.
const REQUEST_CONFIG = { handledErrorCodes: PAYOUT_HANDLED_ERROR_CODES };

const range = (from, to) => Array.from({ length: Math.max(0, to - from) }, (_, i) => from + i);

// Today's hours and minutes after now are not selectable.
const disabledPaidAtTime = (current) => {
  const now = dayjs();
  if (!current || !current.isSame(now, 'day')) return {};
  return {
    disabledHours: () => range(now.hour() + 1, 24),
    disabledMinutes: (hour) => (hour === now.hour() ? range(now.minute() + 1, 60) : []),
  };
};

/**
 * Admin flow for returning a customer's free prepayment: step 1 form, step 2 the backend's preview.
 *
 * The first mode sends `amount` (omitted when the typed amount is empty or 0, so the backend answers with the block
 * and the held orders instead of refusing a 0; the `freeBalance` prop can be stale, so it never decides that),
 * the second `target_balance`, never both. The apply sends the amount the preview answered. `paid_at` goes out only when
 * the admin set it. The idempotency key is made once per open, so a double click or a retry replays.
 *
 * @param {{open: boolean, customerId: number, freeBalance: number, onClose: () => void, onDone: (r: object) => void}} props
 */
const ReturnPrepaymentModal = ({ open, customerId, freeBalance, onClose, onDone }) => {
  const { t } = useTranslation('common');
  const [form] = Form.useForm();
  const [step, setStep] = useState(1);
  const [pending, setPending] = useState(null); // validated values, snapshotted while step 1 is mounted
  const [preview, setPreview] = useState(null);
  const [drivers, setDrivers] = useState([]);
  const [driversFailed, setDriversFailed] = useState(false);
  const [busy, setBusy] = useState(false);
  const idempotencyKey = useRef(null);

  useEffect(() => {
    if (!open) return;
    idempotencyKey.current = crypto.randomUUID();
    setStep(1);
    setPending(null);
    setPreview(null);
    form.resetFields();
    let cancelled = false;
    setDriversFailed(false);
    staffService.getDeliveryPersons({ status: 'active', per_page: 100 })
      .then((resp) => { if (!cancelled) setDrivers(resp?.data?.data?.items || []); })
      .catch(() => { if (!cancelled) { setDrivers([]); setDriversFailed(true); } });
    return () => { cancelled = true; };
  }, [open, form]);

  const mode = Form.useWatch('mode', form) || MODE_AMOUNT;
  const paidBy = Form.useWatch('paid_by', form) || PAID_BY_OFFICE;

  // `amount` is the preview's answer on apply (a stale prop or a moved balance can't make a body the backend
  // refuses); on preview it is the typed one.
  const buildBody = (values, amount = Number(values.amount)) => {
    const body = { paid_by: values.paid_by };
    if (values.mode === MODE_TARGET) body.target_balance = Number(values.target);
    else if (amount > 0) body.amount = amount;
    if (values.paid_by !== PAID_BY_OFFICE) body.driver_user_id = values.driver_user_id;
    if (values.paid_at) body.paid_at = values.paid_at.toISOString();
    return body;
  };

  const handlePreview = async () => {
    let values;
    try {
      values = await form.validateFields();
    } catch (_err) {
      return; // invalid form (incl. a driver mode without a driver): inline errors, no request
    }
    const snapshot = { ...values, reason: values.reason.trim() };
    setBusy(true);
    try {
      const resp = await staffService.previewPrepaymentPayout(customerId, buildBody(snapshot), REQUEST_CONFIG);
      setPending(snapshot);
      setPreview(resp?.data?.data || null);
      setStep(2);
    } catch (err) {
      // A mapped refusal was named in handledErrorCodes, so api.js stayed silent: this is its one message.
      // Anything else api.js has already toasted, so a second toast here would be a double.
      const text = payoutErrorText(err, t);
      if (text) message.error(text);
    } finally {
      setBusy(false);
    }
  };

  const handleApply = async () => {
    if (!pending) return;
    setBusy(true);
    try {
      const resp = await staffService.returnPrepayment(customerId, {
        ...buildBody(pending, Number(preview?.amount)),
        reason: pending.reason,
        idempotency_key: idempotencyKey.current,
      }, REQUEST_CONFIG);
      // The recorded amount, not the typed one: a set-balance return can record a different sum than
      // the preview showed when the balance moved in between.
      const recorded = resp?.data?.data?.payout?.amount;
      message.success(recorded != null
        ? t('ui.prepayments.return_success_amount', 'Returned {{amount}} to the customer', { amount: formatMoneyUZS(recorded) })
        : t('ui.prepayments.return_success', 'Prepayment returned'));
      onDone?.(resp?.data?.data);
      onClose?.();
    } catch (err) {
      const text = payoutErrorText(err, t);
      if (text) message.error(text);
    } finally {
      setBusy(false);
    }
  };

  const blocks = preview?.blocking_reasons || [];
  const warnings = preview?.warnings || {};
  const session = preview?.session_effect;

  const step1 = (
    <Form
      form={form}
      layout="vertical"
      initialValues={{ mode: MODE_AMOUNT, amount: freeBalance, target: 0, paid_by: PAID_BY_OFFICE }}
    >
      <Form.Item name="mode">
        <Radio.Group>
          <Radio value={MODE_AMOUNT}>{t('ui.prepayments.mode_amount', 'Amount to return')}</Radio>
          <Radio value={MODE_TARGET}>{t('ui.prepayments.mode_target', 'Balance after return')}</Radio>
        </Radio.Group>
      </Form.Item>
      {mode === MODE_TARGET ? (
        <Form.Item name="target" rules={[{ required: true }]}>
          <MoneyInput addonAfter="UZS" />
        </Form.Item>
      ) : (
        <Form.Item
          name="amount"
          extra={freeBalance > 0 ? undefined : t(
            'ui.prepayments.return_amount_locked_hint',
            'There is no free prepayment right now. Preview shows what is held.',
          )}
          rules={[
            { required: true },
            {
              // A 0 is never sent. With nothing free the amount is left out and the preview explains why.
              validator: (_rule, value) => (freeBalance > 0 && !(Number(value) > 0)
                ? Promise.reject(new Error(t('ui.prepayments.return_amount_required', 'Enter an amount above 0')))
                : Promise.resolve()),
            },
          ]}
        >
          <MoneyInput addonAfter="UZS" disabled={!(freeBalance > 0)} />
        </Form.Item>
      )}
      <Form.Item name="paid_by" label={t('ui.prepayments.return_paid_by', 'Paid by')}>
        <Radio.Group>
          <Space direction="vertical">
            <Radio value="office">{t('ui.prepayments.paid_by_office', 'Office')}</Radio>
            <Radio value="driver_current_session">
              {t('ui.prepayments.paid_by_driver_current', "Driver, from his current trip's cash")}
            </Radio>
            <Radio value="driver_settled">{t('ui.prepayments.paid_by_driver_settled', 'Driver, already settled')}</Radio>
          </Space>
        </Radio.Group>
      </Form.Item>
      {paidBy !== PAID_BY_OFFICE && (
        <Form.Item
          name="driver_user_id"
          label={t('ui.prepayments.return_driver', 'Driver')}
          rules={[{ required: true, message: t('ui.prepayments.return_driver_required', 'Pick the driver') }]}
        >
          <Select
            showSearch
            optionFilterProp="label"
            placeholder={t('ui.prepayments.return_driver_placeholder', 'Select a driver')}
            status={driversFailed ? 'error' : undefined}
            notFoundContent={driversFailed
              ? t('ui.prepayments.return_drivers_failed', 'Could not load the drivers. Close this window and try again.')
              : undefined}
            options={drivers.map((driver) => ({ value: driver.user_id, label: driver.full_name }))}
          />
        </Form.Item>
      )}
      <Form.Item
        name="paid_at"
        label={t('ui.prepayments.return_paid_at', 'Paid at (optional)')}
        rules={[{
          validator: (_rule, value) => (value && value.isAfter(dayjs())
            ? Promise.reject(new Error(t('ui.prepayments.return_paid_at_future', "Paid at can't be in the future")))
            : Promise.resolve()),
        }]}
      >
        <DatePicker
          showTime
          format="YYYY-MM-DD HH:mm"
          placeholder={t('ui.prepayments.return_paid_at', 'Paid at (optional)')}
          disabledDate={(current) => Boolean(current) && current.isAfter(dayjs(), 'day')}
          disabledTime={disabledPaidAtTime}
          style={{ width: '100%' }}
        />
      </Form.Item>
      <Form.Item
        name="reason"
        label={t('ui.prepayments.return_reason', 'Reason')}
        rules={[{
          required: true,
          min: 5,
          whitespace: true,
          message: t('ui.prepayments.return_reason_required', 'Reason (min 5 chars) is required'),
        }]}
      >
        <Input.TextArea rows={2} />
      </Form.Item>
      <div style={{ textAlign: 'right' }}>
        <Button onClick={onClose} style={{ marginRight: 8 }}>{t('ui.common.cancel', 'Cancel')}</Button>
        <Button type="primary" loading={busy} onClick={handlePreview}>
          {t('ui.prepayments.return_preview', 'Preview')}
        </Button>
      </div>
    </Form>
  );

  const step2 = preview && (
    <div>
      <Descriptions column={1} size="small" bordered style={{ marginBottom: 12 }}>
        <Descriptions.Item label={t('ui.prepayments.return_free_balance', 'Free balance')}>
          {formatMoneyUZS(preview.free_balance)}
        </Descriptions.Item>
        <Descriptions.Item label={t('ui.prepayments.return_amount', 'Amount returned')}>
          {formatMoneyUZS(preview.amount)}
        </Descriptions.Item>
        {preview.target_balance != null && (
          <Descriptions.Item label={t('ui.prepayments.mode_target', 'Balance after return')}>
            {formatMoneyUZS(preview.target_balance)}
          </Descriptions.Item>
        )}
        {Number(preview.card_click_amount) > 0 && (
          <Descriptions.Item label={t('ui.prepayments.card_click_share', 'Of this, from card/Click')}>
            <span data-testid="payout-card-click">{formatMoneyUZS(preview.card_click_amount)}</span>
          </Descriptions.Item>
        )}
      </Descriptions>

      {blocks.map((reason) => (
        <Alert
          key={reason}
          type="error"
          showIcon
          style={{ marginBottom: 6 }}
          message={describeCodedReason(reason, PAYOUT_BLOCK_COPY, t)}
        />
      ))}

      {(warnings.pending_orders || []).length > 0 && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 6 }}
          message={t('ui.prepayments.return_pending_orders', 'Pending orders')}
          description={(warnings.pending_orders || []).map((order) => (
            <div key={order.order_id}>
              <div>
                {`${order.order_number} · ${describeCodedReason(order.status, PAYOUT_ORDER_STATUS_COPY, t)} · ${formatMoneyUZS(order.held_amount)}`}
              </div>
              {Number(order.held_amount) > 0 && (
                <div>
                  {t('ui.prepayments.release_by_cancel', 'Cancel order {{order_number}} to release it', {
                    order_number: order.order_number,
                  })}
                </div>
              )}
            </div>
          ))}
        />
      )}
      {(warnings.live_subscriptions || []).length > 0 && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 6 }}
          message={t('ui.prepayments.return_live_subscriptions', 'Live subscriptions may rely on this credit')}
          description={(warnings.live_subscriptions || []).map((sub) => (
            <div key={sub.subscription_id}>{`#${sub.subscription_id} · ${describeCodedReason(sub.status, PAYOUT_SUBSCRIPTION_STATUS_COPY, t)}`}</div>
          ))}
        />
      )}
      {(warnings.place_debts || []).length > 0 && (
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 6 }}
          message={t('ui.prepayments.return_place_debts', 'Open debts at this customer\'s places')}
          description={(warnings.place_debts || []).map((debt) => (
            <div key={debt.place_group_id}>{`${debt.label} · ${formatMoneyUZS(debt.amount)}`}</div>
          ))}
        />
      )}
      {blocks.length === 0 && session && session.will_close && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 6 }}
          message={t(
            'ui.prepayments.return_session_closes_warning',
            "This return closes the driver's trip; it can't be undone afterwards.",
          )}
        />
      )}
      {session && Number(session.expenses_waiting) > 0 && (
        // Outside the branch below on purpose: a typed amount refused with amount_exceeds_driver_cash
        // is exactly when this explains the lower limit.
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 6 }}
          message={t(
            'ui.prepayments.return_expenses_waiting',
            "{{amount}} of the driver's trip cash is waiting for an expense decision, so the limit is lower",
            { amount: formatMoneyUZS(session.expenses_waiting) },
          )}
        />
      )}
      {blocks.length === 0 && session && (
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 6 }}
          message={t('ui.prepayments.return_session_effect', "The driver's trip cash")}
          description={(
            <div>
              <div>
                {`${formatMoneyUZS(session.on_hand_before)} → ${formatMoneyUZS(session.on_hand_after)}`}
              </div>
            </div>
          )}
        />
      )}
      {(preview.contract_effect || []).length > 0 && (
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 6 }}
          message={t('ui.prepayments.return_contract_effect', 'Contract accounts')}
          description={(preview.contract_effect || []).map((row) => (
            <div key={row.contract_id}>{`${row.contract_number} · ${row.name} · ${formatMoneyUZS(row.amount)}`}</div>
          ))}
        />
      )}

      <div style={{ textAlign: 'right', marginTop: 12 }}>
        <Button onClick={() => setStep(1)} style={{ marginRight: 8 }}>{t('ui.common.back', 'Back')}</Button>
        <Button type="primary" danger loading={busy} disabled={blocks.length > 0} onClick={handleApply}>
          {t('ui.prepayments.return_button', 'Return prepayment')}
        </Button>
      </div>
    </div>
  );

  return (
    <Modal
      open={open}
      title={t('ui.prepayments.return_button', 'Return prepayment')}
      onCancel={onClose}
      footer={null}
      destroyOnClose
    >
      {step === 1 ? step1 : step2}
    </Modal>
  );
};

export default ReturnPrepaymentModal;
