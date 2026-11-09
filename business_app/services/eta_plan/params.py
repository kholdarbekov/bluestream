"""The parameters the engine plans with: the latest weekly fit that exists at `now`, else the seeds (honest-ETA Phase 2
plan, R5, R11 and R13).

`EtaParams` is all the planner, the window rules and the recorder read of a fit. `current_params(now)` builds it from
ONE `eta_model_fits` row, because every fit row is complete: a component the fit kept is copied from the previous fit,
or from the seeds (the row's shape is documented on `models/eta_model_fit.py`). With no fit yet, the seeds of
`shared.business_config` stand in, with no window buckets, so every window reads its own seed (`windows.lo_hi`).

`source` says which: "seed", or "fit:<id>". The recorder stamps it on every engine row, so the accuracy gate can tell
rows planned with a fit from rows planned before one existed.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Optional, Tuple

from business_app.models.eta_model_fit import EtaModelFit
from business_app.utils.timezone_utils import ensure_utc
from shared import business_config

SEED_SOURCE = "seed"


@dataclass(frozen=True)
class EtaParams:
    """What a plan is priced with.

    - `cycle_points`: the stop curve's (km, minutes) points by rising km (`stop_time.cycle_minutes`).
    - `depot_line`: `(a, b)` of the depot leg, `a + b x km` minutes (`stop_time.depot_leg_minutes`).
    - `window_buckets`: `"<band>|started"` or `"<band>|not_started"` to `{"lo", "hi", "n"}`, as the fit stored it. lo
      and hi are None while n is under ETA_WINDOW_MIN_PROMISES, and the seeds have no buckets at all (`windows.lo_hi`).
    - `source`: "seed" or "fit:<id>".
    """

    cycle_points: Tuple[Tuple[float, float], ...]
    depot_line: Tuple[float, float]
    window_buckets: Dict[str, Dict[str, Optional[int]]]
    source: str


def latest_fit(now: datetime) -> Optional[EtaModelFit]:
    """The fit the engine reads at `now`: the latest with `fitted_at <= now` (the newest row among equal times), or
    None before the first. The accuracy report shows the same one."""
    return (
        EtaModelFit.query.filter(EtaModelFit.fitted_at <= ensure_utc(now))
        .order_by(EtaModelFit.fitted_at.desc(), EtaModelFit.id.desc())
        .first()
    )


def current_params(now: datetime) -> EtaParams:
    """The parameters of `latest_fit(now)`, else the seeds."""
    fit = latest_fit(now)
    if fit is None:
        return EtaParams(
            cycle_points=business_config.ETA_CYCLE_SEED_POINTS,
            depot_line=business_config.ETA_DEPOT_LEG_SEED,
            window_buckets={},
            source=SEED_SOURCE,
        )
    stored = fit.params
    intercept, slope = stored["depot_line"]
    return EtaParams(
        cycle_points=tuple((km, minutes) for km, minutes in stored["cycle_points"]),
        depot_line=(intercept, slope),
        # Copied, so the immutable value shares nothing with the row's JSON.
        window_buckets={key: dict(bucket) for key, bucket in stored["window_buckets"].items()},
        source=f"fit:{fit.id}",
    )
