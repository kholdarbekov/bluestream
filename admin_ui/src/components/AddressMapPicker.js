import React, { useState, useEffect, useRef, useCallback } from 'react';
import { MapContainer, TileLayer, Marker, useMapEvents, Rectangle, useMap } from 'react-leaflet';
import { Button, Input, Space, Spin, message, Typography } from 'antd';
import { AimOutlined, SearchOutlined, EnvironmentOutlined } from '@ant-design/icons';
import L from 'leaflet';
import 'leaflet/dist/leaflet.css';
import api from '../services/api';

// Fix Leaflet default icon issue
delete L.Icon.Default.prototype._getIconUrl;
L.Icon.Default.mergeOptions({
  iconRetinaUrl: 'https://unpkg.com/leaflet@1.9.4/dist/images/marker-icon-2x.png',
  iconUrl: 'https://unpkg.com/leaflet@1.9.4/dist/images/marker-icon.png',
  shadowUrl: 'https://unpkg.com/leaflet@1.9.4/dist/images/marker-shadow.png',
});

const { Text } = Typography;

// Component to handle map click events
const MapClickHandler = ({ onLocationSelect, bounds }) => {
  useMapEvents({
    click: (e) => {
      const { lat, lng } = e.latlng;

      // Check if within bounds
      if (bounds &&
          (lat < bounds.min_lat || lat > bounds.max_lat ||
           lng < bounds.min_lng || lng > bounds.max_lng)) {
        message.warning('Selected location is outside the delivery area');
        return;
      }

      onLocationSelect(lat, lng);
    },
  });
  return null;
};

// Component to recenter map when position changes
const MapRecenter = ({ position }) => {
  const map = useMap();

  useEffect(() => {
    if (position) {
      map.setView(position, 16, { animate: true });
    }
  }, [position, map]);

  return null;
};

