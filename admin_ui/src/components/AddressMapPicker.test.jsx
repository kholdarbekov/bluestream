import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi, beforeEach } from 'vitest';

// react-leaflet needs a real DOM canvas we don't have; stub it to plain nodes so
// the assertions are about which API calls the search box makes, not Leaflet.
vi.mock('react-leaflet', () => ({
  MapContainer: React.forwardRef(({ children }, ref) => (
    <div data-testid="map" ref={ref}>{children}</div>
  )),
  TileLayer: () => null,
  Polygon: () => null,
  Marker: ({ position }) => (
    <div data-testid="marker" data-position={JSON.stringify(position)} />
  ),
  useMapEvents: () => ({}),
  useMap: () => ({
    setView: vi.fn(),
    invalidateSize: vi.fn(),
    getZoom: () => 16,
  }),
}));

vi.mock('../services/api', () => ({
  __esModule: true,
  default: { get: vi.fn(), post: vi.fn() },
}));

vi.mock('antd', async () => {
  const actual = await vi.importActual('antd');
  return {
    ...actual,
    message: { warning: vi.fn(), error: vi.fn(), success: vi.fn(), info: vi.fn() },
  };
});

import { message } from 'antd';
import api from '../services/api';
import AddressMapPicker from './AddressMapPicker';

// A square around Tashkent: (41.323396, 69.193840) is inside, (41.9, 69.9) is not.
const POLYGON = [
  [41.2, 69.1],
  [41.2, 69.3],
  [41.4, 69.3],
  [41.4, 69.1],
];

const GEO_CONFIG = {
  data: {
    success: true,
    data: {
      center: { latitude: 41.311081, longitude: 69.240562 },
      polygon: POLYGON,
    },
  },
};

const REVERSE_GEOCODE = {
  data: {
    success: true,
    data: { formatted_address: 'Chilonzor 12', district: 'Chilonzor' },
  },
};

const renderPicker = async (props = {}) => {
  const result = render(<AddressMapPicker {...props} />);
  await screen.findByTestId('map');
  return result;
};

const searchFor = async (text) => {
  const user = userEvent.setup();
  await user.clear(screen.getByPlaceholderText(/search address/i));
  await user.type(screen.getByPlaceholderText(/search address/i), text);
  await user.click(screen.getByRole('button', { name: /search/i }));
};

const postedTo = (path) => api.post.mock.calls.filter(([url]) => url === path);

describe('AddressMapPicker coordinate search', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.get.mockResolvedValue(GEO_CONFIG);
    api.post.mockResolvedValue(REVERSE_GEOCODE);
  });

  it('pins a pasted "lat, lng" pair without calling the address geocoder', async () => {
    const onChange = vi.fn();
    await renderPicker({ onChange });

    await searchFor('41.323396, 69.193840');

    await waitFor(() => expect(postedTo('/addresses/reverse-geocode')).toHaveLength(1));
    expect(postedTo('/addresses/reverse-geocode')[0][1]).toEqual({
      latitude: 41.323396,
      longitude: 69.19384,
    });
    expect(postedTo('/addresses/geocode')).toHaveLength(0);
    expect(onChange).toHaveBeenCalledWith({ latitude: 41.323396, longitude: 69.19384 });
  });

  it('drops the marker on the pasted coordinates', async () => {
    await renderPicker();

    await searchFor('41.323396, 69.193840');

    const marker = await screen.findByTestId('marker');
    expect(JSON.parse(marker.dataset.position)).toEqual([41.323396, 69.19384]);
  });

  it('fills the address form from the reverse-geocoded pasted pair', async () => {
    const onAddressFound = vi.fn();
    await renderPicker({ onAddressFound });

    await searchFor('41.323396, 69.193840');

    await waitFor(() => expect(onAddressFound).toHaveBeenCalledWith({
      formatted_address: 'Chilonzor 12',
      district: 'Chilonzor',
      latitude: 41.323396,
      longitude: 69.19384,
    }));
  });

  it('accepts the "Lat: x, Lng: y" line printed under the map', async () => {
    await renderPicker();

    await searchFor('Lat: 41.323396, Lng: 69.193840');

    await waitFor(() => expect(postedTo('/addresses/reverse-geocode')).toHaveLength(1));
    expect(postedTo('/addresses/reverse-geocode')[0][1]).toEqual({
      latitude: 41.323396,
      longitude: 69.19384,
    });
  });

  it('rejects coordinates outside the delivery area instead of pinning them', async () => {
    const onChange = vi.fn();
    await renderPicker({ onChange });

    await searchFor('41.9, 69.9');

    await waitFor(() => expect(message.warning)
      .toHaveBeenCalledWith('Coordinates are outside the delivery area'));
    expect(api.post).not.toHaveBeenCalled();
    expect(onChange).not.toHaveBeenCalled();
    expect(screen.queryByTestId('marker')).not.toBeInTheDocument();
  });

  it('still geocodes a plain address search', async () => {
    api.post.mockResolvedValue({
      data: {
        success: true,
        data: { latitude: 41.32, longitude: 69.19, formatted_address: 'Chilonzor 12' },
      },
    });
    await renderPicker();

    await searchFor('Chilonzor 12');

    await waitFor(() => expect(postedTo('/addresses/geocode')).toHaveLength(1));
    expect(postedTo('/addresses/geocode')[0][1]).toEqual({ address: 'Chilonzor 12, Tashkent' });
  });

  it('geocodes an address containing numbers rather than reading it as coordinates', async () => {
    api.post.mockResolvedValue({
      data: {
        success: true,
        data: { latitude: 41.32, longitude: 69.19, formatted_address: 'Chilonzor 12' },
      },
    });
    await renderPicker();

    await searchFor('Chilonzor 12, 34');

    await waitFor(() => expect(postedTo('/addresses/geocode')).toHaveLength(1));
    expect(postedTo('/addresses/geocode')[0][1]).toEqual({ address: 'Chilonzor 12, 34, Tashkent' });
    // The pin must come from the geocoder, never from reading "12, 34" as a pair.
    await waitFor(() => expect(postedTo('/addresses/reverse-geocode')).toHaveLength(1));
    expect(postedTo('/addresses/reverse-geocode')[0][1]).toEqual({
      latitude: 41.32,
      longitude: 69.19,
    });
  });
});
