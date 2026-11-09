"""Supply of the capacity-aware ETA plan (honest-ETA Phase 2, Task 2): the cars that deliver on each plan day.

Read through `cars_for_day`, as the engine reads it: who works today (R1), the early-morning set (R2), typical start
and end and today's start signals (R3), where a car's current leg started (R4), bottles (R6), and the supply of later
days (R10). The rows are the builders' (`tests/integration/eta_plan_builders.py`) on the test's own clock, and every
test hands `now` in, so none depends on the time of day. `NOW` is 2026-11-04 06:00Z: 11:00 on a Wednesday in Tashkent.
"""

from datetime import UTC, date, datetime, time, timedelta
from typing import Dict
from unittest.mock import patch

import pytest

from business_app.models.bottle import DriverBottleSession
from business_app.models.delivery import DeliveryPerson, shift_window_on
from business_app.services.eta_plan.supply import Car, cars_for_day
from business_app.utils.delivery_window import window_edge_utc
from business_app.utils.helpers import get_warehouse_coordinates
from business_app.utils.local_windows import minutes_past_local_midnight
from shared.enums import DeliveryStatus, UserStatus
from tests.integration.eta_plan_builders import (
    bind,
    close_session,
    hand_over,
    join_session,
    locate,
    make_driver,
    open_session,
    order_at_km,
    point_at_km,
    reassign,
    reopen_session,
    tap,
    worked_day,
)

pytestmark = pytest.mark.unit

TODAY = date(2026, 11, 4)
NOW = datetime(2026, 11, 4, 6, 0, tzinfo=UTC)  # 11:00 in Tashkent
TOMORROW = TODAY + timedelta(days=1)


def local(hhmm: str, day: date = TODAY) -> datetime:
    """`hhmm` on the Tashkent wall clock on the local date `day`, as the UTC instant a column holds."""
    return window_edge_utc(day, time.fromisoformat(hhmm))


def supply(day: date = TODAY, now: datetime = NOW) -> Dict[int, Car]:
    return {car.driver_id: car for car in cars_for_day(day, now)}


def minutes(instant: datetime, day: date = TODAY) -> int:
    return minutes_past_local_midnight(instant, day)


def accepted_stop(db, driver, at, *, km=2.0):
    """A pool order `driver` accepts at `at`: the stop they then hold, with its accept on the history."""
    order = order_at_km(db, km, created_at=at - timedelta(minutes=30))
    tap(db, order.delivery, DeliveryStatus.ASSIGNED, at, by=driver)
    return order.delivery


