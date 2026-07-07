import React, { useState } from 'react';
import { DEFAULT_PAGE_SIZE } from '../utils/constants';
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
import { useQuery, useMutation, useQueryClient, keepPreviousData } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import adminService from '../services/adminService';
import dayjs from 'dayjs';
import customParseFormat from 'dayjs/plugin/customParseFormat';

dayjs.extend(customParseFormat);

const { Option } = Select;

const DAY_ABBREVIATIONS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
const DAY_ABBR_KEYS = ['day_mon', 'day_tue', 'day_wed', 'day_thu', 'day_fri', 'day_sat', 'day_sun'];

const TimeSlots = () => {
  const { t } = useTranslation('time_slots');
  const [isCreateModalVisible, setIsCreateModalVisible] = useState(false);
  const [isEditModalVisible, setIsEditModalVisible] = useState(false);
  const [selectedSlot, setSelectedSlot] = useState(null);
  const [pagination, setPagination] = useState({ page: 1, per_page: DEFAULT_PAGE_SIZE });
  const [createForm] = Form.useForm();
  const [editForm] = Form.useForm();

  const queryClient = useQueryClient();

  const daysOfWeek = [
    { label: t('monday', { defaultValue: 'Monday' }), value: 0 },
    { label: t('tuesday', { defaultValue: 'Tuesday' }), value: 1 },
    { label: t('wednesday', { defaultValue: 'Wednesday' }), value: 2 },
    { label: t('thursday', { defaultValue: 'Thursday' }), value: 3 },
    { label: t('friday', { defaultValue: 'Friday' }), value: 4 },
    { label: t('saturday', { defaultValue: 'Saturday' }), value: 5 },
    { label: t('sunday', { defaultValue: 'Sunday' }), value: 6 }
  ];

  // Fetch time slots
  const { data, isLoading } = useQuery({
    queryKey: ['timeSlots', pagination],

    queryFn: () => adminService.getTimeSlots({
      page: pagination.page,
      per_page: pagination.per_page
    }),

    placeholderData: keepPreviousData,
  });

  const timeSlots = data?.data?.items || [];
  const total = data?.data?.total || 0;

  // Create mutation
  const createMutation = useMutation({
    mutationFn: (slotData) => adminService.createTimeSlot(slotData),

    onSuccess: () => {
      message.success(t('created', { defaultValue: 'Time slot created successfully' }));
      setIsCreateModalVisible(false);
      createForm.resetFields();
      queryClient.invalidateQueries({
        queryKey: ['timeSlots'],
      });
    },

    onError: (error) => {
      message.error(error.response?.data?.message || t('create_failed', { defaultValue: 'Failed to create time slot' }));
    },
  });

  // Update mutation
  const updateMutation = useMutation({
    mutationFn: ({ id, data }) => adminService.updateTimeSlot(id, data),

    onSuccess: () => {
      message.success(t('updated', { defaultValue: 'Time slot updated successfully' }));
      setIsEditModalVisible(false);
      setSelectedSlot(null);
      editForm.resetFields();
      queryClient.invalidateQueries({
        queryKey: ['timeSlots'],
      });
    },

    onError: (error) => {
      message.error(error.response?.data?.message || t('update_failed', { defaultValue: 'Failed to update time slot' }));
    },
  });

  // Delete mutation
  const deleteMutation = useMutation({
    mutationFn: (slotId) => adminService.deleteTimeSlot(slotId),

    onSuccess: () => {
      message.success(t('deleted', { defaultValue: 'Time slot deleted successfully' }));
      queryClient.invalidateQueries({
        queryKey: ['timeSlots'],
      });
    },

    onError: (error) => {
      message.error(error.response?.data?.message || t('delete_failed', { defaultValue: 'Failed to delete time slot' }));
    },
  });

  const handleCreate = () => {
    setIsCreateModalVisible(true);
  };

  const handleEdit = (slot) => {
    setSelectedSlot(slot);
    editForm.setFieldsValue({
      name: slot.name,
      start_time: dayjs(slot.start_time, 'HH:mm'),
      end_time: dayjs(slot.end_time, 'HH:mm'),
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
      title: t('name', { defaultValue: 'Name' }),
      dataIndex: 'name',
      key: 'name',
      width: 150
    },
    {
      title: t('time_range', { defaultValue: 'Time Range' }),
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
      title: t('max_orders', { defaultValue: 'Max Orders' }),
      dataIndex: 'max_orders',
      key: 'max_orders',
      width: 100,
      align: 'center'
    },
    {
      title: t('delivery_fee', { defaultValue: 'Delivery Fee' }),
      dataIndex: 'delivery_fee',
      key: 'delivery_fee',
      width: 120,
      render: (fee) => `UZS ${fee.toLocaleString()}`
    },
    {
      title: t('premium', { defaultValue: 'Premium' }),
      dataIndex: 'is_premium',
      key: 'is_premium',
      width: 100,
      align: 'center',
      render: (isPremium, record) => isPremium ? (
        <Tag color="gold">
          {t('premium_tag', { fee: record.premium_fee.toLocaleString(), defaultValue: 'Premium (+{{fee}})' })}
        </Tag>
      ) : (
        <Tag>{t('regular', { defaultValue: 'Regular' })}</Tag>
      )
    },
    {
      title: t('available_days', { defaultValue: 'Available Days' }),
      dataIndex: 'available_days',
      key: 'available_days',
      width: 200,
      render: (days) => {
        if (!days || days.length === 7) return <Tag color="green">{t('all_days', { defaultValue: 'All Days' })}</Tag>;
        return (
          <Space size={4} wrap>
            {days.map(day => (
              <Tag key={day} size="small">
                {/* eslint-disable-next-line security/detect-object-injection */}
                {t(DAY_ABBR_KEYS[day], { defaultValue: DAY_ABBREVIATIONS[day] })}
              </Tag>
            ))}
          </Space>
        );
      }
    },
    {
      title: t('status', { defaultValue: 'Status' }),
      dataIndex: 'is_active',
      key: 'is_active',
      width: 100,
      align: 'center',
      render: (isActive) => (
        <Tag color={isActive ? 'green' : 'default'}>
          {isActive ? t('active', { defaultValue: 'Active' }) : t('inactive', { defaultValue: 'Inactive' })}
        </Tag>
      )
    },
    {
      title: t('actions', { defaultValue: 'Actions' }),
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
            {t('edit', { defaultValue: 'Edit' })}
          </Button>
          <Popconfirm
            title={t('delete_confirm', { defaultValue: 'Are you sure you want to delete this time slot?' })}
            onConfirm={() => handleDelete(record.id)}
            okText={t('yes', { defaultValue: 'Yes' })}
            cancelText={t('no', { defaultValue: 'No' })}
          >
            <Button
              type="link"
              danger
              icon={<DeleteOutlined />}
              size="small"
            >
              {t('delete', { defaultValue: 'Delete' })}
            </Button>
          </Popconfirm>
        </Space>
      )
    }
  ];

  return (
    <div>
      <Card
        title={t('page_title', { defaultValue: 'Delivery Time Slots Management' })}
        extra={
          <Button
            type="primary"
            icon={<PlusOutlined />}
            onClick={handleCreate}
          >
            {t('create_time_slot', { defaultValue: 'Create Time Slot' })}
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
            total,
            onChange: (page, pageSize) => {
              setPagination({ page, per_page: pageSize });
            },
            showSizeChanger: true,
            showTotal: (total) => t('total_time_slots', { count: total, defaultValue: 'Total {{count}} time slots' })
          }}
          scroll={{ x: 1200 }}
        />
      </Card>

      {/* Create Modal */}
      <Modal
        title={t('create_time_slot', { defaultValue: 'Create Time Slot' })}
        open={isCreateModalVisible}
        onOk={handleCreateSubmit}
        onCancel={() => {
          setIsCreateModalVisible(false);
          createForm.resetFields();
        }}
        confirmLoading={createMutation.isPending}
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
            label={t('name', { defaultValue: 'Name' })}
            rules={[{ required: true, message: t('name_required', { defaultValue: 'Please enter time slot name' }) }]}
          >
            <Input placeholder={t('name_placeholder', { defaultValue: 'e.g., Morning, Afternoon, Evening' })} />
          </Form.Item>

          <Space style={{ width: '100%' }} size="large">
            <Form.Item
              name="start_time"
              label={t('start_time', { defaultValue: 'Start Time' })}
              rules={[{ required: true, message: t('start_time_required', { defaultValue: 'Please select start time' }) }]}
            >
              <TimePicker format="HH:mm" style={{ width: 200 }} />
            </Form.Item>

            <Form.Item
              name="end_time"
              label={t('end_time', { defaultValue: 'End Time' })}
              rules={[{ required: true, message: t('end_time_required', { defaultValue: 'Please select end time' }) }]}
            >
              <TimePicker format="HH:mm" style={{ width: 200 }} />
            </Form.Item>
          </Space>

          <Form.Item
            name="max_orders"
            label={t('max_orders', { defaultValue: 'Max Orders' })}
            rules={[{ required: true, message: t('max_orders_required', { defaultValue: 'Please enter maximum orders' }) }]}
          >
            <InputNumber min={1} max={1000} style={{ width: '100%' }} />
          </Form.Item>

          <Form.Item
            name="delivery_fee"
            label={t('delivery_fee_uzs', { defaultValue: 'Delivery Fee (UZS)' })}
            rules={[{ required: true, message: t('delivery_fee_required', { defaultValue: 'Please enter delivery fee' }) }]}
          >
            <InputNumber min={0} step={1000} style={{ width: '100%' }} />
          </Form.Item>

          <Form.Item
            name="is_premium"
            label={t('premium_slot', { defaultValue: 'Premium Slot' })}
            valuePropName="checked"
          >
            <Switch />
          </Form.Item>

          <Form.Item
            name="premium_fee"
            label={t('premium_fee_uzs', { defaultValue: 'Premium Fee (UZS)' })}
            tooltip={t('premium_fee_tooltip', { defaultValue: 'Additional fee for premium slots' })}
          >
            <InputNumber min={0} step={1000} style={{ width: '100%' }} />
          </Form.Item>

          <Form.Item
            name="available_days"
            label={t('available_days', { defaultValue: 'Available Days' })}
          >
            <Checkbox.Group options={daysOfWeek} />
          </Form.Item>

          <Form.Item
            name="is_active"
            label={t('active', { defaultValue: 'Active' })}
            valuePropName="checked"
          >
            <Switch />
          </Form.Item>
        </Form>
      </Modal>

      {/* Edit Modal */}
      <Modal
        title={t('edit_time_slot', { defaultValue: 'Edit Time Slot' })}
        open={isEditModalVisible}
        onOk={handleEditSubmit}
        onCancel={() => {
          setIsEditModalVisible(false);
          setSelectedSlot(null);
          editForm.resetFields();
        }}
        confirmLoading={updateMutation.isPending}
        width={600}
      >
        <Form
          form={editForm}
          layout="vertical"
        >
          <Form.Item
            name="name"
            label={t('name', { defaultValue: 'Name' })}
            rules={[{ required: true, message: t('name_required', { defaultValue: 'Please enter time slot name' }) }]}
          >
            <Input placeholder={t('name_placeholder', { defaultValue: 'e.g., Morning, Afternoon, Evening' })} />
          </Form.Item>

          <Space style={{ width: '100%' }} size="large">
            <Form.Item
              name="start_time"
              label={t('start_time', { defaultValue: 'Start Time' })}
              rules={[{ required: true, message: t('start_time_required', { defaultValue: 'Please select start time' }) }]}
            >
              <TimePicker format="HH:mm" style={{ width: 200 }} />
            </Form.Item>

            <Form.Item
              name="end_time"
              label={t('end_time', { defaultValue: 'End Time' })}
              rules={[{ required: true, message: t('end_time_required', { defaultValue: 'Please select end time' }) }]}
            >
              <TimePicker format="HH:mm" style={{ width: 200 }} />
            </Form.Item>
          </Space>

          <Form.Item
            name="max_orders"
            label={t('max_orders', { defaultValue: 'Max Orders' })}
            rules={[{ required: true, message: t('max_orders_required', { defaultValue: 'Please enter maximum orders' }) }]}
          >
            <InputNumber min={1} max={1000} style={{ width: '100%' }} />
          </Form.Item>

          <Form.Item
            name="delivery_fee"
            label={t('delivery_fee_uzs', { defaultValue: 'Delivery Fee (UZS)' })}
            rules={[{ required: true, message: t('delivery_fee_required', { defaultValue: 'Please enter delivery fee' }) }]}
          >
            <InputNumber min={0} step={1000} style={{ width: '100%' }} />
          </Form.Item>

          <Form.Item
            name="is_premium"
            label={t('premium_slot', { defaultValue: 'Premium Slot' })}
            valuePropName="checked"
          >
            <Switch />
          </Form.Item>

          <Form.Item
            name="premium_fee"
            label={t('premium_fee_uzs', { defaultValue: 'Premium Fee (UZS)' })}
            tooltip={t('premium_fee_tooltip', { defaultValue: 'Additional fee for premium slots' })}
          >
            <InputNumber min={0} step={1000} style={{ width: '100%' }} />
          </Form.Item>

          <Form.Item
            name="available_days"
            label={t('available_days', { defaultValue: 'Available Days' })}
          >
            <Checkbox.Group options={daysOfWeek} />
          </Form.Item>

          <Form.Item
            name="is_active"
            label={t('active', { defaultValue: 'Active' })}
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
