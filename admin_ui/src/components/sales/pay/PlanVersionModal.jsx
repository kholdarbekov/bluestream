import React, { useMemo, useState } from 'react';
import {
  Alert, Button, Col, DatePicker, Divider, Form, Input, InputNumber, Modal, Row, Select, Space, Spin, Typography,
} from 'antd';
import { MinusCircleOutlined, PlusOutlined } from '@ant-design/icons';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import dayjs from 'dayjs';
import MoneyInput, { formatMoneyInput, parseMoneyInput } from '../../common/MoneyInput';
import adminService from '../../../services/adminService';
import salesPayService from '../../../services/salesPayService';
import { useAuthStore } from '../../../stores/authStore';
import { fetchAllPages } from '../../../utils/pagination';
import { BULK_LOAD_PAGE_SIZE } from '../../../utils/constants';
import { PayErrorAlert, isHandledPayError } from './payErrors';
import { monthLabel, monthRange, tierRange } from './payFormat';

const { Paragraph, Text } = Typography;

// A percent as ru/uz admins type it (Review Focus 4): "2,5" is two and a half; antd's default
// parser drops the comma and reads 25. A percent is at most 100, so a comma here is always the
// decimal mark. Spaces are dropped.
const parsePercentInput = (text) => String(text ?? '').replace(/[\s\u00a0\u202f]/g, '').replace(',', '.');

// A new schedule: one tier from unit 1 in the first published mode; the admin types the rate.
const firstTier = (rateModes) => ({ from_unit: 1, mode: rateModes[0], value: undefined });

// A14's tiers as the editor holds them. The range A14 published rides along with its row,
// read-only; the editor never derives an upper bound itself (§6.2).
const tierValues = (tiers) => tiers.map(({ from_unit: fromUnit, to_unit: toUnit, mode, value }) => ({
  from_unit: fromUnit, mode, value, published_range: tierRange(fromUnit, toUnit),
}));

/**
 * The editor's starting point (§6.2 Plans tab). A new plan starts from A12's `default_config`
 * (tiers, the bonus amount and the combined-total threshold have no default: an admin always
 * types them, §4.10); a new version starts from the version in force (A14). Either way the
 * effective month starts at the later of the published `current_month` and `editable_from_month`
 * ("YYYY-MM" strings order as months do): an earlier month still open stays pickable, but a
 * version saved without a look at the picker starts this month, not in one a later version may
 * already replace.
 */
const formValues = (config, source) => {
  const defaults = config.default_config;
  const base = source || {
    gate_bands: defaults.gate_bands,
    gate_min_visits_due: defaults.gate_min_visits_due,
    new_outlet_bonus: {
      amount: undefined,
      window_days: defaults.bonus_window_days,
      prior_customer_lookback_days: defaults.bonus_prior_customer_lookback_days,
      min_orders_with_total: defaults.bonus_min_orders_with_total,
      min_combined_total: undefined,
      min_orders_any_amount: defaults.bonus_min_orders_any_amount,
    },
  };
  const startMonth = config.current_month > config.editable_from_month ? config.current_month : config.editable_from_month;
  return {
    effective_month: dayjs(`${startMonth}-01`),
    default_tiers: source ? tierValues(source.default_tiers) : [firstTier(config.rate_modes)],
    rates: source
      ? source.rates.map(({ product_id: productId, tiers }) => ({ product_id: productId, tiers: tierValues(tiers) }))
      : [],
    gate_bands: base.gate_bands.map(({ min_pct: minPct, multiplier }) => ({ min_pct: minPct, multiplier })),
    gate_min_visits_due: base.gate_min_visits_due,
    new_outlet_bonus: { ...base.new_outlet_bonus },
    note: undefined,
  };
};

