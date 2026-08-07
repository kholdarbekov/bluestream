"""Dispatch read model.

Two things this must never do quietly: drop `preparing` orders (they would
vanish from the map mid-pipeline and reappear later), and hide orders whose
address has no coordinates (they are exactly the ones that get forgotten).
"""

from datetime import date, datetime, timedelta, timezone

import pytest

from business_app.models.delivery import Delivery
from business_app.models.user import UserAddress
from business_app.services.dispatch_service import DispatchService
from shared.enums import DeliveryStatus, OrderStatus


def make_order(db, sample_user, sample_order, *, status, delivery_date, lat=41.31, lng=69.25):
    address = UserAddress(
        user_id=sample_user.id,
        full_address="Chilonzor 12",
        city="Tashkent",
        latitude=lat,
        longitude=lng,
    )
    db.session.add(address)
    db.session.flush()
    order = sample_order.__class__(
        user_id=sample_user.id,
        order_number=f"ORD-{status.value}-{delivery_date.isoformat()}-{lat}",
        total_amount=sample_order.total_amount,
        status=status,
        payment_method=sample_order.payment_method,
        delivery_address_id=address.id,
        delivery_date=delivery_date,
    )
    db.session.add(order)
    db.session.flush()
    return order


class TestOrderLayer:
    @pytest.mark.parametrize(
        "status",
        [OrderStatus.PENDING, OrderStatus.CONFIRMED, OrderStatus.PREPARING, OrderStatus.OUT_FOR_DELIVERY],
    )
    def test_includes_every_active_status(self, db, sample_user, sample_order, status):
        make_order(db, sample_user, sample_order, status=status, delivery_date=datetime.now(timezone.utc))
        db.session.commit()

        snapshot = DispatchService.get_snapshot(date.today())
        assert status.value in {o["status"] for o in snapshot["orders"]}

    @pytest.mark.parametrize("status", [OrderStatus.DELIVERED, OrderStatus.CANCELLED, OrderStatus.RETURNED])
    def test_excludes_finished_statuses(self, db, sample_user, sample_order, status):
        make_order(db, sample_user, sample_order, status=status, delivery_date=datetime.now(timezone.utc))
        db.session.commit()

        snapshot = DispatchService.get_snapshot(date.today())
        assert status.value not in {o["status"] for o in snapshot["orders"]}

    def test_yesterdays_unfinished_order_is_included_and_flagged_overdue(
        self, db, sample_user, sample_order
    ):
        make_order(
            db,
            sample_user,
            sample_order,
            status=OrderStatus.CONFIRMED,
            delivery_date=datetime.now(timezone.utc) - timedelta(days=1),
        )
        db.session.commit()

        orders = DispatchService.get_snapshot(date.today())["orders"]
        assert len(orders) == 1
        assert orders[0]["is_overdue"] is True

    def test_tomorrows_order_is_excluded_from_todays_snapshot(self, db, sample_user, sample_order):
        make_order(
            db,
            sample_user,
            sample_order,
            status=OrderStatus.CONFIRMED,
            delivery_date=datetime.now(timezone.utc) + timedelta(days=1),
        )
        db.session.commit()

        assert DispatchService.get_snapshot(date.today())["orders"] == []

    def test_customer_name_comes_from_the_order_owner(self, db, sample_user, sample_order):
        """orders has two FKs to users; an unpinned join(User) picks the wrong one."""
        make_order(db, sample_user, sample_order, status=OrderStatus.CONFIRMED,
                   delivery_date=datetime.now(timezone.utc))
        db.session.commit()

        orders = DispatchService.get_snapshot(date.today())["orders"]
        assert orders[0]["customer_name"] == sample_user.full_name
        assert orders[0]["user_id"] == sample_user.id


