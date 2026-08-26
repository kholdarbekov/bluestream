import React from 'react';
import { MapContainer, Marker, TileLayer } from 'react-leaflet';
import { Typography } from 'antd';
import { useTranslation } from 'react-i18next';
import L from 'leaflet';
import 'leaflet/dist/leaflet.css';

// Fix Leaflet default marker icons (same fix as AddressMapPicker.js /
// DeliveryMap.js / OperationsMap.jsx — without it the default marker image
// paths 404 under webpack's bundling).
delete L.Icon.Default.prototype._getIconUrl;
L.Icon.Default.mergeOptions({
  iconRetinaUrl: 'https://unpkg.com/leaflet@1.9.4/dist/images/marker-icon-2x.png',
  iconUrl: 'https://unpkg.com/leaflet@1.9.4/dist/images/marker-icon.png',
  shadowUrl: 'https://unpkg.com/leaflet@1.9.4/dist/images/marker-shadow.png',
});

const { Link, Text } = Typography;

const LocationBubble = ({ message }) => {
  const { t } = useTranslation('common');
  const { latitude, longitude } = message;
  if (latitude == null || longitude == null) return null;

  return (
    <div>
      <MapContainer
        center={[latitude, longitude]}
        zoom={16}
        style={{ height: 160, width: 240, borderRadius: 6 }}
        dragging={false}
        zoomControl={false}
        scrollWheelZoom={false}
        doubleClickZoom={false}
      >
        <TileLayer url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png" />
        <Marker position={[latitude, longitude]} />
      </MapContainer>
      <Text type="secondary" style={{ fontSize: 11 }}>
        {latitude.toFixed(5)}, {longitude.toFixed(5)}
      </Text>
      <br />
      <Link href={`https://yandex.uz/maps/?pt=${longitude},${latitude}&z=17`} target="_blank" rel="noreferrer">
        {t('ui.support.open_in_maps', { defaultValue: 'Open in maps' })}
      </Link>
    </div>
  );
};

export default LocationBubble;
