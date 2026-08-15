import { render, screen, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';

/**
 * The dispatch page's structural contract.
 *
 * jsdom performs no layout, so nothing here can prove the map is wide or that
 * the panels are not covered — that is verified in a real browser. What these
 * tests CAN pin is the structure those visual claims rest on: the map is a
 * full-width sibling of the panels rather than a grid neighbour competing for
 * the same row, and the per-leg data actually reaches the panel that renders
 * it, keyed the way the API published it.
 */

vi.mock('../components/OperationsMap', () => ({
  default: ({ height }) => <div data-testid="operations-map" data-height={String(height)} />,
}));

vi.mock('react-hot-toast', () => ({
  default: { success: vi.fn(), error: vi.fn() },
}));

const SNAPSHOT = {
  date: '2026-08-15',
  orders: [],
  unmapped: [],
  pool: [],
  drivers: [{ driver_id: 5, full_name: 'Jahongir Elliev' }],
  routes: [
    {
      route_id: 1,
      driver_id: 5,
      manual_override: false,
      total_distance_km: 65.8,
      estimated_duration_minutes: 126,
      start_lat: 41.3,
      start_lng: 69.24,
      stops: [
        {
          delivery_id: 11, order_id: 1, position: 0, pinned: false,
          order_number: 'TG_000381_26', customer_name: 'Serega',
          address_label: 'Taras Shevchenko 33', lat: 41.31, lng: 69.25,
          items: [{ product_id: 1, product_name: 'Pure Water 19L', quantity: 2, is_reward: false }],
          items_total_count: 1, items_hidden_count: 0,
        },
        {
          delivery_id: 22, order_id: 2, position: 1, pinned: false,
          order_number: 'AD_000029_26', customer_name: 'Donald',
          address_label: 'Aloqa 9', lat: 41.32, lng: 69.26,
          items: [], items_total_count: 0, items_hidden_count: 0,
        },
      ],
    },
  ],
};

const GEOMETRY = {
  driver_id: 5,
  geometry: [[41.3, 69.24], [41.31, 69.25]],
  legs: [
    { distance_km: 4.2, duration_minutes: 11 },
    { distance_km: 1.8, duration_minutes: 5 },
  ],
  leg_delivery_ids: [11, 22],
  approximate: false,
  cached: false,
};

const adminService = {
  getDispatchSnapshot: vi.fn(),
  getDispatchRouteGeometry: vi.fn(),
  setDispatchStops: vi.fn(),
  reoptimizeDispatchRoute: vi.fn(),
  assignDispatchStop: vi.fn(),
  unassignDispatchStop: vi.fn(),
};

vi.mock('../services/adminService', () => ({ default: adminService }));

const renderPage = async () => {
  const { QueryClient, QueryClientProvider } = await import('@tanstack/react-query');
  const { default: Dispatch } = await import('./Dispatch');
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <Dispatch />
    </QueryClientProvider>,
  );
};

beforeEach(() => {
  vi.clearAllMocks();
  adminService.getDispatchSnapshot.mockResolvedValue({ data: SNAPSHOT });
  adminService.getDispatchRouteGeometry.mockResolvedValue({ data: GEOMETRY });
});

describe('Dispatch layout', () => {
  it('renders the map outside the row that holds the panels', async () => {
    await renderPage();
    const map = await screen.findByTestId('operations-map');
    const pool = await screen.findByTestId('pool-panel');

    // If the map were a grid neighbour of the panels, it would share their
    // closest `.ant-row` ancestor. Being a full-width band means it does not.
    expect(map.closest('.ant-row')).not.toBe(pool.closest('.ant-row'));
  });

  it('gives the map a taller viewport now that it spans the page', async () => {
    await renderPage();
    const map = await screen.findByTestId('operations-map');
    expect(Number(map.dataset.height)).toBeGreaterThanOrEqual(560);
  });
});

describe('Dispatch leg wiring', () => {
  it('passes the measured legs to the route panel', async () => {
    await renderPage();
    await waitFor(() => expect(screen.getByTestId('stop-leg-11')).toHaveTextContent('4.2 km'));
    expect(screen.getByTestId('stop-leg-22')).toHaveTextContent('1.8 km');
  });

  it('shows no leg figures when the provider measured none', async () => {
    adminService.getDispatchRouteGeometry.mockResolvedValue({
      data: { ...GEOMETRY, legs: null, leg_delivery_ids: [] },
    });
    await renderPage();
    await screen.findByTestId('stop-row-11');
    expect(screen.queryByTestId('stop-leg-11')).not.toBeInTheDocument();
  });

  it('still renders the board when geometry has not arrived yet', async () => {
    // Geometry is a separate request from the snapshot; the stop list must
    // not wait on it.
    adminService.getDispatchRouteGeometry.mockReturnValue(new Promise(() => {}));
    await renderPage();
    expect(await screen.findByTestId('stop-row-11')).toBeInTheDocument();
    expect(screen.queryByTestId('stop-leg-11')).not.toBeInTheDocument();
  });

  it('shows each stop its order items', async () => {
    await renderPage();
    expect(await screen.findByTestId('stop-items-11')).toHaveTextContent('Pure Water 19L ×2');
    expect(screen.queryByTestId('stop-items-22')).not.toBeInTheDocument();
  });
});