class TestUnmapped:
    def test_ungeocoded_order_is_reported_not_dropped(self, db, sample_user, sample_order):
        order = make_order(db, sample_user, sample_order, status=OrderStatus.CONFIRMED,
                           delivery_date=datetime.now(timezone.utc))
        order.delivery_address.latitude = None
        order.delivery_address.longitude = None
        db.session.commit()

        snapshot = DispatchService.get_snapshot(date.today())
        assert snapshot["orders"] == []
        # Filtered by order_number rather than asserting `unmapped` is exactly
        # one item: the `sample_order` fixture itself is an active order with
        # no `delivery_date` and no address, so — correctly, per the
        # `no_delivery_date` fix below — it now ALSO appears in `unmapped`
        # alongside the order under test. Asserting the whole list would make
        # this test depend on the fixture's shape rather than on the behavior
        # being tested here (a set-but-uncoordinatable address).
        unmapped_entries = [u for u in snapshot["unmapped"] if u["order_number"] == order.order_number]
        assert len(unmapped_entries) == 1
        assert unmapped_entries[0]["reason"] == "no_coordinates"

    def test_active_order_without_delivery_date_is_reported_not_dropped(self, db, sample_user, sample_order):
        """`Order.delivery_date` is nullable. SQL `NULL <= x` is falsy, so a naive
        `delivery_date <= end_of_day` filter drops an unscheduled active order
        entirely — it never reaches `orders` OR `unmapped`. It must surface in
        `unmapped` (reason `no_delivery_date`), not disappear."""
        order = make_order(db, sample_user, sample_order, status=OrderStatus.CONFIRMED,
                           delivery_date=datetime.now(timezone.utc))
        order.delivery_date = None
        db.session.commit()

        snapshot = DispatchService.get_snapshot(date.today())
        assert snapshot["orders"] == []
        unmapped_entries = [u for u in snapshot["unmapped"] if u["order_number"] == order.order_number]
        assert len(unmapped_entries) == 1
        assert unmapped_entries[0]["reason"] == "no_delivery_date"


class TestPoolAndRoutes:
    def test_unassigned_claimable_delivery_lands_in_pool(self, db, sample_user, sample_order):
        order = make_order(db, sample_user, sample_order, status=OrderStatus.CONFIRMED,
                           delivery_date=datetime.now(timezone.utc))
        delivery = Delivery(
            order_id=order.id,
            delivery_person_id=None,
            status=DeliveryStatus.SCHEDULED,
            scheduled_date=datetime.now(timezone.utc),
            scheduled_time_slot="09:00-12:00",
        )
        db.session.add(delivery)
        db.session.commit()

        snapshot = DispatchService.get_snapshot(date.today())
        assert [p["delivery_id"] for p in snapshot["pool"]] == [delivery.id]

    def test_unassigned_delivery_in_non_pool_status_is_excluded_from_pool(self, db, sample_user, sample_order):
        """`pool` must be an ALLOWLIST of `DELIVERY_POOL_UNASSIGNED_STATES`
        (SCHEDULED/PENDING) — the same set `staff_service.get_delivery_pool` and
        `assert_unassigned_for_pool_status` use — not a terminal-status
        blocklist. A blocklist admits anything nobody thought to list: an
        ASSIGNED delivery that somehow lost its driver is neither terminal nor
        pool-eligible, but a blocklist would still show it as claimable. This is
        exactly the case a blocklist lets through and an allowlist does not.
        """
        order = make_order(db, sample_user, sample_order, status=OrderStatus.CONFIRMED,
                           delivery_date=datetime.now(timezone.utc))
        delivery = Delivery(
            order_id=order.id,
            delivery_person_id=None,
            status=DeliveryStatus.ASSIGNED,
            scheduled_date=datetime.now(timezone.utc),
            scheduled_time_slot="09:00-12:00",
        )
        db.session.add(delivery)
        db.session.commit()

        snapshot = DispatchService.get_snapshot(date.today())
        assert snapshot["pool"] == []

    def test_route_stops_are_returned_in_optimized_order(
        self, db, sample_user, sample_order, delivery_driver
    ):
        from business_app.models.delivery import DeliveryRoute

        ids = []
        for i in range(2):
            order = make_order(db, sample_user, sample_order, status=OrderStatus.OUT_FOR_DELIVERY,
                               delivery_date=datetime.now(timezone.utc), lat=41.31 + i / 100)
            delivery = Delivery(
                order_id=order.id,
                delivery_person_id=delivery_driver.id,
                status=DeliveryStatus.ASSIGNED,
                scheduled_date=datetime.now(timezone.utc),
                scheduled_time_slot="09:00-12:00",
            )
            db.session.add(delivery)
            db.session.flush()
            ids.append(delivery.id)

        db.session.add(
            DeliveryRoute(
                name="r",
                delivery_person_id=delivery_driver.id,
                start_location_lat=41.30,
                start_location_lng=69.24,
                route_date=datetime.now(timezone.utc),
                optimized_order=[ids[1], ids[0]],
                manual_override=True,
                pinned_stops={str(ids[1]): 0},
            )
        )
        db.session.commit()

        routes = DispatchService.get_snapshot(date.today())["routes"]
        assert len(routes) == 1
        assert [s["delivery_id"] for s in routes[0]["stops"]] == [ids[1], ids[0]]
        assert [s["position"] for s in routes[0]["stops"]] == [0, 1]
        assert routes[0]["stops"][0]["pinned"] is True
        assert routes[0]["manual_override"] is True

    def test_route_surfaces_metrics_stale_when_route_edit_service_set_it(
        self, db, sample_user, sample_order, delivery_driver
    ):
        """`RouteEditService` sets `extra_data["metrics_stale"]` on save/move/
        pool (see route_edit_service.py) specifically so the admin panel can
        stop showing a distance/duration that describes a route that no
        longer matches the current stops. That is only true if the read side
        (this method) actually forwards the flag — it must not get lost
        between the write side setting it and the API response.
        """
        from business_app.models.delivery import DeliveryRoute

        order = make_order(db, sample_user, sample_order, status=OrderStatus.OUT_FOR_DELIVERY,
                            delivery_date=datetime.now(timezone.utc))
        delivery = Delivery(
            order_id=order.id,
            delivery_person_id=delivery_driver.id,
            status=DeliveryStatus.ASSIGNED,
            scheduled_date=datetime.now(timezone.utc),
            scheduled_time_slot="09:00-12:00",
        )
        db.session.add(delivery)
        db.session.flush()

        db.session.add(
            DeliveryRoute(
                name="r",
                delivery_person_id=delivery_driver.id,
                start_location_lat=41.30,
                start_location_lng=69.24,
                route_date=datetime.now(timezone.utc),
                optimized_order=[delivery.id],
                extra_data={"metrics_stale": True},
            )
        )
        db.session.commit()

        routes = DispatchService.get_snapshot(date.today())["routes"]
        assert len(routes) == 1
        assert routes[0]["metrics_stale"] is True

    def test_route_metrics_stale_defaults_to_false_when_never_set(
        self, db, sample_user, sample_order, delivery_driver
    ):
        """A freshly optimised route (never hand-edited, never had a stop
        moved on/off it) has no `extra_data` opinion on staleness at all —
        that must read as "fresh" (`False`), not `None`/missing, since the
        frontend renders on this being a clean boolean.
        """
        from business_app.models.delivery import DeliveryRoute

        order = make_order(db, sample_user, sample_order, status=OrderStatus.OUT_FOR_DELIVERY,
                            delivery_date=datetime.now(timezone.utc))
        delivery = Delivery(
            order_id=order.id,
            delivery_person_id=delivery_driver.id,
            status=DeliveryStatus.ASSIGNED,
            scheduled_date=datetime.now(timezone.utc),
            scheduled_time_slot="09:00-12:00",
        )
        db.session.add(delivery)
        db.session.flush()

        db.session.add(
            DeliveryRoute(
                name="r",
                delivery_person_id=delivery_driver.id,
                start_location_lat=41.30,
                start_location_lng=69.24,
                route_date=datetime.now(timezone.utc),
                optimized_order=[delivery.id],
            )
        )
        db.session.commit()

        routes = DispatchService.get_snapshot(date.today())["routes"]
        assert len(routes) == 1
        assert routes[0]["metrics_stale"] is False


