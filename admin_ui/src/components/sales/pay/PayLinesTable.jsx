import React, { useState } from 'react';
import { Alert, Select, Space, Table, Tag, Tooltip, Typography } from 'antd';
import { keepPreviousData, useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import salesPayService from '../../../services/salesPayService';
import { useAuthStore } from '../../../stores/authStore';
import { extractApiErrorMessage } from '../../../utils/apiError';
import { DEFAULT_PAGE_SIZE } from '../../../utils/constants';
import { grouped, monthLabel, productName, rateLabel, signedMoney, tierRange } from './payFormat';

const { Text } = Typography;

const blankOr = (value, format) => (value === null || value === undefined ? '' : format(value));

/**
 * One product's rows for the Tiers table (§6.2 item 2). A late row adds the share each tier paid
 * at the cut before (`tiers_before`), matched by the published `from_unit`; a tier the order has
 * left keeps its row, blank now, so a shift reads on one table. Nothing is summed.
 */
const segmentRows = (product) => {
  const before = new Map((product.tiers_before || []).map((tier) => [tier.from_unit, tier.share]));
  const now = new Set(product.tiers.map((tier) => tier.from_unit));
  return [
    ...product.tiers.map((tier) => ({ tier, units: tier.units, share: tier.share, before: before.get(tier.from_unit) })),
    ...(product.tiers_before || [])
      .filter((tier) => !now.has(tier.from_unit))
      .map((tier) => ({ tier, units: null, share: null, before: tier.share })),
  ];
};

/**
 * An order row's drill-down (§6.2 item 2): its tier segments per product, the frozen first-credit
 * items, the money (D-Q3), the units that left the count (OQ-B3) and this period's events. Every
 * figure, bound and flag is A9's; the settled hint follows each event's published `follows_money`
 * (ruling T14-R1), never a cause.
 */
const OrderDetail = ({ row }) => {
  const { t, i18n } = useTranslation(['sales_agents', 'common']);
  const name = (value) => productName(value, i18n.language);
  const { money } = row;

  const segmentColumns = (withBefore) => [
    { title: t('sales_agents:pay.col.tier', 'Tier'), key: 'tier', render: (_, segment) => tierRange(segment.tier.from_unit, segment.tier.to_unit) },
    { title: t('sales_agents:pay.col.units', 'Units'), key: 'units', render: (_, segment) => blankOr(segment.units, grouped) },
    { title: t('sales_agents:pay.col.rate', 'Rate'), key: 'rate', render: (_, segment) => rateLabel(t, segment.tier.mode, segment.tier.value) },
    { title: t('sales_agents:pay.col.share', 'Share'), key: 'share', render: (_, segment) => blankOr(segment.share, signedMoney) },
    ...(withBefore
      ? [{ title: t('sales_agents:pay.col.share_before', 'Before'), key: 'before', render: (_, segment) => blankOr(segment.before, signedMoney) }]
      : []),
  ];
  const itemColumns = [
    { title: t('sales_agents:pay.col.product', 'Product'), key: 'product', render: (_, item) => name(item.product_name) },
    { title: t('sales_agents:pay.col.qty', 'Qty'), dataIndex: 'quantity', key: 'quantity' },
    { title: t('sales_agents:pay.col.net', 'Net'), dataIndex: 'net', key: 'net', render: (value) => signedMoney(value) },
  ];

  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      {row.products.map((product) => (
        <Table
          key={product.product_id}
          rowKey={(segment) => segment.tier.from_unit}
          size="small"
          pagination={false}
          title={() => name(product.product_name)}
          columns={segmentColumns(Boolean(product.tiers_before))}
          dataSource={segmentRows(product)}
        />
      ))}
      {row.items.length > 0 ? (
        <Table
          rowKey="key"
          size="small"
          pagination={false}
          columns={itemColumns}
          dataSource={row.items.map((item, index) => ({ ...item, key: index }))}
        />
      ) : null}
      <Text>
        {t('sales_agents:pay.formula.money_line', { defaultValue: 'Money received {{received}} (at credit {{ref}}) · counted {{applied}}', received: signedMoney(money.received), ref: signedMoney(money.received_ref), applied: signedMoney(money.received_applied) })}
      </Text>
      {row.events.filter((event) => event.kind === 'commission_reversal').map((event) => (
        <Text key={`reversal-${event.id}`}>
          {t('sales_agents:pay.formula.reversal', { defaultValue: "Money received {{ref}} → {{received}}: the order's units leave the month's count", ref: signedMoney(money.received_ref), received: signedMoney(event.received) })}
        </Text>
      ))}
      {/* OQ-B3: `counted` is null once the order is reversed (the line above explains that), and
          `null < n` is true in JavaScript, so the null check is spelled out. */}
      {row.units_level.filter((level) => level.counted !== null && level.counted < level.at_credit).map((level) => (
        <Text key={`units-${level.product_id}`}>
          {t('sales_agents:pay.formula.units_line', { defaultValue: '{{product}}: {{on_order}} on the order (at credit {{at_credit}}) · counted {{counted}}', product: name(level.product_name), on_order: grouped(level.on_order), at_credit: grouped(level.at_credit), counted: grouped(level.counted) })}
        </Text>
      ))}
      {row.events.some((event) => event.follows_money) ? (
        <Alert
          type="info"
          showIcon
          message={t('sales_agents:pay.hint.settled_final', "Commission follows the money received and the units still on the order, never above the order's full share at its tiers. Once the month holding a reduction is closed, the reduction is final; if the money arrives later, add an adjustment.")}
        />
      ) : null}
      {row.events.map((event) => (
        <Text key={`event-${event.id}`} type="secondary">
          {[event.date, t(`sales_agents:pay.kind.${event.kind}`, event.kind), event.cause ? t(`sales_agents:pay.cause.${event.cause}`, event.cause) : null]
            .filter(Boolean)
            .join(' · ')}
        </Text>
      ))}
    </Space>
  );
};

