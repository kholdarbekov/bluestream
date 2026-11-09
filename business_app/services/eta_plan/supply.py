"""Supply: the cars that deliver on a plan day, and where each one stands (honest-ETA Phase 2 plan, R1-R4, R6, R10).

The planner places stops on `Car`s and reads nothing else about drivers, so every rule about who works, when, from
where and with how many bottles is decided here, once:

- Who works today (R1). A car is a driver's effective bottle session, `BottleTrackingService.get_effective_session`,
  called once per candidate driver so its precedence is never copied. A ride-along member adds no car: they join the
  session owner's, and so do their stops. A car works when its owner has an active profile and an active account, and
  its session is OPEN with activity in the last ETA_SESSION_ACTIVITY_HOURS (its start, its orders' real delivered
  taps, its transfers' sent, confirmed or resolved times; never a reopen or an order bind), or one of its drivers holds
  a stop (`driver_held_rows_filter`) with any status-history row in the last COMMITTED_STOP_MAX_AGE_HOURS.
- The early-morning set (R2), only while no car works today: active, available drivers whose roster shift
  (`shift_window_on`) still has time left in the plan day. `working_days` is never read: every day counts.
- Start and end (R3). The typical start is the median first real pickup, and the typical end the p75 last real drop
  capped at the roster end, over the driver's last ETA_HISTORY_WORK_DAYS plan days with delivered taps before today;
  the fleet's stand in below ETA_MIN_DRIVER_DAYS samples, and the seeds with no history at all. Today's own signals
  move them: a car on the road starts at its first move today and is never rolled; one not yet on it starts at its
  first load today (a session opened with bottles), else at its typical start, rolled to ETA_START_ROLL_MINUTES from
  now once ETA_START_GRACE_MINUTES overdue, and never before its roster shift starts. An accept moves nothing.
- Where the current leg started (R4), so the planner knows when the next leg leaves the depot (the depot line) and
  when a stop (the curve). The origin is the newer of that leg start and the driver's newest position on record, a
  shared location or a door tap, while it is at most 30 minutes old (owner rule 2026-11-09). A delivered stop without
  a door still starts the leg, from an unknown point. A car that loaded today but has not started leaves the depot at
  the load plus its allowance, or from a fresh position newer than the load (a driver who loaded and drove off, to a
  petrol station in the city say, before any tap), no earlier than its start. Without a load today it loads at the
  depot first.
- Bottles (R6): full bottles on board less those of the car's in-flight stops, or a full load for a car that will
  load at its start; and whether a second load today has already been the day's one reload.
- Later plan days (R10): today's working cars whose driver is available, else that day's early-morning set; each at
  its typical start and end, from the depot, with a full load.

A real move is one into a new status (`moved_into`) by the delivery's own driver. Plan days run 05:00 to 05:00 local
and rows are grouped into them in Python (`work_day_of`), never with an SQL time-zone function. Every rule reads the
one `now` the caller passes. The queries are grouped, plus `get_effective_session` per candidate driver: none per stop.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Dict, Iterable, List, NamedTuple, Optional, Sequence, Set, Tuple

from flask import current_app
from sqlalchemy import func, or_
from sqlalchemy.orm import selectinload

from business_app import db
from business_app.models.bottle import (
    DriverBottleSession,
    DriverBottleSessionOrder,
    DriverBottleTransfer,
    DriverSessionMembership,
)
from business_app.models.delivery import Delivery, DeliveryPerson, DeliveryStatusHistory, shift_window_on
from business_app.models.order import Order, OrderItem
from business_app.models.user import User
from business_app.services.bottle_tracking_service import BottleTrackingService
from business_app.services.eta_plan.common import moved_into, percentile
from business_app.services.eta_plan.scope import IN_FLIGHT_DELIVERY_STATUSES, driver_held_rows_filter
from business_app.services.route_optimization_service import RouteOptimizationService
from business_app.utils.helpers import get_warehouse_coordinates
from business_app.utils.local_windows import (
    local_instant_at_minutes,
    minutes_past_local_midnight,
    work_day_bounds,
    work_day_of,
)
from business_app.utils.timezone_utils import ensure_utc
from shared.enums import DeliveryStatus, DriverBottleSessionStatus, DriverSessionMembershipStatus, UserStatus

Point = Tuple[float, float]

# A real move into any of these, made today, says the car is on the road (R3: "a picked_up or later tap").
_ON_THE_ROAD = IN_FLIGHT_DELIVERY_STATUSES + (DeliveryStatus.DELIVERED,)
# The sessions a full load is learned from (R6): finished trips, an admin's force-close included.
_CLOSED_SESSION_STATUSES = (DriverBottleSessionStatus.CLOSED, DriverBottleSessionStatus.FORCE_CLOSED)


@dataclass(frozen=True)
class Car:
    """One car on one plan day, as the planner reads it. Every instant is aware UTC, and every id is `users.id`.

    - `driver_id` owns the session; `member_ids` ride along with it (sorted). `available` is the owner's
      `is_available`: an unavailable car finishes the stops it holds and takes no pool stop (R1).
    - `working`: one of R1's working cars (on a later day, one of today's). False for R2's early-morning stand-in,
      which the plan uses while no car works: it is what the promise counts as drivers working (spec §4.2).
    - `started`: a real pickup or later tap today by one of the car's drivers (R3). Never on a later day.
    - `start_at`: a started car's first move today. One not started: its first load today (`start_is_load`), else
      its typical start, rolled forward once overdue; never before the roster shift that starts on the day. `end_at`:
      the typical end, capped at `roster_end_at`, the end of that shift (`shift_window_on`), or None for a car with no
      roster, which has no cap.
    - `origin` / `origin_at`: where and when the next leg starts from (R4). `origin` is None when the leg starts at a
      delivered stop whose door is unknown (its address had no coordinates); the planner then prices the first leg as
      `cycle_minutes(ETA_UNKNOWN_LEG_KM)`, R7's convention for a stop without coordinates. `leg_from_depot`: the
      current leg left the depot, so its first leg is a depot leg even from a later position (the planner times a
      depot leg from a position near the stop on the stop curve).
    - `bottles_on_board`: full bottles not yet promised to an in-flight stop, never below 0. `full_load`: what the car
      carries after loading (R6). `reloaded_today`: the car has loaded a second time in today's plan day, so it has
      had the day's one reload and the planner adds none (R6). Never on a later day.
    """

    driver_id: int
    member_ids: Tuple[int, ...]
    available: bool
    working: bool
    started: bool
    start_at: datetime
    end_at: datetime
    roster_end_at: Optional[datetime]
    start_is_load: bool
    origin: Optional[Point]  # None: a door-less stop; the planner's first leg is cycle_minutes(ETA_UNKNOWN_LEG_KM)
    origin_at: datetime
    leg_from_depot: bool
    bottles_on_board: int
    full_load: int
    reloaded_today: bool


@dataclass
class _Crew:
    """A car before its times are known: the session owner, who rides along, and the session if it is active."""

    driver_id: int
    member_ids: Set[int] = field(default_factory=set)
    session: Optional[DriverBottleSession] = None

    @property
    def drivers(self) -> Tuple[int, ...]:
        return (self.driver_id, *sorted(self.member_ids))


class _Move(NamedTuple):
    at: datetime
    status: DeliveryStatus
    point: Optional[Point]


class _Fix(NamedTuple):
    """A crew member's position on record (`delivery_persons`): a location they shared, the door of their last own
    Arrived or Delivered tap, or the depot a load stamps (`StaffService.anchor_driver_at_warehouse`), whichever came
    last. `fresh`: `location_status_for_person` says so as of `now`: at most DRIVER_LOCATION_FRESH_SECONDS (30 minutes)
    old. Only a fresh position is ever an origin (owner rule 2026-11-09)."""

    at: datetime
    point: Point
    fresh: bool


@dataclass(frozen=True)
class _TypicalTimes:
    """R3's typical start and end, in minutes past a plan day's local midnight. A driver below ETA_MIN_DRIVER_DAYS
    samples of one measure is missing from its dict and gets the fleet's value."""

    starts: Dict[int, float]
    ends: Dict[int, float]
    fleet_start: float
    fleet_end: float

    def start(self, driver_id: int) -> float:
        return self.starts.get(driver_id, self.fleet_start)

    def end(self, driver_id: int) -> float:
        return self.ends.get(driver_id, self.fleet_end)