// `TierPayload`s: lower bounds only. `to_unit` and the read-only range never leave the browser;
// the backend derives the upper bounds and refuses a sent one (§5.2).
const tierPayload = (tiers) => (tiers || []).map(({ from_unit: fromUnit, mode, value }) => ({ from_unit: fromUnit, mode, value }));

// Exactly `VersionPayload` (§5.2): nothing the form holds for display leaves the browser.
export const versionPayload = (values) => ({
  effective_month: values.effective_month.format('YYYY-MM'),
  default_tiers: tierPayload(values.default_tiers),
  rates: (values.rates || []).map(({ product_id: productId, tiers }) => ({ product_id: productId, tiers: tierPayload(tiers) })),
  gate_bands: values.gate_bands.map(({ min_pct: minPct, multiplier }) => ({ min_pct: minPct, multiplier })),
  gate_min_visits_due: values.gate_min_visits_due,
  new_outlet_bonus: {
    amount: values.new_outlet_bonus.amount,
    window_days: values.new_outlet_bonus.window_days,
    prior_customer_lookback_days: values.new_outlet_bonus.prior_customer_lookback_days,
    min_orders_with_total: values.new_outlet_bonus.min_orders_with_total,
    min_combined_total: values.new_outlet_bonus.min_combined_total,
    min_orders_any_amount: values.new_outlet_bonus.min_orders_any_amount,
  },
  note: values.note || null,
});

// The months a version picked for `month` keeps before the version from `nextMonth` takes over:
// the picked month alone, or the span up to the month before.
const shadowedMonths = (t, month, nextMonth) => {
  const last = dayjs(`${nextMonth}-01`).subtract(1, 'month').format('YYYY-MM');
  return last === month ? monthLabel(month) : monthRange(t, month, last);
};

/**
 * One schedule's tiers (§6.2): From unit · mode · value per row, a per-unit value as whole UZS
 * and a percent with two decimals. `name` is the list's name inside its parent list and `path`
 * its absolute path, which the per-row mode lookup needs. "Add tier" copies the previous row's
 * mode; the first row (unit 1) stays. Limits (tier count, the largest unit, a percent's range)
 * are the backend's alone and come back as an inline refusal.
 *
 * V5-T5-R2: a published range describes THIS schedule's unit bounds, so it is shown only while
 * the rows still hold exactly the `from_unit`s A14 published for them, in the same order — a
 * pure comparison against the list captured once at mount, never a bound computed in the
 * browser. Any edit to a `from_unit`, or an added or removed row, hides every range of this
 * schedule until the rows match the pre-filled set again.
 */
