"""Decoder for the Google Encoded Polyline Algorithm Format.

Google's Directions API (`routes[].overview_polyline.points`) and OSRM's
`geometries=polyline` route geometry (`routes[].geometry`) both use this
exact encoding — see
https://developers.google.com/maps/documentation/utilities/polylinealgorithm.
Both default to precision 5 (1 unit = 1e-5 degrees): Google's
`overview_polyline` is always precision 5, and OSRM only switches to
precision 6 when explicitly asked for `geometries=polyline6`, which this
codebase never requests (`maps_service.py` sends `geometries=polyline`).
So a single `precision=5` default is correct for every caller here.

Hand-rolled rather than pulled from PyPI's `polyline` package: this feature
may not add new pip dependencies, and the algorithm is short and has been
stable since Google published it in 2008.

Output coordinate order is `[latitude, longitude]` for BOTH providers —
confirmed for OSRM specifically since its raw request coordinates are
`lon,lat` (the opposite order), which would otherwise be an easy transcribe
mistake: OSRM's own docs describe `RouteStep.geometry` as "polyline with
precision 5 in [latitude,longitude] encoding", i.e. it re-orders to match
Google's convention rather than keeping its request-side lon,lat order.
"""

import logging
from typing import List, Optional

logger = logging.getLogger(__name__)


def decode_polyline(encoded: Optional[str], precision: int = 5) -> Optional[List[List[float]]]:
    """Decode an encoded polyline string into `[[lat, lng], ...]`.

    Returns `None` — never `[]` — for empty, missing, or malformed input, so
    every caller can rely on a single `is None` check. An empty list is
    truthy in both Python and JS, and treating it as "real geometry" is
    exactly the bug `OperationsMap.jsx`'s `hasRealGeometry` check already
    guards against on the frontend; this keeps the same guarantee upstream,
    at the source, for every provider.

    Malformed input (truncated string, a stray non-invented value some
    provider quirk hands back) is caught and logged rather than raised: a
    bad decode must degrade to the honest dashed-line fallback, not break
    the whole geometry endpoint.
    """
    if not encoded:
        return None

    coordinates: List[List[float]] = []
    index = 0
    lat = 0
    lng = 0
    factor = 10**precision
    length = len(encoded)

    try:
        while index < length:
            for is_longitude in (False, True):
                shift = 0
                result = 0
                while True:
                    byte = ord(encoded[index]) - 63
                    index += 1
                    result |= (byte & 0x1F) << shift
                    shift += 5
                    if byte < 0x20:
                        break
                delta = ~(result >> 1) if (result & 1) else (result >> 1)
                if is_longitude:
                    lng += delta
                else:
                    lat += delta
            # Round to one digit past the encoding's own precision: this
            # only strips binary floating-point noise from the division
            # (e.g. 41.300000000000004) — it never discards real precision,
            # since the encoding itself can't represent more than
            # `precision` decimal digits to begin with.
            coordinates.append([round(lat / factor, precision + 1), round(lng / factor, precision + 1)])
    except (IndexError, ValueError) as exc:
        logger.warning("Failed to decode polyline (malformed input, len=%d): %s", length, exc)
        return None

    return coordinates or None
