import { render, screen } from '@testing-library/react';
import { describe, it, expect, vi } from 'vitest';
import PoolPanel from './PoolPanel';

/**
 * The same delivery sits in the pool before assignment and on a route after
 * it. If only one of the two panels showed its contents, a dispatcher's view
 * of what they are loading would change the moment they assigned it — so the
 * pool row renders the item line exactly as a stop row does.
 */

const STOPS = [
  {
    delivery_id: 9, order_number: 'AD_000641_26', customer_name: 'Zafar',
    address_label: 'Izzat Street', is_cod: true,
    items: [{ product_id: 1, product_name: 'Pure Water 19L', quantity: 2, is_reward: false }],
    items_total_count: 1, items_hidden_count: 0,
  },
];

const baseProps = {
  stops: STOPS,
  drivers: [{ driver_id: 5, full_name: 'Ali' }],
  assigning: false,
  onAssign: vi.fn(),
};

describe('PoolPanel item lines', () => {
  it('shows the order items with their quantities', () => {
    render(<PoolPanel {...baseProps} />);
    expect(screen.getByTestId('pool-items-9')).toHaveTextContent('Pure Water 19L ×2');
  });

  it('says how many item lines it is not showing', () => {
    const stops = [{ ...STOPS[0], items_total_count: 4, items_hidden_count: 3 }];
    render(<PoolPanel {...baseProps} stops={stops} />);
    expect(screen.getByTestId('pool-items-9')).toHaveTextContent('+3');
  });

  it('renders no items line for an order with no items', () => {
    const stops = [{ ...STOPS[0], items: [], items_total_count: 0, items_hidden_count: 0 }];
    render(<PoolPanel {...baseProps} stops={stops} />);
    expect(screen.queryByTestId('pool-items-9')).not.toBeInTheDocument();
  });

  it('survives a pool payload that predates the items field', () => {
    const stops = [{ delivery_id: 9, order_number: 'AD_1', customer_name: 'Zafar', address_label: 'X' }];
    render(<PoolPanel {...baseProps} stops={stops} />);
    expect(screen.getByTestId('pool-row-9')).toBeInTheDocument();
    expect(screen.queryByTestId('pool-items-9')).not.toBeInTheDocument();
  });

  it('keeps showing the assign control alongside the items', () => {
    // The control lives at the far end of the row; a wider item line must not
    // be able to push it out of the card.
    render(<PoolPanel {...baseProps} />);
    expect(screen.getByTestId('pool-assign-9')).toBeInTheDocument();
  });
});
