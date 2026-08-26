import React from 'react';
import { render, screen } from '@testing-library/react';

import LocationBubble from '../../../components/support/LocationBubble';

// react-leaflet needs a real DOM canvas we don't have; stub it to plain nodes
// so the assertions are about what LocationBubble hands the map, not
// Leaflet's own rendering (same pattern as AddressMapPicker.test.jsx /
// CustomerMap.test.jsx / OperationsMap.test.jsx). Do NOT strip the map out of
// the component itself to make this pass.
vi.mock('react-leaflet', () => ({
  MapContainer: ({ children, center }) => (
    <div data-testid="map" data-center={JSON.stringify(center)}>{children}</div>
  ),
  TileLayer: () => <div data-testid="tiles" />,
  Marker: ({ position }) => <div data-testid="marker" data-position={JSON.stringify(position)} />,
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key, opts) => (opts && opts.defaultValue) || (typeof opts === 'string' ? opts : key) }),
}));

it('renders the map centered on the pin and shows the coordinates', () => {
  render(<LocationBubble message={{ latitude: 41.32354, longitude: 69.241036 }} />);

  expect(screen.getByTestId('map')).toHaveAttribute('data-center', JSON.stringify([41.32354, 69.241036]));
  expect(screen.getByTestId('marker')).toHaveAttribute('data-position', JSON.stringify([41.32354, 69.241036]));
  expect(screen.getByText('41.32354, 69.24104')).toBeInTheDocument();
});

it('links "open in maps" to the exact lat/lng, longitude first as Yandex expects', () => {
  render(<LocationBubble message={{ latitude: 41.32354, longitude: 69.241036 }} />);

  expect(screen.getByRole('link', { name: /open in maps/i })).toHaveAttribute(
    'href',
    'https://yandex.uz/maps/?pt=69.241036,41.32354&z=17',
  );
});

it('renders nothing when the message carries no coordinates', () => {
  const { container } = render(<LocationBubble message={{ latitude: null, longitude: null }} />);
  expect(container).toBeEmptyDOMElement();
});
