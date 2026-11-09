"""The weekly fit: what the last 28 plan days of real deliveries say about stop times and promise windows (honest-ETA
Phase 2 plan, R5, R13 and R14; spec 4.10).

`EtaCalibration().fit(now)` reads the ETA_FIT_WINDOW_DAYS plan days before the plan day `now` is in, and inserts ONE
`eta_model_fits` row: the half-open window `[data_from, data_to)` between 05:00 local instants, with `data_to` unique,
so a second run for the same window inserts nothing. The row is complete (Ruling P4, `models/eta_model_fit.py`): what a
component could not learn is copied from the previous fit, the latest with `fitted_at <= now` (`params.current_params`),
or from the seeds. The engine reads the row back through `current_params`; this module is its one writer.

- Stops (R14). A driver's plan day is the real moves into DELIVERED (`moved_into`) of every delivery assigned to the
  driver (`Delivery.delivery_person_id`), in time order, whoever made them. Only a tap of the delivery's own driver is a
  reliable stop. A delivered row with no actor (the Orders screen closes a delivery with none, and its time is the
  click), or by anyone else, keeps its place in the order and is counted, but is not reliable. A delivered -> delivered
  row (an assignment re-saved) is no move at all, and is counted. `actual_arrivals` is R14's one definition of an actual
  arrival.
- The stop curve (R5). A leg is the minutes between two consecutive stops of a plan day against the straight-line km
  between their doors. It forms only when both stops are reliable: an unreliable stop breaks the chain, so there is no
  leg into it, out of it or across it (a leg across it would put its handover time on the distance between its
  neighbours). A bin of `bin_index` with ETA_FIT_MIN_LEGS_PER_BIN legs or more becomes the point (median km, median
  minutes); a thinner bin keeps the previous point of its place. A curve whose minutes fall as km grows, or whose last
  segment is faster than ETA_FIT_MAX_KMH, is rejected whole.
- The depot line (R4). Each (driver, plan day) gives a pair: the first real pick-up to the day's first delivered stop,
  with the km from the depot to that stop. ETA_FIT_MIN_DEPOT_PAIRS pairs or more are fitted by least squares; the line
  must have `a >= 0` and `b >= 0`, or the previous one stands.
- The window buckets (R11). Each engine row of the window whose delivery has an actual arrival is one sample, (arrival
  - expected), in the row's own bucket (`windows.bucket_key`). A bucket stores its n; its lo and hi, the 10th and 90th
  percentiles, only with ETA_WINDOW_MIN_PROMISES samples. A thinner bucket borrows when it is read, so it is never
  "kept".
- Monitoring (spec 4.5), reported and never read by the engine: each leg's minutes over the curve the row holds, as the
  median per two-hour band of the local time it was delivered (11:00 to 24:00) and per band of the bottles of the order
  it ended at.

A tap, leg or pair that cannot be trusted is left out and counted by reason (`EXCLUDED_REASONS`, in
`rows_used["excluded"]`):

- a stop that is not the delivery's own driver's tap (no actor, or someone else), or whose pick-up and delivered taps
  are under ETA_FIT_MIN_PICKUP_TO_DELIVERED_MINUTES apart (tapped together, so its delivered time is not the
  handover), is unreliable: it keeps its place in the order and breaks the chain, and no depot pair ends at it;
- a leg with a load of the driver's between its two delivered taps, a session opened with bottles (`supply.loads_in`,
  the one definition of a load): the plan prices a reload separately (R6), so the curve must not carry it too;
- a leg or pair of 1 km or more faster than ETA_FIT_MAX_KMH, a leg with no minutes included;
- a leg or pair with a door that has no coordinates.

`rows_used` holds the legs, depot pairs and window samples the components were read from, under `"cycle"`, `"depot"`
and `"window"`, and the rows left out under `"excluded"`. `kept_previous` maps `"bin:<i>"` (a thin bin), `"cycle"` (a
rejected curve) and `"depot"` to the reason the previous value stands.

Straight-line km come only from `calculate_distance`, the depot from `get_warehouse_coordinates()`. No routing provider,
geocoder or Google is called.
"""

