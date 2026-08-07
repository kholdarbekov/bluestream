import React, { useRef } from 'react';
import {
  Button, Card, Dropdown, Popconfirm, Space, Tag, Tooltip, Typography,
} from 'antd';
import {
  ArrowDownOutlined, ArrowUpOutlined, HolderOutlined, PushpinFilled, PushpinOutlined,
  RollbackOutlined, SwapOutlined, ThunderboltOutlined,
} from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import { moveBy, reorder, driverColor } from './dispatchLogic';

const { Text } = Typography;

/**
 * One driver's stop list, editable.
 *
 * Sequence edits are DRAFT: they bubble up through onReorder/onTogglePin and
 * are persisted only by onSave. Assignment edits (onMove/onPool) are immediate,
 * because the server can legitimately refuse them — a COD-blocked driver, a
 * stop that is no longer claimable — and a draft cannot represent a rejection.
 */
const DriverRoutePanel = ({
  route,
  drivers = [],
  stops = [],
  pinned = {},
  dirty = false,
  saving = false,
  onReorder,
  onTogglePin,
  onMove,
  onPool,
  onSave,
  onDiscard,
  onReoptimize,
}) => {
  const { t } = useTranslation('delivery');
  const dragFrom = useRef(null);
  const ids = stops.map((s) => s.delivery_id);

  const handleDrop = (index) => {
    if (dragFrom.current === null || dragFrom.current === index) return;
    onReorder(reorder(ids, dragFrom.current, index));
    dragFrom.current = null;
  };

  const moveTargets = (deliveryId) => ({
    items: drivers
      .filter((d) => d.driver_id !== route.driver_id)
      .map((d) => ({ key: String(d.driver_id), label: d.full_name })),
    onClick: ({ key }) => onMove(deliveryId, Number(key)),
  });
  // A route implies its own driver exists, but not any OTHER driver to move
  // a stop to — with none, the move Dropdown would open empty. Same defect,
  // same fix as PoolPanel's assign control.
  const hasMoveTargets = drivers.some((d) => d.driver_id !== route.driver_id);

  return (
    <Card
      size="small"
      styles={{ body: { padding: 8 } }}
      title={(
        <Space size={6} wrap>
          <span style={{
            width: 10, height: 10, borderRadius: 5, display: 'inline-block',
            background: driverColor(route.driver_id),
          }}
          />
          <Text strong>{route.driver_name || `#${route.driver_id}`}</Text>
          {/* `metrics_stale` (DispatchService._routes(), set by
              RouteEditService whenever a stop moves on/off this route or a
              hand-authored sequence saves without a fresh matrix figure)
              means total_distance_km/estimated_duration_minutes can describe
              a route that no longer matches the stops below. Qualify with
              "≈" + a tooltip rather than showing a confidently wrong number —
              the whole point of the flag existing is that an admin must not
              trust it at face value. */}
          <Tooltip title={route.metrics_stale
            ? t(
              'ui.dispatch.metrics_stale_hint',
              'This route changed since these figures were last measured — they may not match the current stops',
            )
            : undefined}
          >
            <Text type="secondary" data-testid={route.metrics_stale ? 'route-metrics-stale' : undefined}>
              {stops.length} {t('ui.dispatch.stops', 'stops')}
              {route.total_distance_km
                ? ` · ${route.metrics_stale ? '≈' : ''}${route.total_distance_km.toFixed(1)} km`
                : ''}
              {route.estimated_duration_minutes
                ? ` · ${route.metrics_stale ? '≈' : ''}${route.estimated_duration_minutes} min`
                : ''}
            </Text>
          </Tooltip>
          {route.manual_override && (
            <Tag color="gold" data-testid="route-locked-badge">
              {t('ui.dispatch.locked_by', 'Set by')} {route.overridden_by_name || t('ui.dispatch.dispatch', 'dispatch')}
            </Tag>
          )}
        </Space>
      )}
      extra={(
        <Space size={4}>
          <Button
            data-testid="route-save"
            type="primary"
            size="small"
            disabled={!dirty}
            loading={saving}
            onClick={onSave}
          >
            {t('ui.dispatch.save_route', 'Save route')}
          </Button>
          <Button size="small" disabled={!dirty} onClick={onDiscard}>
            {t('ui.dispatch.discard', 'Discard')}
          </Button>
          <Popconfirm
            title={t('ui.dispatch.reoptimize_confirm', 'Clear the manual order and re-optimise?')}
            onConfirm={onReoptimize}
          >
            <Button size="small" icon={<ThunderboltOutlined />}>
              {t('ui.dispatch.reoptimize', 'Reset to optimal')}
            </Button>
          </Popconfirm>
        </Space>
      )}
    >
      {stops.map((stop, index) => {
        const isPinned = String(stop.delivery_id) in (pinned || {});
        return (
          <div
            key={stop.delivery_id}
            data-testid={`stop-row-${stop.delivery_id}`}
            data-delivery-id={String(stop.delivery_id)}
            draggable
            onDragStart={() => { dragFrom.current = index; }}
            onDragOver={(e) => e.preventDefault()}
            onDrop={() => handleDrop(index)}
            style={{
              display: 'flex', alignItems: 'center', gap: 8, padding: '6px 4px',
              borderBottom: '1px solid rgba(0,0,0,.06)', cursor: 'grab',
            }}
          >
            <HolderOutlined style={{ opacity: 0.4 }} />
            <Text strong style={{ minWidth: 18 }}>{index + 1}</Text>
            {/* `overflow: hidden` gives the ellipsis span something bounded to
                clip against; `flex: 1, minWidth: 0` alone lets the flex
                algorithm shrink this column, but without a hard bound the
                text itself still dictates the row's min content size. */}
            <div style={{ flex: 1, minWidth: 0, overflow: 'hidden' }}>
              {/* antd's `ellipsis` boolean only switches on CSS ellipsis
                  after a layout effect measures `text-overflow` support; it
                  does nothing without `white-space: nowrap` actually applied,
                  which is why this line wrapped one character per line
                  instead of truncating. Setting the properties explicitly
                  does not depend on that timing. */}
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
            </div>
            {/* `flexShrink: 0`: without it the flex algorithm satisfies this
                row's width by collapsing the text column above to near-zero
                instead of shrinking this fixed-content button group — the
                actual cause of the one-character-per-line rendering. */}
            <Space size={2} data-testid={`stop-actions-${stop.delivery_id}`} style={{ flexShrink: 0 }}>
              <Button
                data-testid={`stop-up-${stop.delivery_id}`}
                size="small" type="text" icon={<ArrowUpOutlined />}
                disabled={index === 0}
                onClick={() => onReorder(moveBy(ids, index, -1))}
              />
              <Button
                data-testid={`stop-down-${stop.delivery_id}`}
                size="small" type="text" icon={<ArrowDownOutlined />}
                disabled={index === stops.length - 1}
                onClick={() => onReorder(moveBy(ids, index, 1))}
              />
              <Tooltip title={t('ui.dispatch.pin_hint', 'Keep this stop at this position when re-optimising')}>
                <Button
                  data-testid={`stop-pin-${stop.delivery_id}`}
                  size="small" type="text"
                  icon={isPinned ? <PushpinFilled style={{ color: '#faad14' }} /> : <PushpinOutlined />}
                  onClick={() => onTogglePin(stop.delivery_id)}
                />
              </Tooltip>
              <Tooltip title={!hasMoveTargets ? t('ui.dispatch.no_other_drivers', 'No other drivers available') : undefined}>
                {/* antd disabled Buttons don't fire hover events on their
                    own — the Tooltip needs a wrapping element to attach its
                    listeners to. */}
                <span>
                  <Dropdown menu={moveTargets(stop.delivery_id)} trigger={['click']} disabled={!hasMoveTargets}>
                    <Button
                      data-testid={`stop-move-${stop.delivery_id}`}
                      size="small"
                      type="text"
                      icon={<SwapOutlined />}
                      disabled={!hasMoveTargets}
                    />
                  </Dropdown>
                </span>
              </Tooltip>
              <Popconfirm
                title={t('ui.dispatch.pool_confirm', 'Return this stop to the unassigned pool?')}
                onConfirm={() => onPool(stop.delivery_id)}
              >
                <Button data-testid={`stop-pool-${stop.delivery_id}`} size="small" type="text" icon={<RollbackOutlined />} />
              </Popconfirm>
            </Space>
          </div>
        );
      })}
    </Card>
  );
};

export default DriverRoutePanel;
