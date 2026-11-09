"""The planner of the capacity-aware ETA plan (honest-ETA Phase 2, Task 5): the day's cars times the waiting stops gives
an expected arrival E, and a plan day, for every stop.

`build_plan` is pure, so each test builds its `Car`s and `Stop`s as plain values, in the shapes supply (Task 2) and
demand (Task 3) give them, and hands `now` in: no database, and no dependence on the time of day. The depot is the
default one, (41.2995, 69.2401), unless a test moves to the far production depot. Doors sit at known straight-line km
from it (`point`), measured by the production `calculate_distance`, and every expected minute is a sum of the production
`cycle_minutes` and `depot_leg_minutes` on the seed parameters, compared with `pytest.approx`. `TODAY` is Wednesday
2026-11-04, and `at` reads the Tashkent wall clock.
"""

import dataclasses
import random
from datetime import date, datetime, time, timedelta
from typing import Dict, Iterable, Optional, Sequence, Tuple

import pytest
from geopy.distance import geodesic

from business_app.services.eta_plan.common import minutes_between
from business_app.services.eta_plan.demand import Stop
from business_app.services.eta_plan.params import EtaParams
from business_app.services.eta_plan.planner import PlannedStop, build_plan
from business_app.services.eta_plan.stop_time import cycle_minutes, depot_leg_minutes
from business_app.services.eta_plan.supply import Car
from business_app.services.eta_plan.windows import customer_bounded_window, lo_hi
from business_app.utils.delivery_window import window_edge_utc
from business_app.utils.helpers import calculate_distance
from business_app.utils.local_windows import work_day_of
from shared import business_config
from shared.enums import DeliveryStatus

pytestmark = pytest.mark.unit

Point = Tuple[float, float]

TODAY = date(2026, 11, 4)
YESTERDAY = TODAY - timedelta(days=1)
TOMORROW = TODAY + timedelta(days=1)
DEPOT = (41.2995, 69.2401)
# The production depot, about 28.9 km from the centre (R4), and the bearing from it towards the centre.
FAR_DEPOT = (41.050398, 69.139306)
CITYWARD = 45.0
SEED = EtaParams(
    cycle_points=business_config.ETA_CYCLE_SEED_POINTS,
    depot_line=business_config.ETA_DEPOT_LEG_SEED,
    window_buckets={},
    source="seed",
)


def at(hhmm: str, day: date = TODAY) -> datetime:
    """`hhmm` on the Tashkent wall clock on the local date `day`, as the aware UTC instant the plan works in."""
    return window_edge_utc(day, time.fromisoformat(hhmm))


def days(n: int) -> date:
    return TODAY + timedelta(days=n)


def after(instant: datetime, minutes: float) -> datetime:
    return instant + timedelta(minutes=minutes)


def km(a: Point, b: Point) -> float:
    """The engine's straight-line km between two points: the production geodesic distance."""
    return calculate_distance(a[0], a[1], b[0], b[1])


def point(distance_km: float, *, origin: Point = DEPOT, bearing: float = 0.0) -> Point:
    """The point `distance_km` straight-line km from `origin` on `bearing` (due north by default). Points on one bearing
    from one origin lie on one geodesic, so they are as far apart as their distances differ."""
    destination = geodesic(kilometers=distance_km).destination(origin, bearing)
    return destination.latitude, destination.longitude


def cycle(a: Optional[Point], b: Optional[Point], app=None) -> float:
    """The stop curve's minutes from `a` to `b`; with an end without coordinates, ETA_UNKNOWN_LEG_KM's."""
    if a is None or b is None:
        return cycle_minutes(app.config["ETA_UNKNOWN_LEG_KM"], SEED.cycle_points)
    return cycle_minutes(km(a, b), SEED.cycle_points)


def depot_leg(a: Point, b: Point) -> float:
    """The depot line's minutes from `a` to `b`."""
    return depot_leg_minutes(km(a, b), SEED.depot_line)


def waiting_car(
    driver_id: int = 1,
    *,
    day: date = TODAY,
    start_at: Optional[datetime] = None,
    end_at: Optional[datetime] = None,
    roster_end_at: Optional[datetime] = None,
    load: bool = False,
    available: bool = True,
    members: Sequence[int] = (),
    bottles: int = 30,
    full_load: int = 30,
    depot: Point = DEPOT,
) -> Car:
    """A car that has not started, as supply builds it (R3, R4, R6): from the depot at `start_at` (11:40, the fleet's
    start seed, by default), or at a load's time when `load`; its typical end 20:00 under a 23:00 roster."""
    start_at = start_at or at("11:40", day)
    loading = business_config.ETA_LOADING_ALLOWANCE_MINUTES if load else 0
    return Car(
        driver_id=driver_id,
        member_ids=tuple(members),
        available=available,
        working=True,
        started=False,
        start_at=start_at,
        end_at=end_at or at("20:00", day),
        roster_end_at=roster_end_at or at("23:00", day),
        start_is_load=load,
        origin=depot,
        origin_at=after(start_at, loading),
        leg_from_depot=True,
        bottles_on_board=bottles,
        full_load=full_load,
        reloaded_today=False,
    )


def driving_car(
    driver_id: int = 1,
    *,
    origin: Optional[Point],
    origin_at: datetime,
    from_depot: bool = False,
    day: date = TODAY,
    start_at: Optional[datetime] = None,
    end_at: Optional[datetime] = None,
    roster_end_at: Optional[datetime] = None,
    available: bool = True,
    members: Sequence[int] = (),
    bottles: int = 30,
    full_load: int = 30,
    reloaded_today: bool = False,
) -> Car:
    """A car on the road today, as supply builds it (R3, R4, R6): its current leg starts at `origin` (None: a door-less
    stop) at `origin_at`, on the depot line when `from_depot`. It started at its first move, 11:00 by default;
    `reloaded_today` when it has loaded a second time today."""
    return Car(
        driver_id=driver_id,
        member_ids=tuple(members),
        available=available,
        working=True,
        started=True,
        start_at=start_at or at("11:00", day),
        end_at=end_at or at("20:00", day),
        roster_end_at=roster_end_at or at("23:00", day),
        start_is_load=False,
        origin=origin,
        origin_at=origin_at,
        leg_from_depot=from_depot,
        bottles_on_board=bottles,
        full_load=full_load,
        reloaded_today=reloaded_today,
    )


def pool_stop(
    order_id: int,
    door: Optional[Point],
    *,
    rank: int,
    created_at: datetime,
    window: Tuple[Optional[datetime], Optional[datetime]] = (None, None),
    customer_date: Optional[date] = None,
    plan_day: date = TODAY,
    bottles: int = 1,
) -> Stop:
    """A delivery in the drivers' pool (R7): no driver, `rank` in the pool order. Its `not_before` is the customer's
    window start, as demand reads it."""
    return Stop(
        delivery_id=order_id + 5000,
        order_id=order_id,
        status=DeliveryStatus.SCHEDULED,
        driver_id=None,
        point=door,
        plan_day=plan_day,
        customer_date=customer_date,
        created_at=created_at,
        window_start=window[0],
        window_end=window[1],
        not_before=window[0],
        bottles=bottles,
        pool_rank=rank,
        committed=False,
        route_index=None,
    )