import logging
from bisect import bisect_right
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from itertools import pairwise
from statistics import StatisticsError, linear_regression
from typing import Dict, Iterable, List, NamedTuple, Optional, Sequence, Tuple

from flask import current_app
from sqlalchemy import func, not_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from business_app import db
from business_app.models.delivery import Delivery, DeliveryStatusHistory
from business_app.models.delivery_promise import DeliveryPromise
from business_app.models.eta_model_fit import EtaModelFit
from business_app.models.order import Order, OrderItem
from business_app.models.user import UserAddress
from business_app.services.bottle_tracking_service import BottleTrackingService
from business_app.services.eta_plan.common import minutes_between, moved_into, percentile
from business_app.services.eta_plan.params import current_params
from business_app.services.eta_plan.scope import engine_rows_filter
from business_app.services.eta_plan.stop_time import CurvePoint, cycle_minutes
from business_app.services.eta_plan.supply import loads_in
from business_app.services.eta_plan.windows import bucket_key
from business_app.utils.helpers import calculate_distance, get_warehouse_coordinates
from business_app.utils.local_windows import work_day_bounds, work_day_of
from business_app.utils.timezone_utils import ensure_utc, utc_to_local
from shared import business_config
from shared.constants import DISPLAY_TIMEZONE
from shared.enums import DeliveryStatus

logger = logging.getLogger(__name__)

Point = Tuple[float, float]

# Why a tap, a leg or a pair is left out of the fit. `rows_used["excluded"]` counts each.
NO_ACTOR = "no_actor"  # a delivered row with no actor: closed from the Orders screen
NOT_THE_DRIVERS_TAP = "not_the_drivers_tap"  # a delivered row by someone other than the delivery's driver
REASSIGN = "reassign"  # a delivered -> delivered row: an assignment re-saved, not a delivery
SHORT_HANDOVER = "short_handover"  # picked up and delivered under ETA_FIT_MIN_PICKUP_TO_DELIVERED_MINUTES apart
RELOAD = "reload"  # a leg with a load of the driver's between its two delivered taps: the plan prices a reload apart
PICKUP_AFTER_DELIVERY = "pickup_after_delivery"  # the day's first real pick-up is tapped after its first delivery
TOO_FAST = "too_fast"  # a leg or pair of 1 km or more faster than ETA_FIT_MAX_KMH
NO_POINT = "no_point"  # a leg or pair with a door that has no coordinates
EXCLUDED_REASONS = (
    NO_ACTOR,
    NOT_THE_DRIVERS_TAP,
    REASSIGN,
    SHORT_HANDOVER,
    PICKUP_AFTER_DELIVERY,
    RELOAD,
    TOO_FAST,
    NO_POINT,
)

# Spec 4.10: only a leg of a kilometre or more is checked against the speed limit. Over a shorter one the straight-line
# km are too small to tell a quick stop from a tap made late.
_SPEED_CHECK_FROM_KM = 1.0

# The window's lo and hi are these quantiles of (arrival - expected): the 10th and 90th percentiles (spec 4.6).
_WINDOW_LO_Q, _WINDOW_HI_Q = 0.10, 0.90

# Monitoring's bands (spec 4.5): the local two-hour bands from 11:00 to midnight (the last, 23-24, is one hour) and the
# bottles of the order a leg ended at, as (label, most bottles).
_FIRST_HOUR, _LAST_HOUR, _BAND_HOURS = 11, 24, 2
_BOTTLE_BANDS = (("0-5", 5), ("6-10", 10), ("11-20", 20), ("21-40", 40))


def bin_index(km: float) -> int:
    """The fit bin (R5) a leg of `km` falls in, 0 to 5: [0, 0.2) [0.2, 1) [1, 3) [3, 6) [6, 10) [10, inf). Each bin
    holds one point of the curve, in bin order."""
    return bisect_right(business_config.ETA_CYCLE_BIN_EDGES_KM, km)


