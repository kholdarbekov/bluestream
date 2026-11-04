"""The customer's arrival promise once the driver is on the way.

Honest-ETA spec 2026-11-03 §4.8 (S2 and S3), §4.10 and §4.12a. This is the ONE code that writes or reads
`delivery_promises`:

- `record_on_the_way(history_id)` runs in the in-transit notification worker. For the driver's `in_transit` tap, from
  a position at most `ETA_POSITION_MAX_AGE_SECONDS` old (`_start_of_leg`), it makes one Google Routes TRAFFIC_AWARE
  call (`google_routes.get_traffic_aware_leg`, under the daily cap) and promises "by tap + ETA_OFF_ROAD_MINUTES +
  headroom x G", G being Google's drive time, in the SHOWN row (`google_tap_v1`). From an older position it makes no
  call and promises "by tap + our own engine's minutes + an allowance" in the same row. Beside it goes our own engine's
  time for the same leg, a HIDDEN row (`osrm_shadow_v1`): the yardstick, never shown on its own. Only the delivery's own
  driver's tap earns minutes (§4.8 S2): a mark someone else made (an admin's, from the Deliveries page or a bulk
  action) gets a shown row without minutes, no Google call and no shadow.
- `headroom(now=)` learns the factor on G from the legs of the last four weeks.
- `customer_eta(order, now=)` reads the promise back as finished fields (`CustomerEta.as_payload`). The Track payload
  and the delivery message publish them; the bot, the website and the email only format them.

Only the shown promise of the delivery's LATEST real tap is ever read, so a delivery that failed and went out again
never shows its earlier trip's minutes. A real move into a status is one whose old status differs (`_moved_into`):
a reassign row (in_transit -> in_transit, arrived -> arrived) is neither a tap nor an arrival. A promise ends when its
leg is interrupted (`_leg_uninterrupted`, §4.8 S2): the delivery is handed to another driver while on its way (an
in_transit -> in_transit row), or its driver taps in_transit on another stop. From then on the customer is told "on
the way" with no minutes, and the leg does not teach headroom.
"""

from __future__ import annotations

import logging
import math
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from flask import current_app
from sqlalchemy import and_, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased

from business_app import db
from business_app.models.delivery import Delivery, DeliveryPerson, DeliveryStatusHistory
from business_app.models.delivery_promise import MODEL_GOOGLE_TAP, MODEL_OSRM_SHADOW, DeliveryPromise
from business_app.models.order import Order
from business_app.services.maps_service import MapsService
from business_app.services.order_schedule_service import OrderScheduleService
from business_app.services.route_optimization_service import RouteOptimizationService
from business_app.utils import google_routes
from business_app.utils.distance_matrix import STRAIGHT_LINE_SOURCE
from business_app.utils.helpers import get_warehouse_coordinates
from business_app.utils.local_windows import local_date
from business_app.utils.timezone_utils import ensure_utc, get_utc_now, utc_to_local
from shared.constants import DISPLAY_TIMEZONE, ETA_ATTRIBUTION_GOOGLE, ETA_STAGE_ARRIVED, ETA_STAGE_ON_THE_WAY
from shared.enums import DeliveryStatus

logger = logging.getLogger(__name__)

Point = Tuple[float, float]

# `source` of a SHOWN row: where its minutes came from, or why it has none. A shadow row carries the distance
# matrix's own label instead ("osrm_selfhosted", "cache", STRAIGHT_LINE_SOURCE, ...), or SOURCE_ERROR.
SOURCE_GOOGLE_TRAFFIC = "google_traffic"
SOURCE_STALE_ESTIMATE = "stale_estimate"  # position too old for Google: our own engine's minutes plus an allowance
SOURCE_GOOGLE_UNAVAILABLE = "google_unavailable"  # asked, no answer: no key, the day's cap spent, or Google failed
SOURCE_STALE_POSITION = "stale_position"  # position too old for Google, and our own engine had no real answer either
SOURCE_NOT_DRIVER_TAP = "not_driver_tap"  # someone other than the delivery's driver tapped it, so Google was not asked
SOURCE_NO_DESTINATION = "no_destination"  # the order's address has no coordinates
SOURCE_ERROR = "error"  # the shadow's matrix lookup failed

