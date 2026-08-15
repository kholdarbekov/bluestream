import { render, screen, within } from '@testing-library/react';
import { describe, it, expect, vi } from 'vitest';
import DriverRoutePanel from './DriverRoutePanel';

/**
 * What a stop card has to tell a dispatcher without opening the order:
 * which order it is, whose it is, where it goes, what's in it, and how far
 * the driver has to travel to reach it.
 *
 * The leg tests carry most of the weight here, because a distance shown
 * against the wrong stop is worse than no distance at all — and the API
 * deliberately publishes the stop each leg arrives at rather than letting
 * the UI zip two arrays and hope they line up.
 */

const STOPS = [
  {
    delivery_id: 11, position: 0, order_number: 'A-1', customer_name: 'Ann',
    address_label: 'Chilonzor', pinned: false, delivery_status: 'assigned',
    items: [{ product_id: 1, product_name: 'Pure Water 19L', quantity: 2, is_reward: false }],
    items_total_count: 1, items_hidden_count: 0,
  },
  {
    delivery_id: 22, position: 1, order_number: 'A-2', customer_name: 'Bob',
    address_label: 'Yunusobod', pinned: false, delivery_status: 'assigned',
    items: [
      { product_id: 1, product_name: 'Pure Water 19L', quantity: 1, is_reward: false },
      { product_id: 2, product_name: 'Cup', quantity: 3, is_reward: false },
    ],
    items_total_count: 5, items_hidden_count: 3,
  },
];

const baseProps = {
  route: { driver_id: 5, manual_override: false, total_distance_km: 18.2, estimated_duration_minutes: 62 },
  drivers: [{ driver_id: 5, full_name: 'Ali' }, { driver_id: 6, full_name: 'Bek' }],
  stops: STOPS,
  pinned: {},
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

describe('DriverRoutePanel stop card content', () => {
  it('shows the order number and customer name on the stop', () => {
    render(<DriverRoutePanel {...baseProps} />);
    const row = screen.getByTestId('stop-row-11');
    expect(within(row).getByText(/A-1/)).toBeInTheDocument();
    expect(within(row).getByText(/Ann/)).toBeInTheDocument();
  });

  it('shows the order items with their quantities', () => {
    render(<DriverRoutePanel {...baseProps} />);
    expect(screen.getByTestId('stop-items-11')).toHaveTextContent('Pure Water 19L ×2');
  });

  it('says how many item lines it is not showing', () => {
    render(<DriverRoutePanel {...baseProps} />);
    expect(screen.getByTestId('stop-items-22')).toHaveTextContent('+3');
  });

  it('renders no items line for an order with no items', () => {
    const stops = [{ ...STOPS[0], items: [], items_total_count: 0, items_hidden_count: 0 }];
    render(<DriverRoutePanel {...baseProps} stops={stops} />);
    expect(screen.queryByTestId('stop-items-11')).not.toBeInTheDocument();
  });

  it('survives a stop payload that predates the items field', () => {
    // Cached/older responses carry no `items` key at all. A card that assumed
    // an array here would crash the whole dispatch board rather than lose one
    // line of detail.
    const stops = [{ delivery_id: 11, position: 0, order_number: 'A-1', customer_name: 'Ann', address_label: 'X' }];
    render(<DriverRoutePanel {...baseProps} stops={stops} />);
    expect(screen.getByTestId('stop-row-11')).toBeInTheDocument();
    expect(screen.queryByTestId('stop-items-11')).not.toBeInTheDocument();
  });
});

describe('DriverRoutePanel inter-stop legs', () => {
  const legs = [
    { distance_km: 4.2, duration_minutes: 11 },
    { distance_km: 1.8, duration_minutes: 5 },
  ];

  it('shows the travel distance and time to each stop', () => {
    render(<DriverRoutePanel {...baseProps} legs={legs} legDeliveryIds={[11, 22]} />);
    expect(screen.getByTestId('stop-leg-11')).toHaveTextContent('4.2 km');
    expect(screen.getByTestId('stop-leg-11')).toHaveTextContent('11 min');
    expect(screen.getByTestId('stop-leg-22')).toHaveTextContent('1.8 km');
  });

  it('attributes each leg by the published stop id, not by position', () => {
    // The API drops ungeocoded stops before measuring, so the leg list can be
    // shorter than the stop list and the ids are the ONLY correct pairing.
    // Here leg[1] belongs to stop 22 even though stop 22 is at index 1 of a
    // longer list that includes an unmeasured stop.
    const stops = [
      STOPS[0],
      { ...STOPS[0], delivery_id: 99, order_number: 'A-9', customer_name: 'Ungeocoded', position: 1 },
      { ...STOPS[1], position: 2 },
    ];
    render(<DriverRoutePanel {...baseProps} stops={stops} legs={legs} legDeliveryIds={[11, 22]} />);

    expect(screen.getByTestId('stop-leg-22')).toHaveTextContent('1.8 km');
    expect(screen.queryByTestId('stop-leg-99')).not.toBeInTheDocument();
  });

  it('shows nothing when the provider measured no legs', () => {
    // Suppression, not estimation: a straight-line guess rendered in the same
    // place as a measured figure is indistinguishable from one.
    render(<DriverRoutePanel {...baseProps} legs={null} legDeliveryIds={[]} />);
    expect(screen.queryByTestId('stop-leg-11')).not.toBeInTheDocument();
    expect(screen.queryByTestId('stop-leg-22')).not.toBeInTheDocument();
  });

  it('shows nothing when legs were never supplied', () => {
    render(<DriverRoutePanel {...baseProps} />);
    expect(screen.queryByTestId('stop-leg-11')).not.toBeInTheDocument();
  });

  it('drops a leg mapping that is longer than the legs it describes', () => {
    // Defensive: mismatched lengths mean the pairing is unknowable, so no
    // stop gets a number rather than some stops getting the wrong one.
    render(<DriverRoutePanel {...baseProps} legs={[legs[0]]} legDeliveryIds={[11, 22]} />);
    expect(screen.queryByTestId('stop-leg-11')).not.toBeInTheDocument();
    expect(screen.queryByTestId('stop-leg-22')).not.toBeInTheDocument();
  });
});
