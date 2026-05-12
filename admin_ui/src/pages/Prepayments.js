import React, { useEffect, useMemo, useState } from 'react';
import {
  Button,
  Card,
  Col,
  Drawer,
  Empty,
  Input,
  Row,
  Space,
  Statistic,
  Switch,
  Table,
  Tag,
  Typography,
  message,
} from 'antd';
import {
  ReloadOutlined,
  SearchOutlined,
  WalletOutlined,
} from '@ant-design/icons';
import { useQuery, keepPreviousData } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { useLocation, useNavigate } from 'react-router-dom';
import staffService from '../services/staffService';
import { formatLocaleDateTime } from '../utils/dateUtils';
import { extractApiErrorMessage } from '../utils/apiError';

const { Title, Text } = Typography;

// Color palette for allocation modes — mirrors the semantic meaning of each
// mode in the COD reconciliation flow (auto/manual settle COD debts, reserve
// holds prepayment against a pending order, credit applies it on delivery).
const ALLOCATION_MODE_COLORS = new Map([
  ['auto', 'blue'],
  ['manual', 'cyan'],
  ['prepaid_reservation', 'gold'],
  ['prepaid_credit', 'green'],
]);

const SOURCE_COLORS = new Map([
  ['delivery_completion', 'green'],
  ['next_delivery', 'cyan'],
  ['standalone_meeting', 'blue'],
  ['personal_card_transfer', 'geekblue'],
  ['admin_adjustment', 'purple'],
]);

const formatUzs = (value) => `${Number(value || 0).toLocaleString()} UZS`;