def held_stop(
    order_id: int,
    door: Optional[Point],
    *,
    driver_id: int = 1,
    status: DeliveryStatus = DeliveryStatus.ASSIGNED,
    route_index: Optional[int] = None,
    committed: bool = False,
    window: Tuple[Optional[datetime], Optional[datetime]] = (None, None),
    customer_date: Optional[date] = None,
    plan_day: date = TODAY,
    bottles: int = 1,
) -> Stop:
    """A stop driver `driver_id` holds (R7), assigned by default; `route_index` is its place in their stored route,
    `committed` marks the in_transit or arrived stop `get_committed_stop` returns (R8)."""
    return Stop(
        delivery_id=order_id + 5000,
        order_id=order_id,
        status=status,
        driver_id=driver_id,
        point=door,
        plan_day=plan_day,
        customer_date=customer_date,
        created_at=at("09:00", YESTERDAY),
        window_start=window[0],
        window_end=window[1],
        not_before=window[0],
        bottles=bottles,
        pool_rank=None,
        committed=committed,
        route_index=route_index,
    )


def dated_stop(
    order_id: int,
    door: Optional[Point],
    *,
    day: date,
    rank: int,
    release: Optional[datetime] = None,
    bottles: int = 1,
) -> Stop:
    """An order the customer dated for `day`, held for release (R7): no delivery row yet, planned on its date and never
    before its release (07:00 on the day by default, the release sweep's earliest shift start)."""
    return Stop(
        delivery_id=None,
        order_id=order_id,
        status=None,
        driver_id=None,
        point=door,
        plan_day=day,
        customer_date=day,
        created_at=at("09:00", day - timedelta(days=2)),
        window_start=None,
        window_end=None,
        not_before=release or at("07:00", day),
        bottles=bottles,
        pool_rank=rank,
        committed=False,
        route_index=None,
    )


def plan_days(
    app, now: datetime, cars_by_day, stops: Iterable[Stop], *, depot: Point = DEPOT
) -> Dict[int, PlannedStop]:
    """`build_plan` as the engine calls it, keyed by order id. Every stop gets exactly one answer."""
    stops = list(stops)
    planned = build_plan(cars_by_day, stops, SEED, now, depot, km=km, config=app.config)
    by_order = {planned_stop.stop.order_id: planned_stop for planned_stop in planned}
    assert len(planned) == len(by_order) == len(stops)
    return by_order


def plan(app, now: datetime, cars, stops, *, later=None, depot: Point = DEPOT) -> Dict[int, PlannedStop]:
    """The plan with `cars` working on `now`'s plan day, and `later` ({day: cars}) after it."""
    return plan_days(app, now, {work_day_of(now): list(cars), **(later or {})}, stops, depot=depot)


def window_end(planned: PlannedStop, now: datetime) -> datetime:
    """The end of the window the recorder stores for `planned` (R11), through Task 4's own rules."""
    lo, hi, _ = lo_hi(SEED, planned.stops_ahead, planned.day_started)
    stop = planned.stop
    return customer_bounded_window(planned.expected_at, lo, hi, now, stop.window_start, stop.window_end)[1]


def without_a_car(planned: PlannedStop) -> bool:
    return planned.driver_id is None and planned.expected_at is None and planned.stops_ahead is None


class TestPoolOrderAndCapacity:
    def test_one_car_serves_the_pool_in_rank_order(self, app):
        now = at("09:00")
        doors = [point(6.0), point(1.0), point(4.0), point(2.0), point(5.0)]  # the pool order is not the geography
        stops = [pool_stop(rank, door, rank=rank, created_at=at("08:00")) for rank, door in enumerate(doors, start=1)]

        planned = plan(app, now, [waiting_car()], stops)

        times = [planned[rank].expected_at for rank in range(1, 6)]
        assert all(earlier < later for earlier, later in zip(times, times[1:]))
        assert [planned[rank].stops_ahead for rank in range(1, 6)] == [0, 1, 2, 3, 4]
        # The first leg leaves the depot (the depot line), and each later one is the stop curve.
        assert minutes_between(at("11:40"), times[0]) == pytest.approx(depot_leg(DEPOT, doors[0]))
        for k in range(1, 5):
            assert minutes_between(times[k - 1], times[k]) == pytest.approx(cycle(doors[k - 1], doors[k]))

    def test_one_car_vs_two_shortens_at_least_two_of_the_same_five_pool_stops(self, app):
        """Requirement 2 (spec §7): a second working car only ever shortens the windows the same pool is promised."""
        now = at("09:00")
        stops = [pool_stop(rank, point(float(rank)), rank=rank, created_at=at("08:00")) for rank in range(1, 6)]

        one = plan(app, now, [waiting_car(1)], stops)
        two = plan(app, now, [waiting_car(1), waiting_car(2)], stops)

        ends_with_one = [window_end(one[rank], now) for rank in range(1, 6)]
        ends_with_two = [window_end(two[rank], now) for rank in range(1, 6)]
        assert all(second <= first for first, second in zip(ends_with_one, ends_with_two))
        assert sum(second < first for first, second in zip(ends_with_one, ends_with_two)) >= 2
        assert {two[rank].driver_id for rank in range(1, 6)} == {1, 2}

    def test_a_newer_pool_order_never_moves_an_older_promise_it_does_not_outrank(self, app):
        """R9 as the plan restates it: a newer order never moves an older order's E unless it outranks it in the
        drivers' pool order. This one has no window and comes last in that order, so the first five E stay, though it
        takes the wait in front of the windowed stop."""
        now = at("09:00")
        cars = [waiting_car(1), waiting_car(2, start_at=at("14:00"))]
        older = [
            # The customer's window opens at 15:00, so the car waits in front of this stop.
            pool_stop(1, point(3.0), rank=1, created_at=at("07:00"), window=(at("15:00"), at("18:00"))),
            pool_stop(2, point(1.0), rank=2, created_at=at("07:10")),
            pool_stop(3, point(5.0), rank=3, created_at=at("07:20")),
            pool_stop(4, point(2.0), rank=4, created_at=at("07:30")),
            pool_stop(5, point(6.0), rank=5, created_at=at("07:40")),
        ]
        newer = pool_stop(6, point(2.5), rank=6, created_at=at("08:50"))

        before = plan(app, now, cars, older)
        after_it = plan(app, now, cars, [*older, newer])

        assert [after_it[stop.order_id].expected_at for stop in older] == [
            before[stop.order_id].expected_at for stop in older
        ]
        # It went into the wait in front of the windowed stop, not to the end: the gap a newer stop may use.
        assert after_it[6].driver_id == after_it[1].driver_id
        assert after_it[6].stops_ahead < after_it[1].stops_ahead

    def test_a_newer_deadline_order_outranks_older_open_orders(self, app):
        """The other side of that guarantee: the drivers' pool sorts a customer deadline first (spec §4.4). So an "until
        13:00" order placed at 08:50 is served ahead of the open orders of 07:00 and 07:10, and their E move later, as
        they would on the road."""
        now = at("09:00")
        car = waiting_car()
        deadline = pool_stop(3, point(4.0), rank=1, created_at=at("08:50"), window=(None, at("13:00")))

        def open_orders(first_rank: int):
            return [
                pool_stop(1, point(2.0), rank=first_rank, created_at=at("07:00")),
                pool_stop(2, point(3.0), rank=first_rank + 1, created_at=at("07:10")),
            ]

        before = plan(app, now, [car], open_orders(1))
        after_it = plan(app, now, [car], [deadline, *open_orders(2)])

        assert after_it[3].stops_ahead == 0
        assert after_it[3].expected_at <= at("13:00")
        assert all(after_it[order_id].expected_at > before[order_id].expected_at for order_id in (1, 2))

    def test_a_newer_pool_order_never_makes_a_driver_held_customer_later(self, app):
        now = at("12:00")
        driving = driving_car(origin=point(0.5), origin_at=at("11:55"))
        held = [held_stop(1, point(1.5), route_index=0), held_stop(2, point(7.0), route_index=1)]
        newer = pool_stop(3, point(4.0), rank=1, created_at=at("11:50"))  # right on the way between the two

        before = plan(app, now, [driving], held)
        after_it = plan(app, now, [driving], [*held, newer])

        assert [after_it[stop.order_id].expected_at for stop in held] == [
            before[stop.order_id].expected_at for stop in held
        ]
        assert after_it[3].stops_ahead == 2
        assert minutes_between(after_it[2].expected_at, after_it[3].expected_at) == pytest.approx(
            cycle(point(7.0), point(4.0))
        )

    def test_a_late_window_never_idles_the_car(self, app):
        now = at("08:00")
        car = waiting_car(start_at=at("09:00"))
        windowed = pool_stop(1, point(2.0), rank=1, created_at=at("07:00"), window=(at("14:00"), at("18:00")))
        five = [
            pool_stop(rank, point(distance), rank=rank, created_at=at("07:30"))
            for rank, distance in zip(range(2, 7), (1.0, 3.0, 0.5, 2.5, 1.5))
        ]

        with_window = plan(app, now, [car], [windowed, *five])
        without = plan(app, now, [car], five)

        assert [with_window[stop.order_id].expected_at for stop in five] == [
            without[stop.order_id].expected_at for stop in five
        ]
        assert with_window[1].expected_at == at("14:00")
        assert with_window[1].stops_ahead == 5

    def test_a_wider_window_on_the_second_car_keeps_the_stop_on_the_first(self, app):
        now = at("12:00")
        door = point(5.0)
        far_side = point(15.0)  # 10 km past the door, where car A has just delivered
        car_a = driving_car(1, origin=far_side, origin_at=now)
        expected_on_a = after(now, cycle(far_side, door))
        # Car B has not started: it reaches the door 10 minutes sooner, from the depot.
        car_b = waiting_car(2, start_at=after(expected_on_a, -10 - depot_leg(DEPOT, door)))
        # ... but a car that has not started reads a window 60 minutes wider (its seed hi gets the extra hour).
        assert lo_hi(SEED, 0, False)[1] - lo_hi(SEED, 0, True)[1] == 60

        planned = plan(app, now, [car_a, car_b], [pool_stop(1, door, rank=1, created_at=at("11:30"))])

        assert planned[1].driver_id == 1
        assert minutes_between(now, planned[1].expected_at) == pytest.approx(cycle(far_side, door))

    def test_a_car_that_meets_the_customers_window_beats_one_that_misses_it(self, app):
        now = at("15:58")
        door = point(2.0)
        stop = pool_stop(1, door, rank=1, created_at=at("13:00"), window=(at("14:00"), at("17:00")))
        car_a = waiting_car(1, start_at=after(at("16:30"), -depot_leg(DEPOT, door)))  # E 16:30, not started
        far_side = point(15.2)  # 13.2 km past the door, where car B has just delivered
        car_b = driving_car(2, origin=far_side, origin_at=now)
        assert minutes_between(now, at("17:05")) == pytest.approx(cycle(far_side, door))  # B's E: 17:05
        # B's E + hi is the earlier: started, its window is the narrower one.
        assert after(at("17:05"), lo_hi(SEED, 0, True)[1]) < after(at("16:30"), lo_hi(SEED, 0, False)[1])

        planned = plan(app, now, [car_a, car_b], [stop])

        assert planned[1].driver_id == 1
        assert minutes_between(at("16:30"), planned[1].expected_at) == pytest.approx(0.0, abs=1e-6)

    def test_an_unavailable_car_takes_no_pool_stops(self, app):
        now = at("12:00")
        closed = driving_car(1, origin=point(1.0), origin_at=at("11:50"), available=False)
        open_car = waiting_car(2, start_at=at("13:00"))
        held = held_stop(1, point(1.5), driver_id=1)
        pooled = pool_stop(2, point(1.2), rank=1, created_at=at("11:00"))  # right beside the closed car

        planned = plan(app, now, [closed, open_car], [held, pooled])

        assert planned[2].driver_id == 2
        # The closed car still finishes the stop it holds.
        assert (planned[1].driver_id, planned[1].plan_day) == (1, TODAY)