class TestWhoWorks:
    def test_a_fresh_open_session_is_a_car(self, db):
        driver = make_driver(db)
        open_session(db, driver, NOW - timedelta(hours=2))
        # Available, with shift time left: a car only if nobody worked and the early-morning set stood in.
        make_driver(db)

        assert set(supply()) == {driver.id}

    def test_a_session_quiet_for_a_day_is_not_a_car(self, db):
        quiet = make_driver(db)
        session = open_session(db, quiet, NOW - timedelta(hours=25))
        fresh = make_driver(db)
        open_session(db, fresh, NOW - timedelta(hours=1))

        assert set(supply()) == {fresh.id}

        # A real delivered tap on one of the session's orders is activity, and wakes it.
        order = order_at_km(db, 1.0, created_at=NOW - timedelta(hours=26), driver=quiet)
        bind(db, session, order)
        tap(db, order.delivery, DeliveryStatus.PICKED_UP, NOW - timedelta(hours=23))
        tap(db, order.delivery, DeliveryStatus.DELIVERED, NOW - timedelta(hours=22))

        assert set(supply()) == {quiet.id, fresh.id}

    def test_a_reopen_by_an_admin_is_not_activity(self, db):
        corrected = make_driver(db)
        session = open_session(db, corrected, NOW - timedelta(hours=30))
        close_session(db, session, NOW - timedelta(hours=26))
        reopen_session(db, session, NOW - timedelta(hours=1))
        fresh = make_driver(db)
        open_session(db, fresh, NOW - timedelta(hours=1))

        assert set(supply()) == {fresh.id}

    def test_a_transfer_sent_from_a_session_is_activity(self, db):
        sender = make_driver(db)
        session = open_session(db, sender, NOW - timedelta(hours=30))  # opened more than a day ago...
        receiver = make_driver(db)
        hand_over(db, session, receiver, NOW - timedelta(hours=2), quantity=5)  # ...but it sent bottles on today

        assert set(supply()) == {sender.id, receiver.id}

    def test_a_held_delivery_makes_a_car_only_with_history_in_the_last_48_hours(self, db):
        recent = make_driver(db)
        accepted_stop(db, recent, NOW - timedelta(hours=47))
        forgotten = make_driver(db)
        accepted_stop(db, forgotten, NOW - timedelta(hours=49))

        assert set(supply()) == {recent.id}

    def test_an_admin_reassign_today_keeps_a_held_stop_fresh(self, db, admin_user):
        first = make_driver(db)
        second = make_driver(db)
        delivery = accepted_stop(db, first, NOW - timedelta(days=3))
        reassign(db, delivery, second, NOW - timedelta(hours=1), by=admin_user)  # ASSIGNED -> ASSIGNED, today
        make_driver(db)  # a bystander the early-morning set would add

        assert set(supply()) == {second.id}

    def test_every_day_counts_even_off_the_working_days_list(self, db):
        sunday = date(2026, 11, 8)
        sunday_now = local("11:00", sunday)
        assert sunday.weekday() == 6
        driver = make_driver(db, working_days=["monday", "tuesday", "wednesday", "thursday", "friday", "saturday"])

        # Nobody works yet, so the early-morning set stands in: the roster's hours count, its days do not.
        assert set(supply(sunday, sunday_now)) == {driver.id}

        open_session(db, driver, sunday_now - timedelta(hours=1))

        assert set(supply(sunday, sunday_now)) == {driver.id}

    def test_nobody_working_uses_the_early_morning_set_with_shift_time_left(self, db):
        on_shift = make_driver(db, roster=("09:00", "18:00"))
        tonight = make_driver(db, roster=("22:00", "06:00"))  # last night's shift is over; tonight's is to come
        make_driver(db, roster=("06:00", "10:00"))  # done for the day by 11:00
        make_driver(db, available=False)
        make_driver(db, roster=(None, None))
        make_driver(db, active=False)
        make_driver(db, status=UserStatus.INACTIVE)

        cars = supply()

        assert set(cars) == {on_shift.id, tonight.id}
        car = cars[on_shift.id]
        assert (car.available, car.started, car.member_ids) == (True, False, ())
        assert (car.origin, car.leg_from_depot) == (get_warehouse_coordinates(), True)
        assert car.bottles_on_board == car.full_load == 30

    def test_a_ride_along_member_is_not_a_second_car(self, db):
        owner = make_driver(db)
        member = make_driver(db)
        session = open_session(db, owner, NOW - timedelta(hours=2))
        join_session(db, member, session, NOW - timedelta(hours=1, minutes=30))
        accepted_stop(db, member, NOW - timedelta(hours=1))

        cars = cars_for_day(TODAY, NOW)

        assert [(car.driver_id, car.member_ids) for car in cars] == [(owner.id, (member.id,))]

    def test_an_unavailable_driver_finishes_held_stops_but_takes_no_pool(self, db):
        closed = make_driver(db, available=False)
        accepted_stop(db, closed, NOW - timedelta(hours=1))
        bystander = make_driver(db)

        today = supply()

        assert set(today) == {closed.id}
        assert today[closed.id].available is False
        # Later days leave the car out (R10). With no other car working today, the day's early-morning set stands in.
        assert set(supply(TOMORROW)) == {bystander.id}


