import React, { useMemo, useState } from 'react';
import {
  Button,
  Card,
  Col,
  DatePicker,
  Descriptions,
  Drawer,
  Form,
  Input,
  InputNumber,
  Modal,
  Row,
  Select,
  Space,
  Statistic,
  Switch,
  Table,
  Tabs,
  Tag,
  Typography,
  message,
} from 'antd';
import {
  DownloadOutlined,
  EyeOutlined,
  PlusOutlined,
  SwapOutlined,
  UserAddOutlined,
} from '@ant-design/icons';
import { useMutation, useQuery, useQueryClient, keepPreviousData } from '@tanstack/react-query';
import dayjs from 'dayjs';

import adminService from '../services/adminService';
import AddressMapPicker from '../components/AddressMapPicker';
import tryoutService from '../services/tryoutService';
import { formatDate, formatDateTimeShort } from '../utils/dateUtils';
import AsyncButton from '../components/common/AsyncButton';

const { RangePicker } = DatePicker;
const { Text } = Typography;

const PICKUP_STATE_COLORS = {
  no_returnables: 'default',
  not_due: 'blue',
  due_soon: 'gold',
  overdue: 'red',
  partial: 'purple',
  returned: 'green',
};

const STATUS_COLORS = {
  draft: 'default',
  scheduled: 'gold',
  active: 'blue',
  closed: 'green',
  cancelled: 'red',
};

const OUTCOME_COLORS = {
  pending: 'default',
  converted: 'green',
  declined: 'volcano',
};

const normalizeTryoutFormValues = (values) => ({
  trial_contact: {
    first_name: values.first_name,
    last_name: values.last_name,
    phone: values.phone,
    company_name: values.company_name,
    preferred_language: values.preferred_language || 'uz',
    notes: values.contact_notes,
  },
  address: {
    label: values.address_label || 'Try-out',
    full_address: values.full_address,
    district: values.district,
    city: values.city || 'Tashkent',
    latitude: values.latitude ?? null,
    longitude: values.longitude ?? null,
    delivery_notes: values.delivery_notes,
    is_default: true,
  },
  items: (values.items || []).map((item) => ({
    product_id: item.product_id,
    quantity: item.quantity,
  })),
  notes: values.notes,
  internal_notes: values.internal_notes,
  assigned_driver_user_id: values.assigned_driver_user_id || undefined,
  complete_handoff: values.complete_handoff || false,
  return_due_at: values.return_due_at ? values.return_due_at.toISOString() : undefined,
});

const TRYOUT_FORM_INITIAL_VALUES = {
  preferred_language: 'uz',
  city: 'Tashkent',
  items: [{ product_id: undefined, quantity: 1 }],
  latitude: null,
  longitude: null,
  complete_handoff: false,
};

const toTryoutFormValues = (tryout) => ({
  ...TRYOUT_FORM_INITIAL_VALUES,
  first_name: tryout?.trial_contact?.first_name,
  last_name: tryout?.trial_contact?.last_name,
  phone: tryout?.trial_contact?.phone,
  company_name: tryout?.trial_contact?.company_name,
  preferred_language: tryout?.trial_contact?.preferred_language || 'uz',
  contact_notes: tryout?.trial_contact?.notes,
  address_label: tryout?.address_snapshot?.label,
  full_address: tryout?.address_snapshot?.full_address,
  district: tryout?.address_snapshot?.district,
  city: tryout?.address_snapshot?.city || 'Tashkent',
  latitude: tryout?.address_snapshot?.latitude ?? null,
  longitude: tryout?.address_snapshot?.longitude ?? null,
  delivery_notes: tryout?.address_snapshot?.delivery_notes,
  items: (tryout?.items || []).length
    ? tryout.items.map((item) => ({
        product_id: item.product_id,
        quantity: item.quantity,
      }))
    : TRYOUT_FORM_INITIAL_VALUES.items,
  notes: tryout?.notes,
  internal_notes: tryout?.internal_notes,
  assigned_driver_user_id:
    tryout?.assigned_handoff_driver?.user_id
    || tryout?.assigned_pickup_driver?.user_id
    || undefined,
  complete_handoff: Boolean(tryout?.handoff_completed_at),
  return_due_at: tryout?.return_due_at ? dayjs(tryout.return_due_at) : null,
  status: tryout?.status,
  outcome: tryout?.outcome,
});