class TestDepotLegsOriginsAndReloads:
    def test_a_not_started_car_counts_its_first_stop_from_the_depot_line(self, app):
        now = at("10:00")
        first, second = point(5.0), point(6.5)
        stops = [held_stop(1, first, route_index=0), held_stop(2, second, route_index=1)]

        planned = plan(app, now, [waiting_car()], stops)

        assert minutes_between(at("11:40"), planned[1].expected_at) == pytest.approx(depot_leg(DEPOT, first))
        assert depot_leg(DEPOT, first) < cycle(DEPOT, first)  # the line, not the curve
        assert minutes_between(planned[1].expected_at, planned[2].expected_at) == pytest.approx(cycle(first, second))
        assert not planned[1].day_started

    @pytest.mark.parametrize("since_load", [0, 5])
    def test_a_car_that_just_loaded_at_a_far_depot_uses_the_depot_line(self, app, since_load):
        load = at("11:00")
        door = point(28.9, origin=FAR_DEPOT, bearing=CITYWARD)
        loaded = waiting_car(start_at=load, load=True, depot=FAR_DEPOT)

        planned = plan(app, after(load, since_load), [loaded], [held_stop(1, door)], depot=FAR_DEPOT)

        allowance = app.config["ETA_LOADING_ALLOWANCE_MINUTES"]
        assert minutes_between(load, planned[1].expected_at) == pytest.approx(allowance + depot_leg(FAR_DEPOT, door))
        assert minutes_between(load, planned[1].expected_at) == pytest.approx(63.35, abs=0.01)
        assert cycle(FAR_DEPOT, door) == pytest.approx(132.65, abs=0.01)  # what the stop curve would have said

    def test_a_car_that_loaded_hours_ago_never_leaves_before_now(self, app):
        now = at("11:00")
        door = point(4.0)
        loaded = waiting_car(start_at=at("08:00"), load=True)  # no pickup since

        planned = plan(app, now, [loaded], [held_stop(1, door)])

        assert minutes_between(now, planned[1].expected_at) == pytest.approx(depot_leg(DEPOT, door))

    @pytest.mark.parametrize("now", ["11:05", "11:25"])
    def test_a_load_long_before_the_first_pickup_does_not_jump_the_promise(self, app, now):
        """Loaded at 09:00, first pickup at 11:00: supply starts the depot leg at the pickup (R4), and E stays put."""
        door = point(28.9, origin=FAR_DEPOT, bearing=CITYWARD)
        picked_up = driving_car(start_at=at("11:00"), origin=FAR_DEPOT, origin_at=at("11:00"), from_depot=True)

        planned = plan(
            app, at(now), [picked_up], [held_stop(1, door, status=DeliveryStatus.PICKED_UP)], depot=FAR_DEPOT
        )

        assert minutes_between(at("11:00"), planned[1].expected_at) == pytest.approx(depot_leg(FAR_DEPOT, door))

    @pytest.mark.parametrize("run_at, minutes_after_the_drop", [("14:00", 17.07), ("14:10", 17.07), ("14:25", 25.0)])
    def test_a_started_car_does_not_slide_between_taps(self, app, run_at, minutes_after_the_drop):
        last_stop, door = point(3.0), point(4.0)  # 1.0 km apart: 17.07 minutes on the curve
        assert cycle(last_stop, door) == pytest.approx(17.07, abs=0.01)
        driving = driving_car(origin=last_stop, origin_at=at("14:00"))

        planned = plan(app, at(run_at), [driving], [held_stop(1, door)])

        assert minutes_between(at("14:00"), planned[1].expected_at) == pytest.approx(minutes_after_the_drop, abs=0.01)

    @pytest.mark.parametrize("ping_km", [0.005, 2.0])
    @pytest.mark.parametrize("left_at", ["11:00", "15:00"], ids=["after_a_pickup", "after_a_reload"])
    def test_a_gps_ping_on_a_depot_leg_keeps_the_depot_line(self, app, ping_km, left_at):
        door = point(28.9, origin=FAR_DEPOT, bearing=CITYWARD)
        ping = point(ping_km, origin=FAR_DEPOT, bearing=CITYWARD)
        ping_at = after(at(left_at), 20)
        driving = driving_car(origin=ping, origin_at=ping_at, from_depot=True)

        planned = plan(app, after(ping_at, 1), [driving], [held_stop(1, door)], depot=FAR_DEPOT)

        assert minutes_between(ping_at, planned[1].expected_at) == pytest.approx(depot_leg(ping, door))
        assert depot_leg(ping, door) < 60 < 120 < cycle(ping, door)

    def test_a_loaded_car_that_drove_off_counts_its_first_stop_from_where_it_is_on_the_curve(self, app):
        """Owner rule 2026-11-09: loaded at the far depot at 10:00, the driver shared a location from a petrol station
        in the city at 10:40, 5 km short of the door, before any tap. Supply makes that the car's origin; the plan sets
        off from there now, and a leg across the city is timed on the stop curve, not on the depot line."""
        door = point(28.9, origin=FAR_DEPOT, bearing=CITYWARD)
        petrol_station = point(23.9, origin=FAR_DEPOT, bearing=CITYWARD)
        loaded = dataclasses.replace(
            waiting_car(start_at=at("10:00"), load=True, depot=FAR_DEPOT), origin=petrol_station, origin_at=at("10:40")
        )

        planned = plan(app, at("10:45"), [loaded], [held_stop(1, door)], depot=FAR_DEPOT)

        assert minutes_between(at("10:45"), planned[1].expected_at) == pytest.approx(33.55, abs=0.01)
        assert cycle(petrol_station, door) == pytest.approx(33.55, abs=0.01)
        assert depot_leg(petrol_station, door) == pytest.approx(17.5, abs=0.01)  # what the depot line would have said
        assert not planned[1].day_started

    @pytest.mark.parametrize(
        "fix_to_door_km, on_the_curve", [(5.0, True), (13.1, True), (13.4, False)], ids=["5km", "13.1km", "13.4km"]
    )
    def test_a_depot_leg_from_a_fix_is_on_the_curve_within_its_measured_range(self, app, fix_to_door_km, on_the_curve):
        """On its way from the far depot, the driver's position is a fix in the city (a shared location, or a door).
        Within the stop curve's last measured point (13.2 km on the seed) of the door, the leg is a city leg and the
        curve times it; farther out it stays on the depot line, as a fix beside the depot does (R4)."""
        door = point(28.9, origin=FAR_DEPOT, bearing=CITYWARD)
        fix = point(28.9 - fix_to_door_km, origin=FAR_DEPOT, bearing=CITYWARD)
        driving = driving_car(origin=fix, origin_at=at("11:20"), from_depot=True)

        planned = plan(
            app, at("11:21"), [driving], [held_stop(1, door, status=DeliveryStatus.IN_TRANSIT)], depot=FAR_DEPOT
        )

        expected = cycle(fix, door) if on_the_curve else depot_leg(fix, door)
        assert minutes_between(at("11:20"), planned[1].expected_at) == pytest.approx(expected)

    def test_a_started_car_counts_from_its_fresh_position(self, app):
        fix, door = point(2.0), point(5.0)  # where a fresh GPS fix put the car at 14:20
        driving = driving_car(origin=fix, origin_at=at("14:20"))

        planned = plan(app, at("14:25"), [driving], [held_stop(1, door)])

        assert minutes_between(at("14:20"), planned[1].expected_at) == pytest.approx(cycle(fix, door))
        assert planned[1].day_started

    def test_a_started_car_counts_from_its_last_stop_when_gps_is_stale(self, app):
        last_stop, door = point(1.0), point(5.0)  # delivered at 14:00, and no fresh fix since
        driving = driving_car(origin=last_stop, origin_at=at("14:00"))

        planned = plan(app, at("14:05"), [driving], [held_stop(1, door)])

        assert minutes_between(at("14:00"), planned[1].expected_at) == pytest.approx(cycle(last_stop, door))

    def test_a_stop_added_to_an_idle_car_sets_off_no_earlier_than_now(self, app):
        """R4 as ruled: the car delivered its last stop three hours ago and holds nothing. A pool stop 20 km away is not
        reached "now" (max(now, origin_at + leg) is for a driver-held leg already under way): the car sets off now."""
        now = at("15:00")
        last_drop, far_away = point(1.0), point(21.0)
        idle = driving_car(origin=last_drop, origin_at=at("12:00"))
        assert after(at("12:00"), cycle(last_drop, far_away)) < now  # the leg would have ended long ago

        planned = plan(app, now, [idle], [pool_stop(1, far_away, rank=1, created_at=at("14:30"))])

        assert minutes_between(now, planned[1].expected_at) == pytest.approx(cycle(last_drop, far_away))

    def test_a_car_from_an_unknown_origin_prices_its_first_leg_at_the_unknown_km(self, app):
        """The car's last drop had no coordinates (Task 2's ruling): its first leg is R7's unknown leg, on the curve."""
        driving = driving_car(origin=None, origin_at=at("14:00"))

        planned = plan(app, at("14:05"), [driving], [held_stop(1, point(9.0))])

        assert minutes_between(at("14:00"), planned[1].expected_at) == pytest.approx(cycle(None, point(9.0), app))

    def test_a_stop_without_coordinates_is_the_unknown_km_from_its_neighbours(self, app):
        driving = driving_car(origin=point(1.0), origin_at=at("12:00"))
        stops = [
            held_stop(1, point(2.0), route_index=0),
            held_stop(2, None, route_index=1),
            held_stop(3, point(9.0), route_index=2),
        ]

        planned = plan(app, at("12:00"), [driving], stops)

        assert minutes_between(planned[1].expected_at, planned[2].expected_at) == pytest.approx(cycle(None, None, app))
        assert minutes_between(planned[2].expected_at, planned[3].expected_at) == pytest.approx(cycle(None, None, app))
        # The driver's stored route keeps the door-less stop where they put it.
        assert [planned[order_id].stops_ahead for order_id in (1, 2, 3)] == [0, 1, 2]

    @pytest.mark.parametrize("kind", ["held", "left_over", "new_pool"])
    def test_a_stop_without_coordinates_comes_after_every_stop_with_a_door(self, app, kind):
        """R7 as ruled: a door-less stop is ETA_UNKNOWN_LEG_KM from everything, so nearest-next (held stops), cheapest
        insertion (stops left from yesterday) and R9's placing of today's new pool stops (the door-less one ranked
        first in both) put it after every stop with a door. Taken first, it would look 2.3 km from the far depot and
        pull the real door's promise early. The real door, 28 km out, keeps the E it has alone."""
        now = at("10:00")
        car = waiting_car(depot=FAR_DEPOT)
        door = point(28.0, origin=FAR_DEPOT, bearing=CITYWARD)
        if kind == "held":
            real, door_less = held_stop(1, door), held_stop(2, None)
        else:
            placed_at = at("16:00", YESTERDAY) if kind == "left_over" else at("09:00")
            real = pool_stop(1, door, rank=2, created_at=placed_at)
            door_less = pool_stop(2, None, rank=1, created_at=placed_at)

        alone = plan(app, now, [car], [real], depot=FAR_DEPOT)
        both = plan(app, now, [car], [real, door_less], depot=FAR_DEPOT)

        assert (both[1].stops_ahead, both[1].expected_at) == (0, alone[1].expected_at)
        assert both[2].stops_ahead == 1
        assert minutes_between(both[1].expected_at, both[2].expected_at) == pytest.approx(cycle(None, None, app))

    def test_after_a_reload_the_next_leg_uses_the_depot_line(self, app):
        now = at("14:00")
        origin = point(27.0, origin=FAR_DEPOT, bearing=CITYWARD)
        first = point(28.0, origin=FAR_DEPOT, bearing=CITYWARD)
        second = point(29.5, origin=FAR_DEPOT, bearing=CITYWARD)
        driving = driving_car(origin=origin, origin_at=at("13:55"), bottles=2, full_load=20)
        stops = [held_stop(1, first, route_index=0, bottles=2), held_stop(2, second, route_index=1, bottles=3)]

        planned = plan(app, now, [driving], stops, depot=FAR_DEPOT)

        assert (planned[1].reload_before, planned[2].reload_before) == (False, True)
        allowance = app.config["ETA_LOADING_ALLOWANCE_MINUTES"]
        assert minutes_between(planned[1].expected_at, planned[2].expected_at) == pytest.approx(
            depot_leg(first, FAR_DEPOT) + allowance + depot_leg(FAR_DEPOT, second)
        )

    def test_a_reload_is_inserted_once_and_its_next_leg_uses_the_depot_line(self, app):
        now = at("12:00")
        doors = [point(1.0), point(1.5), point(2.0), point(2.5)]
        driving = driving_car(origin=point(0.5), origin_at=at("11:55"), bottles=4, full_load=10)
        stops = [
            held_stop(order_id, door, route_index=order_id - 1, bottles=bottles)
            for order_id, door, bottles in zip(range(1, 5), doors, (3, 3, 8, 8))
        ]

        planned = plan(app, now, [driving], stops)

        # 4 on board: the second stop needs 3 of the 1 left, so the car reloads to 10 before it; the third then needs 8
        # of 7, but a car reloads once a day.
        assert [planned[order_id].reload_before for order_id in range(1, 5)] == [False, True, False, False]
        allowance = app.config["ETA_LOADING_ALLOWANCE_MINUTES"]
        assert minutes_between(planned[1].expected_at, planned[2].expected_at) == pytest.approx(
            depot_leg(doors[0], DEPOT) + allowance + depot_leg(DEPOT, doors[1])
        )
        assert minutes_between(planned[2].expected_at, planned[3].expected_at) == pytest.approx(
            cycle(doors[1], doors[2])
        )

    def test_after_a_real_reload_no_second_depot_trip_is_planned(self, app):
        """R6 as ruled: a car that has loaded twice today has had the day's one reload, and counts as full. Short of
        bottles, its next customer gets no depot trip; the same car before a reload would get one."""
        now = at("16:00")
        origin, door = point(1.0), point(2.0)
        # Three bottles left on board, and the next customer needs five.
        reloaded = driving_car(origin=origin, origin_at=at("15:55"), bottles=3, full_load=20, reloaded_today=True)
        short = held_stop(1, door, bottles=5)

        planned = plan(app, now, [reloaded], [short])
        not_yet = plan(app, now, [dataclasses.replace(reloaded, reloaded_today=False)], [short])

        assert planned[1].reload_before is False
        assert minutes_between(at("15:55"), planned[1].expected_at) == pytest.approx(cycle(origin, door))
        assert not_yet[1].reload_before is True

    def test_in_flight_stops_never_trigger_a_reload(self, app):
        now = at("12:00")
        # Supply has already taken the in-flight stop's bottles off what is on board (R6).
        driving = driving_car(origin=point(0.5), origin_at=at("11:55"), bottles=3, full_load=20)
        stops = [
            held_stop(1, point(1.0), status=DeliveryStatus.PICKED_UP, route_index=0, bottles=5),
            held_stop(2, point(2.0), route_index=1, bottles=3),
        ]

        planned = plan(app, now, [driving], stops)

        # Neither its 5 bottles over the 3 on board, nor those 3 left for the next stop, send the car back.
        assert (planned[1].reload_before, planned[2].reload_before) == (False, False)
        assert minutes_between(planned[1].expected_at, planned[2].expected_at) == pytest.approx(
            cycle(point(1.0), point(2.0))
        )

    def test_a_later_day_car_starts_full_and_may_reload_once(self, app):
        now = at("21:00")
        empty_tonight = driving_car(origin=point(1.0), origin_at=at("20:30"), bottles=0, full_load=10)
        # Supply already sends a later day's car with a full load; the planner starts it full whatever the count says.
        tomorrows = waiting_car(day=TOMORROW, bottles=0, full_load=10)
        dated = [dated_stop(rank, point(float(rank)), day=TOMORROW, rank=rank, bottles=6) for rank in (1, 2, 3)]

        planned = plan(app, now, [empty_tonight], dated, later={TOMORROW: [tomorrows]})

        by_place = sorted(planned.values(), key=lambda planned_stop: planned_stop.stops_ahead)
        assert all((p.plan_day, p.driver_id, p.day_started) == (TOMORROW, 1, False) for p in by_place)
        # Full at the start (10 bottles, whatever was left tonight): the first stop needs no reload; the second does;
        # the third would need another, and gets none.
        assert [p.reload_before for p in by_place] == [False, True, False]