class _Tap(NamedTuple):
    """A history row of the window with the delivery's driver and the order's door. `at` is aware UTC."""

    delivery_id: int
    order_id: int
    driver_id: Optional[int]  # `Delivery.delivery_person_id`: the delivery's driver now
    actor_id: Optional[int]  # `changed_by`: who made the move
    at: datetime
    point: Optional[Point]  # the door as (lat, lng), None without coordinates


class _Stop(NamedTuple):
    """A real delivered move of a delivery assigned to the driver. `reliable` is True only for the driver's own tap
    that was not made together with its pick-up; any other keeps its place in the day and breaks the chain."""

    tap: _Tap
    reliable: bool


class _Day(NamedTuple):
    """One driver's plan day: the time of their first real pick-up, if any, and their stops in time order."""

    first_pickup: Optional[datetime]
    stops: List[_Stop]


class _Leg(NamedTuple):
    """The time between two consecutive stops of a plan day against the km between their doors. `end` is the tap of
    the stop it ends at."""

    km: float
    minutes: float
    end: _Tap


class EtaCalibration:
    """Fits the engine's learned parameters from the history, once per window of plan days."""

    def fit(self, now: datetime) -> Optional[EtaModelFit]:
        """Insert the fit of the plan days before `now`'s, and return it; None when that window is already fitted.

        `now` is the one clock reading of the run and the row's `fitted_at`, so the engine reads the fit from then on.
        """
        now = ensure_utc(now)
        data_from, data_to = _window(now)
        if _already_fitted(data_to):
            logger.info("eta_plan_fit_skipped data_to=%s: the window is already fitted", data_to.isoformat())
            return None
        previous = current_params(now)
        excluded: Counter = Counter()
        days = _driver_days(data_from, data_to, excluded)
        loads = loads_in(sorted({driver_id for driver_id, _ in days}), (data_from, data_to))
        legs = _legs(days, loads, excluded)
        pairs = _depot_pairs(days, excluded)
        points, kept = _stop_curve(legs, previous.cycle_points)
        line, kept_line = _depot_line(pairs, previous.depot_line)
        buckets, samples = _window_buckets(data_from, data_to)
        fit = EtaModelFit(
            fitted_at=now,
            data_from=data_from,
            data_to=data_to,
            params={
                "cycle_points": [list(point) for point in points],
                "depot_line": list(line),
                "window_buckets": buckets,
                "monitoring": _monitoring(legs, points),
            },
            rows_used={
                "cycle": len(legs),
                "depot": len(pairs),
                "window": samples,
                "excluded": {reason: excluded[reason] for reason in EXCLUDED_REASONS},
            },
            kept_previous={**kept, **kept_line},
        )
        db.session.add(fit)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            if not _already_fitted(data_to):
                raise  # not a second run of this window: some other rule refused the row
            logger.info("eta_plan_fit_skipped data_to=%s: a concurrent run committed first", data_to.isoformat())
            return None
        logger.info(
            "eta_plan_fitted id=%s data_to=%s cycle=%s depot=%s window=%s excluded=%s kept_previous=%s",
            fit.id,
            data_to.isoformat(),
            len(legs),
            len(pairs),
            samples,
            dict(+excluded),
            sorted(fit.kept_previous),
        )
        return fit


def actual_arrivals(delivery_ids: Iterable[int]) -> Dict[int, datetime]:
    """R14: each delivery's actual arrival, when it has one: the first real move into DELIVERED by the delivery's own
    driver, as aware UTC. A delivery closed with no actor or by anyone else, or never delivered, is missing."""
    ids = sorted(set(delivery_ids))
    if not ids:
        return {}
    arrivals: Dict[int, datetime] = {}
    for delivered in _taps(DeliveryStatus.DELIVERED, DeliveryStatusHistory.delivery_id.in_(ids)):
        if _not_the_drivers(delivered) is None:
            arrivals.setdefault(delivered.delivery_id, delivered.at)  # oldest first, so the first stands
    return arrivals