const Tryouts = () => {
  const queryClient = useQueryClient();
  const [search, setSearch] = useState('');
  const [status, setStatus] = useState();
  const [outcome, setOutcome] = useState();
  const [pickupState, setPickupState] = useState();
  const [driverId, setDriverId] = useState();
  const [dateRange, setDateRange] = useState();
  const [dueDateRange, setDueDateRange] = useState();
  const [pagination, setPagination] = useState({ page: 1, per_page: 20 });
  const [selectedTryout, setSelectedTryout] = useState(null);
  const [detailOpen, setDetailOpen] = useState(false);
  const [tryoutModalMode, setTryoutModalMode] = useState(null);
  const [tryoutFormTarget, setTryoutFormTarget] = useState(null);
  const [assignOpen, setAssignOpen] = useState(false);
  const [adjustOpen, setAdjustOpen] = useState(false);
  const [assignTarget, setAssignTarget] = useState(null);
  const [adjustTarget, setAdjustTarget] = useState(null);
  const [tryoutCoordinates, setTryoutCoordinates] = useState(null);
  const [tryoutForm] = Form.useForm();
  const [assignForm] = Form.useForm();
  const [adjustForm] = Form.useForm();
  const isEditingTryout = tryoutModalMode === 'edit';
  const isTryoutModalOpen = Boolean(tryoutModalMode);

  const filters = useMemo(
    () => ({
      page: pagination.page,
      per_page: pagination.per_page,
      search: search || undefined,
      status: status || undefined,
      outcome: outcome || undefined,
      pickup_state: pickupState || undefined,
      driver_id: driverId || undefined,
      start_date: dateRange?.[0]?.format('YYYY-MM-DD'),
      end_date: dateRange?.[1]?.format('YYYY-MM-DD'),
      due_start_date: dueDateRange?.[0]?.format('YYYY-MM-DD'),
      due_end_date: dueDateRange?.[1]?.format('YYYY-MM-DD'),
    }),
    [pagination, search, status, outcome, pickupState, driverId, dateRange, dueDateRange]
  );

  const { data, isLoading } = useQuery({
    queryKey: ['tryouts', filters],
    queryFn: () => tryoutService.getTryouts(filters),
    placeholderData: keepPreviousData,
  });

  const { data: productsData } = useQuery({
    queryKey: ['tryout-products'],
    queryFn: () => adminService.getProducts({ per_page: 200, status: 'active' }),
    staleTime: 60_000,
  });

  const { data: driversData } = useQuery({
    queryKey: ['tryout-drivers'],
    queryFn: () => adminService.getDeliveryPersonnel({ per_page: 100 }),
    staleTime: 60_000,
  });

  const products = (productsData?.data?.items || []).filter((product) => product.is_tryout_eligible !== false);
  const drivers = driversData?.data?.items || [];
  const tryouts = data?.items || [];
  const summary = data?.summary || {};

  const refreshTryouts = () => {
    queryClient.invalidateQueries({
      queryKey: ['tryouts'],
    });
  };

  const closeTryoutModal = () => {
    setTryoutModalMode(null);
    setTryoutFormTarget(null);
    setTryoutCoordinates(null);
    tryoutForm.resetFields();
  };

  const openCreate = () => {
    setSelectedTryout(null);
    setTryoutFormTarget(null);
    setTryoutModalMode('create');
    setTryoutCoordinates(null);
    tryoutForm.resetFields();
    tryoutForm.setFieldsValue(TRYOUT_FORM_INITIAL_VALUES);
  };

  const createMutation = useMutation({
    mutationFn: (payload) => tryoutService.createTryout(payload),

    onSuccess: () => {
      message.success('Try-out created');
      closeTryoutModal();
      refreshTryouts();
    },
  });

  const assignMutation = useMutation({
    mutationFn: ({ taskId, assignedDriverUserId }) => tryoutService.assignTask(taskId, assignedDriverUserId),

    onSuccess: () => {
      message.success('Task assigned');
      setAssignOpen(false);
      assignForm.resetFields();
      refreshTryouts();
    },
  });

  const convertMutation = useMutation({
    mutationFn: (tryoutId) => tryoutService.convertTryout(tryoutId),

    onSuccess: ({ tryout: updatedTryout, conversion }) => {
      const action = conversion?.action;
      const user = conversion?.user;
      if (action === 'created_user' && user) {
        message.success(`Try-out converted and created user #${user.id}`);
      } else if (action === 'linked_existing_user' && user) {
        message.success(`Try-out converted and linked existing user #${user.id}`);
      } else if (action === 'already_converted' && user) {
        message.success(`Try-out already linked to user #${user.id}`);
      } else {
        message.success('Try-out converted to customer');
      }
      refreshTryouts();
      if (selectedTryout?.id === updatedTryout?.id) {
        setSelectedTryout(updatedTryout);
      }
    },
  });

  const adjustMutation = useMutation({
    mutationFn: ({ tryoutId, payload }) => tryoutService.adjustBottles(tryoutId, payload),

    onSuccess: () => {
      message.success('Bottle adjustment saved');
      setAdjustOpen(false);
      adjustForm.resetFields();
      refreshTryouts();
      if (adjustTarget?.id) {
        tryoutService.getTryout(adjustTarget.id).then(setSelectedTryout);
      }
    },
  });

  const updateMutation = useMutation({
    mutationFn: ({ tryoutId, payload }) => tryoutService.updateTryout(tryoutId, payload),

    onSuccess: (updatedTryout) => {
      message.success('Try-out updated');
      closeTryoutModal();
      setSelectedTryout(updatedTryout);
      refreshTryouts();
    },
  });

  const openDetails = async (record) => {
    const tryout = await tryoutService.getTryout(record.id);
    setSelectedTryout(tryout);
    setDetailOpen(true);
  };

  const openEdit = async (record) => {
    const tryout = record?.tasks ? record : await tryoutService.getTryout(record.id);
    setSelectedTryout(tryout);
    setTryoutFormTarget(tryout);
    setTryoutModalMode('edit');
    tryoutForm.setFieldsValue(toTryoutFormValues(tryout));
    if (tryout.address_snapshot?.latitude != null && tryout.address_snapshot?.longitude != null) {
      setTryoutCoordinates({
        latitude: tryout.address_snapshot.latitude,
        longitude: tryout.address_snapshot.longitude,
      });
    } else {
      setTryoutCoordinates(null);
    }
  };

  const handleTryoutMapCoordinateChange = (coords) => {
    setTryoutCoordinates(coords);
    tryoutForm.setFieldsValue({
      latitude: coords?.latitude ?? null,
      longitude: coords?.longitude ?? null,
    });
  };

  const handleTryoutMapAddressFound = (addressData) => {
    tryoutForm.setFieldsValue({
      full_address: addressData?.formatted_address || tryoutForm.getFieldValue('full_address'),
      district: addressData?.district || tryoutForm.getFieldValue('district'),
      city: addressData?.city || tryoutForm.getFieldValue('city') || 'Tashkent',
    });
  };

  const handleSubmitTryoutForm = (values) => {
    const basePayload = normalizeTryoutFormValues(values);
    if (isEditingTryout) {
      updateMutation.mutate({
        tryoutId: tryoutFormTarget?.id,
        payload: {
          ...basePayload,
          status: values.status || undefined,
          outcome: values.outcome || undefined,
        },
      });
      return;
    }

    createMutation.mutate(basePayload);
  };

  const handleExport = async () => {
    const blob = await tryoutService.exportTryouts(filters);
    const url = window.URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = 'tryouts_export.csv';
    link.click();
    window.URL.revokeObjectURL(url);
  };

  const columns = [
    {
      title: 'Try-out',
      dataIndex: 'tryout_number',
      key: 'tryout_number',
      render: (value) => <Text strong code>{value}</Text>,
    },
    {
      title: 'Contact',
      dataIndex: 'trial_contact',
      key: 'trial_contact',
      render: (contact, record) => (
        <div>
          <div>{contact?.full_name || 'Unknown'}</div>
          <small style={{ color: '#666' }}>{contact?.phone || '-'}</small>
          {record?.converted_user ? (
            <div>
              <small style={{ color: '#2f855a' }}>
                Linked user: {record.converted_user.full_name || 'User'} ({record.converted_user.phone || '-'})
              </small>
            </div>
          ) : null}
        </div>
      ),
    },
    {
      title: 'Status',
      dataIndex: 'status',
      key: 'status',
      // eslint-disable-next-line security/detect-object-injection
      render: (value) => <Tag color={STATUS_COLORS[value] || 'default'}>{value}</Tag>,
    },
    {
      title: 'Outcome',
      dataIndex: 'outcome',
      key: 'outcome',
      // eslint-disable-next-line security/detect-object-injection
      render: (value) => <Tag color={OUTCOME_COLORS[value] || 'default'}>{value}</Tag>,
    },
    {
      title: 'Outstanding Bottles',
      dataIndex: 'outstanding_bottles_total',
      key: 'outstanding_bottles_total',
      render: (value) => value ?? 0,
    },
    {
      title: 'Pickup State',
      dataIndex: 'pickup_state',
      key: 'pickup_state',
      // eslint-disable-next-line security/detect-object-injection
      render: (value) => <Tag color={PICKUP_STATE_COLORS[value] || 'default'}>{value}</Tag>,
    },
    {
      title: 'Due',
      dataIndex: 'return_due_at',
      key: 'return_due_at',
      render: (value) => formatDate(value),
    },
    {
      title: 'Actions',
      key: 'actions',
      render: (_, record) => (
        <Space>
          <Button icon={<EyeOutlined />} onClick={() => openDetails(record)}>View</Button>
          <Button
            icon={<SwapOutlined />}
            onClick={() => {
              setAssignTarget(record);
              setAssignOpen(true);
            }}
          >
            Assign
          </Button>
          <Button onClick={() => openEdit(record)}>
            Edit
          </Button>
          <AsyncButton
            icon={<UserAddOutlined />}
            disabled={record.outcome === 'converted'}
            onClick={() => convertMutation.mutateAsync(record.id)}
          >
            Convert
          </AsyncButton>
        </Space>
      ),
    },
  ];

  const activeTaskOptions = (assignTarget?.tasks || []).filter((task) =>
    ['open', 'assigned'].includes(task.status)
  );

  return (
    <div>
      <Row gutter={[16, 16]} style={{ marginBottom: 16 }}>
        <Col span={24}>
          <Card>
            <Row justify="space-between" align="middle" gutter={[16, 16]}>
              <Col>
                <Typography.Title level={3} style={{ margin: 0 }}>
                  Try-outs
                </Typography.Title>
                <Text type="secondary">
                  Free product handoffs and returnable bottle recovery
                </Text>
              </Col>
              <Col>
                <Space>
                  <Button icon={<DownloadOutlined />} onClick={handleExport}>
                    Export CSV
                  </Button>
                  <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
                    Create Try-out
                  </Button>
                </Space>
              </Col>
            </Row>
          </Card>
        </Col>
        <Col span={4}><Card><Statistic title="Active" value={summary.active_tryouts || 0} /></Card></Col>
        <Col span={4}><Card><Statistic title="Outstanding Bottles" value={summary.outstanding_bottles_total || 0} precision={2} /></Card></Col>
        <Col span={4}><Card><Statistic title="Due Soon" value={summary.due_soon_count || 0} /></Card></Col>
        <Col span={4}><Card><Statistic title="Overdue" value={summary.overdue_count || 0} /></Card></Col>
        <Col span={4}><Card><Statistic title="Converted" value={summary.converted_count || 0} /></Card></Col>
        <Col span={4}>
          <Card>
            <Statistic
              title="Collection Rate"
              value={summary.collection_rate || 0}
              precision={1}
              suffix="%"
            />
          </Card>
        </Col>
      </Row>

      <Card style={{ marginBottom: 16 }}>
        <Row gutter={[16, 16]}>
          <Col span={6}><Input placeholder="Search try-out / phone / name" value={search} onChange={(e) => setSearch(e.target.value)} /></Col>
          <Col span={4}>
            <Select allowClear placeholder="Status" style={{ width: '100%' }} value={status} onChange={setStatus}>
              {['draft', 'scheduled', 'active', 'closed', 'cancelled'].map((value) => (
                <Select.Option key={value} value={value}>{value}</Select.Option>
              ))}
            </Select>
          </Col>
          <Col span={4}>
            <Select allowClear placeholder="Outcome" style={{ width: '100%' }} value={outcome} onChange={setOutcome}>
              {['pending', 'converted', 'declined'].map((value) => (
                <Select.Option key={value} value={value}>{value}</Select.Option>
              ))}
            </Select>
          </Col>
          <Col span={4}>
            <Select allowClear placeholder="Pickup State" style={{ width: '100%' }} value={pickupState} onChange={setPickupState}>
              {['no_returnables', 'not_due', 'due_soon', 'overdue', 'partial', 'returned'].map((value) => (
                <Select.Option key={value} value={value}>{value}</Select.Option>
              ))}
            </Select>
          </Col>
        <Col span={6}>
            <Select allowClear placeholder="Driver" style={{ width: '100%' }} value={driverId} onChange={setDriverId}>
              {drivers.map((driver) => (
                <Select.Option key={driver.user_id || driver.id} value={driver.user_id || driver.id}>
                  {driver.full_name || driver.name || driver.phone}
                </Select.Option>
              ))}
            </Select>
          </Col>
          <Col span={4}><RangePicker style={{ width: '100%' }} value={dateRange} onChange={setDateRange} placeholder={['Created from', 'Created to']} /></Col>
          <Col span={4}><RangePicker style={{ width: '100%' }} value={dueDateRange} onChange={setDueDateRange} placeholder={['Due from', 'Due to']} /></Col>
        </Row>
      </Card>

      <Card>
        <Table
          loading={isLoading}
          columns={columns}
          dataSource={tryouts}
          rowKey="id"
          pagination={{
            current: data?.page || pagination.page,
            pageSize: data?.per_page || pagination.per_page,
            total: data?.total || 0,
            onChange: (page, per_page) => setPagination({ page, per_page }),
          }}
        />
      </Card>

      <Drawer
        title={selectedTryout?.tryout_number || 'Try-out'}
        open={detailOpen}
        width={760}
        onClose={() => setDetailOpen(false)}
        extra={(
            <Space>
              <Button onClick={() => openEdit(selectedTryout)}>
                Edit
              </Button>
              <Button onClick={() => {
                setAdjustTarget(selectedTryout);
                setAdjustOpen(true);
            }}>
              Adjust Bottles
            </Button>
            <AsyncButton
              type="primary"
              disabled={selectedTryout?.outcome === 'converted'}
              onClick={() => convertMutation.mutateAsync(selectedTryout?.id)}
            >
              Convert
            </AsyncButton>
          </Space>
        )}
      >
        {selectedTryout && (
          <Tabs
            items={[
              {
                key: 'overview',
                label: 'Overview',
                children: (
                  <Descriptions bordered column={2}>
                    <Descriptions.Item label="Contact">{selectedTryout.trial_contact?.full_name}</Descriptions.Item>
                    <Descriptions.Item label="Phone">{selectedTryout.trial_contact?.phone}</Descriptions.Item>
                    <Descriptions.Item label="Status">{selectedTryout.status}</Descriptions.Item>
                    <Descriptions.Item label="Outcome">{selectedTryout.outcome}</Descriptions.Item>
                    <Descriptions.Item label="Due">{formatDate(selectedTryout.return_due_at)}</Descriptions.Item>
                    <Descriptions.Item label="Pickup State">{selectedTryout.pickup_state}</Descriptions.Item>
                    <Descriptions.Item label="Address" span={2}>{selectedTryout.address_snapshot?.full_address}</Descriptions.Item>
                    <Descriptions.Item label="Coordinates" span={2}>
                      {selectedTryout.address_snapshot?.latitude != null && selectedTryout.address_snapshot?.longitude != null
                        ? `${selectedTryout.address_snapshot.latitude}, ${selectedTryout.address_snapshot.longitude}`
                        : '-'}
                    </Descriptions.Item>
                    <Descriptions.Item label="Converted User" span={2}>
                      {selectedTryout.converted_user
                        ? `#${selectedTryout.converted_user.id} ${selectedTryout.converted_user.full_name || 'User'} (${selectedTryout.converted_user.phone || '-'})`
                        : '-'}
                    </Descriptions.Item>
                    <Descriptions.Item label="Notes" span={2}>{selectedTryout.notes || '-'}</Descriptions.Item>
                  </Descriptions>
                ),
              },
              {
                key: 'products',
                label: 'Products',
                children: (
                  <Table
                    rowKey="id"
                    pagination={false}
                    dataSource={selectedTryout.items || []}
                    columns={[
                      { title: 'Product', dataIndex: 'product_name', key: 'product_name' },
                      { title: 'Quantity', dataIndex: 'quantity', key: 'quantity' },
                      { title: 'Price Snapshot', dataIndex: 'unit_price_snapshot', key: 'unit_price_snapshot' },
                      { title: 'Returnable Bottles Due', dataIndex: 'returnable_bottles_due', key: 'returnable_bottles_due' },
                    ]}
                  />
                ),
              },
              {
                key: 'tasks',
                label: 'Tasks',
                children: (
                  <Table
                    rowKey="id"
                    pagination={false}
                    dataSource={selectedTryout.tasks || []}
                    columns={[
                      { title: 'Type', dataIndex: 'task_type', key: 'task_type' },
                      { title: 'Status', dataIndex: 'status', key: 'status' },
                      { title: 'Driver', dataIndex: 'assigned_driver_name', key: 'assigned_driver_name' },
                      { title: 'Due', dataIndex: 'due_at', key: 'due_at', render: (value) => formatDateTimeShort(value) },
                      { title: 'Completed', dataIndex: 'completed_at', key: 'completed_at', render: (value) => formatDateTimeShort(value) },
                    ]}
                  />
                ),
              },
              {
                key: 'timeline',
                label: 'Timeline',
                children: (
                  <Table
                    rowKey="id"
                    pagination={false}
                    dataSource={selectedTryout.ledger || []}
                    columns={[
                      { title: 'Event', dataIndex: 'event_type', key: 'event_type' },
                      { title: 'Product', dataIndex: 'product_name', key: 'product_name' },
                      { title: 'Units', dataIndex: 'units', key: 'units' },
                      { title: 'Occurred', dataIndex: 'occurred_at', key: 'occurred_at', render: (value) => formatDateTimeShort(value) },
                      { title: 'Notes', dataIndex: 'notes', key: 'notes' },
                    ]}
                  />
                ),
              },
            ]}
          />
        )}
      </Drawer>

      <Modal
        title={isEditingTryout ? 'Edit Try-out' : 'Create Try-out'}
        open={isTryoutModalOpen}
        forceRender
        onCancel={closeTryoutModal}
        footer={null}
        width={820}
      >
        <Form
          form={tryoutForm}
          layout="vertical"
          initialValues={TRYOUT_FORM_INITIAL_VALUES}
          onFinish={handleSubmitTryoutForm}
        >
          <Form.Item name="latitude" hidden><Input /></Form.Item>
          <Form.Item name="longitude" hidden><Input /></Form.Item>
          <div style={{ marginBottom: 16 }}>
            <label style={{ display: 'block', marginBottom: 8, fontWeight: 500 }}>
              Select Location on Map
            </label>
            <AddressMapPicker
              value={tryoutCoordinates}
              onChange={handleTryoutMapCoordinateChange}
              onAddressFound={handleTryoutMapAddressFound}
              height={250}
              isVisible={isTryoutModalOpen}
            />
          </div>
          <Row gutter={16}>
            <Col span={8}><Form.Item name="first_name" label="First Name" rules={[{ required: true }]}><Input /></Form.Item></Col>
            <Col span={8}><Form.Item name="last_name" label="Last Name"><Input /></Form.Item></Col>
            <Col span={8}><Form.Item name="phone" label="Phone" rules={[{ required: true }]}><Input /></Form.Item></Col>
          </Row>
          <Row gutter={16}>
            <Col span={8}><Form.Item name="company_name" label="Company"><Input /></Form.Item></Col>
            <Col span={8}><Form.Item name="preferred_language" label="Language"><Select options={[{ value: 'uz', label: 'Uzbek' }, { value: 'ru', label: 'Russian' }, { value: 'en', label: 'English' }]} /></Form.Item></Col>
            <Col span={8}><Form.Item name="assigned_driver_user_id" label="Assign Driver"><Select allowClear options={drivers.map((driver) => ({ value: driver.user_id || driver.id, label: driver.full_name || driver.name || driver.phone }))} /></Form.Item></Col>
          </Row>
          <Row gutter={16}>
            <Col span={12}><Form.Item name="full_address" label="Full Address" rules={[{ required: true }]}><Input.TextArea rows={2} /></Form.Item></Col>
            <Col span={6}><Form.Item name="district" label="District"><Input /></Form.Item></Col>
            <Col span={6}><Form.Item name="city" label="City"><Input /></Form.Item></Col>
          </Row>
          <Row gutter={16}>
            <Col span={8}><Form.Item name="address_label" label="Address Label"><Input /></Form.Item></Col>
            <Col span={8}><Form.Item name="return_due_at" label="Return Due"><DatePicker showTime style={{ width: '100%' }} /></Form.Item></Col>
            <Col span={8}><Form.Item name="complete_handoff" label="Complete Handoff Now" valuePropName="checked"><Switch disabled={Boolean(tryoutFormTarget?.handoff_completed_at)} /></Form.Item></Col>
          </Row>
          <Row gutter={16}>
            <Col span={12}><Form.Item name="contact_notes" label="Contact Notes"><Input.TextArea rows={2} /></Form.Item></Col>
            <Col span={12}><Form.Item name="delivery_notes" label="Delivery Notes"><Input.TextArea rows={2} /></Form.Item></Col>
          </Row>
          <Form.List name="items">
            {(fields, { add, remove }) => (
              <>
                {fields.map(({ key, ...field }) => (
                  <Row gutter={16} key={key}>
                    <Col span={16}>
                      <Form.Item
                        {...field}
                        name={[field.name, 'product_id']}
                        label={field.name === 0 ? 'Product' : ''}
                        rules={[{ required: true }]}
                      >
                        <Select
                          showSearch
                          optionFilterProp="label"
                          disabled={Boolean(tryoutFormTarget?.handoff_completed_at)}
                          options={products.map((product) => ({
                            value: product.id,
                            label: `${product.name} ${product.tracks_returnable_bottles ? '(returnable)' : ''}`,
                          }))}
                        />
                      </Form.Item>
                    </Col>
                    <Col span={6}>
                      <Form.Item {...field} name={[field.name, 'quantity']} label={field.name === 0 ? 'Qty' : ''} rules={[{ required: true }]}>
                        <InputNumber min={1} style={{ width: '100%' }} disabled={Boolean(tryoutFormTarget?.handoff_completed_at)} />
                      </Form.Item>
                    </Col>
                    <Col span={2} style={{ display: 'flex', alignItems: 'center' }}>
                      <Button danger disabled={Boolean(tryoutFormTarget?.handoff_completed_at)} onClick={() => remove(field.name)}>Remove</Button>
                    </Col>
                  </Row>
                ))}
                <Button disabled={Boolean(tryoutFormTarget?.handoff_completed_at)} onClick={() => add({ quantity: 1 })}>Add Product</Button>
                {tryoutFormTarget?.handoff_completed_at ? (
                  <div style={{ marginTop: 8 }}>
                    <Text type="secondary">Products cannot be edited after handoff completion.</Text>
                  </div>
                ) : null}
              </>
            )}
          </Form.List>
          <Row gutter={16} style={{ marginTop: 16 }}>
            <Col span={12}><Form.Item name="notes" label="Notes"><Input.TextArea rows={2} /></Form.Item></Col>
            <Col span={12}><Form.Item name="internal_notes" label="Internal Notes"><Input.TextArea rows={2} /></Form.Item></Col>
          </Row>
          {isEditingTryout ? (
            <Row gutter={16}>
              <Col span={12}>
                <Form.Item name="status" label="Status">
                  <Select options={['draft', 'scheduled', 'active', 'closed', 'cancelled'].map((value) => ({ value, label: value }))} />
                </Form.Item>
              </Col>
              <Col span={12}>
                <Form.Item name="outcome" label="Outcome">
                  <Select options={['pending', 'converted', 'declined'].map((value) => ({ value, label: value }))} />
                </Form.Item>
              </Col>
            </Row>
          ) : null}
          <Form.Item style={{ marginBottom: 0, textAlign: 'right' }}>
            <Space>
              <Button onClick={closeTryoutModal}>Cancel</Button>
              <Button type="primary" htmlType="submit" loading={createMutation.isPending || updateMutation.isPending}>
                {isEditingTryout ? 'Save' : 'Create'}
              </Button>
            </Space>
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title="Assign Try-out Task"
        open={assignOpen}
        forceRender
        onCancel={() => setAssignOpen(false)}
        footer={null}
      >
        <Form
          form={assignForm}
          layout="vertical"
          onFinish={(values) => assignMutation.mutate(values)}
        >
          <Form.Item name="taskId" label="Task" rules={[{ required: true }]}>
            <Select
              options={activeTaskOptions.map((task) => ({
                value: task.id,
                label: `${task.task_type} / ${task.status}`,
              }))}
            />
          </Form.Item>
          <Form.Item name="assignedDriverUserId" label="Driver" rules={[{ required: true }]}>
            <Select options={drivers.map((driver) => ({ value: driver.user_id || driver.id, label: driver.full_name || driver.name || driver.phone }))} />
          </Form.Item>
          <Form.Item style={{ marginBottom: 0, textAlign: 'right' }}>
            <Space>
              <Button onClick={() => setAssignOpen(false)}>Cancel</Button>
              <Button type="primary" htmlType="submit" loading={assignMutation.isPending}>Assign</Button>
            </Space>
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title="Adjust Bottle Ledger"
        open={adjustOpen}
        forceRender
        onCancel={() => setAdjustOpen(false)}
        footer={null}
      >
        <Form
          form={adjustForm}
          layout="vertical"
          onFinish={(values) => adjustMutation.mutate({
            tryoutId: adjustTarget?.id,
            payload: values,
          })}
        >
          <Form.Item name="product_id" label="Product" rules={[{ required: true }]}>
            <Select
              options={(adjustTarget?.items || []).map((item) => ({
                value: item.product_id,
                label: item.product_name,
              }))}
            />
          </Form.Item>
          <Form.Item name="units" label="Units (+/-)" rules={[{ required: true }]}>
            <InputNumber style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item name="notes" label="Notes"><Input.TextArea rows={2} /></Form.Item>
          <Form.Item style={{ marginBottom: 0, textAlign: 'right' }}>
            <Space>
              <Button onClick={() => setAdjustOpen(false)}>Cancel</Button>
              <Button type="primary" htmlType="submit" loading={adjustMutation.isPending}>Save</Button>
            </Space>
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
};

export default Tryouts;