class TestTypicalTimes:
    def test_typical_start_is_the_median_first_pickup_with_a_fleet_fallback(self, db):
        steady = make_driver(db)
        newcomer = make_driver(db)
        brand_new = make_driver(db)
        for n, first_pickup in enumerate((660, 680, 700, 720, 740), start=1):
            worked_day(db, steady, TODAY - timedelta(days=n), first_pickup=first_pickup, last_drop=1200)
        for n in range(1, 5):  # four days: under the five a driver's own median needs
            worked_day(db, newcomer, TODAY - timedelta(days=n), first_pickup=600, last_drop=1200)

        cars = supply()  # nobody works yet: the early-morning set, each car at its typical start

        assert minutes(cars[steady.id].start_at) == 700
        # The fleet's median over all nine driver-days (600 x 4, 660, 680, 700, 720, 740), not the newcomer's own 600.
        assert minutes(cars[newcomer.id].start_at) == 660
        assert minutes(cars[brand_new.id].start_at) == 660

    def test_typical_end_is_the_p75_last_drop_capped_by_the_roster(self, db):
        late = make_driver(db, roster=("07:00", "23:30"))
        capped = make_driver(db, roster=("07:00", "22:00"))
        # 21:30, 22:00, 22:30, 23:00, and 00:40 after midnight, which still closes the plan day it started in.
        for driver in (late, capped):
            for n, last_drop in enumerate((1290, 1320, 1350, 1380, 1480), start=1):
                worked_day(db, driver, TODAY - timedelta(days=n), first_pickup=700, last_drop=last_drop)

        cars = supply()

        assert minutes(cars[late.id].end_at) == 1380
        assert minutes(cars[capped.id].end_at) == 1320

    def test_the_roster_end_is_carried_on_the_car(self, db):
        rostered = make_driver(db, roster=("09:00", "18:00"))
        overnight = make_driver(db, roster=("22:00", "06:00"))
        unrostered = make_driver(db, roster=(None, None))
        for driver in (rostered, overnight, unrostered):
            open_session(db, driver, NOW - timedelta(hours=1))

        cars = supply()

        assert cars[rostered.id].roster_end_at == local("18:00")
        assert cars[rostered.id].end_at == local("18:00")  # the 20:00 seed end, capped by the roster
        assert cars[overnight.id].roster_end_at == local("06:00", TOMORROW)
        assert cars[unrostered.id].roster_end_at is None  # no roster, no cap
        assert cars[unrostered.id].end_at == local("20:00")
        assert supply(TOMORROW)[rostered.id].roster_end_at == local("18:00", TOMORROW)

    def test_a_car_never_starts_before_its_roster_shift(self, db):
        late_shift = make_driver(db, roster=("14:00", "22:00"))  # new, so its typical start is the 11:40 seed

        car = supply()[late_shift.id]

        assert (car.start_at, car.origin_at) == (local("14:00"), local("14:00"))
        assert supply(TOMORROW)[late_shift.id].start_at == local("14:00", TOMORROW)


