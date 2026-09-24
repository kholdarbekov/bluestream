import { DatePicker, Form, Segmented, Space, TimePicker } from 'antd';
import { useTranslation } from 'react-i18next';
import dayjs from 'dayjs';

// The bounds arrive as the backend's ISO 'YYYY-MM-DD' strings (or a dayjs a caller already has).
const asDay = (value) => (value ? dayjs(value) : null);

/**
 * The delivery date + window fields shared by Create Order and Reschedule.
 *
 * Renders four fields into the ENCLOSING antd Form: `delivery_date`, `window_preset`,
 * `window_start`, `window_end`. The parent turns them into a payload with
 * `buildSchedulePayload`, and seeds `window_preset` through its Form `initialValues`
 * (`presetFromWindow` for a stored schedule, 'anytime' for a new one).
 *
 * The date range is the backend's (`minDate`/`maxDate`), never the browser clock's. Until both
 * bounds are known no day is offered at all. `allowClear={false}` is for a schedule that must
 * keep a date (a delivery already released to drivers, R10). There an empty field cannot mean
 * "as soon as possible", so that placeholder is not shown.
 */
const DeliverySchedulePicker = ({ minDate, maxDate, allowClear = true }) => {
  const { t } = useTranslation('orders');
  const form = Form.useFormInstance();
  const preset = Form.useWatch('window_preset', form);
  const min = asDay(minDate);
  const max = asDay(maxDate);
  const boundsKnown = Boolean(min && max);

  return (
    <Form.Item label={t('ui.orders.delivery_schedule', 'Delivery schedule')}>
      <Space direction="vertical" style={{ width: '100%' }}>
        <Form.Item name="delivery_date" noStyle>
          <DatePicker
            style={{ width: '100%' }}
            allowClear={allowClear}
            disabled={!boundsKnown}
            placeholder={allowClear ? t('ui.orders.deliver_asap', 'Deliver as soon as possible') : undefined}
            disabledDate={(current) =>
              Boolean(current) && boundsKnown && (current.isBefore(min, 'day') || current.isAfter(max, 'day'))
            }
          />
        </Form.Item>
        <Form.Item name="window_preset" noStyle>
          <Segmented
            options={[
              { label: t('ui.orders.window_anytime', 'Anytime'), value: 'anytime' },
              { label: t('ui.orders.window_morning', 'Morning'), value: 'morning' },
              { label: t('ui.orders.window_afternoon', 'Afternoon'), value: 'afternoon' },
              { label: t('ui.orders.window_evening', 'Evening'), value: 'evening' },
              { label: t('ui.orders.window_custom', 'Custom'), value: 'custom' },
            ]}
          />
        </Form.Item>
        {preset === 'custom' ? (
          <Space>
            <Form.Item name="window_start" noStyle>
              <TimePicker format="HH:mm" minuteStep={15} allowClear
                placeholder={t('ui.orders.window_from_any', 'From (any)')} />
            </Form.Item>
            <Form.Item name="window_end" noStyle>
              <TimePicker format="HH:mm" minuteStep={15} allowClear
                placeholder={t('ui.orders.window_to_any', 'To (any)')} />
            </Form.Item>
          </Space>
        ) : null}
      </Space>
    </Form.Item>
  );
};

export default DeliverySchedulePicker;
