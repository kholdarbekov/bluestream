"""The admin accuracy report: is the shadow engine good enough to show (honest-ETA Phase 2 plan, Task 9, R12, R14;
spec §5 and §9)?

`EtaAccuracyReport.build(start_date=, end_date=, now=)` reads the plan days `start_date` to `end_date` (05:00 to 05:00
local, `work_day_bounds`) and returns the finished figures as JSON-ready values; the admin page only formats them.

- The gate (spec §5). The sample is each order's FIRST engine row (`engine_s1_v1`, stage `waiting`; the least
  `(computed_at, id)` of all its rows), when that row was computed in the range: the promise the success screen would
  have shown. Plus every order placed in the range that was held for release to a later day (`dated` below), whose
  first promise was its date. Each order lands in exactly one category, by its state when the report runs, checked in
  this order:
  - `dated`: held for release to a later day, whatever the range: a customer date, and a delivery row that first
    appeared on a later local date than the order, or none yet and the date after its local placing date. Asked of
    the order of every first row, wherever it was placed, so an order placed before the range and released in it is
    dated too. Never read off `delivery_date` alone, which a reschedule rewrites;
  - `no_window`: its first row has no window (unconfirmed, or a date with no time). It is never replaced by a later
    row;
  - `cancelled`: the order is cancelled;
  - `returned`: the delivery is returned;
  - `non_driver_close`: delivered, but not by the delivery's own driver (R14, `calibration.actual_arrivals`): closed
    from the Orders screen with no actor, or by an admin, so its time is not the handover;
  - `open`: not delivered, and the first row's window has not ended;
  - judged: every other order. It is kept when its driver's delivered tap came at or before the first row's window
    end; an order still undelivered past that end is not.
  `kept_share` is over the judged orders, overall and for those placed before 11:00 local, and the gate `passes` when
  both reach ETA_GATE_KEPT_SHARE. A row is judged with the parameters it was planned with, fitted before it was made,
  so the gate is out of sample (R12's `source`).
- The figures below the gate read the judged orders, each by its first promise:
  - `by_bucket`: kept share and median window width per (stops ahead band, car started), as the window was priced;
  - `median_window_minutes`: overall, and for orders placed 07:00-15:00 local (spec §9);
  - `today_delivered_today_share` and `tomorrow_delivered_same_day_share`: of the "today" and the "tomorrow" promises,
    the share delivered on the local day the promise was made. A "tomorrow" delivered the same day was falsely long;
  - `early_share` and `median_minutes_early`: the share of judged deliveries that came before their window start, and
    the median minutes by which those did. A window shown "by HH:MM" (`windows.by_only`) tells no start, so nothing
    is early for it;
  - `by_drivers_working`: kept share and median window with one car working when the promise was made, and with two
    or more (spec §9, capacity). A promise made while no car worked (0: the early-morning set stood in, spec §4.2)
    falls into neither;
  - `first_half_day_same_day`: of the judged orders placed before 13:00 local (the delivery statement's "first half
    of the day") that have an arrival or whose placing day is over, the share delivered on the local day they were
    placed; an order never delivered is a miss, one placed this morning and not delivered yet is not counted yet;
  - `change_messages`: the share of orders the §4.9 rule (`changes.messages_that_would_send`) would message none, and
    at most once, over their engine rows while they waited: up to the first in_transit tap (R12), else the arrival,
    else now.
- `s2`: Phase 1's "on the way" legs recorded in the range, read by the one leg query headroom learns from
  (`DeliveryEtaService.s2_leg_rows`), each judged by the arrival of its own trip: kept when it came by the promised
  "by" time; within 15 minutes of it either way (spec §9); and the median absolute error of Google's drive time and of
  our engine's shadow time for the same leg. Only legs with Google's time: those headroom learns from.
- `s2_stale_estimate`: the "on the way" legs whose driver's position was too old for Google at the tap, promised our
  engine's minutes plus an allowance (`SOURCE_STALE_ESTIMATE`), judged the same way on their own line, with our
  engine's median error: whether the allowance fits.
- `latest_fit`: when the fit the engine reads now (`params.latest_fit`) was made, the window of plan days it read, and
  its monitoring ratios. None before the first fit.
- `definitions`: the thresholds above, for the page's copy: `morning_ends` (11:00), `daytime_from` and `daytime_to`
  (07:00 and 15:00) and `first_half_ends` (13:00) as local "HH:MM", and `s2_tolerance_minutes` (15).

A share with nothing to count is None. No key is one the `TimezoneMiddleware` rewrites: the instants of `latest_fit`
are sent as UTC ISO strings and the range as ISO dates, and they cross it unchanged.
"""