class TestRouteWindowMatchesWriteSide:
    def test_route_in_utc_tashkent_divergence_window_is_visible_to_reader_and_writer(
        self, db, sample_user, sample_order, delivery_driver, monkeypatch
    ):
        """`_routes()` must key "today's route" off the same UTC-midnight boundary
        RouteOptimizationService.current_route()/_upsert_route() (the write side a
        later task edits routes through) use for `route_date` — NOT the
        Tashkent-local boundary the order layer correctly uses for
        `delivery_date`. A route stamped inside the Tashkent-local 00:00-05:00
        window is on the UTC side of that UTC day's midnight but already on the
        *next* Tashkent day; if reader and writer used different boundaries, the
        map could hide a route the route editor still finds, or show one it can
        no longer find.

        Real wall-clock time only sits in that 5-hour window for 5 of 24 hours a
        day, so this freezes `datetime.now()` in both service modules rather than
        relying on when the suite happens to run.
        """
        from business_app.models.delivery import DeliveryRoute
        from business_app.services import dispatch_service as dispatch_module
        from business_app.services import route_optimization_service as route_opt_module
        from business_app.services.route_optimization_service import RouteOptimizationService

        # 2026-01-05 20:00 UTC = 2026-01-06 01:00 Tashkent (UTC+5): squarely inside
        # the Tashkent-local 00:00-05:00 divergence window. UTC's calendar day is
        # still Jan 5; Tashkent's has already rolled over to Jan 6.
        frozen_utc = datetime(2026, 1, 5, 20, 0, tzinfo=timezone.utc)

        class _FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return frozen_utc.astimezone(tz) if tz else frozen_utc.replace(tzinfo=None)

        monkeypatch.setattr(dispatch_module, "datetime", _FrozenDatetime)
        monkeypatch.setattr(route_opt_module, "datetime", _FrozenDatetime)

        order = make_order(db, sample_user, sample_order, status=OrderStatus.OUT_FOR_DELIVERY,
                           delivery_date=frozen_utc)
        delivery = Delivery(
            order_id=order.id,
            delivery_person_id=delivery_driver.id,
            status=DeliveryStatus.ASSIGNED,
            scheduled_date=frozen_utc,
            scheduled_time_slot="09:00-12:00",
        )
        db.session.add(delivery)
        db.session.flush()

        # 2026-01-05 10:00 UTC is >= UTC midnight for Jan 5 (00:00 UTC) — the
        # bound the write side uses — but < Tashkent midnight for Jan 6
        # (2026-01-05 19:00 UTC) — the bound a Tashkent-local `_routes()` would
        # wrongly use. Only visible to both reader and writer once they share
        # the UTC bound.
        route_date = datetime(2026, 1, 5, 10, 0, tzinfo=timezone.utc)
        db.session.add(
            DeliveryRoute(
                name="r",
                delivery_person_id=delivery_driver.id,
                start_location_lat=41.30,
                start_location_lng=69.24,
                route_date=route_date,
                optimized_order=[delivery.id],
            )
        )
        db.session.commit()

        # Writer side: RouteOptimizationService.current_route() must find it.
        found = RouteOptimizationService().current_route(delivery_driver.id)
        assert found is not None
        assert found.route_date.replace(tzinfo=None) == route_date.replace(tzinfo=None)

        # Reader side: DispatchService._routes() must find the SAME row.
        routes = DispatchService.get_snapshot(date(2026, 1, 6))["routes"]
        assert [r["driver_id"] for r in routes] == [delivery_driver.id]
        assert [s["delivery_id"] for s in routes[0]["stops"]] == [delivery.id]