def cars_for_day(day: date, now: datetime) -> List[Car]:
    """The cars of plan day `day` as of `now`, ordered by driver id: today's per R1-R4 and R6, a later day's per R10."""
    now = ensure_utc(now)
    today = work_day_of(now)
    if day < today:
        raise ValueError(f"plan day {day} is before today's, {today}")
    people = _eligible_people()
    working = _working_crews(people, now)
    if day > today:
        working = [crew for crew in working if people[crew.driver_id].is_available]  # R10: closed for later days
    crews = working or _early_morning_crews(people, day, now)
    if not crews:
        return []
    typical = _typical_times(today)
    if day == today:
        return _todays_cars(crews, people, typical, day, now, working=bool(working))
    return _later_day_cars(crews, people, typical, day, working=bool(working))


def _eligible_people() -> Dict[int, DeliveryPerson]:
    """The drivers who may form a car at all, by `users.id`: an active profile and an active account."""
    rows = (
        DeliveryPerson.query.join(User, User.id == DeliveryPerson.user_id)
        .filter(DeliveryPerson.is_active.is_(True), User.status == UserStatus.ACTIVE)
        .all()
    )
    return {person.user_id: person for person in rows}


def _working_crews(people: Dict[int, DeliveryPerson], now: datetime) -> List[_Crew]:
    """R1: today's working cars, by owner."""
    config = current_app.config
    open_sessions = DriverBottleSession.query.filter(DriverBottleSession.status == DriverBottleSessionStatus.OPEN).all()
    active_ids = _active_session_ids(open_sessions, now - timedelta(hours=config["ETA_SESSION_ACTIVITY_HOURS"]))
    holders = _recent_holders(now - timedelta(hours=config["COMMITTED_STOP_MAX_AGE_HOURS"]))
    members = {
        member_id
        for (member_id,) in db.session.query(DriverSessionMembership.member_driver_id).filter(
            DriverSessionMembership.status == DriverSessionMembershipStatus.ACTIVE
        )
    }
    owners = {session.driver_user_id for session in open_sessions}
    sessions = BottleTrackingService()
    crews: Dict[int, _Crew] = {}
    for driver_id in sorted(set(people) & (owners | members | holders)):
        session = sessions.get_effective_session(driver_id)
        owner = session.driver_user_id if session is not None else driver_id
        crew = crews.setdefault(owner, _Crew(owner))
        if owner != driver_id:
            crew.member_ids.add(driver_id)
        if session is not None and session.id in active_ids:
            crew.session = session
    return [
        crew
        for owner, crew in sorted(crews.items())
        if owner in people and (crew.session is not None or holders.intersection(crew.drivers))
    ]


