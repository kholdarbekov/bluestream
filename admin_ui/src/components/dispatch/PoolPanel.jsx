import React from 'react';
import {
  Button, Card, Dropdown, Empty, Space, Tag, Tooltip, Typography,
} from 'antd';
import { CarOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import { formatStopItems } from './dispatchLogic';

const { Text } = Typography;

/**
 * The unassigned pool: stops with no driver, waiting to be picked up.
 *
 * Presentational only — no data fetching, no server state, no auto-assign.
 * Mirrors DriverRoutePanel's "move to driver" affordance (a Dropdown of
 * drivers) rather than inventing a second interaction, so an admin who
 * already knows how to move a stop between routes needs no new gesture to
 * pull one out of the pool. Assignment is immediate, not draft-and-save,
 * because the server can legitimately refuse it (a COD-blocked driver, a
 * stop no longer claimable) and a draft cannot represent a rejection.
 */
const PoolPanel = ({
  stops = [],
  drivers = [],
  assigning = false,
  onAssign,
  onFocusStop,
  selectedDeliveryId = null,
}) => {
  const { t } = useTranslation('delivery');
  // Unlike a driver route (which implies at least one driver exists),
  // pooled work can genuinely sit here with nobody on shift — the normal
  // state before the first shift starts. Without this guard the Assign
  // button opens a Dropdown with an empty `items` list: indistinguishable
  // from a broken control to the dispatcher, which `pool-empty` exists to
  // avoid at the panel level and this exists to avoid at the row level.
  const noDrivers = drivers.length === 0;

  const assignTargets = (deliveryId) => ({
    items: drivers.map((d) => ({ key: String(d.driver_id), label: d.full_name })),
    onClick: ({ key }) => onAssign(deliveryId, Number(key)),
  });

  return (
    <Card
      size="small"
      styles={{ body: { padding: 8 } }}
      data-testid="pool-panel"
      title={(
        <Space size={6}>
          <Text strong>{t('ui.dispatch.pool_title', 'Unassigned pool')}</Text>
          <Text type="secondary">{stops.length} {t('ui.dispatch.stops', 'stops')}</Text>
        </Space>
      )}
    >
      {stops.length === 0 && (
        <Empty
          data-testid="pool-empty"
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description={t('ui.dispatch.pool_empty', 'Nothing waiting')}
        />
      )}
      {stops.map((stop) => (
        <div
          key={stop.delivery_id}
          data-testid={`pool-row-${stop.delivery_id}`}
          data-delivery-id={String(stop.delivery_id)}
          data-selected={selectedDeliveryId === stop.delivery_id ? 'true' : 'false'}
          onClick={() => onFocusStop && onFocusStop(stop)}
          style={{
            display: 'flex', alignItems: 'center', gap: 8, padding: '6px 4px',
            borderBottom: '1px solid rgba(0,0,0,.06)',
            background: selectedDeliveryId === stop.delivery_id ? 'rgba(22,119,255,.1)' : undefined,
          }}
        >
          {/* `overflow: hidden` gives the ellipsis text something bounded to
              clip against; see DriverRoutePanel for the full explanation —
              this row hits the identical flexbox constraint. */}
          <div style={{ flex: 1, minWidth: 0, overflow: 'hidden' }}>
            <Text
              ellipsis
              style={{ display: 'block', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}
            >
              {stop.order_number} · {stop.customer_name}
            </Text>
            <Text
              type="secondary"
              ellipsis
              style={{
                display: 'block', fontSize: 11, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
              }}
            >
              {stop.address_label}
            </Text>
            {/* Same line, same formatter as a route stop card — see
                `formatStopItems`. A dispatcher's view of what they are
                loading must not change the moment the stop is assigned. */}
            {formatStopItems(stop) && (
              <Text
                data-testid={`pool-items-${stop.delivery_id}`}
                ellipsis
                style={{
                  display: 'block', fontSize: 11, color: '#1677ff',
                  whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
                }}
              >
                {formatStopItems(stop)}
              </Text>
            )}
            <Space size={4}>
              {stop.is_cod && <Tag color="gold">COD</Tag>}
              {stop.is_overdue && <Tag color="red">{t('ui.dispatch.overdue', 'Overdue')}</Tag>}
            </Space>
          </div>
          {/* `flexShrink: 0`: without it the flex algorithm shrinks the text
              column above to near-zero instead of this fixed-content button —
              the actual cause of the one-character-per-line rendering. */}
          <Tooltip title={noDrivers ? t('ui.dispatch.no_drivers_available', 'No drivers available') : undefined}>
            {/* antd disabled Buttons don't fire hover events on their own —
                the Tooltip needs a wrapping element to attach its listeners
                to. */}
            <span style={{ flexShrink: 0 }} onClick={(e) => e.stopPropagation()}>
              <Dropdown menu={assignTargets(stop.delivery_id)} trigger={['click']} disabled={noDrivers}>
                <Button
                  data-testid={`pool-assign-${stop.delivery_id}`}
                  size="small"
                  icon={<CarOutlined />}
                  loading={assigning}
                  disabled={assigning || noDrivers}
                >
                  {t('ui.dispatch.assign', 'Assign')}
                </Button>
              </Dropdown>
            </span>
          </Tooltip>
        </div>
      ))}
    </Card>
  );
};

export default PoolPanel;
