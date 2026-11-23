"""Test geometry for the duplicate-address guard: one base pin, and points a known number of metres from it.

Offsets use geopy's geodesic, the measure `business_app.utils.helpers.calculate_distance` uses, so a point built
`d` metres away is `d` metres away to the guard too (spec 2026-11-18 §6.12: real offsets around one base pin).
The guard's rules, address-change and suggestion tests all build their points here.
"""

from typing import Tuple

from geopy.distance import geodesic

# The base pin: the `user_address` fixture's point (tests/conftest.py).
BASE: Tuple[float, float] = (41.3111, 69.2797)


def at(north_m: float = 0.0, east_m: float = 0.0, *, origin: Tuple[float, float] = BASE) -> Tuple[float, float]:
    """The point `north_m` metres north and then `east_m` metres east of `origin`; negative values go south or west.
    A point offset along one axis only is exactly that many metres from `origin`."""
    point = geodesic(meters=abs(north_m)).destination(origin, 0 if north_m >= 0 else 180)
    point = geodesic(meters=abs(east_m)).destination(point, 90 if east_m >= 0 else 270)
    return (point.latitude, point.longitude)
