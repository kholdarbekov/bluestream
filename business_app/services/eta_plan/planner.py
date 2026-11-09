"""The planner: the day's cars times the waiting stops gives an expected arrival E, and a plan day, for every stop
(honest-ETA Phase 2 plan, R3, R4, R6, R8 to R11 and R19).

`build_plan` is pure. It reads the `Car`s of supply, the `Stop`s of demand and the fitted parameters, and nothing else:
no database, no routing provider and no clock. Every rule reads the `now` it is handed, every straight-line km comes
from the engine's memoised `km`, and the same inputs in any order give the same plan: cars are taken by driver id, stops
by order id, and every tie is broken on those.

Plan days (05:00 to 05:00 local, `work_day_of`) are built in turn: today, then the next ETA_UNCONFIRMED_HORIZON_DAYS.

- Driver-held stops (R8) ride on the car their driver owns or rides in. Today: the committed stop first, then the stored
  route's order, then nearest-next. A driver-held stop whose driver is in no car today is an orphan (R19).
- Timing (R4, R6). A car that has not started leaves the depot at max(now, its start, plus the loading allowance after a
  load), on the depot line; loaded and driven off before any tap, from its newest position, no earlier than now.
  A started car leaves its origin at `origin_at`, on the depot line when that leg left the depot: a driver-held first
  stop, a leg under way, is reached at max(now, origin_at + leg); a first stop the plan adds sets off no earlier than
  now, so an idle car reaches nothing "now". A depot leg that starts at a fix away from the depot, within the stop
  curve's last measured km of the stop, is a city leg on the curve (owner ruling 2026-11-09). Every later leg is the
  stop curve, and no stop is reached before its `not_before`: the car waits. The first stop that needs more bottles
  than are on board
  gets the car's one reload of the day, in place of its leg: back to the depot, the allowance, and the depot line out
  again. A car that has loaded twice today has had it. In-flight stops neither use up what is on board nor trigger it.
- Roll-over (R3, R10). A stop not in flight whose E passes its car's end goes to the next plan day, with the car's later
  stops not in flight. In-flight stops never roll, and stretch the car's end up to its roster end. A car that starts at
  or after its end takes no stop that day.
- Today's other stops (R9): first those left from before today's plan day began, then those dated today, by cheapest
  feasible insertion (no stop of the car, in flight or not, past its end); then today's new pool stops in the drivers'
  pool order, those with a door before the door-less ones (R7), each in the first wait it fills without delaying anyone
  (else at the end), on the car whose recorded window end is earliest. So a newer order moves no older order's E
  unless it outranks it in that order: a customer deadline ranks first, as drivers see it.
- Later days (R10): each car from the depot with a full load; the carried-over driver-held stops on their driver's car,
  nearest-next, then the other carried-over stops and the day's dated stops, by cheapest feasible insertion.
- A stop without coordinates (R7) is ETA_UNKNOWN_LEG_KM from everything, so nearest-next and cheapest insertion put it
  after every stop with a door, never first, and today's new pool stops place it after those with one. A stored
  route's order is kept as the driver set it.

A stop no car takes within the horizon is unconfirmed (no day, no time), unless the customer dated it after today: then
it keeps that date, without a time, as it does when its own day has no car.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from itertools import takewhile
from typing import Callable, Iterable, List, Mapping, NamedTuple, Optional, Sequence, Tuple

from business_app.services.eta_plan.demand import Stop
from business_app.services.eta_plan.params import EtaParams
from business_app.services.eta_plan.scope import IN_FLIGHT_DELIVERY_STATUSES, is_driver_held
from business_app.services.eta_plan.stop_time import cycle_minutes, depot_leg_minutes
from business_app.services.eta_plan.supply import Car
from business_app.services.eta_plan.windows import customer_bounded_window, lo_hi
from business_app.utils.local_windows import work_day_bounds, work_day_of

Point = Tuple[float, float]


@dataclass(frozen=True)
class PlannedStop:
    """Where the plan puts one stop. Every instant is aware UTC, and `driver_id` is `users.id`.

    - `driver_id`: the owner of the car that takes the stop; None when no car does.
    - `plan_day`: the plan day the stop is planned on. With no car: the customer's date when that is after today (the
      stop keeps its date, without a time), else None: the stop is unconfirmed.
    - `expected_at`: E, when the car reaches the door; None with no car.
    - `stops_ahead`: the stop's index in its car's sequence for that day; None with no car.
    - `day_started`: the car has started today (`Car.started`); always False on a later day.
    - `reload_before`: the car goes back to the depot to reload on its way to this stop.
    - `orphan`: a driver holds the stop but is in no car today (R19), so no car takes it.
    """

    stop: Stop
    driver_id: Optional[int]
    plan_day: Optional[date]
    expected_at: Optional[datetime]
    stops_ahead: Optional[int]
    day_started: bool
    reload_before: bool
    orphan: bool


def build_plan(
    cars_by_day: Mapping[date, Iterable[Car]],
    stops: Iterable[Stop],
    params: EtaParams,
    now: datetime,
    depot: Point,
    *,
    km: Callable[[Point, Point], float],
    config: Mapping,
) -> List[PlannedStop]:
    """One `PlannedStop` per stop, from the cars of today (`now`'s plan day) and of the next
    ETA_UNCONFIRMED_HORIZON_DAYS plan days; a day missing from `cars_by_day` has no cars.

    `depot` is where every car loads, `km` the straight-line distance between two points, and `config` holds the plan's
    tunables (the engine passes `current_app.config`). Runs inside an app context: the window rules read theirs there.
    """
    return _Planner(params, now, depot, km, config).plan(cars_by_day, stops)


class _Leg(NamedTuple):
    """Where and when a car's next leg starts, and whether it leaves the depot (priced on the depot line)."""

    point: Optional[Point]
    at: datetime
    from_depot: bool


class _Timed(NamedTuple):
    """One stop of a timed sequence: when the car gets there, E (the later of that and the stop's `not_before`), and
    whether the car reloads on its way."""

    arrival: datetime
    expected_at: datetime
    reload_before: bool


@dataclass
class _Route:
    """One car's stops on one plan day, in order, and the end they must keep to."""

    car: Car
    today: bool
    end_at: datetime
    stops: List[Stop] = field(default_factory=list)
    pinned: int = 0  # the committed stops at its head (R8), which nothing is put in front of

    @property
    def started(self) -> bool:
        return self.today and self.car.started

    @property
    def bottles_at_start(self) -> int:
        """Today what is on board; a later day starts with a full load (R6)."""
        return self.car.bottles_on_board if self.today else self.car.full_load

    @property
    def reloaded_at_start(self) -> bool:
        """The car has had the day's one reload already: a second load today (R6). A later day starts without one."""
        return self.today and self.car.reloaded_today

    @property
    def has_capacity(self) -> bool:
        """A car that starts at or after its end takes no stop that day."""
        return self.car.start_at < self.car.end_at

    @property
    def takes_waiting_stops(self) -> bool:
        """An unavailable car finishes the stops it holds and takes no other (R1)."""
        return self.car.available and self.has_capacity


class _Planner:
    """One plan: the context every rule reads, and one method per rule."""

    def __init__(
        self, params: EtaParams, now: datetime, depot: Point, km: Callable[[Point, Point], float], config: Mapping
    ):
        self.params = params
        self.now = now
        self.depot = depot
        self.km = km
        self.allowance = config["ETA_LOADING_ALLOWANCE_MINUTES"]
        self.unknown_km = config["ETA_UNKNOWN_LEG_KM"]
        self.horizon = config["ETA_UNCONFIRMED_HORIZON_DAYS"]
        self.today = work_day_of(now)

    def plan(self, cars_by_day: Mapping[date, Iterable[Car]], stops: Iterable[Stop]) -> List[PlannedStop]:
        stops = sorted(stops, key=_identity)
        todays_cars = _by_driver(cars_by_day.get(self.today, ()))
        orphans, waiting = [], []
        for stop in stops:
            is_orphan = _is_held(stop) and not any(_carries(car, stop.driver_id) for car in todays_cars)
            (orphans if is_orphan else waiting).append(stop)

        planned: List[PlannedStop] = []
        carried: List[Stop] = []
        for offset in range(self.horizon + 1):
            day = self.today + timedelta(days=offset)
            due = [stop for stop in waiting if self._due_day(stop) == day]
            day_planned, carried = self._plan_day(day, _by_driver(cars_by_day.get(day, ())), carried, due)
            planned.extend(day_planned)

        last_day = self.today + timedelta(days=self.horizon)
        beyond = [stop for stop in waiting if self._due_day(stop) > last_day]
        planned.extend(self._without_a_car(stop) for stop in carried + beyond)
        planned.extend(self._without_a_car(stop, orphan=True) for stop in orphans)
        return planned

    def _due_day(self, stop: Stop) -> date:
        """The first plan day a stop may be planned on: its `plan_day` (the customer's date when that is after today),
        never before today. An in-flight stop is on the car now: today."""
        if _in_flight(stop):
            return self.today
        return max(self.today, stop.plan_day)

    def _plan_day(
        self, day: date, cars: Sequence[Car], carried: Sequence[Stop], due: Sequence[Stop]
    ) -> Tuple[List[PlannedStop], List[Stop]]:
        """Plans `day` with the stops carried into it and those due on it. Returns its planned stops and the stops it
        rolls to the next day."""
        routes = [_Route(car=car, today=day == self.today, end_at=car.end_at) for car in cars]
        carried_others = self._hand_out_held(routes, carried)
        due_others = self._hand_out_held(routes, due)
        rolled: List[Stop] = []
        for route in routes:
            self._sequence(route)
            rolled.extend(self._roll_over(route))
        if day == self.today:
            unplaced = self._place_todays_others(routes, [*carried_others, *due_others])
        else:
            unplaced = self._insert_cheapest(routes, _in_pool_order(carried_others) + _in_pool_order(due_others))

        # A stop dated for a later day with no car that can take it keeps its date, without a time, rather than move on
        # (R10).
        no_car = day > self.today and not any(route.takes_waiting_stops for route in routes)
        kept = [stop for stop in unplaced if no_car and stop.customer_date == day]
        planned = [planned_stop for route in routes for planned_stop in self._planned(route, day)]
        planned.extend(self._without_a_car(stop) for stop in kept)
        rolled.extend(stop for stop in unplaced if stop not in kept)
        return planned, rolled

    @staticmethod
    def _hand_out_held(routes: Sequence[_Route], stops: Iterable[Stop]) -> List[Stop]:
        """Puts each driver-held stop on the car its driver owns or rides in (R8, R10), and returns the stops left:
        every other stop, and a driver-held one whose car is not in the day's supply."""
        others = []
        for stop in stops:
            holders = [route for route in routes if _is_held(stop) and _carries(route.car, stop.driver_id)]
            if holders:
                holders[0].stops.append(stop)
            else:
                others.append(stop)
        return others

    def _sequence(self, route: _Route) -> None:
        """Orders a car's driver-held stops (R8). Today: the committed stop, then the stored route, then nearest-next
        from there. A later day has neither (no route yet): all nearest-next from the depot."""
        held = sorted(route.stops, key=_identity)
        committed = [stop for stop in held if route.today and stop.committed]
        routed = sorted(
            (stop for stop in held if route.today and not stop.committed and stop.route_index is not None),
            key=lambda stop: (stop.route_index, _identity(stop)),
        )
        head = committed + routed
        rest = [stop for stop in held if stop not in head]
        start = head[-1].point if head else self._origin(route).point
        route.stops = head + self._nearest_next(start, rest)
        route.pinned = len(committed)

    def _nearest_next(self, start: Optional[Point], stops: Sequence[Stop]) -> List[Stop]:
        """Nearest-next from `start` over the stops with a door; those without one come after them all, by order id
        (R7): ETA_UNKNOWN_LEG_KM from everything, they would otherwise always be taken first."""
        remaining, ordered, point = [stop for stop in stops if stop.point is not None], [], start
        while remaining:
            nearest = min(remaining, key=lambda stop: (self._km(point, stop.point), _identity(stop)))
            remaining.remove(nearest)
            ordered.append(nearest)
            point = nearest.point
        return ordered + sorted((stop for stop in stops if stop.point is None), key=_identity)

    def _roll_over(self, route: _Route) -> List[Stop]:
        """R3 and R10: from the first stop not in flight whose E passes the car's end (from the first stop, for a car
        with no capacity), the stops not in flight leave the car for the next day. In-flight stops stay, and first
        stretch the end to their own. What remains is re-timed whenever it is next timed."""
        timed = list(zip(route.stops, self._time(route, route.stops)))
        route.end_at = _stretched_end(route.car, [t.expected_at for stop, t in timed if _in_flight(stop)])
        late = (i for i, (stop, t) in enumerate(timed) if not _in_flight(stop) and t.expected_at > route.end_at)
        cut = next(late, len(route.stops)) if route.has_capacity else 0
        rolled = [stop for stop in route.stops[cut:] if not _in_flight(stop)]
        route.stops = route.stops[:cut] + [stop for stop in route.stops[cut:] if _in_flight(stop)]
        return rolled

    def _place_todays_others(self, routes: Sequence[_Route], others: Sequence[Stop]) -> List[Stop]:
        """R9: the stops left from before today's plan day began, then those dated today, by cheapest insertion in
        R10's order; then today's new pool stops. Returns the stops no car can take today."""
        began = work_day_bounds(self.today)[0]
        dated = [stop for stop in others if stop.customer_date == self.today]
        earlier = [stop for stop in others if stop.customer_date != self.today and stop.created_at < began]
        new = [stop for stop in others if stop.customer_date != self.today and stop.created_at >= began]
        unplaced = self._insert_cheapest(routes, _in_pool_order(earlier) + _in_pool_order(dated))
        return unplaced + self._place_new_pool_stops(routes, _doors_first(_in_pool_order(new)))

    def _insert_cheapest(self, routes: Sequence[_Route], stops: Sequence[Stop]) -> List[Stop]:
        """R10, one stop at a time: the car and position that add the fewest minutes to that car's day. Returns the
        stops no car can take."""
        return _place_each(routes, stops, self._cheapest_position)

    def _place_new_pool_stops(self, routes: Sequence[_Route], stops: Sequence[Stop]) -> List[Stop]:
        """R9 for today's new pool stops, in the order given (pool order, doors first): each on the car whose recorded
        window end is earliest, where it delays no one. Returns the stops no car can take today."""
        return _place_each(routes, stops, self._first_fit_offer)

    def _cheapest_position(self, route: _Route, stop: Stop) -> Optional[Tuple[tuple, int]]:
        """R10's offer of one car: the position (`_positions`) that adds the fewest minutes to its day, where every stop
        of the car, in flight or not, keeps to its end (a windowed stop waits for its start). An in-flight stop may be
        delayed, but never past the end. Ties: the car that finishes earlier, the lower driver id, the earlier
        position. None when no position keeps to the end."""
        finish = self._finish(route, self._time(route, route.stops))
        offers = []
        for position in _positions(route, stop):
            timed = self._time(route, _inserted(route.stops, position, stop))
            if any(t.expected_at > route.end_at for t in timed):
                continue
            offers.append(((self._finish(route, timed) - finish, finish, route.car.driver_id, position), position))
        return min(offers, default=None)

    def _first_fit_offer(self, route: _Route, stop: Stop) -> Optional[Tuple[tuple, int]]:
        """R9's offer of one car: the first position where the stop delays no one (`_first_fit`), keyed by the window
        end the stop would be recorded with there. Ties: the earlier E, the car that finishes earlier, the lower driver
        id. None when E there would pass the car's end."""
        position, timed = self._first_fit(route, stop)
        expected = timed[position].expected_at
        if expected > route.end_at:
            return None
        finish = self._finish(route, self._time(route, route.stops))
        window_end = self._recorded_window_end(stop, expected, position, route.started)
        return (window_end, expected, finish, route.car.driver_id), position

    def _first_fit(self, route: _Route, stop: Stop) -> Tuple[int, List[_Timed]]:
        """Where a new pool stop goes on `route` (R9), with the car's sequence timed with it there: in front of the
        first stop the car waits at (for its `not_before`), when it fits in that wait without making any stop later;
        else at the end. So a late customer window never idles the car in front of other customers, and a newer stop
        never makes an older one later."""
        current = self._time(route, route.stops)
        for position in range(route.pinned, len(route.stops)):
            if current[position].arrival >= current[position].expected_at:
                continue  # the car does not wait here
            timed = self._time(route, _inserted(route.stops, position, stop))
            if all(timed[i + (i >= position)].expected_at <= t.expected_at for i, t in enumerate(current)):
                return position, timed
        return len(route.stops), self._time(route, [*route.stops, stop])

    def _recorded_window_end(self, stop: Stop, expected: datetime, stops_ahead: int, started: bool) -> datetime:
        """The end of the window the stop would be recorded with in this place (R11): the customer's own window's when
        E meets it, else E + hi of the bucket, unrounded."""
        lo, hi, _ = lo_hi(self.params, stops_ahead, started)
        has_window = stop.window_start is not None or stop.window_end is not None
        if has_window and (stop.window_end is None or expected <= stop.window_end):
            return customer_bounded_window(expected, lo, hi, self.now, stop.window_start, stop.window_end)[1]
        return expected + timedelta(minutes=hi)

    def _origin(self, route: _Route) -> _Leg:
        """Where and when a car's first leg starts (R4): its origin, a depot leg when its current leg left the depot. A
        started car leaves it at `origin_at`. Any other car sets off no earlier than now: supply gives it the depot at
        its start, plus the loading allowance after a load, or, loaded and driven off before any tap, its newest
        position."""
        car = route.car
        if route.started:
            return _Leg(car.origin, car.origin_at, car.leg_from_depot)
        return _Leg(car.origin, max(self.now, car.origin_at), car.leg_from_depot)

    def _time(self, route: _Route, sequence: Sequence[Stop]) -> List[_Timed]:
        """E for each stop of `sequence` on the route's car (R4, R6). The first stop is reached no earlier than now: a
        driver-held one at max(now, the leg's start + leg), the leg already under way; any other sets off no earlier
        than now, so an idle car reaches nothing "now". Each later leg is the stop curve; no stop is reached before its
        `not_before`, the car waits there. The first stop not in flight that needs more bottles than are on board gets
        the car's one reload of the day, unless it has had it, in place of its leg; a full load is then on board."""
        leg = self._origin(route)
        on_board, reloaded = route.bottles_at_start, route.reloaded_at_start
        timed: List[_Timed] = []
        for stop in sequence:
            in_flight = _in_flight(stop)
            reload = not in_flight and not reloaded and stop.bottles > on_board
            if reload:
                minutes = self._depot_leg(leg.point, self.depot) + self.allowance
                minutes += self._depot_leg(self.depot, stop.point)
                on_board, reloaded = route.car.full_load, True
            elif leg.from_depot and not self._across_the_city(leg.point, stop.point):
                minutes = self._depot_leg(leg.point, stop.point)
            else:
                minutes = self._cycle(leg.point, stop.point)
            if timed:
                arrival = leg.at + timedelta(minutes=minutes)
            elif _is_held(stop):
                arrival = max(self.now, leg.at + timedelta(minutes=minutes))
            else:
                arrival = max(self.now, leg.at) + timedelta(minutes=minutes)
            expected = arrival if stop.not_before is None else max(arrival, stop.not_before)
            if not in_flight:
                on_board -= stop.bottles
            timed.append(_Timed(arrival, expected, reload))
            leg = _Leg(stop.point, expected, False)
        return timed

    def _finish(self, route: _Route, timed: Sequence[_Timed]) -> datetime:
        """When the car's day of stops ends: its last E, or, with no stop, when it can set off."""
        return timed[-1].expected_at if timed else max(self.now, self._origin(route).at)

    def _km(self, a: Optional[Point], b: Optional[Point]) -> float:
        """Straight-line km. A leg with an end without coordinates is ETA_UNKNOWN_LEG_KM long (R7)."""
        if a is None or b is None:
            return self.unknown_km
        return self.km(a, b)

    def _depot_leg(self, a: Optional[Point], b: Optional[Point]) -> float:
        return depot_leg_minutes(self._km(a, b), self.params.depot_line)

    def _across_the_city(self, a: Optional[Point], b: Optional[Point]) -> bool:
        """A depot leg from `a` to `b` is timed on the stop curve when it starts away from the depot, at a fix (a
        location the driver shared, or a door they tapped), no farther from `b` than the curve's last measured point
        (owner ruling 2026-11-09: from a petrol station in the city, say). The depot line is the run in from the far
        depot, and too quick for a city leg. From the depot itself, or farther out, the leg stays on the line: a fix
        beside the far depot never prices its long run on the curve (R4)."""
        return a is not None and a != self.depot and self._km(a, b) <= self.params.cycle_points[-1][0]

    def _cycle(self, a: Optional[Point], b: Optional[Point]) -> float:
        return cycle_minutes(self._km(a, b), self.params.cycle_points)

    def _planned(self, route: _Route, day: date) -> List[PlannedStop]:
        timed = self._time(route, route.stops)
        return [
            PlannedStop(
                stop=stop,
                driver_id=route.car.driver_id,
                plan_day=day,
                expected_at=t.expected_at,
                stops_ahead=index,
                day_started=route.started,
                reload_before=t.reload_before,
                orphan=False,
            )
            for index, (stop, t) in enumerate(zip(route.stops, timed))
        ]

    def _without_a_car(self, stop: Stop, *, orphan: bool = False) -> PlannedStop:
        """R10's no-car rules: a stop the customer dated after today keeps that date, without a time; any other stop is
        unconfirmed, with neither."""
        dated_later = stop.customer_date is not None and stop.customer_date > self.today
        return PlannedStop(
            stop=stop,
            driver_id=None,
            plan_day=stop.customer_date if dated_later else None,
            expected_at=None,
            stops_ahead=None,
            day_started=False,
            reload_before=False,
            orphan=orphan,
        )


def _place_each(
    routes: Sequence[_Route], stops: Sequence[Stop], offer: Callable[[_Route, Stop], Optional[Tuple[tuple, int]]]
) -> List[Stop]:
    """Places `stops` in turn, each on the car with the lowest offer: `offer(route, stop)` is a (key, position) pair, or
    None when that car cannot take the stop. Only cars that take waiting stops are asked, and every key ends in its
    driver id, so no two offers tie. Returns the stops no car takes."""
    unplaced = []
    for stop in stops:
        offers = [(offer(route, stop), route) for route in routes if route.takes_waiting_stops]
        offers = [(made, route) for made, route in offers if made is not None]
        if not offers:
            unplaced.append(stop)
            continue
        (_, position), route = min(offers, key=lambda made_by: made_by[0][0])
        route.stops.insert(position, stop)
    return unplaced


def _stretched_end(car: Car, in_flight_ends: Sequence[datetime]) -> datetime:
    """R3: a car's end, stretched to the E of its last in-flight stop, but never past its roster end (None: no cap)."""
    if not in_flight_ends:
        return car.end_at
    reach = max(in_flight_ends)
    if car.roster_end_at is not None:
        reach = min(reach, car.roster_end_at)
    return max(car.end_at, reach)


def _identity(stop: Stop) -> Tuple[int, int]:
    """A stop's place in every tie: its order id (one stop per order), then its delivery id."""
    return stop.order_id, stop.delivery_id if stop.delivery_id is not None else -1


def _is_held(stop: Stop) -> bool:
    return is_driver_held(stop.status, stop.driver_id)


def _in_flight(stop: Stop) -> bool:
    return stop.status in IN_FLIGHT_DELIVERY_STATUSES


def _carries(car: Car, driver_id: Optional[int]) -> bool:
    """`driver_id` owns the car or rides along in it."""
    return driver_id == car.driver_id or driver_id in car.member_ids


def _by_driver(cars: Iterable[Car]) -> List[Car]:
    return sorted(cars, key=lambda car: car.driver_id)


def _in_pool_order(stops: Iterable[Stop]) -> List[Stop]:
    """By `pool_rank`. A driver-held stop whose car is not in the day's supply has no rank, and goes first: it was
    taken from the pool before any stop still in it."""
    return sorted(stops, key=lambda stop: (stop.pool_rank is not None, stop.pool_rank or 0, _identity(stop)))


def _doors_first(stops: Sequence[Stop]) -> List[Stop]:
    """R7 for today's new pool stops: those with a door, then the door-less ones, each part in the order given. Placed
    first on an empty car, a door-less stop would price its depot leg at ETA_UNKNOWN_LEG_KM and every later stop of
    that car from an unknown point. Only the stops still to place move: one already on a car stays where it is (R9)."""
    return [stop for stop in stops if stop.point is not None] + [stop for stop in stops if stop.point is None]


def _positions(route: _Route, stop: Stop) -> range:
    """Where cheapest insertion may put `stop` on `route`: never in front of its committed stops (R8), and a stop
    without a door only at the end, after every stop with one (R7), so a stop with a door goes in front of the
    door-less ones there. A door-less stop is ETA_UNKNOWN_LEG_KM from everything: anywhere else it would look closest
    to all and pull every later promise early."""
    if stop.point is None:
        return range(len(route.stops), len(route.stops) + 1)
    door_less_tail = sum(1 for _ in takewhile(lambda placed: placed.point is None, reversed(route.stops)))
    return range(route.pinned, max(route.pinned, len(route.stops) - door_less_tail) + 1)


def _inserted(sequence: Sequence[Stop], position: int, stop: Stop) -> List[Stop]:
    return [*sequence[:position], stop, *sequence[position:]]