# Headroom learns from the shown Google promises of the last four weeks...
_HEADROOM_WINDOW = timedelta(days=28)
# ...whose drive was a drive: not `in_transit` and `arrived` pressed together, nor a tap left open for hours...
_DRIVE_MINUTES_MIN = 1.0
_DRIVE_MINUTES_MAX = 180.0
# ...and whose Google time is long enough to divide by.
_GOOGLE_MINUTES_MIN = 1.0
# The promise is never sooner than Google's own drive time plus the off-road minutes.
_HEADROOM_FLOOR = 1.0


@dataclass(frozen=True)
class CustomerEta:
    """What the customer is told about the arrival, as finished fields. All None: nothing is promised."""

    stage: Optional[str] = None  # ETA_STAGE_ON_THE_WAY or ETA_STAGE_ARRIVED
    by: Optional[datetime] = None  # the promised "by" instant, aware UTC
    by_time: Optional[str] = None  # `by` as HH:MM on the Tashkent clock
    minutes_left: Optional[int] = None  # whole minutes to `by`, rounded up; 0 once it has passed
    attribution: Optional[str] = None  # ETA_ATTRIBUTION_GOOGLE when the minutes came from Google

    def as_payload(self) -> Dict[str, Any]:
        """The five Track keys of Phase 1 (spec §4.8)."""
        return {
            "eta_stage": self.stage,
            "eta_by": self.by.isoformat() if self.by is not None else None,
            "eta_by_time": self.by_time,
            "eta_minutes_left": self.minutes_left,
            "eta_attribution": self.attribution,
        }


_NOTHING_PROMISED = CustomerEta()


@dataclass(frozen=True)
class _LegStart:
    """Where the leg of a tap starts, by what is known of the driver at the tap."""

    point: Point
    fresh: bool  # at most ETA_POSITION_MAX_AGE_SECONDS old at the tap: where the driver is, so Google's traffic applies
    from_depot: bool = False  # stale, and the point is the depot


def _moved_into(history, status: DeliveryStatus):
    """A real move into `status` on `DeliveryStatusHistory` (or an alias of it). A reassign row keeps old == new."""
    return and_(history.new_status == status, history.old_status != status)


def _leg_uninterrupted(tap, delivery, *, before=None):
    """Whether the leg of `tap`, a real `in_transit` tap, is still one drive: not interrupted (spec §4.8 S2). The one
    definition of the rule; every reader of a promise or a leg uses it.

    A correlated clause over the enclosing query's `tap` (`DeliveryStatusHistory` or an alias of it) and `delivery`
    (`Delivery` or an alias of it, joined there as `tap`'s own delivery). A leg is interrupted by a status row strictly
    after the tap's `changed_at` (and strictly before `before`, when it is given) that is either
    - (a) a hand-over: the tapped delivery gets an in_transit -> in_transit row, an admin reassign while it is on its
      way. The tap's promise came from the previous driver's position;
    - (b) its driver setting off elsewhere: a real move into in_transit (`_moved_into`, so a reassign row never
      counts) on ANOTHER delivery, made by the tapped delivery's driver: the row's `changed_by` is the tapped
      delivery's current `delivery_person_id`, as `first_arrival_at` pairs an arrival. The driver reaches the newer
      stop first, so the earlier stop's leg is no longer one drive. History rows never change, so an ended promise
      stays ended whatever later happens to the other stop. Someone else's move on the driver's other stop (an admin
      marking it in transit) does not count, and a delivery with no driver is interrupted by (a) alone.

    Built as NOT EXISTS (a) AND NOT EXISTS (b), each keyed on one column (the tapped delivery; the driver), so that in
    the WHERE of `_leg_ratios` Postgres plans them as two hash anti-joins. A NOT over one EXISTS of (a) OR (b), or over
    an OR of two EXISTS, is instead checked leg by leg against in-transit rows; TestThePostgresPlanShape guards the
    shape. Keyed on the driver, (b) still checks each leg against all of that driver's real in_transit rows ever. In
    `customer_eta` the clause is a select-list SubPlan, which an index on `changed_at` would bound; the anti-join would
    not use one.
    """
    handover = aliased(DeliveryStatusHistory, name="handover")
    elsewhere = aliased(DeliveryStatusHistory, name="elsewhere")

    def within_the_leg(row):
        bounds = [row.changed_at > tap.changed_at]
        if before is not None:
            bounds.append(row.changed_at < before)
        return bounds

    handed_over = (
        select(handover.id)
        .where(
            handover.delivery_id == tap.delivery_id,
            handover.old_status == DeliveryStatus.IN_TRANSIT,
            handover.new_status == DeliveryStatus.IN_TRANSIT,
            *within_the_leg(handover),
        )
        .correlate(tap, delivery)
        .exists()
    )
    set_off_elsewhere = (
        select(elsewhere.id)
        .where(
            elsewhere.changed_by == delivery.delivery_person_id,
            # Mirrors the rule's "another stop"; redundant for both readers (a latest tap has no later tap on its own
            # delivery, and `_leg_ratios`' next-tap check already voids a leg re-tapped before its arrival).
            elsewhere.delivery_id != tap.delivery_id,
            _moved_into(elsewhere, DeliveryStatus.IN_TRANSIT),
            *within_the_leg(elsewhere),
        )
        .correlate(tap, delivery)
        .exists()
    )
    return and_(~handed_over, ~set_off_elsewhere)


