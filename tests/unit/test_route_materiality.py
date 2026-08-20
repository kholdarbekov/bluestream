"""Materiality (spec §5.1) is computed by the optimizer and PERSISTED to
DeliveryRoute.extra_data['materiality'] — the single representation every
consumer (task gate, webhook, Plan 3 card) reads. Nothing re-derives it."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from business_app.models.delivery import Delivery, DeliveryPerson, DeliveryRoute
from business_app.models.order import Order
from business_app.models.user import User, UserAddress
from business_app.services.route_optimization_service import (
    DRIVER_INITIATED_TRIGGERS,
    RouteOptimizationService,
)
from shared.enums import DeliveryStatus, OrderStatus, UserRole, UserType


@pytest.fixture
def driver(db):
    user = User(
        email="mat-driver@example.com",
        phone="+998900000051",
        password_hash="x",
        first_name="Mat",
        last_name="Driver",
        user_type=UserType.STAFF,
        role=UserRole.DELIVERY_DRIVER,
        is_verified=True,
    )
    db.session.add(user)
    db.session.commit()
    person = DeliveryPerson(
        user_id=user.id,
        full_name="Mat Driver",
        phone="+998900000051",
        current_location_lat=41.3000,
        current_location_lng=69.2500,
        last_location_update=datetime.now(UTC),
        is_active=True,
        is_available=True,
    )
    db.session.add(person)
    db.session.commit()
    return user


@pytest.fixture
def customer(db):
    user = User(
        email="mat-cust@example.com",
        phone="+998900000052",
        password_hash="x",
        first_name="C",
        last_name="",
        user_type=UserType.INDIVIDUAL,
        role=UserRole.CUSTOMER,
        is_verified=True,
    )
    db.session.add(user)
    db.session.commit()
    return user


def _make_delivery(db, customer_id, driver_id, order_no, lat, lng, status=DeliveryStatus.ASSIGNED):
    addr = UserAddress(
        user_id=customer_id,
        title="Stop",
        full_address=f"Stop {order_no}",
        street_address=f"Stop {order_no}",
        latitude=lat,
        longitude=lng,
    )
    db.session.add(addr)
    db.session.flush()
    order = Order(
        user_id=customer_id,
        order_number=order_no,
        status=OrderStatus.CONFIRMED,
        subtotal=Decimal("10000"),
        total_amount=Decimal("10000"),
        delivery_address_id=addr.id,
        delivery_date=datetime.now(UTC) + timedelta(hours=2),
    )
    db.session.add(order)
    db.session.flush()
    delivery = Delivery(
        order_id=order.id,
        delivery_person_id=driver_id,
        status=status,
        scheduled_date=datetime.now(UTC),
        scheduled_time_slot="09:00-12:00",
    )
    db.session.add(delivery)
    db.session.commit()
    return delivery


def _haversine_matrix(self, points, traffic=True, use_cache=True):
    from business_app.utils.helpers import calculate_distance

    matrix = {}
    for i, pi in enumerate(points):
        for j, pj in enumerate(points):
            if i == j:
                matrix[(i, j)] = {"distance_km": 0.0, "duration_minutes": 0.0}
            else:
                km = calculate_distance(pi[0], pi[1], pj[0], pj[1])
                matrix[(i, j)] = {"distance_km": km, "duration_minutes": km * 2.4}
    return matrix, "haversine"


@pytest.mark.unit
@pytest.mark.delivery
class TestComputeMateriality:
    """Pure-rule tests against the public helper (imported, never copied)."""

    def test_first_solve_head_appears(self, app, db, driver, customer):
        d1 = _make_delivery(db, customer.id, driver.id, "ORD-M-1", 41.30, 69.26)
        with app.app_context():
            m = RouteOptimizationService().compute_materiality(
                prev_order=[], new_order=[d1.id], deliveries=[d1], trigger="accept"
            )
        assert m["head_changed"] is True
        assert m["set_changed"] is True
        assert m["sequence_changed"] is True
        assert m["driver_initiated"] is True  # accept is a driver action

    def test_mid_tail_insertion_head_unchanged(self, app, db, driver, customer):
        d1 = _make_delivery(db, customer.id, driver.id, "ORD-M-2", 41.30, 69.26)
        d2 = _make_delivery(db, customer.id, driver.id, "ORD-M-3", 41.30, 69.29)
        d3 = _make_delivery(db, customer.id, driver.id, "ORD-M-4", 41.30, 69.33)
        with app.app_context():
            m = RouteOptimizationService().compute_materiality(
                prev_order=[d1.id, d3.id],
                new_order=[d1.id, d2.id, d3.id],
                deliveries=[d1, d2, d3],
                trigger="auto",
            )
        assert m["head_changed"] is False
        assert m["set_changed"] is True
        assert m["sequence_changed"] is True
        assert m["driver_initiated"] is False

    def test_started_stops_are_skipped_when_finding_the_head(
        self, app, db, driver, customer
    ):
        """The committed (IN_TRANSIT) stop sits at position 0 in both orders;
        the head is the first UNSTARTED stop after it."""
        c = _make_delivery(db, customer.id, driver.id, "ORD-M-5", 41.30, 69.26, DeliveryStatus.IN_TRANSIT)
        d2 = _make_delivery(db, customer.id, driver.id, "ORD-M-6", 41.30, 69.29)
        d3 = _make_delivery(db, customer.id, driver.id, "ORD-M-7", 41.30, 69.33)
        with app.app_context():
            m = RouteOptimizationService().compute_materiality(
                prev_order=[c.id, d2.id, d3.id],
                new_order=[c.id, d3.id, d2.id],
                deliveries=[c, d2, d3],
                trigger="auto",
            )
        assert m["head_changed"] is True   # first unstarted: d2 -> d3
        assert m["set_changed"] is False
        assert m["sequence_changed"] is True

    def test_delivered_stop_leaving_is_a_set_change_but_not_a_head_change(
        self, app, db, driver, customer
    ):
        d2 = _make_delivery(db, customer.id, driver.id, "ORD-M-8", 41.30, 69.29)
        d3 = _make_delivery(db, customer.id, driver.id, "ORD-M-9", 41.30, 69.33)
        gone_id = 999_999  # completed delivery, no longer in the active set
        with app.app_context():
            m = RouteOptimizationService().compute_materiality(
                prev_order=[gone_id, d2.id, d3.id],
                new_order=[d2.id, d3.id],
                deliveries=[d2, d3],
                trigger="delivery",
            )
        assert m["set_changed"] is True
        assert m["head_changed"] is False  # first unstarted was d2, still d2
        assert m["driver_initiated"] is True

    def test_set_grew_head_unchanged_when_new_stop_lands_in_tail(
        self, app, db, driver, customer
    ):
        """A newly-pooled stop slots into the tail: the set changed, but the
        driver's next unstarted stop is untouched."""
        d1 = _make_delivery(db, customer.id, driver.id, "ORD-M-10", 41.30, 69.26)
        d2 = _make_delivery(db, customer.id, driver.id, "ORD-M-11", 41.30, 69.33)
        with app.app_context():
            m = RouteOptimizationService().compute_materiality(
                prev_order=[d1.id],
                new_order=[d1.id, d2.id],
                deliveries=[d1, d2],
                trigger="auto",
            )
        assert m["head_changed"] is False
        assert m["set_changed"] is True
        assert m["sequence_changed"] is True
        assert m["driver_initiated"] is False

    def test_pure_resequence_head_unchanged_tail_reordered(
        self, app, db, driver, customer
    ):
        """Same set, same head, tail order flips — sequence changed, but
        nothing material for the driver's next stop."""
        d1 = _make_delivery(db, customer.id, driver.id, "ORD-M-12", 41.30, 69.26)
        d2 = _make_delivery(db, customer.id, driver.id, "ORD-M-13", 41.30, 69.29)
        d3 = _make_delivery(db, customer.id, driver.id, "ORD-M-14", 41.30, 69.33)
        with app.app_context():
            m = RouteOptimizationService().compute_materiality(
                prev_order=[d1.id, d2.id, d3.id],
                new_order=[d1.id, d3.id, d2.id],
                deliveries=[d1, d2, d3],
                trigger="auto",
            )
        assert m["head_changed"] is False
        assert m["set_changed"] is False
        assert m["sequence_changed"] is True

    def test_previous_route_empty_is_first_solve_shape(self, app, db, driver, customer):
        """`prev_order=[]` (no previous route row at all) behaves exactly
        like the documented first-solve case."""
        d1 = _make_delivery(db, customer.id, driver.id, "ORD-M-15", 41.30, 69.26)
        with app.app_context():
            m = RouteOptimizationService().compute_materiality(
                prev_order=[], new_order=[d1.id], deliveries=[d1], trigger="auto"
            )
        assert m["head_changed"] is True
        assert m["set_changed"] is True
        assert m["sequence_changed"] is True

    def test_driver_initiated_trigger_set_is_exact(self):
        assert DRIVER_INITIATED_TRIGGERS == frozenset(
            {"arrival", "delivery", "location_update", "manual", "accept", "picked_up", "in_transit"}
        )