class TestStartSignals:
    def test_a_load_sets_the_start_and_a_pickup_marks_started(self, db):
        driver = make_driver(db)
        open_session(db, driver, local("09:00"), loaded=20)

        car = supply()[driver.id]

        assert (car.start_at, car.start_is_load, car.started) == (local("09:00"), True, False)
        assert (car.origin, car.origin_at, car.leg_from_depot) == (get_warehouse_coordinates(), local("09:10"), True)

        order = order_at_km(db, 3.0, created_at=local("08:00"), driver=driver)
        tap(db, order.delivery, DeliveryStatus.PICKED_UP, local("10:00"))

        car = supply()[driver.id]

        # On the road, its start is its first move: the load is now its depot departure (R4), not its start.
        assert (car.start_at, car.start_is_load, car.started) == (local("10:00"), False, True)

    def test_an_accept_does_not_move_the_start(self, db):
        driver = make_driver(db)
        accepted_stop(db, driver, local("08:00"))

        car = supply()[driver.id]

        # The fleet's seed start, 11:40: accepting from home is not setting off.
        assert (car.start_at, car.start_is_load, car.started) == (local("11:40"), False, False)
        assert car.origin_at == local("11:40")

    def test_a_transfer_opened_session_is_activity_but_not_a_load(self, db):
        colleague = make_driver(db)
        colleague_session = open_session(db, colleague, local("08:00"))
        driver = make_driver(db)
        order = order_at_km(db, 2.0, created_at=local("09:00"), driver=driver)
        tap(db, order.delivery, DeliveryStatus.PICKED_UP, local("10:00"))
        tap(db, order.delivery, DeliveryStatus.DELIVERED, local("10:20"))
        hand_over(db, colleague_session, driver, local("10:30"), quantity=10)  # nothing loaded at the depot
        idle = make_driver(db)
        hand_over(db, colleague_session, idle, local("10:30"), quantity=5)  # not on the road yet

        # Each hand-off's session is its driver's only claim to a car: neither holds a stop.
        cars = supply()

        # Not a load. The idle driver is still expected at the 11:40 seed start, not at the hand-off. The other's leg
        # starts at the door just delivered, not at a depot departure 10 minutes after the hand-off.
        assert (cars[idle.id].start_at, cars[idle.id].start_is_load) == (local("11:40"), False)
        car = cars[driver.id]
        assert (car.start_at, car.start_is_load) == (local("10:00"), False)
        assert (car.origin, car.origin_at, car.leg_from_depot) == (point_at_km(2.0), local("10:20"), False)

    def test_no_load_or_pickup_by_start_plus_thirty_rolls_the_start_forward(self, db):
        driver = make_driver(db)
        accepted_stop(db, driver, local("08:00"))

        assert supply(now=local("12:10"))[driver.id].start_at == local("11:40")  # the seed start, 30 minutes on
        later = local("12:15")
        car = supply(now=later)[driver.id]
        assert (car.start_at, car.origin_at) == (later + timedelta(minutes=15), later + timedelta(minutes=15))

        # A car on the road is never rolled, though it has no load or pickup today: picked up at 04:30, the plan day
        # before, and on its way since 09:00. Its start is that first move.
        night = make_driver(db)
        carried = order_at_km(db, 2.0, created_at=local("04:00"), driver=night)
        tap(db, carried.delivery, DeliveryStatus.PICKED_UP, local("04:30"))
        tap(db, carried.delivery, DeliveryStatus.IN_TRANSIT, local("09:00"))

        assert supply(now=later)[night.id].start_at == local("09:00")