def _minutes(start: datetime, end: datetime) -> float:
    return (end - start).total_seconds() / 60.0


def _p90(values: List[float]) -> float:
    """The 90th percentile, interpolated as Postgres's percentile_cont(0.9) is (the ETA analysis SQL's own)."""
    return statistics.quantiles(values, n=10, method="inclusive")[-1]


class DeliveryEtaService:
    """Records the promise of an "on the way" tap, learns its headroom, and tells the customer."""

    def record_on_the_way(self, history_id: int) -> Optional[DeliveryPromise]:
        """Record the promise of the driver's `in_transit` tap `history_id`, and return its SHOWN row.

        None when the row is not a tap: any other move, or a reassign row. Only the delivery's own driver's tap earns
        minutes (spec §4.8 S2): when the row's `changed_by` is not the delivery's `delivery_person_id` (an admin's mark,
        say), the shown row alone is written, with SOURCE_NOT_DRIVER_TAP and no minutes; Google is not asked and no
        shadow is kept. The leg starts where `_start_of_leg` places the driver. From a fresh position Google is asked,
        and the shown row carries its time. From a stale one Google is not asked: the row says SOURCE_STALE_ESTIMATE,
        with a promise of our own engine's minutes plus an allowance and no `expected_at`, which is Google's time and
        teaches headroom, or SOURCE_STALE_POSITION with no minutes when our engine has no real answer. Idempotent: a tap
        already recorded returns its row with no provider call, so the notification worker re-running the tap (Celery
        acks_late) neither asks Google again nor writes a second row. Commits. When a concurrent run of the same tap
        commits first, the unique (tap, model) index refuses these rows; the session is rolled back and the other run's
        shown row is returned.
        """
        tap = DeliveryStatusHistory.query.filter(
            DeliveryStatusHistory.id == history_id,
            _moved_into(DeliveryStatusHistory, DeliveryStatus.IN_TRANSIT),
        ).first()
        if tap is None:
            return None
        recorded = self._tap_promise(history_id)
        if recorded is not None:
            return recorded

        now = get_utc_now()
        tap_at = ensure_utc(tap.changed_at)
        drivers_own_tap = tap.changed_by is not None and tap.changed_by == tap.delivery.delivery_person_id
        destination = RouteOptimizationService().delivery_point(tap.delivery) if drivers_own_tap else None

        shown = self._promise_row(tap, now, model_version=MODEL_GOOGLE_TAP, shown=True)
        rows = [shown]
        if not drivers_own_tap:
            shown.source = SOURCE_NOT_DRIVER_TAP
        elif destination is None:
            shown.source = SOURCE_NO_DESTINATION
        else:
            start = self._start_of_leg(tap.delivery, tap_at)
            engine_minutes, engine_source = self._engine_time(tap, start.point, destination)
            if start.fresh:
                self._promise_google_time(shown, now, tap_at, start.point, destination)
            else:
                self._promise_engine_time(shown, tap_at, start, engine_minutes)
            rows.append(self._shadow_row(tap, now, tap_at, engine_minutes, engine_source))

        delivery_id, source, window_end = shown.delivery_id, shown.source, shown.window_end
        db.session.add_all(rows)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            recorded = self._tap_promise(history_id)
            if recorded is None:
                raise  # not a second run of this tap: some other rule refused the rows
            logger.info("eta_on_the_way_already_recorded history=%s (a concurrent run committed first)", history_id)
            return recorded
        logger.info(
            "eta_on_the_way_recorded delivery=%s history=%s source=%s window_end=%s",
            delivery_id,
            history_id,
            source,
            window_end.isoformat() if window_end is not None else None,
        )
        return shown

    def _promise_google_time(
        self, shown: DeliveryPromise, now: datetime, tap_at: datetime, start: Point, destination: Point
    ) -> None:
        """The shown row of a fresh tap: by tap + ETA_OFF_ROAD_MINUTES + headroom x Google's drive time."""
        leg = google_routes.get_traffic_aware_leg(start, destination)
        if leg is None:
            shown.source = SOURCE_GOOGLE_UNAVAILABLE
            return
        google_minutes = leg["duration_minutes"]
        off_road = current_app.config["ETA_OFF_ROAD_MINUTES"]
        shown.expected_at = tap_at + timedelta(minutes=google_minutes)
        shown.window_end = tap_at + timedelta(minutes=math.ceil(off_road + self.headroom(now=now) * google_minutes))
        shown.source = SOURCE_GOOGLE_TRAFFIC

    @staticmethod
    def _promise_engine_time(
        shown: DeliveryPromise, tap_at: datetime, start: _LegStart, engine_minutes: Optional[float]
    ) -> None:
        """The shown row of a stale tap: by tap + our own engine's minutes + an allowance; none if it has no answer."""
        if engine_minutes is None:
            shown.source = SOURCE_STALE_POSITION
            return
        allowance = current_app.config[
            "ETA_STALE_DEPOT_ALLOWANCE_MINUTES" if start.from_depot else "ETA_STALE_DOOR_ALLOWANCE_MINUTES"
        ]
        shown.window_end = tap_at + timedelta(minutes=math.ceil(engine_minutes + allowance))
        shown.source = SOURCE_STALE_ESTIMATE

    def headroom(self, *, now: datetime) -> float:
        """The factor on Google's drive time in the promise, learned from the legs of the four weeks before `now`.

        Below ETA_HEADROOM_MIN_LEGS legs the seed stands. From there the legs' 90th-percentile ratio is blended with
        the seed, the seed weighing as ETA_HEADROOM_PRIOR_LEGS legs. Never below 1.0.
        """
        config = current_app.config
        seed = config["ETA_HEADROOM_SEED"]
        ratios = self._leg_ratios(now=ensure_utc(now), off_road=config["ETA_OFF_ROAD_MINUTES"])
        legs = len(ratios)
        if legs == 0 or legs < config["ETA_HEADROOM_MIN_LEGS"]:
            headroom = seed
        else:
            prior = config["ETA_HEADROOM_PRIOR_LEGS"]
            headroom = (legs * _p90(ratios) + prior * seed) / (legs + prior)
        return max(_HEADROOM_FLOOR, headroom)

    def customer_eta(self, order: Order, *, now: datetime) -> CustomerEta:
        """What the customer is told about the arrival of `order` at `now`.

        Nothing before the driver sets off, after the delivery, or while a failed delivery waits for a new date
        (F15). "Arrived" once the driver tapped it. On the way: the shown promise of the delivery's LATEST real tap,
        counted down to its "by" time. An earlier trip's promise is never read. "On the way" alone when that promise
        has no minutes, when the worker has not recorded this trip's tap yet, or once the tap's leg was interrupted
        (`_leg_uninterrupted`, spec §4.8 S2), at any time after the tap: the delivery was handed to another driver, or
        its driver set off to another stop. A tap on another stop made BEFORE this tap does not interrupt it: the
        driver goes to the newest stop first.
        """
        delivery = order.delivery
        if delivery is None or OrderScheduleService.awaiting_new_date(order):
            return _NOTHING_PROMISED
        if delivery.status == DeliveryStatus.ARRIVED:
            return CustomerEta(stage=ETA_STAGE_ARRIVED)
        if delivery.status != DeliveryStatus.IN_TRANSIT:
            return _NOTHING_PROMISED

        # One query: the latest tap, and whether its leg still stands.
        tap = aliased(DeliveryStatusHistory, name="tap")
        latest_tap = (
            db.session.query(tap.id, _leg_uninterrupted(tap, Delivery).label("uninterrupted"))
            .join(Delivery, Delivery.id == tap.delivery_id)
            .filter(tap.delivery_id == delivery.id, _moved_into(tap, DeliveryStatus.IN_TRANSIT))
            .order_by(tap.changed_at.desc(), tap.id.desc())
            .first()
        )
        if latest_tap is None or not latest_tap.uninterrupted:
            return CustomerEta(stage=ETA_STAGE_ON_THE_WAY)
        promise = self._tap_promise(latest_tap.id)
        if promise is None or promise.window_end is None:
            return CustomerEta(stage=ETA_STAGE_ON_THE_WAY)
        by = ensure_utc(promise.window_end)
        return CustomerEta(
            stage=ETA_STAGE_ON_THE_WAY,
            by=by,
            by_time=utc_to_local(by, DISPLAY_TIMEZONE).strftime("%H:%M"),
            minutes_left=max(0, math.ceil((by - ensure_utc(now)).total_seconds() / 60)),
            attribution=ETA_ATTRIBUTION_GOOGLE if promise.source == SOURCE_GOOGLE_TRAFFIC else None,
        )

    @staticmethod
    def _tap_promise(history_id: int) -> Optional[DeliveryPromise]:
        """The shown `google_tap_v1` promise of one tap; the unique (tap, model) index allows at most one."""
        return DeliveryPromise.query.filter_by(
            delivery_status_history_id=history_id, model_version=MODEL_GOOGLE_TAP
        ).first()

    @staticmethod
    def _promise_row(tap: DeliveryStatusHistory, now: datetime, **columns) -> DeliveryPromise:
        # Always the tap's own delivery: nothing in the schema ties a promise's delivery to its tap's.
        return DeliveryPromise(
            delivery_id=tap.delivery_id,
            delivery_status_history_id=tap.id,
            computed_at=now,
            stage=ETA_STAGE_ON_THE_WAY,
            **columns,
        )

    @staticmethod
    def _engine_time(tap: DeliveryStatusHistory, start: Point, destination: Point) -> Tuple[Optional[float], str]:
        """Our own engine's minutes for the leg and where they came from. No minutes when it only guessed or failed: a
        straight-line answer is never recorded as a drive time, and a failed lookup must never cost the customer a
        promise from Google."""
        try:
            matrix, source = MapsService().get_distance_matrix([start, destination], traffic=True)
            return (None if source == STRAIGHT_LINE_SOURCE else matrix[(0, 1)]["duration_minutes"]), source
        except Exception as exc:  # noqa: BLE001
            logger.warning("eta_shadow_lookup_failed history=%s: %s", tap.id, exc)
            return None, SOURCE_ERROR

    def _shadow_row(
        self,
        tap: DeliveryStatusHistory,
        now: datetime,
        tap_at: datetime,
        drive_minutes: Optional[float],
        source: str,
    ) -> DeliveryPromise:
        """Our own engine's time for the same leg: hidden, kept to judge the shown promise (§4.12a, "Accuracy data")."""
        return self._promise_row(
            tap,
            now,
            model_version=MODEL_OSRM_SHADOW,
            shown=False,
            expected_at=tap_at + timedelta(minutes=drive_minutes) if drive_minutes is not None else None,
            source=source,
        )

    @staticmethod
    def _start_of_leg(delivery: Delivery, tap_at: datetime) -> _LegStart:
        """Where the leg of the driver's tap starts, from their position on record: every own Arrived or Delivered tap
        moves it to that door (`DeliveryService._capture_arrival_position`), loading or returning bottles to the depot
        (`StaffService.anchor_driver_at_warehouse`) and a shared location to where they are.

        - At most ETA_POSITION_MAX_AGE_SECONDS old at the tap: fresh, that position.
        - Older, from the tap's Tashkent day: stale, that position, `from_depot` when it is the depot's stamp.
        - None from that day: stale, from the depot, where a driver's day starts.

        Read as the worker finds it: the row holds one value, so a position stamped since the tap counts as fresh. The
        worker runs within a second of the tap.
        """
        person = DeliveryPerson.query.filter_by(user_id=delivery.delivery_person_id).first()
        depot = get_warehouse_coordinates()
        if (
            person is None
            or person.current_location_lat is None
            or person.current_location_lng is None
            or person.last_location_update is None
        ):
            return _LegStart(depot, fresh=False, from_depot=True)
        seen_at = ensure_utc(person.last_location_update)
        position = (person.current_location_lat, person.current_location_lng)
        if seen_at >= tap_at - timedelta(seconds=current_app.config["ETA_POSITION_MAX_AGE_SECONDS"]):
            return _LegStart(position, fresh=True)
        if local_date(seen_at) != local_date(tap_at):
            return _LegStart(depot, fresh=False, from_depot=True)
        return _LegStart(position, fresh=False, from_depot=position == depot)

    @staticmethod
    def _leg_ratios(*, now: datetime, off_road: float) -> List[float]:
        """(actual drive - off-road minutes) / Google's drive time, for each leg headroom learns from.

        A leg is a shown `google_tap_v1` promise with a Google time, recorded in the four weeks before `now`. Its
        drive ends at the delivery's first real `arrived` after the tap, tapped by the delivery's own driver, and
        before the delivery's next real tap: an arrival that ended a later trip (failed, then out again) never
        pairs with an earlier tap. A leg without such an arrival, with a drive outside 1-180 minutes, or with a
        Google time under a minute teaches nothing. Nor does a leg interrupted strictly between its tap and that
        arrival (`_leg_uninterrupted`, spec §4.8 S2): handed to another driver, or its driver set off to another stop,
        so it was not one drive. An interruption after the arrival does not matter.
        """
        tap = aliased(DeliveryStatusHistory, name="tap")
        arrival = aliased(DeliveryStatusHistory, name="arrival")
        next_tap = aliased(DeliveryStatusHistory, name="next_tap")
        first_arrival_at = (
            select(func.min(arrival.changed_at))
            .where(
                arrival.delivery_id == tap.delivery_id,
                _moved_into(arrival, DeliveryStatus.ARRIVED),
                arrival.changed_by == Delivery.delivery_person_id,
                arrival.changed_at > tap.changed_at,
            )
            .correlate(tap, Delivery)
            .scalar_subquery()
        )
        next_tap_at = (
            select(func.min(next_tap.changed_at))
            .where(
                next_tap.delivery_id == tap.delivery_id,
                _moved_into(next_tap, DeliveryStatus.IN_TRANSIT),
                next_tap.changed_at > tap.changed_at,
            )
            .correlate(tap)
            .scalar_subquery()
        )
        legs = (
            db.session.query(tap.changed_at, DeliveryPromise.expected_at, first_arrival_at, next_tap_at)
            .select_from(DeliveryPromise)
            .join(tap, tap.id == DeliveryPromise.delivery_status_history_id)
            .join(Delivery, Delivery.id == tap.delivery_id)
            .filter(
                DeliveryPromise.model_version == MODEL_GOOGLE_TAP,
                DeliveryPromise.shown.is_(True),
                DeliveryPromise.expected_at.isnot(None),
                DeliveryPromise.computed_at >= now - _HEADROOM_WINDOW,
                # Bounded by the arrival the leg pairs with. With no arrival nothing interrupts it, and the leg is
                # skipped below for want of one.
                _leg_uninterrupted(tap, Delivery, before=first_arrival_at),
            )
            .all()
        )
        ratios = []
        for tapped_at, expected_at, arrived, next_tapped in legs:
            if arrived is None:
                continue
            if next_tapped is not None and ensure_utc(arrived) >= ensure_utc(next_tapped):
                continue  # the first arrival ended a later trip: this tap's own trip never arrived
            tapped_at = ensure_utc(tapped_at)
            drive = _minutes(tapped_at, ensure_utc(arrived))
            google_minutes = _minutes(tapped_at, ensure_utc(expected_at))
            if not _DRIVE_MINUTES_MIN <= drive <= _DRIVE_MINUTES_MAX or google_minutes < _GOOGLE_MINUTES_MIN:
                continue
            ratios.append((drive - off_road) / google_minutes)
        return ratios
