import React, { useState } from 'react';
import { DEFAULT_PAGE_SIZE } from '../utils/constants';
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
  Steps,
  Progress,
  Badge,
  Divider,
  Descriptions
} from 'antd';
import {
  SearchOutlined,
  UserOutlined,
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
  CalendarOutlined,
  RedoOutlined
} from '@ant-design/icons';
import { useQuery, useMutation, useQueryClient, keepPreviousData } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { formatDate, formatDateTime, formatDateTimeShort } from '../utils/dateUtils';
import adminService from '../services/adminService';
import AssignDeliveryModal from '../components/AssignDeliveryModal';

const { Option } = Select;
const { RangePicker } = DatePicker;
const { Step } = Steps;

// Every `DeliveryStatus` in shared/enums.py, in the order the filter lists them. The
// backend accepts each as `?status=` (AdminDeliveryService.STATUS_ALIASES is derived
// from the enum). tests/unit/test_admin_ui_payload_fixture_contracts.py fails if this
// list and the enum drift apart.
const DELIVERY_STATUSES = ['scheduled', 'pending', 'rescheduled', 'assigned', 'picked_up', 'in_transit', 'arrived', 'delivered', 'failed', 'cancelled', 'returned'];

// English fallbacks for the seeded `ui.delivery.status_<value>` rows, each the seed's
// `en` value exactly (scripts/seed_backend_translations.py), so a label reads the same
// before and after the seed runs.
const DELIVERY_STATUS_FALLBACK_LABELS = {
  scheduled: 'Scheduled',
  pending: 'Pending',
  rescheduled: 'Rescheduled',
  assigned: 'Assigned',
  picked_up: 'Picked up',
  in_transit: 'In Transit',
  arrived: 'Arrived',
  delivered: 'Delivered',
  failed: 'Failed',
  cancelled: 'Cancelled',
  returned: 'Returned'
};