class TestQueryBudget:
    def test_snapshot_is_not_n_plus_one(self, db, sample_user, sample_order, count_queries):
        for i in range(12):
            make_order(db, sample_user, sample_order, status=OrderStatus.CONFIRMED,
                       delivery_date=datetime.now(timezone.utc), lat=41.31 + i / 1000)

        # Several ACTIVE drivers with real location data, so `_drivers()`'s
        # per-driver loop (and its location-freshness lookup) is actually
        # exercised — a previous version of this test created zero drivers,
        # so a per-driver query in that loop went undetected by the "<=12"
        # bound below. Query count must stay O(1) in driver count: a
        # per-driver query re-appearing here (6 drivers) would blow well past
        # the tightened budget.
        from business_app.models.delivery import DeliveryPerson
        from business_app.models.user import User
        from business_app.utils.password_security import hash_password
        from shared.enums import UserRole, UserType

        for i in range(6):
            driver_user = User(
                email=f"qbudget-driver{i}@example.com",
                phone=f"+998900000{i:03d}",
                password_hash=hash_password("DriverPassword123!"),
                first_name="Driver",
                last_name=str(i),
                user_type=UserType.STAFF,
                role=UserRole.DELIVERY_DRIVER,
                is_verified=True,
            )
            db.session.add(driver_user)
            db.session.flush()
            db.session.add(
                DeliveryPerson(
                    user_id=driver_user.id,
                    full_name=f"Driver {i}",
                    phone=f"+998900000{i:03d}",
                    is_active=True,
                    is_available=True,
                    current_location_lat=41.31,
                    current_location_lng=69.25,
                    last_location_update=datetime.now(timezone.utc),
                )
            )
        db.session.commit()

        with count_queries() as counter:
            DispatchService.get_snapshot(date.today())

        # Fixed cost regardless of order/driver count: one query for active
        # orders, one for active drivers (single joinedload, no per-driver
        # follow-up), one for today's routes (no DeliveryRoute rows exist
        # here, so no follow-up delivery lookup either) = 3. A per-driver
        # query in `_drivers()` would push this to 3 + 6 = 9 with the drivers
        # created above — well past this tightened bound.
        assert counter.count <= 6