const Prepayments = () => {
  const { t } = useTranslation('common');
  const navigate = useNavigate();
  const location = useLocation();

  const [searchText, setSearchText] = useState('');
  const [activeSearch, setActiveSearch] = useState('');
  const [selectedCustomerId, setSelectedCustomerId] = useState(null);
  const [includeVoided, setIncludeVoided] = useState(false);
  const [includeFullyApplied, setIncludeFullyApplied] = useState(true);

  // Allow deep-linking from Users.js: /staff/prepayments?customer_id=123
  useEffect(() => {
    const params = new URLSearchParams(location.search);
    const customerIdParam = params.get('customer_id');
    if (customerIdParam) {
      const parsed = parseInt(customerIdParam, 10);
      if (!Number.isNaN(parsed)) {
        setSelectedCustomerId(parsed);
      }
    }
  }, [location.search]);

  const customersQuery = useQuery({
    queryKey: ['prepayment-customers', activeSearch],
    queryFn: () => staffService.listCustomersWithPrepaymentBalance({
      limit: 200,
      search: activeSearch || undefined,
    }),
    placeholderData: keepPreviousData,
  });

  const historyQuery = useQuery({
    queryKey: ['prepayment-history', selectedCustomerId, includeVoided, includeFullyApplied],
    queryFn: () => staffService.getCustomerPrepaymentHistory(selectedCustomerId, {
      include_voided: includeVoided ? 1 : 0,
      include_fully_applied: includeFullyApplied ? 1 : 0,
      limit: 200,
    }),
    enabled: Boolean(selectedCustomerId),
  });

  useEffect(() => {
    if (customersQuery.error) {
      message.error(extractApiErrorMessage(
        customersQuery.error,
        t('ui.prepayments.list_load_error', 'Failed to load customers with prepayment balance'),
      ));
    }
  }, [customersQuery.error, t]);

  useEffect(() => {
    if (historyQuery.error) {
      message.error(extractApiErrorMessage(
        historyQuery.error,
        t('ui.prepayments.history_load_error', 'Failed to load prepayment history'),
      ));
    }
  }, [historyQuery.error, t]);

  // success_response wraps the payload as {success, data: {items, total}} and
  // axios then nests that under response.data, so the list lives at
  // ...data.data.data.items. Matches the unwrap pattern in DeliveryReports.js
  // and Users.js.
  const customersResponse = customersQuery.data?.data?.data ?? {};
  const customers = useMemo(
    () => customersResponse.items || [],
    [customersResponse.items],
  );
  const totals = useMemo(() => {
    const totalBalance = customers.reduce(
      (sum, row) => sum + Number(row.available_prepayment_balance || 0),
      0,
    );
    return { totalCustomers: customers.length, totalBalance };
  }, [customers]);

  const historyResponse = historyQuery.data?.data?.data ?? null;
  const selectedRow = customers.find((row) => row.id === selectedCustomerId) || null;
  const drawerName = historyResponse
    ? `${historyResponse.first_name || ''} ${historyResponse.last_name || ''}`.trim()
    : (selectedRow
      ? `${selectedRow.first_name || ''} ${selectedRow.last_name || ''}`.trim()
      : '');

  const handleSearchSubmit = () => setActiveSearch(searchText.trim());

  const handleCloseDrawer = () => {
    setSelectedCustomerId(null);
    // Strip the query param so reopening the page later doesn't auto-open.
    if (location.search) {
      navigate(location.pathname, { replace: true });
    }
  };

  const renderSourceTag = (value) => (
    <Tag color={SOURCE_COLORS.get(value) || 'default'} style={{ textTransform: 'capitalize' }}>
      {String(value || '').replace(/_/g, ' ')}
    </Tag>
  );

  const renderAllocationModeTag = (value) => (
    <Tag color={ALLOCATION_MODE_COLORS.get(value) || 'default'} style={{ textTransform: 'capitalize' }}>
      {String(value || '').replace(/_/g, ' ')}
    </Tag>
  );

  const customerColumns = [
    {
      title: t('ui.prepayments.customer', 'Customer'),
      key: 'customer',
      render: (_, row) => (
        <Space direction="vertical" size={0}>
          <Text strong>{`${row.first_name || ''} ${row.last_name || ''}`.trim() || `#${row.id}`}</Text>
          <Text type="secondary" style={{ fontSize: 12 }}>{row.phone}</Text>
        </Space>
      ),
    },
    {
      title: t('ui.prepayments.role', 'Role'),
      dataIndex: 'role',
      key: 'role',
      render: (value) => <Tag>{value}</Tag>,
    },
    {
      title: t('ui.prepayments.balance', 'Prepayment balance'),
      dataIndex: 'available_prepayment_balance',
      key: 'available_prepayment_balance',
      align: 'right',
      sorter: (a, b) => (a.available_prepayment_balance || 0) - (b.available_prepayment_balance || 0),
      defaultSortOrder: 'descend',
      render: (value) => <Text strong style={{ color: '#389e0d' }}>{formatUzs(value)}</Text>,
    },
    {
      title: t('ui.prepayments.last_collection_at', 'Last collection'),
      dataIndex: 'last_collection_at',
      key: 'last_collection_at',
      render: (value) => (value ? formatLocaleDateTime(value) : '—'),
    },
    {
      title: '',
      key: 'actions',
      align: 'right',
      render: (_, row) => (
        <Button type="link" onClick={() => setSelectedCustomerId(row.id)}>
          {t('ui.prepayments.view_ledger', 'View ledger')}
        </Button>
      ),
    },
  ];

  const eventColumns = [
    {
      title: t('ui.prepayments.occurred_at', 'When'),
      dataIndex: 'occurred_at',
      key: 'occurred_at',
      render: (value, row) => (
        <Space direction="vertical" size={0}>
          <Text>{value ? formatLocaleDateTime(value) : '—'}</Text>
          {row.voided_at && (
            <Tag color="red" style={{ marginTop: 2 }}>
              {t('ui.prepayments.voided', 'Voided')}
            </Tag>
          )}
        </Space>
      ),
    },
    {
      title: t('ui.prepayments.source', 'Source'),
      dataIndex: 'source',
      key: 'source',
      render: renderSourceTag,
    },
    {
      title: t('ui.prepayments.collected_amount', 'Collected'),
      dataIndex: 'amount',
      key: 'amount',
      align: 'right',
      render: (value, row) => (
        <Text style={row.voided_at ? { textDecoration: 'line-through', color: '#999' } : undefined}>
          {formatUzs(value)}
        </Text>
      ),
    },
    {
      title: t('ui.prepayments.unapplied_amount', 'Unapplied'),
      dataIndex: 'unapplied_amount',
      key: 'unapplied_amount',
      align: 'right',
      render: (value, row) => (
        <Text
          strong={Number(value) > 0}
          style={row.voided_at ? { color: '#999' } : (Number(value) > 0 ? { color: '#389e0d' } : undefined)}
        >
          {formatUzs(value)}
        </Text>
      ),
    },
    {
      title: t('ui.prepayments.origin_order', 'Origin order'),
      key: 'origin_order',
      render: (_, row) => row.order_number || '—',
    },
    {
      title: t('ui.prepayments.notes', 'Notes'),
      dataIndex: 'notes',
      key: 'notes',
      render: (value) => value || '—',
    },
  ];

  const allocationColumns = [
    {
      title: t('ui.prepayments.allocated_at', 'Allocated at'),
      dataIndex: 'allocated_at',
      key: 'allocated_at',
      render: (value) => (value ? formatLocaleDateTime(value) : '—'),
    },
    {
      title: t('ui.prepayments.allocation_mode', 'Mode'),
      dataIndex: 'allocation_mode',
      key: 'allocation_mode',
      render: renderAllocationModeTag,
    },
    {
      title: t('ui.prepayments.order_number', 'Order'),
      dataIndex: 'order_number',
      key: 'order_number',
      render: (value) => value || '—',
    },
    {
      title: t('ui.prepayments.allocated_amount', 'Amount'),
      dataIndex: 'allocated_amount',
      key: 'allocated_amount',
      align: 'right',
      render: (value, row) => (
        <Text style={row.reversed_at ? { textDecoration: 'line-through', color: '#cf1322' } : undefined}>
          {formatUzs(value)}
        </Text>
      ),
    },
    {
      title: t('ui.prepayments.reversed', 'Reversed'),
      key: 'reversed_at',
      render: (_, row) => (row.reversed_at
        ? (
          <Space direction="vertical" size={0}>
            <Text type="danger">{formatLocaleDateTime(row.reversed_at)}</Text>
            {row.reversal_reason && (
              <Text type="secondary" style={{ fontSize: 12 }}>{row.reversal_reason}</Text>
            )}
          </Space>
        )
        : '—'),
    },
  ];

  return (
    <div style={{ padding: 16 }}>
      <Row align="middle" justify="space-between" style={{ marginBottom: 16 }}>
        <Col>
          <Space align="center">
            <WalletOutlined style={{ fontSize: 22, color: '#389e0d' }} />
            <Title level={3} style={{ margin: 0 }}>
              {t('ui.prepayments.title', 'Customer Prepayments')}
            </Title>
          </Space>
        </Col>
        <Col>
          <Button
            icon={<ReloadOutlined />}
            onClick={() => customersQuery.refetch()}
            loading={customersQuery.isFetching}
          >
            {t('ui.common.refresh', 'Refresh')}
          </Button>
        </Col>
      </Row>

      <Row gutter={[16, 16]} style={{ marginBottom: 16 }}>
        <Col xs={24} sm={12} md={8}>
          <Card size="small">
            <Statistic
              title={t('ui.prepayments.total_customers', 'Customers with balance')}
              value={totals.totalCustomers}
            />
          </Card>
        </Col>
        <Col xs={24} sm={12} md={8}>
          <Card size="small">
            <Statistic
              title={t('ui.prepayments.total_balance', 'Total prepayment balance')}
              value={totals.totalBalance}
              precision={0}
              suffix="UZS"
              valueStyle={{ color: '#389e0d' }}
            />
          </Card>
        </Col>
      </Row>

      <Card size="small" style={{ marginBottom: 16 }}>
        <Space>
          <Input
            allowClear
            placeholder={t('ui.prepayments.search_placeholder', 'Search by name or phone')}
            prefix={<SearchOutlined />}
            value={searchText}
            onChange={(e) => setSearchText(e.target.value)}
            onPressEnter={handleSearchSubmit}
            style={{ width: 280 }}
          />
          <Button type="primary" onClick={handleSearchSubmit}>
            {t('ui.common.search', 'Search')}
          </Button>
          {activeSearch && (
            <Button onClick={() => { setSearchText(''); setActiveSearch(''); }}>
              {t('ui.common.clear', 'Clear')}
            </Button>
          )}
        </Space>
      </Card>

      <Card size="small">
        <Table
          rowKey="id"
          loading={customersQuery.isLoading}
          dataSource={customers}
          columns={customerColumns}
          pagination={{ pageSize: 20, showSizeChanger: true }}
          onRow={(row) => ({ onClick: () => setSelectedCustomerId(row.id) })}
          locale={{ emptyText: <Empty description={t('ui.prepayments.no_customers', 'No customers carry an open prepayment balance.')} /> }}
        />
      </Card>

      <Drawer
        title={drawerName || t('ui.prepayments.customer_ledger', 'Customer ledger')}
        width={920}
        open={Boolean(selectedCustomerId)}
        onClose={handleCloseDrawer}
        destroyOnClose
      >
        {historyQuery.isLoading ? (
          <div style={{ textAlign: 'center', padding: 24 }}>
            {t('ui.common.loading', 'Loading...')}
          </div>
        ) : historyResponse ? (
          <>
            <Row gutter={[16, 16]} style={{ marginBottom: 16 }}>
              <Col xs={24} sm={8}>
                <Card size="small">
                  <Statistic
                    title={t('ui.prepayments.balance', 'Prepayment balance')}
                    value={historyResponse.available_prepayment_balance || 0}
                    precision={0}
                    suffix="UZS"
                    valueStyle={{ color: '#389e0d' }}
                  />
                </Card>
              </Col>
              <Col xs={24} sm={8}>
                <Card size="small">
                  <Statistic
                    title={t('ui.prepayments.lifetime_collected', 'Lifetime collected')}
                    value={historyResponse.lifetime_collected || 0}
                    precision={0}
                    suffix="UZS"
                  />
                </Card>
              </Col>
              <Col xs={24} sm={8}>
                <Card size="small">
                  <Statistic
                    title={t('ui.prepayments.lifetime_applied', 'Lifetime applied')}
                    value={historyResponse.lifetime_applied || 0}
                    precision={0}
                    suffix="UZS"
                  />
                </Card>
              </Col>
            </Row>

            <Card size="small" style={{ marginBottom: 12 }}>
              <Space size="large" wrap>
                <Space>
                  <Switch checked={includeVoided} onChange={setIncludeVoided} />
                  <Text>{t('ui.prepayments.include_voided', 'Include voided')}</Text>
                </Space>
                <Space>
                  <Switch checked={includeFullyApplied} onChange={setIncludeFullyApplied} />
                  <Text>{t('ui.prepayments.include_fully_applied', 'Include fully applied')}</Text>
                </Space>
              </Space>
            </Card>

            <Card
              size="small"
              title={t('ui.prepayments.events_table', 'Cash collection events')}
            >
              <Table
                rowKey="id"
                dataSource={historyResponse.events || []}
                columns={eventColumns}
                pagination={false}
                size="small"
                rowClassName={(row) => (row.voided_at ? 'prepayment-voided-row' : '')}
                expandable={{
                  expandedRowRender: (row) => (
                    <Card size="small" type="inner" title={t('ui.prepayments.allocations', 'Allocations')}>
                      {(row.allocations || []).length > 0 ? (
                        <Table
                          rowKey="id"
                          dataSource={row.allocations}
                          columns={allocationColumns}
                          pagination={false}
                          size="small"
                        />
                      ) : (
                        <Empty description={t('ui.prepayments.no_allocations', 'No allocations yet — this event sits as available prepayment.')} />
                      )}
                    </Card>
                  ),
                  rowExpandable: () => true,
                }}
                locale={{ emptyText: <Empty description={t('ui.prepayments.no_events', 'No cash collection events match these filters.')} /> }}
              />
            </Card>
          </>
        ) : (
          <Empty description={t('ui.prepayments.no_history', 'No prepayment activity for this customer.')} />
        )}
      </Drawer>
    </div>
  );
};

export default Prepayments;
