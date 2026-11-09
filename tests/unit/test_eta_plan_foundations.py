"""Foundations of the capacity-aware ETA plan (honest-ETA Phase 2, Task 1): what every later task imports.

- The plan day (spec §6): `work_day_of`, `work_day_bounds` and `minutes_past_local_midnight` in `local_windows`, and
  `window_edge_utc` in `delivery_window`. Every instant is frozen and handed in, so no test depends on the time of day.
- The ETA math that moved out of `delivery_eta_service` into `eta_plan.common` (`moved_into`, `minutes_between`,
  `percentile`), and the stop sets and the driver-held filter of `eta_plan.scope`.
- The tunables, the structured seeds, the Redis keys and the row vocabulary of the engine's hidden rows.

Real rows where a query is the point (the driver-held filter, `moved_into`); nothing is faked.
"""

import ast
import itertools
import random
import statistics
import sys
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.orm import aliased

from business_app.config import testing as testing_config
from business_app.models.delivery import Delivery, DeliveryStatusHistory
from business_app.models.delivery_promise import (
    MODEL_ENGINE_S1,
    MODEL_GOOGLE_TAP,
    MODEL_OSRM_SHADOW,
    PROMISE_STAGES,
    DeliveryPromise,
)
from business_app.models.order import Order
from business_app.services import delivery_eta_service
from business_app.services.eta_plan import common
from business_app.services.eta_plan.calibration import bin_index
from business_app.services.eta_plan.common import minutes_between, moved_into, percentile
from business_app.services.eta_plan.scope import (
    DRIVER_HELD_ORDER_STATUSES,
    IN_FLIGHT_DELIVERY_STATUSES,
    driver_held_rows_filter,
    is_driver_held,
)
from business_app.utils.delivery_window import window_edge_utc
from business_app.utils.local_windows import (
    local_instant_at_minutes,
    minutes_past_local_midnight,
    work_day_bounds,
    work_day_of,
)
from shared import business_config
from shared.constants import ETA_STAGE_WAITING
from shared.enums import DeliveryStatus, OrderStatus
from shared.redis_keyspace import KEYSPACE_TIERS, RedisKeyspace, RedisUsageTier

pytestmark = pytest.mark.unit

# Tashkent is UTC+5 all year. 2026-11-04 05:00 local is 2026-11-04 00:00Z: the start of that plan day.
PLAN_DAY = date(2026, 11, 4)
PLAN_DAY_START = datetime(2026, 11, 4, 0, 0, tzinfo=UTC)
NEXT_PLAN_DAY_START = datetime(2026, 11, 5, 0, 0, tzinfo=UTC)
A_MORNING = datetime(2026, 11, 4, 4, 0, tzinfo=UTC)  # 09:00 in Tashkent


