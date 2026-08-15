import { render } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';

/**
 * Panning to a stop picked in a side panel.
 *
 * `setView` rather than `flyTo`, and the CURRENT zoom rather than a chosen
 * one: the admin sets the zoom to the working scale they want (one district,
 * or the whole city), and a focus gesture that also rezoomed would keep
 * undoing that.
 */

const setView = vi.fn();

vi.mock('react-leaflet', async () => {
  const React = await import('react');
  return {
    MapContainer: ({ children }) => React.createElement('div', { 'data-testid': 'map' }, children),
    TileLayer: () => null,
    CircleMarker: () => null,
    Marker: () => null,
    Polyline: () => null,
    Popup: () => null,
    useMap: () => ({ setView, getZoom: () => 13 }),
  };
});

vi.mock('./HeatLayer', () => ({ default: () => null }));

const renderMap = async (props) => {
  const { default: OperationsMap } = await import('./OperationsMap');
  render(<OperationsMap {...props} />);
};

beforeEach(() => vi.clearAllMocks());

describe('OperationsMap focusPoint', () => {
  it('pans to the requested point at the current zoom', async () => {
    await renderMap({ focusPoint: [41.31, 69.25] });
    expect(setView).toHaveBeenCalledWith([41.31, 69.25], 13);
  });

  it('does nothing when no point is requested', async () => {
    await renderMap({ focusPoint: null });
    expect(setView).not.toHaveBeenCalled();
  });

  it('ignores a point that is not a usable coordinate pair', async () => {
    // A stop with no geocode reaches this as [null, null]; panning to it
    // would throw the admin somewhere off the map with no way back.
    await renderMap({ focusPoint: [null, null] });
    expect(setView).not.toHaveBeenCalled();
  });
});
