import { render, screen, within } from '@testing-library/react';
import { describe, it, expect, vi } from 'vitest';

// react-leaflet renders to a real DOM canvas we don't need; stub it down to
// testable markers so assertions are about WHICH layers render, not Leaflet.
vi.mock('react-leaflet', () => ({
  MapContainer: ({ children }) => <div data-testid="map">{children}</div>,
  TileLayer: () => null,
  CircleMarker: ({ children, ...props }) => (
    <div data-testid="circle-marker" data-key={props['data-key']} data-color={props.pathOptions?.color}>{children}</div>
  ),
  // `data-icon` is the divIcon's HTML. An order pin's whole style (fill, border, overdue
  // ring) lives only there, so that is what the pin tests read.
  Marker: ({ children, ...props }) => (
    <div data-testid="marker" data-kind={props['data-kind']} data-icon={props.icon?.options?.html ?? ''}>
      {children}
    </div>
  ),
  // Positions and dashArray are what Defect-1 (depot point) and Important-1
  // (empty-geometry-must-still-dash) actually turn on, so the mock has to
  // surface both rather than just proving a <Polyline> rendered.
  Polyline: (props) => (
    <div data-testid="polyline" data-driver={props['data-driver']}
      data-positions={JSON.stringify(props.positions)}
      data-dash={props.pathOptions?.dashArray ?? ''} />
  ),
  Popup: ({ children }) => <div>{children}</div>,
  useMap: () => ({}),
}));
vi.mock('./HeatLayer', () => ({ default: () => null }));

import OperationsMap from './OperationsMap';

// Every key `DispatchService._order_entry` publishes in `GET /admin/dispatch/snapshot` `orders[]`,
// pinned against the live route by tests/unit/test_admin_ui_payload_fixture_contracts.py
// (`test_operations_map_order_entry_fixture_matches_the_live_snapshot`), which parses this set
// straight out of this file. Every order fixture in this file is built with `orderEntry`: each
// published key starts as `null`, so a fixture has the snapshot's full shape, and a key the
// snapshot never publishes throws.
const ORDER_ENTRY_KEYS = new Set([
  'order_id', 'order_number', 'status', 'delivery_id', 'delivery_status', 'driver_id', 'lat', 'lng',
  'address_label', 'customer_name', 'customer_phone', 'user_id', 'total_amount', 'payment_method', 'is_cod',
  'delivery_window', 'delivery_date', 'is_overdue', 'items', 'items_total_count', 'items_hidden_count',
  'awaiting_new_date',
]);
const orderEntry = (overrides) => {
  const invented = Object.keys(overrides).filter((key) => !ORDER_ENTRY_KEYS.has(key));
  if (invented.length) {
    throw new Error(`Order-entry fixture invents keys the snapshot never publishes: ${invented.join(', ')}`);
  }
  return { ...Object.fromEntries([...ORDER_ENTRY_KEYS].map((key) => [key, null])), ...overrides };
};

const ORDERS = [
  orderEntry({ order_id: 1, order_number: 'A-1', status: 'confirmed', lat: 41.3, lng: 69.2, delivery_id: 11, driver_id: null, is_overdue: false, customer_name: 'C', address_label: 'x', total_amount: 0 }),
];
const DRIVERS = [
  { driver_id: 5, full_name: 'Ali', lat: 41.31, lng: 69.21, location_status: 'fresh', active_count: 2, phone: '' },
];
const ROUTES = [
  {
    driver_id: 5,
    start_lat: 41.29,
    start_lng: 69.24,
    stops: [{ delivery_id: 11, position: 0, lat: 41.3, lng: 69.2, pinned: false, order_number: 'A-1', customer_name: 'C', address_label: 'x', delivery_status: 'assigned' }],
    manual_override: false,
  },
];

const baseProps = {
  customers: [], orders: ORDERS, drivers: DRIVERS, routes: ROUTES, geometry: {},
  visibleLayers: { customers: false, orders: true, drivers: true },
};

