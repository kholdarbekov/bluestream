import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
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
//   1. `mapPin` rejects a fixture that disagrees with MAP_PIN_KEYS (and any
//      `members[]` row that disagrees with MAP_PLACE_MEMBER_KEYS) — no invented
//      key, and no MISSING key either, since a rename lands as "the old key is
//      gone" and an extras-only validator is blind to exactly that; and
//   2. both sets are themselves pinned against the LIVE `GET /admin/customers/map-pins`
//      response by tests/unit/test_admin_ui_payload_fixture_contracts.py
//      (`test_customer_map_pin_key_set_matches_the_live_route`), which parses them
//      straight out of this file. Drop `by_alias=True` and that guard goes red,
//      naming this file — without it, the set and the fixture would go stale
//      together and agree with each other perfectly.
// Declared via vi.hoisted because vi.mock's factory is hoisted above module-scope
// consts and would otherwise hit the TDZ.
const { mapPin } = vi.hoisted(() => {
  // Both pin kinds — an ungrouped address (`kind: 'address'`) and a shared place
  // (`kind: 'place'`) — carry EVERY key; a place pin has no single owner
  // (`userId: null`) and lists its owners under `members`.
  const MAP_PIN_KEYS = new Set([
    'kind', 'addressId', 'userId', 'fullName', 'phone', 'userType', 'entitySubtype',
    'lat', 'lng', 'isDefault', 'addressLabel', 'addressIndex', 'addressCount',
    'lastOrderDate', 'orderCount', 'bottleBalance', 'outstandingDebt',
    'activeCodDebtCount', 'codRestricted', 'isSharedPlace', 'hasDebt',
    'placeGroupId', 'placeLabel', 'lastOrderBy', 'members',
  ]);
  // One row of a place pin's `members`, pinned against the live route the same way.
  const MAP_PLACE_MEMBER_KEYS = new Set(['userId', 'fullName', 'phone', 'lastOrderDate', 'outstandingDebt', 'activeCodDebtCount', 'codRestricted']);

  const where = 'See CustomerMapPinSchema in business_app/serializers/admin_serializers.py.';
  const checkKeys = (row, keys, what) => {
    const invented = Object.keys(row).filter((key) => !keys.has(key));
    if (invented.length) {
      throw new Error(`${what} fixture invents keys the backend never emits: ${invented.join(', ')}. ${where}`);
    }
    const missing = [...keys].filter((key) => !(key in row));
    if (missing.length) {
      throw new Error(`${what} fixture is missing keys the backend always emits: ${missing.join(', ')}. ${where}`);
    }
  };
  return {
    mapPin: (pin) => {
      checkKeys(pin, MAP_PIN_KEYS, 'Customer-map pin');
      pin.members.forEach((member) => checkKeys(member, MAP_PLACE_MEMBER_KEYS, 'Customer-map place member'));
      return pin;
    },
  };
});