class TestThePlanDay:
    def test_a_plan_day_runs_from_five_to_five(self):
        # 04:59 in Tashkent on the 4th is still the 3rd's plan day: the UTC date and the plan day disagree.
        assert work_day_of(datetime(2026, 11, 3, 23, 59, tzinfo=UTC)) == date(2026, 11, 3)
        assert work_day_of(PLAN_DAY_START) == PLAN_DAY  # 05:00 opens the day
        assert work_day_of(datetime(2026, 11, 4, 18, 59, tzinfo=UTC)) == PLAN_DAY  # 23:59
        # Past local midnight the plan day is still running: 00:40 on the 5th belongs to the 4th.
        assert work_day_of(datetime(2026, 11, 4, 19, 40, tzinfo=UTC)) == PLAN_DAY
        assert work_day_of(datetime(2026, 11, 4, 23, 59, 59, tzinfo=UTC)) == PLAN_DAY  # 04:59:59, its last second
        assert work_day_of(NEXT_PLAN_DAY_START) == date(2026, 11, 5)
        assert work_day_bounds(PLAN_DAY) == (PLAN_DAY_START, NEXT_PLAN_DAY_START)

    def test_consecutive_plan_days_tile(self):
        """Half-open: nothing is counted twice and nothing falls between two days."""
        assert work_day_bounds(date(2026, 11, 5))[0] == work_day_bounds(PLAN_DAY)[1]

    def test_the_day_of_an_instant_is_the_day_whose_bounds_hold_it(self):
        """The two helpers are one definition: at every half hour of three days, across both edges."""
        instant = datetime(2026, 11, 3, 0, 0, tzinfo=UTC)
        for _ in range(3 * 48):
            start, end = work_day_bounds(work_day_of(instant))
            assert start <= instant < end, instant
            instant += timedelta(minutes=30)

    def test_a_naive_instant_is_utc_as_a_sqlite_column_returns_it(self):
        assert work_day_of(datetime(2026, 11, 3, 23, 59)) == date(2026, 11, 3)
        assert work_day_of(datetime(2026, 11, 4, 0, 0)) == date(2026, 11, 4)

    @pytest.mark.parametrize(
        "instant, minutes",
        [
            # 00:40 local on the 5th, measured from the 4th's local midnight (19:00Z on the 3rd): 24 h + 40 min.
            pytest.param(datetime(2026, 11, 4, 19, 40, tzinfo=UTC), 1480, id="past-local-midnight"),
            pytest.param(datetime(2026, 11, 4, 6, 40, tzinfo=UTC), 700, id="11:40"),
            pytest.param(datetime(2026, 11, 3, 19, 0, tzinfo=UTC), 0, id="local-midnight-itself"),
            pytest.param(datetime(2026, 11, 4, 6, 40, 59, tzinfo=UTC), 700, id="seconds-are-dropped"),
            pytest.param(datetime(2026, 11, 4, 6, 40), 700, id="a-naive-instant-is-utc"),
        ],
    )
    def test_minutes_past_midnight_can_exceed_a_day(self, instant, minutes):
        assert minutes_past_local_midnight(instant, PLAN_DAY) == minutes

    def test_the_instant_at_minutes_past_midnight_is_the_inverse(self):
        """1480 minutes from the 4th's local midnight is 00:40 on the 5th, 19:40Z on the 4th."""
        assert local_instant_at_minutes(PLAN_DAY, 1480) == datetime(2026, 11, 4, 19, 40, tzinfo=UTC)


class TestAWindowEdge:
    @pytest.mark.parametrize(
        "day, local_time, utc",
        [
            pytest.param(PLAN_DAY, time(9, 0), datetime(2026, 11, 4, 4, 0, tzinfo=UTC), id="09:00"),
            # Local early morning is the UTC evening before: the edge's date is the customer's, not UTC's.
            pytest.param(PLAN_DAY, time(2, 30), datetime(2026, 11, 3, 21, 30, tzinfo=UTC), id="02:30"),
            pytest.param(PLAN_DAY, time(23, 59), datetime(2026, 11, 4, 18, 59, tzinfo=UTC), id="23:59"),
        ],
    )
    def test_a_window_edge_is_local_wall_clock(self, day, local_time, utc):
        edge = window_edge_utc(day, local_time)

        assert edge == utc
        assert edge.utcoffset() == timedelta(0)  # an aware UTC instant, comparable with any column read


