import React, { useEffect, useMemo } from 'react';
import {
  MapContainer, TileLayer, CircleMarker, Marker, Polyline, Popup, useMap,
} from 'react-leaflet';
import L from 'leaflet';
import 'leaflet/dist/leaflet.css';
import { Space, Tag, Typography } from 'antd';
import { useTranslation } from 'react-i18next';
import HeatLayer from './HeatLayer';
import { driverColor } from './dispatch/dispatchLogic';
import { daysSince, colorForDays } from '../utils/customerMapLogic';
import { formatMoney } from '../utils/formatMoney';

const { Text } = Typography;
const CENTER = [41.2995, 69.2401];
const ZOOM = 11;

const ORDER_STATUS_COLOR = {
  pending: '#8c8c8c',
  confirmed: '#1677ff',
  preparing: '#13c2c2',
  out_for_delivery: '#fa8c16',
};

/** Numbered route-stop badge. A route is only readable if you can see the order. */
const stopIcon = (position, color, pinned) => L.divIcon({
  className: '',
  html: `<div style="background:${color};color:#fff;border-radius:50%;width:24px;height:24px;
    display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:600;
    border:2px solid ${pinned ? '#faad14' : '#fff'};box-shadow:0 1px 4px rgba(0,0,0,.4)">${position + 1}</div>`,
  iconSize: [24, 24],
  iconAnchor: [12, 12],
});

const orderIcon = (order) => {
  const color = ORDER_STATUS_COLOR[order.status] || '#8c8c8c';
  const unassigned = !order.driver_id;
  return L.divIcon({
    className: '',
    html: `<div style="width:14px;height:14px;border-radius:3px;
      background:${unassigned ? 'transparent' : color};
      border:2px ${unassigned ? 'dashed' : 'solid'} ${color};
      box-shadow:${order.is_overdue ? '0 0 0 3px rgba(255,77,79,.55)' : 'none'}"></div>`,
    iconSize: [14, 14],
    iconAnchor: [7, 7],
  });
};

const driverIcon = (color) => L.divIcon({
  className: '',
  html: `<div style="font-size:20px;line-height:20px;filter:drop-shadow(0 1px 2px rgba(0,0,0,.5));
    color:${color}">&#128666;</div>`,
  iconSize: [20, 20],
  iconAnchor: [10, 10],
});

/**
 * Pans the map to `point` when it changes.
 *
 * A child component rather than a prop on MapContainer, because Leaflet's map
 * instance only exists inside the container's context — `MapContainer`'s
 * `center` is an INITIAL value and re-rendering with a new one does not move
 * an already-mounted map.
 *
 * Keeps the current zoom deliberately: the admin picks a working scale (one
 * district, or the whole city) and a focus gesture that also rezoomed would
 * keep undoing that choice.
 */
const FocusController = ({ point }) => {
  const map = useMap();
  const lat = Array.isArray(point) ? point[0] : null;
  const lng = Array.isArray(point) ? point[1] : null;
  useEffect(() => {
    if (typeof lat !== 'number' || typeof lng !== 'number') return;
    map.setView([lat, lng], map.getZoom());
  }, [map, lat, lng]);
  return null;
};

/**
 * Shared operations map. Every layer is data-in / callbacks-out.
 *
 * `geometry` maps driverId -> { geometry: [[lat,lng],...] | null, approximate }.
 * A driver with no real geometry falls back to straight dashed legs so the map
 * never goes blank — and the dashes make it obvious it isn't a road path.
 */
