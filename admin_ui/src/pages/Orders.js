import { useMemo, useState } from 'react';
import {
  Table,
  Card,
  Input,
  Button,
  Space,
  Tag,
  Dropdown,
  Modal,
  Form,
  Select,
  DatePicker,
  Row,
  Col,
  Statistic,
  message,
  Descriptions,
  Divider,
  Spin,
  Alert,
  Switch,
  InputNumber,
} from 'antd';
import {
  ShoppingCartOutlined,
  MoreOutlined,
  ExportOutlined,
  EyeOutlined,
  EditOutlined,
  DollarOutlined,
  PlusOutlined,
  UserOutlined,
  MinusCircleOutlined,
  ReloadOutlined,
  LinkOutlined,
  BarcodeOutlined,
} from '@ant-design/icons';
import { useQuery, useMutation, useQueryClient, keepPreviousData } from '@tanstack/react-query';
import { formatDate, formatDateTimeShort } from '../utils/dateUtils';
import adminService from '../services/adminService';
import api from '../services/api';
import { useTranslation } from 'react-i18next';
import { extractApiErrorMessages } from '../utils/apiError';
import AsyncButton from '../components/common/AsyncButton';
import EmptyState from '../components/common/EmptyState';

const { Option } = Select;
const { RangePicker } = DatePicker;

const paymentStatusColor = (status) => {
  if (status === 'completed') return 'green';
  if (['pending', 'partially_paid'].includes(status)) return 'orange';
  if (status === 'not_required') return 'default';
  return 'red';
};

const fiscalizationStatusColor = (status) => {
  if (status === 'completed') return 'green';
  if (status === 'processing') return 'processing';
  if (status === 'not_required') return 'default';
  if (status === 'failed') return 'red';
  return 'orange';
};

const getOrderStatusColor = (status) => {
  switch (status) {
    case 'pending':
      return 'orange';
    case 'confirmed':
      return 'blue';
    case 'preparing':
      return 'cyan';
    case 'out_for_delivery':
      return 'purple';
    case 'delivered':
      return 'green';
    case 'cancelled':
      return 'red';
    case 'returned':
      return 'volcano';
    default:
      return 'default';
  }
};

const getMarkingActionColor = (action) => {
  switch (action) {
    case 'reserved':
      return 'blue';
    case 'used':
      return 'geekblue';
    case 'utilised':
      return 'green';
    case 'released':
      return 'orange';
    case 'created':
      return 'cyan';
    case 'imported':
      return 'purple';
    case 'restored':
      return 'gold';
    case 'archived':
    default:
      return 'default';
  }
};

const humanizeAuditAction = (value) =>
  value ? String(value).replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase()) : '—';

