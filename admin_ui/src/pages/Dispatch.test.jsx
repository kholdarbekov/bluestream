import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { describe, it, expect, vi, beforeEach } from 'vitest';

// Exposes the props the wiring tests need to observe: the `geometry` map the
// page built, a way to simulate picking a driver (which is normally done by
// clicking a driver marker inside OperationsMap itself — out of scope for
// this mock, so it's exposed as a plain button), and a way to simulate
// clicking an unassigned order marker via `onSelectStop`.
vi.mock('../components/OperationsMap', () => ({
  default: ({ geometry, onSelectDriver, onSelectStop }) => (
    <div data-testid="ops-map">
      <div data-testid="ops-map-geometry">{JSON.stringify(geometry)}</div>
      <button type="button" data-testid="ops-map-select-driver" onClick={() => onSelectDriver(5)}>
        select driver 5
      </button>
      <button
        type="button"
        data-testid="ops-map-select-stop"
        disabled={typeof onSelectStop !== 'function'}
        onClick={() => onSelectStop({ delivery_id: 33 })}
      >
        select stop 33
      </button>
    </div>
  ),
}));

vi.mock('react-hot-toast', () => ({ default: { success: vi.fn(), error: vi.fn() } }));

const SNAPSHOT = {
  data: {
    date: '2026-08-06',
    orders: [],
    unmapped: [{ order_id: 4, order_number: 'A-9', customer_name: 'Zed', address_label: '', reason: 'no_coordinates' }],
    pool: [{
      delivery_id: 33, order_id: 44, order_number: 'A-3', customer_name: 'Cid', customer_phone: '',
      address_label: 'z', lat: 41.32, lng: 69.22, total_amount: 20000, is_cod: true, is_overdue: false, time_slot: null,
    }],
    drivers: [{ driver_id: 5, full_name: 'Ali', lat: 41.3, lng: 69.2, location_status: 'fresh', active_count: 2, phone: '' }],
    routes: [{
      route_id: 1,
      driver_id: 5,
      manual_override: false,
      total_distance_km: 18.2,
      estimated_duration_minutes: 62,
      stops: [
        { delivery_id: 11, position: 0, order_number: 'A-1', customer_name: 'Ann', address_label: 'x', pinned: false, lat: 41.3, lng: 69.2, delivery_status: 'assigned' },
        { delivery_id: 22, position: 1, order_number: 'A-2', customer_name: 'Bob', address_label: 'y', pinned: false, lat: 41.31, lng: 69.21, delivery_status: 'assigned' },
      ],
    }],
  },
};

vi.mock('../services/adminService', () => {
  const explicit = {
    getDispatchSnapshot: vi.fn(),
    getDispatchRouteGeometry: vi.fn().mockResolvedValue({ data: { geometry: null, approximate: false } }),
    setDispatchStops: vi.fn().mockResolvedValue({ data: {} }),
    reoptimizeDispatchRoute: vi.fn().mockResolvedValue({ data: {} }),
    assignDispatchStop: vi.fn().mockResolvedValue({ data: {} }),
    unassignDispatchStop: vi.fn().mockResolvedValue({ data: {} }),
  };
  return { default: new Proxy(explicit, { get: (t, p) => (p in t ? t[p] : vi.fn().mockResolvedValue({ data: {} })) }) };
});

import adminService from '../services/adminService';
import toast from 'react-hot-toast';
import Dispatch from './Dispatch';

const renderPage = () => {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter><Dispatch /></MemoryRouter>
    </QueryClientProvider>,
  );
};

beforeEach(() => {
  vi.clearAllMocks();
  adminService.getDispatchSnapshot.mockResolvedValue(SNAPSHOT);
});