/**
 * The Kind cell: the latest event's kind and cause. A tier-shift row has no event of its own
 * (§5.2), so it draws the tag and its hint; a bonus row is its own line.
 */
const RowEvent = ({ row }) => {
  const { t } = useTranslation(['sales_agents', 'common']);
  if (row.tier_shift) {
    return (
      <Tooltip title={t('sales_agents:pay.hint.tier_shift', "Another order of that month left or joined the count, so this order's units now fall in another tier, or its tier's rounded total was divided again (at most 1 UZS). The order itself did not change.")}>
        <Tag color="purple">{t('sales_agents:pay.lines.tier_shift', 'Tier change')}</Tag>
      </Tooltip>
    );
  }
  const latest = row.row === 'order' ? row.events.slice(-1)[0] : row;
  if (!latest) return null;
  return (
    <Space size={4} wrap>
      <Tag>{t(`sales_agents:pay.kind.${latest.kind}`, latest.kind)}</Tag>
      {latest.cause ? <Text type="secondary">{t(`sales_agents:pay.cause.${latest.cause}`, latest.cause)}</Text> : null}
    </Space>
  );
};

/**
 * The drawer's Orders tab (§6.2 item 2): A9, paginated on the server and filtered by a kind from
 * the published `line_kinds`. One row per order (its share of the month's tiers) and one per
 * new-outlet bonus; an order row expands into its tiers, items, money, units and events.
 */
const PayLinesTable = ({ month, agentId, lineKinds, active }) => {
  const { t, i18n } = useTranslation(['sales_agents', 'common']);
  const canManagePay = useAuthStore().hasPermission('can_manage_sales_pay');
  const [kind, setKind] = useState();
  const [page, setPage] = useState(1);

  const linesQuery = useQuery({
    queryKey: ['salesPay', 'lines', month, agentId, kind, page],
    queryFn: () => salesPayService.getStatementLines(month, agentId, { kind, page, perPage: DEFAULT_PAGE_SIZE }),
    enabled: canManagePay && Boolean(active),
    placeholderData: keepPreviousData,
  });
  const data = linesQuery.data || {};

  const columns = [
    { title: t('sales_agents:pay.col.date', 'Date'), dataIndex: 'date', key: 'date', render: (value) => value || '' },
    { title: t('sales_agents:pay.col.order', 'Order'), dataIndex: 'order_number', key: 'order_number', render: (value) => value || '—' },
    { title: t('sales_agents:pay.col.outlet', 'Outlet'), dataIndex: 'outlet_name', key: 'outlet_name', render: (value) => value || '—' },
    {
      title: t('sales_agents:pay.col.units', 'Units'),
      key: 'units',
      render: (_, row) => (row.row === 'order'
        ? row.products.map((product) => `${productName(product.product_name, i18n.language)} × ${grouped(product.units)}`).join(', ')
        : ''),
    },
    { title: t('sales_agents:pay.col.kind', 'Kind'), key: 'kind', render: (_, row) => <RowEvent row={row} /> },
    {
      title: t('sales_agents:pay.col.commission', 'Commission'),
      key: 'amount',
      render: (_, row) => (
        <Space direction="vertical" size={0}>
          <Text>{signedMoney(row.amount)}</Text>
          {row.row === 'order' && row.is_late
            ? <Text type="secondary">{`${signedMoney(row.share_before)} → ${signedMoney(row.share)}`}</Text>
            : null}
        </Space>
      ),
    },
    {
      title: '',
      key: 'tags',
      render: (_, row) => (
        <Space size={4} wrap>
          {row.is_late ? (
            <Tag color="orange">
              {`${t('sales_agents:pay.lines.late', 'late')} · ${t('sales_agents:pay.lines.for_month', { defaultValue: 'for {{month}}', month: monthLabel(row.earned_month) })}`}
            </Tag>
          ) : null}
          {row.counted === false ? <Tag>{t('sales_agents:pay.lines.not_counted_shadow', 'not counted (trial month)')}</Tag> : null}
        </Space>
      ),
    },
  ];

  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      {linesQuery.isError ? (
        <Alert type="error" showIcon message={extractApiErrorMessage(linesQuery.error, t('ui.common.error_occurred', 'An error occurred'))} />
      ) : null}
      <Select
        allowClear
        style={{ width: 260 }}
        placeholder={t('sales_agents:pay.lines.all_kinds', 'All kinds')}
        value={kind}
        onChange={(value) => { setKind(value); setPage(1); }}
        options={(data.line_kinds || lineKinds || []).map((value) => ({ value, label: t(`sales_agents:pay.kind.${value}`, value) }))}
      />
      <Table
        rowKey={(row) => (row.row === 'order' ? `order-${row.order_id}` : `bonus-${row.id}`)}
        size="small"
        columns={columns}
        dataSource={data.items || []}
        loading={linesQuery.isLoading}
        locale={{ emptyText: t('sales_agents:pay.empty.lines', 'No lines.') }}
        expandable={{
          rowExpandable: (row) => row.row === 'order',
          expandedRowRender: (row) => <OrderDetail row={row} />,
        }}
        pagination={{
          current: page,
          pageSize: DEFAULT_PAGE_SIZE,
          total: data.meta?.total || 0,
          showSizeChanger: false,
          onChange: setPage,
        }}
      />
    </Space>
  );
};

export default PayLinesTable;