def _active_session_ids(open_sessions: Sequence[DriverBottleSession], since: datetime) -> Set[int]:
    """The open sessions with activity at or after `since` (R1a): a start, a real delivered tap on one of their
    orders, or a transfer sent, confirmed or resolved. A reopen, an order bind and a collection are not activity."""
    active = {session.id for session in open_sessions if ensure_utc(session.started_at) >= since}
    ids = [session.id for session in open_sessions]
    if not ids:
        return active
    delivered = (
        db.session.query(DriverBottleSessionOrder.session_id)
        .join(Delivery, Delivery.order_id == DriverBottleSessionOrder.order_id)
        .join(DeliveryStatusHistory, DeliveryStatusHistory.delivery_id == Delivery.id)
        .filter(
            DriverBottleSessionOrder.session_id.in_(ids),
            moved_into(DeliveryStatusHistory, DeliveryStatus.DELIVERED),
            DeliveryStatusHistory.changed_by == Delivery.delivery_person_id,
            DeliveryStatusHistory.changed_at >= since,
        )
        .distinct()
    )
    active.update(session_id for (session_id,) in delivered)
    transfers = db.session.query(
        DriverBottleTransfer.sender_session_id, DriverBottleTransfer.receiver_session_id
    ).filter(
        or_(DriverBottleTransfer.sender_session_id.in_(ids), DriverBottleTransfer.receiver_session_id.in_(ids)),
        or_(
            DriverBottleTransfer.sent_at >= since,
            DriverBottleTransfer.confirmed_at >= since,
            DriverBottleTransfer.resolved_at >= since,
        ),
    )
    active.update(session_id for pair in transfers for session_id in pair if session_id in ids)
    return active