class TestSequencingAndWindows:
    def test_the_committed_stop_goes_first_then_the_stored_route_then_nearest_next(self, app):
        now = at("13:00")
        driving = driving_car(origin=point(0.2), origin_at=at("12:55"))
        committed = held_stop(1, point(6.0), status=DeliveryStatus.IN_TRANSIT, committed=True, route_index=2)
        first_in_route = held_stop(2, point(3.0), route_index=0)
        second_in_route = held_stop(3, point(1.0), route_index=1)
        far_unrouted = held_stop(4, point(5.0))
        near_unrouted = held_stop(5, point(1.6))
        stops = [near_unrouted, second_in_route, far_unrouted, committed, first_in_route]

        planned = plan(app, now, [driving], stops)

        assert [planned[order_id].stops_ahead for order_id in (1, 2, 3, 5, 4)] == [0, 1, 2, 3, 4]

    def test_nothing_is_put_in_front_of_the_committed_stop(self, app):
        """The driver is driving to (or is at) the committed stop: a stop from the pool, however close to the car, is
        planned after it."""
        now = at("13:00")
        driving = driving_car(origin=point(0.2), origin_at=at("12:55"))
        committed = held_stop(1, point(6.0), status=DeliveryStatus.IN_TRANSIT, committed=True)
        beside_the_car = pool_stop(2, point(0.3), rank=1, created_at=at("12:00", YESTERDAY))

        planned = plan(app, now, [driving], [committed, beside_the_car])

        assert (planned[1].stops_ahead, planned[2].stops_ahead) == (0, 1)
        assert planned[2].driver_id == 1

    def test_a_windowed_stop_is_never_before_its_window_start(self, app):
        now = at("12:00")
        driving = driving_car(origin=point(1.0), origin_at=at("11:55"))
        held = held_stop(1, point(2.0), window=(at("15:00"), at("18:00")))
        after_four = pool_stop(2, point(2.5), rank=1, created_at=at("11:00"), window=(at("16:00"), None))

        planned = plan(app, now, [driving], [held, after_four])

        assert after(at("11:55"), cycle(point(1.0), point(2.0))) < at("15:00")  # the car would be there long before
        assert planned[1].expected_at == at("15:00")
        assert planned[2].expected_at == at("16:00")

    def test_a_dated_order_due_later_today_is_never_before_its_release(self, app):
        now = at("06:00")
        early = waiting_car(start_at=at("06:00"))
        dated = dated_stop(1, point(2.0), day=TODAY, rank=1, release=at("07:00"))

        planned = plan(app, now, [early], [dated])

        assert after(at("06:00"), depot_leg(DEPOT, point(2.0))) < at("07:00")
        assert planned[1].expected_at == at("07:00")