class TestTheSharedEtaMath:
    def test_percentile_matches_the_phase_one_p90_and_handles_one_value(self):
        assert percentile(range(1, 31), 0.9) == statistics.quantiles(range(1, 31), n=10, method="inclusive")[-1]
        assert percentile([5], 0.75) == 5
        with pytest.raises(ValueError):
            percentile([], 0.5)

    def test_percentile_is_exactly_what_statistics_quantiles_returns(self):
        """Phase 1's `_p90` was `statistics.quantiles(n=10, method="inclusive")[-1]`, and headroom must not move by
        so much as the last bit. A float `lower + (upper - lower) * 0.1` differs from it for about a third of inputs."""
        rng = random.Random(20261104)
        for q, parts, cut in ((0.5, 2, 0), (0.75, 4, 2), (0.9, 10, 8)):
            for size in range(2, 61):
                values = [rng.uniform(0.5, 6.0) for _ in range(size)]  # unsorted: the function sorts
                expected = statistics.quantiles(values, n=parts, method="inclusive")[cut]
                assert percentile(values, q) == expected, (q, size)

    def test_percentile_runs_from_the_smallest_to_the_largest_value(self):
        assert percentile([3, 1, 2], 0) == 1
        assert percentile([3, 1, 2], 1) == 3
        assert percentile([3, 1, 2], 0.5) == 2

    @pytest.mark.parametrize("q", [-0.1, 1.1, 75])
    def test_a_percentile_outside_zero_to_one_is_refused(self, q):
        """`percentile(values, 75)` is a slip for 0.75 and must not read past the end of the values."""
        with pytest.raises(ValueError):
            percentile([1, 2, 3], q)

    def test_minutes_between_is_fractional_minutes(self):
        start = datetime(2026, 11, 4, 4, 0, tzinfo=UTC)

        assert minutes_between(start, start + timedelta(seconds=90)) == pytest.approx(1.5)
        assert minutes_between(start, start - timedelta(minutes=2)) == pytest.approx(-2.0)

    def test_moved_into_is_a_real_move_never_a_reassign_row(self, db, sample_user, delivery_driver):
        delivery = _stop(
            db,
            sample_user,
            delivery_driver,
            delivery_status=DeliveryStatus.IN_TRANSIT,
            order_status=OrderStatus.OUT_FOR_DELIVERY,
            number="FND-0001",
        )
        moves = {
            "tap": (DeliveryStatus.PICKED_UP, DeliveryStatus.IN_TRANSIT),
            "reassign": (DeliveryStatus.IN_TRANSIT, DeliveryStatus.IN_TRANSIT),
            "pickup": (DeliveryStatus.ASSIGNED, DeliveryStatus.PICKED_UP),
        }
        ids = {}
        for index, (name, (old, new)) in enumerate(moves.items()):
            row = DeliveryStatusHistory(
                delivery_id=delivery.id,
                old_status=old,
                new_status=new,
                changed_by=delivery_driver.id,
                changed_at=A_MORNING + timedelta(minutes=index),
            )
            db.session.add(row)
            db.session.flush()
            ids[name] = row.id
        db.session.commit()

        into_in_transit = DeliveryStatusHistory.query.filter(
            moved_into(DeliveryStatusHistory, DeliveryStatus.IN_TRANSIT)
        )
        assert [row.id for row in into_in_transit] == [ids["tap"]]
        # The same clause works on an alias, which is how the correlated queries of `delivery_eta_service` use it.
        history = aliased(DeliveryStatusHistory, name="history")
        into_picked_up = db.session.query(history.id).filter(moved_into(history, DeliveryStatus.PICKED_UP))
        assert [row.id for row in into_picked_up] == [ids["pickup"]]


def _imported_modules(module):
    """Every module a source file imports, as dotted names: `import a.b` gives `a.b`, and `from a.b import c` gives
    both `a.b` and `a.b.c`. A relative import keeps its leading dots."""
    imported = set()
    for node in ast.walk(ast.parse(Path(module.__file__).read_text())):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = "." * node.level + (node.module or "")
            imported.add(base)
            imported.update(f"{base}.{alias.name}" for alias in node.names)
    return imported


class TestTheMovedHelpers:
    def test_delivery_eta_service_uses_the_shared_helpers(self):
        """Phase 1 keeps no copy of what moved: the service's names are the shared functions."""
        assert delivery_eta_service.moved_into is common.moved_into
        assert delivery_eta_service.minutes_between is common.minutes_between
        assert delivery_eta_service.percentile is common.percentile
        for private in ("_moved_into", "_minutes", "_p90"):
            assert not hasattr(delivery_eta_service, private), private

    def test_common_imports_only_the_standard_library_and_sqlalchemy(self):
        """`notification_service` imports `delivery_eta_service` at module level, and that imports `common`: keeping
        `common` free of the app's own modules means it can never close an import cycle with them."""
        top_level = {name.split(".")[0] for name in _imported_modules(common)}

        assert top_level <= set(sys.stdlib_module_names) | {"sqlalchemy"}, top_level

    def test_neither_the_service_nor_common_imports_scope(self):
        """`scope` pulls in `StaffService`, and `notification_service` imports the Phase 1 service at module level."""
        for module in (delivery_eta_service, common):
            assert "business_app.services.eta_plan.scope" not in _imported_modules(module), module.__name__


