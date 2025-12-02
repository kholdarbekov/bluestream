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
  Timeline,
  Steps,
  Progress,
  Badge,
  Divider,
  Descriptions
} from 'antd';
import {
  SearchOutlined,
  TruckOutlined,
  MoreOutlined,
  PlusOutlined,
  EditOutlined,
  EyeOutlined,
  EnvironmentOutlined,
  ClockCircleOutlined,
  CheckCircleOutlined,
  ExclamationCircleOutlined,
  ExportOutlined,
  FilterOutlined,
  CalendarOutlined
} from '@ant-design/icons';
import { useQuery, useMutation, useQueryClient } from 'react-query';
import moment from 'moment';
import adminService from '../services/adminService';

const { Option } = Select;
const { RangePicker } = DatePicker;
const { Step } = Steps;

const Delivery = () => {
  const [searchText, setSearchText] = useState('');
  const [statusFilter, setStatusFilter] = useState('');
  const [dateRange, setDateRange] = useState(null);
  const [selectedDelivery, setSelectedDelivery] = useState(null);
  const [isDetailModalVisible, setIsDetailModalVisible] = useState(false);
  const [isTrackingModalVisible, setIsTrackingModalVisible] = useState(false);
  const [isUpdateModalVisible, setIsUpdateModalVisible] = useState(false);
  const [pagination, setPagination] = useState({ page: 1, per_page: 20 });
  const [form] = Form.useForm();

  const queryClient = useQueryClient();

  // Fetch deliveries
  const { data, isLoading } = useQuery(
    ['deliveries', pagination, searchText, statusFilter, dateRange],
    () => adminService.getDeliveries({
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

  // Update delivery mutation
  const updateDeliveryMutation = useMutation(
    ({ deliveryId, data }) => adminService.updateDelivery(deliveryId, data),
    {
      onSuccess: () => {
        message.success('Delivery updated successfully');
        queryClient.invalidateQueries('deliveries');
        setIsUpdateModalVisible(false);
        form.resetFields();
      },
      onError: (error) => {
        message.error('Failed to update delivery');
      }
    }
  );

  const deliveryStatusColors = {
    pending: 'orange',
    assigned: 'blue',
    picked_up: 'cyan',
    in_transit: 'purple',
    delivered: 'green',
    failed: 'red',
    returned: 'volcano'
  };

  const getStatusIcon = (status) => {
    switch (status) {
      case 'pending': return <ClockCircleOutlined />;
      case 'assigned': return <TruckOutlined />;
      case 'picked_up': return <EnvironmentOutlined />;
      case 'in_transit': return <TruckOutlined />;
      case 'delivered': return <CheckCircleOutlined />;
      case 'failed': return <ExclamationCircleOutlined />;
      case 'returned': return <ExclamationCircleOutlined />;
      default: return <ClockCircleOutlined />;
    }
  };

  const columns = [
    {
      title: 'Delivery ID',
      dataIndex: 'delivery_id',
      key: 'delivery_id',
      width: 120,
      render: (text) => (
        <span style={{ fontFamily: 'monospace', fontWeight: 'bold' }}>
          {text}
        </span>
      )
    },
    {
      title: 'Order #',
      dataIndex: 'order_number',
      key: 'order_number',
      width: 120,
      render: (text) => (
        <span style={{ color: '#1890ff' }}>{text}</span>
      )
    },
    {
      title: 'Customer',
      dataIndex: 'customer',
      key: 'customer',
      render: (_, record) => (
        <div>
          <div>{record.customer_name}</div>
          <small style={{ color: '#666' }}>{record.customer_phone}</small>
        </div>
      )
    },
    {
      title: 'Driver',
      dataIndex: 'driver_name',
      key: 'driver_name',
      render: (name, record) => (
        <div>
          <div>{name || 'Not Assigned'}</div>
          {record.driver_phone && (
            <small style={{ color: '#666' }}>{record.driver_phone}</small>
          )}
        </div>
      )
    },
    {
      title: 'Address',
      dataIndex: 'delivery_address',
      key: 'delivery_address',
      render: (address) => (
        <div style={{ maxWidth: 200 }}>
          <EnvironmentOutlined style={{ marginRight: 4, color: '#1890ff' }} />
          {address}
        </div>
      )
    },
    {
      title: 'Status',
      dataIndex: 'status',
      key: 'status',
      width: 120,
      render: (status) => (
        <Tag color={deliveryStatusColors[status] || 'default'} icon={getStatusIcon(status)}>
          {status?.toUpperCase().replace('_', ' ')}
        </Tag>
      )
    },
    {
      title: 'Priority',
      dataIndex: 'priority',
      key: 'priority',
      width: 90,
      render: (priority) => (
        <Badge
          color={priority === 'high' ? 'red' : priority === 'medium' ? 'orange' : 'green'}
          text={priority?.toUpperCase()}
        />
      )
    },
    {
      title: 'Scheduled',
      dataIndex: 'scheduled_date',
      key: 'scheduled_date',
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
                key: 'track',
                label: 'Track Delivery',
                icon: <EnvironmentOutlined />,
                onClick: () => handleTrackDelivery(record)
              },
              {
                key: 'details',
                label: 'View Details',
                icon: <EyeOutlined />,
                onClick: () => handleViewDelivery(record)
              },
              {
                key: 'update',
                label: 'Update Status',
                icon: <EditOutlined />,
                onClick: () => handleUpdateDelivery(record)
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

  const handleViewDelivery = (delivery) => {
    setSelectedDelivery(delivery);
    setIsDetailModalVisible(true);
  };

  const handleTrackDelivery = (delivery) => {
    setSelectedDelivery(delivery);
    setIsTrackingModalVisible(true);
  };

  const handleUpdateDelivery = (delivery) => {
    setSelectedDelivery(delivery);
    form.setFieldsValue({
      status: delivery.status,
      driver_id: delivery.driver_id,
      notes: delivery.notes
    });
    setIsUpdateModalVisible(true);
  };

  const handleUpdateSubmit = (values) => {
    updateDeliveryMutation.mutate({
      deliveryId: selectedDelivery.id,
      data: values
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
  const deliveries = data?.data?.items || [];
  const totalDeliveries = data?.meta?.total || 0;
  const pendingDeliveries = deliveries.filter(d => d.status === 'pending').length;
  const inTransitDeliveries = deliveries.filter(d => d.status === 'in_transit').length;
  const completedDeliveries = deliveries.filter(d => d.status === 'delivered').length;
  const onTimeRate = deliveries.length > 0 ? ((completedDeliveries / deliveries.length) * 100).toFixed(1) : 0;

  const getDeliveryProgress = (status) => {
    const statusOrder = ['pending', 'assigned', 'picked_up', 'in_transit', 'delivered'];
    const currentIndex = statusOrder.indexOf(status);
    return currentIndex >= 0 ? ((currentIndex + 1) / statusOrder.length) * 100 : 0;
  };

  const getTrackingSteps = (delivery) => [
    {
      title: 'Order Created',
      description: 'Delivery request created',
      status: 'finish',
      icon: <CheckCircleOutlined />
    },
    {
      title: 'Driver Assigned',
      description: delivery.driver_name || 'Waiting for assignment',
      status: ['assigned', 'picked_up', 'in_transit', 'delivered'].includes(delivery.status) ? 'finish' : 'wait',
      icon: delivery.driver_name ? <CheckCircleOutlined /> : <ClockCircleOutlined />
    },
    {
      title: 'Package Picked Up',
      description: 'Driver collected the package',
      status: ['picked_up', 'in_transit', 'delivered'].includes(delivery.status) ? 'finish' : 'wait',
      icon: ['picked_up', 'in_transit', 'delivered'].includes(delivery.status) ? <CheckCircleOutlined /> : <ClockCircleOutlined />
    },
    {
      title: 'In Transit',
      description: 'Package is on the way',
      status: ['in_transit', 'delivered'].includes(delivery.status) ? 'finish' : delivery.status === 'failed' ? 'error' : 'wait',
      icon: delivery.status === 'in_transit' ? <TruckOutlined /> : ['delivered'].includes(delivery.status) ? <CheckCircleOutlined /> : <ClockCircleOutlined />
    },
    {
      title: 'Delivered',
      description: delivery.status === 'delivered' ? 'Package delivered successfully' : delivery.status === 'failed' ? 'Delivery failed' : 'Waiting for delivery',
      status: delivery.status === 'delivered' ? 'finish' : delivery.status === 'failed' ? 'error' : 'wait',
      icon: delivery.status === 'delivered' ? <CheckCircleOutlined /> : delivery.status === 'failed' ? <ExclamationCircleOutlined /> : <ClockCircleOutlined />
    }
  ];

  return (
    <div>
      {/* Summary Cards */}
      <Row gutter={[16, 16]} style={{ marginBottom: 24 }}>
        <Col xs={24} sm={6}>
          <Card>
            <Statistic
              title="Total Deliveries"
              value={totalDeliveries}
              prefix={<TruckOutlined />}
            />
          </Card>
        </Col>
        <Col xs={24} sm={6}>
          <Card>
            <Statistic
              title="Pending"
              value={pendingDeliveries}
              valueStyle={{ color: '#faad14' }}
              prefix={<ClockCircleOutlined />}
            />
          </Card>
        </Col>
        <Col xs={24} sm={6}>
          <Card>
            <Statistic
              title="In Transit"
              value={inTransitDeliveries}
              valueStyle={{ color: '#1890ff' }}
              prefix={<TruckOutlined />}
            />
          </Card>
        </Col>
        <Col xs={24} sm={6}>
          <Card>
            <Statistic
              title="Completion Rate"
              value={onTimeRate}
              precision={1}
              suffix="%"
              valueStyle={{ color: '#52c41a' }}
              prefix={<CheckCircleOutlined />}
            />
          </Card>
        </Col>
      </Row>

      <Card>
        {/* Filter Controls */}
        <div className="table-actions">
          <Space wrap>
            <Input.Search
              placeholder="Search deliveries..."
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
              <Option value="assigned">Assigned</Option>
              <Option value="picked_up">Picked Up</Option>
              <Option value="in_transit">In Transit</Option>
              <Option value="delivered">Delivered</Option>
              <Option value="failed">Failed</Option>
              <Option value="returned">Returned</Option>
            </Select>
            <RangePicker
              onChange={handleDateRangeChange}
              format="YYYY-MM-DD"
              placeholder={['Start Date', 'End Date']}
            />
          </Space>

          <Space>
            <Button
              type="primary"
              icon={<PlusOutlined />}
              onClick={() => message.info('Create delivery functionality coming soon')}
            >
              Schedule Delivery
            </Button>
            <Button icon={<ExportOutlined />}>
              Export Report
            </Button>
          </Space>
        </div>

        {/* Deliveries Table */}
        <Table
          columns={columns}
          dataSource={deliveries}
          loading={isLoading}
          rowKey="id"
          pagination={{
            current: pagination.page,
            pageSize: pagination.per_page,
            total: totalDeliveries,
            showSizeChanger: true,
            showQuickJumper: true,
            showTotal: (total, range) =>
              `${range[0]}-${range[1]} of ${total} deliveries`
          }}
          onChange={handleTableChange}
          className="admin-table"
          scroll={{ x: 1200 }}
        />
      </Card>

      {/* Delivery Details Modal */}
      <Modal
        title={`Delivery Details - ${selectedDelivery?.delivery_id}`}
        open={isDetailModalVisible}
        onCancel={() => setIsDetailModalVisible(false)}
        footer={null}
        width={700}
      >
        {selectedDelivery && (
          <div>
            <Descriptions column={2} bordered>
              <Descriptions.Item label="Delivery ID">
                {selectedDelivery.delivery_id}
              </Descriptions.Item>
              <Descriptions.Item label="Order Number">
                {selectedDelivery.order_number}
              </Descriptions.Item>
              <Descriptions.Item label="Status">
                <Tag color={deliveryStatusColors[selectedDelivery.status]} icon={getStatusIcon(selectedDelivery.status)}>
                  {selectedDelivery.status?.toUpperCase().replace('_', ' ')}
                </Tag>
              </Descriptions.Item>
              <Descriptions.Item label="Priority">
                <Badge
                  color={selectedDelivery.priority === 'high' ? 'red' : selectedDelivery.priority === 'medium' ? 'orange' : 'green'}
                  text={selectedDelivery.priority?.toUpperCase()}
                />
              </Descriptions.Item>
              <Descriptions.Item label="Customer">
                {selectedDelivery.customer_name}
              </Descriptions.Item>
              <Descriptions.Item label="Customer Phone">
                {selectedDelivery.customer_phone}
              </Descriptions.Item>
              <Descriptions.Item label="Driver">
                {selectedDelivery.driver_name || 'Not Assigned'}
              </Descriptions.Item>
              <Descriptions.Item label="Driver Phone">
                {selectedDelivery.driver_phone || 'N/A'}
              </Descriptions.Item>
              <Descriptions.Item label="Scheduled Date" span={2}>
                {moment(selectedDelivery.scheduled_date).format('YYYY-MM-DD HH:mm')}
              </Descriptions.Item>
              <Descriptions.Item label="Delivery Address" span={2}>
                {selectedDelivery.delivery_address}
              </Descriptions.Item>
            </Descriptions>

            <Divider>Delivery Progress</Divider>
            <Progress
              percent={getDeliveryProgress(selectedDelivery.status)}
              status={selectedDelivery.status === 'delivered' ? 'success' : selectedDelivery.status === 'failed' ? 'exception' : 'active'}
              strokeColor={selectedDelivery.status === 'failed' ? '#ff4d4f' : '#52c41a'}
            />

            <div style={{ marginTop: 16, textAlign: 'right' }}>
              <Space>
                <Button
                  type="primary"
                  icon={<EnvironmentOutlined />}
                  onClick={() => {
                    setIsDetailModalVisible(false);
                    handleTrackDelivery(selectedDelivery);
                  }}
                >
                  Track Delivery
                </Button>
                <Button
                  icon={<EditOutlined />}
                  onClick={() => {
                    setIsDetailModalVisible(false);
                    handleUpdateDelivery(selectedDelivery);
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

      {/* Tracking Modal */}
      <Modal
        title={`Track Delivery - ${selectedDelivery?.delivery_id}`}
        open={isTrackingModalVisible}
        onCancel={() => setIsTrackingModalVisible(false)}
        footer={null}
        width={600}
      >
        {selectedDelivery && (
          <div>
            <div style={{ marginBottom: 24 }}>
              <h4>Current Status: <Tag color={deliveryStatusColors[selectedDelivery.status]}>
                {selectedDelivery.status?.toUpperCase().replace('_', ' ')}
              </Tag></h4>
              <p><strong>Estimated Delivery:</strong> {moment(selectedDelivery.scheduled_date).format('YYYY-MM-DD HH:mm')}</p>
            </div>

            <Steps
              direction="vertical"
              size="small"
              current={getTrackingSteps(selectedDelivery).findIndex(step => step.status === 'wait')}
            >
              {getTrackingSteps(selectedDelivery).map((step, index) => (
                <Step
                  key={index}
                  title={step.title}
                  description={step.description}
                  status={step.status}
                  icon={step.icon}
                />
              ))}
            </Steps>

            {selectedDelivery.notes && (
              <div style={{ marginTop: 24 }}>
                <Divider>Delivery Notes</Divider>
                <p>{selectedDelivery.notes}</p>
              </div>
            )}

            <div style={{ marginTop: 24, textAlign: 'right' }}>
              <Button type="primary" onClick={() => setIsTrackingModalVisible(false)}>
                Close
              </Button>
            </div>
          </div>
        )}
      </Modal>

      {/* Update Status Modal */}
      <Modal
        title={`Update Delivery - ${selectedDelivery?.delivery_id}`}
        open={isUpdateModalVisible}
        onCancel={() => setIsUpdateModalVisible(false)}
        footer={null}
      >
        <Form
          form={form}
          layout="vertical"
          onFinish={handleUpdateSubmit}
        >
          <Form.Item
            name="status"
            label="Status"
            rules={[{ required: true, message: 'Please select a status' }]}
          >
            <Select>
              <Option value="pending">Pending</Option>
              <Option value="assigned">Assigned</Option>
              <Option value="picked_up">Picked Up</Option>
              <Option value="in_transit">In Transit</Option>
              <Option value="delivered">Delivered</Option>
              <Option value="failed">Failed</Option>
              <Option value="returned">Returned</Option>
            </Select>
          </Form.Item>

          <Form.Item
            name="driver_id"
            label="Assign Driver"
          >
            <Select placeholder="Select a driver" allowClear>
              <Option value="1">John Doe</Option>
              <Option value="2">Jane Smith</Option>
              <Option value="3">Mike Johnson</Option>
            </Select>
          </Form.Item>

          <Form.Item
            name="notes"
            label="Notes"
          >
            <Input.TextArea
              rows={3}
              placeholder="Add notes about this delivery..."
            />
          </Form.Item>

          <Form.Item style={{ marginBottom: 0, textAlign: 'right' }}>
            <Space>
              <Button onClick={() => setIsUpdateModalVisible(false)}>
                Cancel
              </Button>
              <Button
                type="primary"
                htmlType="submit"
                loading={updateDeliveryMutation.isLoading}
              >
                Update Delivery
              </Button>
            </Space>
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
};

export default Delivery;