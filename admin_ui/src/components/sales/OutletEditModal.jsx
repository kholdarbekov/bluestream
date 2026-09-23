import React from 'react';
import { Button, Col, Form, Input, InputNumber, Modal, Row, Select, TimePicker, Typography, message } from 'antd';
import { useTranslation } from 'react-i18next';
import dayjs from 'dayjs';
import customParseFormat from 'dayjs/plugin/customParseFormat';

import { OUTLET_CLASSES, PAYMENT_TERMS } from './outletVocabulary';

dayjs.extend(customParseFormat);

const { Text } = Typography;

// The wire shape of both window columns: `serialize_outlet` publishes "HH:MM" and
// `parse_window_time` reads it back. Same string, both directions.
const WINDOW_FORMAT = 'HH:mm';
const LANGUAGES = ['uz', 'ru', 'en'];
const TEXT_FIELDS = ['name', 'channel', 'preferred_visit_window', 'legal_form', 'tax_id', 'competitor_note', 'status_warning', 'notes'];

const blankToNull = (value) => (value === undefined || value === '' ? null : value);
const trimmed = (value) => (typeof value === 'string' ? value.trim() : value);

// What the admin actually changed, and only that (D29). The PUT is exclude_unset: an absent key is
// left alone and an explicit null clears the column. The window travels as a PAIR — one edge alone
// is a window the order path ignores — so if either edge changed, both are sent.
export const changedFields = (outlet, values) => {
  const wire = {
    // eslint-disable-next-line security/detect-object-injection
    ...Object.fromEntries(TEXT_FIELDS.map((key) => [key, blankToNull(trimmed(values[key]))])),
    class: blankToNull(values.class),
    cadence_days_override: blankToNull(values.cadence_days_override),
    payment_terms: values.payment_terms,
    preferred_language: values.preferred_language,
  };
  const payload = {};
  Object.entries(wire).forEach(([key, value]) => {
    // eslint-disable-next-line security/detect-object-injection
    if (value !== blankToNull(outlet[key])) payload[key] = value;
  });
  const start = values.delivery_window_start ? values.delivery_window_start.format(WINDOW_FORMAT) : null;
  const end = values.delivery_window_end ? values.delivery_window_end.format(WINDOW_FORMAT) : null;
  if (start !== blankToNull(outlet.delivery_window_start) || end !== blankToNull(outlet.delivery_window_end)) {
    payload.delivery_window_start = start;
    payload.delivery_window_end = end;
  }
  return payload;
};

const OutletEditModal = ({ outlet, open, saving, onCancel, onSubmit }) => {
  const { t } = useTranslation(['sales_agents', 'common']);
  if (!outlet) return null;

  const finish = (values) => {
    if (Boolean(values.delivery_window_start) !== Boolean(values.delivery_window_end)) {
      message.error(t('sales_agents:outlets.delivery_window.invalid', 'Set both the start and the end, or clear both.'));
      return;
    }
    const payload = changedFields(outlet, values);
    if (Object.keys(payload).length === 0) {
      onCancel();
      return;
    }
    onSubmit(payload);
  };

  const text = (name, label, testId) => (
    <Form.Item name={name} label={label}><Input data-testid={testId} /></Form.Item>
  );

  return (
    <Modal title={t('sales_agents:outlets.edit_title', 'Edit outlet')} open={open} onCancel={onCancel} footer={null} destroyOnClose width={680}>
      {/* destroyOnClose is what makes `initialValues` re-read the outlet on every open. */}
      <Form
        layout="vertical"
        onFinish={finish}
        initialValues={{
          ...Object.fromEntries(TEXT_FIELDS.map((key) => [key, outlet[key]])), // eslint-disable-line security/detect-object-injection
          class: outlet.class || undefined,
          cadence_days_override: outlet.cadence_days_override,
          payment_terms: outlet.payment_terms,
          preferred_language: outlet.preferred_language,
          delivery_window_start: outlet.delivery_window_start ? dayjs(outlet.delivery_window_start, WINDOW_FORMAT) : null,
          delivery_window_end: outlet.delivery_window_end ? dayjs(outlet.delivery_window_end, WINDOW_FORMAT) : null,
        }}
      >
        <Row gutter={16}>
          <Col span={16}>
            <Form.Item name="name" label={t('sales_agents:outlet_name', 'Outlet')} rules={[{ required: true }]}><Input maxLength={200} /></Form.Item>
          </Col>
          <Col span={8}>
            <Form.Item name="class" label={t('sales_agents:class', 'Class')}>
              <Select allowClear options={OUTLET_CLASSES.map((c) => ({ value: c, label: c }))} data-testid="edit-class-select" />
            </Form.Item>
          </Col>
          <Col span={12}>{text('channel', t('sales_agents:outlets.fields.channel', 'Channel'))}</Col>
          <Col span={12}>
            <Form.Item name="cadence_days_override" label={t('sales_agents:outlets.fields.cadence_days_override', 'Visit every (days)')} extra={t('sales_agents:outlets.fields.cadence_help', 'Leave empty to use the class default.')}>
              <InputNumber min={1} max={365} style={{ width: '100%' }} />
            </Form.Item>
          </Col>
          <Col span={12}>{text('preferred_visit_window', t('sales_agents:outlets.fields.preferred_visit_window', 'Best time to visit'))}</Col>
          <Col span={6}>
            <Form.Item name="delivery_window_start" label={t('sales_agents:outlets.delivery_window.start', 'From')}>
              <TimePicker format={WINDOW_FORMAT} style={{ width: '100%' }} />
            </Form.Item>
          </Col>
          <Col span={6}>
            <Form.Item name="delivery_window_end" label={t('sales_agents:outlets.delivery_window.end', 'Until')}>
              <TimePicker format={WINDOW_FORMAT} style={{ width: '100%' }} />
            </Form.Item>
          </Col>
          <Col span={24}><Text type="secondary">{t('sales_agents:outlets.delivery_window.help', 'The default delivery window for every order placed at this outlet. Clear both fields to remove it.')}</Text></Col>
          <Col span={12}>
            <Form.Item name="payment_terms" label={t('sales_agents:outlets.fields.payment_terms', 'Payment terms')}>
              <Select options={PAYMENT_TERMS.map((v) => ({ value: v, label: t(`sales_agents:outlets.payment_terms.${v}`, v) }))} />
            </Form.Item>
          </Col>
          <Col span={12}>
            <Form.Item name="preferred_language" label={t('sales_agents:outlets.fields.preferred_language', 'Language')}>
              <Select options={LANGUAGES.map((v) => ({ value: v, label: v }))} />
            </Form.Item>
          </Col>
          <Col span={12}>{text('legal_form', t('sales_agents:outlets.fields.legal_form', 'Legal form'))}</Col>
          <Col span={12}>{text('tax_id', t('sales_agents:outlets.fields.tax_id', 'Tax ID (INN)'))}</Col>
          <Col span={24}>{text('competitor_note', t('sales_agents:outlets.fields.competitor_note', 'Competitor note'))}</Col>
          <Col span={24}>{text('status_warning', t('sales_agents:outlets.fields.status_warning', 'Warning for staff'))}</Col>
          <Col span={24}>
            <Form.Item name="notes" label={t('sales_agents:notes', 'Notes')}><Input.TextArea rows={2} data-testid="edit-notes" /></Form.Item>
          </Col>
        </Row>
        <Button type="primary" htmlType="submit" loading={saving}>{t('ui.common.save', 'Save')}</Button>
      </Form>
    </Modal>
  );
};

export default OutletEditModal;
