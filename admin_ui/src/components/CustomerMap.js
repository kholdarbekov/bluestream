import React, { useMemo, useState } from 'react';
import { MapContainer, TileLayer, CircleMarker, Popup } from 'react-leaflet';
import 'leaflet/dist/leaflet.css';
import {
  Card, Space, InputNumber, Segmented, Checkbox, Button, Tag, Typography, Empty, Spin,
} from 'antd';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import adminService from '../services/adminService';
import HeatLayer from './HeatLayer';
import {
  daysSince, colorForDays, loadThresholds, saveThresholds, applyFilters,
} from '../utils/customerMapLogic';
import { formatMoney } from '../utils/formatMoney';

const { Text } = Typography;
const CENTER = [41.2995, 69.2401];
const ZOOM = 11;

// Same translation keys as this component's type filter (Segmented options above);
// defensive fallback to the raw value if an unknown enum slips through.
const userTypeLabel = (tt, userType) => {
  if (userType === 'individual') return tt('ui.users.map.type_individual', 'Individual');
  if (userType === 'entity') return tt('ui.users.map.type_entity', 'Entity');
  return userType;
};

// Same keys/labels as getEntitySubtypeMeta in Users.js, so the map stays
// consistent with the rest of the admin UI.
const entitySubtypeLabel = (tt, subtype) => {
  if (subtype === 'workplace') return tt('ui.users.entity_subtype_workplace', 'Workplace');
  if (subtype === 'grocery_store') return tt('ui.users.entity_subtype_grocery_store', 'Grocery Store');
  return subtype;
};