def _window(now: datetime) -> Tuple[datetime, datetime]:
    """`[data_from, data_to)` of a fit run at `now`: the ETA_FIT_WINDOW_DAYS plan days before `now`'s plan day, as UTC
    instants on plan-day boundaries (05:00 local)."""
    today = work_day_of(now)
    window_days = timedelta(days=current_app.config["ETA_FIT_WINDOW_DAYS"])
    return work_day_bounds(today - window_days)[0], work_day_bounds(today)[0]


def _already_fitted(data_to: datetime) -> bool:
    return db.session.query(EtaModelFit.id).filter(EtaModelFit.data_to == data_to).first() is not None


def _not_the_drivers(tap: _Tap) -> Optional[str]:
    """Why `tap` is not a move of the delivery's own driver (R14), or None when it is: NO_ACTOR or NOT_THE_DRIVERS_TAP.
    A delivery with no driver has no tap of its own driver."""
    if tap.actor_id is None:
        return NO_ACTOR
    if tap.actor_id != tap.driver_id:
        return NOT_THE_DRIVERS_TAP
    return None


def _taps(status: DeliveryStatus, *criteria) -> List[_Tap]:
    """The real moves into `status` (`moved_into`: a reassign row keeps old == new) that `criteria` select, oldest
    first, each with the door of its order."""
    rows = (
        db.session.query(
            DeliveryStatusHistory.delivery_id,
            Delivery.order_id,
            Delivery.delivery_person_id,
            DeliveryStatusHistory.changed_by,
            DeliveryStatusHistory.changed_at,
            UserAddress.latitude,
            UserAddress.longitude,
        )
        .join(Delivery, Delivery.id == DeliveryStatusHistory.delivery_id)
        .join(Order, Order.id == Delivery.order_id)
        .outerjoin(UserAddress, UserAddress.id == Order.delivery_address_id)
        .filter(moved_into(DeliveryStatusHistory, status), *criteria)
        .order_by(DeliveryStatusHistory.changed_at, DeliveryStatusHistory.id)
    )
    return [
        _Tap(
            delivery_id,
            order_id,
            driver_id,
            actor_id,
            ensure_utc(changed_at),
            (latitude, longitude) if latitude is not None and longitude is not None else None,
        )
        for delivery_id, order_id, driver_id, actor_id, changed_at, latitude, longitude in rows
    ]


def _driver_days(data_from: datetime, data_to: datetime, excluded: Counter) -> Dict[Tuple[int, date], _Day]:
    """Each driver's plan days of the window that had a delivered move, grouped in Python with `work_day_of`. The stops
    of a day are the real delivered moves of every delivery assigned to the driver, whoever made them (R14), in time
    order. A move that is not the driver's own, or was tapped together with its pick-up, keeps its place as an
    unreliable stop, and `_legs` lets it break the chain. Counts those (NO_ACTOR, NOT_THE_DRIVERS_TAP, SHORT_HANDOVER)
    and the delivered -> delivered rows (REASSIGN) in `excluded`. A delivery nobody holds is in no driver's day."""
    in_window = (DeliveryStatusHistory.changed_at >= data_from, DeliveryStatusHistory.changed_at < data_to)
    minimum = current_app.config["ETA_FIT_MIN_PICKUP_TO_DELIVERED_MINUTES"]
    pickups: Dict[int, List[datetime]] = defaultdict(list)  # a delivery's real pick-ups
    first_pickup: Dict[Tuple[int, date], datetime] = {}
    for pickup in _taps(DeliveryStatus.PICKED_UP, *in_window):
        if _not_the_drivers(pickup) is None:
            pickups[pickup.delivery_id].append(pickup.at)
            key = (pickup.driver_id, work_day_of(pickup.at))
            first_pickup[key] = min(pickup.at, first_pickup.get(key, pickup.at))
    stops: Dict[Tuple[int, date], List[_Stop]] = defaultdict(list)
    for delivered in _taps(DeliveryStatus.DELIVERED, *in_window):
        why_not = _not_the_drivers(delivered)
        if why_not is not None:
            excluded[why_not] += 1
            reliable = False
        else:
            picked_up_at = max((at for at in pickups[delivered.delivery_id] if at <= delivered.at), default=None)
            reliable = picked_up_at is None or minutes_between(picked_up_at, delivered.at) >= minimum
            if not reliable:
                excluded[SHORT_HANDOVER] += 1
        if delivered.driver_id is not None:
            stops[(delivered.driver_id, work_day_of(delivered.at))].append(_Stop(delivered, reliable))
    excluded[REASSIGN] += (
        db.session.query(func.count(DeliveryStatusHistory.id))
        .filter(
            DeliveryStatusHistory.new_status == DeliveryStatus.DELIVERED,
            not_(moved_into(DeliveryStatusHistory, DeliveryStatus.DELIVERED)),
            *in_window,
        )
        .scalar()
    )
    return {key: _Day(first_pickup.get(key), day_stops) for key, day_stops in stops.items()}