def _stop(db, customer, driver, *, delivery_status, order_status, number):
    """An order and its delivery: `driver`'s (a users.id), or nobody's when `driver` is None."""
    order = Order(
        user_id=customer.id,
        order_number=number,
        status=order_status,
        subtotal=Decimal("30000"),
        total_amount=Decimal("30000"),
    )
    db.session.add(order)
    db.session.flush()
    delivery = Delivery(
        order_id=order.id,
        delivery_person_id=driver.id if driver is not None else None,
        status=delivery_status,
        scheduled_date=A_MORNING,
        scheduled_time_slot="09:00-12:00",
        tracking_number=f"TRK-{number}",
    )
    db.session.add(delivery)
    db.session.commit()
    return delivery


def _driver_held_ids(db):
    """The deliveries the filter keeps, joined the way a caller must: `orders` and `deliveries` explicitly."""
    rows = db.session.query(Delivery.id).join(Order, Order.id == Delivery.order_id).filter(driver_held_rows_filter())
    return {row.id for row in rows}


class TestTheStopSets:
    def test_the_shared_stop_sets(self, db, sample_user, delivery_driver):
        # Sorted by value, so an SQL `IN` list is the same on every run.
        assert IN_FLIGHT_DELIVERY_STATUSES == (
            DeliveryStatus.ARRIVED,
            DeliveryStatus.IN_TRANSIT,
            DeliveryStatus.PICKED_UP,
        )
        # A pickup moves the order to OUT_FOR_DELIVERY, so a confirmed/preparing-only gate drops every in-flight stop.
        assert DRIVER_HELD_ORDER_STATUSES == frozenset(
            {OrderStatus.CONFIRMED, OrderStatus.PREPARING, OrderStatus.OUT_FOR_DELIVERY}
        )
        out_for_delivery = _stop(
            db,
            sample_user,
            delivery_driver,
            delivery_status=DeliveryStatus.IN_TRANSIT,
            order_status=OrderStatus.OUT_FOR_DELIVERY,
            number="FND-0010",
        )
        pending = _stop(
            db,
            sample_user,
            delivery_driver,
            delivery_status=DeliveryStatus.ASSIGNED,
            order_status=OrderStatus.PENDING,
            number="FND-0011",
        )

        held = _driver_held_ids(db)

        assert out_for_delivery.id in held
        assert pending.id not in held

    @pytest.mark.parametrize(
        "delivery_status, order_status, has_driver, held",
        [
            pytest.param(DeliveryStatus.ASSIGNED, OrderStatus.CONFIRMED, True, True, id="accepted"),
            pytest.param(DeliveryStatus.ASSIGNED, OrderStatus.PREPARING, True, True, id="accepted-while-preparing"),
            pytest.param(DeliveryStatus.PICKED_UP, OrderStatus.OUT_FOR_DELIVERY, True, True, id="picked-up"),
            pytest.param(DeliveryStatus.IN_TRANSIT, OrderStatus.OUT_FOR_DELIVERY, True, True, id="on-the-way"),
            pytest.param(DeliveryStatus.ARRIVED, OrderStatus.OUT_FOR_DELIVERY, True, True, id="arrived"),
            # Not held: the pool (no driver), a finished or failed stop (a FAILED row keeps its driver id), a stop
            # whose order is not planned (unconfirmed) or no longer alive.
            pytest.param(DeliveryStatus.PENDING, OrderStatus.CONFIRMED, False, False, id="in-the-pool"),
            pytest.param(DeliveryStatus.ASSIGNED, OrderStatus.CONFIRMED, False, False, id="no-driver"),
            pytest.param(DeliveryStatus.FAILED, OrderStatus.OUT_FOR_DELIVERY, True, False, id="failed"),
            pytest.param(DeliveryStatus.DELIVERED, OrderStatus.DELIVERED, True, False, id="delivered"),
            pytest.param(DeliveryStatus.RESCHEDULED, OrderStatus.CONFIRMED, True, False, id="rescheduled"),
            pytest.param(DeliveryStatus.ASSIGNED, OrderStatus.CANCELLED, True, False, id="cancelled-order"),
        ],
    )
    def test_the_driver_held_filter_keeps_exactly_the_stops_a_driver_holds(
        self, db, sample_user, delivery_driver, delivery_status, order_status, has_driver, held
    ):
        delivery = _stop(
            db,
            sample_user,
            delivery_driver if has_driver else None,
            delivery_status=delivery_status,
            order_status=order_status,
            number="FND-0020",
        )

        assert (delivery.id in _driver_held_ids(db)) is held

    def test_the_driver_held_predicate_agrees_with_the_filter(self, db, sample_user, delivery_driver):
        """The planner asks `is_driver_held` of a stop demand already gated by its order; SQL asks the filter. Over
        every delivery status, with and without a driver, on each order status a driver can hold, they keep the same
        stops."""
        order_statuses = sorted(DRIVER_HELD_ORDER_STATUSES, key=lambda status: status.value)
        cases = itertools.product(DeliveryStatus, (delivery_driver, None), order_statuses)
        deliveries = [
            _stop(db, sample_user, driver, delivery_status=status, order_status=order_status, number=f"FND-{n:04d}")
            for n, (status, driver, order_status) in enumerate(cases, start=100)
        ]

        assert {d.id for d in deliveries if is_driver_held(d.status, d.delivery_person_id)} == _driver_held_ids(db)