describe('OperationsMap layers', () => {
  it('renders order markers when the orders layer is on', () => {
    render(<OperationsMap {...baseProps} />);
    expect(screen.getAllByTestId('marker').some((n) => n.dataset.kind === 'order')).toBe(true);
  });

  it('hides order markers when the orders layer is off', () => {
    render(<OperationsMap {...baseProps} visibleLayers={{ customers: false, orders: false, drivers: true }} />);
    expect(screen.queryAllByTestId('marker').filter((n) => n.dataset.kind === 'order')).toHaveLength(0);
  });

  it('renders a driver marker and its route polyline', () => {
    render(<OperationsMap {...baseProps} />);
    expect(screen.getAllByTestId('marker').some((n) => n.dataset.kind === 'driver')).toBe(true);
    expect(screen.getByTestId('polyline').dataset.driver).toBe('5');
  });

  it('draws the route line starting at the route\'s depot, then the stops in order', () => {
    // No real geometry supplied (geometry: {} in baseProps), so the line is
    // built from route.start_lat/start_lng followed by each stop. A single
    // stop with no depot point would only be one coordinate — too few to draw
    // a line at all, which is exactly Defect 1 this fixture now guards.
    render(<OperationsMap {...baseProps} />);
    const positions = JSON.parse(screen.getByTestId('polyline').dataset.positions);
    expect(positions).toEqual([[41.29, 69.24], [41.3, 69.2]]);
  });

  it('draws the fallback (no real geometry) route line dashed, never solid', () => {
    render(<OperationsMap {...baseProps} />);
    expect(screen.getByTestId('polyline').dataset.dash).toBe('6 8');
  });

  it('draws the route line dashed even when geometry resolves to an empty array', () => {
    // The geometry endpoint does `result.get("polyline") or result.get("geometry")`,
    // which can legitimately resolve to `[]` — and `[]` is truthy, so a naive
    // `geo && geo.geometry` check would call this "real" and draw it solid,
    // presenting straight hops between stops as an actual driveable road.
    render(<OperationsMap {...baseProps} geometry={{ 5: { geometry: [], approximate: true } }} />);
    const polyline = screen.getByTestId('polyline');
    expect(polyline.dataset.dash).toBe('6 8');
    expect(JSON.parse(polyline.dataset.positions)).toEqual([[41.29, 69.24], [41.3, 69.2]]);
  });

  it('draws the route line solid using the real geometry when one is supplied', () => {
    const geometry = { 5: { geometry: [[41.29, 69.24], [41.295, 69.23], [41.3, 69.2]], approximate: false } };
    render(<OperationsMap {...baseProps} geometry={geometry} />);
    const polyline = screen.getByTestId('polyline');
    expect(polyline.dataset.dash).toBe('');
    expect(JSON.parse(polyline.dataset.positions)).toEqual(geometry[5].geometry);
  });

  it('draws no route line at all once a route has lost every stop', () => {
    // The stale-polyline half of the reassignment bug. `geometry` is a cached
    // payload measured over stops the driver had at fetch time; `route.stops`
    // is what they have now. Deciding "this is a real road path" from the
    // cached payload ALONE meant an emptied route kept a full, solid,
    // multi-kilometre path drawn across the city — the fallback branch
    // correctly collapses to a single depot point and bails, so only the
    // geometry branch could produce this, and only by outliving its stops.
    const geometry = { 5: { geometry: [[41.29, 69.24], [41.295, 69.23], [41.3, 69.2]], approximate: false } };
    const emptied = [{ ...ROUTES[0], stops: [] }];
    render(<OperationsMap {...baseProps} routes={emptied} geometry={geometry} />);
    expect(screen.queryAllByTestId('polyline')).toHaveLength(0);
  });

  it('hides drivers and routes when the drivers layer is off', () => {
    render(<OperationsMap {...baseProps} visibleLayers={{ customers: false, orders: true, drivers: false }} />);
    expect(screen.queryAllByTestId('polyline')).toHaveLength(0);
    expect(screen.queryAllByTestId('marker').filter((n) => n.dataset.kind === 'driver')).toHaveLength(0);
  });

  it('renders customer circles only when the customers layer is on', () => {
    const customers = [{ addressId: 1, lat: 41.3, lng: 69.2, fullName: 'X', lastOrderDate: null, bottleBalance: 0, outstandingDebt: 0, orderCount: 0, addressIndex: 1, addressCount: 1 }];
    const { rerender } = render(<OperationsMap {...baseProps} customers={customers} />);
    expect(screen.queryAllByTestId('circle-marker')).toHaveLength(0);
    rerender(
      <OperationsMap {...baseProps} customers={customers}
        visibleLayers={{ customers: true, orders: false, drivers: false }} />,
    );
    expect(screen.getAllByTestId('circle-marker')).toHaveLength(1);
  });
});

