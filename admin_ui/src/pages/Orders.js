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
  Divider
} from 'antd';
import {
  SearchOutlined,
  ShoppingCartOutlined,
  MoreOutlined,
  ExportOutlined,
  EyeOutlined,
  EditOutlined,
  DollarOutlined,
  CalendarOutlined
} from '@ant-design/icons';
import { useQuery, useMutation, useQueryClient } from 'react-query';
import moment from 'moment';
import adminService from '../services/adminService';

const { Option } = Select;
const { RangePicker } = DatePicker;

const Orders = () => {
  const [searchText, setSearchText] = useState('');
  const [statusFilter, setStatusFilter] = useState('');
  const [dateRange, setDateRange] = useState(null);
  const [selectedOrder, setSelectedOrder] = useState(null);
  const [isDetailModalVisible, setIsDetailModalVisible] = useState(false);
  const [isStatusModalVisible, setIsStatusModalVisible] = useState(false);
  const [pagination, setPagination] = useState({ page: 1, per_page: 20 });
  const [form] = Form.useForm();

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

  // Update order status mutation
  const updateOrderMutation = useMutation(
    ({ orderId, status, notes }) => adminService.updateOrderStatus(orderId, status, notes),
    {
      onSuccess: () => {
        message.success('Order status updated successfully');
        queryClient.invalidateQueries('orders');
        setIsStatusModalVisible(false);
        form.resetFields();
      },
      onError: (error) => {
        message.error('Failed to update order status');
      }
    }
  );

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
      title: 'Order #',
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
      title: 'Customer',
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
      title: 'Items',
      dataIndex: 'items_count',
      key: 'items_count',
      width: 80,
      render: (count) => (
        <Tag color="blue">{count} items</Tag>
      )
    },
    {
      title: 'Total Amount',
      dataIndex: 'total_amount',
      key: 'total_amount',
      width: 120,
      render: (amount) => (
        <span style={{ fontWeight: 'bold', color: '#52c41a' }}>
          ${amount?.toFixed(2)}
        </span>
      )
    },
    {
      title: 'Status',
      dataIndex: 'status',
      key: 'status',
      width: 110,
      render: (status) => (
        <Tag color={orderStatusColors[status] || 'default'}>
          {status?.toUpperCase()}
        </Tag>
      )
    },
    {
      title: 'Payment',
      dataIndex: 'payment_status',
      key: 'payment_status',
      width: 100,
      render: (status) => (
        <Tag color={status === 'paid' ? 'green' : status === 'pending' ? 'orange' : 'red'}>
          {status?.toUpperCase()}
        </Tag>
      )
    },
    {
      title: 'Order Date',
      dataIndex: 'created_at',
      key: 'created_at',
      width: 120,
      render: (date) => moment(date).format('MMM DD, YYYY')
    },
    {
      title: 'Actions',
      key: 'actions',
      width: 100,
      render: (_, record) => (
        <Dropdown
          menu={{
            items: [
              {
                key: 'view',
                label: 'View Details',
                icon: <EyeOutlined />,
                onClick: () => handleViewOrder(record)
              },
              {
                key: 'status',
                label: 'Update Status',
                icon: <EditOutlined />,
                onClick: () => handleUpdateStatus(record)
              },
              {
                type: 'divider'
              },
              {
                key: 'cancel',
                label: 'Cancel Order',
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

  const handleViewOrder = (order) => {
    setSelectedOrder(order);
    setIsDetailModalVisible(true);
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
      title: 'Cancel Order?',
      content: `Are you sure you want to cancel order ${order.order_number}?`,
      onOk: () => {
        updateOrderMutation.mutate({
          orderId: order.id,
          status: 'cancelled',
          notes: 'Cancelled by admin'
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
  const totalRevenue = orders.reduce((sum, order) => sum + (order.total_amount || 0), 0);
  const pendingOrders = orders.filter(order => order.status === 'pending').length;
  const completedOrders = orders.filter(order => order.status === 'delivered').length;

  return (
    <div>
      {/* Summary Cards */}
      <Row gutter={[16, 16]} style={{ marginBottom: 24 }}>
        <Col xs={24} sm={8}>
          <Card>
            <Statistic
              title="Total Orders"
              value={data?.meta?.total || 0}
              prefix={<ShoppingCartOutlined />}
            />
          </Card>
        </Col>
        <Col xs={24} sm={8}>
          <Card>
            <Statistic
              title="Total Revenue"
              value={totalRevenue}
              precision={2}
              prefix={<DollarOutlined />}
              suffix="USD"
            />
          </Card>
        </Col>
        <Col xs={24} sm={8}>
          <Card>
            <Statistic
              title="Pending Orders"
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
              placeholder="Search orders..."
              allowClear
              onSearch={handleSearch}
              style={{ width: 250 }}
            />
            <Select
              placeholder="Filter by status"
              allowClear
              onChange={handleStatusFilter}
              style={{ width: 150 }}
            >
              <Option value="pending">Pending</Option>
              <Option value="confirmed">Confirmed</Option>
              <Option value="processing">Processing</Option>
              <Option value="shipped">Shipped</Option>
              <Option value="delivered">Delivered</Option>
              <Option value="cancelled">Cancelled</Option>
              <Option value="refunded">Refunded</Option>
            </Select>
            <RangePicker
              onChange={handleDateRangeChange}
              format="YYYY-MM-DD"
              placeholder={['Start Date', 'End Date']}
            />
          </Space>

          <Space>
            <Button icon={<ExportOutlined />}>
              Export Orders
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
              `${range[0]}-${range[1]} of ${total} orders`
          }}
          onChange={handleTableChange}
          className="admin-table"
          scroll={{ x: 1000 }}
        />
      </Card>

      {/* Order Details Modal */}
      <Modal
        title={`Order Details - ${selectedOrder?.order_number}`}
        open={isDetailModalVisible}
        onCancel={() => setIsDetailModalVisible(false)}
        footer={null}
        width={700}
      >
        {selectedOrder && (
          <div>
            <Descriptions column={2} bordered>
              <Descriptions.Item label="Order Number">
                {selectedOrder.order_number}
              </Descriptions.Item>
              <Descriptions.Item label="Status">
                <Tag color={orderStatusColors[selectedOrder.status]}>
                  {selectedOrder.status?.toUpperCase()}
                </Tag>
              </Descriptions.Item>
              <Descriptions.Item label="Customer">
                {selectedOrder.customer_name}
              </Descriptions.Item>
              <Descriptions.Item label="Email">
                {selectedOrder.customer_email}
              </Descriptions.Item>
              <Descriptions.Item label="Phone">
                {selectedOrder.customer_phone}
              </Descriptions.Item>
              <Descriptions.Item label="Total Amount">
                <span style={{ fontWeight: 'bold', color: '#52c41a' }}>
                  ${selectedOrder.total_amount?.toFixed(2)}
                </span>
              </Descriptions.Item>
              <Descriptions.Item label="Payment Status">
                <Tag color={selectedOrder.payment_status === 'paid' ? 'green' : 'orange'}>
                  {selectedOrder.payment_status?.toUpperCase()}
                </Tag>
              </Descriptions.Item>
              <Descriptions.Item label="Order Date">
                {moment(selectedOrder.created_at).format('YYYY-MM-DD HH:mm')}
              </Descriptions.Item>
            </Descriptions>

            <Divider>Order Items</Divider>

            <div style={{ marginTop: 16 }}>
              {/* Order items would be displayed here */}
              <p>Order items details would be shown here based on API response</p>
            </div>

            <div style={{ marginTop: 16, textAlign: 'right' }}>
              <Space>
                <Button
                  type="primary"
                  onClick={() => {
                    setIsDetailModalVisible(false);
                    handleUpdateStatus(selectedOrder);
                  }}
                >
                  Update Status
                </Button>
                <Button onClick={() => setIsDetailModalVisible(false)}>
                  Close
                </Button>
              </Space>
            </div>
          </div>
        )}
      </Modal>

      {/* Status Update Modal */}
      <Modal
        title={`Update Order Status - ${selectedOrder?.order_number}`}
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
            label="New Status"
            rules={[{ required: true, message: 'Please select a status' }]}
          >
            <Select>
              <Option value="pending">Pending</Option>
              <Option value="confirmed">Confirmed</Option>
              <Option value="processing">Processing</Option>
              <Option value="shipped">Shipped</Option>
              <Option value="delivered">Delivered</Option>
              <Option value="cancelled">Cancelled</Option>
              <Option value="refunded">Refunded</Option>
            </Select>
          </Form.Item>

          <Form.Item
            name="notes"
            label="Notes (Optional)"
          >
            <Input.TextArea
              rows={3}
              placeholder="Add notes about this status change..."
            />
          </Form.Item>

          <Form.Item style={{ marginBottom: 0, textAlign: 'right' }}>
            <Space>
              <Button onClick={() => setIsStatusModalVisible(false)}>
                Cancel
              </Button>
              <Button
                type="primary"
                htmlType="submit"
                loading={updateOrderMutation.isLoading}
              >
                Update Status
              </Button>
            </Space>
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
};

export default Orders;