@pytest.mark.unit
@pytest.mark.delivery
class TestMaterialityPersistence:
    def test_optimize_persists_materiality_on_normal_solve(
        self, app, db, driver, customer, monkeypatch
    ):
        d1 = _make_delivery(db, customer.id, driver.id, "ORD-MP-1", 41.30, 69.26)
        monkeypatch.setattr(
            "business_app.services.maps_service.MapsService.get_distance_matrix",
            _haversine_matrix,
        )
        with app.app_context():
            route = RouteOptimizationService().optimize_for_driver(driver.id, trigger="accept")
            m = (route.extra_data or {}).get("materiality")
            assert m is not None
            assert m["head_changed"] is True
            assert m["set_changed"] is True
            assert m["sequence_changed"] is True
            assert m["driver_initiated"] is True
            assert m["trigger"] == "accept"
            assert "computed_at" in m

    def test_second_identical_solve_is_immaterial(
        self, app, db, driver, customer, monkeypatch
    ):
        _make_delivery(db, customer.id, driver.id, "ORD-MP-2", 41.30, 69.26)
        monkeypatch.setattr(
            "business_app.services.maps_service.MapsService.get_distance_matrix",
            _haversine_matrix,
        )
        with app.app_context():
            svc = RouteOptimizationService()
            svc.optimize_for_driver(driver.id, trigger="accept")
            route = svc.optimize_for_driver(driver.id, trigger="auto")
            m = (route.extra_data or {}).get("materiality")
            assert m["head_changed"] is False
            assert m["set_changed"] is False
            assert m["sequence_changed"] is False
            assert m["driver_initiated"] is False

    def test_override_skip_path_stamps_fresh_materiality(
        self, app, db, driver, customer, monkeypatch
    ):
        """The manual-override 'set unchanged -> skip' early return must not
        leave the PREVIOUS run's verdict in extra_data — the task gate would
        replay a stale sound decision."""
        d1 = _make_delivery(db, customer.id, driver.id, "ORD-MP-3", 41.30, 69.26)
        d2 = _make_delivery(db, customer.id, driver.id, "ORD-MP-4", 41.30, 69.33)
        monkeypatch.setattr(
            "business_app.services.maps_service.MapsService.get_distance_matrix",
            _haversine_matrix,
        )
        with app.app_context():
            svc = RouteOptimizationService()
            first = svc.optimize_for_driver(driver.id, trigger="auto")
            assert (first.extra_data or {})["materiality"]["head_changed"] is True
            # Admin locks the current sequence.
            first.manual_override = True
            first.pinned_stops = {str(d1.id): 0, str(d2.id): 1}
            db.session.commit()
            route = svc.optimize_for_driver(driver.id, trigger="auto")
            m = (route.extra_data or {}).get("materiality")
            assert m["head_changed"] is False
            assert m["set_changed"] is False
            assert m["sequence_changed"] is False

    def test_shrink_early_return_stamps_fresh_materiality(
        self, app, db, driver, customer, monkeypatch
    ):
        """The manual-override 'set only shrank -> drop departed stops' early
        return must also stamp a FRESH materiality verdict against the
        CURRENT prev/new pair — not carry over the value computed on the
        previous solve, which is exactly the class of bug the earlier
        `committed_delivery_id` staleness fix round closed."""
        d1 = _make_delivery(db, customer.id, driver.id, "ORD-MP-5", 41.30, 69.26)
        d2 = _make_delivery(db, customer.id, driver.id, "ORD-MP-6", 41.30, 69.33)
        monkeypatch.setattr(
            "business_app.services.maps_service.MapsService.get_distance_matrix",
            _haversine_matrix,
        )
        with app.app_context():
            svc = RouteOptimizationService()
            first = svc.optimize_for_driver(driver.id, trigger="auto")
            assert first.optimized_order == [d1.id, d2.id]
            # Admin locks the current sequence.
            first.manual_override = True
            first.pinned_stops = {str(d1.id): 0, str(d2.id): 1}
            db.session.commit()
            # d1 completes and drops out of the active set (the "shrank" case).
            d1_row = Delivery.query.get(d1.id)
            d1_row.status = DeliveryStatus.DELIVERED
            db.session.commit()
            route = svc.optimize_for_driver(driver.id, trigger="delivery")
            assert route.optimized_order == [d2.id]
            m = (route.extra_data or {}).get("materiality")
            assert m["set_changed"] is True  # d1 left the route
            assert m["head_changed"] is False  # first unstarted stays d2
            assert m["driver_initiated"] is True
            assert m["trigger"] == "delivery"