const OperationsMap = ({
  customers = [],
  orders = [],
  drivers = [],
  routes = [],
  geometry = {},
  visibleLayers = { customers: true, orders: false, drivers: false },
  selectedDriverId = null,
  onSelectStop,
  onSelectDriver,
  height = 640,
  thresholds = { t1: 7, t2: 30 },
  heatPoints = null,
  renderCustomerPopup,
  focusPoint = null,
}) => {
  const { t } = useTranslation(['delivery', 'users']);
  const now = useMemo(() => new Date(), []);

  const dimmed = (driverId) => selectedDriverId !== null && selectedDriverId !== driverId;

  return (
    <MapContainer center={CENTER} zoom={ZOOM} style={{ height, width: '100%' }} preferCanvas scrollWheelZoom>
      <FocusController point={focusPoint} />
      <TileLayer
        attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
        url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
      />

      {visibleLayers.customers && customers.map((p) => {
        const d = daysSince(p.lastOrderDate, now);
        const color = colorForDays(d, thresholds.t1, thresholds.t2);
        return (
          <CircleMarker
            key={`c-${p.addressId}`}
            data-key={`c-${p.addressId}`}
            center={[p.lat, p.lng]}
            radius={8}
            pathOptions={{
              color,
              fillColor: color,
              fillOpacity: 0.85,
              weight: p.isSharedPlace ? 3 : 1,
              dashArray: p.isSharedPlace ? '3' : undefined,
            }}
          >
            {renderCustomerPopup ? <Popup>{renderCustomerPopup(p, d, color)}</Popup> : null}
          </CircleMarker>
        );
      })}

      {/* Gated on `heatPoints` alone, not `visibleLayers.customers`: the caller
          (CustomerMap) decides when to hand over points — heat mode renders
          independently of the pins layer, matching the pre-extraction map
          where the heat layer was never tied to the pins-view condition. */}
      {heatPoints ? <HeatLayer points={heatPoints} /> : null}

      {visibleLayers.orders && orders
        .filter((o) => o.lat !== null && o.lng !== null)
        .map((o) => (
          <Marker
            key={`o-${o.order_id}`}
            data-kind="order"
            position={[o.lat, o.lng]}
            icon={orderIcon(o)}
            eventHandlers={onSelectStop ? { click: () => onSelectStop(o) } : undefined}
          >
            <Popup>
              <Space direction="vertical" size={2}>
                <Text strong>{o.order_number}</Text>
                <Space size={4} wrap>
                  <Tag color={ORDER_STATUS_COLOR[o.status] ? undefined : 'default'}>{o.status}</Tag>
                  {!o.driver_id && <Tag>{t('delivery:ui.dispatch.unassigned', 'Unassigned')}</Tag>}
                  {o.is_overdue && <Tag color="red">{t('delivery:ui.dispatch.overdue', 'Overdue')}</Tag>}
                  {o.is_cod && <Tag color="gold">COD</Tag>}
                </Space>
                <Text>{o.customer_name}</Text>
                <Text type="secondary" style={{ fontSize: 11 }}>{o.address_label}</Text>
                <Text>{formatMoney(o.total_amount)} UZS</Text>
              </Space>
            </Popup>
          </Marker>
        ))}

      {visibleLayers.drivers && routes.map((route) => {
        const color = driverColor(route.driver_id);
        const geo = geometry[route.driver_id];
        const stopPoints = route.stops
          .filter((s) => s.lat !== null && s.lng !== null)
          .map((s) => [s.lat, s.lng]);
        // The line starts at the route's start location (`start_location_lat/lng`
        // on DeliveryRoute — the depot, NOT NULL on the DB row), not just between
        // stops: otherwise a single-stop route never has two points to draw, and
        // a multi-stop route is missing the depot->first-stop leg the optimiser
        // actually costed into total_distance_km/estimated_duration_minutes.
        const legPoints = [[route.start_lat, route.start_lng], ...stopPoints];
        // A single shared condition for BOTH which points to draw and whether
        // they're a real road path: `geo.geometry` can resolve to `[]` (the
        // geometry endpoint does `result.get("polyline") or result.get("geometry")`,
        // which can plausibly yield an empty list), and `[]` is truthy. Two
        // separate truthiness checks previously let that empty-array case fall
        // through to the straight-leg fallback while still being drawn SOLID —
        // presenting hops between stops as if they were a driveable road.
        const hasRealGeometry = Boolean(geo && geo.geometry && geo.geometry.length);
        const line = hasRealGeometry ? geo.geometry : legPoints;
        if (line.length < 2) return null;
        return (
          <Polyline
            key={`r-${route.driver_id}`}
            data-driver={String(route.driver_id)}
            positions={line}
            pathOptions={{
              color,
              weight: dimmed(route.driver_id) ? 2 : 4,
              opacity: dimmed(route.driver_id) ? 0.25 : 0.9,
              // Dashes are the honest signal that this is not a road path.
              dashArray: hasRealGeometry ? undefined : '6 8',
            }}
          />
        );
      })}

      {visibleLayers.drivers && routes.flatMap((route) => {
        const color = driverColor(route.driver_id);
        if (dimmed(route.driver_id)) return [];
        return route.stops
          .filter((s) => s.lat !== null && s.lng !== null)
          .map((s) => (
            <Marker
              key={`s-${s.delivery_id}`}
              data-kind="stop"
              position={[s.lat, s.lng]}
              icon={stopIcon(s.position, color, s.pinned)}
              eventHandlers={onSelectStop ? { click: () => onSelectStop(s) } : undefined}
            >
              <Popup>
                <Space direction="vertical" size={2}>
                  <Text strong>{`${s.position + 1}. ${s.order_number || ''}`}</Text>
                  <Text>{s.customer_name}</Text>
                  <Text type="secondary" style={{ fontSize: 11 }}>{s.address_label}</Text>
                  {s.pinned && <Tag color="gold">{t('delivery:ui.dispatch.pinned', 'Pinned')}</Tag>}
                </Space>
              </Popup>
            </Marker>
          ));
      })}

      {visibleLayers.drivers && drivers
        .filter((d) => d.lat !== null && d.lat !== undefined && d.lng !== null && d.lng !== undefined)
        .map((d) => (
          <Marker
            key={`d-${d.driver_id}`}
            data-kind="driver"
            position={[d.lat, d.lng]}
            icon={driverIcon(driverColor(d.driver_id))}
            eventHandlers={onSelectDriver ? { click: () => onSelectDriver(d.driver_id) } : undefined}
          >
            <Popup>
              <Space direction="vertical" size={2}>
                <Text strong>{d.full_name}</Text>
                <Text copyable>{d.phone}</Text>
                <Space size={4}>
                  <Tag color={d.location_status === 'fresh' ? 'green' : 'orange'}>{d.location_status}</Tag>
                  <Text type="secondary">
                    {d.active_count} {t('delivery:ui.dispatch.active_stops', 'active stops')}
                  </Text>
                </Space>
              </Space>
            </Popup>
          </Marker>
        ))}
    </MapContainer>
  );
};

export default OperationsMap;