def _distance(
    origin: Optional[Point], destination: Optional[Point], minutes: float, excluded: Counter
) -> Optional[float]:
    """The straight-line km between two doors, or None when the leg must be left out, and counted: a door has no
    coordinates, or the leg is 1 km or more and faster than ETA_FIT_MAX_KMH. A leg of no minutes is faster than any."""
    if origin is None or destination is None:
        excluded[NO_POINT] += 1
        return None
    km = calculate_distance(*origin, *destination)
    if km >= _SPEED_CHECK_FROM_KM and (minutes <= 0 or km / (minutes / 60) > current_app.config["ETA_FIT_MAX_KMH"]):
        excluded[TOO_FAST] += 1
        return None
    return km


def _legs(days: Dict[Tuple[int, date], _Day], loads: Dict[int, List[datetime]], excluded: Counter) -> List[_Leg]:
    """The legs between consecutive stops of each plan day, `loads` being each driver's loads, oldest first.

    A leg forms only between two reliable stops: an unreliable one (already counted when it was read) breaks the chain,
    so no leg is made into it, out of it or across it. And only when none of the driver's loads falls between the two
    taps (RELOAD): the plan prices a reload apart, so the curve must not carry it too."""
    legs = []
    for (driver_id, _), day in days.items():
        driver_loads = loads.get(driver_id, ())
        for before, after in pairwise(day.stops):
            if not (before.reliable and after.reliable):
                continue
            if _load_between(driver_loads, before.tap.at, after.tap.at):
                excluded[RELOAD] += 1
                continue
            minutes = minutes_between(before.tap.at, after.tap.at)
            km = _distance(before.tap.point, after.tap.point, minutes, excluded)
            if km is not None:
                legs.append(_Leg(km, minutes, after.tap))
    return legs


def _load_between(loads: Sequence[datetime], start: datetime, end: datetime) -> bool:
    """Whether one of `loads` (oldest first) falls strictly between `start` and `end`."""
    first_after_start = bisect_right(loads, start)
    return first_after_start < len(loads) and loads[first_after_start] < end


def _depot_pairs(days: Dict[Tuple[int, date], _Day], excluded: Counter) -> List[Tuple[float, float]]:
    """Each plan day's (km from the depot to the first stop, minutes from the day's first real pick-up to that stop).
    A day whose first stop is unreliable (not the driver's own tap, or tapped together) gives no pair: the next stop's
    time would carry the first one's handover."""
    minimum = current_app.config["ETA_FIT_MIN_PICKUP_TO_DELIVERED_MINUTES"]
    depot = get_warehouse_coordinates()
    pairs = []
    for day in days.values():
        first = day.stops[0]
        if day.first_pickup is None or not first.reliable:
            continue
        minutes = minutes_between(day.first_pickup, first.tap.at)
        if minutes < 0:
            excluded[PICKUP_AFTER_DELIVERY] += 1
            continue
        if minutes < minimum:
            excluded[SHORT_HANDOVER] += 1
            continue
        km = _distance(depot, first.tap.point, minutes, excluded)
        if km is not None:
            pairs.append((km, minutes))
    return pairs