describe('OperationsMap check-in layer', () => {
  // Field check-ins as `GET /admin/sales/visits` publishes them: `in_radius` is TRI-state and
  // `null` is a different fact from `false` — the check-in was not measurable (skipped, no
  // coordinates, or an outlet that has no pin yet), which must never be drawn as a violation.
  const CHECKINS = [
    { visit_id: 501, lat: 41.3111, lng: 69.2797, in_radius: true, distance_m: 12.4, outlet_name: 'Bahor market', agent_name: 'Sardor Alimov' },
    { visit_id: 502, lat: 41.2555, lng: 69.1888, in_radius: false, distance_m: 640, outlet_name: 'Yunus shop', agent_name: 'Nodira Karimova' },
    { visit_id: 503, lat: 41.3333, lng: 69.2999, in_radius: null, distance_m: null, outlet_name: 'Chorsu kiosk', agent_name: 'Sardor Alimov' },
  ];
  const checkinProps = {
    customers: [], orders: [], drivers: [], routes: [], geometry: {}, checkins: CHECKINS,
    visibleLayers: { customers: false, orders: false, drivers: false, checkins: true },
  };

  it('colours every check-in by the published verdict, and an unmeasured one grey', () => {
    render(<OperationsMap {...checkinProps} />);
    const markers = screen.getAllByTestId('circle-marker');
    // Three distinct colours, in the order the page handed the points over. Routing these
    // points through the `customers` layer instead would recolour them by `lastOrderDate`
    // (OperationsMap.jsx:116-136) and silently paint an out-of-range check-in green.
    expect(markers.map((n) => [n.dataset.key, n.dataset.color])).toEqual([
      ['v-501', '#52c41a'],
      ['v-502', '#ff4d4f'],
      ['v-503', '#8c8c8c'],
    ]);
  });

  it('names the outlet, the agent and the measured distance in each popup', () => {
    render(<OperationsMap {...checkinProps} />);
    expect(screen.getByText('Yunus shop')).toBeInTheDocument();
    expect(screen.getByText('Nodira Karimova')).toBeInTheDocument();
    expect(screen.getByText('640 m')).toBeInTheDocument();
    // No distance was measured for the third one — a dash, not a fabricated 0 m.
    expect(screen.getByText('—')).toBeInTheDocument();
  });

  it('draws nothing when the check-ins layer is off', () => {
    render(<OperationsMap {...checkinProps} visibleLayers={{ customers: false, orders: false, drivers: false }} />);
    expect(screen.queryAllByTestId('circle-marker')).toHaveLength(0);
  });
});