// Re-dispatch refusals the page explains itself, in the admin's language:
// `data.error_code` -> [translation key, English fallback]. The same Map is the list of
// codes the page asks api.js not to toast, so every refusal is shown exactly once. A Map
// because the lookup key comes off the wire (security/detect-object-injection).
const REDISPATCH_ERROR_MESSAGES = new Map([
  [
    'ORDER_NOT_RESCHEDULABLE',
    [
      'ui.delivery.redispatch_error.ORDER_NOT_RESCHEDULABLE',
      'This order is delivered, cancelled or returned, so its delivery can no longer be re-dispatched.',
    ],
  ],
  [
    'STAFF_DELIVERY_NOT_REDISPATCHABLE',
    [
      'ui.delivery.redispatch_error.STAFF_DELIVERY_NOT_REDISPATCHABLE',
      'This delivery is no longer failed, so there is nothing to re-dispatch. Refresh the list.',
    ],
  ],
]);

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
  const [assignmentTarget, setAssignmentTarget] = useState(null);
  const [pagination, setPagination] = useState({ page: 1, per_page: DEFAULT_PAGE_SIZE });
  const [form] = Form.useForm();
  const selectedStatus = Form.useWatch('status', form);
  // A failure reason belongs to a move to `failed`. Saving notes on a row that already
  // failed (the status is kept, so the backend ignores a reason) asks for none.
  const isRecordingFailure = selectedStatus === 'failed' && selectedStatus !== selectedDelivery?.status;

  const queryClient = useQueryClient();

  // eslint-disable-next-line security/detect-object-injection
  const statusLabel = (status) => t(`ui.delivery.status_${status}`, DELIVERY_STATUS_FALLBACK_LABELS[status] || status);
  const statusOptions = DELIVERY_STATUSES.map((value) => ({ value, label: statusLabel(value) }));
  // Every row opens the update form: a row with no status move (failed, delivered,
  // cancelled, returned, held) still has notes to edit, so its action says that instead.
  const updateActionLabel = (delivery) => (delivery.allowed_status_transitions?.length
    ? t('ui.delivery.update_status')
    : t('ui.delivery.edit_notes', 'Edit notes'));

  // Fetch deliveries
  const { data, isLoading } = useQuery({
    queryKey: ['deliveries', pagination, searchText, statusFilter, dateRange],

    queryFn: () => adminService.getDeliveries({
      page: pagination.page,
      per_page: pagination.per_page,
      search: searchText || undefined,
      status: statusFilter || undefined,
      start_date: dateRange?.[0]?.format('YYYY-MM-DD'),
      end_date: dateRange?.[1]?.format('YYYY-MM-DD')
    }),

    placeholderData: keepPreviousData,
  });

  // Update delivery mutation
  const updateDeliveryMutation = useMutation({
    mutationFn: ({ deliveryId, data }) => adminService.updateDelivery(deliveryId, data),

    onSuccess: (response) => {
      message.success(response?.message || t('ui.delivery.updated_success'));
      queryClient.invalidateQueries({
        queryKey: ['deliveries'],
      });
      setIsUpdateModalVisible(false);
      form.resetFields();
    },

    onError: (error) => {
      message.error(error?.response?.data?.message || t('ui.delivery.update_failed'));
    },
  });

  // Re-dispatch = reschedule to today (R18). The page explains the refusals in
  // REDISPATCH_ERROR_MESSAGES itself, so it tells api.js not to toast those too.
  const redispatchDeliveryMutation = useMutation({
    mutationFn: (deliveryId) => adminService.redispatchDelivery(deliveryId, {
      handledErrorCodes: [...REDISPATCH_ERROR_MESSAGES.keys()],
    }),

    onSuccess: (response) => {
      // Before today's first shift it lands `rescheduled`, and drivers see it only at
      // `data.release_at` (R25): say when, in local time. `release_at` is null when it
      // went straight back to the pool, and the usual message stands. Both are the
      // page's own keys: the backend's `message` is English whatever the admin reads.
      const releaseAt = response?.data?.release_at;
      if (releaseAt) {
        message.success(t(
          'ui.delivery.redispatch_held',
          "Re-dispatched. Drivers will see it when today's shift opens at {{time}}.",
          { time: formatDateTime(releaseAt, 'HH:mm') },
        ));
      } else {
        message.success(t('ui.delivery.redispatch_success', 'Delivery re-dispatched to pool'));
      }
      queryClient.invalidateQueries({
        queryKey: ['deliveries'],
      });
    },

    onError: (error) => {
      // api.js has already toasted every failure except the refusals this page
      // explains, so only those get a message here: one message per failure.
      const known = REDISPATCH_ERROR_MESSAGES.get(error?.response?.data?.data?.error_code);
      if (known) {
        message.error(t(known[0], known[1]));
      }
    },
  });

  const deliveryStatusColors = {
    scheduled: 'gold',
    pending: 'orange',
    rescheduled: 'lime',
    assigned: 'blue',
    picked_up: 'cyan',
    in_transit: 'purple',
    arrived: 'geekblue',
    delivered: 'green',
    failed: 'red',
    cancelled: 'magenta',
    returned: 'volcano'
  };

  const getStatusIcon = (status) => {
    switch (status) {
      case 'scheduled': return <CalendarOutlined />;
      case 'rescheduled': return <CalendarOutlined />;
      case 'pending': return <ClockCircleOutlined />;
      case 'assigned': return <TruckOutlined />;
      case 'picked_up': return <EnvironmentOutlined />;
      case 'in_transit': return <TruckOutlined />;
      case 'arrived': return <EnvironmentOutlined />;
      case 'delivered': return <CheckCircleOutlined />;
      case 'failed': return <ExclamationCircleOutlined />;
      case 'cancelled': return <ExclamationCircleOutlined />;
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
        // eslint-disable-next-line security/detect-object-injection
        <Tag color={deliveryStatusColors[status] || 'default'} icon={getStatusIcon(status)}>
          {statusLabel(status)}
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
          text={t(`ui.delivery.priority_${priority}`, priority)}
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
                // eslint-disable-next-line no-use-before-define
                onClick: () => handleTrackDelivery(record)
              },
              {
                key: 'details',
                label: t('ui.delivery.view_details'),
                icon: <EyeOutlined />,
                // eslint-disable-next-line no-use-before-define
                onClick: () => handleViewDelivery(record)
              },
              {
                key: 'update',
                label: updateActionLabel(record),
                icon: <EditOutlined />,
                // eslint-disable-next-line no-use-before-define
                onClick: () => handleUpdateDelivery(record)
              },
              // Each action below is offered exactly when the backend would accept it.
              // The row publishes the answer (AdminDeliveryService.serialize_delivery),
              // so the page re-derives no rule. A held (`rescheduled`) row offers none.
              ...(record.can_assign
                ? [{
                    key: 'assign',
                    label: record.driver_id
                      ? t('ui.delivery.reassign_driver', 'Reassign driver')
                      : t('ui.delivery.assign_driver'),
                    icon: <UserOutlined />,
                    // eslint-disable-next-line no-use-before-define
                    onClick: () => handleAssignDelivery(record)
                  }]
                : []),
              // Re-dispatch = reschedule to today (R18): a FAILED delivery whose order
              // is still active.
              ...(record.can_redispatch
                ? [{
                    key: 'redispatch',
                    label: t('ui.delivery.redispatch_delivery'),
                    icon: <RedoOutlined />,
                    // eslint-disable-next-line no-use-before-define
                    onClick: () => handleRedispatchDelivery(record)
                  }]
                : [])
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
      notes: delivery.notes,
      fail_reason: delivery.failed_delivery_reason || undefined
    });
    setIsUpdateModalVisible(true);
  };

  const handleAssignDelivery = (delivery) => {
    setAssignmentTarget(delivery);
  };

  const handleRedispatchDelivery = (delivery) => {
    Modal.confirm({
      title: t('ui.delivery.redispatch_confirm_title'),
      content: t('ui.delivery.redispatch_confirm_message'),
      okText: t('ui.delivery.redispatch_delivery'),
      cancelText: t('common:cancel'),
      onOk() {
        redispatchDeliveryMutation.mutate(delivery.id);
      }
    });
  };

  const handleUpdateSubmit = (values) => {
    const payload = {
      status: values.status,
      notes: values.notes
    };
    if (isRecordingFailure && values.fail_reason) {
      payload.fail_reason = values.fail_reason;
    }
    updateDeliveryMutation.mutate({
      deliveryId: selectedDelivery.id,
      data: payload
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

  const handleExportReport = () => {
    // eslint-disable-next-line no-use-before-define
    if (!deliveries.length) {
      message.info(t('ui.delivery.no_data_to_export', 'No deliveries to export'));
      return;
    }

    const rows = [
      [
        'delivery_id',
        'tracking_number',
        'order_number',
        'status',
        'customer_name',
        'customer_phone',
        'driver_name',
        'driver_phone',
        'scheduled_date',
        'address',
        'total_amount'
      ],
      // eslint-disable-next-line no-use-before-define
      ...deliveries.map((delivery) => ([
        delivery.delivery_id,
        delivery.tracking_number || '',
        delivery.order_number || '',
        delivery.status || '',
        delivery.customer_name || '',
        delivery.customer_phone || '',
        delivery.driver_name || '',
        delivery.driver_phone || '',
        delivery.scheduled_date || '',
        delivery.delivery_address || '',
        delivery.order_total_amount ?? ''
      ]))
    ];

    const csv = rows
      .map((row) => row.map((value) => `"${String(value).replace(/"/g, '""')}"`).join(','))
      .join('\n');

    const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
    const url = window.URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.setAttribute('download', `deliveries-page-${pagination.page}.csv`);
    document.body.appendChild(link);
    link.click();
    link.remove();
    window.URL.revokeObjectURL(url);
  };

  // Calculate summary statistics
  const deliveries = data?.data?.items || [];
  const summary = data?.meta?.summary || {};
  const totalDeliveries = data?.meta?.total || summary.total_deliveries || 0;
  const pendingDeliveries = (summary.scheduled_deliveries || 0) + (summary.pending_deliveries || 0);
  const activeDeliveries = summary.active_deliveries || 0;
  const completionRate = Number(summary.completion_rate || 0);

  const getDeliveryProgress = (status) => {
    if (status === 'cancelled') {
      return 100;
    }
    const statusOrder = ['scheduled', 'pending', 'assigned', 'picked_up', 'in_transit', 'arrived', 'delivered'];
    const currentIndex = statusOrder.indexOf(status);
    return currentIndex >= 0 ? ((currentIndex + 1) / statusOrder.length) * 100 : 0;
  };

  const getStatusTimestampMap = (delivery) => {
    const timestamps = {
      created: delivery.created_at || null
    };

    (delivery.status_history || []).forEach((item) => {
      if (item.new_status && item.changed_at && !timestamps[item.new_status]) {
        timestamps[item.new_status] = item.changed_at;
      }
    });

    if (delivery.actual_delivery_time) {
      timestamps.delivered = delivery.actual_delivery_time;
    }

    return timestamps;
  };

  const renderStepDescription = (messageText, timestamp) => (
    <div>
      <div>{messageText}</div>
      <small style={{ color: '#8c8c8c' }}>
        {timestamp ? formatDateTimeShort(timestamp) : t('ui.delivery.no_timestamp_available', 'No time recorded')}
      </small>
    </div>
  );

  const getTrackingSteps = (delivery) => {
    const timestamps = getStatusTimestampMap(delivery);

    return [
      {
        title: t('ui.delivery.order_created'),
        description: renderStepDescription(
          t('ui.delivery.delivery_request_created'),
          timestamps.created
        ),
        status: 'finish',
        icon: <CheckCircleOutlined />
      },
      {
        title: t('ui.delivery.driver_assigned'),
        description: renderStepDescription(
          delivery.driver_name
            ? `${t('ui.delivery.assigned_to_driver', 'Assigned to')}: ${delivery.driver_name}`
            : t('ui.delivery.waiting_for_assignment'),
          timestamps.assigned
        ),
        status: ['assigned', 'picked_up', 'in_transit', 'arrived', 'delivered'].includes(delivery.status) ? 'finish' : delivery.status === 'cancelled' ? 'error' : 'wait',
        icon: delivery.driver_name ? <CheckCircleOutlined /> : <ClockCircleOutlined />
      },
      {
        title: t('ui.delivery.package_picked_up'),
        description: renderStepDescription(
          t('ui.delivery.driver_collected_package'),
          timestamps.picked_up
        ),
        status: ['picked_up', 'in_transit', 'arrived', 'delivered'].includes(delivery.status) ? 'finish' : delivery.status === 'cancelled' ? 'error' : 'wait',
        icon: ['picked_up', 'in_transit', 'arrived', 'delivered'].includes(delivery.status) ? <CheckCircleOutlined /> : <ClockCircleOutlined />
      },
      {
        title: t('ui.delivery.in_transit'),
        description: renderStepDescription(
          t('ui.delivery.package_on_way'),
          timestamps.in_transit
        ),
        status: ['in_transit', 'arrived', 'delivered'].includes(delivery.status) ? 'finish' : ['failed', 'cancelled'].includes(delivery.status) ? 'error' : 'wait',
        icon: delivery.status === 'in_transit' ? <TruckOutlined /> : ['arrived', 'delivered'].includes(delivery.status) ? <CheckCircleOutlined /> : <ClockCircleOutlined />
      },
      {
        title: t('ui.delivery.status_arrived', 'Arrived'),
        description: renderStepDescription(
          ['arrived', 'delivered'].includes(delivery.status)
            ? t('ui.delivery.package_arrived_destination', 'Package arrived at destination')
            : t('ui.delivery.waiting_for_arrival', 'Waiting to arrive at destination'),
          timestamps.arrived
        ),
        status: ['arrived', 'delivered'].includes(delivery.status) ? 'finish' : ['failed', 'cancelled'].includes(delivery.status) ? 'error' : 'wait',
        icon: delivery.status === 'arrived' ? <EnvironmentOutlined /> : delivery.status === 'delivered' ? <CheckCircleOutlined /> : <ClockCircleOutlined />
      },
      {
        title: t('ui.delivery.delivered'),
        description: renderStepDescription(
          delivery.status === 'delivered'
            ? t('ui.delivery.package_delivered_success')
            : delivery.status === 'failed'
              ? t('ui.delivery.delivery_failed')
              : delivery.status === 'cancelled'
                ? t('ui.delivery.delivery_cancelled', 'Delivery cancelled')
              : t('ui.delivery.waiting_for_delivery'),
          timestamps.delivered || timestamps.failed || timestamps.cancelled
        ),
        status: delivery.status === 'delivered' ? 'finish' : ['failed', 'cancelled'].includes(delivery.status) ? 'error' : 'wait',
        icon: delivery.status === 'delivered' ? <CheckCircleOutlined /> : ['failed', 'cancelled'].includes(delivery.status) ? <ExclamationCircleOutlined /> : <ClockCircleOutlined />
      }
    ];
  };

  // The current status is listed but disabled: the form opens on it, so a notes-only
  // save keeps the status, yet it is never offered as a move. The moves are the
  // backend's own list; the write path refuses anything else.
  const getUpdateStatusOptions = (delivery) => {
    if (!delivery) {
      return [];
    }
    const allowed = new Set(delivery.allowed_status_transitions || []);
    return statusOptions
      .filter((option) => option.value === delivery.status || allowed.has(option.value))
      .map((option) => ({ ...option, disabled: option.value === delivery.status }));
  };

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
              title={t('ui.delivery.active_deliveries', 'Active deliveries')}
              value={activeDeliveries}
              valueStyle={{ color: '#1890ff' }}
              prefix={<TruckOutlined />}
            />
          </Card>
        </Col>
        <Col xs={24} sm={6}>
          <Card>
            <Statistic
              title={t('ui.delivery.completion_rate')}
              value={completionRate}
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
              data-testid="delivery-status-filter"
            >
              {statusOptions.map((option) => (
                <Option key={option.value} value={option.value}>
                  {option.label}
                </Option>
              ))}
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
            <Button icon={<ExportOutlined />} onClick={handleExportReport}>
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
                  {statusLabel(selectedDelivery.status)}
                </Tag>
              </Descriptions.Item>
              <Descriptions.Item label={t('ui.delivery.priority')}>
                <Badge
                  color={selectedDelivery.priority === 'high' ? 'red' : selectedDelivery.priority === 'medium' ? 'orange' : 'green'}
                  text={t(`ui.delivery.priority_${selectedDelivery.priority}`, selectedDelivery.priority)}
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
              <Descriptions.Item label={t('ui.delivery.tracking_number', 'Tracking number')} span={2}>
                {selectedDelivery.tracking_number || t('ui.delivery.na')}
              </Descriptions.Item>
              <Descriptions.Item label={t('ui.delivery.time_slot', 'Time slot')}>
                {selectedDelivery.scheduled_time_slot || t('ui.delivery.na')}
              </Descriptions.Item>
              <Descriptions.Item label={t('ui.delivery.total_amount', 'Total amount')}>
                {selectedDelivery.order_total_amount || 0}
              </Descriptions.Item>
              <Descriptions.Item label={t('ui.delivery.delivery_address')} span={2}>
                {selectedDelivery.delivery_address}
              </Descriptions.Item>
              <Descriptions.Item label={t('ui.delivery.items', 'Items')} span={2}>
                {selectedDelivery.items_summary || t('ui.delivery.na')}
              </Descriptions.Item>
              {selectedDelivery.failed_delivery_reason && (
                <Descriptions.Item label={t('ui.delivery.failure_reason', 'Failure reason')} span={2}>
                  {selectedDelivery.failed_delivery_reason}
                </Descriptions.Item>
              )}
            </Descriptions>

            <Divider>{t('ui.delivery.delivery_progress')}</Divider>
            <Progress
              percent={getDeliveryProgress(selectedDelivery.status)}
              status={selectedDelivery.status === 'delivered' ? 'success' : ['failed', 'cancelled'].includes(selectedDelivery.status) ? 'exception' : 'active'}
              strokeColor={['failed', 'cancelled'].includes(selectedDelivery.status) ? '#ff4d4f' : '#52c41a'}
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
                {selectedDelivery.can_assign && (
                  <Button
                    icon={<UserOutlined />}
                    onClick={() => {
                      setIsDetailModalVisible(false);
                      handleAssignDelivery(selectedDelivery);
                    }}
                  >
                    {selectedDelivery.driver_id
                      ? t('ui.delivery.reassign_driver', 'Reassign driver')
                      : t('ui.delivery.assign_driver')}
                  </Button>
                )}
                <Button
                  icon={<EditOutlined />}
                  onClick={() => {
                    setIsDetailModalVisible(false);
                    handleUpdateDelivery(selectedDelivery);
                  }}
                >
                  {updateActionLabel(selectedDelivery)}
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
                {statusLabel(selectedDelivery.status)}
              </Tag></h4>
              <p>
                <strong>{t('ui.delivery.scheduled_date')}:</strong> {formatDateTimeShort(selectedDelivery.scheduled_date)}
              </p>
              {selectedDelivery.status === 'delivered' && selectedDelivery.actual_delivery_time ? (
                <p>
                  <strong>{t('ui.delivery.delivered_at', 'Delivered at')}:</strong> {formatDateTimeShort(selectedDelivery.actual_delivery_time)}
                </p>
              ) : ['failed', 'cancelled'].includes(selectedDelivery.status) && selectedDelivery.updated_at ? (
                <p>
                  <strong>{selectedDelivery.status === 'cancelled' ? t('ui.delivery.cancelled_at', 'Cancelled at') : t('ui.delivery.failed_at', 'Failed at')}:</strong> {formatDateTimeShort(selectedDelivery.updated_at)}
                </p>
              ) : (
                <p>
                  <strong>{t('ui.delivery.estimated_delivery')}:</strong> {formatDateTimeShort(selectedDelivery.estimated_delivery_time || selectedDelivery.scheduled_date)}
                </p>
              )}
              {selectedDelivery.driver_name && (
                <p>
                  <strong>{t('ui.delivery.driver')}:</strong> {selectedDelivery.driver_name}
                </p>
              )}
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
            <Select data-testid="delivery-update-status">
              {getUpdateStatusOptions(selectedDelivery).map((option) => (
                <Option key={option.value} value={option.value} disabled={option.disabled}>
                  {option.label}
                </Option>
              ))}
            </Select>
          </Form.Item>

          {isRecordingFailure && (
            <Form.Item
              name="fail_reason"
              label={t('ui.delivery.failure_reason', 'Failure reason')}
              rules={[{ required: true, message: t('ui.delivery.select_failure_reason', 'Select a failure reason') }]}
            >
              <Select>
                <Option value="customer_unavailable">{t('ui.delivery.failure_customer_unavailable', 'Customer unavailable')}</Option>
                <Option value="wrong_address">{t('ui.delivery.failure_wrong_address', 'Wrong address')}</Option>
                <Option value="customer_refused">{t('ui.delivery.failure_customer_refused', 'Customer refused')}</Option>
                <Option value="product_damaged">{t('ui.delivery.failure_product_damaged', 'Product damaged')}</Option>
                <Option value="other">{t('ui.delivery.failure_other', 'Other')}</Option>
              </Select>
            </Form.Item>
          )}

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
                loading={updateDeliveryMutation.isPending}
              >
                {t('ui.delivery.update_delivery_button')}
              </Button>
            </Space>
          </Form.Item>
        </Form>
      </Modal>
      <AssignDeliveryModal
        open={Boolean(assignmentTarget)}
        onCancel={() => setAssignmentTarget(null)}
        deliveryId={assignmentTarget?.id}
        currentPersonId={assignmentTarget?.driver_id || null}
        onSuccess={() => {
          queryClient.invalidateQueries({
            queryKey: ['deliveries'],
          });
        }}
      />
    </div>
  );
};

export default Delivery;