const TierList = ({ name, path, modeOptions, required }) => {
  const { t } = useTranslation(['sales_agents', 'common']);
  const form = Form.useFormInstance();
  const fromUnit = t('sales_agents:pay.form.from_unit', 'From unit');
  const rateMode = t('sales_agents:pay.form.rate_mode', 'Mode');
  const rateValue = t('sales_agents:pay.form.rate_value', 'Value');
  const modeAt = (fieldName) => form.getFieldValue([...path, fieldName, 'mode']);
  const [prefilledFromUnits] = useState(() => (form.getFieldValue(path) || []).map((tier) => tier.from_unit));
  return (
    <Form.List name={name}>
      {(fields, { add, remove }) => (
        <Form.Item noStyle shouldUpdate>
          {() => {
            const currentFromUnits = fields.map((field) => form.getFieldValue([...path, field.name, 'from_unit']));
            const rowsMatchPrefilled = currentFromUnits.length === prefilledFromUnits.length
              && currentFromUnits.every((value, index) => value === prefilledFromUnits.at(index));
            return (
              <>
                {fields.map((field, index) => {
                  const publishedRange = rowsMatchPrefilled ? form.getFieldValue([...path, field.name, 'published_range']) : null;
                  return (
                    <Space key={field.key} align="baseline" wrap data-testid="tier-row">
                      <Form.Item name={[field.name, 'from_unit']} rules={required}>
                        <InputNumber
                          aria-label={fromUnit}
                          placeholder={fromUnit}
                          precision={0}
                          min={1}
                          formatter={formatMoneyInput}
                          parser={parseMoneyInput}
                          style={{ width: 130 }}
                        />
                      </Form.Item>
                      <Form.Item name={[field.name, 'mode']} rules={required}>
                        <Select aria-label={rateMode} style={{ width: 180 }} options={modeOptions} />
                      </Form.Item>
                      <Form.Item noStyle dependencies={[[...path, field.name, 'mode']]}>
                        {() => (
                          <Form.Item name={[field.name, 'value']} rules={required}>
                            {modeAt(field.name) === 'percent'
                              ? <InputNumber aria-label={rateValue} placeholder={rateValue} precision={2} parser={parsePercentInput} style={{ width: 160 }} />
                              : <MoneyInput aria-label={rateValue} placeholder={rateValue} style={{ width: 160 }} />}
                          </Form.Item>
                        )}
                      </Form.Item>
                      {publishedRange ? <Text type="secondary">{publishedRange}</Text> : null}
                      {index > 0 ? (
                        <MinusCircleOutlined aria-label={t('sales_agents:pay.form.remove', 'Remove')} onClick={() => remove(field.name)} />
                      ) : null}
                    </Space>
                  );
                })}
                <Button type="dashed" size="small" icon={<PlusOutlined />} onClick={() => add({ mode: modeAt(fields.slice(-1)[0].name) })}>
                  {t('sales_agents:pay.form.add_tier', 'Add tier')}
                </Button>
              </>
            );
          }}
        </Form.Item>
      )}
    </Form.List>
  );
};

