import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { describe, it, expect, vi } from 'vitest';

// Mock react-leaflet primitives to simple divs so jsdom can render them.
vi.mock('react-leaflet', () => ({
  MapContainer: ({ children }) => <div data-testid="map">{children}</div>,
  TileLayer: () => <div data-testid="tiles" />,
  CircleMarker: ({ children, pathOptions }) => (
    <div data-testid="pin" data-color={pathOptions?.fillColor}>{children}</div>
  ),
  Popup: ({ children }) => <div data-testid="popup">{children}</div>,
  Polygon: () => <div data-testid="polygon" />,
  useMap: () => ({}),
}));
vi.mock('./HeatLayer', () => ({ default: () => <div data-testid="heat" /> }));

vi.mock('../services/adminService', () => ({
  default: {
    getCustomerMapPins: vi.fn().mockResolvedValue({
      data: { pins: [
        { addressId: 1, userId: 1, fullName: 'Recent Guy', phone: '+998900000001',
          userType: 'individual', entitySubtype: null, lat: 41.31, lng: 69.28,
          isDefault: true, addressLabel: 'A1', addressIndex: 1, addressCount: 1,
          lastOrderDate: new Date().toISOString(), orderCount: 3,
          bottleBalance: 2, outstandingDebt: 0, activeCodDebtCount: 0, codRestricted: false },
        { addressId: 2, userId: 2, fullName: 'Idle Guy', phone: '+998900000002',
          userType: 'entity', entitySubtype: 'grocery_store', lat: 41.32, lng: 69.29,
          isDefault: true, addressLabel: 'A2', addressIndex: 1, addressCount: 1,
          lastOrderDate: '2026-01-01T00:00:00Z', orderCount: 1,
          bottleBalance: 0, outstandingDebt: 9000, activeCodDebtCount: 1, codRestricted: false },
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
    await waitFor(() => expect(screen.getAllByTestId('pin')).toHaveLength(2));
    expect(adminService.getCustomerMapPins).toHaveBeenCalled();
  });

  it('idle-filter reduces the rendered pins', async () => {
    renderMap();
    await waitFor(() => expect(screen.getAllByTestId('pin')).toHaveLength(2));
    // Idle >= 60 days: only the Jan pin remains.
    fireEvent.change(screen.getByLabelText('idle minimum days'), { target: { value: '60' } });
    await waitFor(() => expect(screen.getAllByTestId('pin')).toHaveLength(1));
  });

  it('"View full profile" calls onViewUser with the userId', async () => {
    const onViewUser = vi.fn();
    renderMap({ onViewUser });
    await waitFor(() => expect(screen.getAllByTestId('pin')).toHaveLength(2));
    fireEvent.click(screen.getAllByText(/view full profile/i)[0]);
    expect(onViewUser).toHaveBeenCalledWith(1);
  });

  it('switching to Heatmap renders the heat layer', async () => {
    renderMap();
    await waitFor(() => expect(screen.getAllByTestId('pin')).toHaveLength(2));
    fireEvent.click(screen.getByRole('radio', { name: /heatmap/i }));
    await waitFor(() => expect(screen.getByTestId('heat')).toBeInTheDocument());
  });
});
