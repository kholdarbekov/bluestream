import React, { useState } from 'react';
import { Button, Card, Form, Input, Modal, Space, Table, Tag, Tooltip, Typography, message } from 'antd';
import { InfoCircleOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import staffService from '../../services/staffService';
import {
  PAYOUT_HANDLED_ERROR_CODES, UNDO_BLOCK_COPY, describeCodedReason, payoutErrorText,
} from '../../utils/cashScopeDisplay';
import { apiErrorCode } from '../../utils/apiError';
import { formatLocaleDateTime } from '../../utils/dateUtils';
import { formatMoneyUZS } from '../../utils/formatMoney';

const { Text } = Typography;

const PAID_BY_COPY = new Map([
  ['office', ['ui.prepayments.paid_by_office', 'Office']],
  ['driver_current_session', ['ui.prepayments.paid_by_driver_current', "Driver, from his current trip's cash"]],
  ['driver_settled', ['ui.prepayments.paid_by_driver_settled', 'Driver, already settled']],
]);

/**
 * The returned-prepayment rows of a customer's history and the Undo modal behind them.
 *
 * Undo is offered only where the backend published `can_undo`, which already includes whether THIS viewer
 * may undo (a manager reads false). `undo_block_code` explains a return-level reason; a row refused only
 * because of the viewer carries none, so it shows nothing rather than a misleading reason.
 *
 * @param {{ payouts: Array, onChanged: () => void }} props `onChanged` runs after a successful undo
 */
const PayoutRows = ({ payouts, onChanged }) => {
  const { t } = useTranslation('common');
  const [form] = Form.useForm();
  const [target, setTarget] = useState(null);
  const [submitting, setSubmitting] = useState(false);

  if (!payouts || payouts.length === 0) return null;

  const closeUndo = () => setTarget(null);

  const submitUndo = async () => {
    let values;
    try {
      values = await form.validateFields();
    } catch (_err) {
      return; // antd shows the inline field error
    }
    setSubmitting(true);
    try {
      await staffService.undoPrepaymentPayout(
        target.id,
        { reason: values.reason },
        { handledErrorCodes: PAYOUT_HANDLED_ERROR_CODES },
      );
      message.success(t('ui.prepayments.undo_success', 'Return undone'));
      closeUndo();
      onChanged?.();
    } catch (err) {
      // A mapped refusal was named in handledErrorCodes (api.js stayed silent): this is its one message.
      // Anything else was toasted by api.js already.
      const text = payoutErrorText(err, t);
      if (text) {
        message.error(text);
        // A refused undo means the row is out of date (e.g. the trip closed meanwhile): refresh, so it
        // stops offering Undo.
        if (apiErrorCode(err) === 'PAYOUT_UNDO_BLOCKED') {
          closeUndo();
          onChanged?.();
        }
      }
    } finally {
      setSubmitting(false);
    }
  };

  const columns = [
    {
      title: t('ui.prepayments.occurred_at', 'When'),
      dataIndex: 'paid_at',
      key: 'paid_at',
      render: (value) => (value ? formatLocaleDateTime(value) : '—'),
    },
    {
      title: t('ui.prepayments.return_amount', 'Amount returned'),
      dataIndex: 'amount',
      key: 'amount',
      align: 'right',
      render: (value, row) => (
        <Text style={row.voided_at ? { textDecoration: 'line-through', color: '#999' } : undefined}>
          {formatMoneyUZS(value)}
        </Text>
      ),
    },
    {
      title: t('ui.prepayments.return_paid_by', 'Paid by'),
      key: 'paid_by',
      render: (_, row) => {
        const entry = PAID_BY_COPY.get(row.paid_by);
        return (
          <Space direction="vertical" size={0}>
            <span>{entry ? t(entry[0], entry[1]) : row.paid_by}</span>
            {row.driver_name && <Text type="secondary">{row.driver_name}</Text>}
          </Space>
        );
      },
    },
    {
      title: t('ui.prepayments.return_reason', 'Reason'),
      dataIndex: 'reason',
      key: 'reason',
    },
    {
      title: '',
      key: 'actions',
      align: 'right',
      render: (_, row) => {
        if (row.can_undo) {
          return (
            <Button size="small" danger onClick={() => { form.resetFields(); setTarget(row); }}>
              {t('ui.prepayments.undo', 'Undo')}
            </Button>
          );
        }
        return (
          <Space direction="vertical" size={0} align="end">
            {row.voided_at && (
              <>
                <Tag color="red">{t('ui.prepayments.undone', 'Undone')}</Tag>
                {row.void_reason && <Text type="secondary">{row.void_reason}</Text>}
              </>
            )}
            {!row.voided_at && row.undo_block_code && (
              <Tooltip title={describeCodedReason(row.undo_block_code, UNDO_BLOCK_COPY, t)}>
                <InfoCircleOutlined />
              </Tooltip>
            )}
          </Space>
        );
      },
    },
  ];

  return (
    <Card size="small" style={{ marginBottom: 12 }} title={t('ui.prepayments.returns_table', 'Returned to the customer')}>
      <div data-testid="payout-rows">
        <Table
          rowKey="id"
          size="small"
          pagination={false}
          dataSource={payouts}
          columns={columns}
          rowClassName={(row) => (row.voided_at ? 'prepayment-voided-row' : '')}
        />
      </div>
      <Modal
        open={Boolean(target)}
        title={t('ui.prepayments.undo_title', 'Undo this return')}
        okText={t('ui.prepayments.undo', 'Undo')}
        okButtonProps={{ danger: true }}
        confirmLoading={submitting}
        onOk={submitUndo}
        onCancel={closeUndo}
        destroyOnClose
      >
        <Form form={form} layout="vertical">
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
        </Form>
      </Modal>
    </Card>
  );
};

export default PayoutRows;
