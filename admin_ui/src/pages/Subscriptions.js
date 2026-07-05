import React, { useMemo, useState } from 'react';
import {
  Alert, Button, Card, Col, DatePicker, Descriptions, Divider, Drawer,
  Form, Input, InputNumber, Modal, Popconfirm, Row, Select, Space, Switch,
  Table, Tag, Typography, message,
} from 'antd';
import {
  CloseCircleOutlined, DeleteOutlined, DollarOutlined, EditOutlined, EyeOutlined,
  PauseCircleOutlined, PlayCircleOutlined, PlusOutlined,
} from '@ant-design/icons';
import { useMutation, useQuery, useQueryClient, keepPreviousData } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import dayjs from 'dayjs';
import adminService from '../services/adminService';
import { formatDate, formatDateTime } from '../utils/dateUtils';
import EmptyState from '../components/common/EmptyState';
import { buildSubscriptionPayload } from '../utils/subscriptionPayload';

const DEFAULT_PAGE = { page: 1, per_page: 20 };
const STATUS_COLORS = { active: 'green', paused: 'orange', cancelled: 'red', expired: 'default', trial: 'blue' };
const STATUS_OPTIONS = ['active', 'paused', 'cancelled', 'expired', 'trial'];
const FREQUENCY_OPTIONS = ['daily', 'weekly', 'biweekly', 'monthly'];
const FREQUENCY_RANK = { daily: 1, weekly: 2, biweekly: 3, monthly: 4 };
const PAYMENT_METHODS = ['cash', 'card', 'payme', 'click', 'business_account'];

const mapDetailToForm = (d) => ({
  name: d.name,
  description: d.description,
  billing_cycle: d.billing_cycle,
  delivery_frequency: d.delivery_frequency,
  delivery_day_of_week: d.delivery_day_of_week,
  delivery_day_of_month: d.delivery_day_of_month,
  delivery_time_slot_id: d.delivery_time_slot_id ?? d.delivery_time_slot?.id,
  delivery_address_id: d.delivery_address_id,
  payment_method: d.payment_method,
  auto_payment: d.auto_payment,
  auto_renew: d.auto_renew,
  discount_percentage: d.discount_percentage,
  loyalty_points_multiplier: d.loyalty_points_multiplier,
  start_date: d.start_date ? dayjs(d.start_date) : null,
  end_date: d.end_date ? dayjs(d.end_date) : null,
  billing_amount: d.billing_amount,
  next_billing_date: d.next_billing_date ? dayjs(d.next_billing_date) : null,
  last_billing_date: d.last_billing_date ? dayjs(d.last_billing_date) : null,
  override_edit_any_status: false,
  override_manual_billing_amount: false,
  override_manual_billing_dates: false,
});

