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
import { useTranslation } from 'react-i18next';
import { formatDate, formatDateTimeShort } from '../utils/dateUtils';
import adminService from '../services/adminService';

const { Option } = Select;
const { RangePicker } = DatePicker;
const { Step } = Steps;

const Delivery = () => {
  // Load delivery namespace for ui.delivery.* keys
  const { t } = useTranslation('delivery');
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
        message.success(t('ui.delivery.updated_success'));
        queryClient.invalidateQueries('deliveries');
        setIsUpdateModalVisible(false);
        form.resetFields();
      },
      onError: (error) => {
        message.error(t('ui.delivery.update_failed'));
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
      title: t('ui.delivery.delivery_id'),
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
      title: t('ui.delivery.order_number'),
      dataIndex: 'order_number',
      key: 'order_number',
      width: 120,
      render: (text) => (
        <span style={{ color: '#1890ff' }}>{text}</span>
      )
    },
    {
      title: t('ui.delivery.customer'),
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
      title: t('ui.delivery.driver'),
      dataIndex: 'driver_name',
      key: 'driver_name',
      render: (name, record) => (
        <div>
          <div>{name || t('ui.delivery.not_assigned')}</div>
          {record.driver_phone && (
            <small style={{ color: '#666' }}>{record.driver_phone}</small>
          )}
        </div>
      )
    },
    {
      title: t('ui.delivery.address'),
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
      title: t('ui.delivery.status'),
      dataIndex: 'status',
      key: 'status',
      width: 120,
      render: (status) => (
        <Tag color={deliveryStatusColors[status] || 'default'} icon={getStatusIcon(status)}>
          {t(`ui.delivery.status_${status}`)}
        </Tag>
      )
    },
    {
      title: t('ui.delivery.priority'),
      dataIndex: 'priority',
      key: 'priority',
      width: 90,
      render: (priority) => (
        <Badge
          color={priority === 'high' ? 'red' : priority === 'medium' ? 'orange' : 'green'}
          text={t(`ui.delivery.priority_${priority}`)}
        />
      )
    },
    {
      title: t('ui.delivery.scheduled'),
      dataIndex: 'scheduled_date',
      key: 'scheduled_date',
      width: 120,
      render: (date) => formatDate(date)
    },
    {
      title: t('ui.delivery.actions'),
      key: 'actions',
      width: 100,
      render: (_, record) => (
        <Dropdown
          menu={{
            items: [
              {
                key: 'track',
                label: t('ui.delivery.track_delivery'),
                icon: <EnvironmentOutlined />,
                onClick: () => handleTrackDelivery(record)
              },
              {
                key: 'details',
                label: t('ui.delivery.view_details'),
                icon: <EyeOutlined />,
                onClick: () => handleViewDelivery(record)
              },
              {
                key: 'update',
                label: t('ui.delivery.update_status'),
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
      title: t('ui.delivery.order_created'),
      description: t('ui.delivery.delivery_request_created'),
      status: 'finish',
      icon: <CheckCircleOutlined />
    },
    {
      title: t('ui.delivery.driver_assigned'),
      description: delivery.driver_name || t('ui.delivery.waiting_for_assignment'),
      status: ['assigned', 'picked_up', 'in_transit', 'delivered'].includes(delivery.status) ? 'finish' : 'wait',
      icon: delivery.driver_name ? <CheckCircleOutlined /> : <ClockCircleOutlined />
    },
    {
      title: t('ui.delivery.package_picked_up'),
      description: t('ui.delivery.driver_collected_package'),
      status: ['picked_up', 'in_transit', 'delivered'].includes(delivery.status) ? 'finish' : 'wait',
      icon: ['picked_up', 'in_transit', 'delivered'].includes(delivery.status) ? <CheckCircleOutlined /> : <ClockCircleOutlined />
    },
    {
      title: t('ui.delivery.in_transit'),
      description: t('ui.delivery.package_on_way'),
      status: ['in_transit', 'delivered'].includes(delivery.status) ? 'finish' : delivery.status === 'failed' ? 'error' : 'wait',
      icon: delivery.status === 'in_transit' ? <TruckOutlined /> : ['delivered'].includes(delivery.status) ? <CheckCircleOutlined /> : <ClockCircleOutlined />
    },
    {
      title: t('ui.delivery.delivered'),
      description: delivery.status === 'delivered' ? t('ui.delivery.package_delivered_success') : delivery.status === 'failed' ? t('ui.delivery.delivery_failed') : t('ui.delivery.waiting_for_delivery'),
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
              title={t('ui.delivery.total_deliveries')}
              value={totalDeliveries}
              prefix={<TruckOutlined />}
            />
          </Card>
        </Col>
        <Col xs={24} sm={6}>
          <Card>
            <Statistic
              title={t('ui.delivery.pending')}
              value={pendingDeliveries}
              valueStyle={{ color: '#faad14' }}
              prefix={<ClockCircleOutlined />}
            />
          </Card>
        </Col>
        <Col xs={24} sm={6}>
          <Card>
            <Statistic
              title={t('ui.delivery.in_transit')}
              value={inTransitDeliveries}
              valueStyle={{ color: '#1890ff' }}
              prefix={<TruckOutlined />}
            />
          </Card>
        </Col>
        <Col xs={24} sm={6}>
          <Card>
            <Statistic
              title={t('ui.delivery.completion_rate')}
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
              placeholder={t('ui.delivery.search_placeholder')}
              allowClear
              onSearch={handleSearch}
              style={{ width: 250 }}
            />
            <Select
              placeholder={t('ui.delivery.filter_by_status')}
              allowClear
              onChange={handleStatusFilter}
              style={{ width: 150 }}
            >
              <Option value="pending">{t('ui.delivery.status_pending')}</Option>
              <Option value="assigned">{t('ui.delivery.status_assigned')}</Option>
              <Option value="picked_up">{t('ui.delivery.status_picked_up')}</Option>
              <Option value="in_transit">{t('ui.delivery.status_in_transit')}</Option>
              <Option value="delivered">{t('ui.delivery.status_delivered')}</Option>
              <Option value="failed">{t('ui.delivery.status_failed')}</Option>
              <Option value="returned">{t('ui.delivery.status_returned')}</Option>
            </Select>
            <RangePicker
              onChange={handleDateRangeChange}
              format="YYYY-MM-DD"
              placeholder={[t('ui.delivery.start_date'), t('ui.delivery.end_date')]}
            />
          </Space>

          <Space>
            <Button
              type="primary"
              icon={<PlusOutlined />}
              onClick={() => message.info(t('ui.delivery.create_delivery_coming_soon'))}
            >
              {t('ui.delivery.schedule_delivery')}
            </Button>
            <Button icon={<ExportOutlined />}>
              {t('ui.delivery.export_report')}
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
              `${range[0]}-${range[1]} of ${total} ${t('ui.delivery.pagination_text')}`
          }}
          onChange={handleTableChange}
          className="admin-table"
          scroll={{ x: 1200 }}
        />
      </Card>

      {/* Delivery Details Modal */}
      <Modal
        title={`${t('ui.delivery.delivery_details')} - ${selectedDelivery?.delivery_id}`}
        open={isDetailModalVisible}
        onCancel={() => setIsDetailModalVisible(false)}
        footer={null}
        width={700}
      >
        {selectedDelivery && (
          <div>
            <Descriptions column={2} bordered>
              <Descriptions.Item label={t('ui.delivery.delivery_id')}>
                {selectedDelivery.delivery_id}
              </Descriptions.Item>
              <Descriptions.Item label={t('ui.delivery.order_number')}>
                {selectedDelivery.order_number}
              </Descriptions.Item>
              <Descriptions.Item label={t('ui.delivery.status')}>
                <Tag color={deliveryStatusColors[selectedDelivery.status]} icon={getStatusIcon(selectedDelivery.status)}>
                  {t(`ui.delivery.status_${selectedDelivery.status}`)}
                </Tag>
              </Descriptions.Item>
              <Descriptions.Item label={t('ui.delivery.priority')}>
                <Badge
                  color={selectedDelivery.priority === 'high' ? 'red' : selectedDelivery.priority === 'medium' ? 'orange' : 'green'}
                  text={t(`ui.delivery.priority_${selectedDelivery.priority}`)}
                />
              </Descriptions.Item>
              <Descriptions.Item label={t('ui.delivery.customer')}>
                {selectedDelivery.customer_name}
              </Descriptions.Item>
              <Descriptions.Item label={t('ui.delivery.customer_phone')}>
                {selectedDelivery.customer_phone}
              </Descriptions.Item>
              <Descriptions.Item label={t('ui.delivery.driver')}>
                {selectedDelivery.driver_name || t('ui.delivery.not_assigned')}
              </Descriptions.Item>
              <Descriptions.Item label={t('ui.delivery.driver_phone')}>
                {selectedDelivery.driver_phone || t('ui.delivery.na')}
              </Descriptions.Item>
              <Descriptions.Item label={t('ui.delivery.scheduled_date')} span={2}>
                {formatDateTimeShort(selectedDelivery.scheduled_date)}
              </Descriptions.Item>
              <Descriptions.Item label={t('ui.delivery.delivery_address')} span={2}>
                {selectedDelivery.delivery_address}
              </Descriptions.Item>
            </Descriptions>

            <Divider>{t('ui.delivery.delivery_progress')}</Divider>
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
                  {t('ui.delivery.track_delivery')}
                </Button>
                <Button
                  icon={<EditOutlined />}
                  onClick={() => {
                    setIsDetailModalVisible(false);
                    handleUpdateDelivery(selectedDelivery);
                  }}
                >
                  {t('ui.delivery.update_status')}
                </Button>
                <Button onClick={() => setIsDetailModalVisible(false)}>
                  {t('ui.delivery.close')}
                </Button>
              </Space>
            </div>
          </div>
        )}
      </Modal>

      {/* Tracking Modal */}
      <Modal
        title={`${t('ui.delivery.track_delivery_title')} - ${selectedDelivery?.delivery_id}`}
        open={isTrackingModalVisible}
        onCancel={() => setIsTrackingModalVisible(false)}
        footer={null}
        width={600}
      >
        {selectedDelivery && (
          <div>
            <div style={{ marginBottom: 24 }}>
              <h4>{t('ui.delivery.current_status')}: <Tag color={deliveryStatusColors[selectedDelivery.status]}>
                {t(`ui.delivery.status_${selectedDelivery.status}`)}
              </Tag></h4>
              <p><strong>{t('ui.delivery.estimated_delivery')}:</strong> {formatDateTimeShort(selectedDelivery.scheduled_date)}</p>
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
                <Divider>{t('ui.delivery.delivery_notes')}</Divider>
                <p>{selectedDelivery.notes}</p>
              </div>
            )}

            <div style={{ marginTop: 24, textAlign: 'right' }}>
              <Button type="primary" onClick={() => setIsTrackingModalVisible(false)}>
                {t('ui.delivery.close')}
              </Button>
            </div>
          </div>
        )}
      </Modal>

      {/* Update Status Modal */}
      <Modal
        title={`${t('ui.delivery.update_delivery')} - ${selectedDelivery?.delivery_id}`}
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
            label={t('ui.delivery.status')}
            rules={[{ required: true, message: t('ui.delivery.select_status_required') }]}
          >
            <Select>
              <Option value="pending">{t('ui.delivery.status_pending')}</Option>
              <Option value="assigned">{t('ui.delivery.status_assigned')}</Option>
              <Option value="picked_up">{t('ui.delivery.status_picked_up')}</Option>
              <Option value="in_transit">{t('ui.delivery.status_in_transit')}</Option>
              <Option value="delivered">{t('ui.delivery.status_delivered')}</Option>
              <Option value="failed">{t('ui.delivery.status_failed')}</Option>
              <Option value="returned">{t('ui.delivery.status_returned')}</Option>
            </Select>
          </Form.Item>

          <Form.Item
            name="driver_id"
            label={t('ui.delivery.assign_driver')}
          >
            <Select placeholder={t('ui.delivery.select_driver')} allowClear>
              <Option value="1">John Doe</Option>
              <Option value="2">Jane Smith</Option>
              <Option value="3">Mike Johnson</Option>
            </Select>
          </Form.Item>

          <Form.Item
            name="notes"
            label={t('ui.delivery.notes')}
          >
            <Input.TextArea
              rows={3}
              placeholder={t('ui.delivery.notes_placeholder')}
            />
          </Form.Item>

          <Form.Item style={{ marginBottom: 0, textAlign: 'right' }}>
            <Space>
              <Button onClick={() => setIsUpdateModalVisible(false)}>
                {t('ui.delivery.cancel')}
              </Button>
              <Button
                type="primary"
                htmlType="submit"
                loading={updateDeliveryMutation.isLoading}
              >
                {t('ui.delivery.update_delivery_button')}
              </Button>
            </Space>
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
};

export default Delivery;