class TestDays:
    def test_a_stop_past_the_expected_end_rolls_to_the_next_day(self, app):
        now = at("19:30")
        late = driving_car(origin=point(1.0), origin_at=at("19:25"))  # its end is 20:00
        far = held_stop(1, point(14.0), route_index=0)
        # Next in the driver's route: on its own it would fit before 20:00, but it comes after a stop that does not, so
        # it rolls with it.
        near = held_stop(2, point(1.5), route_index=1)
        assert after(at("19:25"), cycle(point(1.0), point(14.0))) > at("20:00")
        assert after(at("19:25"), cycle(point(1.0), point(1.5))) < at("20:00")

        planned = plan(app, now, [late], [far, near], later={TOMORROW: [waiting_car(day=TOMORROW)]})

        assert all((p.plan_day, p.driver_id, p.day_started) == (TOMORROW, 1, False) for p in planned.values())
        # Tomorrow it is all from the depot, nearest-next: the near stop first.
        assert minutes_between(at("11:40", TOMORROW), planned[2].expected_at) == pytest.approx(
            depot_leg(DEPOT, point(1.5))
        )
        assert minutes_between(planned[2].expected_at, planned[1].expected_at) == pytest.approx(
            cycle(point(1.5), point(14.0))
        )

    def test_in_flight_stops_never_roll_and_extend_the_end_up_to_the_roster(self, app):
        now = at("22:50")
        a, b, c = point(1.0), point(2.55), point(6.575)
        assert (cycle(a, b), cycle(b, c)) == (pytest.approx(20.0), pytest.approx(30.0))
        # Its typical end is long past (20:00) and its roster ends at 23:00. The stops would be reached at 22:50 (now),
        # 23:10 and 23:40, the last one in flight.
        driving = driving_car(origin=point(0.0), origin_at=at("22:30"), end_at=at("20:00"), roster_end_at=at("23:00"))
        stops = [
            held_stop(1, a, route_index=0),
            held_stop(2, b, route_index=1),
            held_stop(3, c, status=DeliveryStatus.PICKED_UP, route_index=2),
        ]

        planned = plan(app, now, [driving], stops, later={TOMORROW: [waiting_car(day=TOMORROW)]})

        # The in-flight 23:40 stretches the end, but only to the roster's 23:00: the 22:50 stop stays, the 23:10 one
        # rolls, and the in-flight one stays although it too is past 23:00.
        assert (planned[1].plan_day, planned[1].expected_at) == (TODAY, now)
        assert planned[2].plan_day == TOMORROW
        assert (planned[3].plan_day, planned[3].driver_id) == (TODAY, 1)
        assert planned[3].expected_at > at("23:00")

    def test_after_a_roll_the_remaining_stops_are_re_timed(self, app):
        now = at("19:00")
        origin = point(0.5)
        driving = driving_car(origin=origin, origin_at=at("18:55"), end_at=at("20:00"), roster_end_at=at("20:00"))
        far = held_stop(1, point(16.0), route_index=0)  # reached after 20:00
        aboard = held_stop(2, point(1.0), status=DeliveryStatus.PICKED_UP, route_index=1)

        planned = plan(app, now, [driving], [far, aboard], later={TOMORROW: [waiting_car(day=TOMORROW)]})

        assert planned[1].plan_day == TOMORROW
        # The in-flight stop no longer waits behind the far one: it is timed straight from the origin.
        assert (planned[2].plan_day, planned[2].stops_ahead) == (TODAY, 0)
        assert minutes_between(at("18:55"), planned[2].expected_at) == pytest.approx(cycle(origin, point(1.0)))

    def test_an_insertion_that_would_push_a_later_stop_past_the_end_is_not_feasible(self, app):
        now = at("19:00")
        car_a = driving_car(1, origin=point(0.0), origin_at=at("19:00"), end_at=at("19:45"))
        held = held_stop(1, point(6.0), driver_id=1, route_index=0)  # 19:37, inside car A's end
        left_over = pool_stop(2, point(3.0), rank=1, created_at=at("12:00", YESTERDAY))  # on the way to it
        south = point(10.0, bearing=180.0)
        car_b = driving_car(2, origin=south, origin_at=at("19:00"), end_at=at("22:00"))
        # On the way to the held stop it would cost car A far fewer minutes than car B's whole leg...
        added_on_a = cycle(point(0.0), point(3.0)) + cycle(point(3.0), point(6.0)) - cycle(point(0.0), point(6.0))
        assert added_on_a < cycle(south, point(3.0))
        # ... but it would bring the held stop past 19:45.
        assert after(at("19:00"), cycle(point(0.0), point(3.0)) + cycle(point(3.0), point(6.0))) > at("19:45")

        planned = plan(app, now, [car_a, car_b], [held, left_over])

        assert planned[2].driver_id == 2
        assert minutes_between(at("19:00"), planned[1].expected_at) == pytest.approx(cycle(point(0.0), point(6.0)))

    def test_an_insertion_that_would_push_an_in_flight_stop_past_the_end_is_not_feasible(self, app):
        """The review's case. The car ends at 19:55; its picked-up customer, 3 km out, is due at 19:56:26, which
        stretches the end to then. A stop from yesterday 1 km out, on the way, would be reached at 19:47 and make that
        customer wait until 20:09:28, past the end: it rolls to tomorrow instead."""
        now = at("19:30")
        driving = driving_car(origin=point(0.0), origin_at=now, end_at=at("19:55"))
        picked_up = held_stop(1, point(3.0), status=DeliveryStatus.PICKED_UP)
        left_over = pool_stop(2, point(1.0), rank=1, created_at=at("16:00", YESTERDAY))
        assert minutes_between(now, at("19:56")) < cycle(point(0.0), point(3.0)) < minutes_between(now, at("19:57"))

        planned = plan(app, now, [driving], [picked_up, left_over], later={TOMORROW: [waiting_car(day=TOMORROW)]})

        assert planned[2].plan_day == TOMORROW
        assert (planned[1].plan_day, planned[1].stops_ahead) == (TODAY, 0)
        assert minutes_between(now, planned[1].expected_at) == pytest.approx(cycle(point(0.0), point(3.0)))

    def test_an_insertion_may_delay_an_in_flight_stop_within_the_end(self, app):
        """Picked-up customers are not pinned (spec §4.7 sequences a day's stops by geography): with the car working to
        20:30, the stop on the way goes first at 19:47, and the picked-up customer is then due at 20:09:28."""
        now = at("19:30")
        driving = driving_car(origin=point(0.0), origin_at=now, end_at=at("20:30"))
        picked_up = held_stop(1, point(3.0), status=DeliveryStatus.PICKED_UP)
        left_over = pool_stop(2, point(1.0), rank=1, created_at=at("16:00", YESTERDAY))

        planned = plan(app, now, [driving], [picked_up, left_over], later={TOMORROW: [waiting_car(day=TOMORROW)]})

        assert [(planned[order_id].plan_day, planned[order_id].stops_ahead) for order_id in (2, 1)] == [
            (TODAY, 0),
            (TODAY, 1),
        ]
        assert minutes_between(now, planned[2].expected_at) == pytest.approx(cycle(point(0.0), point(1.0)))
        assert minutes_between(planned[2].expected_at, planned[1].expected_at) == pytest.approx(
            cycle(point(1.0), point(3.0))
        )

    def test_todays_carry_over_is_placed_before_the_stops_dated_today(self, app):
        """R9's first group in R10's order: the stops left from before today's plan day first, then those dated today,
        each by pool rank. With room for one more stop today, the leftover gets it, though the dated stop ranks first
        in the pool (a customer window end sorts it ahead)."""
        now = at("12:00")
        nearly_done = driving_car(origin=point(0.0), origin_at=at("12:00"), end_at=at("12:25"))
        dated_today = pool_stop(
            1, point(1.2), rank=1, created_at=at("09:00", YESTERDAY), customer_date=TODAY, window=(None, at("18:00"))
        )
        left_over = pool_stop(2, point(1.0), rank=2, created_at=at("16:00", YESTERDAY))

        planned = plan(app, now, [nearly_done], [dated_today, left_over], later={TOMORROW: [waiting_car(day=TOMORROW)]})

        assert (planned[2].plan_day, planned[2].stops_ahead) == (TODAY, 0)
        assert planned[1].plan_day == TOMORROW

    def test_a_carried_over_stop_is_sequenced_by_geography_not_first(self, app):
        """Spec §7 "Leftovers": tomorrow, an evening's far leftover goes after the near dated stops, not first."""
        now = at("21:00")
        done_for_today = driving_car(origin=point(1.0), origin_at=at("20:30"))  # its end, 20:00, has passed
        far = pool_stop(1, point(12.0), rank=1, created_at=at("18:00"))
        near = [dated_stop(2, point(1.0), day=TOMORROW, rank=2), dated_stop(3, point(1.5), day=TOMORROW, rank=3)]

        planned = plan(app, now, [done_for_today], [far, *near], later={TOMORROW: [waiting_car(day=TOMORROW)]})

        assert all(planned[order_id].plan_day == TOMORROW for order_id in (1, 2, 3))
        assert [planned[order_id].stops_ahead for order_id in (2, 3, 1)] == [0, 1, 2]

    def test_a_carried_over_driver_held_stop_stays_with_its_driver(self, app):
        now = at("21:00")
        tonight = driving_car(1, origin=point(1.0), origin_at=at("20:30"))  # its end, 20:00, has passed
        held = held_stop(1, point(3.0), driver_id=1)
        # Tomorrow driver 2 starts hours earlier, and would reach the stop first.
        tomorrow = [
            waiting_car(1, day=TOMORROW, start_at=at("13:00", TOMORROW)),
            waiting_car(2, day=TOMORROW, start_at=at("09:00", TOMORROW)),
        ]

        planned = plan(app, now, [tonight], [held], later={TOMORROW: tomorrow})

        assert (planned[1].plan_day, planned[1].driver_id) == (TOMORROW, 1)
        assert minutes_between(at("13:00", TOMORROW), planned[1].expected_at) == pytest.approx(
            depot_leg(DEPOT, point(3.0))
        )

    def test_the_plan_is_the_same_either_side_of_five(self, app):
        before, at_five = at("04:59"), at("05:00")
        assert (work_day_of(before), work_day_of(at_five)) == (YESTERDAY, TODAY)
        # The same cars both sides, with fixed typical times (the planner is pure): yesterday's car, its day long over,
        # and the driver's car from today on.
        yesterdays = driving_car(origin=point(2.0), origin_at=at("20:10", YESTERDAY), day=YESTERDAY)
        from_today = {day: [waiting_car(day=day)] for day in (TODAY, TOMORROW, days(2), days(3))}

        def stops_as_demand_reads_them(now: datetime):
            plan_day = work_day_of(now)
            two_days_ago = YESTERDAY - timedelta(days=1)
            return [
                held_stop(1, point(4.0), plan_day=plan_day),
                held_stop(2, point(1.0), plan_day=plan_day),
                pool_stop(3, point(2.5), rank=1, created_at=at("12:00", two_days_ago), plan_day=plan_day),
                pool_stop(4, point(6.0), rank=2, created_at=at("22:00", YESTERDAY), plan_day=plan_day),
                pool_stop(5, point(0.5), rank=3, created_at=at("03:00"), plan_day=plan_day),
                dated_stop(6, point(3.5), day=TODAY, rank=4),
            ]

        just_before = plan_days(
            app, before, {YESTERDAY: [yesterdays], **from_today}, stops_as_demand_reads_them(before)
        )
        from_five = plan_days(app, at_five, from_today, stops_as_demand_reads_them(at_five))

        def sequence(planned: Dict[int, PlannedStop]):
            return {
                order_id: (p.driver_id, p.plan_day, p.expected_at, p.stops_ahead) for order_id, p in planned.items()
            }

        assert sequence(just_before) == sequence(from_five)
        assert {p.plan_day for p in from_five.values()} == {TODAY}

    def test_no_car_within_three_days_leaves_an_undated_stop_unconfirmed(self, app):
        now = at("12:00")
        horizon = app.config["ETA_UNCONFIRMED_HORIZON_DAYS"]
        assert horizon == 3
        beyond = days(horizon + 1)  # a car the day after the horizon is no car within it
        undated = pool_stop(1, point(2.0), rank=1, created_at=at("11:00"))

        planned = plan_days(app, now, {beyond: [waiting_car(day=beyond)]}, [undated])

        assert without_a_car(planned[1])
        assert (planned[1].plan_day, planned[1].orphan) == (None, False)

    def test_a_car_on_the_horizons_last_day_still_takes_a_stop(self, app):
        now = at("12:00")
        last_day = days(app.config["ETA_UNCONFIRMED_HORIZON_DAYS"])
        undated = pool_stop(1, point(2.0), rank=1, created_at=at("11:00"))

        planned = plan_days(app, now, {last_day: [waiting_car(day=last_day)]}, [undated])

        assert (planned[1].plan_day, planned[1].driver_id) == (last_day, 1)
        assert minutes_between(at("11:40", last_day), planned[1].expected_at) == pytest.approx(
            depot_leg(DEPOT, point(2.0))
        )

    def test_a_dated_stop_keeps_its_date_when_its_days_car_has_no_capacity(self, app):
        """A car that starts at or after its end takes no stop that day, so the day has no car for a stop dated for it:
        the stop keeps its date, rather than move on to the next day with room (R10)."""
        now = at("12:00")
        no_capacity = waiting_car(day=TOMORROW, start_at=at("20:00", TOMORROW), end_at=at("19:00", TOMORROW))
        cars = {TOMORROW: [no_capacity], days(2): [waiting_car(day=days(2))]}

        planned = plan_days(app, now, cars, [dated_stop(1, point(2.0), day=TOMORROW, rank=1)])

        assert without_a_car(planned[1]) and planned[1].plan_day == TOMORROW

    def test_a_stop_dated_after_today_keeps_its_date_beyond_the_horizon(self, app):
        now = at("12:00")
        cars = {day: [waiting_car(day=day)] for day in (TODAY, TOMORROW, days(3))}  # none on the day after tomorrow
        week_out = dated_stop(1, point(2.0), day=days(7), rank=1)
        no_car_on_its_day = dated_stop(2, point(2.0), day=days(2), rank=2)

        planned = plan_days(app, now, cars, [week_out, no_car_on_its_day])

        assert without_a_car(planned[1]) and planned[1].plan_day == days(7)
        # On its own day with no car it keeps that date too, rather than moving to the next day that has a car.
        assert without_a_car(planned[2]) and planned[2].plan_day == days(2)

    def test_a_stop_dated_today_on_a_closed_day_is_unconfirmed(self, app):
        """An admin closes a day by making its driver unavailable (spec §3): that car finishes what it holds, takes
        nothing else, and is left out of later days' supply."""
        now = at("10:00")
        closed = waiting_car(available=False)
        dated_today = pool_stop(1, point(2.0), rank=1, created_at=at("09:00", YESTERDAY), customer_date=TODAY)

        planned = plan(app, now, [closed], [dated_today])

        assert without_a_car(planned[1]) and planned[1].plan_day is None

    def test_an_order_at_eight_waits_for_the_typical_start(self, app):
        """Spec §7 "Before the start": an 08:00 order on a car that typically starts at 11:40."""
        now = at("08:00")
        order = pool_stop(1, point(3.0), rank=1, created_at=at("08:00"))

        planned = plan(app, now, [waiting_car(start_at=at("11:40"))], [order])

        assert minutes_between(at("11:40"), planned[1].expected_at) == pytest.approx(depot_leg(DEPOT, point(3.0)))

    def test_a_car_with_no_capacity_takes_no_stop_and_keeps_its_in_flight_ones(self, app):
        """A car that starts at or after its end (here: rolled past it, its pickup made last night) takes no stop that
        day: its other held stops roll, though its in-flight stop stays and stretches its end past them."""
        now = at("21:00")
        rolled_past = waiting_car(1, start_at=at("21:15"), end_at=at("21:00"))
        held = held_stop(1, point(1.0), driver_id=1)
        aboard = held_stop(2, point(2.0), driver_id=1, status=DeliveryStatus.PICKED_UP)
        pooled = pool_stop(3, point(1.5), rank=1, created_at=at("20:00"))
        other = waiting_car(2, start_at=at("21:00"), end_at=at("23:00"))

        tomorrow = {TOMORROW: [waiting_car(1, day=TOMORROW)]}
        planned = plan(app, now, [rolled_past, other], [held, aboard, pooled], later=tomorrow)

        assert (planned[1].plan_day, planned[1].driver_id) == (TOMORROW, 1)
        assert (planned[2].plan_day, planned[2].driver_id, planned[2].stops_ahead) == (TODAY, 1, 0)
        assert minutes_between(at("21:15"), planned[2].expected_at) == pytest.approx(depot_leg(DEPOT, point(2.0)))
        assert (planned[3].plan_day, planned[3].driver_id) == (TODAY, 2)