from collections import defaultdict
from datetime import date, datetime, time
from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Sequence, Set, Tuple

from flask import current_app
from sqlalchemy import func
from sqlalchemy.orm import joinedload

from business_app import db
from business_app.models.delivery import Delivery, DeliveryStatusHistory
from business_app.models.delivery_promise import DAY_KIND_TODAY, DAY_KIND_TOMORROW, DeliveryPromise
from business_app.models.order import Order
from business_app.services.delivery_eta_service import SOURCE_STALE_ESTIMATE, DeliveryEtaService, S2Leg
from business_app.services.eta_plan.calibration import actual_arrivals
from business_app.services.eta_plan.changes import messages_that_would_send
from business_app.services.eta_plan.common import minutes_between, moved_into, percentile
from business_app.services.eta_plan.params import latest_fit
from business_app.services.eta_plan.scope import engine_rows_filter
from business_app.services.eta_plan.windows import STOPS_AHEAD_BANDS, band_of, by_only
from business_app.utils.local_windows import local_date, work_day_bounds
from business_app.utils.timezone_utils import ensure_utc, ensure_utc_or_none, utc_to_local
from shared.constants import DISPLAY_TIMEZONE
from shared.enums import DeliveryStatus, OrderStatus

# The gate's categories of an order not judged, in the order they are checked.
DATED = "dated"
NO_WINDOW = "no_window"
CANCELLED = "cancelled"
RETURNED = "returned"
NON_DRIVER_CLOSE = "non_driver_close"
OPEN = "open"
EXCLUDED = (DATED, NO_WINDOW, CANCELLED, RETURNED, NON_DRIVER_CLOSE, OPEN)

# Spec §5: the gate is also judged for the orders placed before 11:00 local. Spec §9: the median window is reported for
# the orders placed 07:00-15:00 local.
_MORNING_ENDS = time(11, 0)
_DAYTIME = (time(7, 0), time(15, 0))
# The delivery statement promises "the first half of the day" for orders placed before this local time.
_FIRST_HALF_ENDS = time(13, 0)
# Spec §9: an "on the way" promise is kept "within ±15 minutes".
_S2_WITHIN_MINUTES = 15
# The thresholds above, as the report publishes them for the page's copy: the page names them, it never states them.
_DEFINITIONS = {
    "morning_ends": _MORNING_ENDS.strftime("%H:%M"),
    "daytime_from": _DAYTIME[0].strftime("%H:%M"),
    "daytime_to": _DAYTIME[1].strftime("%H:%M"),
    "first_half_ends": _FIRST_HALF_ENDS.strftime("%H:%M"),
    "s2_tolerance_minutes": _S2_WITHIN_MINUTES,
}


class _Judged(NamedTuple):
    """A judged order: its first promise, and its R14 arrival if it has one. `window_start`/`window_end` are aware
    UTC."""

    order: Order
    first: DeliveryPromise
    window_start: Optional[datetime]
    window_end: datetime
    arrival: Optional[datetime]

    @property
    def kept(self) -> bool:
        return self.arrival is not None and self.arrival <= self.window_end

    @property
    def window_minutes(self) -> Optional[float]:
        return minutes_between(self.window_start, self.window_end) if self.window_start is not None else None