def _stop_curve(legs: Sequence[_Leg], previous: Sequence[CurvePoint]) -> Tuple[List[CurvePoint], Dict[str, str]]:
    """The curve's points and what was kept of `previous`. Bin i keeps point i while it has too few legs; a curve that
    is physically wrong is rejected whole."""
    minimum = current_app.config["ETA_FIT_MIN_LEGS_PER_BIN"]
    in_bin: Dict[int, List[_Leg]] = defaultdict(list)
    for leg in legs:
        in_bin[bin_index(leg.km)].append(leg)
    points: List[CurvePoint] = []
    kept: Dict[str, str] = {}
    for index, previous_point in enumerate(previous):
        members = in_bin[index]
        if len(members) < minimum:
            points.append(previous_point)
            kept[f"bin:{index}"] = f"thin: {len(members)} legs, {minimum} needed"
        else:
            points.append(
                (percentile([leg.km for leg in members], 0.5), percentile([leg.minutes for leg in members], 0.5))
            )
    problem = _curve_problem(points)
    if problem is not None:
        return list(previous), {**kept, "cycle": problem}
    return points, kept


def _curve_problem(points: Sequence[CurvePoint]) -> Optional[str]:
    """Why the curve is physically wrong (R5), or None: its minutes fall as km grows, or its last segment, whose slope
    prices every leg beyond the last point, is faster than ETA_FIT_MAX_KMH (a slope under 60 / ETA_FIT_MAX_KMH minutes
    per km)."""
    for (km, minutes), (next_km, next_minutes) in pairwise(points):
        if next_minutes < minutes:
            return (
                f"minutes fall with distance: {minutes:.1f} min at {km:.2f} km, then {next_minutes:.1f} min at "
                f"{next_km:.2f} km"
            )
    max_kmh = current_app.config["ETA_FIT_MAX_KMH"]
    (km_before, minutes_before), (km_last, minutes_last) = points[-2], points[-1]
    extra_minutes, extra_km = minutes_last - minutes_before, km_last - km_before
    if extra_km <= 0:
        return f"last two points do not move forward in distance: {km_before:.2f} km, then {km_last:.2f} km"
    if extra_minutes / extra_km < 60 / max_kmh:
        return f"last segment faster than {max_kmh} km/h: {extra_minutes:.1f} min over {extra_km:.2f} km"
    return None


def _depot_line(
    pairs: Sequence[Tuple[float, float]], previous: Tuple[float, float]
) -> Tuple[Tuple[float, float], Dict]:
    """`(a, b)` of the depot leg `a + b x km` and what was kept of `previous`: ETA_FIT_MIN_DEPOT_PAIRS pairs or more fit
    a line by least squares, which stands when neither term is negative."""
    minimum = current_app.config["ETA_FIT_MIN_DEPOT_PAIRS"]
    if len(pairs) < minimum:
        return previous, {"depot": f"thin: {len(pairs)} pairs, {minimum} needed"}
    try:
        line = linear_regression([km for km, _ in pairs], [minutes for _, minutes in pairs])
    except StatisticsError:  # every pair at one distance: no slope to find
        return previous, {"depot": "pairs all at one distance: no line to fit"}
    if line.intercept < 0 or line.slope < 0:
        return previous, {
            "depot": f"implausible: {line.intercept:.2f} min + {line.slope:.2f} min/km (neither may be negative)"
        }
    return (line.intercept, line.slope), {}