const CustomerMap = ({ onViewUser, height = 640 }) => {
  const { t } = useTranslation('users');
  const { data, isLoading } = useQuery({
    queryKey: ['customerMapPins'],
    queryFn: () => adminService.getCustomerMapPins(),
    staleTime: 5 * 60 * 1000,
  });
  const pins = data?.data?.pins || [];

  const [{ t1, t2 }, setThresholds] = useState(loadThresholds());
  const [viewMode, setViewMode] = useState('pins'); // 'pins' | 'heat'
  const [heatOverlay, setHeatOverlay] = useState(false);
  const [idleMinDays, setIdleMinDays] = useState(0);
  const [bottleOnly, setBottleOnly] = useState(false);
  const [debtOnly, setDebtOnly] = useState(false);
  const [type, setType] = useState('all');

  const now = new Date();
  const visible = useMemo(
    () => applyFilters(pins, { idleMinDays, bottleOnly, debtOnly, type }, now),
    [pins, idleMinDays, bottleOnly, debtOnly, type], // eslint-disable-line react-hooks/exhaustive-deps
  );
  const heatPoints = useMemo(() => visible.map((p) => [p.lat, p.lng, 1]), [visible]);

  const updateThreshold = (next) => {
    const clean = { t1: next.t1 ?? t1, t2: next.t2 ?? t2 };
    if (clean.t1 >= clean.t2) return;
    setThresholds(clean);
    saveThresholds(clean);
  };

  return (
    <Card styles={{ body: { padding: 12 } }}>
      <Space wrap size="middle" style={{ marginBottom: 12 }}>
        <Segmented
          options={[
            { label: t('ui.users.map.view_pins', 'Pins'), value: 'pins' },
            { label: t('ui.users.map.view_heat', 'Heatmap'), value: 'heat' },
          ]}
          value={viewMode}
          onChange={setViewMode}
        />
        {viewMode === 'pins' && (
          <Checkbox checked={heatOverlay} onChange={(e) => setHeatOverlay(e.target.checked)}>
            {t('ui.users.map.heat_overlay', 'Heat overlay')}
          </Checkbox>
        )}
        <Space>
          <Text>{t('ui.users.map.fresh_within', 'Fresh ≤')}</Text>
          <InputNumber min={1} max={t2 - 1} value={t1} onChange={(v) => updateThreshold({ t1: v })} />
          <Text>{t('ui.users.map.idle_after', 'Idle ≥')}</Text>
          <InputNumber min={t1 + 1} value={t2} onChange={(v) => updateThreshold({ t2: v })} />
          <Text type="secondary">{t('ui.users.map.days', 'days')}</Text>
        </Space>
        <Space>
          {/* Single accessible name via aria-label (no htmlFor) so the test's
              getByLabelText('idle minimum days') resolves one element. */}
          <Text>{t('ui.users.map.idle_min', 'Idle ≥ (filter)')}</Text>
          <InputNumber aria-label="idle minimum days"
            min={0} value={idleMinDays} onChange={(v) => setIdleMinDays(v || 0)} />
        </Space>
        <Checkbox checked={bottleOnly} onChange={(e) => setBottleOnly(e.target.checked)}>
          {t('ui.users.map.filter_bottles', 'Has bottles')}
        </Checkbox>
        <Checkbox checked={debtOnly} onChange={(e) => setDebtOnly(e.target.checked)}>
          {t('ui.users.map.filter_debt', 'Has debt')}
        </Checkbox>
        <Segmented
          options={[
            { label: t('ui.users.map.type_all', 'All'), value: 'all' },
            { label: t('ui.users.map.type_individual', 'Individual'), value: 'individual' },
            { label: t('ui.users.map.type_entity', 'Entity'), value: 'entity' },
          ]}
          value={type}
          onChange={setType}
        />
      </Space>

      {/* Gradient legend */}
      <div style={{ marginBottom: 8 }}>
        <div style={{
          height: 10, borderRadius: 5,
          background: 'linear-gradient(90deg, hsl(120,70%,45%), hsl(60,70%,45%), hsl(0,70%,45%))',
        }} />
        <Space style={{ justifyContent: 'space-between', width: '100%' }}>
          <Text type="secondary">{t('ui.users.map.legend_recent', 'Recent')} (≤{t1}d)</Text>
          <Text type="secondary">{t('ui.users.map.legend_idle', 'Idle')} (≥{t2}d)</Text>
        </Space>
        <Text type="secondary">
          {t('ui.users.map.showing', 'Showing')} {visible.length}/{pins.length}
        </Text>
      </div>

      {isLoading ? (
        <div style={{ height, display: 'flex', alignItems: 'center', justifyContent: 'center' }}><Spin /></div>
      ) : pins.length === 0 ? (
        <Empty description={t('ui.users.map.empty', 'No customers to display')} />
      ) : (
        <MapContainer center={CENTER} zoom={ZOOM} style={{ height, width: '100%' }} preferCanvas scrollWheelZoom>
          <TileLayer
            attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
            url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
          />
          {viewMode === 'pins' && visible.map((p) => {
            const d = daysSince(p.lastOrderDate, now);
            const color = colorForDays(d, t1, t2);
            return (
              <CircleMarker
                key={p.addressId}
                center={[p.lat, p.lng]}
                radius={8}
                pathOptions={{ color, fillColor: color, fillOpacity: 0.85, weight: 1 }}
              >
                <Popup>
                  <Space direction="vertical" size={2}>
                    <Text strong>{p.fullName}</Text>
                    <Space size={4}>
                      <Tag>
                        {userTypeLabel(t, p.userType)}
                        {p.entitySubtype ? ` · ${entitySubtypeLabel(t, p.entitySubtype)}` : ''}
                      </Tag>
                      {p.codRestricted && <Tag color="red">{t('ui.users.map.cod_restricted', 'COD restricted')}</Tag>}
                    </Space>
                    <Text copyable>{p.phone}</Text>
                    <Text>
                      {t('ui.users.map.last_order', 'Last order')}:{' '}
                      {d === null ? '—' : `${d} ${t('ui.users.map.days_ago', 'days ago')}`}
                      <span style={{ display: 'inline-block', width: 10, height: 10, marginLeft: 6,
                        borderRadius: 5, background: color }} />
                    </Text>
                    {p.lastOrderDate && (
                      <Text type="secondary" style={{ fontSize: 11 }}>
                        {new Date(p.lastOrderDate).toLocaleDateString()} · {p.orderCount} {t('ui.users.map.orders', 'orders')}
                      </Text>
                    )}
                    <Text>
                      {t('ui.users.map.bottles', 'Bottles')}: {p.bottleBalance}
                      {p.addressCount > 1 && (
                        <Text type="secondary"> ({t('ui.users.map.address', 'address')} {p.addressIndex}/{p.addressCount})</Text>
                      )}
                    </Text>
                    <Text>{t('ui.users.map.debt', 'Debt')}: {formatMoney(p.outstandingDebt)} UZS</Text>
                    <Button type="link" size="small" style={{ padding: 0 }}
                      onClick={() => onViewUser && onViewUser(p.userId)}>
                      {t('ui.users.map.view_profile', 'View full profile')}
                    </Button>
                  </Space>
                </Popup>
              </CircleMarker>
            );
          })}
          {(viewMode === 'heat' || (viewMode === 'pins' && heatOverlay)) && (
            <HeatLayer points={heatPoints} />
          )}
        </MapContainer>
      )}
    </Card>
  );
};

export default CustomerMap;