class TestOrigins:
    def test_a_started_cars_origin_is_the_newest_position_the_system_knows(self, db):
        """Owner rule 2026-11-09: the driver's newest known position wins (a shared location or a door tap) when it is
        newer than the leg's start and at most 30 minutes old (DRIVER_LOCATION_FRESH_SECONDS); else the leg's start."""
        driver = make_driver(db)
        open_session(db, driver, local("09:00"))
        order = order_at_km(db, 2.0, created_at=local("08:00"), driver=driver)
        tap(db, order.delivery, DeliveryStatus.PICKED_UP, local("09:30"))
        tap(db, order.delivery, DeliveryStatus.IN_TRANSIT, local("09:40"))
        tap(db, order.delivery, DeliveryStatus.ARRIVED, local("09:55"))
        tap(db, order.delivery, DeliveryStatus.DELIVERED, local("10:00"))
        door, ping = point_at_km(2.0), point_at_km(3.0)

        locate(db, driver, ping, local("10:45"))  # newer than the stop
        car = supply()[driver.id]
        assert (car.origin, car.origin_at, car.leg_from_depot) == (ping, local("10:45"), False)

        locate(db, driver, ping, local("10:20"))  # newer than the stop too, but 40 minutes old: too old to use
        car = supply()[driver.id]
        assert (car.origin, car.origin_at, car.leg_from_depot) == (door, local("10:00"), False)

        locate(db, driver, ping, local("09:50"))  # older than the stop: the door is newer
        car = supply()[driver.id]
        assert (car.origin, car.origin_at, car.leg_from_depot) == (door, local("10:00"), False)

    def test_a_delivered_stop_without_a_door_still_starts_the_leg(self, db):
        driver = make_driver(db)
        open_session(db, driver, local("09:00"))
        first = order_at_km(db, 2.0, created_at=local("08:00"), driver=driver)
        second = order_at_km(db, 3.0, created_at=local("08:00"), driver=driver)
        tap(db, first.delivery, DeliveryStatus.PICKED_UP, local("09:20"))
        tap(db, second.delivery, DeliveryStatus.PICKED_UP, local("09:25"))
        tap(db, first.delivery, DeliveryStatus.DELIVERED, local("10:00"))  # at its door
        tap(db, second.delivery, DeliveryStatus.DELIVERED, local("10:40"), point=None)  # its address had no coordinates

        car = supply(now=local("10:41"))[driver.id]

        # Where the car is is unknown, but not since when: from the 10:00 door, the next stop would be due "now".
        assert (car.origin, car.origin_at, car.leg_from_depot) == (None, local("10:40"), False)

    def test_after_a_reload_the_leg_starts_at_the_depot_at_load_plus_allowance(self, db):
        driver = make_driver(db)
        morning = open_session(db, driver, local("09:00"))
        for picked_up, delivered in (("09:30", "11:00"), ("13:30", "14:30")):
            order = order_at_km(db, 2.0, created_at=local("08:00"), driver=driver)
            tap(db, order.delivery, DeliveryStatus.PICKED_UP, local(picked_up))
            tap(db, order.delivery, DeliveryStatus.DELIVERED, local(delivered))
        close_session(db, morning, local("14:55"))
        open_session(db, driver, local("15:00"))  # the reload

        car = supply(now=local("15:05"))[driver.id]

        assert car.started
        assert (car.origin, car.origin_at, car.leg_from_depot) == (get_warehouse_coordinates(), local("15:10"), True)

    def test_a_load_hours_before_the_first_pickup_departs_at_the_pickup(self, db):
        driver = make_driver(db)
        open_session(db, driver, local("08:00"))
        order = order_at_km(db, 2.0, created_at=local("07:30"), driver=driver)
        tap(db, order.delivery, DeliveryStatus.PICKED_UP, local("11:00"))

        car = supply(now=local("11:05"))[driver.id]

        assert (car.origin, car.origin_at, car.leg_from_depot) == (get_warehouse_coordinates(), local("11:00"), True)

    def test_with_no_load_today_the_leg_starts_at_the_first_pickup(self, db):
        driver = make_driver(db)
        open_session(db, driver, local("15:00", TODAY - timedelta(days=1)))  # opened yesterday
        # Picked up yesterday and set off for it at 09:30 today: a move today, but not today's first pickup.
        carried = order_at_km(db, 2.0, created_at=local("14:00", TODAY - timedelta(days=1)), driver=driver)
        tap(db, carried.delivery, DeliveryStatus.PICKED_UP, local("16:00", TODAY - timedelta(days=1)))
        tap(db, carried.delivery, DeliveryStatus.IN_TRANSIT, local("09:30"))
        for picked_up in ("10:00", "10:20"):
            order = order_at_km(db, 2.0, created_at=local("09:00"), driver=driver)
            tap(db, order.delivery, DeliveryStatus.PICKED_UP, local(picked_up))

        car = supply()[driver.id]

        assert car.start_is_load is False
        assert (car.origin, car.origin_at, car.leg_from_depot) == (get_warehouse_coordinates(), local("10:00"), True)

    def test_a_ping_after_leaving_the_depot_keeps_leg_from_depot(self, db):
        driver = make_driver(db)
        open_session(db, driver, local("09:00"))
        order = order_at_km(db, 4.0, created_at=local("08:00"), driver=driver)
        tap(db, order.delivery, DeliveryStatus.PICKED_UP, local("09:15"))
        ping = point_at_km(2.0)
        locate(db, driver, ping, local("10:50"))

        car = supply()[driver.id]

        # The ping is the origin, but the leg it is on left the depot, so its first leg keeps the depot line.
        assert (car.origin, car.origin_at, car.leg_from_depot) == (ping, local("10:50"), True)

    def test_a_loaded_car_not_yet_on_the_road_starts_from_its_newest_position(self, db):
        """Owner rule 2026-11-09: a driver who loaded and then drove off (to a petrol station in the city, say) before
        any tap is planned from the newest position the system knows, shared at any time after the load (no wait for
        the loading allowance), while it is at most 30 minutes old. With none, the car leaves the depot once loaded."""
        driver = make_driver(db)
        open_session(db, driver, local("10:00"))  # loaded at the depot
        depot, petrol_station = get_warehouse_coordinates(), point_at_km(6.0)

        car = supply()[driver.id]
        assert (car.started, car.origin, car.origin_at, car.leg_from_depot) == (False, depot, local("10:10"), True)

        locate(db, driver, petrol_station, local("10:05"))  # 5 minutes after the load: no wait, the newest wins
        car = supply(now=local("10:20"))[driver.id]
        driven_off = (False, petrol_station, local("10:05"), True)
        assert (car.started, car.origin, car.origin_at, car.leg_from_depot) == driven_off
        assert (car.start_at, car.start_is_load) == (local("10:00"), True)  # the load still sets its start

        car = supply(now=local("10:35"))[driver.id]  # 30 minutes old: still used
        assert (car.origin, car.origin_at) == (petrol_station, local("10:05"))

        car = supply(now=local("11:30"))[driver.id]  # 85 minutes old: too old, the depot once loaded
        assert (car.origin, car.origin_at) == (get_warehouse_coordinates(), local("10:10"))

    def test_the_loads_own_depot_stamp_is_not_a_newer_position(self, db):
        """A load stamps the depot as the driver's position (`anchor_driver_at_warehouse`) by its own clock, a moment
        after the session starts. That stamp is the load itself: the car still leaves after the loading allowance."""
        driver = make_driver(db)
        open_session(db, driver, local("10:00"))
        locate(db, driver, get_warehouse_coordinates(), local("10:00") + timedelta(milliseconds=3))

        car = supply()[driver.id]

        assert (car.origin, car.origin_at) == (get_warehouse_coordinates(), local("10:10"))

    def test_a_position_shared_before_the_shift_sets_off_when_the_shift_starts(self, db):
        """Loaded at 08:00 and shared from the city at 08:20, ahead of a 09:00 roster: planned from the city, but never
        before the shift starts."""
        driver = make_driver(db, roster=("09:00", "18:00"))
        open_session(db, driver, local("08:00"))
        petrol_station = point_at_km(6.0)
        locate(db, driver, petrol_station, local("08:20"))

        car = supply(now=local("08:30"))[driver.id]

        assert (car.started, car.start_at, car.start_is_load) == (False, local("09:00"), False)
        assert (car.origin, car.origin_at) == (petrol_station, local("09:00"))

    def test_a_car_that_has_not_loaded_today_ignores_a_shared_location(self, db):
        """Without a load today the car is taken to load at the depot at its start (R6), wherever it is now."""
        driver = make_driver(db)
        open_session(db, driver, local("15:00", TODAY - timedelta(days=1)))  # loaded yesterday: 20 hours of activity
        locate(db, driver, point_at_km(6.0), local("10:50"))  # fresh at 11:00

        car = supply()[driver.id]

        assert (car.started, car.start_is_load) == (False, False)
        assert (car.origin, car.leg_from_depot) == (get_warehouse_coordinates(), True)


