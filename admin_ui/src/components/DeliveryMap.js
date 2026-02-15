import React, { useMemo } from 'react';
import {
    MapContainer, TileLayer, Marker, Popup, useMap,
} from 'react-leaflet';
import { Card, Tag, Space, Typography, Empty } from 'antd';
import { useQuery } from 'react-query';
import { useTranslation } from 'react-i18next';
import L from 'leaflet';
import 'leaflet/dist/leaflet.css';
import staffService from '../services/staffService';

const { Text } = Typography;

// Fix Leaflet default marker icons
delete L.Icon.Default.prototype._getIconUrl;
L.Icon.Default.mergeOptions({
    iconRetinaUrl: 'https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/images/marker-icon-2x.png',
    iconUrl: 'https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/images/marker-icon.png',
    shadowUrl: 'https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/images/marker-shadow.png',
});

// Custom icon for delivery persons
const deliveryIcon = new L.Icon({
    iconUrl: 'https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/images/marker-icon.png',
    iconRetinaUrl: 'https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/images/marker-icon-2x.png',
    shadowUrl: 'https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/images/marker-shadow.png',
    iconSize: [25, 41],
    iconAnchor: [12, 41],
    popupAnchor: [1, -34],
});

// Tashkent center as default
const DEFAULT_CENTER = [41.2995, 69.2401];
const DEFAULT_ZOOM = 12;

/**
 * Real-time delivery map showing delivery person locations.
 *
 * Props:
 *   height {string|number} - map container height (default: 500)
 *   style {object} - additional styles
 */
const DeliveryMap = ({ height = 500, style = {} }) => {
    const { t } = useTranslation(['staff']);

    // Fetch delivery persons with location data, auto-refresh every 30s
    const { data, isLoading } = useQuery(
        ['deliveryPersonsMap'],
        () => staffService.getDeliveryPersons({ status: 'active', per_page: 100 }),
        { refetchInterval: 30000 }
    );

    const persons = data?.data?.data?.items || [];

    // Filter persons that have location data
    const locatedPersons = useMemo(
        () => persons.filter((p) => p.current_location_lat && p.current_location_lng),
        [persons]
    );

    if (isLoading) {
        return (
            <Card loading style={{ height, ...style }} />
        );
    }

    if (locatedPersons.length === 0) {
        return (
            <Card style={{ height, display: 'flex', alignItems: 'center', justifyContent: 'center', ...style }}>
                <Empty description={t('staff:no_locations_available')} />
            </Card>
        );
    }

    return (
        <Card
            title={t('staff:delivery_map')}
            bodyStyle={{ padding: 0 }}
            style={style}
        >
            <MapContainer
                center={DEFAULT_CENTER}
                zoom={DEFAULT_ZOOM}
                style={{ height, width: '100%' }}
                scrollWheelZoom
            >
                <TileLayer
                    attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
                    url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
                />

                {locatedPersons.map((person) => (
                    <Marker
                        key={person.id}
                        position={[person.current_location_lat, person.current_location_lng]}
                        icon={deliveryIcon}
                    >
                        <Popup>
                            <Space direction="vertical" size="small">
                                <Text strong>{person.full_name}</Text>
                                <Text>{person.phone}</Text>
                                <Space>
                                    <Tag color={person.is_available ? 'green' : 'orange'}>
                                        {person.is_available ? t('staff:available') : t('staff:busy')}
                                    </Tag>
                                    <Text type="secondary">
                                        {person.current_active_deliveries || 0} {t('staff:active_deliveries')}
                                    </Text>
                                </Space>
                                {person.last_location_update && (
                                    <Text type="secondary" style={{ fontSize: 11 }}>
                                        {t('staff:last_update')}: {new Date(person.last_location_update).toLocaleTimeString()}
                                    </Text>
                                )}
                            </Space>
                        </Popup>
                    </Marker>
                ))}
            </MapContainer>
        </Card>
    );
};

export default DeliveryMap;