const Subscriptions = () => {
  const { t } = useTranslation('subscriptions');
  const queryClient = useQueryClient();

  const [search, setSearch] = useState('');
  const [status, setStatus] = useState();
  const [billingCycle, setBillingCycle] = useState();
  const [pagination, setPagination] = useState(DEFAULT_PAGE);

  const [modal, setModal] = useState({ open: false, subscription: null });
  const [drawerId, setDrawerId] = useState(null);
  const [userSearch, setUserSearch] = useState('');
  const [addItemValue, setAddItemValue] = useState({ product_id: undefined, quantity: 1 });
  const [qtyDrafts, setQtyDrafts] = useState({});

  const [form] = Form.useForm();
  const watchedUserId = Form.useWatch('user_id', form);
  const watchedDeliveryFreq = Form.useWatch('delivery_frequency', form);
  const manualAmount = Form.useWatch('override_manual_billing_amount', form);
  const manualDates = Form.useWatch('override_manual_billing_dates', form);

  const isEdit = Boolean(modal.subscription);
  const formUserId = isEdit ? modal.subscription.user_id : watchedUserId;

  const listQuery = useQuery({
    queryKey: ['admin-subscriptions', pagination, search, status, billingCycle],
    queryFn: () => adminService.getSubscriptions({
      page: pagination.page, per_page: pagination.per_page, search, status, billing_cycle: billingCycle,
    }),
    placeholderData: keepPreviousData,
  });

  const usersQuery = useQuery({
    queryKey: ['admin-subscription-users', userSearch],
    queryFn: () => adminService.getUsers({ search: userSearch, per_page: 20 }),
    enabled: modal.open && !isEdit,
    placeholderData: keepPreviousData,
  });

  const productsQuery = useQuery({
    queryKey: ['admin-subscription-products'],
    queryFn: () => adminService.getProducts({ page: 1, per_page: 100, is_active: true }),
    enabled: modal.open || Boolean(drawerId),
    placeholderData: keepPreviousData,
  });

  const timeSlotsQuery = useQuery({
    queryKey: ['admin-subscription-timeslots'],
    queryFn: () => adminService.getTimeSlots({ page: 1, per_page: 100 }),
    enabled: modal.open,
    placeholderData: keepPreviousData,
  });

  const addressesQuery = useQuery({
    queryKey: ['admin-subscription-addresses', formUserId],
    queryFn: () => adminService.getUserAddresses(formUserId),
    enabled: modal.open && Boolean(formUserId),
    placeholderData: keepPreviousData,
  });

  const detailQuery = useQuery({
    queryKey: ['admin-subscription-detail', drawerId],
    queryFn: () => adminService.getSubscription(drawerId),
    enabled: Boolean(drawerId),
  });

  const rows = listQuery.data?.items || [];
  const total = listQuery.data?.total || 0;
  const users = usersQuery.data?.data?.items || usersQuery.data?.items || [];
  const products = productsQuery.data?.data?.items || productsQuery.data?.items || [];
  const timeSlots = timeSlotsQuery.data?.data?.items || timeSlotsQuery.data?.items || [];
  const addresses = addressesQuery.data?.data?.addresses || addressesQuery.data?.addresses || [];
  const detail = detailQuery.data || {};

  const invalidateList = () => queryClient.invalidateQueries({ queryKey: ['admin-subscriptions'] });
  const invalidateDetail = () => queryClient.invalidateQueries({ queryKey: ['admin-subscription-detail'] });

  const createMutation = useMutation({
    mutationFn: (payload) => adminService.createSubscription(payload),
    onSuccess: () => {
      message.success(t('created', { defaultValue: 'Subscription created' }));
      setModal({ open: false, subscription: null });
      form.resetFields();
      invalidateList();
    },
    onError: (e) => message.error(e.response?.data?.message || t('create_failed', { defaultValue: 'Failed to create subscription' })),
  });

  const updateMutation = useMutation({
    mutationFn: ({ id, payload }) => adminService.updateSubscription(id, payload),
    onSuccess: () => {
      message.success(t('updated', { defaultValue: 'Subscription updated' }));
      setModal({ open: false, subscription: null });
      form.resetFields();
      invalidateList();
      invalidateDetail();
    },
    onError: (e) => message.error(e.response?.data?.message || t('update_failed', { defaultValue: 'Failed to update subscription' })),
  });

  const lifecycleMutation = useMutation({
    mutationFn: ({ action, id }) => {
      if (action === 'pause') return adminService.pauseSubscription(id, { pause_reason: 'Paused by administrator' });
      if (action === 'resume') return adminService.resumeSubscription(id);
      if (action === 'cancel') return adminService.cancelSubscription(id, { cancellation_reason: 'Cancelled by administrator' });
      return adminService.processSubscriptionBilling(id);
    },
    onSuccess: () => {
      message.success(t('action_done', { defaultValue: 'Done' }));
      invalidateList();
      invalidateDetail();
    },
    onError: (e) => message.error(e.response?.data?.message || t('action_failed', { defaultValue: 'Action failed' })),
  });

  const addItemMutation = useMutation({
    mutationFn: ({ id, payload }) => adminService.addSubscriptionItem(id, payload),
    onSuccess: () => {
      message.success(t('item_added', { defaultValue: 'Item added' }));
      setAddItemValue({ product_id: undefined, quantity: 1 });
      invalidateDetail();
      invalidateList();
    },
    onError: (e) => message.error(e.response?.data?.message || t('item_failed', { defaultValue: 'Failed' })),
  });

  const updateItemMutation = useMutation({
    mutationFn: ({ id, itemId, payload }) => adminService.updateSubscriptionItem(id, itemId, payload),
    onSuccess: () => { message.success(t('item_updated', { defaultValue: 'Item updated' })); setQtyDrafts({}); invalidateDetail(); invalidateList(); },
    onError: (e) => message.error(e.response?.data?.message || t('item_failed', { defaultValue: 'Failed' })),
  });

  const removeItemMutation = useMutation({
    mutationFn: ({ id, itemId }) => adminService.removeSubscriptionItem(id, itemId),
    onSuccess: () => { message.success(t('item_removed', { defaultValue: 'Item removed' })); invalidateDetail(); invalidateList(); },
    onError: (e) => message.error(e.response?.data?.message || t('item_failed', { defaultValue: 'Failed' })),
  });

  const openCreate = () => {
    setModal({ open: true, subscription: null });
    form.resetFields();
    form.setFieldsValue({
      billing_cycle: 'monthly',
      delivery_frequency: 'weekly',
      payment_method: 'cash',
      auto_payment: true,
      auto_renew: true,
      discount_percentage: 0,
      items: [{ quantity: 1 }],
    });
  };

  const openEdit = async (record) => {
    const d = await adminService.getSubscription(record.id);
    setModal({ open: true, subscription: { ...d, user_id: d.user?.id ?? record.user_id } });
    form.resetFields();
    form.setFieldsValue(mapDetailToForm(d));
  };

  const closeModal = () => { setModal({ open: false, subscription: null }); form.resetFields(); };

  const onFinish = (values) => {
    const payload = buildSubscriptionPayload(values, { isEdit });
    if (isEdit) updateMutation.mutate({ id: modal.subscription.id, payload });
    else createMutation.mutate(payload);
  };

  const billingDisabled = (value) => {
    if (!watchedDeliveryFreq) return false;
    // eslint-disable-next-line security/detect-object-injection
    return FREQUENCY_RANK[value] < FREQUENCY_RANK[watchedDeliveryFreq];
  };

  const columns = useMemo(() => ([
    { title: t('number', { defaultValue: 'Number' }), dataIndex: 'subscription_number', key: 'subscription_number' },
    {
      title: t('customer', { defaultValue: 'Customer' }), key: 'customer',
      render: (_, r) => (<div><div>{r.user_name}</div><Typography.Text type="secondary">{r.user_email}</Typography.Text></div>),
    },
    {
      title: t('status', { defaultValue: 'Status' }), dataIndex: 'status', key: 'status', width: 110,
      // eslint-disable-next-line security/detect-object-injection
      render: (v) => <Tag color={STATUS_COLORS[v] || 'default'}>{v}</Tag>,
    },
    { title: t('billing_cycle', { defaultValue: 'Billing' }), dataIndex: 'billing_cycle', key: 'billing_cycle', width: 100 },
    {
      title: t('billing_amount', { defaultValue: 'Amount' }), dataIndex: 'billing_amount', key: 'billing_amount', width: 110,
      render: (v) => Number(v || 0).toLocaleString(),
    },
    {
      title: t('next_billing', { defaultValue: 'Next Billing' }), dataIndex: 'next_billing_date', key: 'next_billing_date', width: 130,
      render: (v) => (v ? formatDate(v) : '-'),
    },
    { title: t('items', { defaultValue: 'Items' }), dataIndex: 'items_count', key: 'items_count', width: 70 },
    {
      title: t('actions', { defaultValue: 'Actions' }), key: 'actions', width: 110, fixed: 'right',
      render: (_, record) => (
        <Space>
          <Button type="text" icon={<EyeOutlined />} onClick={() => setDrawerId(record.id)} />
          <Button type="text" icon={<EditOutlined />} onClick={() => openEdit(record)} />
        </Space>
      ),
    },
  ]), [t]);

  const itemColumns = [
    { title: t('product', { defaultValue: 'Product' }), dataIndex: 'product_name', key: 'product_name' },
    {
      title: t('quantity', { defaultValue: 'Qty' }), dataIndex: 'quantity', key: 'quantity', width: 170,
      render: (v, item) => (
        <Space>
          <InputNumber
            min={1}
            // eslint-disable-next-line security/detect-object-injection
            value={qtyDrafts[item.id] ?? v}
            onChange={(nv) => setQtyDrafts((d) => ({ ...d, [item.id]: nv }))}
            style={{ width: 80 }}
          />
          <Button
            size="small"
            loading={updateItemMutation.isPending && updateItemMutation.variables?.itemId === item.id}
            onClick={() => updateItemMutation.mutate({
              id: drawerId,
              itemId: item.id,
              // eslint-disable-next-line security/detect-object-injection
              payload: { quantity: qtyDrafts[item.id] ?? v },
            })}
          >
            {t('save', { defaultValue: 'Save' })}
          </Button>
        </Space>
      ),
    },
    {
      title: t('unit_price', { defaultValue: 'Unit price' }), dataIndex: 'unit_price', key: 'unit_price', width: 110,
      render: (v) => Number(v || 0).toLocaleString(),
    },
    {
      title: '', key: 'remove', width: 60,
      render: (_, item) => (
        <Popconfirm title={t('remove_item_confirm', { defaultValue: 'Remove this item?' })}
          onConfirm={() => removeItemMutation.mutate({ id: drawerId, itemId: item.id })}>
          <Button type="text" danger icon={<DeleteOutlined />} />
        </Popconfirm>
      ),
    },
  ];

  return (
    <div>
      <Card>
        <Space style={{ marginBottom: 16, justifyContent: 'space-between', width: '100%' }} wrap>
          <Space wrap>
            <Input.Search allowClear placeholder={t('search_placeholder', { defaultValue: 'Search number / name / customer' })}
              style={{ width: 260 }} onSearch={(v) => { setSearch(v); setPagination(DEFAULT_PAGE); }} />
            <Select allowClear placeholder={t('status', { defaultValue: 'Status' })} style={{ width: 150 }}
              value={status} onChange={(v) => { setStatus(v); setPagination(DEFAULT_PAGE); }}
              options={STATUS_OPTIONS.map((s) => ({ value: s, label: s }))} />
            <Select allowClear placeholder={t('billing_cycle', { defaultValue: 'Billing cycle' })} style={{ width: 150 }}
              value={billingCycle} onChange={(v) => { setBillingCycle(v); setPagination(DEFAULT_PAGE); }}
              options={FREQUENCY_OPTIONS.map((f) => ({ value: f, label: f }))} />
          </Space>
          <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
            {t('create_button', { defaultValue: 'Create Subscription' })}
          </Button>
        </Space>
        <Table rowKey="id" columns={columns} dataSource={rows} loading={listQuery.isLoading} scroll={{ x: 1200 }}
          locale={{ emptyText: <EmptyState description={t('no_subscriptions', { defaultValue: 'No subscriptions found' })} /> }}
          pagination={{ current: pagination.page, pageSize: pagination.per_page, total, showSizeChanger: true }}
          onChange={(p) => setPagination({ page: p.current, per_page: p.pageSize })} />
      </Card>

      <Modal open={modal.open} width={880} footer={null} destroyOnClose
        title={isEdit ? t('edit_title', { defaultValue: 'Edit Subscription' }) : t('create_title', { defaultValue: 'Create Subscription' })}
        onCancel={closeModal}>
        <Form form={form} layout="vertical" onFinish={onFinish}>
          <Divider orientation="left">{t('section_details', { defaultValue: 'Details' })}</Divider>
          {!isEdit && (
            <Form.Item name="user_id" label={t('customer', { defaultValue: 'Customer' })} rules={[{ required: true }]}>
              <Select showSearch filterOption={false} placeholder={t('search_customer', { defaultValue: 'Search customer…' })}
                onSearch={setUserSearch} loading={usersQuery.isLoading}
                onChange={() => form.setFieldsValue({ delivery_address_id: undefined })}
                options={users.map((u) => ({ value: u.id, label: `${u.first_name || ''} ${u.last_name || ''} ${u.phone || ''}`.trim() }))} />
            </Form.Item>
          )}
          <Row gutter={16}>
            <Col span={12}>
              <Form.Item name="name" label={t('name', { defaultValue: 'Name' })} rules={[{ required: true, min: 3 }]}>
                <Input />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="description" label={t('description', { defaultValue: 'Description' })}>
                <Input />
              </Form.Item>
            </Col>
          </Row>

          {!isEdit && (
            <>
              <Divider orientation="left">{t('section_items', { defaultValue: 'Items' })}</Divider>
              <Form.List name="items">
                {(fields, { add, remove }) => (
                  <>
                    {fields.map((field) => (
                      <Row gutter={8} key={field.key} align="middle">
                        <Col span={11}>
                          <Form.Item name={[field.name, 'product_id']} rules={[{ required: true }]}>
                            <Select showSearch optionFilterProp="label" placeholder={t('product', { defaultValue: 'Product' })}
                              options={products.map((p) => ({ value: p.id, label: p.name }))} />
                          </Form.Item>
                        </Col>
                        <Col span={6}>
                          <Form.Item name={[field.name, 'quantity']} rules={[{ required: true }]}>
                            <InputNumber min={1} style={{ width: '100%' }} placeholder={t('quantity', { defaultValue: 'Qty' })} />
                          </Form.Item>
                        </Col>
                        <Col span={5}>
                          <Form.Item name={[field.name, 'special_instructions']}>
                            <Input placeholder={t('instructions', { defaultValue: 'Notes' })} />
                          </Form.Item>
                        </Col>
                        <Col span={2}>
                          <Button type="text" danger icon={<DeleteOutlined />} onClick={() => remove(field.name)} />
                        </Col>
                      </Row>
                    ))}
                    <Button onClick={() => add({ quantity: 1 })} icon={<PlusOutlined />}>
                      {t('add_item', { defaultValue: 'Add item' })}
                    </Button>
                  </>
                )}
              </Form.List>
            </>
          )}

          <Divider orientation="left">{t('section_billing', { defaultValue: 'Billing' })}</Divider>
          <Row gutter={16}>
            <Col span={8}>
              <Form.Item name="billing_cycle" label={t('billing_cycle', { defaultValue: 'Billing cycle' })} rules={[{ required: true }]}>
                <Select options={FREQUENCY_OPTIONS.map((f) => ({ value: f, label: f, disabled: billingDisabled(f) }))} />
              </Form.Item>
            </Col>
            <Col span={8}>
              <Form.Item name="payment_method" label={t('payment_method', { defaultValue: 'Payment method' })} rules={[{ required: true }]}>
                <Select options={PAYMENT_METHODS.map((m) => ({ value: m, label: m }))} />
              </Form.Item>
            </Col>
            <Col span={8}>
              <Form.Item name="discount_percentage" label={t('discount', { defaultValue: 'Discount %' })}>
                <InputNumber min={0} max={100} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
          </Row>
          <Row gutter={16}>
            <Col span={8}>
              <Form.Item name="loyalty_points_multiplier" label={t('loyalty_multiplier', { defaultValue: 'Loyalty multiplier' })}>
                <InputNumber min={0} step={0.1} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col span={8}>
              <Form.Item name="auto_payment" label={t('auto_payment', { defaultValue: 'Auto payment' })} valuePropName="checked">
                <Switch />
              </Form.Item>
            </Col>
            <Col span={8}>
              <Form.Item name="auto_renew" label={t('auto_renew', { defaultValue: 'Auto renew' })} valuePropName="checked">
                <Switch />
              </Form.Item>
            </Col>
          </Row>

          <Divider orientation="left">{t('section_delivery', { defaultValue: 'Delivery schedule' })}</Divider>
          <Row gutter={16}>
            <Col span={8}>
              <Form.Item name="delivery_frequency" label={t('delivery_frequency', { defaultValue: 'Delivery frequency' })} rules={[{ required: true }]}>
                <Select options={FREQUENCY_OPTIONS.map((f) => ({ value: f, label: f }))} />
              </Form.Item>
            </Col>
            <Col span={8}>
              <Form.Item name="delivery_day_of_week" label={t('day_of_week', { defaultValue: 'Day of week (0=Mon)' })}>
                <InputNumber min={0} max={6} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col span={8}>
              <Form.Item name="delivery_day_of_month" label={t('day_of_month', { defaultValue: 'Day of month' })}>
                <InputNumber min={1} max={31} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
          </Row>
          <Row gutter={16}>
            <Col span={8}>
              <Form.Item name="delivery_address_id" label={t('address', { defaultValue: 'Delivery address' })} rules={[{ required: true }]}>
                <Select loading={addressesQuery.isLoading} placeholder={t('select_address', { defaultValue: 'Select address' })}
                  options={addresses.map((a) => ({ value: a.id, label: a.full_address || a.title || `#${a.id}` }))} />
              </Form.Item>
            </Col>
            <Col span={8}>
              <Form.Item name="delivery_time_slot_id" label={t('time_slot', { defaultValue: 'Time slot' })}>
                <Select allowClear options={timeSlots.map((s) => ({ value: s.id, label: s.name || `#${s.id}` }))} />
              </Form.Item>
            </Col>
          </Row>
          <Row gutter={16}>
            <Col span={12}>
              <Form.Item name="start_date" label={t('start_date', { defaultValue: 'Start date' })}>
                <DatePicker showTime style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="end_date" label={t('end_date', { defaultValue: 'End date' })}>
                <DatePicker showTime style={{ width: '100%' }} />
              </Form.Item>
            </Col>
          </Row>

          {isEdit && (
            <>
              <Divider orientation="left">{t('section_overrides', { defaultValue: 'Danger zone / overrides' })}</Divider>
              <Alert type="warning" showIcon style={{ marginBottom: 12 }}
                message={t('override_warning', { defaultValue: 'Manual overrides can break automated billing. Use with care.' })} />
              <Form.Item name="override_edit_any_status" label={t('override_status', { defaultValue: 'Allow editing any status' })} valuePropName="checked">
                <Switch />
              </Form.Item>
              <Row gutter={16}>
                <Col span={8}>
                  <Form.Item name="override_manual_billing_amount" label={t('override_amount', { defaultValue: 'Manual billing amount' })} valuePropName="checked">
                    <Switch />
                  </Form.Item>
                </Col>
                <Col span={8}>
                  <Form.Item name="billing_amount" label={t('billing_amount', { defaultValue: 'Billing amount' })}>
                    <InputNumber min={0} disabled={!manualAmount} style={{ width: '100%' }} />
                  </Form.Item>
                </Col>
              </Row>
              <Form.Item name="override_manual_billing_dates" label={t('override_dates', { defaultValue: 'Manual billing dates' })} valuePropName="checked">
                <Switch />
              </Form.Item>
              <Row gutter={16}>
                <Col span={12}>
                  <Form.Item name="next_billing_date" label={t('next_billing', { defaultValue: 'Next billing date' })}>
                    <DatePicker showTime disabled={!manualDates} style={{ width: '100%' }} />
                  </Form.Item>
                </Col>
                <Col span={12}>
                  <Form.Item name="last_billing_date" label={t('last_billing', { defaultValue: 'Last billing date' })}>
                    <DatePicker showTime disabled={!manualDates} style={{ width: '100%' }} />
                  </Form.Item>
                </Col>
              </Row>
            </>
          )}

          <Form.Item style={{ marginTop: 16, marginBottom: 0, textAlign: 'right' }}>
            <Space>
              <Button onClick={closeModal}>{t('cancel', { defaultValue: 'Cancel' })}</Button>
              <Button type="primary" htmlType="submit" loading={createMutation.isPending || updateMutation.isPending}>
                {isEdit ? t('submit_update', { defaultValue: 'Update' }) : t('submit_create', { defaultValue: 'Create' })}
              </Button>
            </Space>
          </Form.Item>
        </Form>
      </Modal>

      <Drawer open={Boolean(drawerId)} width={780} onClose={() => { setDrawerId(null); setQtyDrafts({}); }}
        title={detail.subscription_number || t('details', { defaultValue: 'Subscription' })}>
        {drawerId && (
          <>
            <Descriptions bordered column={2} size="small">
              <Descriptions.Item label={t('customer', { defaultValue: 'Customer' })} span={2}>
                {detail.user?.name} {detail.user?.phone ? `(${detail.user.phone})` : ''}
              </Descriptions.Item>
              <Descriptions.Item label={t('status', { defaultValue: 'Status' })}>
                <Tag color={STATUS_COLORS[detail.status] || 'default'}>{detail.status}</Tag>
              </Descriptions.Item>
              <Descriptions.Item label={t('billing_cycle', { defaultValue: 'Billing' })}>{detail.billing_cycle}</Descriptions.Item>
              <Descriptions.Item label={t('billing_amount', { defaultValue: 'Amount' })}>{Number(detail.billing_amount || 0).toLocaleString()}</Descriptions.Item>
              <Descriptions.Item label={t('delivery_frequency', { defaultValue: 'Delivery' })}>{detail.delivery_frequency}</Descriptions.Item>
              <Descriptions.Item label={t('next_billing', { defaultValue: 'Next billing' })}>{detail.next_billing_date ? formatDateTime(detail.next_billing_date) : '-'}</Descriptions.Item>
              <Descriptions.Item label={t('total_orders', { defaultValue: 'Orders generated' })}>{detail.total_orders_generated ?? 0}</Descriptions.Item>
            </Descriptions>

            <Divider orientation="left">{t('lifecycle', { defaultValue: 'Actions' })}</Divider>
            <Space wrap>
              <Button icon={<PauseCircleOutlined />} disabled={detail.status !== 'active'}
                onClick={() => lifecycleMutation.mutate({ action: 'pause', id: drawerId })}>
                {t('pause', { defaultValue: 'Pause' })}
              </Button>
              <Button icon={<PlayCircleOutlined />} disabled={detail.status !== 'paused'}
                onClick={() => lifecycleMutation.mutate({ action: 'resume', id: drawerId })}>
                {t('resume', { defaultValue: 'Resume' })}
              </Button>
              <Popconfirm title={t('cancel_confirm', { defaultValue: 'Cancel (delete) this subscription?' })}
                onConfirm={() => lifecycleMutation.mutate({ action: 'cancel', id: drawerId })}>
                <Button danger icon={<CloseCircleOutlined />} disabled={detail.status === 'cancelled'}>
                  {t('cancel_sub', { defaultValue: 'Cancel' })}
                </Button>
              </Popconfirm>
              <Popconfirm title={t('bill_now_confirm', { defaultValue: 'Generate an order and bill now?' })}
                onConfirm={() => lifecycleMutation.mutate({ action: 'bill', id: drawerId })}>
                <Button icon={<DollarOutlined />} disabled={detail.status === 'cancelled'}>{t('bill_now', { defaultValue: 'Process billing now' })}</Button>
              </Popconfirm>
            </Space>

            <Divider orientation="left">{t('section_items', { defaultValue: 'Items' })}</Divider>
            <Table rowKey="id" size="small" pagination={false} columns={itemColumns}
              dataSource={detail.items || []} loading={detailQuery.isLoading} />
            <Space style={{ marginTop: 12 }} wrap>
              <Select showSearch optionFilterProp="label" style={{ width: 240 }}
                placeholder={t('product', { defaultValue: 'Product' })} value={addItemValue.product_id}
                onChange={(v) => setAddItemValue((s) => ({ ...s, product_id: v }))}
                options={products.map((p) => ({ value: p.id, label: p.name }))} />
              <InputNumber min={1} value={addItemValue.quantity}
                onChange={(v) => setAddItemValue((s) => ({ ...s, quantity: v }))} />
              <Button type="primary" icon={<PlusOutlined />} loading={addItemMutation.isPending}
                disabled={!addItemValue.product_id}
                onClick={() => addItemMutation.mutate({ id: drawerId, payload: { product_id: addItemValue.product_id, quantity: addItemValue.quantity } })}>
                {t('add_item', { defaultValue: 'Add item' })}
              </Button>
            </Space>
          </>
        )}
      </Drawer>
    </div>
  );
};

export default Subscriptions;