/** A13 (`mode="plan"`, with a name) and A15 (`mode="version"`). */
const PlanVersionModal = ({ mode, plan, config, onClose }) => {
  const { t } = useTranslation(['sales_agents', 'common']);
  const queryClient = useQueryClient();
  const canManagePay = useAuthStore().hasPermission('can_manage_sales_pay');
  const [form] = Form.useForm();
  const [refusal, setRefusal] = useState(null);
  const versionId = plan?.version_in_force?.id;
  const required = [{ required: true, message: t('sales_agents:pay.form.required', 'Required') }];
  const productLabel = t('sales_agents:pay.form.product', 'Product');

  const versionQuery = useQuery({
    queryKey: ['salesPay', 'planVersion', plan?.id, versionId],
    queryFn: () => salesPayService.getPlanVersion(plan.id, versionId),
    enabled: canManagePay && mode === 'version' && Boolean(versionId),
  });
  // Every product, inactive ones included: a version keeps an inactive product's tiers (T-PLAN-3).
  const productsQuery = useQuery({
    queryKey: ['salesPay', 'products'],
    queryFn: () => fetchAllPages(
      (page) => adminService.getProducts({ page, per_page: BULK_LOAD_PAGE_SIZE }),
      (resp) => resp?.data?.items || [],
      BULK_LOAD_PAGE_SIZE,
    ),
    enabled: canManagePay,
    staleTime: 60_000,
  });
  const source = mode === 'version' ? versionQuery.data?.version : null;
  const ready = mode === 'plan' || Boolean(source);
  // The version that takes over after the picked month: the first entry of A12's `timeline` later
  // than it. Which months a version applies to is A12's answer; this only looks the next one up.
  const picked = Form.useWatch('effective_month', form)?.format('YYYY-MM');
  const next = mode === 'version' && picked ? plan.timeline.find((entry) => entry.from_month > picked) : null;

  const inactive = t('sales_agents:pay.form.inactive_product', '(inactive)');
  const productOptions = useMemo(() => {
    const byId = new Map((productsQuery.data || []).map((product) => [product.id, { name: product.name, active: product.is_active !== false }]));
    (source?.rates || []).forEach((rate) => {
      if (!byId.has(rate.product_id)) byId.set(rate.product_id, { name: rate.product_name, active: rate.product_is_active });
    });
    return [...byId].map(([id, product]) => ({ value: id, label: product.active ? product.name : `${product.name} ${inactive}` }));
  }, [productsQuery.data, source, inactive]);
  // The names a tier refusal's `details.product_id` is shown by (`pay.error.tier_position`).
  const productNames = useMemo(() => new Map(productOptions.map((option) => [option.value, option.label])), [productOptions]);
  const modeOptions = config.rate_modes.map((value) => ({ value, label: t(`sales_agents:pay.rate_mode.${value}`, value) }));

  const mutation = useMutation({
    mutationFn: (values) => (mode === 'plan'
      ? salesPayService.createPlan({ name: values.name, version: versionPayload(values) })
      : salesPayService.createPlanVersion(plan.id, versionPayload(values))),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['salesPay'] });
      onClose();
    },
    onError: (error) => setRefusal(isHandledPayError(error) ? error : null),
  });

  return (
    <Modal
      open
      width={760}
      title={mode === 'plan'
        ? t('sales_agents:pay.plans.new', 'New plan')
        : t('sales_agents:pay.plans.version_title', { defaultValue: 'New version of {{plan}}', plan: plan.name })}
      okText={t('sales_agents:pay.form.save', 'Save')}
      okButtonProps={{ disabled: !ready }}
      confirmLoading={mutation.isPending}
      onOk={() => form.submit()}
      onCancel={onClose}
    >
      <PayErrorAlert error={refusal} productNames={productNames} />
      {!ready ? <Spin /> : (
        <Form
          form={form}
          layout="vertical"
          initialValues={formValues(config, source)}
          onFinish={(values) => { setRefusal(null); mutation.mutate(values); }}
        >
          {mode === 'plan' ? (
            <Form.Item name="name" label={t('sales_agents:pay.form.plan_name', 'Plan name')} rules={[{ required: true, whitespace: true, message: t('sales_agents:pay.form.required', 'Required') }]}>
              <Input maxLength={100} />
            </Form.Item>
          ) : null}
          <Form.Item name="effective_month" label={t('sales_agents:pay.form.effective_month', 'Effective month')} rules={required}>
            <DatePicker picker="month" format="MM.YYYY" disabledDate={(value) => value.format('YYYY-MM') < config.editable_from_month} />
          </Form.Item>
          {next ? (
            <Alert
              type="warning"
              showIcon
              data-testid="month-shadowed"
              style={{ marginBottom: 12 }}
              message={t('sales_agents:pay.form.month_shadowed', {
                defaultValue: 'This version will apply only to {{months}}. Version {{version}} takes over from {{next}}. To change the plan from {{next}} on, pick {{next}} or later.',
                months: shadowedMonths(t, picked, next.from_month),
                version: next.version_no,
                next: monthLabel(next.from_month),
              })}
            />
          ) : null}

          <Divider orientation="left">{t('sales_agents:pay.form.default_tiers', 'Default tiers')}</Divider>
          <Paragraph type="secondary">
            {t('sales_agents:pay.hint.tiers', "Units of one product that the agent sells in a month fill the tiers in the order the orders were delivered and paid. Each tier pays its own rate on the units inside it and runs up to the next tier's first unit; the last tier has no upper limit. Products without their own tiers use the default tiers, counted per product.")}
          </Paragraph>
          <div data-testid="default-tiers">
            <TierList name="default_tiers" path={['default_tiers']} modeOptions={modeOptions} required={required} />
          </div>

          <Divider orientation="left">{t('sales_agents:pay.form.product_tiers', 'Product tiers')}</Divider>
          <Form.List name="rates">
            {(fields, { add, remove }) => (
              <>
                {fields.map((field) => (
                  <div key={field.key} data-testid="product-tiers" style={{ marginBottom: 16 }}>
                    <Space align="baseline">
                      <Form.Item name={[field.name, 'product_id']} rules={required}>
                        <Select aria-label={productLabel} placeholder={productLabel} style={{ width: 280 }} options={productOptions} showSearch optionFilterProp="label" />
                      </Form.Item>
                      <MinusCircleOutlined aria-label={t('sales_agents:pay.form.remove', 'Remove')} onClick={() => remove(field.name)} />
                    </Space>
                    <TierList name={[field.name, 'tiers']} path={['rates', field.name, 'tiers']} modeOptions={modeOptions} required={required} />
                  </div>
                ))}
                <Button type="dashed" icon={<PlusOutlined />} onClick={() => add({ tiers: [firstTier(config.rate_modes)] })}>
                  {t('sales_agents:pay.form.add_product', 'Add product')}
                </Button>
              </>
            )}
          </Form.List>

          <Divider orientation="left">{t('sales_agents:pay.form.gate_bands', 'Plan-vs-fact bands')}</Divider>
          <Form.List name="gate_bands">
            {(fields, { add, remove }) => (
              <>
                {fields.map((field) => (
                  <Space key={field.key} align="baseline">
                    <Form.Item name={[field.name, 'min_pct']} label={t('sales_agents:pay.form.min_pct', 'From %')} rules={required}>
                      <InputNumber min={0} max={100} step={0.1} />
                    </Form.Item>
                    <Form.Item name={[field.name, 'multiplier']} label={t('sales_agents:pay.form.multiplier', 'Multiplier')} rules={required}>
                      <InputNumber min={0} max={1} step={0.1} />
                    </Form.Item>
                    <MinusCircleOutlined aria-label={t('sales_agents:pay.form.remove', 'Remove')} onClick={() => remove(field.name)} />
                  </Space>
                ))}
                <Button type="dashed" icon={<PlusOutlined />} onClick={() => add()}>{t('sales_agents:pay.form.add_band', 'Add band')}</Button>
              </>
            )}
          </Form.List>
          <Form.Item name="gate_min_visits_due" label={t('sales_agents:pay.form.min_visits_due', 'Min visits due')} rules={required}>
            <InputNumber min={0} max={10000} />
          </Form.Item>

          <Divider orientation="left">{t('sales_agents:pay.form.bonus_amount', 'New-outlet bonus')}</Divider>
          <Row gutter={8}>
            <Col span={12}>
              <Form.Item name={['new_outlet_bonus', 'amount']} label={t('sales_agents:pay.form.bonus_amount', 'New-outlet bonus')} rules={required}>
                <MoneyInput />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name={['new_outlet_bonus', 'window_days']} label={t('sales_agents:pay.form.bonus_window_days', 'Window (days)')} rules={required}>
                <InputNumber min={1} max={365} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name={['new_outlet_bonus', 'prior_customer_lookback_days']} label={t('sales_agents:pay.form.bonus_lookback_days', 'Prior-customer lookback (days)')} rules={required}>
                <InputNumber min={0} max={730} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name={['new_outlet_bonus', 'min_orders_with_total']} label={t('sales_agents:pay.form.bonus_min_orders_with_total', 'Orders with total')} rules={required}>
                <InputNumber min={1} max={50} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name={['new_outlet_bonus', 'min_combined_total']} label={t('sales_agents:pay.form.bonus_min_combined_total', 'Min combined total')} rules={required}>
                <MoneyInput />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name={['new_outlet_bonus', 'min_orders_any_amount']} label={t('sales_agents:pay.form.bonus_min_orders_any', 'Orders of any amount')} rules={required}>
                <InputNumber min={1} max={100} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
          </Row>
          <Form.Item name="note" label={t('sales_agents:pay.form.note', 'Note')}>
            <Input.TextArea rows={2} maxLength={500} />
          </Form.Item>
        </Form>
      )}
    </Modal>
  );
};

export default PlanVersionModal;