class TestBottles:
    def test_bottles_on_board_exclude_collected_empties_and_in_flight_stops(self, db):
        driver = make_driver(db)
        session = open_session(db, driver, local("09:00"), loaded=30, delivered=27, collected=15)
        order = order_at_km(db, 1.0, created_at=local("08:00"), driver=driver, bottles=2)
        tap(db, order.delivery, DeliveryStatus.PICKED_UP, local("10:00"))

        # The 15 empties ride along (`current_inventory`), but they are not water anyone can be given.
        assert session.current_inventory == 18
        assert supply()[driver.id].bottles_on_board == 1

        session.bottles_delivered = 29
        db.session.commit()

        assert supply()[driver.id].bottles_on_board == 0  # 1 full bottle, 2 of them already promised

    def test_current_inventory_is_unchanged_for_a_negative_base_session(self):
        session = DriverBottleSession(
            bottles_loaded=10,
            bottles_transferred_in=1,
            bottles_delivered=14,
            bottles_transferred_out=0,
            bottles_collected_from_customers=6,
            bottles_returned_to_warehouse=2,
        )

        assert session.full_bottles_on_board == -3  # unclamped: the tallies disagree, and the sign says so
        assert session.current_inventory == 3
        session.compute_discrepancy()
        assert session.discrepancy == 1

    def test_a_car_with_no_session_carries_a_full_load(self, db):
        sessionless = make_driver(db)
        accepted_stop(db, sessionless, NOW - timedelta(hours=1))
        # A session kept open overnight, not started today and with no load today: it leaves full, as later days do.
        overnight = make_driver(db)
        open_session(db, overnight, local("16:00", TODAY - timedelta(days=1)), loaded=30, delivered=25)

        cars = supply()

        assert cars[sessionless.id].bottles_on_board == cars[sessionless.id].full_load == 30
        assert cars[overnight.id].bottles_on_board == cars[overnight.id].full_load == 30

    def test_a_second_load_today_is_the_days_reload(self, db):
        """R6: a car reloads at most once a day. A second load in today's plan day was that reload, so the planner adds
        none; a load yesterday and one today is no reload, and a later day starts with none."""
        reloaded = make_driver(db)
        morning = open_session(db, reloaded, local("09:00"))
        close_session(db, morning, local("14:55"))
        open_session(db, reloaded, local("15:00"))
        once = make_driver(db)
        yesterday = open_session(db, once, local("16:00", TODAY - timedelta(days=1)))
        close_session(db, yesterday, local("20:00", TODAY - timedelta(days=1)))
        open_session(db, once, local("09:00"))

        cars = supply(now=local("15:05"))

        assert (cars[reloaded.id].reloaded_today, cars[once.id].reloaded_today) == (True, False)
        tomorrow = cars_for_day(TOMORROW, local("15:05"))
        assert {car.driver_id for car in tomorrow} == {reloaded.id, once.id}
        assert not any(car.reloaded_today for car in tomorrow)

    def test_full_load_is_the_drivers_median_load(self, db):
        driver = make_driver(db)
        newcomer = make_driver(db)

        assert supply()[driver.id].full_load == 30  # no closed session with a load anywhere: the default

        other = make_driver(db)
        for owner, loads in ((driver, (30, 40, 31)), (other, (50, 45))):
            for n, loaded in enumerate(loads, start=1):
                session = open_session(db, owner, local("09:00", TODAY - timedelta(days=n)), loaded=loaded)
                close_session(db, session, local("18:00", TODAY - timedelta(days=n)))

        cars = supply()

        assert cars[driver.id].full_load == 31
        assert cars[newcomer.id].full_load == 40  # the fleet's: 30, 40, 31, 50 and 45


