import React, { useMemo, useRef } from 'react';
import {
  Button, Card, Dropdown, Popconfirm, Space, Tag, Tooltip, Typography,
} from 'antd';
import {
  ArrowDownOutlined, ArrowUpOutlined, HolderOutlined, PushpinFilled, PushpinOutlined,
  RollbackOutlined, SwapOutlined, ThunderboltOutlined,
} from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import {
  moveBy, reorder, driverColor, formatStopItems, formatLeg,
} from './dispatchLogic';

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
  legs = null,
  legDeliveryIds = null,
  onFocusStop,
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

  // Per-stop travel figures, keyed by the delivery the API says each leg
  // ARRIVES at. Deliberately not `legs[index]` against this component's own
  // stop list: the backend drops stops with no coordinates before measuring,
  // so on a route with one ungeocoded stop the two sequences diverge and
  // every subsequent leg would be captioned with the wrong stop — silently,
  // and looking entirely plausible.
  //
  // A length mismatch means the pairing is unknowable, so nothing is shown at
  // all. Showing some stops a number that might belong to a neighbour is the
  // failure this guard exists to prevent.
  const legByDeliveryId = useMemo(() => {
    if (!Array.isArray(legs) || !Array.isArray(legDeliveryIds)) return {};
    if (legs.length !== legDeliveryIds.length) return {};
    return Object.fromEntries(
      legDeliveryIds
        .map((deliveryId, index) => [String(deliveryId), legs.at(index)])
        .filter(([, leg]) => leg != null),
    );
  }, [legs, legDeliveryIds]);

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
              trust it at face value.

              Separately (final review round, I5): `estimated_duration_minutes`
              is TRAVEL + a flat per-stop service-time allowance
              (`route_optimization_service.py::_sum_route_metrics`, spec 8.4)
              — not travel alone. The number itself is never altered here
              (that would be re-deriving a backend decision in JS, the exact
              thing CLAUDE.md forbids); when it isn't already flagged stale,
              the tooltip instead explains what's included so "62 min" isn't
              read as a pure drive-time estimate. */}
          <Tooltip title={route.metrics_stale
            ? t(
              'ui.dispatch.metrics_stale_hint',
              'This route changed since these figures were last measured — they may not match the current stops',
            )
            : (route.estimated_duration_minutes
              ? t(
                'ui.dispatch.duration_includes_service_time_hint',
                'Includes stop time (loading/handoff) at each delivery, not travel alone',
              )
              : undefined)}
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
        const itemsLine = formatStopItems(stop);
        const legLine = formatLeg(legByDeliveryId[String(stop.delivery_id)]);
        return (
          <div
            key={stop.delivery_id}
            data-testid={`stop-row-${stop.delivery_id}`}
            data-delivery-id={String(stop.delivery_id)}
            draggable
            onDragStart={() => { dragFrom.current = index; }}
            onDragOver={(e) => e.preventDefault()}
            onDrop={() => handleDrop(index)}
            // Clicking the row body moves the map to this stop, which is how
            // the stacked layout keeps the list and the map connected. The
            // action buttons stop propagation themselves (see the group
            // below), so nudging a stop never also yanks the map.
            onClick={() => onFocusStop && onFocusStop(stop)}
            style={{
              display: 'flex', alignItems: 'flex-start', gap: 8, padding: '8px 4px',
              borderBottom: '1px solid rgba(0,0,0,.06)', cursor: 'grab',
            }}
          >
            <HolderOutlined style={{ opacity: 0.4, marginTop: 4 }} />
            <Text strong style={{ minWidth: 18 }}>{index + 1}</Text>
            {/* `overflow: hidden` gives the ellipsis span something bounded to
                clip against; `flex: 1, minWidth: 0` alone lets the flex
                algorithm shrink this column, but without a hard bound the
                text itself still dictates the row's min content size.
                (Until 2026-08 a global `.ant-space { width: 100% }` in
                index.css overrode all of this from the sibling action group
                and squeezed this column to exactly 0px — the order number and
                customer name were present in the DOM and invisible on screen.
                See the note in index.css before reintroducing anything of
                that shape.) */}
            <div style={{ flex: 1, minWidth: 0, overflow: 'hidden' }}>
              {/* Travel to THIS stop, above its name — the number describes the
                  hop the driver makes to arrive here, so reading it before the
                  destination matches the order things happen in. Absent
                  whenever the provider did not measure it. */}
              {legLine && (
                <Text
                  type="secondary"
                  data-testid={`stop-leg-${stop.delivery_id}`}
                  style={{ display: 'block', fontSize: 11, opacity: 0.75 }}
                >
                  ↳ {legLine}
                </Text>
              )}
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
              {itemsLine && (
                <Text
                  data-testid={`stop-items-${stop.delivery_id}`}
                  ellipsis
                  style={{
                    display: 'block', fontSize: 11, color: '#1677ff',
                    whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
                  }}
                >
                  {itemsLine}
                </Text>
              )}
            </div>
            {/* `flexShrink: 0`: without it the flex algorithm satisfies this
                row's width by collapsing the text column above to near-zero
                instead of shrinking this fixed-content button group — the
                actual cause of the one-character-per-line rendering. */}
            {/* `stopPropagation`: these controls sit inside the row's
                focus-the-map click target, and reordering a stop must not
                also scroll the map out from under the admin. */}
            <Space
              size={2}
              data-testid={`stop-actions-${stop.delivery_id}`}
              style={{ flexShrink: 0 }}
              onClick={(e) => e.stopPropagation()}
            >
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
