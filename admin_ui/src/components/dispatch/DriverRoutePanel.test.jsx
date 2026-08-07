import { render, screen, fireEvent } from '@testing-library/react';
import { describe, it, expect, vi } from 'vitest';
import DriverRoutePanel from './DriverRoutePanel';

const STOPS = [
  { delivery_id: 11, position: 0, order_number: 'A-1', customer_name: 'Ann', address_label: 'Chilonzor', pinned: false, delivery_status: 'assigned' },
  { delivery_id: 22, position: 1, order_number: 'A-2', customer_name: 'Bob', address_label: 'Yunusobod', pinned: false, delivery_status: 'assigned' },
  { delivery_id: 33, position: 2, order_number: 'A-3', customer_name: 'Cid', address_label: 'Sergeli', pinned: true, delivery_status: 'assigned' },
];

const DRIVERS = [
  { driver_id: 5, full_name: 'Ali' },
  { driver_id: 6, full_name: 'Bek' },
];

const baseProps = {
  route: { driver_id: 5, manual_override: false, total_distance_km: 18.2, estimated_duration_minutes: 62 },
  drivers: DRIVERS,
  stops: STOPS,
  pinned: { 33: 2 },
  dirty: false,
  saving: false,
  onReorder: vi.fn(),
  onTogglePin: vi.fn(),
  onMove: vi.fn(),
  onPool: vi.fn(),
  onSave: vi.fn(),
  onDiscard: vi.fn(),
  onReoptimize: vi.fn(),
};

