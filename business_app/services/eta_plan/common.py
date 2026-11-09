"""The ETA math both stages share: the S2 tap promises of `delivery_eta_service` and the S1 engine of `eta_plan`.

`moved_into`, `minutes_between` and `percentile` were private to `delivery_eta_service` (`_moved_into`, `_minutes`,
`_p90`) and live here so the engine reads history, durations and quantiles by the same code.

Imports only the standard library and SQLAlchemy, and must stay that way: `notification_service` imports
`delivery_eta_service` at module level and `delivery_eta_service` imports this module, so a service imported here could
close an import cycle at start-up.
"""

from datetime import datetime
from enum import Enum
from fractions import Fraction
from typing import Iterable

from sqlalchemy import and_


def moved_into(history, status: Enum):
    """A real move into `status` on `DeliveryStatusHistory` (or an alias of it). A reassign row keeps old == new."""
    return and_(history.new_status == status, history.old_status != status)


def minutes_between(start: datetime, end: datetime) -> float:
    return (end - start).total_seconds() / 60.0


def percentile(values: Iterable[float], q: float) -> float:
    """The `q` quantile of `values` (`q` from 0 to 1), interpolated linearly between the two nearest ranks of the sorted
    values: Postgres's `percentile_cont(q)` (the ETA analysis SQL's own), and the cut points of
    `statistics.quantiles(..., method="inclusive")`.

    It returns what `statistics.quantiles` returns to the last bit, which Phase 1's `_p90` (`n=10`, the last cut) relied
    on. `q` is read as the decimal it was written as (0.9 is 9/10), so the interpolation is whole-number arithmetic and
    one division, as `statistics` does it. `lower + (upper - lower) * fraction` in floats differs from that in the last
    digit for about a third of inputs. One value returns that value; no values raise ValueError.
    """
    ordered = sorted(values)
    if not ordered:
        raise ValueError("percentile of no values")
    if not 0 <= q <= 1:
        raise ValueError(f"q must be between 0 and 1, not {q!r}")
    if len(ordered) == 1:
        return ordered[0]
    last = len(ordered) - 1
    ratio = Fraction(str(q))
    below, remainder = divmod(ratio.numerator * last, ratio.denominator)
    above = min(below + 1, last)  # past the last rank only when q is 1, where the weight on it is 0
    return (ordered[below] * (ratio.denominator - remainder) + ordered[above] * remainder) / ratio.denominator
