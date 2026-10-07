import React, { useState } from 'react';
import {
  Alert, Button, Card, Input, Modal, Popover, Segmented, Select, Space, Table, Tag, Typography, message,
} from 'antd';
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import salesService, { ORDER_APPROVAL_HANDLED_CODES } from '../services/salesService';
import staffService from '../services/staffService';
import { useAuthStore } from '../stores/authStore';
import { fetchAllPages } from '../utils/pagination';
import { BULK_LOAD_PAGE_SIZE, DEFAULT_PAGE_SIZE } from '../utils/constants';
import { apiErrorCode, extractApiErrorMessage } from '../utils/apiError';
import { formatDate, formatDateTimeShort } from '../utils/dateUtils';
import { formatMoney } from '../utils/formatMoney';
import { getOrderStatusColor } from '../utils/orderStatusColor';

const { Title, Text } = Typography;

// Presentation only. The vocabulary is the backend's published `statuses` (§5.7); a status this
// map does not know still renders, as a plain tag.
const APPROVAL_STATUS_COLORS = { pending: 'gold', approved: 'green', rejected: 'red', cancelled: 'default' };

/**
 * The same-day order approval queue (C14, spec §6.7), for managers and admins.
 *
 * An agent's second or later order for one outlet on one local day waits here until someone
 * approves it (it is confirmed and goes to the drivers) or rejects it (it is cancelled). The page
 * decides nothing itself: the rows, the published statuses, `can_decide` and `self_decided` are the
 * backend's. It shows order data only; no pay figure of any kind exists here (C11, T-HOLD-11).
 */
