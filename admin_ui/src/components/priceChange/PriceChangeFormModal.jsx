import React, { useState } from 'react';
import { Alert, Button, DatePicker, Form, InputNumber, Modal, Select, Space, Tooltip, message } from 'antd';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import dayjs from 'dayjs';
import { useTranslation } from 'react-i18next';

import adminService from '../../services/adminService';
import MoneyInput from '../common/MoneyInput';
import { extractApiErrorMessages } from '../../utils/apiError';
import { PRICE_CHANGES_KEY } from '../../utils/constants';
import { formatMoneyUZS } from '../../utils/formatMoney';

const DATE_FORMAT = 'YYYY-MM-DD';

// Presentational starting values only: the backend validates whatever it receives, so these are not a second copy of
// its defaults.
const INITIAL_REMINDER_LEAD_DAYS = 7;
const INITIAL_COURTESY_DAYS = 14;

// The form's values for an existing change (edit) or an empty one (create).
const toFormValues = (priceChange) => (priceChange ? {
  announce_on: dayjs(priceChange.announce_on, DATE_FORMAT),
  change_on: dayjs(priceChange.change_on, DATE_FORMAT),
  reminder_lead_days: priceChange.reminder_lead_days,
  courtesy_days: priceChange.courtesy_days,
  items: priceChange.items.map((item) => ({
    product_id: item.product_id, new_price: item.new_price, courtesy_cap: item.courtesy_cap,
  })),
} : {
  reminder_lead_days: INITIAL_REMINDER_LEAD_DAYS, courtesy_days: INITIAL_COURTESY_DAYS, items: [{}],
});

// The one body create and edit send.
const toPayload = (values) => ({
  announce_on: values.announce_on.format(DATE_FORMAT),
  change_on: values.change_on.format(DATE_FORMAT),
  reminder_lead_days: values.reminder_lead_days,
  courtesy_days: values.courtesy_days,
  items: values.items.map((item) => ({
    product_id: item.product_id, new_price: item.new_price, courtesy_cap: item.courtesy_cap,
  })),
});

const useInvalidatePriceChanges = () => {
  const queryClient = useQueryClient();
  return () => Promise.all([
    queryClient.invalidateQueries({ queryKey: PRICE_CHANGES_KEY }),
    queryClient.invalidateQueries({ queryKey: ['products'] }),
  ]);
};

const PriceChangeFormModal = ({ open, priceChange, onClose, onSaved }) => {
  const { t } = useTranslation();
  const [form] = Form.useForm();
  const [errors, setErrors] = useState([]);
  const invalidate = useInvalidatePriceChanges();
  const { data: productsData } = useQuery({
    queryKey: ['products', 'price-change-form'],
    queryFn: () => adminService.getProducts({ per_page: 100, is_active: true }),
    enabled: open,
  });
  const products = productsData?.data?.items || [];
  const options = [
    ...products.map((product) => ({
      value: product.id,
      label: `${product.name} — ${formatMoneyUZS(product.catalogue_unit_price)}`,
    })),
    // a change that holds a product the active list no longer carries still shows its name
    ...(priceChange?.items || [])
      .filter((item) => !products.some((product) => product.id === item.product_id))
      .map((item) => ({ value: item.product_id, label: item.product_name })),
  ];
  const required = { required: true, message: t('ui.price_changes.form.required', 'Required') };

  const mutation = useMutation({
    mutationFn: (payload) => (priceChange
      ? adminService.updatePriceChange(priceChange.id, payload)
      : adminService.createPriceChange(payload)),
    onSuccess: async (saved) => {
      await invalidate();
      setErrors([]);
      onSaved?.(saved);
      onClose();
    },
    onError: (error) => setErrors(
      extractApiErrorMessages(error, t('ui.price_changes.form.save_failed', 'Could not save the price change')),
    ),
  });

  const prefillCap = (name) => (productId) => {
    const product = products.find((candidate) => candidate.id === productId);
    if (product) form.setFieldValue(['items', name, 'courtesy_cap'], product.min_order_quantity);
  };

  return (
    <Modal
      open={open}
      title={priceChange
        ? t('ui.price_changes.form.title_edit', 'Edit price change')
        : t('ui.price_changes.new', 'New price change')}
      okText={t('ui.price_changes.form.save', 'Save')}
      confirmLoading={mutation.isPending}
      onOk={() => form.submit()}
      onCancel={onClose}
      destroyOnHidden
      width={720}
    >
      <Form
        form={form}
        layout="vertical"
        preserve={false}
        initialValues={toFormValues(priceChange)}
        onFinish={(values) => { setErrors([]); mutation.mutate(toPayload(values)); }}
      >
        {errors.length > 0 && (
          <Alert
            type="error"
            showIcon
            style={{ marginBottom: 16 }}
            message={(
              <ul style={{ margin: 0, paddingLeft: 18 }}>
                {errors.map((errorText) => <li key={errorText}>{errorText}</li>)}
              </ul>
            )}
          />
        )}
        <Space align="start" wrap>
          <Form.Item name="announce_on" label={t('ui.price_changes.form.announce_on', 'Announcement date')} rules={[required]}>
            <DatePicker format={DATE_FORMAT} />
          </Form.Item>
          <Form.Item name="change_on" label={t('ui.price_changes.form.change_on', 'New prices from')} rules={[required]}>
            <DatePicker format={DATE_FORMAT} />
          </Form.Item>
          <Form.Item name="reminder_lead_days" label={t('ui.price_changes.form.reminder_lead_days', 'Reminder days before the change')} rules={[required]}>
            <InputNumber />
          </Form.Item>
          <Form.Item name="courtesy_days" label={t('ui.price_changes.form.courtesy_days', 'Old-price days after the change')} rules={[required]}>
            <InputNumber />
          </Form.Item>
        </Space>
        <Form.List name="items">
          {(fields, { add, remove }) => (
            <>
              {fields.map(({ key, name }) => (
                <Space key={key} align="start" wrap>
                  <Form.Item name={[name, 'product_id']} label={t('ui.price_changes.form.product', 'Product')} rules={[required]}>
                    <Select
                      style={{ minWidth: 260 }}
                      options={options}
                      onChange={prefillCap(name)}
                    />
                  </Form.Item>
                  <Form.Item name={[name, 'new_price']} label={t('ui.price_changes.form.new_price', 'New price')} rules={[required]}>
                    <MoneyInput />
                  </Form.Item>
                  <Form.Item name={[name, 'courtesy_cap']} label={t('ui.price_changes.form.courtesy_cap', 'Old-price units per order')} rules={[required]}>
                    <InputNumber />
                  </Form.Item>
                  {fields.length > 1 && (
                    <Button onClick={() => remove(name)}>{t('ui.price_changes.form.remove_product', 'Remove')}</Button>
                  )}
                </Space>
              ))}
              <Button onClick={() => add()}>{t('ui.price_changes.form.add_product', 'Add product')}</Button>
            </>
          )}
        </Form.List>
      </Form>
    </Modal>
  );
};

