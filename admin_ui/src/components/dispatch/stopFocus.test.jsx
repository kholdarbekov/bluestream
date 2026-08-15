import { render, screen, fireEvent } from '@testing-library/react';
import { describe, it, expect, vi } from 'vitest';
import DriverRoutePanel from './DriverRoutePanel';
import PoolPanel from './PoolPanel';

/**
 * Stacking the map above the panels means they are rarely both in view. That
 * is only an acceptable trade if picking a stop in a list can still move the
 * map to it — otherwise the layout fix would have cost the workflow the old
 * side-by-side arrangement was for.
 *
 * The row must stay draggable and its buttons must keep working, so the
 * focus gesture is a plain click on the row body and nothing else.
 */

const STOP = {
  delivery_id: 11, position: 0, order_number: 'A-1', customer_name: 'Ann',
  address_label: 'Chilonzor', lat: 41.31, lng: 69.25, pinned: false,
};

const routeProps = {
  route: { driver_id: 5, manual_override: false },
  drivers: [{ driver_id: 5, full_name: 'Ali' }, { driver_id: 6, full_name: 'Bek' }],
  stops: [STOP],
  pinned: {},
  onReorder: vi.fn(),
  onTogglePin: vi.fn(),
  onMove: vi.fn(),
  onPool: vi.fn(),
  onSave: vi.fn(),
  onDiscard: vi.fn(),
  onReoptimize: vi.fn(),
};

const poolProps = {
  stops: [{ ...STOP, is_cod: true }],
  drivers: [{ driver_id: 5, full_name: 'Ali' }],
  onAssign: vi.fn(),
};

describe('focusing a stop from a panel', () => {
  it('a click on a route stop row reports the stop', () => {
    const onFocusStop = vi.fn();
    render(<DriverRoutePanel {...routeProps} onFocusStop={onFocusStop} />);

    fireEvent.click(screen.getByTestId('stop-row-11'));

    expect(onFocusStop).toHaveBeenCalledWith(expect.objectContaining({
      delivery_id: 11, lat: 41.31, lng: 69.25,
    }));
  });

  it('a click on a pool row reports the stop', () => {
    const onFocusStop = vi.fn();
    render(<PoolPanel {...poolProps} onFocusStop={onFocusStop} />);

    fireEvent.click(screen.getByTestId('pool-row-11'));

    expect(onFocusStop).toHaveBeenCalledWith(expect.objectContaining({ delivery_id: 11 }));
  });

  it('using a row action does not also focus the stop', () => {
    // Reordering is a frequent action; hijacking the map on every nudge would
    // make the page jump under the admin while they work.
    const onFocusStop = vi.fn();
    const onReorder = vi.fn();
    render(<DriverRoutePanel
      {...routeProps}
      stops={[STOP, { ...STOP, delivery_id: 22, position: 1 }]}
      onFocusStop={onFocusStop}
      onReorder={onReorder}
    />);

    fireEvent.click(screen.getByTestId('stop-down-11'));

    expect(onReorder).toHaveBeenCalled();
    expect(onFocusStop).not.toHaveBeenCalled();
  });

  it('works without the callback', () => {
    render(<DriverRoutePanel {...routeProps} />);
    expect(() => fireEvent.click(screen.getByTestId('stop-row-11'))).not.toThrow();
  });
});