class EtaAccuracyReport:
    """The accuracy report the admin page shows; the go-live gate of Phase 3."""

    @staticmethod
    def build(*, start_date: date, end_date: date, now: datetime) -> Dict[str, Any]:
        """The report of the plan days `start_date` to `end_date` (inclusive local dates), as of `now`."""
        now = ensure_utc(now)
        range_from, range_to = work_day_bounds(start_date)[0], work_day_bounds(end_date)[1]
        firsts = _first_promises(range_from, range_to)
        deliveries = _deliveries({first.delivery_id for first in firsts})
        dated_ids = _dated_order_ids(range_from, range_to) | {
            delivery.order_id
            for delivery in deliveries.values()
            if _held_for_release(delivery.order.created_at, delivery.order.delivery_date, delivery.created_at)
        }
        arrivals = actual_arrivals(deliveries)
        excluded = {category: 0 for category in EXCLUDED}
        excluded[DATED] = len(dated_ids)
        judged: List[_Judged] = []
        for first in firsts:
            delivery = deliveries[first.delivery_id]
            if delivery.order_id in dated_ids:
                continue
            category = _category(delivery, first, arrivals.get(delivery.id), now)
            if category is None:
                judged.append(
                    _Judged(
                        order=delivery.order,
                        first=first,
                        window_start=ensure_utc_or_none(first.window_start),
                        window_end=ensure_utc(first.window_end),
                        arrival=arrivals.get(delivery.id),
                    )
                )
            else:
                excluded[category] += 1
        return {
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "gate": _gate(judged, excluded),
            "by_bucket": _by_bucket(judged),
            "median_window_minutes": {
                "overall": _median_window(judged),
                "placed_7_to_15": _median_window(
                    [judged_order for judged_order in judged if _DAYTIME[0] <= _placed_at(judged_order) < _DAYTIME[1]]
                ),
            },
            "today_delivered_today_share": _delivered_the_day_promised(judged, DAY_KIND_TODAY),
            "tomorrow_delivered_same_day_share": _delivered_the_day_promised(judged, DAY_KIND_TOMORROW),
            "first_half_day_same_day": _first_half_same_day(judged, now),
            **_early(judged),
            "by_drivers_working": {
                "one": _group([judged_order for judged_order in judged if judged_order.first.drivers_working == 1]),
                "two_or_more": _group(
                    [judged_order for judged_order in judged if (judged_order.first.drivers_working or 0) >= 2]
                ),
            },
            "change_messages": _change_messages(judged, now),
            **_s2(range_from, range_to),
            "latest_fit": _latest_fit(now),
            "definitions": dict(_DEFINITIONS),
        }


def _first_promises(range_from: datetime, range_to: datetime) -> List[DeliveryPromise]:
    """Each order's first engine row (the least `(computed_at, id)` of its delivery's rows; an order has one delivery),
    when it was computed in `[range_from, range_to)`."""
    oldest_first = func.row_number().over(
        partition_by=DeliveryPromise.delivery_id,
        order_by=(DeliveryPromise.computed_at, DeliveryPromise.id),
    )
    ranked = (
        db.session.query(DeliveryPromise.id.label("id"), oldest_first.label("oldest"))
        .filter(engine_rows_filter())
        .subquery()
    )
    return (
        DeliveryPromise.query.join(ranked, ranked.c.id == DeliveryPromise.id)
        .filter(
            ranked.c.oldest == 1,
            DeliveryPromise.computed_at >= range_from,
            DeliveryPromise.computed_at < range_to,
        )
        .order_by(DeliveryPromise.computed_at, DeliveryPromise.id)
        .all()
    )


def _dated_order_ids(range_from: datetime, range_to: datetime) -> Set[int]:
    """The orders placed in `[range_from, range_to)` that were held for release to a later day (`_held_for_release`),
    with or without a first row in the range: one still held has no delivery row, so no engine row either."""
    placed = (
        db.session.query(Order.id, Order.created_at, Order.delivery_date, Delivery.created_at)
        .outerjoin(Delivery, Delivery.order_id == Order.id)
        .filter(
            Order.delivery_date.isnot(None),
            Order.created_at >= range_from,
            Order.created_at < range_to,
        )
    )
    return {
        order_id
        for order_id, placed_at, customer_date, released_at in placed
        if _held_for_release(placed_at, customer_date, released_at)
    }


def _held_for_release(placed_at: datetime, customer_date: Optional[date], released_at: Optional[datetime]) -> bool:
    """The dated test, whenever the order was placed: a customer date, and a delivery row that first appeared
    (`released_at`) on a later local date than the order (the release sweep made it on its day), or, still held, no
    delivery row yet and the date after the local date the order was placed.

    Read off when the delivery row was made, never off `delivery_date` alone: a reschedule rewrites the date
    (`OrderScheduleService._apply_reschedule`) but never deletes or re-creates the row, so a failed delivery moved to a
    later day stays judged on its first promise. An order with no customer date was never held for release; one whose
    row came the next day was waiting for its confirmation."""
    if customer_date is None:
        return False
    held_until = local_date(ensure_utc(released_at)) if released_at is not None else customer_date
    return held_until > local_date(ensure_utc(placed_at))


def _deliveries(delivery_ids: Iterable[int]) -> Dict[int, Delivery]:
    ids = sorted(set(delivery_ids))
    if not ids:
        return {}
    rows = Delivery.query.filter(Delivery.id.in_(ids)).options(joinedload(Delivery.order))
    return {delivery.id: delivery for delivery in rows}


def _category(delivery: Delivery, first: DeliveryPromise, arrival: Optional[datetime], now: datetime) -> Optional[str]:
    """The gate category of an order not dated, or None when it is judged."""
    if first.window_end is None:
        return NO_WINDOW
    if delivery.order.status == OrderStatus.CANCELLED:
        return CANCELLED
    if delivery.status == DeliveryStatus.RETURNED:
        return RETURNED
    delivered = delivery.status == DeliveryStatus.DELIVERED
    if delivered and arrival is None:
        return NON_DRIVER_CLOSE
    if not delivered and ensure_utc(first.window_end) > now:
        return OPEN
    return None