def _recent_holders(since: datetime) -> Set[int]:
    """R1b: the drivers holding a stop with any status-history row at or after `since`. A reassign row counts, so a
    stop an admin has just moved keeps its new driver working."""
    rows = (
        db.session.query(Delivery.delivery_person_id)
        .join(Order, Order.id == Delivery.order_id)
        .join(DeliveryStatusHistory, DeliveryStatusHistory.delivery_id == Delivery.id)
        .filter(driver_held_rows_filter(), DeliveryStatusHistory.changed_at >= since)
        .distinct()
    )
    return {driver_id for (driver_id,) in rows}


def _early_morning_crews(people: Dict[int, DeliveryPerson], day: date, now: datetime) -> List[_Crew]:
    """R2: the available drivers whose roster shift still has time left in plan day `day`."""
    return [
        _Crew(driver_id)
        for driver_id, person in sorted(people.items())
        if person.is_available and _has_shift_time_left(person, day, now)
    ]


def _has_shift_time_left(person: DeliveryPerson, day: date, now: datetime) -> bool:
    """A shift of the local date `day - 1` or `day` with time after max(now, the plan day's start) and before its end.
    The day before counts, so an overnight shift that runs into the plan day is in it."""
    day_start, day_end = work_day_bounds(day)
    since = max(now, day_start)
    for shift_day in (day - timedelta(days=1), day):
        shift = shift_window_on(shift_day, person.working_hours_start, person.working_hours_end)
        if shift is not None and min(shift[1], day_end) > max(shift[0], since):
            return True
    return False


def _roster(person: DeliveryPerson, day: date) -> Tuple[Optional[datetime], Optional[datetime]]:
    """The (start, end) of the roster shift that starts on `day`; (None, None) for no roster."""
    shift = shift_window_on(day, person.working_hours_start, person.working_hours_end)
    return shift if shift is not None else (None, None)


def _floored(start: datetime, roster_start: Optional[datetime]) -> datetime:
    """A start never before the roster shift's, as an end is never after it (`_capped`, R3)."""
    return max(start, roster_start) if roster_start is not None else start


def _capped(end: datetime, roster_end: Optional[datetime]) -> datetime:
    return min(end, roster_end) if roster_end is not None else end