class TestOrphansAndDeterminism:
    def test_a_stop_held_by_an_inactive_driver_is_an_orphan(self, app):
        now = at("12:00")
        working = waiting_car(1, start_at=at("12:00"))  # with room to spare
        held_by_inactive = held_stop(1, point(2.0), driver_id=7)  # supply made no car for driver 7

        planned = plan(app, now, [working], [held_by_inactive])

        assert planned[1].orphan
        assert without_a_car(planned[1]) and planned[1].plan_day is None

    def test_a_stop_held_by_an_active_driver_who_is_not_working_is_an_orphan(self, app):
        """Driver 2 is no car today (R1), though tomorrow's early-morning set has them: the stop is not carried to them
        tomorrow, nor handed to driver 1 today. R19 asks only whether its driver is a car today."""
        now = at("12:00")
        held = held_stop(1, point(2.0), driver_id=2)

        planned = plan(app, now, [waiting_car(1)], [held], later={TOMORROW: [waiting_car(2, day=TOMORROW)]})

        assert planned[1].orphan
        assert without_a_car(planned[1]) and planned[1].plan_day is None

    def test_a_ride_along_members_stops_join_the_owners_car(self, app):
        now = at("12:00")
        shared = driving_car(1, origin=point(1.0), origin_at=at("11:55"), members=(4,))
        members_stop = held_stop(1, point(2.0), driver_id=4)

        planned = plan(app, now, [shared], [members_stop])

        assert not planned[1].orphan
        assert (planned[1].driver_id, planned[1].plan_day, planned[1].stops_ahead) == (1, TODAY, 0)

    def test_an_orphan_dated_today_is_unconfirmed(self, app):
        now = at("12:00")
        dated_today = held_stop(1, point(2.0), driver_id=7, customer_date=TODAY)
        dated_later = held_stop(2, point(2.0), driver_id=7, customer_date=days(2), plan_day=days(2))
        cars = {day: [waiting_car(1, day=day)] for day in (TODAY, TOMORROW, days(2), days(3))}

        planned = plan_days(app, now, cars, [dated_today, dated_later])

        assert planned[1].orphan and without_a_car(planned[1]) and planned[1].plan_day is None
        # An orphan the customer dated after today keeps that date (R19).
        assert planned[2].orphan and without_a_car(planned[2]) and planned[2].plan_day == days(2)

    def test_the_plan_is_deterministic(self, app):
        now = at("12:00")
        cars_by_day = {
            TODAY: [
                driving_car(1, origin=point(1.0), origin_at=at("11:50"), members=(4,), bottles=5, full_load=12),
                waiting_car(2, start_at=at("12:30")),
                waiting_car(3, start_at=at("12:30")),  # the same as car 2: ties are broken, never left to input order
                driving_car(5, origin=point(2.0), origin_at=at("11:40"), available=False),
            ],
            TOMORROW: [waiting_car(1, day=TOMORROW), waiting_car(2, day=TOMORROW)],
            days(2): [waiting_car(2, day=days(2))],
        }
        stops = [
            held_stop(1, point(3.0), status=DeliveryStatus.IN_TRANSIT, committed=True),
            held_stop(2, point(5.0), route_index=0, bottles=4),
            held_stop(3, point(4.0), bottles=3),
            held_stop(4, point(4.0), driver_id=4, bottles=3),
            held_stop(5, point(2.5), driver_id=5),
            held_stop(6, point(1.0), driver_id=9),
            pool_stop(7, point(2.0), rank=1, created_at=at("12:00", YESTERDAY)),
            pool_stop(8, point(2.0), rank=2, created_at=at("09:00")),
            pool_stop(9, point(2.0), rank=3, created_at=at("09:30")),
            pool_stop(10, None, rank=4, created_at=at("10:00")),
            pool_stop(11, point(6.0), rank=5, created_at=at("10:30"), window=(at("15:00"), at("17:00"))),
            pool_stop(12, point(13.0), rank=6, created_at=at("11:00")),
            dated_stop(13, point(1.5), day=TODAY, rank=7, release=at("13:00")),
            dated_stop(14, point(2.5), day=TOMORROW, rank=8),
            dated_stop(15, point(2.5), day=days(9), rank=9),
        ]
        reference = build_plan(cars_by_day, stops, SEED, now, DEPOT, km=km, config=app.config)

        for seed in range(5):
            shuffle = random.Random(seed)
            shuffled_days = shuffle.sample(list(cars_by_day.items()), len(cars_by_day))
            shuffled_cars = {day: shuffle.sample(cars, len(cars)) for day, cars in shuffled_days}
            shuffled_stops = shuffle.sample(stops, len(stops))

            assert build_plan(shuffled_cars, shuffled_stops, SEED, now, DEPOT, km=km, config=app.config) == reference
