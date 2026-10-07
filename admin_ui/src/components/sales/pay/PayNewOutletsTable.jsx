import React from 'react';
import { Space, Table, Tag, Tooltip } from 'antd';
import { useTranslation } from 'react-i18next';
import { dateLabel, instant, signedMoney } from './payFormat';

// A flag is set when the published value is `true` or a positive count (`SALES_PAY_REVIEW_FLAGS`
// mixes booleans and counts, §4.6). The label key is built from the published key, so a new flag
// needs only its seed row (pinned in tests/unit/test_admin_ui_payload_fixture_contracts.py).
const setFlags = (flags) => Object.entries(flags || {}).filter(([, value]) => value === true || Number(value) > 0);

/**
 * The drawer's New outlets tab (§6.2 item 4): every outlet check touched in the month, with its
 * window, rule, amount and review-flag badges. Qualifying or tracked orders expand below.
 */
const PayNewOutletsTable = ({ rows }) => {
  const { t } = useTranslation(['sales_agents', 'common']);

  const orderColumns = [
    { title: t('sales_agents:pay.col.order', 'Order'), dataIndex: 'order_number', key: 'order_number' },
    { title: t('sales_agents:pay.col.source', 'Source'), dataIndex: 'order_source', key: 'order_source', render: (value) => value || '—' },
    { title: t('sales_agents:pay.col.date', 'Date'), key: 'instant', render: (_, order) => instant(order.earned_instant || order.delivered_instant) },
    { title: t('sales_agents:pay.col.total', 'Total'), dataIndex: 'total', key: 'total', render: (value) => (value == null ? '—' : signedMoney(value)) },
    {
      title: '',
      key: 'staff_approved',
      render: (_, order) => (order.staff_approved
        ? <Tag color="blue">{t('sales_agents:pay.outlet.staff_approved', 'approved extra order')}</Tag>
        : null),
    },
  ];

  const windowTip = (row) => (
    <>
      {row.window_opened_by ? (
        <div>
          {t('sales_agents:pay.outlet.window_opened_by', { defaultValue: 'Window opened by the first delivery: {{order}}, {{date}}', order: row.window_opened_by.order_number, date: instant(row.window_opened_by.delivered_instant) })}
        </div>
      ) : null}
      {/* The hint names the window's length, A8's `window_days` read from the check's frozen
          plan version (PR9). It is null only for a check with no plan version, so the guard
          stays for that row alone. */}
      {row.window_days != null ? (
        <div>
          {t('sales_agents:pay.outlet.window_hint', { defaultValue: 'The {{days}}-day window starts at the first delivery after onboarding, paid or not. Only orders delivered and paid inside it count.', days: row.window_days })}
        </div>
      ) : null}
    </>
  );

  const columns = [
    { title: t('sales_agents:pay.col.outlet', 'Outlet'), dataIndex: 'outlet_name', key: 'outlet_name' },
    { title: t('sales_agents:pay.col.status', 'Status'), dataIndex: 'status', key: 'status', render: (value) => <Tag>{t(`sales_agents:pay.check_status.${value}`, value)}</Tag> },
    { title: t('sales_agents:pay.col.activation', 'Activation'), dataIndex: 'activation_reason', key: 'activation_reason', render: (value) => value || '—' },
    { title: t('sales_agents:pay.col.onboarded', 'Onboarded'), dataIndex: 'onboarded_at', key: 'onboarded_at', render: instant },
    {
      title: t('sales_agents:pay.col.window', 'Window'),
      key: 'window',
      render: (_, row) => (row.window_start || row.window_end ? (
        <Tooltip title={windowTip(row)}>
          <span>{`${row.window_start ? dateLabel(row.window_start) : '—'} – ${row.window_end ? dateLabel(row.window_end) : '—'}`}</span>
        </Tooltip>
      ) : '—'),
    },
    { title: t('sales_agents:pay.col.rule', 'Rule'), dataIndex: 'rule', key: 'rule', render: (value) => (value ? t(`sales_agents:pay.bonus_rule.${value}`, value) : '—') },
    {
      title: t('sales_agents:pay.col.amount', 'Amount'),
      key: 'amount',
      render: (_, row) => (
        <Space size={4}>
          {row.amount == null ? '—' : signedMoney(row.amount)}
          {row.is_late ? <Tag color="orange">{t('sales_agents:pay.lines.late', 'late')}</Tag> : null}
        </Space>
      ),
    },
    {
      title: t('sales_agents:pay.col.review', 'review'),
      key: 'flags',
      render: (_, row) => (
        <Space size={4} wrap>
          {setFlags(row.review_flags).map(([key, value]) => (
            <Tag key={key} color="red">{t(`sales_agents:pay.flag.${key}`, { defaultValue: key, count: value })}</Tag>
          ))}
        </Space>
      ),
    },
  ];

  return (
    <Table
      rowKey="outlet_id"
      size="small"
      pagination={false}
      scroll={{ x: 1100 }}
      columns={columns}
      dataSource={rows}
      expandable={{
        rowExpandable: (row) => (row.orders || []).length > 0,
        expandedRowRender: (row) => (
          <Table rowKey="order_id" size="small" pagination={false} columns={orderColumns} dataSource={row.orders} />
        ),
      }}
    />
  );
};

export default PayNewOutletsTable;
