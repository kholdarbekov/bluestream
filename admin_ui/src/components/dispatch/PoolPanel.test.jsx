import { render, screen, fireEvent, within } from '@testing-library/react';
import { describe, it, expect, vi } from 'vitest';
import PoolPanel from './PoolPanel';

const POOL = [
  {
    delivery_id: 101, order_id: 201, order_number: 'B-1', customer_name: 'Dee',
    customer_phone: '+998901112233', address_label: 'Chilonzor 5', lat: 41.3, lng: 69.2,
    total_amount: 50000, is_cod: true, is_overdue: false, time_slot: '10-12',
  },
  {
    delivery_id: 102, order_id: 202, order_number: 'B-2', customer_name: 'Eli',
    customer_phone: '+998901112244', address_label: 'Yunusobod 9', lat: 41.31, lng: 69.21,
    total_amount: 30000, is_cod: false, is_overdue: true, time_slot: '12-14',
  },
];

const DRIVERS = [
  { driver_id: 5, full_name: 'Ali' },
  { driver_id: 6, full_name: 'Bek' },
];

const baseProps = {
  stops: POOL,
  drivers: DRIVERS,
  assigning: false,
  onAssign: vi.fn(),
};

describe('PoolPanel', () => {
  it('lists one row per pool entry with the order number visible', () => {
    render(<PoolPanel {...baseProps} />);
    const rows = screen.getAllByTestId(/^pool-row-/);
    expect(rows).toHaveLength(2);
    expect(screen.getByText(/B-1/)).toBeInTheDocument();
    expect(screen.getByText(/B-2/)).toBeInTheDocument();
  });

  it('renders an explicit empty state when the pool is empty, not a blank card', () => {
    render(<PoolPanel {...baseProps} stops={[]} />);
    expect(screen.getByTestId('pool-panel')).toBeInTheDocument();
    expect(screen.getByTestId('pool-empty')).toBeInTheDocument();
    expect(screen.queryByTestId(/^pool-row-/)).not.toBeInTheDocument();
  });

  it('shows the COD and overdue flags the snapshot provides', () => {
    render(<PoolPanel {...baseProps} />);
    const codRow = screen.getByTestId('pool-row-101');
    expect(within(codRow).getByText('COD')).toBeInTheDocument();
    expect(within(codRow).queryByText('Overdue')).not.toBeInTheDocument();

    const overdueRow = screen.getByTestId('pool-row-102');
    expect(within(overdueRow).getByText('Overdue')).toBeInTheDocument();
    expect(within(overdueRow).queryByText('COD')).not.toBeInTheDocument();
  });

  it('assigning a pool stop to a driver emits onAssign with the exact ids', async () => {
    const onAssign = vi.fn();
    render(<PoolPanel {...baseProps} onAssign={onAssign} />);
    fireEvent.click(screen.getByTestId('pool-assign-101'));
    const target = await screen.findByText('Bek');
    fireEvent.click(target);
    expect(onAssign).toHaveBeenCalledWith(101, 6);
    expect(onAssign).toHaveBeenCalledTimes(1);
  });

  it('disables the assign control while an assignment is in flight', () => {
    render(<PoolPanel {...baseProps} assigning />);
    expect(screen.getByTestId('pool-assign-101')).toBeDisabled();
  });

  it('highlights the row matching selectedDeliveryId and no other', () => {
    render(<PoolPanel {...baseProps} selectedDeliveryId={102} />);
    expect(screen.getByTestId('pool-row-102')).toHaveAttribute('data-selected', 'true');
    expect(screen.getByTestId('pool-row-101')).toHaveAttribute('data-selected', 'false');
  });

  // Before the first shift starts, pooled work can genuinely sit there with
  // nobody on shift — unlike a driver route (which implies at least one
  // driver exists), the pool is the first place an empty roster is actually
  // reachable. An enabled button opening an empty Dropdown menu would look
  // identical to a broken control, so it must be disabled instead.
  it('disables the assign control when no drivers are available and does not emit onAssign', () => {
    const onAssign = vi.fn();
    render(<PoolPanel {...baseProps} drivers={[]} onAssign={onAssign} />);
    const button = screen.getByTestId('pool-assign-101');
    expect(button).toBeDisabled();
    fireEvent.click(button);
    expect(onAssign).not.toHaveBeenCalled();
  });

  // Same flexbox squeeze DriverRoutePanel hits (see its test for the full
  // explanation): the text column and the Assign control sit in the same
  // row, in the same left-hand panel column. jsdom does no real layout, so
  // this asserts the applied styling contract rather than rendered geometry
  // — the part that actually regresses if the fix is reverted.
  it('marks the assign control as non-shrinking and both text lines as truncating', () => {
    render(<PoolPanel {...baseProps} />);
    expect(screen.getByTestId('pool-assign-101').closest('span')).toHaveStyle({ flexShrink: '0' });
    const expectedTruncation = { whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' };
    expect(screen.getByText('B-1 · Dee')).toHaveStyle(expectedTruncation);
    expect(screen.getByText('Chilonzor 5')).toHaveStyle(expectedTruncation);
  });
});