# The shipped defaults, as the plan states them (rulings R1-R19 and Task 1). Pinned with their types: a float read
# through `_int` would not load an override such as `ETA_UNKNOWN_LEG_KM=2.5`, and a bool is not an int.
PLAN_TUNABLES = {
    # R1: when a car works today
    "ETA_SESSION_ACTIVITY_HOURS": 24,
    # R3: start and end of a driver's day
    "ETA_HISTORY_WORK_DAYS": 14,
    "ETA_HISTORY_LOOKBACK_DAYS": 60,
    "ETA_MIN_DRIVER_DAYS": 5,
    "ETA_FLEET_START_SEED_MINUTES": 700,
    "ETA_FLEET_END_SEED_MINUTES": 1200,
    "ETA_START_GRACE_MINUTES": 30,
    "ETA_START_ROLL_MINUTES": 15,
    # R4, R6, R7: legs, bottles, stops without coordinates
    "ETA_LOADING_ALLOWANCE_MINUTES": 10,
    "ETA_FULL_LOAD_SESSIONS": 10,
    "ETA_RELOAD_FULL_BOTTLES_DEFAULT": 30,
    "ETA_UNKNOWN_LEG_KM": 2.3,
    # R10, R11, R12: later days, windows, recording
    "ETA_UNCONFIRMED_HORIZON_DAYS": 3,
    "ETA_NOT_STARTED_EXTRA_HI_MINUTES": 60,
    "ETA_RECORD_MOVE_MINUTES": 15,
    # R15: the trigger
    "ETA_RECOMPUTE_SCHEDULED_TTL_SECONDS": 30,
    "ETA_RECOMPUTE_COUNTDOWN_SECONDS": 5,
    "ETA_RECOMPUTE_LOCK_SECONDS": 120,
    # Spec §4.9: when a changed estimate is worth telling the customer (counted in shadow in Phase 2)
    "ETA_CHANGE_LATER_MINUTES": 30,
    "ETA_CHANGE_EARLIER_MINUTES": 60,
    "ETA_CHANGE_MIN_GAP_MINUTES": 60,
    "ETA_CHANGE_MAX_PER_ORDER": 3,
    "ETA_QUIET_START_MINUTES": 1320,
    "ETA_QUIET_END_MINUTES": 480,
    # R5, R13: the weekly fit
    "ETA_FIT_WINDOW_DAYS": 28,
    "ETA_FIT_MIN_LEGS_PER_BIN": 30,
    "ETA_FIT_MIN_DEPOT_PAIRS": 15,
    "ETA_FIT_MAX_KMH": 60,
    "ETA_FIT_MIN_PICKUP_TO_DELIVERED_MINUTES": 2,
    # The accuracy report and the gate
    "ETA_GATE_KEPT_SHARE": 0.90,
    "ETA_REPORT_DEFAULT_DAYS": 14,
    # R11: windows
    "ETA_WINDOW_MIN_PROMISES": 30,
    "ETA_WINDOW_ROUND_MINUTES": 30,
    "ETA_WINDOW_BY_ONLY_HOURS": 4,
}