def _typical_times(today: date) -> _TypicalTimes:
    """R3 over the plan days before today, within ETA_HISTORY_LOOKBACK_DAYS. Today's is not over: its last drop so far
    would pull the end early."""
    config = current_app.config
    first_pickup: Dict[Tuple[int, date], int] = {}
    last_drop: Dict[Tuple[int, date], int] = {}
    history = (
        db.session.query(
            DeliveryStatusHistory.changed_by, DeliveryStatusHistory.new_status, DeliveryStatusHistory.changed_at
        )
        .join(Delivery, Delivery.id == DeliveryStatusHistory.delivery_id)
        .filter(
            DeliveryStatusHistory.changed_by == Delivery.delivery_person_id,
            or_(
                moved_into(DeliveryStatusHistory, DeliveryStatus.PICKED_UP),
                moved_into(DeliveryStatusHistory, DeliveryStatus.DELIVERED),
            ),
            DeliveryStatusHistory.changed_at
            >= work_day_bounds(today - timedelta(days=config["ETA_HISTORY_LOOKBACK_DAYS"]))[0],
            DeliveryStatusHistory.changed_at < work_day_bounds(today)[0],
        )
    )
    for driver_id, status, changed_at in history:
        at = ensure_utc(changed_at)
        key = (driver_id, work_day_of(at))
        minutes = minutes_past_local_midnight(at, key[1])
        if status is DeliveryStatus.PICKED_UP:
            first_pickup[key] = min(minutes, first_pickup.get(key, minutes))
        else:
            last_drop[key] = max(minutes, last_drop.get(key, minutes))

    # The days that count are the ones with delivered taps: a driver's own last ETA_HISTORY_WORK_DAYS, and the
    # fleet's last as many, over which every driver's day is a sample.
    window = config["ETA_HISTORY_WORK_DAYS"]
    days_by_driver: Dict[int, List[date]] = defaultdict(list)
    for driver_id, day in last_drop:
        days_by_driver[driver_id].append(day)
    fleet_days = set(sorted({day for _, day in last_drop}, reverse=True)[:window])
    fleet_keys = [key for key in last_drop if key[1] in fleet_days]
    fleet_starts = [first_pickup[key] for key in fleet_keys if key in first_pickup]
    fleet_ends = [last_drop[key] for key in fleet_keys]

    minimum = config["ETA_MIN_DRIVER_DAYS"]
    starts: Dict[int, float] = {}
    ends: Dict[int, float] = {}
    for driver_id, days in days_by_driver.items():
        recent = [(driver_id, day) for day in sorted(days, reverse=True)[:window]]
        own_starts = [first_pickup[key] for key in recent if key in first_pickup]
        own_ends = [last_drop[key] for key in recent]
        if len(own_starts) >= minimum:
            starts[driver_id] = percentile(own_starts, 0.5)
        if len(own_ends) >= minimum:
            ends[driver_id] = percentile(own_ends, 0.75)
    return _TypicalTimes(
        starts=starts,
        ends=ends,
        fleet_start=percentile(fleet_starts, 0.5) if fleet_starts else config["ETA_FLEET_START_SEED_MINUTES"],
        fleet_end=percentile(fleet_ends, 0.75) if fleet_ends else config["ETA_FLEET_END_SEED_MINUTES"],
    )


def _full_loads(driver_ids: Iterable[int]) -> Tuple[Dict[int, int], int]:
    """R6's full load: by driver, the median `bottles_loaded` of their last ETA_FULL_LOAD_SESSIONS closed sessions that
    had a load; and the fleet's median over its last as many, else ETA_RELOAD_FULL_BOTTLES_DEFAULT. Whole bottles,
    rounded down: a car is never planned with a bottle it is not likely to carry."""
    config = current_app.config
    count = config["ETA_FULL_LOAD_SESSIONS"]
    closed_with_a_load = (
        DriverBottleSession.status.in_(_CLOSED_SESSION_STATUSES),
        DriverBottleSession.bottles_loaded > 0,
    )
    newest_first = (DriverBottleSession.started_at.desc(), DriverBottleSession.id.desc())
    ranked = (
        db.session.query(
            DriverBottleSession.driver_user_id.label("driver_id"),
            DriverBottleSession.bottles_loaded.label("loaded"),
            func.row_number()
            .over(partition_by=DriverBottleSession.driver_user_id, order_by=newest_first)
            .label("newest"),
        )
        .filter(*closed_with_a_load, DriverBottleSession.driver_user_id.in_(list(driver_ids)))
        .subquery()
    )
    loads: Dict[int, List[int]] = defaultdict(list)
    for driver_id, loaded in db.session.query(ranked.c.driver_id, ranked.c.loaded).filter(ranked.c.newest <= count):
        loads[driver_id].append(loaded)
    fleet = [
        loaded
        for (loaded,) in db.session.query(DriverBottleSession.bottles_loaded)
        .filter(*closed_with_a_load)
        .order_by(*newest_first)
        .limit(count)
    ]
    fleet_load = int(percentile(fleet, 0.5)) if fleet else config["ETA_RELOAD_FULL_BOTTLES_DEFAULT"]
    return {driver_id: int(percentile(values, 0.5)) for driver_id, values in loads.items()}, fleet_load


