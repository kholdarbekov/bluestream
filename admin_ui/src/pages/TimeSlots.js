import React, { useState } from 'react';
import {
  Card,
  Table,
  Button,
  Space,
  Tag,
  Modal,
  Form,
  Input,
  InputNumber,
  Switch,
  Select,
  message,
  Popconfirm,
  TimePicker,
  Checkbox
} from 'antd';
import {
  PlusOutlined,
  EditOutlined,
  DeleteOutlined,
  ClockCircleOutlined
} from '@ant-design/icons';
import { useQuery, useMutation, useQueryClient } from 'react-query';
import adminService from '../services/adminService';
import moment from 'moment';

const { Option } = Select;

const TimeSlots = () => {
  const [isCreateModalVisible, setIsCreateModalVisible] = useState(false);
  const [isEditModalVisible, setIsEditModalVisible] = useState(false);
  const [selectedSlot, setSelectedSlot] = useState(null);
  const [pagination, setPagination] = useState({ page: 1, per_page: 20 });
  const [createForm] = Form.useForm();
  const [editForm] = Form.useForm();

  const queryClient = useQueryClient();

  const daysOfWeek = [
    { label: 'Monday', value: 0 },
    { label: 'Tuesday', value: 1 },
    { label: 'Wednesday', value: 2 },
    { label: 'Thursday', value: 3 },
    { label: 'Friday', value: 4 },
    { label: 'Saturday', value: 5 },
    { label: 'Sunday', value: 6 }
  ];

  // Fetch time slots
  const { data, isLoading } = useQuery(
    ['timeSlots', pagination],
    () => adminService.getTimeSlots({
      page: pagination.page,
      per_page: pagination.per_page
    }),
    {
      keepPreviousData: true
    }
  );

  const timeSlots = data?.data?.items || [];
  const total = data?.data?.total || 0;

  // Create mutation
  const createMutation = useMutation(
    (slotData) => adminService.createTimeSlot(slotData),
    {
      onSuccess: () => {
        message.success('Time slot created successfully');
        setIsCreateModalVisible(false);
        createForm.resetFields();
        queryClient.invalidateQueries('timeSlots');
      },
      onError: (error) => {
        message.error(error.response?.data?.message || 'Failed to create time slot');
      }
    }
  );

  // Update mutation
  const updateMutation = useMutation(
    ({ id, data }) => adminService.updateTimeSlot(id, data),
    {
      onSuccess: () => {
        message.success('Time slot updated successfully');
        setIsEditModalVisible(false);
        setSelectedSlot(null);
        editForm.resetFields();
        queryClient.invalidateQueries('timeSlots');
      },
      onError: (error) => {
        message.error(error.response?.data?.message || 'Failed to update time slot');
      }
    }
  );

  // Delete mutation
  const deleteMutation = useMutation(
    (slotId) => adminService.deleteTimeSlot(slotId),
    {
      onSuccess: () => {
        message.success('Time slot deleted successfully');
        queryClient.invalidateQueries('timeSlots');
      },
      onError: (error) => {
        message.error(error.response?.data?.message || 'Failed to delete time slot');
      }
    }
  );

  const handleCreate = () => {
    setIsCreateModalVisible(true);
  };

  const handleEdit = (slot) => {
    setSelectedSlot(slot);
    editForm.setFieldsValue({
      name: slot.name,
      start_time: moment(slot.start_time, 'HH:mm'),
      end_time: moment(slot.end_time, 'HH:mm'),
      max_orders: slot.max_orders,
      delivery_fee: slot.delivery_fee,
      is_premium: slot.is_premium,
      premium_fee: slot.premium_fee,
      is_active: slot.is_active,
      available_days: slot.available_days
    });
    setIsEditModalVisible(true);
  };

  const handleDelete = (slotId) => {
    deleteMutation.mutate(slotId);
  };

  const handleCreateSubmit = async () => {
    try {
      const values = await createForm.validateFields();

      const slotData = {
        name: values.name,
        start_time: values.start_time.format('HH:mm'),
        end_time: values.end_time.format('HH:mm'),
        max_orders: values.max_orders,
        delivery_fee: values.delivery_fee,
        is_premium: values.is_premium || false,
        premium_fee: values.premium_fee || 0,
        is_active: values.is_active !== false,
        available_days: values.available_days || [0, 1, 2, 3, 4, 5, 6]
      };

      createMutation.mutate(slotData);
    } catch (error) {
      console.error('Validation failed:', error);
    }
  };

  const handleEditSubmit = async () => {
    try {
      const values = await editForm.validateFields();

      const slotData = {
        name: values.name,
        start_time: values.start_time.format('HH:mm'),
        end_time: values.end_time.format('HH:mm'),
        max_orders: values.max_orders,
        delivery_fee: values.delivery_fee,
        is_premium: values.is_premium,
        premium_fee: values.premium_fee || 0,
        is_active: values.is_active,
        available_days: values.available_days
      };

      updateMutation.mutate({ id: selectedSlot.id, data: slotData });
    } catch (error) {
      console.error('Validation failed:', error);
    }
  };

  const columns = [
    {
      title: 'Name',
      dataIndex: 'name',
      key: 'name',
      width: 150
    },
    {
      title: 'Time Range',
      key: 'time_range',
      width: 150,
      render: (_, record) => (
        <Space>
          <ClockCircleOutlined />
          <span>{record.start_time} - {record.end_time}</span>
        </Space>
      )
    },
    {
      title: 'Max Orders',
      dataIndex: 'max_orders',
      key: 'max_orders',
      width: 100,
      align: 'center'
    },
    {
      title: 'Delivery Fee',
      dataIndex: 'delivery_fee',
      key: 'delivery_fee',
      width: 120,
      render: (fee) => `UZS ${fee.toLocaleString()}`
    },
    {
      title: 'Premium',
      dataIndex: 'is_premium',
      key: 'is_premium',
      width: 100,
      align: 'center',
      render: (isPremium, record) => isPremium ? (
        <Tag color="gold">
          Premium (+{record.premium_fee.toLocaleString()})
        </Tag>
      ) : (
        <Tag>Regular</Tag>
      )
    },
    {
      title: 'Available Days',
      dataIndex: 'available_days',
      key: 'available_days',
      width: 200,
      render: (days) => {
        if (!days || days.length === 7) return <Tag color="green">All Days</Tag>;
        return (
          <Space size={4} wrap>
            {days.map(day => (
              <Tag key={day} size="small">
                {['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'][day]}
              </Tag>
            ))}
          </Space>
        );
      }
    },
    {
      title: 'Status',
      dataIndex: 'is_active',
      key: 'is_active',
      width: 100,
      align: 'center',
      render: (isActive) => (
        <Tag color={isActive ? 'green' : 'default'}>
          {isActive ? 'Active' : 'Inactive'}
        </Tag>
      )
    },
    {
      title: 'Actions',
      key: 'actions',
      width: 150,
      fixed: 'right',
      render: (_, record) => (
        <Space>
          <Button
            type="link"
            icon={<EditOutlined />}
            onClick={() => handleEdit(record)}
            size="small"
          >
            Edit
          </Button>
          <Popconfirm
            title="Are you sure you want to delete this time slot?"
            onConfirm={() => handleDelete(record.id)}
            okText="Yes"
            cancelText="No"
          >
            <Button
              type="link"
              danger
              icon={<DeleteOutlined />}
              size="small"
            >
              Delete
            </Button>
          </Popconfirm>
        </Space>
      )
    }
  ];

  return (
    <div>
      <Card
        title="Delivery Time Slots Management"
        extra={
          <Button
            type="primary"
            icon={<PlusOutlined />}
            onClick={handleCreate}
          >
            Create Time Slot
          </Button>
        }
      >
        <Table
          columns={columns}
          dataSource={timeSlots}
          rowKey="id"
          loading={isLoading}
          pagination={{
            current: pagination.page,
            pageSize: pagination.per_page,
            total: total,
            onChange: (page, pageSize) => {
              setPagination({ page, per_page: pageSize });
            },
            showSizeChanger: true,
            showTotal: (total) => `Total ${total} time slots`
          }}
          scroll={{ x: 1200 }}
        />
      </Card>

      {/* Create Modal */}
      <Modal
        title="Create Time Slot"
        open={isCreateModalVisible}
        onOk={handleCreateSubmit}
        onCancel={() => {
          setIsCreateModalVisible(false);
          createForm.resetFields();
        }}
        confirmLoading={createMutation.isLoading}
        width={600}
      >
        <Form
          form={createForm}
          layout="vertical"
          initialValues={{
            is_active: true,
            is_premium: false,
            premium_fee: 0,
            available_days: [0, 1, 2, 3, 4, 5, 6]
          }}
        >
          <Form.Item
            name="name"
            label="Name"
            rules={[{ required: true, message: 'Please enter time slot name' }]}
          >
            <Input placeholder="e.g., Morning, Afternoon, Evening" />
          </Form.Item>

          <Space style={{ width: '100%' }} size="large">
            <Form.Item
              name="start_time"
              label="Start Time"
              rules={[{ required: true, message: 'Please select start time' }]}
            >
              <TimePicker format="HH:mm" style={{ width: 200 }} />
            </Form.Item>

            <Form.Item
              name="end_time"
              label="End Time"
              rules={[{ required: true, message: 'Please select end time' }]}
            >
              <TimePicker format="HH:mm" style={{ width: 200 }} />
            </Form.Item>
          </Space>

          <Form.Item
            name="max_orders"
            label="Maximum Orders"
            rules={[{ required: true, message: 'Please enter maximum orders' }]}
          >
            <InputNumber min={1} max={1000} style={{ width: '100%' }} />
          </Form.Item>

          <Form.Item
            name="delivery_fee"
            label="Delivery Fee (UZS)"
            rules={[{ required: true, message: 'Please enter delivery fee' }]}
          >
            <InputNumber min={0} step={1000} style={{ width: '100%' }} />
          </Form.Item>

          <Form.Item
            name="is_premium"
            label="Premium Slot"
            valuePropName="checked"
          >
            <Switch />
          </Form.Item>

          <Form.Item
            name="premium_fee"
            label="Premium Fee (UZS)"
            tooltip="Additional fee for premium slots"
          >
            <InputNumber min={0} step={1000} style={{ width: '100%' }} />
          </Form.Item>

          <Form.Item
            name="available_days"
            label="Available Days"
          >
            <Checkbox.Group options={daysOfWeek} />
          </Form.Item>

          <Form.Item
            name="is_active"
            label="Active"
            valuePropName="checked"
          >
            <Switch />
          </Form.Item>
        </Form>
      </Modal>

      {/* Edit Modal */}
      <Modal
        title="Edit Time Slot"
        open={isEditModalVisible}
        onOk={handleEditSubmit}
        onCancel={() => {
          setIsEditModalVisible(false);
          setSelectedSlot(null);
          editForm.resetFields();
        }}
        confirmLoading={updateMutation.isLoading}
        width={600}
      >
        <Form
          form={editForm}
          layout="vertical"
        >
          <Form.Item
            name="name"
            label="Name"
            rules={[{ required: true, message: 'Please enter time slot name' }]}
          >
            <Input placeholder="e.g., Morning, Afternoon, Evening" />
          </Form.Item>

          <Space style={{ width: '100%' }} size="large">
            <Form.Item
              name="start_time"
              label="Start Time"
              rules={[{ required: true, message: 'Please select start time' }]}
            >
              <TimePicker format="HH:mm" style={{ width: 200 }} />
            </Form.Item>

            <Form.Item
              name="end_time"
              label="End Time"
              rules={[{ required: true, message: 'Please select end time' }]}
            >
              <TimePicker format="HH:mm" style={{ width: 200 }} />
            </Form.Item>
          </Space>

          <Form.Item
            name="max_orders"
            label="Maximum Orders"
            rules={[{ required: true, message: 'Please enter maximum orders' }]}
          >
            <InputNumber min={1} max={1000} style={{ width: '100%' }} />
          </Form.Item>

          <Form.Item
            name="delivery_fee"
            label="Delivery Fee (UZS)"
            rules={[{ required: true, message: 'Please enter delivery fee' }]}
          >
            <InputNumber min={0} step={1000} style={{ width: '100%' }} />
          </Form.Item>

          <Form.Item
            name="is_premium"
            label="Premium Slot"
            valuePropName="checked"
          >
            <Switch />
          </Form.Item>

          <Form.Item
            name="premium_fee"
            label="Premium Fee (UZS)"
            tooltip="Additional fee for premium slots"
          >
            <InputNumber min={0} step={1000} style={{ width: '100%' }} />
          </Form.Item>

          <Form.Item
            name="available_days"
            label="Available Days"
          >
            <Checkbox.Group options={daysOfWeek} />
          </Form.Item>

          <Form.Item
            name="is_active"
            label="Active"
            valuePropName="checked"
          >
            <Switch />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
};

export default TimeSlots;