class TestTheTunables:
    def test_the_phase_two_tunables_default_to_the_plan_values(self, app):
        """Defined once, in business_config, and only derived by the app config. No fixture sets them: it would set
        the very keys this test reads."""
        for name, value in PLAN_TUNABLES.items():
            default = getattr(business_config, name)
            assert (default, type(default)) == (value, type(value)), name
            assert app.config[name] == default, name

        # The kill switch (R15) gates the detector, the beat run and the weekly fit. It ships on; a test app must not
        # publish a recompute from every commit it makes, so TestingConfig switches it off.
        shipped = business_config.ETA_SHADOW_ENGINE_ENABLED
        assert (shipped, type(shipped)) == (True, bool)
        assert testing_config.TestingConfig.ETA_SHADOW_ENGINE_ENABLED is False
        assert app.config["ETA_SHADOW_ENGINE_ENABLED"] is False

    def test_the_route_optimisers_keys_are_reused_not_defined_again(self, app):
        """Two definitions of one tunable is two places deciding one thing (R1 and R4 read these)."""
        for name in ("COMMITTED_STOP_MAX_AGE_HOURS", "DRIVER_LOCATION_FRESH_SECONDS"):
            assert not hasattr(business_config, name), name
            assert name in app.config, name


class TestTheStructuredSeeds:
    def test_the_seeds_are_the_plan_values(self):
        assert business_config.ETA_CYCLE_SEED_POINTS == (
            (0.1, 13.0),
            (0.8, 16.0),
            (2.3, 24.0),
            (4.6, 32.0),
            (7.7, 44.0),
            (13.2, 67.0),
        )
        assert business_config.ETA_CYCLE_BIN_EDGES_KM == (0.2, 1.0, 3.0, 6.0, 10.0)
        assert business_config.ETA_DEPOT_LEG_SEED == (10.0, 1.5)
        assert business_config.ETA_WINDOW_SEEDS == {
            "0": (-20, 45),
            "1-2": (-40, 90),
            "3-4": (-60, 120),
            "5-7": (-75, 150),
            "8+": (-90, 180),
        }

    def test_each_seed_point_sits_in_its_own_fit_bin(self):
        """R5: a fitted bin's (median km, median minutes) replaces the point of the bin, so the seed has one per bin.
        The binning is the weekly fit's own `bin_index`, not a second derivation of it."""
        bins = [bin_index(km) for km, _ in business_config.ETA_CYCLE_SEED_POINTS]

        assert bins == list(range(len(business_config.ETA_CYCLE_BIN_EDGES_KM) + 1))

    def test_the_seeds_grow_with_the_distance_and_with_the_queue_ahead(self):
        points = business_config.ETA_CYCLE_SEED_POINTS
        assert all(a[0] < b[0] and a[1] < b[1] for a, b in zip(points, points[1:]))
        # R11: hi never falls as the band grows; a window opens before the expected time and closes after it.
        bands = list(business_config.ETA_WINDOW_SEEDS.values())
        assert all(lo < 0 < hi for lo, hi in bands)
        assert all(a[1] < b[1] for a, b in zip(bands, bands[1:]))


class TestTheKeysAndTheRowVocabulary:
    def test_the_recompute_keys_are_registered_with_their_tiers(self):
        """The key format is a production contract (redis_keyspace rule 2), and the tier says how a Redis outage is
        met: the debounce marker is a cache, the run lock is a reliability key."""
        assert RedisKeyspace.eta_recompute_scheduled() == "bs:eta_recompute:scheduled"
        assert RedisKeyspace.eta_recompute_lock() == "bs:eta_recompute:lock"
        assert KEYSPACE_TIERS["eta_recompute_scheduled"] is RedisUsageTier.TIER_CACHE
        assert KEYSPACE_TIERS["eta_recompute_lock"] is RedisUsageTier.TIER_RELIABILITY

    def test_the_engines_hidden_rows_have_a_stage_and_a_model_of_their_own(self):
        assert ETA_STAGE_WAITING == "waiting"
        assert ETA_STAGE_WAITING in PROMISE_STAGES  # the stage CHECK of `delivery_promises` accepts it
        assert MODEL_ENGINE_S1 == "engine_s1_v1"
        assert MODEL_ENGINE_S1 not in (MODEL_GOOGLE_TAP, MODEL_OSRM_SHADOW)  # the Phase 1 readers never see it
        assert len(MODEL_ENGINE_S1) <= DeliveryPromise.__table__.c.model_version.type.length