def _later_day_cars(
    crews: Sequence[_Crew], people: Dict[int, DeliveryPerson], typical: _TypicalTimes, day: date, *, working: bool
) -> List[Car]:
    """R10: not started, at the typical start and end, from the depot, with a full load. `working`: the crews are
    today's working cars, not the day's early-morning set."""
    depot = get_warehouse_coordinates()
    loads, fleet_load = _full_loads(crew.driver_id for crew in crews)
    cars = []
    for crew in crews:
        person = people[crew.driver_id]
        roster_start, roster_end_at = _roster(person, day)
        start_at = _floored(local_instant_at_minutes(day, typical.start(crew.driver_id)), roster_start)
        full_load = loads.get(crew.driver_id, fleet_load)
        cars.append(
            Car(
                driver_id=crew.driver_id,
                member_ids=tuple(sorted(crew.member_ids)),
                available=bool(person.is_available),
                working=working,
                started=False,
                start_at=start_at,
                end_at=_capped(local_instant_at_minutes(day, typical.end(crew.driver_id)), roster_end_at),
                roster_end_at=roster_end_at,
                start_is_load=False,
                origin=depot,
                origin_at=start_at,
                leg_from_depot=True,
                bottles_on_board=full_load,
                full_load=full_load,
                reloaded_today=False,
            )
        )
    return cars


def _todays_cars(
    crews: Sequence[_Crew],
    people: Dict[int, DeliveryPerson],
    typical: _TypicalTimes,
    day: date,
    now: datetime,
    *,
    working: bool,
) -> List[Car]:
    """R3, R4 and R6 for today's cars, from today's signals: loads, real moves, live positions, in-flight stops.
    `working`: the crews are R1's working cars, not the early-morning set."""
    config = current_app.config
    allowance = timedelta(minutes=config["ETA_LOADING_ALLOWANCE_MINUTES"])
    grace = timedelta(minutes=config["ETA_START_GRACE_MINUTES"])
    roll = timedelta(minutes=config["ETA_START_ROLL_MINUTES"])
    depot = get_warehouse_coordinates()
    bounds = work_day_bounds(day)
    owners = [crew.driver_id for crew in crews]
    drivers = sorted({driver_id for crew in crews for driver_id in crew.drivers})
    moves = _moves_today(drivers, bounds)
    loads_today = loads_in(owners, bounds)
    in_flight = _in_flight_bottles(drivers)
    full_loads, fleet_load = _full_loads(owners)
    cars = []
    for crew in crews:
        person = people[crew.driver_id]
        crew_moves = sorted((move for driver_id in crew.drivers for move in moves[driver_id]), key=lambda m: m.at)
        pickups = [move.at for move in crew_moves if move.status is DeliveryStatus.PICKED_UP]
        loads = loads_today[crew.driver_id]
        started = bool(crew_moves)
        roster_start, roster_end_at = _roster(person, day)
        if started:
            start_at, start_is_load = crew_moves[0].at, False  # on the road: its first move, never rolled forward
        else:
            typical_start = local_instant_at_minutes(day, typical.start(crew.driver_id))
            if loads:
                start_at, start_is_load = loads[0], True
            elif now > typical_start + grace:
                start_at, start_is_load = now + roll, False  # overdue with no sign of a start: it starts soon
            else:
                start_at, start_is_load = typical_start, False
            floored = _floored(start_at, roster_start)
            if floored != start_at:
                # Not out before its shift. A load made before it is long done by then: no allowance follows.
                start_at, start_is_load = floored, False

        crew_fixes = _fixes((people[driver_id] for driver_id in crew.drivers), now)
        if started:
            origin, origin_at, leg_from_depot = _started_origin(
                crew_moves, pickups, loads, crew_fixes, depot, bounds, allowance
            )
        else:
            origin, origin_at, leg_from_depot = depot, start_at + (allowance if start_is_load else timedelta(0)), True
            # Loaded, then a newer fresh position before any tap: it wins, with no wait for the loading allowance, and
            # the car sets off from it no earlier than its start (owner rule 2026-11-09). The load's own depot stamp, a
            # moment after the session starts, is the load itself and moves nothing.
            newest = _newest_fresh_fix([fix for fix in crew_fixes if fix.point != depot])
            if loads and newest is not None and newest.at > loads[-1]:
                origin, origin_at = newest.point, max(newest.at, start_at)

        full_load = full_loads.get(crew.driver_id, fleet_load)
        if crew.session is None or (not started and not loads):
            bottles_on_board = full_load  # it loads at its start, as on later days
        else:
            promised = sum(in_flight[driver_id] for driver_id in crew.drivers)
            bottles_on_board = max(0, crew.session.full_bottles_on_board - promised)

        cars.append(
            Car(
                driver_id=crew.driver_id,
                member_ids=tuple(sorted(crew.member_ids)),
                available=bool(person.is_available),
                working=working,
                started=started,
                start_at=start_at,
                end_at=_capped(local_instant_at_minutes(day, typical.end(crew.driver_id)), roster_end_at),
                roster_end_at=roster_end_at,
                start_is_load=start_is_load,
                origin=origin,
                origin_at=origin_at,
                leg_from_depot=leg_from_depot,
                bottles_on_board=bottles_on_board,
                full_load=full_load,
                reloaded_today=len(loads) > 1,
            )
        )
    return cars


