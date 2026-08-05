import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { describe, it, expect, vi } from 'vitest';

// Mock react-leaflet primitives to simple divs so jsdom can render them.
vi.mock('react-leaflet', () => ({
  MapContainer: ({ children }) => <div data-testid="map">{children}</div>,
  TileLayer: () => <div data-testid="tiles" />,
  // data-weight/data-dash expose the shared-place glyph, which has to be legible
  // WITHOUT opening the popup.
  CircleMarker: ({ children, pathOptions }) => (
    <div data-testid="pin" data-color={pathOptions?.fillColor}
      data-weight={pathOptions?.weight} data-dash={pathOptions?.dashArray ?? ''}>{children}</div>
  ),
  Popup: ({ children }) => <div data-testid="popup">{children}</div>,
  Polygon: () => <div data-testid="polygon" />,
  useMap: () => ({}),
}));
// Serialise the points so a test can assert the intensity CustomerMap actually
// hands the heat layer, not just that a layer rendered.
vi.mock('./HeatLayer', () => ({
  default: ({ points }) => <div data-testid="heat" data-points={JSON.stringify(points)} />,
}));

// --- Pin fixture contract --------------------------------------------------
// The pins below are hand-written camelCase, and camelCase is NOT what the
// service produces: `CustomerMapService.get_customer_map_pins` returns
// snake_case and the aliases are minted at the last moment by
// `CustomerMapPinSchema` (`alias_generator=to_camel`) plus the route's
// `model_dump(by_alias=True)`. Two guards, the same split PlaceGroupPanel.test.jsx
// uses:
//   1. `mapPin` rejects a fixture that disagrees with MAP_PIN_KEYS — no invented
//      key, and no MISSING key either, since a rename lands as "the old key is
//      gone" and an extras-only validator is blind to exactly that; and
//   2. MAP_PIN_KEYS is itself pinned against the LIVE `GET /admin/customers/map-pins`
//      response by tests/unit/test_admin_ui_payload_fixture_contracts.py
//      (`test_customer_map_pin_key_set_matches_the_live_route`), which parses this
//      set straight out of this file. Drop `by_alias=True` and that guard goes red,
//      naming this file — without it, the set and the fixture would go stale
//      together and agree with each other perfectly.
// Declared via vi.hoisted because vi.mock's factory is hoisted above module-scope
// consts and would otherwise hit the TDZ.
const { mapPin } = vi.hoisted(() => {
  const MAP_PIN_KEYS = new Set([
    'addressId', 'userId', 'fullName', 'phone', 'userType', 'entitySubtype',
    'lat', 'lng', 'isDefault', 'addressLabel', 'addressIndex', 'addressCount',
    'lastOrderDate', 'orderCount', 'bottleBalance', 'outstandingDebt',
    'activeCodDebtCount', 'codRestricted',
    // The place axis. `CustomerMap.js` reads `isSharedPlace` for the badge and
    // the dashed glyph; `customerMapLogic.js` divides the heat weight by
    // `placeMemberCount`, and a missing key silently falls back to a divisor of 1.
    'isSharedPlace', 'placeMemberCount',
  ]);

  const where = 'See CustomerMapPinSchema in business_app/serializers/admin_serializers.py.';
  return {
    mapPin: (pin) => {
      const invented = Object.keys(pin).filter((key) => !MAP_PIN_KEYS.has(key));
      if (invented.length) {
        throw new Error(
          `Customer-map pin fixture invents keys the backend never emits: ${invented.join(', ')}. ${where}`
        );
      }
      const missing = [...MAP_PIN_KEYS].filter((key) => !(key in pin));
      if (missing.length) {
        throw new Error(
          `Customer-map pin fixture is missing keys the backend always emits: ${missing.join(', ')}. ${where}`
        );
      }
      return pin;
    },
  };
});

vi.mock('../services/adminService', () => ({
  default: {
    getCustomerMapPins: vi.fn().mockResolvedValue({
      data: { pins: [
        mapPin({ addressId: 1, userId: 1, fullName: 'Recent Guy', phone: '+998900000001',
          userType: 'individual', entitySubtype: null, lat: 41.31, lng: 69.28,
          isDefault: true, addressLabel: 'A1', addressIndex: 1, addressCount: 1,
          lastOrderDate: new Date().toISOString(), orderCount: 3,
          bottleBalance: 2, outstandingDebt: 0, activeCodDebtCount: 0, codRestricted: false,
          isSharedPlace: false, placeMemberCount: 1 }),
        mapPin({ addressId: 2, userId: 2, fullName: 'Idle Guy', phone: '+998900000002',
          userType: 'entity', entitySubtype: 'grocery_store', lat: 41.32, lng: 69.29,
          isDefault: true, addressLabel: 'A2', addressIndex: 1, addressCount: 1,
          lastOrderDate: '2026-01-01T00:00:00Z', orderCount: 1,
          bottleBalance: 0, outstandingDebt: 9000, activeCodDebtCount: 1, codRestricted: false,
          isSharedPlace: false, placeMemberCount: 1 }),
        // Coincident with pin 1 (same lat/lng): a coworker at a shared workplace.
        // `bottleBalance` is the PLACE pool, so this 7 is the same 7 the other two
        // members of the group report — it must not read as a third 7.
        mapPin({ addressId: 3, userId: 3, fullName: 'Shared Office Coworker', phone: '+998900000003',
          userType: 'entity', entitySubtype: 'workplace', lat: 41.31, lng: 69.28,
          isDefault: true, addressLabel: 'A3', addressIndex: 1, addressCount: 1,
          lastOrderDate: new Date().toISOString(), orderCount: 5,
          bottleBalance: 7, outstandingDebt: 0, activeCodDebtCount: 0, codRestricted: false,
          isSharedPlace: true, placeMemberCount: 3 }),
      ] },
    }),
  },
}));