class TestShifts:
    def test_a_null_roster_is_no_shift_and_is_working_now_returns_false(self):
        assert shift_window_on(TODAY, None, None) is None
        assert shift_window_on(TODAY, "09:00", None) is None
        assert shift_window_on(TODAY, "9 am", "18:00") is None
        driver = DeliveryPerson(is_active=True, is_available=True, working_hours_start=None, working_hours_end=None)

        with patch("business_app.utils.timezone_utils.get_utc_now", return_value=NOW):
            assert driver.is_working_now is False

    def test_an_overnight_roster_still_works_at_one_in_the_morning(self):
        assert shift_window_on(TODAY, "22:00", "06:00") == (local("22:00"), local("06:00", TOMORROW))
        driver = DeliveryPerson(
            is_active=True, is_available=True, working_hours_start="22:00", working_hours_end="06:00"
        )

        with patch("business_app.utils.timezone_utils.get_utc_now", return_value=local("01:00")):
            assert driver.is_working_now is True

        # An end equal to the start is an empty shift, which is no shift: not a day-long one.
        assert shift_window_on(TODAY, "09:00", "09:00") is None
        empty = DeliveryPerson(
            is_active=True, is_available=True, working_hours_start="09:00", working_hours_end="09:00"
        )

        with patch("business_app.utils.timezone_utils.get_utc_now", return_value=NOW):
            assert empty.is_working_now is False