def _started_origin(
    moves: Sequence[_Move],
    pickups: Sequence[datetime],
    loads: Sequence[datetime],
    fixes: Sequence[_Fix],
    depot: Point,
    bounds: Tuple[datetime, datetime],
    allowance: timedelta,
) -> Tuple[Optional[Point], datetime, bool]:
    """R4 for a car on the road today: (origin, origin_at, leg_from_depot).

    The current leg started at the depot when the latest depot departure today is newer than the last delivered stop,
    and at that stop otherwise. A depot departure is: after a load, the later of the load plus the loading allowance and
    the first pickup after it; a fix at the depot (`StaffService.anchor_driver_at_warehouse`), at its time; and, with
    no load today, the first pickup today, the event the depot line is fitted from. A delivered stop's point is the
    door on its row (`_capture_arrival_position` stamps every one whose address has coordinates); one without a door
    still ends the leg before it, at its time, from an unknown point (None). A car with neither, on the road only since
    before the plan day began, is taken to have left the depot at its first move today.

    The origin is the newer of that leg start and the crew's newest position on record when it is fresh (at most
    DRIVER_LOCATION_FRESH_SECONDS old): the newest evidence wins (owner rule 2026-11-09). `leg_from_depot` stays the
    leg's: the planner decides how a depot leg from a position is timed.
    """
    departures = [max(load + allowance, next((p for p in pickups if p >= load), load)) for load in loads]
    if not loads and pickups:
        departures.append(pickups[0])
    departures.extend(fix.at for fix in fixes if fix.point == depot and bounds[0] <= fix.at < bounds[1])
    stops = [move for move in moves if move.status is DeliveryStatus.DELIVERED]
    last_stop = stops[-1] if stops else None
    departed = max(departures, default=None)
    if departed is not None and (last_stop is None or departed > last_stop.at):
        leg_start, leg_start_at, leg_from_depot = depot, departed, True
    elif last_stop is not None:
        leg_start, leg_start_at, leg_from_depot = last_stop.point, last_stop.at, False
    else:
        leg_start, leg_start_at, leg_from_depot = depot, moves[0].at, True

    newest = _newest_fresh_fix(fixes)
    if newest is not None and newest.at > leg_start_at:
        return newest.point, newest.at, leg_from_depot
    return leg_start, leg_start_at, leg_from_depot