import adminService from '../services/adminService';
import CustomerMap from './CustomerMap';

function renderMap(props = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}><CustomerMap {...props} /></QueryClientProvider>
  );
}

describe('CustomerMap', () => {
  it('renders one pin per address from the endpoint', async () => {
    renderMap();
    await waitFor(() => expect(screen.getAllByTestId('pin')).toHaveLength(3));
    expect(adminService.getCustomerMapPins).toHaveBeenCalled();
  });

  it('idle-filter reduces the rendered pins', async () => {
    renderMap();
    await waitFor(() => expect(screen.getAllByTestId('pin')).toHaveLength(3));
    // Idle >= 60 days: only the Jan pin remains.
    fireEvent.change(screen.getByLabelText('idle minimum days'), { target: { value: '60' } });
    await waitFor(() => expect(screen.getAllByTestId('pin')).toHaveLength(1));
  });

  it('"View full profile" calls onViewUser with the userId', async () => {
    const onViewUser = vi.fn();
    renderMap({ onViewUser });
    await waitFor(() => expect(screen.getAllByTestId('pin')).toHaveLength(3));
    fireEvent.click(screen.getAllByText(/view full profile/i)[0]);
    expect(onViewUser).toHaveBeenCalledWith(1);
  });

  it('switching to Heatmap renders the heat layer', async () => {
    renderMap();
    await waitFor(() => expect(screen.getAllByTestId('pin')).toHaveLength(3));
    fireEvent.click(screen.getByRole('radio', { name: /heatmap/i }));
    await waitFor(() => expect(screen.getByTestId('heat')).toBeInTheDocument());
  });

  it('a shared-place pin is badged so coincident pins do not read as independent totals', async () => {
    renderMap();
    await waitFor(() => expect(screen.getAllByTestId('pin')).toHaveLength(3));
    expect(await screen.findByText(/shared place/i)).toBeInTheDocument();
    // Exactly one of the three pins is at a shared place: a badge on every popup
    // would be just as misleading as no badge at all.
    expect(screen.getAllByText(/shared place/i)).toHaveLength(1);
  });

  it('draws the shared-place pin with a thicker dashed outline, legible without the popup', async () => {
    renderMap();
    await waitFor(() => expect(screen.getAllByTestId('pin')).toHaveLength(3));
    const pins = screen.getAllByTestId('pin');
    expect(pins.map((el) => el.getAttribute('data-weight'))).toEqual(['1', '1', '3']);
    expect(pins.map((el) => el.getAttribute('data-dash'))).toEqual(['', '', '3']);
  });

  it('weights the heat layer by place, so one office is not counted thrice', async () => {
    renderMap();
    await waitFor(() => expect(screen.getAllByTestId('pin')).toHaveLength(3));
    fireEvent.click(screen.getByRole('radio', { name: /heatmap/i }));
    await waitFor(() => expect(screen.getByTestId('heat')).toBeInTheDocument());
    const points = JSON.parse(screen.getByTestId('heat').getAttribute('data-points'));
    expect(points).toHaveLength(3);
    expect(points[0]).toEqual([41.31, 69.28, 1]);   // solo place: one unit
    expect(points[1]).toEqual([41.32, 69.29, 1]);   // solo place with NO bottles: still one unit
    expect(points[2][2]).toBeCloseTo(1 / 3);        // shared place: 3 pins sharing one unit
    // The three coincident pins of the office sum to one solo customer's heat.
    expect(points[2][2] * 3).toBeCloseTo(points[0][2]);
  });

  it('leaves the zero-balance customer visible on the heat layer', async () => {
    // "Idle Guy" holds 0 bottles and 9,000 UZS of debt — exactly the segment an
    // admin opens the heatmap for. Weighting by bottle balance emitted him at
    // intensity 0, which leaflet.heat neither accumulates nor draws once two of
    // them share a grid cell (0/0 -> NaN centre). No pin may ever be 0.
    renderMap();
    await waitFor(() => expect(screen.getAllByTestId('pin')).toHaveLength(3));
    fireEvent.click(screen.getByRole('radio', { name: /heatmap/i }));
    await waitFor(() => expect(screen.getByTestId('heat')).toBeInTheDocument());
    const points = JSON.parse(screen.getByTestId('heat').getAttribute('data-points'));
    const zeroBalancePin = points[1];
    expect(zeroBalancePin[2]).toBeGreaterThan(0);
    for (const [, , intensity] of points) {
      expect(Number.isFinite(intensity)).toBe(true);
      expect(intensity).toBeGreaterThan(0);
    }
  });
});