describe('Dispatch page', () => {
  it('renders the map and the driver panel', async () => {
    renderPage();
    expect(await screen.findByTestId('ops-map')).toBeInTheDocument();
    expect(await screen.findByTestId('stop-row-11')).toBeInTheDocument();
  });

  it('surfaces the ungeocoded-order count instead of hiding it', async () => {
    renderPage();
    expect(await screen.findByTestId('unmapped-count')).toHaveTextContent('1');
  });

  // Regression for bug #5: the unmapped card used to hardcode "no
  // coordinates" for every row, which was false for orders that were simply
  // unscheduled. `DispatchService` now emits a `reason` per row
  // (`not_scheduled` / `no_coordinates`) and the panel must show which one
  // actually applies, not a single blanket claim.
  it('shows the per-row reason instead of a single hardcoded "no coordinates" claim', async () => {
    adminService.getDispatchSnapshot.mockResolvedValueOnce({
      data: {
        ...SNAPSHOT.data,
        unmapped: [
          { order_id: 4, order_number: 'A-9', customer_name: 'Zed', address_label: '', reason: 'no_coordinates' },
          { order_id: 8, order_number: 'A-11', customer_name: 'Wex', address_label: '', reason: 'not_scheduled' },
        ],
      },
    });
    renderPage();

    await waitFor(() => expect(screen.getByTestId('unmapped-reason-4')).toHaveTextContent('No coordinates'));
    expect(screen.getByTestId('unmapped-reason-8')).toHaveTextContent('Not scheduled');
  });

  // A reason that is neither of the two the backend currently emits must not
  // be relabelled as one of them with false confidence — that mislabelling
  // IS bug #5 (a row shown under a heading that doesn't describe it). Proves
  // the match is an explicit two-way check with a neutral fallback, not a
  // ternary that defaults anything unrecognised to "Not scheduled".
  it('shows a neutral fallback for a reason the frontend does not recognise', async () => {
    adminService.getDispatchSnapshot.mockResolvedValueOnce({
      data: {
        ...SNAPSHOT.data,
        unmapped: [
          { order_id: 9, order_number: 'A-12', customer_name: 'Yui', address_label: '', reason: 'some_future_reason' },
        ],
      },
    });
    renderPage();

    await waitFor(() => expect(screen.getByTestId('unmapped-reason-9')).toHaveTextContent('Unknown reason'));
  });

  it('saving a reorder sends the draft order and the server set as the guard', async () => {
    renderPage();
    fireEvent.click(await screen.findByTestId('stop-down-11'));
    fireEvent.click(screen.getByTestId('route-save'));

    await waitFor(() => expect(adminService.setDispatchStops).toHaveBeenCalledTimes(1));
    expect(adminService.setDispatchStops).toHaveBeenCalledWith(5, {
      ordered_delivery_ids: [22, 11],
      pinned: {},
      expected_delivery_ids: [11, 22],
    });
  });

  it('pinning a stop is included in the saved payload', async () => {
    renderPage();
    fireEvent.click(await screen.findByTestId('stop-pin-22'));
    fireEvent.click(screen.getByTestId('route-save'));

    await waitFor(() => expect(adminService.setDispatchStops).toHaveBeenCalledTimes(1));
    expect(adminService.setDispatchStops.mock.calls[0][1].pinned).toEqual({ 22: 1 });
  });

  it('does not save until the admin presses Save', async () => {
    renderPage();
    fireEvent.click(await screen.findByTestId('stop-down-11'));
    expect(adminService.setDispatchStops).not.toHaveBeenCalled();
  });

  it('pooling a stop calls unassign immediately with the delivery id', async () => {
    renderPage();
    fireEvent.click(await screen.findByTestId('stop-pool-22'));
    fireEvent.click(await screen.findByText('OK'));

    await waitFor(() => expect(adminService.unassignDispatchStop).toHaveBeenCalledWith(22, { reason: null }));
  });

  it('a 409 surfaces the conflict notice', async () => {
    adminService.setDispatchStops.mockRejectedValueOnce({
      response: { status: 409, data: { error_code: 'DISPATCH_ROUTE_STALE', data: { current_delivery_ids: [22] } } },
    });
    renderPage();
    fireEvent.click(await screen.findByTestId('stop-down-11'));
    fireEvent.click(screen.getByTestId('route-save'));

    expect(await screen.findByTestId('route-conflict')).toBeInTheDocument();
  });
});