describe('DriverRoutePanel', () => {
  it('lists every stop in route order', () => {
    render(<DriverRoutePanel {...baseProps} />);
    const rows = screen.getAllByTestId(/^stop-row-/);
    expect(rows.map((r) => r.dataset.deliveryId)).toEqual(['11', '22', '33']);
  });

  it('moving a stop down emits the reordered id list', () => {
    const onReorder = vi.fn();
    render(<DriverRoutePanel {...baseProps} onReorder={onReorder} />);
    fireEvent.click(screen.getByTestId('stop-down-11'));
    expect(onReorder).toHaveBeenCalledWith([22, 11, 33]);
  });

  it('moving the first stop up does nothing', () => {
    const onReorder = vi.fn();
    render(<DriverRoutePanel {...baseProps} onReorder={onReorder} />);
    expect(screen.getByTestId('stop-up-11')).toBeDisabled();
    expect(onReorder).not.toHaveBeenCalled();
  });

  it('a drop emits the dragged-to order', () => {
    const onReorder = vi.fn();
    render(<DriverRoutePanel {...baseProps} onReorder={onReorder} />);
    fireEvent.dragStart(screen.getByTestId('stop-row-33'));
    fireEvent.dragOver(screen.getByTestId('stop-row-11'));
    fireEvent.drop(screen.getByTestId('stop-row-11'));
    expect(onReorder).toHaveBeenCalledWith([33, 11, 22]);
  });

  it('pin toggles emit the delivery id', () => {
    const onTogglePin = vi.fn();
    render(<DriverRoutePanel {...baseProps} onTogglePin={onTogglePin} />);
    fireEvent.click(screen.getByTestId('stop-pin-22'));
    expect(onTogglePin).toHaveBeenCalledWith(22);
  });

  it('pooling a stop emits the delivery id', () => {
    // The pool button is an antd Popconfirm trigger: a single click only opens
    // the confirmation popover (the click is not "swallowed" — a real admin
    // sees the same two-step prompt). Deleting the confirmation to make this a
    // one-click test would turn an accidental tap into a real dispatch error,
    // so the test drives the actual confirm flow: open, then click its OK.
    const onPool = vi.fn();
    render(<DriverRoutePanel {...baseProps} onPool={onPool} />);
    fireEvent.click(screen.getByTestId('stop-pool-22'));
    fireEvent.click(screen.getByRole('button', { name: /^OK$/ }));
    expect(onPool).toHaveBeenCalledWith(22);
  });

  it('save is disabled until the draft is dirty', () => {
    const { rerender } = render(<DriverRoutePanel {...baseProps} />);
    expect(screen.getByTestId('route-save')).toBeDisabled();
    rerender(<DriverRoutePanel {...baseProps} dirty />);
    expect(screen.getByTestId('route-save')).not.toBeDisabled();
  });

  it('shows the dispatch-locked badge when the route is overridden', () => {
    render(<DriverRoutePanel {...baseProps} route={{ ...baseProps.route, manual_override: true, overridden_by_name: 'Umar' }} />);
    expect(screen.getByTestId('route-locked-badge')).toBeInTheDocument();
  });

  // `metrics_stale` is set server-side (route_edit_service.py) whenever a
  // stop moves on/off this route, or a hand-authored sequence is saved
  // without a fresh matrix figure — the distance/duration below can
  // describe a route that no longer matches the stop list. It must be
  // visibly qualified, not shown as a plain confident number.
  it('marks distance and duration as approximate when metrics_stale is set', () => {
    render(<DriverRoutePanel {...baseProps} route={{ ...baseProps.route, metrics_stale: true }} />);
    const marker = screen.getByTestId('route-metrics-stale');
    expect(marker).toHaveTextContent('≈18.2 km');
    expect(marker).toHaveTextContent('≈62 min');
  });

  it('does not mark distance and duration as approximate when metrics are fresh', () => {
    render(<DriverRoutePanel {...baseProps} route={{ ...baseProps.route, metrics_stale: false }} />);
    expect(screen.queryByTestId('route-metrics-stale')).not.toBeInTheDocument();
    expect(screen.getByText(/18\.2 km/)).toBeInTheDocument();
  });

  // Not part of the brief's required suite, but the move control sits in the
  // same antd-portal risk category the pool Popconfirm turned out to be in
  // (Step 3's note calls out both `Popconfirm`/`Dropdown` together) — worth
  // proving the end-to-end click path actually reaches onMove with the right
  // (deliveryId, toDriverId) pair, and that the CURRENT driver is excluded
  // from its own move menu.
  it('picking a driver from the move menu emits the delivery id and target driver', async () => {
    const onMove = vi.fn();
    render(<DriverRoutePanel {...baseProps} onMove={onMove} />);
    fireEvent.click(screen.getByTestId('stop-move-22'));
    const target = await screen.findByText('Bek');
    fireEvent.click(target);
    expect(onMove).toHaveBeenCalledWith(22, 6);
    expect(screen.queryByText('Ali')).not.toBeInTheDocument();
  });

  // route.driver_id is 5 (Ali). With no OTHER driver on the roster, the move
  // menu would open with nothing in it — the same "looks broken" defect the
  // pool panel's assign control has to guard against.
  it('disables the move control when no other driver exists and does not emit onMove', () => {
    const onMove = vi.fn();
    render(<DriverRoutePanel {...baseProps} drivers={[{ driver_id: 5, full_name: 'Ali' }]} onMove={onMove} />);
    const button = screen.getByTestId('stop-move-11');
    expect(button).toBeDisabled();
    fireEvent.click(button);
    expect(onMove).not.toHaveBeenCalled();
  });

  // Regression for the production bug: the button group (`Space`, no
  // `flexShrink: 0`) had no defense against being squeezed by its
  // `flex: 1, minWidth: 0` sibling, so the flex algorithm collapsed the text
  // column to near-zero and the browser wrapped it one character per line.
  // jsdom performs no real layout, so nothing here can assert on rendered
  // geometry (an actual collapsed width, actual line count) — that would be
  // a test that always passes regardless of the bug. What IS meaningfully
  // assertable, and what actually regresses if someone removes the fix, is
  // the applied styling contract: the action-button group is marked
  // non-shrinking, and both text lines are marked non-wrapping +
  // ellipsis-truncating rather than left to wrap.
  it('marks the action-button group as non-shrinking so it cannot be squeezed into the text column', () => {
    render(<DriverRoutePanel {...baseProps} />);
    expect(screen.getByTestId('stop-actions-11')).toHaveStyle({ flexShrink: '0' });
  });

  it('marks both stop text lines to truncate instead of wrap', () => {
    render(<DriverRoutePanel {...baseProps} />);
    const expectedTruncation = { whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' };
    expect(screen.getByText('A-1 · Ann')).toHaveStyle(expectedTruncation);
    expect(screen.getByText('Chilonzor')).toHaveStyle(expectedTruncation);
  });
});
