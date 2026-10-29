import React, { useEffect, useMemo, useState } from 'react';
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
import adminService from '../services/adminService';
import EmptyState from '../components/common/EmptyState';
import {
  ANY_TIME, buildSubscriptionPayload, mapDetailToForm, usesDayOfMonth, usesWeekday,
} from '../utils/subscriptionPayload';
import { formatUpcomingDelivery } from '../utils/subscriptionDisplay';

const DEFAULT_PAGE = { page: 1, per_page: 20 };
const STATUS_COLORS = { active: 'green', paused: 'orange', cancelled: 'red', expired: 'default', trial: 'blue' };
const STATUS_OPTIONS = ['active', 'paused', 'cancelled', 'expired', 'trial'];
const FREQUENCY_OPTIONS = ['daily', 'weekly', 'biweekly', 'monthly'];
// 0 = Monday: the backend's weekday convention, shown by name so nobody types a number.
const WEEKDAYS = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday'];

// The backend publishes, per action, whether it may be done now and why not (`allowed_actions`). The
// admin never derives that from the status; a missing answer means "not allowed".
// eslint-disable-next-line security/detect-object-injection
const actionOf = (actions, name) => actions?.[name];
const isAllowed = (actions, name) => Boolean(actionOf(actions, name)?.allowed);
const actionReason = (actions, name) => (isAllowed(actions, name) ? undefined : actionOf(actions, name)?.block_reason || undefined);
// "Edit" stays open when only the admin's edit-any-status override can lift the refusal.
const canEdit = (actions) => isAllowed(actions, 'edit') || Boolean(actions?.edit?.override_available);