const AddressMapPicker = ({
  value,
  onChange,
  onAddressFound,
  style,
  height = 300
}) => {
  const [geoConfig, setGeoConfig] = useState(null);
  const [loading, setLoading] = useState(true);
  const [searchLoading, setSearchLoading] = useState(false);
  const [locationLoading, setLocationLoading] = useState(false);
  const [searchAddress, setSearchAddress] = useState('');
  const [position, setPosition] = useState(null);
  const [statusMessage, setStatusMessage] = useState('');
  const mapRef = useRef(null);

  // Load geo configuration
  useEffect(() => {
    const fetchGeoConfig = async () => {
      try {
        const lang = localStorage.getItem('language') || 'en';
        const response = await api.get(`/addresses/geo-config?lang=${lang}`);

        if (response.data?.success && response.data?.data) {
          setGeoConfig(response.data.data);

          // Set initial position from value or config center
          if (value?.latitude && value?.longitude) {
            setPosition([value.latitude, value.longitude]);
          }
        }
      } catch (error) {
        console.error('Failed to load geo config:', error);
        message.error('Failed to load map configuration');
      } finally {
        setLoading(false);
      }
    };

    fetchGeoConfig();
  }, []);

  // Update position when value changes externally
  useEffect(() => {
    if (value?.latitude && value?.longitude) {
      setPosition([value.latitude, value.longitude]);
    }
  }, [value?.latitude, value?.longitude]);

  // Handle location selection
  const handleLocationSelect = useCallback(async (lat, lng) => {
    setPosition([lat, lng]);
    setStatusMessage('Getting address...');

    // Notify parent of coordinate change
    onChange?.({ latitude: lat, longitude: lng });

    // Reverse geocode to get address
    try {
      const response = await api.post('/addresses/reverse-geocode', {
        latitude: lat,
        longitude: lng
      });

      if (response.data?.success && response.data?.data) {
        setStatusMessage('Location selected!');
        onAddressFound?.({
          formatted_address: response.data.data.formatted_address,
          district: response.data.data.district,
          latitude: lat,
          longitude: lng
        });
      } else {
        setStatusMessage('Location selected (address not found)');
      }
    } catch (error) {
      console.error('Reverse geocode failed:', error);
      setStatusMessage('Location selected');
    }

    // Clear status after delay
    setTimeout(() => setStatusMessage(''), 3000);
  }, [onChange, onAddressFound]);

  // Use current location
  const handleUseMyLocation = useCallback(() => {
    if (!navigator.geolocation) {
      message.error('Geolocation is not supported by your browser');
      return;
    }

    setLocationLoading(true);

    navigator.geolocation.getCurrentPosition(
      (pos) => {
        const { latitude, longitude } = pos.coords;

        // Check bounds
        if (geoConfig?.bounds) {
          const bounds = geoConfig.bounds;
          if (latitude < bounds.min_lat || latitude > bounds.max_lat ||
              longitude < bounds.min_lng || longitude > bounds.max_lng) {
            message.warning('Your location is outside the delivery area');
            setLocationLoading(false);
            return;
          }
        }

        handleLocationSelect(latitude, longitude);
        setLocationLoading(false);
      },
      (error) => {
        console.error('Geolocation error:', error);
        if (error.code === error.PERMISSION_DENIED) {
          message.error('Location access denied. Please enable location or select on map.');
        } else {
          message.error('Could not get your location');
        }
        setLocationLoading(false);
      },
      { enableHighAccuracy: true, timeout: 10000, maximumAge: 0 }
    );
  }, [geoConfig, handleLocationSelect]);

  // Search address
  const handleSearchAddress = useCallback(async () => {
    if (!searchAddress.trim()) {
      message.warning('Please enter an address to search');
      return;
    }

    setSearchLoading(true);

    try {
      const response = await api.post('/addresses/geocode', {
        address: searchAddress + ', Tashkent'
      });

      const result = response.data;

      if (result?.success && result?.data?.latitude && result?.data?.longitude) {
        const { latitude, longitude } = result.data;

        // Check bounds
        if (geoConfig?.bounds) {
          const bounds = geoConfig.bounds;
          if (latitude < bounds.min_lat || latitude > bounds.max_lat ||
              longitude < bounds.min_lng || longitude > bounds.max_lng) {
            message.warning('Address is outside the delivery area');
            setSearchLoading(false);
            return;
          }
        }

        handleLocationSelect(latitude, longitude);

        if (result.data.formatted_address) {
          onAddressFound?.({
            formatted_address: result.data.formatted_address,
            latitude,
            longitude
          });
        }
      } else {
        message.warning('Address not found. Try a different search.');
      }
    } catch (error) {
      console.error('Address search failed:', error);
      message.error('Search failed. Please try again.');
    } finally {
      setSearchLoading(false);
    }
  }, [searchAddress, geoConfig, handleLocationSelect, onAddressFound]);

  if (loading) {
    return (
      <div style={{
        height,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        background: '#f5f5f5',
        borderRadius: 8
      }}>
        <Spin tip="Loading map..." />
      </div>
    );
  }

  if (!geoConfig) {
    return (
      <div style={{
        height,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        background: '#f5f5f5',
        borderRadius: 8,
        color: '#999'
      }}>
        Failed to load map
      </div>
    );
  }

  const center = position || [geoConfig.center.latitude, geoConfig.center.longitude];
  const bounds = geoConfig.bounds;
  const boundaryCoords = bounds ? [
    [bounds.min_lat, bounds.min_lng],
    [bounds.max_lat, bounds.max_lng]
  ] : null;

  return (
    <div style={style}>
      {/* Search controls */}
      <Space.Compact style={{ width: '100%', marginBottom: 8 }}>
        <Input
          placeholder="Search address..."
          value={searchAddress}
          onChange={(e) => setSearchAddress(e.target.value)}
          onPressEnter={handleSearchAddress}
          style={{ flex: 1 }}
          prefix={<EnvironmentOutlined style={{ color: '#999' }} />}
        />
        <Button
          icon={<SearchOutlined />}
          onClick={handleSearchAddress}
          loading={searchLoading}
        >
          Search
        </Button>
        <Button
          icon={<AimOutlined />}
          onClick={handleUseMyLocation}
          loading={locationLoading}
          title="Use my location"
        />
      </Space.Compact>

      {/* Map container */}
      <div style={{
        height,
        borderRadius: 8,
        overflow: 'hidden',
        border: '1px solid #d9d9d9'
      }}>
        <MapContainer
          ref={mapRef}
          center={center}
          zoom={position ? 16 : 12}
          style={{ height: '100%', width: '100%' }}
          scrollWheelZoom={true}
        >
          <TileLayer
            attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
            url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
          />

          {/* Show delivery area boundary */}
          {boundaryCoords && (
            <Rectangle
              bounds={boundaryCoords}
              pathOptions={{
                color: '#1890ff',
                weight: 2,
                opacity: 0.5,
                fillOpacity: 0.05,
                dashArray: '5, 10'
              }}
            />
          )}

          {/* Marker for selected position */}
          {position && (
            <Marker
              position={position}
              draggable={true}
              eventHandlers={{
                dragend: (e) => {
                  const marker = e.target;
                  const pos = marker.getLatLng();

                  // Check bounds
                  if (bounds &&
                      (pos.lat < bounds.min_lat || pos.lat > bounds.max_lat ||
                       pos.lng < bounds.min_lng || pos.lng > bounds.max_lng)) {
                    // Reset to previous position
                    marker.setLatLng(position);
                    message.warning('Cannot move marker outside the delivery area');
                    return;
                  }

                  handleLocationSelect(pos.lat, pos.lng);
                }
              }}
            />
          )}

          {/* Handle map clicks */}
          <MapClickHandler
            onLocationSelect={handleLocationSelect}
            bounds={bounds}
          />

          {/* Recenter when position changes */}
          <MapRecenter position={position} />
        </MapContainer>
      </div>

      {/* Status message */}
      {statusMessage && (
        <div style={{ marginTop: 8, textAlign: 'center' }}>
          <Text type="secondary">{statusMessage}</Text>
        </div>
      )}

      {/* Coordinates display */}
      {position && (
        <div style={{ marginTop: 8, textAlign: 'center' }}>
          <Text type="secondary" style={{ fontSize: 12 }}>
            Lat: {position[0].toFixed(6)}, Lng: {position[1].toFixed(6)}
          </Text>
        </div>
      )}

      {/* Instructions */}
      {!position && (
        <div style={{ marginTop: 8, textAlign: 'center' }}>
          <Text type="secondary" style={{ fontSize: 12 }}>
            Click on the map to select delivery location, or search/use your location
          </Text>
        </div>
      )}
    </div>
  );
};

export default AddressMapPicker;