describe('Dispatch page pool panel', () => {
  it('renders the pool panel with the unassigned stop', async () => {
    renderPage();
    expect(await screen.findByTestId('pool-panel')).toBeInTheDocument();
    expect(await screen.findByTestId('pool-row-33')).toBeInTheDocument();
  });

  it('assigning a pool stop calls assignDispatchStop with exactly the delivery id and driver id', async () => {
    renderPage();
    fireEvent.click(await screen.findByTestId('pool-assign-33'));
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Ali' }));

    await waitFor(() => expect(adminService.assignDispatchStop).toHaveBeenCalledWith(33, { driver_id: 5 }));
  });

  it("a rejected pool assignment surfaces the server's message, not a generic failure", async () => {
    adminService.assignDispatchStop.mockRejectedValueOnce({
      response: { data: { message: 'This driver is COD-blocked' } },
    });
    renderPage();
    fireEvent.click(await screen.findByTestId('pool-assign-33'));
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Ali' }));

    await waitFor(() => expect(toast.error).toHaveBeenCalledWith('This driver is COD-blocked'));
  });

  // OperationsMap already renders unassigned order markers and already
  // accepts `onSelectStop` — it was simply never passed one. Proving the
  // page passes a real callback (not just any truthy value) that lands on
  // the matching pool row is the meaningful assertion here; a prop-existence
  // check alone could pass against a no-op.
  it('passes a working onSelectStop to the map that selects the matching pool row', async () => {
    renderPage();
    await screen.findByTestId('pool-row-33');
    expect(screen.getByTestId('ops-map-select-stop')).not.toBeDisabled();

    fireEvent.click(screen.getByTestId('ops-map-select-stop'));
    expect(screen.getByTestId('pool-row-33')).toHaveAttribute('data-selected', 'true');
  });
});

describe('Dispatch page route geometry (fetched for every visible route)', () => {
  // The bug: geometry fetching was gated on `selectedDriverId != null`, and
  // nothing selects a driver by default, so every route always rendered the
  // dashed straight-leg fallback. The fix fetches geometry for every route in
  // the snapshot's `routes`, independent of selection — proven here by NOT
  // selecting a driver at all.
  it('fetches geometry for a rendered route without any driver being selected', async () => {
    renderPage();
    await screen.findByTestId('stop-row-11');

    await waitFor(() => expect(adminService.getDispatchRouteGeometry).toHaveBeenCalledWith(5));
  });

  it('fetches geometry for every route when more than one is visible, not just one', async () => {
    adminService.getDispatchSnapshot.mockResolvedValueOnce({
      data: {
        ...SNAPSHOT.data,
        routes: [
          ...SNAPSHOT.data.routes,
          {
            route_id: 2,
            driver_id: 6,
            manual_override: false,
            total_distance_km: 5.4,
            estimated_duration_minutes: 21,
            stops: [
              {
                delivery_id: 77, position: 0, order_number: 'A-7', customer_name: 'Gia',
                address_label: 'g', pinned: false, lat: 41.33, lng: 69.23, delivery_status: 'assigned',
              },
            ],
          },
        ],
      },
    });
    renderPage();
    await screen.findByTestId('stop-row-77');

    await waitFor(() => expect(adminService.getDispatchRouteGeometry).toHaveBeenCalledWith(5));
    await waitFor(() => expect(adminService.getDispatchRouteGeometry).toHaveBeenCalledWith(6));
  });

  it('feeds the fetched geometry to OperationsMap keyed by driver id', async () => {
    renderPage();
    await screen.findByTestId('stop-row-11');

    await waitFor(() => expect(screen.getByTestId('ops-map-geometry')).toHaveTextContent(
      JSON.stringify({ 5: { geometry: null, approximate: false } }),
    ));
  });

  it('re-fetches every visible route geometry after a successful save', async () => {
    renderPage();
    await waitFor(() => expect(adminService.getDispatchRouteGeometry).toHaveBeenCalledTimes(1));

    fireEvent.click(await screen.findByTestId('stop-down-11'));
    fireEvent.click(screen.getByTestId('route-save'));

    await waitFor(() => expect(adminService.setDispatchStops).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(adminService.getDispatchRouteGeometry).toHaveBeenCalledTimes(2));
  });
});
