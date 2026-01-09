import React, { useState } from 'react';
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
  Spin
} from 'antd';
import {
  SearchOutlined,
  ShoppingCartOutlined,
  MoreOutlined,
  ExportOutlined,
  EyeOutlined,
  EditOutlined,
  DollarOutlined,
  CalendarOutlined,
  PlusOutlined,
  UserOutlined,
  MinusCircleOutlined
} from '@ant-design/icons';
import { useQuery, useMutation, useQueryClient } from 'react-query';
import moment from 'moment';
import adminService from '../services/adminService';
import { useTranslation } from 'react-i18next';

const { Option } = Select;
const { RangePicker } = DatePicker;

const Orders = () => {
  // Load orders namespace for ui.orders.* keys
  const { t } = useTranslation('orders');
  const [searchText, setSearchText] = useState('');
  const [statusFilter, setStatusFilter] = useState('');
  const [dateRange, setDateRange] = useState(null);
  const [selectedOrder, setSelectedOrder] = useState(null);
  const [isDetailModalVisible, setIsDetailModalVisible] = useState(false);
  const [isStatusModalVisible, setIsStatusModalVisible] = useState(false);
  const [isCreateModalVisible, setIsCreateModalVisible] = useState(false);
  const [selectedUserId, setSelectedUserId] = useState(null);
  const [userAddresses, setUserAddresses] = useState([]);
  const [pagination, setPagination] = useState({ page: 1, per_page: 20 });
  const [orderDetailsLoading, setOrderDetailsLoading] = useState(false);
  const [form] = Form.useForm();
  const [createOrderForm] = Form.useForm();

  const queryClient = useQueryClient();

  // Fetch orders
  const { data, isLoading } = useQuery(
    ['orders', pagination, searchText, statusFilter, dateRange],
    () => adminService.getOrders({
      page: pagination.page,
      per_page: pagination.per_page,
      search: searchText,
      status: statusFilter,
      start_date: dateRange?.[0]?.format('YYYY-MM-DD'),
      end_date: dateRange?.[1]?.format('YYYY-MM-DD')
    }),
    {
      keepPreviousData: true
    }
  );

  // Fetch users for order creation
  const { data: usersData } = useQuery(
    ['users-for-order'],
    () => adminService.getUsers({ per_page: 100 }),
    { enabled: isCreateModalVisible }
  );

  // Fetch products for order creation
  const { data: productsData } = useQuery(
    ['products-for-order'],
    () => adminService.getProducts({ per_page: 100, is_active: true }),
    { enabled: isCreateModalVisible }
  );

  // Update order status mutation
  const updateOrderMutation = useMutation(
    ({ orderId, status, notes }) => adminService.updateOrderStatus(orderId, status, notes),
    {
      onSuccess: () => {
        message.success(t('ui.orders.status_updated_success'));
        queryClient.invalidateQueries('orders');
        setIsStatusModalVisible(false);
        form.resetFields();
      },
      onError: () => {
        message.error(t('ui.orders.status_update_failed'));
      }
    }
  );

  // Create order mutation
  const createOrderMutation = useMutation(
    (orderData) => adminService.createOrderForUser(orderData),
    {
      onSuccess: () => {
        message.success(t('ui.orders.order_created_success', 'Order created successfully'));
        queryClient.invalidateQueries('orders');
        setIsCreateModalVisible(false);
        createOrderForm.resetFields();
        setSelectedUserId(null);
        setUserAddresses([]);
      },
      onError: (error) => {
        const errorMessage = error.response?.data?.message || t('ui.orders.order_create_failed', 'Failed to create order');
        message.error(errorMessage);
      }
    }
  );

  // Handle user selection - fetch their addresses
  const handleUserSelect = async (userId) => {
    setSelectedUserId(userId);
    createOrderForm.setFieldsValue({ delivery_address_id: undefined });

    if (userId) {
      try {
        const response = await adminService.getUserAddresses(userId);
        setUserAddresses(response.data?.addresses || []);
      } catch (error) {
        message.error('Failed to load user addresses');
        setUserAddresses([]);
      }
    } else {
      setUserAddresses([]);
    }
  };

  // Handle create order submit
  const handleCreateOrderSubmit = (values) => {
    const orderData = {
      user_id: values.user_id,
      delivery_address_id: values.delivery_address_id,
      payment_method: values.payment_method || 'cash',
      delivery_notes: values.delivery_notes || '',
      items: values.items.map(item => ({
        product_id: item.product_id,
        quantity: item.quantity
      }))
    };
    createOrderMutation.mutate(orderData);
  };

  const orderStatusColors = {
    pending: 'orange',
    confirmed: 'blue',
    processing: 'cyan',
    shipped: 'purple',
    delivered: 'green',
    cancelled: 'red',
    refunded: 'volcano'
  };

  const columns = [
    {
      title: t('ui.orders.order_number'),
      dataIndex: 'order_number',
      key: 'order_number',
      width: 120,
      render: (text) => (
        <span style={{ fontFamily: 'monospace', fontWeight: 'bold' }}>
          {text}
        </span>
      )
    },
    {
      title: t('ui.orders.customer'),
      dataIndex: 'customer',
      key: 'customer',
      render: (_, record) => (
        <div>
          <div>{record.customer_name}</div>
          <small style={{ color: '#666' }}>{record.customer_email}</small>
        </div>
      )
    },
    {
      title: t('ui.orders.items'),
      dataIndex: 'items_summary',
      key: 'items_summary',
      width: 200,
      render: (items, record) => {
        if (!items || items.length === 0) {
          return <Tag color="blue">{record.items_count || 0} {t('ui.orders.items_count')}</Tag>;
        }
        return (
          <div style={{ fontSize: 12 }}>
            {items.slice(0, 2).map((item, idx) => (
              <div key={idx} style={{ whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                {item.quantity}x {item.product_name}
              </div>
            ))}
            {items.length > 2 && (
              <span style={{ color: '#999' }}>+{items.length - 2} {t('ui.orders.more_items', 'more')}</span>
            )}
          </div>
        );
      }
    },
    {
      title: t('ui.orders.total_amount'),
      dataIndex: 'total_amount',
      key: 'total_amount',
      width: 120,
      render: (amount) => (
        <span style={{ fontWeight: 'bold', color: '#52c41a' }}>
          {amount?.toFixed(0)} UZS
        </span>
      )
    },
    {
      title: t('ui.orders.status'),
      dataIndex: 'status',
      key: 'status',
      width: 110,
      render: (status) => (
        <Tag color={orderStatusColors[status] || 'default'}>
          {t(`ui.orders.status_${status}`)}
        </Tag>
      )
    },
    {
      title: t('ui.orders.payment'),
      dataIndex: 'payment_status',
      key: 'payment_status',
      width: 100,
      render: (status) => (
        <Tag color={status === 'paid' ? 'green' : status === 'pending' ? 'orange' : 'red'}>
          {t(`ui.orders.payment_${status}`)}
        </Tag>
      )
    },
    {
      title: t('ui.orders.order_date'),
      dataIndex: 'created_at',
      key: 'created_at',
      width: 120,
      render: (date) => moment(date).format('MMM DD, YYYY')
    },
    {
      title: t('ui.orders.actions'),
      key: 'actions',
      width: 100,
      render: (_, record) => (
        <Dropdown
          menu={{
            items: [
              {
                key: 'view',
                label: t('ui.orders.view_details'),
                icon: <EyeOutlined />,
                onClick: () => handleViewOrder(record)
              },
              {
                key: 'status',
                label: t('ui.orders.update_status'),
                icon: <EditOutlined />,
                onClick: () => handleUpdateStatus(record)
              },
              {
                type: 'divider'
              },
              {
                key: 'cancel',
                label: t('ui.orders.cancel_order'),
                danger: true,
                disabled: ['delivered', 'cancelled'].includes(record.status),
                onClick: () => handleCancelOrder(record)
              }
            ]
          }}
          trigger={['click']}
        >
          <Button type="text" icon={<MoreOutlined />} />
        </Dropdown>
      )
    }
  ];

  const handleViewOrder = async (order) => {
    setSelectedOrder(order); // Show basic data immediately
    setIsDetailModalVisible(true);
    setOrderDetailsLoading(true);

    try {
      const response = await adminService.getOrderDetails(order.id);
      if (response.success && response.data?.order) {
        setSelectedOrder(response.data.order);
      }
    } catch (error) {
      console.error('Failed to load order details:', error);
      // Keep the basic order data if fetch fails
    } finally {
      setOrderDetailsLoading(false);
    }
  };

  const handleUpdateStatus = (order) => {
    setSelectedOrder(order);
    form.setFieldsValue({
      status: order.status,
      notes: ''
    });
    setIsStatusModalVisible(true);
  };

  const handleCancelOrder = (order) => {
    Modal.confirm({
      title: t('ui.orders.cancel_order_title'),
      content: `${t('ui.orders.cancel_order_confirm')} ${order.order_number}?`,
      onOk: () => {
        updateOrderMutation.mutate({
          orderId: order.id,
          status: 'cancelled',
          notes: t('ui.orders.cancelled_by_admin')
        });
      }
    });
  };

  const handleStatusSubmit = (values) => {
    updateOrderMutation.mutate({
      orderId: selectedOrder.id,
      status: values.status,
      notes: values.notes
    });
  };

  const handleTableChange = (paginationInfo) => {
    setPagination({
      page: paginationInfo.current,
      per_page: paginationInfo.pageSize
    });
  };

  const handleSearch = (value) => {
    setSearchText(value);
    setPagination({ ...pagination, page: 1 });
  };

  const handleStatusFilter = (value) => {
    setStatusFilter(value);
    setPagination({ ...pagination, page: 1 });
  };

  const handleDateRangeChange = (dates) => {
    setDateRange(dates);
    setPagination({ ...pagination, page: 1 });
  };

  // Calculate summary statistics
  const orders = data?.data?.items || [];
  const totalRevenue = orders
    .filter(order => !['cancelled', 'refunded'].includes(order.status))
    .reduce((sum, order) => sum + (order.total_amount || 0), 0);
  const pendingOrders = orders.filter(order => order.status === 'pending').length;
  const completedOrders = orders.filter(order => order.status === 'delivered').length;

  return (
    <div>
      {/* Summary Cards */}
      <Row gutter={[16, 16]} style={{ marginBottom: 24 }}>
        <Col xs={24} sm={8}>
          <Card>
            <Statistic
              title={t('ui.orders.total_orders')}
              value={data?.meta?.total || 0}
              prefix={<ShoppingCartOutlined />}
            />
          </Card>
        </Col>
        <Col xs={24} sm={8}>
          <Card>
            <Statistic
              title={t('ui.orders.total_revenue')}
              value={totalRevenue}
              precision={2}
              prefix={<DollarOutlined />}
              suffix="UZS"
            />
          </Card>
        </Col>
        <Col xs={24} sm={8}>
          <Card>
            <Statistic
              title={t('ui.orders.pending_orders')}
              value={pendingOrders}
              valueStyle={{ color: '#faad14' }}
            />
          </Card>
        </Col>
      </Row>

      <Card>
        {/* Filter Controls */}
        <div className="table-actions">
          <Space wrap>
            <Input.Search
              placeholder={t('ui.orders.search_placeholder')}
              allowClear
              onSearch={handleSearch}
              style={{ width: 250 }}
            />
            <Select
              placeholder={t('ui.orders.filter_by_status')}
              allowClear
              onChange={handleStatusFilter}
              style={{ width: 150 }}
            >
              <Option value="pending">{t('ui.orders.status_pending')}</Option>
              <Option value="confirmed">{t('ui.orders.status_confirmed')}</Option>
              <Option value="processing">{t('ui.orders.status_processing')}</Option>
              <Option value="shipped">{t('ui.orders.status_shipped')}</Option>
              <Option value="delivered">{t('ui.orders.status_delivered')}</Option>
              <Option value="cancelled">{t('ui.orders.status_cancelled')}</Option>
              <Option value="refunded">{t('ui.orders.status_refunded')}</Option>
            </Select>
            <RangePicker
              onChange={handleDateRangeChange}
              format="YYYY-MM-DD"
              placeholder={[t('ui.orders.start_date'), t('ui.orders.end_date')]}
            />
          </Space>

          <Space>
            <Button icon={<ExportOutlined />}>
              {t('ui.orders.export_orders')}
            </Button>
            <Button
              type="primary"
              icon={<PlusOutlined />}
              onClick={() => setIsCreateModalVisible(true)}
            >
              {t('ui.orders.create_order', 'Create Order')}
            </Button>
          </Space>
        </div>

        {/* Orders Table */}
        <Table
          columns={columns}
          dataSource={orders}
          loading={isLoading}
          rowKey="id"
          pagination={{
            current: pagination.page,
            pageSize: pagination.per_page,
            total: data?.meta?.total || 0,
            showSizeChanger: true,
            showQuickJumper: true,
            showTotal: (total, range) =>
              `${range[0]}-${range[1]} of ${total} ${t('ui.orders.pagination_text')}`
          }}
          onChange={handleTableChange}
          className="admin-table"
          scroll={{ x: 1000 }}
        />
      </Card>

      {/* Order Details Modal */}
      <Modal
        title={`${t('ui.orders.order_details')} - ${selectedOrder?.order_number}`}
        open={isDetailModalVisible}
        onCancel={() => setIsDetailModalVisible(false)}
        footer={null}
        width={700}
      >
        {selectedOrder && (
          <div>
            <Descriptions column={2} bordered>
              <Descriptions.Item label={t('ui.orders.order_number')}>
                {selectedOrder.order_number}
              </Descriptions.Item>
              <Descriptions.Item label={t('ui.orders.status')}>
                <Tag color={orderStatusColors[selectedOrder.status]}>
                  {t(`ui.orders.status_${selectedOrder.status}`)}
                </Tag>
              </Descriptions.Item>
              <Descriptions.Item label={t('ui.orders.customer')}>
                {selectedOrder.customer_name}
              </Descriptions.Item>
              <Descriptions.Item label={t('ui.orders.email')}>
                {selectedOrder.customer_email}
              </Descriptions.Item>
              <Descriptions.Item label={t('ui.orders.phone')}>
                {selectedOrder.customer_phone}
              </Descriptions.Item>
              <Descriptions.Item label={t('ui.orders.total_amount')}>
                <span style={{ fontWeight: 'bold', color: '#52c41a' }}>
                  ${selectedOrder.total_amount?.toFixed(2)}
                </span>
              </Descriptions.Item>
              <Descriptions.Item label={t('ui.orders.payment_status')}>
                <Tag color={selectedOrder.payment_status === 'paid' ? 'green' : 'orange'}>
                  {t(`ui.orders.payment_${selectedOrder.payment_status}`)}
                </Tag>
              </Descriptions.Item>
              <Descriptions.Item label={t('ui.orders.order_date')}>
                {moment(selectedOrder.created_at).format('YYYY-MM-DD HH:mm')}
              </Descriptions.Item>
            </Descriptions>

            <Divider>{t('ui.orders.order_items')}</Divider>

            <Spin spinning={orderDetailsLoading}>
              <div style={{ marginTop: 16 }}>
                {(selectedOrder.items && selectedOrder.items.length > 0) || (selectedOrder.items_summary && selectedOrder.items_summary.length > 0) ? (
                <Table
                  dataSource={selectedOrder.items || selectedOrder.items_summary}
                  rowKey={(_, index) => `item-${index}`}
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
                      width: 120,
                      align: 'right',
                      render: (price) => `${price?.toLocaleString()} UZS`,
                    },
                    {
                      title: t('ui.orders.total_price', 'Total'),
                      dataIndex: 'total_price',
                      key: 'total_price',
                      width: 120,
                      align: 'right',
                      render: (price) => (
                        <span style={{ fontWeight: 'bold' }}>
                          {price?.toLocaleString()} UZS
                        </span>
                      ),
                    },
                  ]}
                  footer={() => (
                    <div style={{ textAlign: 'right' }}>
                      <strong>{t('ui.orders.order_total', 'Order Total')}: </strong>
                      <span style={{ fontSize: 16, color: '#52c41a', fontWeight: 'bold' }}>
                        {selectedOrder.total_amount?.toLocaleString()} UZS
                      </span>
                    </div>
                  )}
                />
              ) : (
                <p style={{ color: '#999', textAlign: 'center' }}>
                  {t('ui.orders.no_items', 'No items in this order')}
                </p>
              )}
              </div>
            </Spin>

            <div style={{ marginTop: 16, textAlign: 'right' }}>
              <Space>
                <Button
                  type="primary"
                  onClick={() => {
                    setIsDetailModalVisible(false);
                    handleUpdateStatus(selectedOrder);
                  }}
                >
                  {t('ui.orders.update_status')}
                </Button>
                <Button onClick={() => setIsDetailModalVisible(false)}>
                  {t('ui.orders.close')}
                </Button>
              </Space>
            </div>
          </div>
        )}
      </Modal>

      {/* Status Update Modal */}
      <Modal
        title={`${t('ui.orders.update_order_status')} - ${selectedOrder?.order_number}`}
        open={isStatusModalVisible}
        onCancel={() => setIsStatusModalVisible(false)}
        footer={null}
      >
        <Form
          form={form}
          layout="vertical"
          onFinish={handleStatusSubmit}
        >
          <Form.Item
            name="status"
            label={t('ui.orders.new_status')}
            rules={[{ required: true, message: t('ui.orders.select_status_required') }]}
          >
            <Select>
              <Option value="pending">{t('ui.orders.status_pending')}</Option>
              <Option value="confirmed">{t('ui.orders.status_confirmed')}</Option>
              <Option value="processing">{t('ui.orders.status_processing')}</Option>
              <Option value="shipped">{t('ui.orders.status_shipped')}</Option>
              <Option value="delivered">{t('ui.orders.status_delivered')}</Option>
              <Option value="cancelled">{t('ui.orders.status_cancelled')}</Option>
              <Option value="refunded">{t('ui.orders.status_refunded')}</Option>
            </Select>
          </Form.Item>

          <Form.Item
            name="notes"
            label={t('ui.orders.notes_optional')}
          >
            <Input.TextArea
              rows={3}
              placeholder={t('ui.orders.notes_placeholder')}
            />
          </Form.Item>

          <Form.Item style={{ marginBottom: 0, textAlign: 'right' }}>
            <Space>
              <Button onClick={() => setIsStatusModalVisible(false)}>
                {t('ui.orders.close')}
              </Button>
              <Button
                type="primary"
                htmlType="submit"
                loading={updateOrderMutation.isLoading}
              >
                {t('ui.orders.update_status')}
              </Button>
            </Space>
          </Form.Item>
        </Form>
      </Modal>

      {/* Create Order Modal */}
      <Modal
        title={t('ui.orders.create_order', 'Create Order')}
        open={isCreateModalVisible}
        onCancel={() => {
          setIsCreateModalVisible(false);
          createOrderForm.resetFields();
          setSelectedUserId(null);
          setUserAddresses([]);
        }}
        footer={null}
        width={700}
      >
        <Form
          form={createOrderForm}
          layout="vertical"
          onFinish={handleCreateOrderSubmit}
          initialValues={{ payment_method: 'cash', items: [{}] }}
        >
          {/* User Selection */}
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
              filterOption={(input, option) =>
                option.children.toLowerCase().includes(input.toLowerCase())
              }
            >
              {(usersData?.data?.items || []).map(user => (
                <Option key={user.id} value={user.id}>
                  {user.first_name} {user.last_name} - {user.phone}
                </Option>
              ))}
            </Select>
          </Form.Item>

          {/* Address Selection */}
          <Form.Item
            name="delivery_address_id"
            label={t('ui.orders.select_address', 'Select Delivery Address')}
            rules={[{ required: true, message: t('ui.orders.address_required', 'Please select a delivery address') }]}
          >
            <Select
              placeholder={
                selectedUserId
                  ? (userAddresses.length > 0
                    ? t('ui.orders.select_address_placeholder', 'Select an address')
                    : t('ui.orders.no_addresses', 'No addresses found for this user'))
                  : t('ui.orders.select_customer_first', 'Select a customer first')
              }
              disabled={!selectedUserId || userAddresses.length === 0}
            >
              {userAddresses.map(addr => (
                <Option key={addr.id} value={addr.id}>
                  {addr.title ? `${addr.title}: ` : ''}{addr.full_address}
                  {addr.is_default && ' (Default)'}
                </Option>
              ))}
            </Select>
          </Form.Item>

          {/* Add Address Link if no addresses */}
          {selectedUserId && userAddresses.length === 0 && (
            <div style={{
              background: '#fff7e6',
              border: '1px solid #ffd591',
              borderRadius: 6,
              padding: 12,
              marginBottom: 16
            }}>
              <UserOutlined style={{ marginRight: 8 }} />
              {t('ui.orders.no_address_hint', 'This user has no saved addresses. Please add an address from the Users page first.')}
            </div>
          )}

          {/* Product Items */}
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
                          filterOption={(input, option) =>
                            option.children.toLowerCase().includes(input.toLowerCase())
                          }
                        >
                          {(productsData?.data?.items || []).map(product => (
                            <Option key={product.id} value={product.id}>
                              {product.name} - {product.price?.toLocaleString()} UZS
                            </Option>
                          ))}
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
                          {[1, 2, 3, 4, 5, 6, 7, 8, 9, 10].map(n => (
                            <Option key={n} value={n}>{n}</Option>
                          ))}
                        </Select>
                      </Form.Item>
                    </Col>
                    <Col span={4}>
                      {fields.length > 1 && (
                        <Button
                          type="text"
                          danger
                          icon={<MinusCircleOutlined />}
                          onClick={() => remove(name)}
                        />
                      )}
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

          {/* Payment Method */}
          <Form.Item
            name="payment_method"
            label={t('ui.orders.payment_method', 'Payment Method')}
          >
            <Select>
              <Option value="cash">{t('ui.orders.payment_cash', 'Cash on Delivery')}</Option>
              <Option value="payme">{t('ui.orders.payment_payme', 'Payme')}</Option>
              <Option value="click">{t('ui.orders.payment_click', 'Click')}</Option>
            </Select>
          </Form.Item>

          {/* Delivery Notes */}
          <Form.Item
            name="delivery_notes"
            label={t('ui.orders.delivery_notes', 'Delivery Notes')}
          >
            <Input.TextArea
              rows={2}
              placeholder={t('ui.orders.delivery_notes_placeholder', 'Any special delivery instructions...')}
            />
          </Form.Item>

          {/* Submit Buttons */}
          <Form.Item style={{ marginBottom: 0, textAlign: 'right' }}>
            <Space>
              <Button onClick={() => {
                setIsCreateModalVisible(false);
                createOrderForm.resetFields();
                setSelectedUserId(null);
                setUserAddresses([]);
              }}>
                {t('ui.common.cancel', 'Cancel')}
              </Button>
              <Button
                type="primary"
                htmlType="submit"
                loading={createOrderMutation.isLoading}
                icon={<ShoppingCartOutlined />}
              >
                {t('ui.orders.create_order', 'Create Order')}
              </Button>
            </Space>
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
};

export default Orders;