def _gate(judged: Sequence[_Judged], excluded: Dict[str, int]) -> Dict[str, Any]:
    target = current_app.config["ETA_GATE_KEPT_SHARE"]
    morning = [judged_order for judged_order in judged if _placed_at(judged_order) < _MORNING_ENDS]
    kept_share = _kept_share(judged)
    morning_share = _kept_share(morning)
    return {
        "sample": len(judged) + sum(excluded.values()),
        "n": len(judged),
        "kept": sum(judged_order.kept for judged_order in judged),
        "kept_share": kept_share,
        "before_eleven": {
            "n": len(morning),
            "kept": sum(judged_order.kept for judged_order in morning),
            "kept_share": morning_share,
        },
        "target": target,
        "passes": kept_share is not None
        and morning_share is not None
        and kept_share >= target
        and morning_share >= target,
        "excluded": excluded,
    }


def _by_bucket(judged: Sequence[_Judged]) -> List[Dict[str, Any]]:
    """One row per (stops ahead band, started) that has judged orders, in band order, not started first."""
    buckets: Dict[Tuple[str, bool], List[_Judged]] = defaultdict(list)
    for judged_order in judged:
        first = judged_order.first
        if first.stops_ahead is not None and first.day_started is not None:
            buckets[(band_of(first.stops_ahead), bool(first.day_started))].append(judged_order)
    return [
        {"stops_ahead_band": band, "started": started, **_group(buckets[(band, started)])}
        for band in STOPS_AHEAD_BANDS
        for started in (False, True)
        if (band, started) in buckets
    ]


def _group(judged: Sequence[_Judged]) -> Dict[str, Any]:
    return {"n": len(judged), "kept_share": _kept_share(judged), "median_window_minutes": _median_window(judged)}


def _delivered_the_day_promised(judged: Sequence[_Judged], label: str) -> Optional[float]:
    """Of the judged first promises labelled `label`, the share delivered on the local day the promise was made."""
    promises = [judged_order for judged_order in judged if judged_order.first.day_kind == label]
    same_day = [
        judged_order
        for judged_order in promises
        if judged_order.arrival is not None
        and local_date(judged_order.arrival) == local_date(judged_order.first.computed_at)
    ]
    return _share(len(same_day), len(promises))


def _first_half_same_day(judged: Sequence[_Judged], now: datetime) -> Dict[str, Any]:
    """Of the judged orders placed before 13:00 local, how many were delivered on the local day they were placed.

    Counted: an order with an arrival, or whose placing day is over (never delivered is a miss, not dropped). An order
    placed today and not delivered yet is not counted yet."""
    counted = [
        judged_order
        for judged_order in judged
        if _placed_at(judged_order) < _FIRST_HALF_ENDS
        and (
            judged_order.arrival is not None or local_date(ensure_utc(judged_order.order.created_at)) < local_date(now)
        )
    ]
    same_day = sum(
        judged_order.arrival is not None
        and local_date(judged_order.arrival) == local_date(ensure_utc(judged_order.order.created_at))
        for judged_order in counted
    )
    return {"n": len(counted), "same_day": same_day, "share": _share(same_day, len(counted))}


def _early(judged: Sequence[_Judged]) -> Dict[str, Optional[float]]:
    arrived = [judged_order for judged_order in judged if judged_order.arrival is not None]
    minutes_early = [
        minutes_between(judged_order.arrival, judged_order.window_start)
        for judged_order in arrived
        if judged_order.window_start is not None
        and not by_only(judged_order.window_start, judged_order.window_end)
        and judged_order.arrival < judged_order.window_start
    ]
    return {
        "early_share": _share(len(minutes_early), len(arrived)),
        "median_minutes_early": percentile(minutes_early, 0.5) if minutes_early else None,
    }