const Subscriptions = () => {
  const { t } = useTranslation('subscriptions');
  const queryClient = useQueryClient();

  const [search, setSearch] = useState('');
  const [status, setStatus] = useState();
  const [frequencyFilter, setFrequencyFilter] = useState();
  const [pagination, setPagination] = useState(DEFAULT_PAGE);

  const [modal, setModal] = useState({ open: false, subscription: null });
  const [drawerId, setDrawerId] = useState(null);
  const [userSearch, setUserSearch] = useState('');
  const [addItemValue, setAddItemValue] = useState({ product_id: undefined, quantity: 1 });
  const [qtyDrafts, setQtyDrafts] = useState({});

  const [form] = Form.useForm();
  const watchedUserId = Form.useWatch('user_id', form);
  // `preserve`: read from the form's values, not only from mounted fields. The modal's fields mount after openEdit
  // fills the form, so without it the first render of every open sees no address, frequency or day, and a menu
  // or slot list still cached for "no address" would be reconciled against the stored values (re-review
  // admin-bot-backend-2).
  const watchedFrequency = Form.useWatch('frequency', { form, preserve: true });
  const watchedWeekday = Form.useWatch('delivery_day_of_week', { form, preserve: true });
  const watchedAddressId = Form.useWatch('delivery_address_id', { form, preserve: true });
  const watchedItems = Form.useWatch('items', form);
  const manualAmount = Form.useWatch('override_manual_billing_amount', form);
  const manualDates = Form.useWatch('override_manual_billing_dates', form);

  const isEdit = Boolean(modal.subscription);
  const formUserId = isEdit ? modal.subscription.user_id : watchedUserId;
  // The weekday the slot list is asked for: the chosen one for weekly and biweekly, any for daily and monthly.
  // Weekly and biweekly are never asked `any`: until their (required) day is chosen there is no list, because
  // create and update judge the slot on that day (spec 2026-10-26 B9, re-review G1).
  const slotWeekday = usesWeekday(watchedFrequency) ? watchedWeekday : 'any';
  const slotsAskable = Boolean(watchedFrequency) && slotWeekday != null;

  const listQuery = useQuery({
    queryKey: ['admin-subscriptions', pagination, search, status, frequencyFilter],
    queryFn: () => adminService.getSubscriptions({
      page: pagination.page, per_page: pagination.per_page, search, status, frequency: frequencyFilter,
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

  // Only the slots the backend offers this day and frequency (spec 2026-10-26 §8.2: monthly gets the slots
  // offered every day), the rule the create and update guards check.
  const timeSlotsQuery = useQuery({
    queryKey: ['admin-subscription-timeslots', slotWeekday, watchedFrequency],
    queryFn: () => adminService.getSubscriptionTimeSlots({ weekday: slotWeekday, frequency: watchedFrequency }),
    enabled: modal.open && slotsAskable,
  });

  // The delivery days the backend publishes (spec 2026-10-27 §6.4): the weekly and biweekly day select offers these.
  const weekdaysQuery = useQuery({
    queryKey: ['admin-subscription-delivery-weekdays'],
    queryFn: () => adminService.getDeliveryWeekdays(),
    enabled: modal.open,
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

  // The CUSTOMER's subscription menu at the chosen address: exactly what the backend's B8 guard accepts
  // (spec 2026-10-26 §8.1), asked again whenever the customer or the address changes.
  const paymentMethodsQuery = useQuery({
    queryKey: ['admin-subscription-payment-methods', formUserId, watchedAddressId],
    queryFn: () => adminService.getCustomerSubscriptionPaymentMethods(formUserId, watchedAddressId),
    enabled: modal.open && Boolean(formUserId),
  });

  const rows = listQuery.data?.items || [];
  const total = listQuery.data?.total || 0;
  const users = usersQuery.data?.data?.items || usersQuery.data?.items || [];
  const products = productsQuery.data?.data?.items || productsQuery.data?.items || [];
  // A subscription line below the product's minimum order quantity is one billing's
  // create_order refuses every cycle, so every quantity input here is floored by it.
  const minQtyFor = (productId) => Number(products.find((p) => p.id === productId)?.min_order_quantity) || 1;
  const deliveryWeekdays = weekdaysQuery.data;
  const timeSlots = (slotsAskable && timeSlotsQuery.data) || [];
  const addresses = addressesQuery.data?.data?.addresses || addressesQuery.data?.addresses || [];
  const detail = detailQuery.data || {};
  const paymentMethods = paymentMethodsQuery.data || [];

  // The form never holds a value its own list does not offer (re-review G3). When the payment menu or the slot
  // list is asked again because the admin changed what it depends on (customer, address; frequency, day) and
  // the answer no longer has the current value, the value is cleared, and the payment method's required rule
  // says so. A stored value the list does not offer while nothing it depends on changed stays: the update
  // re-checks only a real change (B8, B9), so an unrelated edit still saves.
  const stored = modal.subscription;
  const menuUnchanged = isEdit && watchedAddressId === stored.delivery_address_id;
  const slotsUnchanged = isEdit && watchedFrequency === stored.frequency
    && (!usesWeekday(watchedFrequency) || watchedWeekday === stored.delivery_day_of_week);
  useEffect(() => {
    if (!modal.open || !paymentMethodsQuery.isSuccess) return;
    const offered = paymentMethodsQuery.data || [];
    const current = form.getFieldValue('payment_method');
    if (current && !menuUnchanged && !offered.some((m) => m.method === current)) {
      form.setFieldValue('payment_method', undefined);
      form.validateFields(['payment_method']).catch(() => {});
    }
    // A new subscription starts on the method the menu itself flags as its default, if it flags one.
    const preset = offered.find((m) => m.is_default);
    if (!isEdit && preset && !form.getFieldValue('payment_method')) form.setFieldValue('payment_method', preset.method);
  }, [modal.open, isEdit, menuUnchanged, paymentMethodsQuery.isSuccess, paymentMethodsQuery.data, form]);
  useEffect(() => {
    if (!modal.open || !slotsAskable || !timeSlotsQuery.isSuccess || slotsUnchanged) return;
    const current = form.getFieldValue('delivery_time_slot_id');
    // Only a numeric slot the new list lacks is cleared: Any time is always on offer.
    if (typeof current === 'number' && !(timeSlotsQuery.data || []).some((slot) => slot.id === current)) {
      form.setFieldValue('delivery_time_slot_id', undefined);
      form.validateFields(['delivery_time_slot_id']).catch(() => {});
    }
  }, [modal.open, slotsAskable, slotsUnchanged, timeSlotsQuery.isSuccess, timeSlotsQuery.data, form]);

  const invalidateList = () => queryClient.invalidateQueries({ queryKey: ['admin-subscriptions'] });
  const invalidateDetail = () => queryClient.invalidateQueries({ queryKey: ['admin-subscription-detail'] });

  const createMutation = useMutation({
    mutationFn: (payload) => adminService.createSubscription(payload),
    onSuccess: () => {
      message.success(t('sub_created', { defaultValue: 'Subscription created' }));
      setModal({ open: false, subscription: null });
      form.resetFields();
      invalidateList();
    },
    onError: (e) => message.error(e.response?.data?.message || t('sub_create_failed', { defaultValue: 'Failed to create subscription' })),
  });

  const updateMutation = useMutation({
    mutationFn: ({ id, payload }) => adminService.updateSubscription(id, payload),
    onSuccess: () => {
      message.success(t('sub_updated', { defaultValue: 'Subscription updated' }));
      setModal({ open: false, subscription: null });
      form.resetFields();
      invalidateList();
      invalidateDetail();
    },
    onError: (e) => message.error(e.response?.data?.message || t('sub_update_failed', { defaultValue: 'Failed to update subscription' })),
  });

  const lifecycleMutation = useMutation({
    mutationFn: ({ action, id }) => {
      if (action === 'pause') return adminService.pauseSubscription(id, { pause_reason: 'Paused by administrator' });
      if (action === 'resume') return adminService.resumeSubscription(id);
      if (action === 'cancel') return adminService.cancelSubscription(id, { cancellation_reason: 'Cancelled by administrator' });
      return adminService.processSubscriptionBilling(id);
    },
    onSuccess: (response, { action }) => {
      // "Create next order now" answers 200 even when it created nothing (a refusal, an outage, or the
      // order already exists): say why, never "Done".
      if (action === 'bill' && !response?.data?.created_order_ids?.length) {
        message.warning(response?.message || t('sub_no_order_created', { defaultValue: 'No order was created' }));
      } else {
        message.success(t('action_done', { defaultValue: 'Done' }));
      }
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
    // No payment method until the customer's menu answers (re-review G3): it may not offer cash.
    form.setFieldsValue({
      frequency: 'weekly',
      delivery_time_slot_id: ANY_TIME,
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

  const columns = useMemo(() => ([
    { title: t('number', { defaultValue: 'Number' }), dataIndex: 'subscription_number', key: 'subscription_number' },
    {
      title: t('sub_customer', { defaultValue: 'Customer' }), key: 'customer',
      render: (_, r) => (<div><div>{r.user_name}</div><Typography.Text type="secondary">{r.user_email}</Typography.Text></div>),
    },
    {
      title: t('sub_status', { defaultValue: 'Status' }), dataIndex: 'status', key: 'status', width: 110,
      // eslint-disable-next-line security/detect-object-injection
      render: (v) => <Tag color={STATUS_COLORS[v] || 'default'}>{v}</Tag>,
    },
    { title: t('frequency', { defaultValue: 'Frequency' }), dataIndex: 'frequency', key: 'frequency', width: 100 },
    {
      title: t('billing_amount', { defaultValue: 'Amount' }), dataIndex: 'billing_amount', key: 'billing_amount', width: 110,
      render: (v) => Number(v || 0).toLocaleString(),
    },
    {
      title: t('next_delivery', { defaultValue: 'Next delivery' }), key: 'upcoming_delivery', width: 210,
      render: (_, r) => formatUpcomingDelivery(r),
    },
    { title: t('sub_items', { defaultValue: 'Items' }), dataIndex: 'items_count', key: 'items_count', width: 70 },
    {
      title: t('sub_actions', { defaultValue: 'Actions' }), key: 'actions', width: 110, fixed: 'right',
      render: (_, record) => (
        <Space>
          <Button type="text" icon={<EyeOutlined />} onClick={() => setDrawerId(record.id)} />
          <Button type="text" icon={<EditOutlined />} onClick={() => openEdit(record)}
            disabled={!canEdit(record.allowed_actions)} title={actionReason(record.allowed_actions, 'edit')} />
        </Space>
      ),
    },
  ]), [t]);

  // The drawer's item editors follow the published `edit` answer. The item routes take no edit-any-status
  // override (that switch is the Edit form's), so `override_available` does not open them.
  const canEditItems = isAllowed(detail.allowed_actions, 'edit');
  const editItemsReason = actionReason(detail.allowed_actions, 'edit');

  const itemColumns = [
    { title: t('sub_product', { defaultValue: 'Product' }), dataIndex: 'product_name', key: 'product_name' },
    {
      title: t('sub_quantity', { defaultValue: 'Qty' }), dataIndex: 'quantity', key: 'quantity', width: 170,
      render: (v, item) => (
        <Space>
          <InputNumber
            min={minQtyFor(item.product_id)}
            // eslint-disable-next-line security/detect-object-injection
            value={qtyDrafts[item.id] ?? v}
            onChange={(nv) => setQtyDrafts((d) => ({ ...d, [item.id]: nv }))}
            disabled={!canEditItems}
            style={{ width: 80 }}
          />
          <Button
            size="small"
            disabled={!canEditItems}
            title={editItemsReason}
            loading={updateItemMutation.isPending && updateItemMutation.variables?.itemId === item.id}
            onClick={() => updateItemMutation.mutate({
              id: drawerId,
              itemId: item.id,
              // eslint-disable-next-line security/detect-object-injection
              payload: { quantity: qtyDrafts[item.id] ?? v },
            })}
          >
            {t('sub_save', { defaultValue: 'Save' })}
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
          <Button type="text" danger icon={<DeleteOutlined />}
            disabled={!isAllowed(detail.allowed_actions, 'remove_item')} title={actionReason(detail.allowed_actions, 'remove_item')} />
        </Popconfirm>
      ),
    },
  ];

  return (
    <div>
      <Card>
        <Space style={{ marginBottom: 16, justifyContent: 'space-between', width: '100%' }} wrap>
          <Space wrap>
            <Input.Search allowClear placeholder={t('sub_search_placeholder', { defaultValue: 'Search number / name / customer' })}
              style={{ width: 260 }} onSearch={(v) => { setSearch(v); setPagination(DEFAULT_PAGE); }} />
            <Select allowClear placeholder={t('sub_status', { defaultValue: 'Status' })} style={{ width: 150 }}
              value={status} onChange={(v) => { setStatus(v); setPagination(DEFAULT_PAGE); }}
              options={STATUS_OPTIONS.map((s) => ({ value: s, label: s }))} />
            <Select allowClear placeholder={t('frequency', { defaultValue: 'Frequency' })} style={{ width: 150 }}
              value={frequencyFilter} onChange={(v) => { setFrequencyFilter(v); setPagination(DEFAULT_PAGE); }}
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
            <Form.Item name="user_id" label={t('sub_customer', { defaultValue: 'Customer' })} rules={[{ required: true }]}>
              <Select showSearch filterOption={false} placeholder={t('search_customer', { defaultValue: 'Search customer…' })}
                onSearch={setUserSearch} loading={usersQuery.isLoading}
                onChange={() => form.setFieldsValue({ delivery_address_id: undefined })}
                options={users.map((u) => ({ value: u.id, label: `${u.first_name || ''} ${u.last_name || ''} ${u.phone || ''}`.trim() }))} />
            </Form.Item>
          )}
          <Row gutter={16}>
            <Col span={12}>
              <Form.Item name="name" label={t('sub_name', { defaultValue: 'Name' })} rules={[{ required: true, min: 3 }]}>
                <Input />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="description" label={t('sub_description', { defaultValue: 'Description' })}>
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
                            <Select showSearch optionFilterProp="label" placeholder={t('sub_product', { defaultValue: 'Product' })}
                              options={products.map((p) => ({ value: p.id, label: p.name }))}
                              onChange={(productId) => {
                                const path = ['items', field.name, 'quantity'];
                                const minimum = minQtyFor(productId);
                                if ((form.getFieldValue(path) || 0) < minimum) form.setFieldValue(path, minimum);
                              }} />
                          </Form.Item>
                        </Col>
                        <Col span={6}>
                          <Form.Item name={[field.name, 'quantity']} rules={[{ required: true }]}>
                            <InputNumber
                              min={minQtyFor(watchedItems?.[field.name]?.product_id)}
                              style={{ width: '100%' }} placeholder={t('sub_quantity', { defaultValue: 'Qty' })} />
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
              <Form.Item name="payment_method" label={t('payment_method', { defaultValue: 'Payment method' })} rules={[{ required: true }]}>
                <Select
                  loading={paymentMethodsQuery.isLoading}
                  options={paymentMethods.map((m) => ({ value: m.method, label: m.display_name || m.method }))}
                />
              </Form.Item>
            </Col>
            <Col span={8}>
              <Form.Item name="discount_percentage" label={t('discount', { defaultValue: 'Discount %' })}>
                <InputNumber min={0} max={100} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
          </Row>
          <Row gutter={16}>
            <Col span={12}>
              <Form.Item name="loyalty_points_multiplier" label={t('loyalty_multiplier', { defaultValue: 'Loyalty multiplier' })}>
                <InputNumber min={0} step={0.1} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="auto_renew" label={t('auto_renew', { defaultValue: 'Auto renew' })} valuePropName="checked">
                <Switch />
              </Form.Item>
            </Col>
          </Row>

          <Divider orientation="left">{t('section_delivery', { defaultValue: 'Delivery schedule' })}</Divider>
          <Row gutter={16}>
            <Col span={8}>
              <Form.Item name="frequency" label={t('frequency', { defaultValue: 'Frequency' })} rules={[{ required: true }]}>
                <Select options={FREQUENCY_OPTIONS.map((f) => ({ value: f, label: f }))} />
              </Form.Item>
            </Col>
            {usesWeekday(watchedFrequency) && (
              <Col span={8}>
                <Form.Item name="delivery_day_of_week" label={t('day_of_week', { defaultValue: 'Delivery day' })}
                  rules={[{ required: true }]}>
                  <Select options={WEEKDAYS.map((name, index) => ({
                    value: index, label: t(`weekday_${index}`, { defaultValue: name.charAt(0).toUpperCase() + name.slice(1) }),
                  })).filter((option) => !deliveryWeekdays || deliveryWeekdays.includes(option.value)
                    // a stored day that is no longer a delivery day stays, so the Select shows its name, not the raw number
                    || option.value === modal.subscription?.delivery_day_of_week)} />
                </Form.Item>
              </Col>
            )}
            {usesDayOfMonth(watchedFrequency) && (
              <Col span={8}>
                <Form.Item name="delivery_day_of_month" label={t('day_of_month', { defaultValue: 'Day of month' })}
                  rules={[{ required: true }]}>
                  <InputNumber min={1} max={31} style={{ width: '100%' }} />
                </Form.Item>
              </Col>
            )}
          </Row>
          <Row gutter={16}>
            <Col span={8}>
              <Form.Item name="delivery_address_id" label={t('sub_address', { defaultValue: 'Delivery address' })} rules={[{ required: true }]}>
                <Select loading={addressesQuery.isLoading} placeholder={t('select_address', { defaultValue: 'Select address' })}
                  options={addresses.map((a) => ({ value: a.id, label: a.full_address || a.title || `#${a.id}` }))} />
              </Form.Item>
            </Col>
            <Col span={8}>
              <Form.Item name="delivery_time_slot_id" label={t('time_slot', { defaultValue: 'Time slot' })}
                rules={[{ required: true, message: t('sub_slot_or_any_time', { defaultValue: 'Choose a time slot or Any time' }) }]}>
                <Select disabled={!slotsAskable} loading={slotsAskable && timeSlotsQuery.isLoading}
                  options={[
                    { value: ANY_TIME, label: t('sub_any_time', { defaultValue: 'Any time' }) },
                    ...timeSlots.map((s) => ({ value: s.id, label: s.name || `#${s.id}` })),
                  ]} />
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
              <Form.Item name="override_manual_billing_dates" label={t('override_dates', { defaultValue: 'Set next delivery date' })} valuePropName="checked">
                <Switch />
              </Form.Item>
              <Row gutter={16}>
                <Col span={12}>
                  <Form.Item name="next_delivery_date" label={t('next_delivery_date', { defaultValue: 'Next delivery date' })}>
                    <DatePicker disabled={!manualDates} style={{ width: '100%' }} />
                  </Form.Item>
                </Col>
              </Row>
            </>
          )}

          <Form.Item style={{ marginTop: 16, marginBottom: 0, textAlign: 'right' }}>
            <Space>
              <Button onClick={closeModal}>{t('sub_cancel', { defaultValue: 'Cancel' })}</Button>
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
              <Descriptions.Item label={t('sub_customer', { defaultValue: 'Customer' })} span={2}>
                {detail.user?.name} {detail.user?.phone ? `(${detail.user.phone})` : ''}
              </Descriptions.Item>
              <Descriptions.Item label={t('sub_status', { defaultValue: 'Status' })}>
                <Tag color={STATUS_COLORS[detail.status] || 'default'}>{detail.status}</Tag>
              </Descriptions.Item>
              <Descriptions.Item label={t('frequency', { defaultValue: 'Frequency' })}>{detail.frequency}</Descriptions.Item>
              <Descriptions.Item label={t('billing_amount', { defaultValue: 'Amount' })}>{Number(detail.billing_amount || 0).toLocaleString()}</Descriptions.Item>
              <Descriptions.Item label={t('payment_method', { defaultValue: 'Payment method' })}>{detail.payment_method || '—'}</Descriptions.Item>
              <Descriptions.Item label={t('next_delivery', { defaultValue: 'Next delivery' })}>
                {formatUpcomingDelivery(detail)}
              </Descriptions.Item>
              <Descriptions.Item label={t('total_orders', { defaultValue: 'Orders generated' })}>{detail.total_orders_generated ?? 0}</Descriptions.Item>
            </Descriptions>

            <Divider orientation="left">{t('lifecycle', { defaultValue: 'Actions' })}</Divider>
            <Space wrap>
              <Button icon={<PauseCircleOutlined />} disabled={!isAllowed(detail.allowed_actions, 'pause')}
                title={actionReason(detail.allowed_actions, 'pause')}
                onClick={() => lifecycleMutation.mutate({ action: 'pause', id: drawerId })}>
                {t('pause', { defaultValue: 'Pause' })}
              </Button>
              <Button icon={<PlayCircleOutlined />} disabled={!isAllowed(detail.allowed_actions, 'resume')}
                title={actionReason(detail.allowed_actions, 'resume')}
                onClick={() => lifecycleMutation.mutate({ action: 'resume', id: drawerId })}>
                {t('resume', { defaultValue: 'Resume' })}
              </Button>
              <Popconfirm title={t('cancel_confirm', { defaultValue: 'Cancel (delete) this subscription?' })}
                onConfirm={() => lifecycleMutation.mutate({ action: 'cancel', id: drawerId })}>
                <Button danger icon={<CloseCircleOutlined />} disabled={!isAllowed(detail.allowed_actions, 'cancel')}
                  title={actionReason(detail.allowed_actions, 'cancel')}>
                  {t('cancel_sub', { defaultValue: 'Cancel' })}
                </Button>
              </Popconfirm>
              <Popconfirm title={t('bill_now_confirm', { defaultValue: 'Create the next order now?' })}
                onConfirm={() => lifecycleMutation.mutate({ action: 'bill', id: drawerId })}>
                <Button icon={<DollarOutlined />} disabled={!isAllowed(detail.allowed_actions, 'create_next_order')}
                  title={actionReason(detail.allowed_actions, 'create_next_order')}>{t('bill_now', { defaultValue: 'Create next order now' })}</Button>
              </Popconfirm>
            </Space>

            <Divider orientation="left">{t('section_items', { defaultValue: 'Items' })}</Divider>
            <Table rowKey="id" size="small" pagination={false} columns={itemColumns}
              dataSource={detail.items || []} loading={detailQuery.isLoading} />
            <Space style={{ marginTop: 12 }} wrap>
              <Select showSearch optionFilterProp="label" style={{ width: 240 }} disabled={!canEditItems}
                placeholder={t('sub_product', { defaultValue: 'Product' })} value={addItemValue.product_id}
                onChange={(v) => setAddItemValue((s) => ({ product_id: v, quantity: Math.max(s.quantity || 0, minQtyFor(v)) }))}
                options={products.map((p) => ({ value: p.id, label: p.name }))} />
              <InputNumber min={minQtyFor(addItemValue.product_id)} value={addItemValue.quantity} disabled={!canEditItems}
                onChange={(v) => setAddItemValue((s) => ({ ...s, quantity: v }))} />
              <Button type="primary" icon={<PlusOutlined />} loading={addItemMutation.isPending}
                disabled={!canEditItems || !addItemValue.product_id} title={editItemsReason}
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
