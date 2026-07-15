import { useEffect } from 'react';
import { useMap } from 'react-leaflet';
import L from 'leaflet';
import 'leaflet.heat';

/**
 * Renders a Leaflet heat layer for the given points on the enclosing map.
 * points: array of [lat, lng, intensity].
 */
// Module-level default so the effect deps stay referentially stable (a fresh
// inline `{}` each render would tear down + rebuild the heat layer on every
// keystroke in a threshold/filter input).
const DEFAULT_OPTS = { radius: 25, blur: 15, maxZoom: 17 };

const HeatLayer = ({ points = [], options = DEFAULT_OPTS }) => {
  const map = useMap();
  useEffect(() => {
    if (!map) return undefined;
    const layer = L.heatLayer(points, { ...DEFAULT_OPTS, ...options });
    layer.addTo(map);
    return () => { map.removeLayer(layer); };
  }, [map, points, options]);
  return null;
};

export default HeatLayer;