// A button that follows a published `{allowed, block_reason}`: allowed -> enabled; refused with a reason -> disabled,
// the reason as its tooltip; refused without one -> not drawn. The page never re-derives a role or a status.
export const ActionButton = ({ action, children, ...buttonProps }) => {
  if (!action || (!action.allowed && !action.block_reason)) return null;
  return (
    <Tooltip title={action.allowed ? undefined : action.block_reason}>
      {/* a disabled button swallows hover: the span carries it */}
      <span style={{ display: 'inline-block' }}>
        <Button {...buttonProps} disabled={!action.allowed}>{children}</Button>
      </span>
    </Tooltip>
  );
};

export const NewPriceChangeButton = ({ action }) => {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  return (
    <>
      <ActionButton action={action} type="primary" onClick={() => setOpen(true)}>
        {t('ui.price_changes.new', 'New price change')}
      </ActionButton>
      {open && <PriceChangeFormModal open priceChange={null} onClose={() => setOpen(false)} />}
    </>
  );
};

// Edit and Cancel change for one change, on its list row and in its drawer.
export const PriceChangeActions = ({ priceChange, size = 'small' }) => {
  const { t } = useTranslation();
  const [editing, setEditing] = useState(false);
  const invalidate = useInvalidatePriceChanges();
  const cancelMutation = useMutation({
    mutationFn: () => adminService.cancelPriceChange(priceChange.id),
    onSuccess: invalidate,
    onError: (error) => message.error(
      extractApiErrorMessages(error, t('ui.price_changes.cancel_failed', 'Could not cancel the price change'))[0],
    ),
  });
  const askToCancel = () => Modal.confirm({
    title: t('ui.price_changes.cancel_title', 'Cancel this price change?'),
    content: priceChange.announcement_started_at
      ? t('ui.price_changes.cancel_told', 'Customers were already told about this change. Cancelling it sends them nothing, and prices stay as they are.')
      : t('ui.price_changes.cancel_not_told', 'Prices stay as they are. No customer has been told yet.'),
    okText: t('ui.price_changes.cancel_change', 'Cancel change'),
    cancelText: t('ui.price_changes.cancel_keep', 'Keep it'),
    okButtonProps: { danger: true },
    onOk: () => cancelMutation.mutateAsync().catch(() => undefined),
  });
  const actions = priceChange.allowed_actions;
  return (
    <Space size="small">
      <ActionButton action={actions.edit} size={size} onClick={() => setEditing(true)}>
        {t('ui.price_changes.edit', 'Edit')}
      </ActionButton>
      <ActionButton action={actions.cancel} size={size} danger onClick={askToCancel}>
        {t('ui.price_changes.cancel_change', 'Cancel change')}
      </ActionButton>
      {editing && (
        <PriceChangeFormModal open priceChange={priceChange} onClose={() => setEditing(false)} />
      )}
    </Space>
  );
};

export default PriceChangeFormModal;