const SalesOrderApprovals = () => {
  const { t } = useTranslation(['sales_agents', 'common']);
  const queryClient = useQueryClient();
  const { hasPermission } = useAuthStore();
  const canReviewAgentOrders = hasPermission('can_review_agent_orders');

  const [status, setStatus] = useState('pending');
  const [agentId, setAgentId] = useState();
  const [page, setPage] = useState(1);
  // `{ kind: 'approve' | 'reject', row }` while the decision modal is open.
  const [decision, setDecision] = useState(null);
  const [reason, setReason] = useState('');
  // The translated refusal of the last decision, shown inside the modal (§6.5), or null.
  const [refusal, setRefusal] = useState(null);

  const approvalsQuery = useQuery({
    queryKey: ['orderApprovals', status, agentId, page],
    queryFn: () => salesService.listOrderApprovals({ status, agentId, page, perPage: DEFAULT_PAGE_SIZE }),
    enabled: canReviewAgentOrders,
    placeholderData: keepPreviousData,
  });
  const listing = approvalsQuery.data;

  // Same query key as Outlets.js and Visits.js, so react-query serves every page from one fetch.
  const { data: agentsData } = useQuery({
    queryKey: ['salesAgentOptions'],
    queryFn: () => fetchAllPages(
      (agentsPage) => staffService.getSalesAgents({ page: agentsPage, per_page: BULK_LOAD_PAGE_SIZE }),
      (resp) => resp?.data?.data?.items || [],
      BULK_LOAD_PAGE_SIZE,
    ),
    staleTime: 60_000,
  });
  const agents = agentsData || [];

  const closeDecision = () => {
    setDecision(null);
    setReason('');
    setRefusal(null);
  };

  const decide = useMutation({
    mutationFn: ({ kind, orderId, text }) => (kind === 'approve'
      ? salesService.approveAgentOrder(orderId)
      : salesService.rejectAgentOrder(orderId, text)),
    onSuccess: (_, { kind }) => {
      message.success(kind === 'approve'
        ? t('sales_agents:order_approvals.approved', 'Order approved')
        : t('sales_agents:order_approvals.rejected', 'Order rejected and cancelled'));
      // `['orderApprovals']` is the list's and the nav badge's shared prefix; the order changed too.
      queryClient.invalidateQueries({ queryKey: ['orderApprovals'] });
      queryClient.invalidateQueries({ queryKey: ['orders'] });
      closeDecision();
    },
    onError: (error) => {
      // §6.5: a 404/409 carries its code under `data`, a 403 at the top level; one reader for both.
      const code = apiErrorCode(error);
      // A code the request did not name was already toasted by api.js: one refusal, one message.
      if (!ORDER_APPROVAL_HANDLED_CODES.includes(code)) return;
      setRefusal(t(`sales_agents:order_approvals.error.${code.toLowerCase()}`, extractApiErrorMessage(error)));
      // Decided elsewhere, or the order was cancelled meanwhile: show the queue as it is now.
      if (code === 'SALES_ORDER_APPROVAL_NOT_PENDING') {
        queryClient.invalidateQueries({ queryKey: ['orderApprovals'] });
      }
    },
  });

  const resetToFirstPage = (setter) => (value) => {
    setter(value);
    setPage(1);
  };

  const renderDelivery = (order) => [
    formatDate(order.delivery_date),
    order.delivery_window_start && order.delivery_window_end
      ? `${order.delivery_window_start}–${order.delivery_window_end}`
      : null,
  ].filter(Boolean).join(' · ');

  const columns = [
    {
      title: t('sales_agents:order_approvals.col.requested_at', 'Placed'),
      dataIndex: 'requested_at',
      key: 'requested_at',
      render: (value) => formatDateTimeShort(value),
    },
    {
      title: t('sales_agents:order_approvals.col.agent', 'Agent'),
      key: 'agent',
      render: (_, row) => row.agent?.name || '—',
    },
    {
      title: t('sales_agents:order_approvals.col.outlet', 'Outlet'),
      key: 'outlet',
      render: (_, row) => row.outlet?.name || '—',
    },
    {
      title: t('sales_agents:order_approvals.col.order', 'Order'),
      key: 'order',
      render: (_, row) => (
        <Space direction="vertical" size={0}>
          <Text strong>{row.order.order_number}</Text>
          {row.order.items.map((item) => (
            <Text key={item.product_id} type="secondary">{`${item.product_name} × ${item.quantity}`}</Text>
          ))}
        </Space>
      ),
    },
    {
      title: t('sales_agents:order_approvals.col.total', 'Total'),
      key: 'total',
      render: (_, row) => `${formatMoney(row.order.total_amount)} UZS`,
    },
    {
      title: t('sales_agents:order_approvals.col.payment_method', 'Payment'),
      key: 'payment_method',
      // The Orders page's payment-method labels.
      render: (_, row) => t(`ui.orders.payment_${row.order.payment_method}`, row.order.payment_method),
    },
    {
      title: t('sales_agents:order_approvals.col.delivery_date', 'Delivery'),
      key: 'delivery',
      render: (_, row) => renderDelivery(row.order),
    },
    {
      title: t('sales_agents:order_approvals.col.earlier_orders', 'Earlier today'),
      key: 'earlier_orders',
      // The frozen earlier ids (I-22), each with its LIVE status.
      render: (_, row) => row.earlier_orders.map((earlier) => (
        <Tag
          key={earlier.id}
          color={getOrderStatusColor(earlier.status)}
          title={t(`ui.orders.status_${earlier.status}`, earlier.status)}
        >
          {earlier.order_number}
        </Tag>
      )),
    },
    {
      title: t('sales_agents:order_approvals.col.status', 'Status'),
      key: 'status',
      render: (_, row) => (
        <Space direction="vertical" size={2}>
          <Space size={4} wrap>
            <Tag color={APPROVAL_STATUS_COLORS[row.status] || 'default'}>
              {t(`sales_agents:order_approvals.status.${row.status}`, row.status)}
            </Tag>
            {/* The decider placed the order or onboarded the outlet (I-24): an admin may, and is tagged. */}
            {row.self_decided ? (
              <Tag color="orange">{t('sales_agents:order_approvals.self_decided', 'Self-decided')}</Tag>
            ) : null}
          </Space>
          {row.decided_by ? (
            <Text type="secondary">
              {t('sales_agents:order_approvals.decided_by', 'by {{name}}', { name: row.decided_by.name })}
            </Text>
          ) : null}
          {row.reason ? (
            <Popover content={<div style={{ maxWidth: 320, whiteSpace: 'pre-wrap' }}>{row.reason}</div>}>
              <Text ellipsis style={{ maxWidth: 200 }}>{row.reason}</Text>
            </Popover>
          ) : null}
        </Space>
      ),
    },
    {
      title: '',
      key: 'actions',
      // Drawn from `can_decide` only (§4.18.6): the backend knows who placed it and who onboarded it.
      render: (_, row) => (row.can_decide ? (
        <Space>
          <Button type="primary" size="small" onClick={() => setDecision({ kind: 'approve', row })}>
            {t('sales_agents:order_approvals.action.approve', 'Approve')}
          </Button>
          <Button danger size="small" onClick={() => setDecision({ kind: 'reject', row })}>
            {t('sales_agents:order_approvals.action.reject', 'Reject')}
          </Button>
        </Space>
      ) : null),
    },
  ];

  const isReject = decision?.kind === 'reject';
  const orderNumber = decision?.row.order.order_number;

  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <div>
        <Title level={3}>{t('sales_agents:order_approvals.title', 'Order approvals')}</Title>
        <Text type="secondary">
          {t(
            'sales_agents:order_approvals.hint',
            "An agent's second or later order for one outlet on one day waits here. Approve sends it to delivery; Reject cancels it.",
          )}
        </Text>
      </div>

      <Card size="small">
        <Space wrap>
          <Segmented
            value={status}
            options={(listing?.statuses || [status]).map((value) => ({
              value,
              label: t(`sales_agents:order_approvals.status.${value}`, value),
            }))}
            onChange={resetToFirstPage(setStatus)}
          />
          <Select
            data-testid="order-approvals-agent"
            allowClear
            showSearch
            optionFilterProp="label"
            style={{ minWidth: 220 }}
            placeholder={t('sales_agents:agent', 'Agent')}
            value={agentId}
            options={agents.map((agent) => ({ value: agent.user_id, label: agent.full_name }))}
            onChange={resetToFirstPage(setAgentId)}
          />
        </Space>
      </Card>

      {approvalsQuery.isError ? (
        <Alert
          type="error"
          showIcon
          message={extractApiErrorMessage(approvalsQuery.error, t('ui.common.error_occurred', 'An error occurred'))}
        />
      ) : null}

      <Table
        rowKey="order_id"
        columns={columns}
        dataSource={listing?.items || []}
        loading={approvalsQuery.isFetching}
        locale={{ emptyText: t('sales_agents:order_approvals.empty', 'No orders are waiting for approval.') }}
        pagination={{
          current: page,
          pageSize: DEFAULT_PAGE_SIZE,
          total: listing?.meta?.total || 0,
          showSizeChanger: false,
          onChange: setPage,
        }}
        scroll={{ x: 1200 }}
      />

      {/* One controlled modal for both decisions, not Modal.confirm: a static confirm cannot show
          the refusal inline (§6.7 Errors), and the admin must see one message, not a toast too. */}
      <Modal
        open={Boolean(decision)}
        title={isReject
          ? t('sales_agents:order_approvals.reject.title', 'Reject order {{order_number}}', { order_number: orderNumber })
          : t('sales_agents:order_approvals.confirm.approve.title', 'Approve order {{order_number}}?', { order_number: orderNumber })}
        okText={isReject
          ? t('sales_agents:order_approvals.action.reject', 'Reject')
          : t('sales_agents:order_approvals.action.approve', 'Approve')}
        okButtonProps={{ danger: isReject, disabled: isReject && !reason.trim() }}
        confirmLoading={decide.isPending}
        onOk={() => {
          setRefusal(null);
          decide.mutate({ kind: decision.kind, orderId: decision.row.order_id, text: reason });
        }}
        onCancel={closeDecision}
        destroyOnClose
      >
        {refusal ? <Alert type="error" showIcon message={refusal} style={{ marginBottom: 12 }} /> : null}
        {isReject ? (
          <Space direction="vertical" style={{ width: '100%' }}>
            <Text type="secondary">
              {t(
                'sales_agents:order_approvals.reject.hint',
                'The agent sees this reason. The store is only told that the order was cancelled.',
              )}
            </Text>
            <label htmlFor="order-approval-reject-reason">
              {t('sales_agents:order_approvals.reject.reason', 'Reason')}
            </label>
            <Input.TextArea
              id="order-approval-reject-reason"
              rows={3}
              value={reason}
              onChange={(event) => setReason(event.target.value)}
            />
          </Space>
        ) : (
          <Text>{t('sales_agents:order_approvals.confirm.approve.body', 'It is confirmed and goes to the drivers.')}</Text>
        )}
      </Modal>
    </Space>
  );
};

export default SalesOrderApprovals;