def _change_messages(judged: Sequence[_Judged], now: datetime) -> Dict[str, Any]:
    """How many messages the §4.9 rule would send each judged order, over its engine rows, while it was waiting: until
    its delivery's first real in_transit tap, where the "on the way" promise (S2) takes over (R12), else its arrival,
    else now."""
    rows: Dict[int, List[DeliveryPromise]] = defaultdict(list)
    delivery_ids = [judged_order.first.delivery_id for judged_order in judged]
    set_off = _first_set_off(delivery_ids)
    if delivery_ids:
        for row in (
            DeliveryPromise.query.filter(DeliveryPromise.delivery_id.in_(delivery_ids), engine_rows_filter())
            .order_by(DeliveryPromise.computed_at, DeliveryPromise.id)
            .all()
        ):
            rows[row.delivery_id].append(row)
    counts = [
        len(
            messages_that_would_send(
                rows[judged_order.first.delivery_id],
                until=set_off.get(judged_order.first.delivery_id) or judged_order.arrival or now,
            )
        )
        for judged_order in judged
    ]
    return {
        "n": len(counts),
        "none_share": _share(sum(count == 0 for count in counts), len(counts)),
        "at_most_one_share": _share(sum(count <= 1 for count in counts), len(counts)),
    }


def _first_set_off(delivery_ids: Sequence[int]) -> Dict[int, datetime]:
    """Each delivery's first real move into in_transit (`moved_into`), by anyone, as aware UTC."""
    if not delivery_ids:
        return {}
    taps = (
        db.session.query(DeliveryStatusHistory.delivery_id, func.min(DeliveryStatusHistory.changed_at))
        .filter(
            DeliveryStatusHistory.delivery_id.in_(delivery_ids),
            moved_into(DeliveryStatusHistory, DeliveryStatus.IN_TRANSIT),
        )
        .group_by(DeliveryStatusHistory.delivery_id)
    )
    return {delivery_id: ensure_utc(tapped_at) for delivery_id, tapped_at in taps}


def _s2(range_from: datetime, range_to: datetime) -> Dict[str, Dict[str, Any]]:
    """`s2` (Google's legs) and `s2_stale_estimate` (legs from a position too old for Google), each judged alone."""
    legs = [
        leg
        for leg in DeliveryEtaService.s2_leg_rows(start=range_from, end=range_to)
        if leg.arrival is not None and leg.window_end is not None
    ]
    google = [leg for leg in legs if leg.expected_at is not None]
    stale = [leg for leg in legs if leg.source == SOURCE_STALE_ESTIMATE]
    shadowed = [leg for leg in google if leg.shadow_expected_at is not None]
    return {
        "s2": {
            **_s2_judged(google),
            "google_median_error_minutes": _median_error([(leg.arrival, leg.expected_at) for leg in google]),
            "engine_n": len(shadowed),
            "engine_median_error_minutes": _median_error([(leg.arrival, leg.shadow_expected_at) for leg in shadowed]),
        },
        # The shown time is our engine's plus an allowance; the engine's own error says whether the allowance fits.
        "s2_stale_estimate": {
            **_s2_judged(stale),
            "engine_median_error_minutes": _median_error(
                [(leg.arrival, leg.shadow_expected_at) for leg in stale if leg.shadow_expected_at is not None]
            ),
        },
    }


def _s2_judged(legs: Sequence[S2Leg]) -> Dict[str, Any]:
    """How many legs, and the shares kept by their "by" time and within 15 minutes of it either way (spec §9)."""
    return {
        "n": len(legs),
        "kept_share": _share(sum(leg.arrival <= leg.window_end for leg in legs), len(legs)),
        "within_15_share": _share(
            sum(abs(minutes_between(leg.window_end, leg.arrival)) <= _S2_WITHIN_MINUTES for leg in legs), len(legs)
        ),
    }


def _latest_fit(now: datetime) -> Optional[Dict[str, Any]]:
    fit = latest_fit(now)
    if fit is None:
        return None
    return {
        "fitted_at": ensure_utc(fit.fitted_at).isoformat(),
        "data_from": ensure_utc(fit.data_from).isoformat(),
        "data_to": ensure_utc(fit.data_to).isoformat(),
        "monitoring": fit.params["monitoring"],
    }


def _placed_at(judged_order: _Judged) -> time:
    """The local time of day the order was placed."""
    return utc_to_local(ensure_utc(judged_order.order.created_at), DISPLAY_TIMEZONE).time()


def _kept_share(judged: Sequence[_Judged]) -> Optional[float]:
    return _share(sum(judged_order.kept for judged_order in judged), len(judged))


def _median_window(judged: Sequence[_Judged]) -> Optional[float]:
    widths = [judged_order.window_minutes for judged_order in judged if judged_order.window_minutes is not None]
    return percentile(widths, 0.5) if widths else None


def _median_error(pairs: Sequence[Tuple[datetime, datetime]]) -> Optional[float]:
    errors = [abs(minutes_between(expected, arrived)) for arrived, expected in pairs]
    return percentile(errors, 0.5) if errors else None


def _share(hits: int, n: int) -> Optional[float]:
    return hits / n if n else None