const Orders = () => {
  const { t } = useTranslation('orders');
  const queryClient = useQueryClient();

  const [searchText, setSearchText] = useState('');
  const [statusFilter, setStatusFilter] = useState('');
  const [dateRange, setDateRange] = useState(null);
  const [selectedOrder, setSelectedOrder] = useState(null);
  const [isDetailModalVisible, setIsDetailModalVisible] = useState(false);
  const [isStatusModalVisible, setIsStatusModalVisible] = useState(false);
  const [isCreateModalVisible, setIsCreateModalVisible] = useState(false);
  const [selectedUserId, setSelectedUserId] = useState(null);
  const [userAddresses, setUserAddresses] = useState([]);
  const [userPaymentMethods, setUserPaymentMethods] = useState([]);
  const [paymentRestrictions, setPaymentRestrictions] = useState(null);
  const [paymentMethodsLoading, setPaymentMethodsLoading] = useState(false);
  const [pagination, setPagination] = useState({ page: 1, per_page: 20 });
  const [orderDetailsLoading, setOrderDetailsLoading] = useState(false);
  const [createOrderErrors, setCreateOrderErrors] = useState([]);
  const [isPersonalCardModalVisible, setIsPersonalCardModalVisible] = useState(false);

  const [statusForm] = Form.useForm();
  const [createOrderForm] = Form.useForm();
  const [personalCardForm] = Form.useForm();
  const watchedPaymentMethod = Form.useWatch('payment_method', createOrderForm);
  const watchedStatusValue = Form.useWatch('status', statusForm);

  const { data, isLoading } = useQuery({
    queryKey: ['orders', pagination, searchText, statusFilter, dateRange],

    queryFn: () =>
      adminService.getOrders({
        page: pagination.page,
        per_page: pagination.per_page,
        search: searchText,
        status: statusFilter,
        start_date: dateRange?.[0]?.format('YYYY-MM-DD'),
        end_date: dateRange?.[1]?.format('YYYY-MM-DD'),
      }),

    placeholderData: keepPreviousData,
  });

  const { data: usersData } = useQuery({
    queryKey: ['users-for-order'],
    queryFn: () => adminService.getUsers({ per_page: 100 }),
    enabled: isCreateModalVisible,
  });

  const { data: productsData } = useQuery({
    queryKey: ['products-for-order', selectedUserId],

    queryFn: () =>
      adminService.getProducts({
        per_page: 100,
        is_active: true,
        ...(selectedUserId ? { pricing_user_id: selectedUserId } : {}),
      }),

    enabled: isCreateModalVisible,
  });

  const { data: statusesData } = useQuery({
    queryKey: ['order-statuses'],

    queryFn: async () => {
      const response = await api.get('/orders/statuses');
      return response.data;
    },

    staleTime: 1000 * 60 * 60 * 24,
  });
  const orderStatuses = statusesData?.data?.statuses || [];

  const updateOrderMutation = useMutation({
    mutationFn: ({ orderId, status, notes, bottles_returned }) => adminService.updateOrderStatus(orderId, status, notes, { bottles_returned }),

    onSuccess: () => {
      message.success(t('ui.orders.status_updated_success', 'Order status updated successfully'));
      queryClient.invalidateQueries({
        queryKey: ['orders'],
      });
      setIsStatusModalVisible(false);
      statusForm.resetFields();
    },

    onError: (error) => {
      const errors = extractApiErrorMessages(error, t('ui.orders.status_update_failed', 'Failed to update order status'));
      message.error(errors[0]);
    },
  });

  const createOrderMutation = useMutation({
    mutationFn: (orderData) => adminService.createOrderForUser(orderData),

    onSuccess: (response) => {
      message.success(t('ui.orders.order_created_success', 'Order created successfully'));
      queryClient.invalidateQueries({
        queryKey: ['orders'],
      });
      setIsCreateModalVisible(false);
      createOrderForm.resetFields();
      setCreateOrderErrors([]);
      setSelectedUserId(null);
      setUserAddresses([]);
      setUserPaymentMethods([]);
      setPaymentRestrictions(null);

      const paymentUrl = response?.data?.payment_url;
      if (paymentUrl) {
        Modal.success({
          title: t('ui.orders.payment_link_ready', 'Payment link created'),
          content: (
            <a href={paymentUrl} target="_blank" rel="noreferrer">
              {paymentUrl}
            </a>
          ),
        });
      }
    },

    onError: (error) => {
      const errorMessages = extractApiErrorMessages(
        error,
        t('ui.orders.order_create_failed', 'Failed to create order'),
      );
      setCreateOrderErrors(errorMessages);
      message.error(errorMessages[0]);
    },
  });

  const recordPersonalCardPaymentMutation = useMutation({
    mutationFn: (payload) => adminService.recordStaffCashCollection(payload),

    onSuccess: async () => {
      message.success(t('ui.orders.personal_card_payment_recorded', 'Personal card payment recorded'));
      queryClient.invalidateQueries({
        queryKey: ['orders'],
      });
      setIsPersonalCardModalVisible(false);
      personalCardForm.resetFields();

      if (selectedOrder?.id) {
        try {
          const response = await adminService.getOrderDetails(selectedOrder.id);
          if (response.success && response.data?.order) {
            setSelectedOrder(response.data.order);
          }
        } catch (_error) {
          // Keep the current modal state when refresh fails.
        }
      }
    },

    onError: (error) => {
      const errorMessages = extractApiErrorMessages(
        error,
        t('ui.orders.personal_card_payment_failed', 'Failed to record personal card payment'),
      );
      message.error(errorMessages[0]);
    },
  });

  const retryFiscalizationMutation = useMutation({
    mutationFn: (paymentId) => adminService.retryPaymentFiscalization(paymentId),

    onSuccess: async () => {
      message.success(t('ui.orders.fiscalization_retry_success', 'Fiscalization retry queued successfully'));
      queryClient.invalidateQueries({
        queryKey: ['orders'],
      });
      if (selectedOrder?.id) {
        const response = await adminService.getOrderDetails(selectedOrder.id);
        if (response.success && response.data?.order) {
          setSelectedOrder(response.data.order);
        }
      }
    },

    onError: (error) => {
      const errors = extractApiErrorMessages(error, t('ui.orders.fiscalization_retry_failed', 'Failed to retry fiscalization'));
      message.error(errors[0]);
    },
  });

  const handleUserSelect = async (userId) => {
    setSelectedUserId(userId);
    createOrderForm.setFieldsValue({
      delivery_address_id: undefined,
      consume_marking_codes: false,
    });

    if (!userId) {
      setUserAddresses([]);
      setUserPaymentMethods([]);
      setPaymentRestrictions(null);
      return;
    }

    setPaymentMethodsLoading(true);
    try {
      const [addressResponse, paymentResponse] = await Promise.all([
        adminService.getUserAddresses(userId),
        adminService.getUserPaymentMethods(userId),
      ]);
      const addresses = addressResponse.data?.addresses || [];
      const paymentPayload = paymentResponse.data || {};
      const availableMethods = paymentPayload.available_methods || [];

      setUserAddresses(addresses);
      setUserPaymentMethods(availableMethods);
      setPaymentRestrictions(paymentPayload.payment_restrictions || null);
      createOrderForm.setFieldsValue({
        payment_method: availableMethods[0]?.method,
        consume_marking_codes: false,
      });
    } catch (error) {
      message.error(t('ui.orders.load_customer_context_failed', 'Failed to load customer payment context'));
      setUserAddresses([]);
      setUserPaymentMethods([]);
      setPaymentRestrictions(null);
    } finally {
      setPaymentMethodsLoading(false);
    }
  };

  const handleCreateOrderSubmit = (values) => {
    const allowedMethods = userPaymentMethods.map((method) => method.method);
    if (allowedMethods.length > 0 && !allowedMethods.includes(values.payment_method)) {
      message.error(t('ui.orders.payment_method_unavailable', 'Selected payment method is not available for this user'));
      return;
    }

    setCreateOrderErrors([]);
    createOrderMutation.mutate({
      user_id: values.user_id,
      delivery_address_id: values.delivery_address_id,
      payment_method: values.payment_method || 'cash',
      delivery_notes: values.delivery_notes || '',
      consume_marking_codes: values.payment_method === 'business_account' ? Boolean(values.consume_marking_codes) : false,
      items: values.items.map((item) => ({
        product_id: item.product_id,
        quantity: item.quantity,
      })),
    });
  };

  const handleViewOrder = async (order) => {
    setSelectedOrder(order);
    setIsDetailModalVisible(true);
    setOrderDetailsLoading(true);

    try {
      const response = await adminService.getOrderDetails(order.id);
      if (response.success && response.data?.order) {
        setSelectedOrder(response.data.order);
      }
    } catch (error) {
      // Keep lightweight table data if the detail request fails.
    } finally {
      setOrderDetailsLoading(false);
    }
  };

  const handleUpdateStatus = (order) => {
    setSelectedOrder(order);
    statusForm.setFieldsValue({
      status: order.status,
      notes: '',
    });
    setIsStatusModalVisible(true);
  };

  const handleCancelOrder = (order) => {
    Modal.confirm({
      title: t('ui.orders.cancel_order_title', 'Cancel order'),
      content: `${t('ui.orders.cancel_order_confirm', 'Cancel order')} ${order.order_number}?`,
      onOk: () => {
        updateOrderMutation.mutate({
          orderId: order.id,
          status: 'cancelled',
          notes: t('ui.orders.cancelled_by_admin', 'Cancelled by admin'),
        });
      },
    });
  };

  const orders = data?.data?.items || [];
  const totalRevenue = orders
    .filter((order) => !['cancelled', 'refunded'].includes(order.status))
    .reduce((sum, order) => sum + (order.total_amount || 0), 0);
  const pendingOrders = orders.filter((order) => order.status === 'pending').length;
  const clickOrders = orders.filter((order) => ['click', 'card'].includes(order.payment_provider || order.payment_method)).length;

  const getEffectiveProductPrice = (product) => {
    if (selectedUserId && product?.effective_unit_price !== undefined && product?.effective_unit_price !== null) {
      return product.effective_unit_price;
    }
    return product?.price;
  };

  const selectedOrderFiscalization = selectedOrder?.fiscalization || null;
  const selectedOrderMarkingSummary = useMemo(
    () => selectedOrder?.marking_code_summary || { events: {}, codes_by_order_item: {} },
    [selectedOrder?.marking_code_summary],
  );
  const selectedOrderPaymentTransactions = selectedOrder?.payment_transactions || [];
  const selectedOrderClickCallbacks = selectedOrder?.click_callback_history || [];
  const selectedOrderFiscalizationTrail = selectedOrder?.fiscalization_audit_trail || [];
  const selectedOrderMarkingActivity = selectedOrder?.marking_code_activity || [];

  const orderColumns = [
    {
      title: t('ui.orders.order_number', 'Order Number'),
      dataIndex: 'order_number',
      key: 'order_number',
      width: 140,
      render: (text) => <span style={{ fontFamily: 'monospace', fontWeight: 600 }}>{text}</span>,
    },
    {
      title: t('ui.orders.customer', 'Customer'),
      dataIndex: 'customer',
      key: 'customer',
      render: (_, record) => (
        <div>
          <div>{record.customer_name}</div>
          <small style={{ color: '#666' }}>{record.customer_email}</small>
        </div>
      ),
    },
    {
      title: t('ui.orders.items', 'Items'),
      dataIndex: 'items_summary',
      key: 'items_summary',
      width: 220,
      render: (items, record) => {
        if (!items || items.length === 0) {
          return <Tag color="blue">{record.items_count || 0} {t('ui.orders.items_count', 'items')}</Tag>;
        }
        return (
          <div style={{ fontSize: 12 }}>
            {items.slice(0, 2).map((item) => (
              <div key={`${item.product_id || 'product'}-${item.product_name || 'item'}`} style={{ whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                {item.quantity}x {item.product_name}
              </div>
            ))}
            {items.length > 2 ? <span style={{ color: '#999' }}>+{items.length - 2} {t('ui.orders.more_items', 'more')}</span> : null}
          </div>
        );
      },
    },
    {
      title: t('ui.orders.total_amount', 'Total Amount'),
      dataIndex: 'total_amount',
      key: 'total_amount',
      width: 130,
      render: (amount) => <span style={{ fontWeight: 600, color: '#52c41a' }}>{Number(amount || 0).toLocaleString()} UZS</span>,
    },
    {
      title: t('ui.orders.status', 'Status'),
      dataIndex: 'status',
      key: 'status',
      width: 120,
      render: (status) => <Tag color={getOrderStatusColor(status)}>{t(`ui.orders.status_${status}`, status)}</Tag>,
    },
    {
      title: t('ui.orders.payment', 'Payment'),
      dataIndex: 'payment_status',
      key: 'payment_status',
      width: 120,
      render: (status) => <Tag color={paymentStatusColor(status)}>{t(`ui.orders.payment_${status}`, status || 'pending')}</Tag>,
    },
    {
      title: t('ui.orders.payment_provider', 'Provider'),
      dataIndex: 'payment_provider',
      key: 'payment_provider',
      width: 130,
      render: (value, record) => (value || record.payment_method || '—'),
    },
    {
      title: t('ui.orders.order_date', 'Order Date'),
      dataIndex: 'created_at',
      key: 'created_at',
      width: 140,
      render: (date) => formatDate(date),
    },
    {
      title: t('ui.orders.actions', 'Actions'),
      key: 'actions',
      width: 100,
      render: (_, record) => (
        <Dropdown
          trigger={['click']}
          menu={{
            items: [
              {
                key: 'view',
                label: t('ui.orders.view_details', 'View Details'),
                icon: <EyeOutlined />,
                onClick: () => handleViewOrder(record),
              },
              {
                key: 'status',
                label: t('ui.orders.update_status', 'Update Status'),
                icon: <EditOutlined />,
                onClick: () => handleUpdateStatus(record),
              },
              { type: 'divider' },
              {
                key: 'cancel',
                label: t('ui.orders.cancel_order', 'Cancel Order'),
                danger: true,
                disabled: ['delivered', 'cancelled'].includes(record.status),
                onClick: () => handleCancelOrder(record),
              },
            ],
          }}
        >
          <Button type="text" icon={<MoreOutlined />} />
        </Dropdown>
      ),
    },
  ];

  const createOrderReset = () => {
    setIsCreateModalVisible(false);
    createOrderForm.resetFields();
    setCreateOrderErrors([]);
    setSelectedUserId(null);
    setUserAddresses([]);
    setUserPaymentMethods([]);
    setPaymentRestrictions(null);
  };

  const markingCodeRows = useMemo(() => {
    const entries = Object.entries(selectedOrderMarkingSummary.codes_by_order_item || {});
    return entries.map(([orderItemId, codes]) => ({
      orderItemId,
      codes,
    }));
  }, [selectedOrderMarkingSummary]);

  return (
    <div>
      <Row gutter={[16, 16]} style={{ marginBottom: 24 }}>
        <Col xs={24} sm={8}>
          <Card>
            <Statistic title={t('ui.orders.total_orders', 'Total Orders')} value={data?.meta?.total || 0} prefix={<ShoppingCartOutlined />} />
          </Card>
        </Col>
        <Col xs={24} sm={8}>
          <Card>
            <Statistic title={t('ui.orders.total_revenue', 'Total Revenue')} value={totalRevenue} precision={2} prefix={<DollarOutlined />} suffix="UZS" />
          </Card>
        </Col>
        <Col xs={24} sm={8}>
          <Card>
            <Statistic title={t('ui.orders.click_orders', 'Click/Card Orders')} value={clickOrders} prefix={<BarcodeOutlined />} />
          </Card>
        </Col>
      </Row>

      <Card style={{ marginBottom: 24 }}>
        <Statistic title={t('ui.orders.pending_orders', 'Pending Orders')} value={pendingOrders} valueStyle={{ color: '#faad14' }} />
      </Card>

      <Card>
        <div className="table-actions">
          <Space wrap>
            <Input.Search
              placeholder={t('ui.orders.search_placeholder', 'Search orders')}
              allowClear
              onSearch={(value) => {
                setSearchText(value);
                setPagination((current) => ({ ...current, page: 1 }));
              }}
              style={{ width: 250 }}
            />
            <Select
              placeholder={t('ui.orders.filter_by_status', 'Filter by status')}
              allowClear
              onChange={(value) => {
                setStatusFilter(value || '');
                setPagination((current) => ({ ...current, page: 1 }));
              }}
              style={{ width: 180 }}
            >
              {orderStatuses.map((status) => (
                <Option key={status.value} value={status.value}>
                  {t(`ui.orders.status_${status.value}`, status.label)}
                </Option>
              ))}
            </Select>
            <RangePicker
              onChange={(dates) => {
                setDateRange(dates);
                setPagination((current) => ({ ...current, page: 1 }));
              }}
              format="YYYY-MM-DD"
              placeholder={[t('ui.orders.start_date', 'Start date'), t('ui.orders.end_date', 'End date')]}
            />
          </Space>

          <Space>
            <Button icon={<ExportOutlined />} disabled>
              {t('ui.orders.export_orders', 'Export Orders')}
            </Button>
            <Button type="primary" icon={<PlusOutlined />} onClick={() => setIsCreateModalVisible(true)}>
              {t('ui.orders.create_order', 'Create Order')}
            </Button>
          </Space>
        </div>

        <Table
          columns={orderColumns}
          dataSource={orders}
          loading={isLoading}
          rowKey="id"
          locale={{
            emptyText: <EmptyState description={t('ui.orders.no_orders', 'No orders found')} />,
          }}
          pagination={{
            current: pagination.page,
            pageSize: pagination.per_page,
            total: data?.meta?.total || 0,
            showSizeChanger: true,
            showQuickJumper: true,
            showTotal: (total, range) => `${range[0]}-${range[1]} of ${total} ${t('ui.orders.pagination_text', 'orders')}`,
          }}
          onChange={(paginationInfo) => {
            setPagination({
              page: paginationInfo.current,
              per_page: paginationInfo.pageSize,
            });
          }}
          className="admin-table"
          scroll={{ x: 1200 }}
        />
      </Card>

      <Modal
        title={`${t('ui.orders.order_details', 'Order Details')} - ${selectedOrder?.order_number || ''}`}
        open={isDetailModalVisible}
        onCancel={() => setIsDetailModalVisible(false)}
        footer={null}
        width={980}
      >
        {selectedOrder ? (
          <div>
            <Descriptions column={2} bordered>
              <Descriptions.Item label={t('ui.orders.order_number', 'Order Number')}>
                {selectedOrder.order_number}
              </Descriptions.Item>
              <Descriptions.Item label={t('ui.orders.status', 'Status')}>
                <Tag color={getOrderStatusColor(selectedOrder.status)}>
                  {t(`ui.orders.status_${selectedOrder.status}`, selectedOrder.status)}
                </Tag>
              </Descriptions.Item>
              <Descriptions.Item label={t('ui.orders.customer', 'Customer')}>
                {selectedOrder.customer_name}
              </Descriptions.Item>
              <Descriptions.Item label={t('ui.orders.email', 'Email')}>
                {selectedOrder.customer_email || '—'}
              </Descriptions.Item>
              <Descriptions.Item label={t('ui.orders.phone', 'Phone')}>
                {selectedOrder.customer_phone || '—'}
              </Descriptions.Item>
              <Descriptions.Item label={t('ui.orders.total_amount', 'Total Amount')}>
                <span style={{ fontWeight: 600, color: '#52c41a' }}>{Number(selectedOrder.total_amount || 0).toLocaleString()} UZS</span>
              </Descriptions.Item>
              <Descriptions.Item label={t('ui.orders.payment_status', 'Payment Status')}>
                <Tag color={paymentStatusColor(selectedOrder.payment_status)}>
                  {t(`ui.orders.payment_${selectedOrder.payment_status}`, selectedOrder.payment_status || 'pending')}
                </Tag>
              </Descriptions.Item>
              <Descriptions.Item label={t('ui.orders.payment_method', 'Payment Method')}>
                {selectedOrder.payment_method || '—'}
              </Descriptions.Item>
              <Descriptions.Item label={t('ui.orders.payment_provider', 'Payment Provider')}>
                {selectedOrder.payment_provider || '—'}
              </Descriptions.Item>
              <Descriptions.Item label={t('ui.orders.provider_transaction_id', 'Provider Transaction ID')}>
                {selectedOrder.provider_transaction_id || '—'}
              </Descriptions.Item>
              <Descriptions.Item label={t('ui.orders.order_date', 'Order Date')}>
                {formatDateTimeShort(selectedOrder.created_at)}
              </Descriptions.Item>
            </Descriptions>

            <Divider>{t('ui.orders.payment_summary', 'Payment Summary')}</Divider>
            <Descriptions column={3} bordered size="small">
              <Descriptions.Item label={t('ui.orders.total_amount', 'Total Amount')}>
                {Number(selectedOrder.total_amount || 0).toLocaleString()} UZS
              </Descriptions.Item>
              <Descriptions.Item label={t('ui.orders.amount_collected', 'Collected')}>
                {Number(selectedOrder.amount_collected || 0).toLocaleString()} UZS
              </Descriptions.Item>
              <Descriptions.Item label={t('ui.orders.outstanding_amount', 'Outstanding')}>
                {Number(selectedOrder.outstanding_amount || 0).toLocaleString()} UZS
              </Descriptions.Item>
            </Descriptions>

            <Divider>{t('ui.orders.fiscalization', 'Fiscalization')}</Divider>
            <Descriptions column={2} bordered size="small">
              <Descriptions.Item label={t('ui.orders.fiscalization_status', 'Fiscalization Status')}>
                <Tag color={fiscalizationStatusColor(selectedOrder.fiscalization_status)}>
                  {t(`ui.orders.fiscalization_${selectedOrder.fiscalization_status}`, selectedOrder.fiscalization_status || 'pending')}
                </Tag>
              </Descriptions.Item>
              <Descriptions.Item label={t('ui.orders.consume_marking_codes', 'Consume Marking Codes')}>
                {selectedOrder.consume_marking_codes ? t('ui.common.yes', 'Yes') : t('ui.common.no', 'No')}
              </Descriptions.Item>
              <Descriptions.Item label={t('ui.orders.payment_link', 'Payment Link')}>
                {selectedOrder.payment_link ? (
                  <a href={selectedOrder.payment_link} target="_blank" rel="noreferrer">
                    {t('ui.orders.open_payment_link', 'Open payment link')}
                  </a>
                ) : '—'}
              </Descriptions.Item>
              <Descriptions.Item label={t('ui.orders.receipt_link', 'Receipt Link')}>
                {selectedOrderFiscalization?.provider_receipt_url ? (
                  <a href={selectedOrderFiscalization.provider_receipt_url} target="_blank" rel="noreferrer">
                    {t('ui.orders.open_receipt', 'Open receipt')}
                  </a>
                ) : '—'}
              </Descriptions.Item>
              <Descriptions.Item label={t('ui.orders.receipt_id', 'Receipt ID')}>
                {selectedOrderFiscalization?.provider_receipt_id || '—'}
              </Descriptions.Item>
              <Descriptions.Item label={t('ui.orders.fiscalization_failure_reason', 'Failure Reason')}>
                {selectedOrderFiscalization?.failure_reason || '—'}
              </Descriptions.Item>
            </Descriptions>

            {selectedOrder?.payment_provider && ['click', 'card'].includes(selectedOrder.payment_provider) && selectedOrder.fiscalization_status !== 'completed' && selectedOrder.fiscalization_status !== 'not_required' ? (
              <div style={{ marginTop: 16 }}>
                <AsyncButton
                  icon={<ReloadOutlined />}
                  disabled={!selectedOrder.payment_id}
                  loading={retryFiscalizationMutation.isPending}
                  onClick={() => retryFiscalizationMutation.mutateAsync(selectedOrder.payment_id)}
                >
                  {t('ui.orders.retry_fiscalization', 'Retry Fiscalization')}
                </AsyncButton>
              </div>
            ) : null}

            <Divider>{t('ui.orders.fiscalization_audit_trail', 'Fiscalization Audit Trail')}</Divider>
            {selectedOrderFiscalizationTrail.length ? (
              <Table
                dataSource={selectedOrderFiscalizationTrail}
                rowKey={(record) => `${record.action || 'event'}-${record.occurred_at || 'unknown'}-${record.actor_user_id ?? 'na'}`}
                pagination={false}
                size="small"
                columns={[
                  {
                    title: t('ui.orders.audit_action', 'Action'),
                    dataIndex: 'action',
                    key: 'action',
                    render: (value) => humanizeAuditAction(value),
                  },
                  {
                    title: t('ui.orders.audit_status', 'Status'),
                    dataIndex: 'success',
                    key: 'success',
                    render: (value) => (
                      <Tag color={value ? 'green' : 'red'}>
                        {value ? t('ui.common.success', 'Success') : t('ui.common.failed', 'Failed')}
                      </Tag>
                    ),
                  },
                  {
                    title: t('ui.orders.receipt_id', 'Receipt ID'),
                    key: 'provider_receipt_id',
                    render: (_, record) => record?.additional_data?.provider_receipt_id || '—',
                  },
                  {
                    title: t('ui.orders.error', 'Error'),
                    dataIndex: 'error_message',
                    key: 'error_message',
                    render: (value) => value || '—',
                  },
                  {
                    title: t('ui.orders.time', 'Time'),
                    dataIndex: 'occurred_at',
                    key: 'occurred_at',
                    render: (value) => formatDateTimeShort(value),
                  },
                ]}
              />
            ) : (
              <Alert type="info" showIcon message={t('ui.orders.no_fiscalization_audit_trail', 'No fiscalization audit trail recorded yet')} />
            )}

            <Divider>{t('ui.orders.payment_transactions', 'Payment Transactions')}</Divider>
            {selectedOrderPaymentTransactions.length ? (
              <Table
                dataSource={selectedOrderPaymentTransactions}
                rowKey="id"
                pagination={false}
                size="small"
                columns={[
                  {
                    title: t('ui.orders.transaction_type', 'Type'),
                    dataIndex: 'transaction_type',
                    key: 'transaction_type',
                  },
                  {
                    title: t('ui.orders.transaction_status', 'Status'),
                    dataIndex: 'status',
                    key: 'status',
                    render: (value, record) => (
                      <Tag color={record?.success ? 'green' : 'red'}>
                        {value || '—'}
                      </Tag>
                    ),
                  },
                  {
                    title: t('ui.orders.provider_transaction_id', 'Provider Transaction ID'),
                    dataIndex: 'provider_transaction_id',
                    key: 'provider_transaction_id',
                    render: (value) => value || '—',
                  },
                  {
                    title: t('ui.orders.notes', 'Notes'),
                    key: 'notes',
                    render: (_, record) => record.failure_reason || record.notes || '—',
                  },
                  {
                    title: t('ui.orders.time', 'Time'),
                    dataIndex: 'created_at',
                    key: 'created_at',
                    render: (value) => formatDateTimeShort(value),
                  },
                ]}
              />
            ) : (
              <Alert type="info" showIcon message={t('ui.orders.no_payment_transactions', 'No payment transactions recorded yet')} />
            )}

            <Divider>{t('ui.orders.click_callback_history', 'Click Callback History')}</Divider>
            {selectedOrderClickCallbacks.length ? (
              <Table
                dataSource={selectedOrderClickCallbacks}
                rowKey={(record) => `${record.stage || 'callback'}-${record.received_at || 'unknown'}-${record?.response?.error ?? 'na'}`}
                pagination={false}
                size="small"
                columns={[
                  {
                    title: t('ui.orders.callback_stage', 'Stage'),
                    dataIndex: 'stage',
                    key: 'stage',
                  },
                  {
                    title: t('ui.orders.callback_result', 'Result'),
                    key: 'result',
                    render: (_, record) => {
                      const responseError = record?.response?.error;
                      if (responseError === 0) {
                        return <Tag color="green">{t('ui.common.success', 'Success')}</Tag>;
                      }
                      if (responseError !== undefined && responseError !== null) {
                        return <Tag color="red">{`${responseError}`}</Tag>;
                      }
                      return '—';
                    },
                  },
                  {
                    title: t('ui.orders.callback_note', 'Note'),
                    key: 'note',
                    render: (_, record) => record?.response?.error_note || record?.request?.error_note || '—',
                  },
                  {
                    title: t('ui.orders.time', 'Time'),
                    dataIndex: 'received_at',
                    key: 'received_at',
                    render: (value) => formatDateTimeShort(value),
                  },
                ]}
              />
            ) : (
              <Alert type="info" showIcon message={t('ui.orders.no_click_callbacks', 'No Click callback history recorded yet')} />
            )}

            <Divider>{t('ui.orders.marking_code_summary', 'Marking-Code Summary')}</Divider>
            <Row gutter={[16, 16]}>
              {Object.entries(selectedOrderMarkingSummary.events || {}).length ? (
                Object.entries(selectedOrderMarkingSummary.events || {}).map(([event, count]) => (
                  <Col xs={12} md={6} key={event}>
                    <Card>
                      <Statistic title={t(`ui.orders.marking_code_event_${event}`, event)} value={count} />
                    </Card>
                  </Col>
                ))
              ) : (
                <Col span={24}>
                  <Alert type="info" showIcon message={t('ui.orders.no_marking_code_activity', 'No marking-code activity recorded for this order')} />
                </Col>
              )}
            </Row>

            {markingCodeRows.length ? (
              <Table
                style={{ marginTop: 16 }}
                dataSource={markingCodeRows}
                rowKey="orderItemId"
                pagination={false}
                size="small"
                columns={[
                  {
                    title: t('ui.orders.order_item', 'Order Item'),
                    dataIndex: 'orderItemId',
                    key: 'orderItemId',
                    render: (value) => `#${value}`,
                  },
                  {
                    title: t('ui.orders.marking_codes', 'Marking Codes'),
                    dataIndex: 'codes',
                    key: 'codes',
                    render: (codes) => (
                      <Space wrap>
                        <Tag color="blue">
                          {t('ui.orders.marking_codes_count', '{{count}} codes').replace('{{count}}', (codes || []).length)}
                        </Tag>
                        {(codes || []).map((code) => (
                          <Tag key={code} style={{ fontFamily: 'monospace' }}>{code}</Tag>
                        ))}
                      </Space>
                    ),
                  },
                ]}
              />
            ) : null}

            <Divider>{t('ui.orders.marking_code_activity', 'Marking-Code Activity')}</Divider>
            {selectedOrderMarkingActivity.length ? (
              <Table
                dataSource={selectedOrderMarkingActivity}
                rowKey="id"
                pagination={false}
                size="small"
                columns={[
                  {
                    title: t('ui.orders.action', 'Action'),
                    dataIndex: 'action',
                    key: 'action',
                    render: (value) => value ? (
                      <Tag color={getMarkingActionColor(value)}>
                        {t(`ui.orders.marking_code_event_${value}`, value)}
                      </Tag>
                    ) : '—',
                  },
                  {
                    title: t('ui.orders.marking_code', 'Marking Code'),
                    dataIndex: 'code',
                    key: 'code',
                    render: (value) => value ? <Tag style={{ fontFamily: 'monospace' }}>{value}</Tag> : '—',
                  },
                  {
                    title: t('ui.orders.order_item', 'Order Item'),
                    dataIndex: 'order_item_id',
                    key: 'order_item_id',
                    render: (value) => `#${value}`,
                  },
                  {
                    title: t('ui.orders.notes', 'Notes'),
                    key: 'notes',
                    render: (_, record) => record.notes || record?.event_metadata?.reason || '—',
                  },
                  {
                    title: t('ui.orders.time', 'Time'),
                    dataIndex: 'occurred_at',
                    key: 'occurred_at',
                    render: (value) => formatDateTimeShort(value),
                  },
                ]}
              />
            ) : (
              <Alert type="info" showIcon message={t('ui.orders.no_marking_code_activity', 'No marking-code activity recorded for this order')} />
            )}

            <Divider>{t('ui.orders.order_items', 'Order Items')}</Divider>
            <Spin spinning={orderDetailsLoading}>
              <Table
                dataSource={selectedOrder.items || selectedOrder.items_summary || []}
                rowKey={(record) => record.id || `${record.product_id || 'product'}-${record.product_name || 'item'}`}
                pagination={false}
                size="small"
                columns={[
                  {
                    title: t('ui.orders.product_name', 'Product'),
                    dataIndex: 'product_name',
                    key: 'product_name',
                  },
                  {
                    title: t('ui.orders.quantity', 'Qty'),
                    dataIndex: 'quantity',
                    key: 'quantity',
                    width: 80,
                    align: 'center',
                  },
                  {
                    title: t('ui.orders.unit_price', 'Unit Price'),
                    dataIndex: 'unit_price',
                    key: 'unit_price',
                    width: 140,
                    align: 'right',
                    render: (price) => `${Number(price || 0).toLocaleString()} UZS`,
                  },
                  {
                    title: t('ui.orders.total_price', 'Total'),
                    dataIndex: 'total_price',
                    key: 'total_price',
                    width: 140,
                    align: 'right',
                    render: (price) => <span style={{ fontWeight: 600 }}>{Number(price || 0).toLocaleString()} UZS</span>,
                  },
                ]}
                footer={() => (
                  <div style={{ textAlign: 'right' }}>
                    <strong>{t('ui.orders.order_total', 'Order Total')}: </strong>
                    <span style={{ fontSize: 16, color: '#52c41a', fontWeight: 600 }}>
                      {Number(selectedOrder.total_amount || 0).toLocaleString()} UZS
                    </span>
                  </div>
                )}
              />
            </Spin>

            {selectedOrder.payment_timeline?.timeline?.length ? (
              <>
                <Divider>{t('ui.orders.payment_timeline', 'Payment Timeline')}</Divider>
                <Table
                  dataSource={selectedOrder.payment_timeline.timeline}
                  rowKey={(record) => `${record.type}-${record.timestamp || record.notes || 'row'}`}
                  pagination={false}
                  size="small"
                  columns={[
                    {
                      title: t('ui.orders.timeline_type', 'Type'),
                      dataIndex: 'type',
                      key: 'type',
                    },
                    {
                      title: t('ui.orders.timeline_timestamp', 'Timestamp'),
                      dataIndex: 'timestamp',
                      key: 'timestamp',
                    },
                    {
                      title: t('ui.orders.timeline_amount', 'Amount'),
                      key: 'amount',
                      render: (_, record) => `${Number(record.allocated_amount ?? record.amount ?? 0).toLocaleString()} UZS`,
                    },
                    {
                      title: t('ui.orders.timeline_notes', 'Notes'),
                      dataIndex: 'notes',
                      key: 'notes',
                      render: (value) => value || '—',
                    },
                  ]}
                />
              </>
            ) : null}

            <div style={{ marginTop: 16, textAlign: 'right' }}>
              <Space>
                {selectedOrder.payment_link ? (
                  <Button icon={<LinkOutlined />} href={selectedOrder.payment_link} target="_blank">
                    {t('ui.orders.open_payment_link', 'Open Payment Link')}
                  </Button>
                ) : null}
                {(selectedOrder.payment_method === 'cash' ||
                  (['click', 'payme', 'card'].includes(selectedOrder.payment_method) &&
                   selectedOrder.payment_status === 'pending')) ? (
                  <Button
                    icon={<DollarOutlined />}
                    disabled={['cancelled', 'returned'].includes(selectedOrder.status)}
                    onClick={() => {
                      personalCardForm.setFieldsValue({
                        amount: selectedOrder.outstanding_amount || 0,
                        notes: '',
                      });
                      setIsPersonalCardModalVisible(true);
                    }}
                  >
                    {t('ui.orders.record_personal_card_payment', 'Record Personal Card Payment')}
                  </Button>
                ) : null}
                <Button
                  type="primary"
                  onClick={() => {
                    setIsDetailModalVisible(false);
                    handleUpdateStatus(selectedOrder);
                  }}
                >
                  {t('ui.orders.update_status', 'Update Status')}
                </Button>
                <Button onClick={() => setIsDetailModalVisible(false)}>{t('ui.orders.close', 'Close')}</Button>
              </Space>
            </div>
          </div>
        ) : null}
      </Modal>

      <Modal
        title={`${t('ui.orders.update_order_status', 'Update Order Status')} - ${selectedOrder?.order_number || ''}`}
        open={isStatusModalVisible}
        onCancel={() => setIsStatusModalVisible(false)}
        footer={null}
      >
        <Form form={statusForm} layout="vertical" onFinish={(values) => {
          updateOrderMutation.mutate({
            orderId: selectedOrder.id,
            status: values.status,
            notes: values.notes,
            ...(values.status === 'delivered' && values.bottles_returned != null
              ? { bottles_returned: values.bottles_returned }
              : {}),
          });
        }}>
          <Form.Item
            name="status"
            label={t('ui.orders.new_status', 'New Status')}
            rules={[{ required: true, message: t('ui.orders.select_status_required', 'Please select a status') }]}
          >
            <Select>
              {orderStatuses.map((status) => (
                <Option key={status.value} value={status.value}>
                  {t(`ui.orders.status_${status.value}`, status.label)}
                </Option>
              ))}
            </Select>
          </Form.Item>
          {watchedStatusValue === 'delivered' && (
            <Form.Item
              name="bottles_returned"
              label={t('ui.orders.bottles_returned', 'Bottles Returned')}
              extra={t('ui.orders.bottles_returned_hint', 'Number of returnable bottles collected from customer (optional)')}
            >
              <InputNumber min={0} style={{ width: '100%' }} placeholder="0" />
            </Form.Item>
          )}
          <Form.Item name="notes" label={t('ui.orders.notes_optional', 'Notes (Optional)')}>
            <Input.TextArea rows={3} placeholder={t('ui.orders.notes_placeholder', 'Notes')} />
          </Form.Item>
          <Form.Item style={{ marginBottom: 0, textAlign: 'right' }}>
            <Space>
              <Button onClick={() => setIsStatusModalVisible(false)}>{t('ui.orders.close', 'Close')}</Button>
              <AsyncButton type="primary" htmlType="submit" loading={updateOrderMutation.isPending}>
                {t('ui.orders.update_status', 'Update Status')}
              </AsyncButton>
            </Space>
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={t('ui.orders.create_order', 'Create Order')}
        open={isCreateModalVisible}
        onCancel={createOrderReset}
        footer={null}
        width={760}
      >
        <Form form={createOrderForm} layout="vertical" onFinish={handleCreateOrderSubmit} initialValues={{ items: [{}], consume_marking_codes: false }}>
          {createOrderErrors.length > 0 ? (
            <Alert
              type="error"
              showIcon
              style={{ marginBottom: 16 }}
              message={t('ui.orders.order_create_validation_title', 'Could not create order')}
              description={
                <ul style={{ margin: 0, paddingLeft: 18 }}>
                  {createOrderErrors.map((errorText) => (
                    <li key={errorText}>{errorText}</li>
                  ))}
                </ul>
              }
            />
          ) : null}

          <Form.Item
            name="user_id"
            label={t('ui.orders.select_customer', 'Select Customer')}
            rules={[{ required: true, message: t('ui.orders.customer_required', 'Please select a customer') }]}
          >
            <Select
              showSearch
              placeholder={t('ui.orders.search_customer', 'Search customer by name or phone')}
              optionFilterProp="children"
              onChange={handleUserSelect}
              filterOption={(input, option) => String(option.children).toLowerCase().includes(input.toLowerCase())}
            >
              {(usersData?.data?.items || []).map((user) => (
                <Option key={user.id} value={user.id}>
                  {user.first_name} {user.last_name} - {user.phone}
                </Option>
              ))}
            </Select>
          </Form.Item>

          <Form.Item
            name="delivery_address_id"
            label={t('ui.orders.select_address', 'Select Delivery Address')}
            rules={[{ required: true, message: t('ui.orders.address_required', 'Please select a delivery address') }]}
          >
            <Select
              placeholder={
                selectedUserId
                  ? userAddresses.length > 0
                    ? t('ui.orders.select_address_placeholder', 'Select an address')
                    : t('ui.orders.no_addresses', 'No addresses found for this user')
                  : t('ui.orders.select_customer_first', 'Select a customer first')
              }
              disabled={!selectedUserId || userAddresses.length === 0}
            >
              {userAddresses.map((address) => (
                <Option key={address.id} value={address.id}>
                  {address.title ? `${address.title}: ` : ''}
                  {address.full_address}
                  {address.is_default ? ' (Default)' : ''}
                </Option>
              ))}
            </Select>
          </Form.Item>

          {selectedUserId && userAddresses.length === 0 ? (
            <div
              style={{
                background: '#fff7e6',
                border: '1px solid #ffd591',
                borderRadius: 6,
                padding: 12,
                marginBottom: 16,
              }}
            >
              <UserOutlined style={{ marginRight: 8 }} />
              {t('ui.orders.no_address_hint', 'This user has no saved addresses. Please add an address from the Users page first.')}
            </div>
          ) : null}

          {selectedUserId && paymentRestrictions?.cod_restricted ? (
            <Alert
              type="warning"
              showIcon
              style={{ marginBottom: 16 }}
              message={t('ui.orders.cod_restricted', 'Cash on delivery is restricted for this customer')}
              description={t(
                'ui.orders.cod_restricted_description',
                'This customer has reached the active COD debt limit. Use one of the prepaid methods below.',
              )}
            />
          ) : null}

          <Divider>{t('ui.orders.order_items', 'Order Items')}</Divider>
          <Form.List name="items">
            {(fields, { add, remove }) => (
              <>
                {fields.map(({ key, name, ...restField }) => (
                  <Row key={key} gutter={16} align="middle">
                    <Col span={14}>
                      <Form.Item
                        {...restField}
                        name={[name, 'product_id']}
                        rules={[{ required: true, message: t('ui.orders.product_required', 'Select product') }]}
                      >
                        <Select
                          showSearch
                          placeholder={t('ui.orders.select_product', 'Select product')}
                          optionFilterProp="children"
                          filterOption={(input, option) => String(option.children).toLowerCase().includes(input.toLowerCase())}
                        >
                          {(productsData?.data?.items || []).map((product) => {
                            const effectivePrice = getEffectiveProductPrice(product);
                            return (
                              <Option key={product.id} value={product.id}>
                                {product.name} - {Number(effectivePrice || 0).toLocaleString()} UZS
                                {product.pricing_source === 'contract' ? ' (Contract)' : ''}
                              </Option>
                            );
                          })}
                        </Select>
                      </Form.Item>
                    </Col>
                    <Col span={6}>
                      <Form.Item
                        {...restField}
                        name={[name, 'quantity']}
                        rules={[{ required: true, message: t('ui.orders.quantity_required', 'Qty') }]}
                        initialValue={1}
                      >
                        <Select placeholder={t('ui.orders.quantity', 'Qty')}>
                          {Array.from({ length: 100 }, (_, index) => index + 1).map((value) => (
                            <Option key={value} value={value}>{value}</Option>
                          ))}
                        </Select>
                      </Form.Item>
                    </Col>
                    <Col span={4}>
                      {fields.length > 1 ? (
                        <Button type="text" danger icon={<MinusCircleOutlined />} onClick={() => remove(name)} />
                      ) : null}
                    </Col>
                  </Row>
                ))}
                <Form.Item>
                  <Button type="dashed" onClick={() => add()} block icon={<PlusOutlined />}>
                    {t('ui.orders.add_item', 'Add Item')}
                  </Button>
                </Form.Item>
              </>
            )}
          </Form.List>

          <Form.Item
            name="payment_method"
            label={t('ui.orders.payment_method', 'Payment Method')}
            rules={[{ required: true, message: t('ui.orders.payment_method_required', 'Please select a payment method') }]}
          >
            <Select
              loading={paymentMethodsLoading}
              disabled={!selectedUserId || userPaymentMethods.length === 0}
              placeholder={
                selectedUserId
                  ? t('ui.orders.select_payment_method', 'Select a payment method')
                  : t('ui.orders.select_customer_first', 'Select a customer first')
              }
            >
              {userPaymentMethods.map((method) => (
                <Option key={method.method} value={method.method}>
                  {t(`ui.orders.payment_${method.method}`, method.name || method.method)}
                </Option>
              ))}
            </Select>
          </Form.Item>

          {watchedPaymentMethod === 'business_account' ? (
            <Form.Item
              name="consume_marking_codes"
              label={t('ui.orders.consume_marking_codes', 'Consume Marking Codes')}
              valuePropName="checked"
              extra={t(
                'ui.orders.consume_marking_codes_help',
                'Leave disabled unless this business-account order should permanently consume product marking codes.',
              )}
            >
              <Switch />
            </Form.Item>
          ) : null}

          <Form.Item name="delivery_notes" label={t('ui.orders.delivery_notes', 'Delivery Notes')}>
            <Input.TextArea rows={2} placeholder={t('ui.orders.delivery_notes_placeholder', 'Any special delivery instructions...')} />
          </Form.Item>

          <Form.Item style={{ marginBottom: 0, textAlign: 'right' }}>
            <Space>
              <Button onClick={createOrderReset}>{t('ui.common.cancel', 'Cancel')}</Button>
              <AsyncButton type="primary" htmlType="submit" loading={createOrderMutation.isPending} icon={<ShoppingCartOutlined />}>
                {t('ui.orders.create_order', 'Create Order')}
              </AsyncButton>
            </Space>
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={t('ui.orders.record_personal_card_payment', 'Record Personal Card Payment')}
        open={isPersonalCardModalVisible}
        onCancel={() => {
          setIsPersonalCardModalVisible(false);
          personalCardForm.resetFields();
        }}
        onOk={() => personalCardForm.submit()}
        confirmLoading={recordPersonalCardPaymentMutation.isPending}
      >
        <Form
          form={personalCardForm}
          layout="vertical"
          onFinish={(values) => {
            if (!selectedOrder?.id || !selectedOrder?.user_id) {
              message.error(t('ui.orders.order_context_missing', 'Order context is missing'));
              return;
            }
            recordPersonalCardPaymentMutation.mutate({
              customer_id: selectedOrder.user_id,
              order_id: selectedOrder.id,
              amount: values.amount,
              notes: values.notes,
              source: 'personal_card_transfer',
              proof_data: { channel: 'admin_ui_orders' },
            });
          }}
        >
          <Form.Item label={t('ui.orders.order_number', 'Order Number')}>
            <Input value={selectedOrder?.order_number} disabled />
          </Form.Item>
          <Form.Item label={t('ui.orders.outstanding_amount', 'Outstanding')}>
            <Input value={`${Number(selectedOrder?.outstanding_amount || 0).toLocaleString()} UZS`} disabled />
          </Form.Item>
          <Form.Item name="amount" label={t('ui.orders.amount', 'Amount')} rules={[{ required: true, message: t('ui.orders.amount_required', 'Amount is required') }]}>
            <Input type="number" min={0} />
          </Form.Item>
          <Form.Item name="notes" label={t('ui.orders.notes', 'Notes')} rules={[{ required: true, message: t('ui.orders.notes_required', 'Notes are required') }]}>
            <Input.TextArea rows={3} placeholder={t('ui.orders.personal_card_notes_placeholder', 'Example: Customer transferred to owner personal card')} />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
};

export default Orders;