describe('OperationsMap order pins', () => {
  // Orders 21 and 22 are both held by driver 7 and both past their date. The only difference
  // is the backend's `awaiting_new_date`: order 22's delivery failed and is waiting to be re-dated.
  const LATE_ON_ROUTE = orderEntry({
    order_id: 21, order_number: 'TG_000021_26', status: 'out_for_delivery', delivery_id: 31,
    delivery_status: 'in_transit', driver_id: 7, lat: 41.3111, lng: 69.2797, address_label: 'Chilonzor 9',
    customer_name: 'Dilnoza Rahimova', total_amount: 42000, is_cod: false, is_overdue: true,
    awaiting_new_date: false,
  });
  const AWAITING_NEW_DATE = orderEntry({
    order_id: 22, order_number: 'TG_000022_26', status: 'out_for_delivery', delivery_id: 32,
    delivery_status: 'failed', driver_id: 7, lat: 41.3222, lng: 69.2888, address_label: 'Yunusobod 4',
    customer_name: 'Bekzod Tursunov', total_amount: 38000, is_cod: false, is_overdue: true,
    awaiting_new_date: true,
  });
  const WAITING_IN_POOL = orderEntry({
    order_id: 23, order_number: 'TG_000023_26', status: 'confirmed', delivery_id: 33,
    delivery_status: 'pending', driver_id: null, lat: 41.2555, lng: 69.1888, address_label: 'Sergeli 2',
    customer_name: 'Nodira Karimova', total_amount: 27000, is_cod: true, is_overdue: false,
    awaiting_new_date: false,
  });
  const pinProps = {
    customers: [], drivers: [], routes: [], geometry: {},
    visibleLayers: { customers: false, orders: true, drivers: false },
  };
  const onlyOrderPin = () => {
    const pins = screen.getAllByTestId('marker').filter((n) => n.dataset.kind === 'order');
    expect(pins).toHaveLength(1);
    return pins[0];
  };

  it('draws an order awaiting a new date as its own pin, with no status fill and no overdue ring', () => {
    render(<OperationsMap {...pinProps} orders={[AWAITING_NEW_DATE]} />);
    const icon = onlyOrderPin().dataset.icon;
    expect(icon).toContain('border:3px solid #722ed1');
    expect(icon).toContain('rotate(45deg)');
    // The failed row keeps its driver and its old date. Drawn the regular way, it would be
    // filled in its status colour (someone is bringing it) and ringed red (it is late).
    expect(icon).not.toContain('#fa8c16');
    expect(icon).not.toContain('rgba(255,77,79,.55)');
  });

  it('says in the popup why the pin needs a new date, and does not call it overdue', () => {
    render(<OperationsMap {...pinProps} orders={[AWAITING_NEW_DATE]} />);
    const pin = onlyOrderPin();
    expect(within(pin).getByText('TG_000022_26')).toBeInTheDocument();
    expect(within(pin).getByText('Delivery failed: needs a new date')).toBeInTheDocument();
    expect(within(pin).queryByText('Overdue')).not.toBeInTheDocument();
  });

  it('keeps a late order on a route filled in its status colour, ringed and labelled Overdue', () => {
    render(<OperationsMap {...pinProps} orders={[LATE_ON_ROUTE]} />);
    const pin = onlyOrderPin();
    expect(pin.dataset.icon).toContain('background:#fa8c16');
    expect(pin.dataset.icon).toContain('border:2px solid #fa8c16');
    expect(pin.dataset.icon).toContain('box-shadow:0 0 0 3px rgba(255,77,79,.55)');
    expect(pin.dataset.icon).not.toContain('#722ed1');
    expect(within(pin).getByText('Overdue')).toBeInTheDocument();
    expect(within(pin).queryByText('Delivery failed: needs a new date')).not.toBeInTheDocument();
  });

  it('keeps an unassigned order hollow and dashed, unringed and labelled Unassigned', () => {
    render(<OperationsMap {...pinProps} orders={[WAITING_IN_POOL]} />);
    const pin = onlyOrderPin();
    expect(pin.dataset.icon).toContain('background:transparent');
    expect(pin.dataset.icon).toContain('border:2px dashed #1677ff');
    expect(pin.dataset.icon).toContain('box-shadow:none');
    expect(within(pin).getByText('Unassigned')).toBeInTheDocument();
    expect(within(pin).queryByText('Delivery failed: needs a new date')).not.toBeInTheDocument();
  });
});
