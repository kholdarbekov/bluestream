"""Stop time: the minutes one leg of a car's day takes (honest-ETA Phase 2 plan, R4 and R5).

Two prices, by where the leg leaves from. Both take the parameters they price with (the latest fit's, or the seeds':
`params.EtaParams`), so a fitted curve and the seed curve are priced by the same code. `km` is the straight-line
distance between the two ends, from `helpers.calculate_distance`.

- `cycle_minutes`: a leg between two stops. The curve is piecewise linear through its (km, minutes) points, which
  already hold the driving, the parking and the handover because they were measured on real deliveries. Below the first
  point it is that point's value; beyond the last it carries on at the last segment's slope.
- `depot_leg_minutes`: any leg that leaves the depot, `a + b x km`. The curve is fitted to legs of 13.2 km or less and
  says about 133 minutes at the production depot, 28.9 km from the centre, where loading to the first delivery takes 33
  to 119. So the plan never prices a depot leg with it (R4).

Standard library only: the planner calls these for every candidate placement.
"""

from itertools import pairwise
from typing import Sequence, Tuple

CurvePoint = Tuple[float, float]  # (straight-line km, minutes)


def cycle_minutes(km: float, points: Sequence[CurvePoint]) -> float:
    """Minutes from one stop to the next, `km` apart. `points` runs by rising km and holds at least two."""
    first_km, first_minutes = points[0]
    if km <= first_km:
        return float(first_minutes)
    segments = list(pairwise(points))
    # The segment that holds km; beyond the last point, the last segment, so its slope carries on.
    (km0, minutes0), (km1, minutes1) = next((segment for segment in segments if km <= segment[1][0]), segments[-1])
    return minutes0 + (minutes1 - minutes0) * (km - km0) / (km1 - km0)


def depot_leg_minutes(km: float, line: Sequence[float]) -> float:
    """Minutes from the depot to a stop `km` away, or back: `a + b x km` for the line `(a, b)`."""
    a, b = line
    return float(a + b * km)