def _fixes(crew_people: Iterable[DeliveryPerson], now: datetime) -> List[_Fix]:
    """The position on record of each of the crew who has one, and whether it is fresh as of `now`."""
    return [
        _Fix(
            ensure_utc(person.last_location_update),
            (person.current_location_lat, person.current_location_lng),
            RouteOptimizationService.location_status_for_person(person, now=now) == "fresh",
        )
        for person in crew_people
        if person.current_location_lat is not None
        and person.current_location_lng is not None
        and person.last_location_update is not None
    ]


def _newest_fresh_fix(fixes: Sequence[_Fix]) -> Optional[_Fix]:
    return max((fix for fix in fixes if fix.fresh), key=lambda fix: fix.at, default=None)


def _moves_today(driver_ids: Sequence[int], bounds: Tuple[datetime, datetime]) -> Dict[int, List[_Move]]:
    """Each driver's real moves today into picked_up, in_transit, arrived or delivered, with the door on the row."""
    moves: Dict[int, List[_Move]] = defaultdict(list)
    if not driver_ids:
        return moves
    rows = (
        db.session.query(
            DeliveryStatusHistory.changed_by,
            DeliveryStatusHistory.new_status,
            DeliveryStatusHistory.changed_at,
            DeliveryStatusHistory.location_lat,
            DeliveryStatusHistory.location_lng,
        )
        .join(Delivery, Delivery.id == DeliveryStatusHistory.delivery_id)
        .filter(
            DeliveryStatusHistory.changed_by.in_(driver_ids),
            DeliveryStatusHistory.changed_by == Delivery.delivery_person_id,
            or_(*(moved_into(DeliveryStatusHistory, status) for status in _ON_THE_ROAD)),
            DeliveryStatusHistory.changed_at >= bounds[0],
            DeliveryStatusHistory.changed_at < bounds[1],
        )
    )
    for driver_id, status, changed_at, lat, lng in rows:
        point = (lat, lng) if lat is not None and lng is not None else None
        moves[driver_id].append(_Move(ensure_utc(changed_at), status, point))
    return moves


def loads_in(owner_ids: Sequence[int], bounds: Tuple[datetime, datetime]) -> Dict[int, List[datetime]]:
    """Each owner's loads in `bounds`, a half-open pair of instants, oldest first: their sessions opened with bottles. A
    session a transfer opened (nothing loaded) is activity, never a load. Supply asks it for the plan day (R3); the
    weekly fit (`calibration`) asks it for its window, so what a load is stays defined here once."""
    loads: Dict[int, List[datetime]] = defaultdict(list)
    if not owner_ids:
        return loads
    rows = db.session.query(DriverBottleSession.driver_user_id, DriverBottleSession.started_at).filter(
        DriverBottleSession.driver_user_id.in_(owner_ids),
        DriverBottleSession.bottles_loaded > 0,
        DriverBottleSession.started_at >= bounds[0],
        DriverBottleSession.started_at < bounds[1],
    )
    for driver_id, started_at in rows:
        loads[driver_id].append(ensure_utc(started_at))
    for times in loads.values():
        times.sort()
    return loads


def _in_flight_bottles(driver_ids: Sequence[int]) -> Dict[int, int]:
    """Each driver's bottles on in-flight stops (R6), booked as the session books them (`calculate_bottles_for_order`).
    Supply subtracts them once; the planner never does again."""
    bottles: Dict[int, int] = defaultdict(int)
    if not driver_ids:
        return bottles
    rows = (
        db.session.query(Delivery.delivery_person_id, Order)
        .select_from(Delivery)
        .join(Order, Order.id == Delivery.order_id)
        .options(selectinload(Order.order_items).joinedload(OrderItem.product))
        .filter(
            Delivery.delivery_person_id.in_(driver_ids),
            Delivery.status.in_(IN_FLIGHT_DELIVERY_STATUSES),
            driver_held_rows_filter(),
        )
    )
    for driver_id, order in rows:
        bottles[driver_id] += int(BottleTrackingService.calculate_bottles_for_order(order))
    return bottles