def _window_buckets(data_from: datetime, data_to: datetime) -> Tuple[Dict[str, Dict[str, Optional[int]]], int]:
    """The window buckets of `params["window_buckets"]` and how many samples fed them (R11, R13, R14)."""
    promises = (
        db.session.query(
            DeliveryPromise.delivery_id,
            DeliveryPromise.expected_at,
            DeliveryPromise.stops_ahead,
            DeliveryPromise.day_started,
        )
        .filter(
            engine_rows_filter(),
            DeliveryPromise.computed_at >= data_from,
            DeliveryPromise.computed_at < data_to,
            DeliveryPromise.expected_at.isnot(None),
            DeliveryPromise.stops_ahead.isnot(None),
            DeliveryPromise.day_started.isnot(None),
        )
        .all()
    )
    arrivals = actual_arrivals({promise.delivery_id for promise in promises})
    late: Dict[str, List[float]] = defaultdict(list)  # minutes after the expected time, early ones negative
    for delivery_id, expected_at, stops_ahead, day_started in promises:
        if delivery_id in arrivals:
            late[bucket_key(stops_ahead, day_started)].append(
                minutes_between(ensure_utc(expected_at), arrivals[delivery_id])
            )
    minimum = current_app.config["ETA_WINDOW_MIN_PROMISES"]
    buckets: Dict[str, Dict[str, Optional[int]]] = {}
    for key, values in late.items():
        enough = len(values) >= minimum
        buckets[key] = {
            "lo": round(percentile(values, _WINDOW_LO_Q)) if enough else None,
            "hi": round(percentile(values, _WINDOW_HI_Q)) if enough else None,
            "n": len(values),
        }
    return buckets, sum(len(values) for values in late.values())


def _monitoring(legs: Sequence[_Leg], points: Sequence[CurvePoint]) -> Dict[str, Dict[str, Dict[str, float]]]:
    """Each leg's minutes over the curve's, as the median and the count per band (spec 4.5)."""
    orders = {}
    if legs:
        orders = {
            order.id: order
            for order in Order.query.filter(Order.id.in_({leg.end.order_id for leg in legs})).options(
                selectinload(Order.order_items).joinedload(OrderItem.product)
            )
        }
    by_hour: Dict[str, List[float]] = defaultdict(list)
    by_bottles: Dict[str, List[float]] = defaultdict(list)
    for leg in legs:
        curve = cycle_minutes(leg.km, points)
        if curve <= 0:
            continue
        ratio = leg.minutes / curve
        hour_band = _hour_band(leg.end.at)
        if hour_band is not None:
            by_hour[hour_band].append(ratio)
        bottle_band = _bottle_band(int(BottleTrackingService.calculate_bottles_for_order(orders[leg.end.order_id])))
        if bottle_band is not None:
            by_bottles[bottle_band].append(ratio)
    return {"by_hour_band": _medians(by_hour), "by_bottles_band": _medians(by_bottles)}


def _medians(ratios_by_band: Dict[str, List[float]]) -> Dict[str, Dict[str, float]]:
    return {band: {"ratio": percentile(ratios, 0.5), "n": len(ratios)} for band, ratios in ratios_by_band.items()}


def _hour_band(at: datetime) -> Optional[str]:
    """The two-hour band of the local time `at`, "11-13" to "23-24", or None before 11:00 and after midnight."""
    hour = utc_to_local(at, DISPLAY_TIMEZONE).hour
    if not _FIRST_HOUR <= hour < _LAST_HOUR:
        return None
    start = _FIRST_HOUR + (hour - _FIRST_HOUR) // _BAND_HOURS * _BAND_HOURS
    return f"{start}-{min(start + _BAND_HOURS, _LAST_HOUR)}"


def _bottle_band(bottles: int) -> Optional[str]:
    """The band of an order of `bottles` bottles: "0-5", "6-10", "11-20" or "21-40", or None above 40."""
    return next((label for label, most in _BOTTLE_BANDS if bottles <= most), None)