vi.mock('../services/adminService', () => ({
  default: {
    getCustomerMapPins: vi.fn().mockResolvedValue({
      data: { pins: [
        mapPin({ kind: 'address', addressId: 1, userId: 1, fullName: 'Recent Guy', phone: '+998900000001',
          userType: 'individual', entitySubtype: null, lat: 41.31, lng: 69.28,
          isDefault: true, addressLabel: 'A1', addressIndex: 1, addressCount: 1,
          lastOrderDate: new Date().toISOString(), orderCount: 3,
          bottleBalance: 2, outstandingDebt: 0, activeCodDebtCount: 0, codRestricted: false,
          isSharedPlace: false, hasDebt: false, placeGroupId: null, placeLabel: null,
          lastOrderBy: null, members: [] }),
        mapPin({ kind: 'address', addressId: 2, userId: 2, fullName: 'Idle Guy', phone: '+998900000002',
          userType: 'entity', entitySubtype: 'grocery_store', lat: 41.32, lng: 69.29,
          isDefault: true, addressLabel: 'A2', addressIndex: 1, addressCount: 1,
          lastOrderDate: '2026-01-01T00:00:00Z', orderCount: 1,
          bottleBalance: 0, outstandingDebt: 9000, activeCodDebtCount: 1, codRestricted: false,
          isSharedPlace: false, hasDebt: true, placeGroupId: null, placeLabel: null,
          lastOrderBy: null, members: [] }),
        // A shared workplace: ONE pin for the whole place, coincident with pin 1
        // (same lat/lng). `bottleBalance` is the place pool, reported once.
        mapPin({ kind: 'place', addressId: 3, userId: null, fullName: '', phone: null,
          userType: 'individual', entitySubtype: null, lat: 41.31, lng: 69.28,
          isDefault: false, addressLabel: 'A3', addressIndex: 1, addressCount: 1,
          lastOrderDate: new Date().toISOString(), orderCount: 5,
          bottleBalance: 7, outstandingDebt: 0, activeCodDebtCount: 0, codRestricted: false,
          isSharedPlace: true, hasDebt: false, placeGroupId: 7, placeLabel: 'Acme office',
          lastOrderBy: 'Walk-in Carol',
          members: [
            { userId: 3, fullName: 'Recent Alice', phone: '+998900000003',
              lastOrderDate: new Date().toISOString(), outstandingDebt: 0, activeCodDebtCount: 0,
              codRestricted: false },
            { userId: 5, fullName: 'Old Bob', phone: '+998900000005',
              lastOrderDate: '2026-01-01T00:00:00Z', outstandingDebt: 0, activeCodDebtCount: 0,
              codRestricted: false },
            { userId: 4, fullName: 'Office Newcomer', phone: '+998900000004',
              lastOrderDate: null, outstandingDebt: 0, activeCodDebtCount: 0, codRestricted: false },
          ] }),
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

  it('lays the "no confirmed order" legend entry and the Showing count out as separate items with a gap', async () => {
    renderMap();
    await waitFor(() => expect(screen.getAllByTestId('pin')).toHaveLength(3));
    const none = screen.getByText('No confirmed order yet');
    const showing = screen.getByText(/^Showing/);
    expect(none).not.toBe(showing);
    expect(none.contains(showing)).toBe(false);
    // Both sit in ONE antd Space (each its own item), so they cannot run together.
    const space = none.closest('.ant-space-horizontal').parentElement.closest('.ant-space-horizontal');
    expect(space).not.toBeNull();
    expect(space.contains(showing)).toBe(true);
    expect(none.closest('.ant-space-item').parentElement.closest('.ant-space-item'))
      .not.toBe(showing.closest('.ant-space-item'));
  });

  it('shows the address text under the title of an unlabeled place popup', async () => {
    const pins = (await adminService.getCustomerMapPins()).data.pins;
    adminService.getCustomerMapPins.mockResolvedValueOnce({
      data: { pins: pins.map((p) => (p.kind === 'place' ? { ...p, placeLabel: null } : p)) },
    });
    renderMap();
    await waitFor(() => expect(screen.getAllByTestId('pin')).toHaveLength(3));
    // The fixture place's address text is "A3"; the title falls back to Place #7.
    const placePopup = screen.getAllByTestId('popup').find((el) => within(el).queryByText('Place #7'));
    expect(within(placePopup).getByText('A3')).toBeInTheDocument();
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

  it('counts a shared place once on the heat layer', async () => {
    renderMap();
    await waitFor(() => expect(screen.getAllByTestId('pin')).toHaveLength(3));
    fireEvent.click(screen.getByRole('radio', { name: /heatmap/i }));
    await waitFor(() => expect(screen.getByTestId('heat')).toBeInTheDocument());
    const points = JSON.parse(screen.getByTestId('heat').getAttribute('data-points'));
    expect(points).toHaveLength(3);
    expect(points[0]).toEqual([41.31, 69.28, 1]);   // solo address: one unit
    expect(points[1]).toEqual([41.32, 69.29, 1]);   // solo address with NO bottles: still one unit
    // The office is ONE place pin however many members it has: one unit, not one per member.
    expect(points[2]).toEqual([41.31, 69.28, 1]);
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

  it('does not paint a place red when one member ordered recently', async () => {
    renderMap();
    await waitFor(() => expect(screen.getAllByTestId('pin')).toHaveLength(3));
    // The place date is the newest order to the place, not the oldest member's.
    expect(screen.getAllByTestId('pin')[2].getAttribute('data-color')).toBe('hsl(120, 70%, 45%)');
  });

  it('lists every member of a place with their own last order here', async () => {
    renderMap();
    await waitFor(() => expect(screen.getAllByTestId('pin')).toHaveLength(3));
    expect(screen.getByText(/Recent Alice/)).toBeInTheDocument();
    expect(screen.getByText(/Old Bob/)).toBeInTheDocument();
    expect(screen.getByText(/never here/)).toBeInTheDocument();
    expect(screen.getByText('People at this place')).toBeInTheDocument();
  });

  it('opens a member profile from the place popup', async () => {
    const onViewUser = vi.fn();
    renderMap({ onViewUser });
    await waitFor(() => expect(screen.getAllByTestId('pin')).toHaveLength(3));
    const bob = screen.getByText(/Old Bob/).closest('[data-member]');
    fireEvent.click(within(bob).getByText('Profile'));
    expect(onViewUser).toHaveBeenCalledWith(5);
  });

  it('renders an unlabeled place as Place #id', async () => {
    const pins = (await adminService.getCustomerMapPins()).data.pins;
    adminService.getCustomerMapPins.mockResolvedValueOnce({
      data: { pins: pins.map((p) => (p.kind === 'place' ? { ...p, placeLabel: null } : p)) },
    });
    renderMap();
    await waitFor(() => expect(screen.getAllByTestId('pin')).toHaveLength(3));
    expect(screen.getByText('Place #7')).toBeInTheDocument();
  });

  it('shows a grey legend entry for places with no confirmed order', async () => {
    renderMap();
    await waitFor(() => expect(screen.getAllByTestId('pin')).toHaveLength(3));
    expect(screen.getByText('No confirmed order yet')).toBeInTheDocument();
  });
});
