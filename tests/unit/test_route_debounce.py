"""Debounce (spec §4.5): trigger='location_update' is coalesced per driver —
skip when the last solve is younger than ROUTE_OPTIMIZE_DEBOUNCE_SECONDS OR
the driver moved less than ROUTE_OPTIMIZE_MIN_MOVE_METERS since it. Other
triggers are NEVER debounced (they move the anchor)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from business_app.models.delivery import Delivery, DeliveryPerson, DeliveryRoute
from business_app.models.order import Order
from business_app.models.user import User, UserAddress
from business_app.services.route_optimization_service import RouteOptimizationService
from shared.enums import DeliveryStatus, OrderStatus, UserRole, UserType


@pytest.fixture
def driver(db):
    user = User(
        email="deb-driver@example.com",
        phone="+998900000111",
        password_hash="x",
        first_name="Deb",
        last_name="Driver",
        user_type=UserType.STAFF,
        role=UserRole.DELIVERY_DRIVER,
        is_verified=True,
    )
    db.session.add(user)
    db.session.commit()
    person = DeliveryPerson(
        user_id=user.id,
        full_name="Deb Driver",
        phone="+998900000111",
        current_location_lat=41.3000,
        current_location_lng=69.2500,
        last_location_update=datetime.now(UTC),
        is_active=True,
        is_available=True,
    )
    db.session.add(person)
    db.session.commit()
    return user, person


@pytest.fixture
def customer(db):
    user = User(
        email="deb-cust@example.com",
        phone="+998900000112",
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


def _make_delivery(db, customer_id, driver_id, order_no, lat, lng):
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
        delivery_time_slot="09:00-12:00",
    )
    db.session.add(order)
    db.session.flush()
    delivery = Delivery(
        order_id=order.id,
        delivery_person_id=driver_id,
        status=DeliveryStatus.ASSIGNED,
        scheduled_date=datetime.now(UTC),
        scheduled_time_slot="09:00-12:00",
    )
    db.session.add(delivery)
    db.session.commit()
    return delivery


def _haversine_matrix_counting(calls):
    from business_app.utils.helpers import calculate_distance

    def fake(self, points, traffic=True, use_cache=True):
        calls.append(list(points))
        matrix = {}
        for i, pi in enumerate(points):
            for j, pj in enumerate(points):
                km = 0.0 if i == j else calculate_distance(pi[0], pi[1], pj[0], pj[1])
                matrix[(i, j)] = {"distance_km": km, "duration_minutes": km * 2.4}
        return matrix, "haversine"

    return fake


def _age_last_solve(db, driver_id, *, seconds):
    """Backdate extra_data['last_optimized_at'] so the debounce window lapses."""
    route = (
        DeliveryRoute.query.filter_by(delivery_person_id=driver_id)
        .order_by(DeliveryRoute.created_at.desc())
        .first()
    )
    extra = dict(route.extra_data or {})
    extra["last_optimized_at"] = (
        datetime.now(UTC) - timedelta(seconds=seconds)
    ).isoformat()
    route.extra_data = extra
    db.session.commit()


@pytest.mark.unit
@pytest.mark.delivery
class TestLocationDebounce:
    def test_location_update_within_window_is_skipped(self, app, db, driver, customer, monkeypatch):
        user, _person = driver
        _make_delivery(db, customer.id, user.id, "ORD-D-1", 41.31, 69.27)
        calls = []
        monkeypatch.setattr(
            "business_app.services.maps_service.MapsService.get_distance_matrix",
            _haversine_matrix_counting(calls),
        )
        with app.app_context():
            svc = RouteOptimizationService()
            assert svc.optimize_for_driver(user.id, trigger="location_update") is not None
            first_calls = len(calls)
            # Seconds later, another share from (almost) the same spot.
            assert svc.optimize_for_driver(user.id, trigger="location_update") is None
        assert len(calls) == first_calls  # no second matrix call

    def test_big_move_after_window_reoptimizes(self, app, db, driver, customer, monkeypatch):
        user, person = driver
        _make_delivery(db, customer.id, user.id, "ORD-D-2", 41.31, 69.27)
        calls = []
        monkeypatch.setattr(
            "business_app.services.maps_service.MapsService.get_distance_matrix",
            _haversine_matrix_counting(calls),
        )
        with app.app_context():
            svc = RouteOptimizationService()
            svc.optimize_for_driver(user.id, trigger="location_update")
            _age_last_solve(db, user.id, seconds=120)  # window (60s) lapsed
            # Re-fetch: `person` was created by the `driver` fixture under a
            # different (pre-context) session than this block's — Flask-
            # SQLAlchemy 3.1.1 scopes `db.session` to the app context, so
            # mutating the fixture object directly and committing silently
            # no-ops here. Mutate a freshly-queried instance instead.
            person = DeliveryPerson.query.filter_by(user_id=user.id).first()
            person.current_location_lat = 41.3200     # ~2.2 km move
            person.current_location_lng = 69.2500
            person.last_location_update = datetime.now(UTC)
            db.session.commit()
            assert svc.optimize_for_driver(user.id, trigger="location_update") is not None

    def test_tiny_move_after_window_is_still_skipped(self, app, db, driver, customer, monkeypatch):
        """Window lapsed but the driver moved ~30 m < ROUTE_OPTIMIZE_MIN_MOVE_METERS."""
        user, person = driver
        _make_delivery(db, customer.id, user.id, "ORD-D-3", 41.31, 69.27)
        calls = []
        monkeypatch.setattr(
            "business_app.services.maps_service.MapsService.get_distance_matrix",
            _haversine_matrix_counting(calls),
        )
        with app.app_context():
            svc = RouteOptimizationService()
            svc.optimize_for_driver(user.id, trigger="location_update")
            _age_last_solve(db, user.id, seconds=120)
            # Re-fetch for the same reason as the big-move test above.
            person = DeliveryPerson.query.filter_by(user_id=user.id).first()
            person.current_location_lat = 41.30027  # ~30 m north
            person.last_location_update = datetime.now(UTC)
            db.session.commit()
            n = len(calls)
            assert svc.optimize_for_driver(user.id, trigger="location_update") is None
        assert len(calls) == n

    def test_status_triggers_are_never_debounced(self, app, db, driver, customer, monkeypatch):
        user, _person = driver
        _make_delivery(db, customer.id, user.id, "ORD-D-4", 41.31, 69.27)
        calls = []
        monkeypatch.setattr(
            "business_app.services.maps_service.MapsService.get_distance_matrix",
            _haversine_matrix_counting(calls),
        )
        with app.app_context():
            svc = RouteOptimizationService()
            svc.optimize_for_driver(user.id, trigger="location_update")
            # Immediately after — an anchor-moving trigger must still solve.
            assert svc.optimize_for_driver(user.id, trigger="in_transit") is not None

    def test_permanently_ungeocodable_delivery_does_not_defeat_debounce(
        self, app, db, driver, customer, monkeypatch
    ):
        """Review fix 1: `prev_order_raw` (the persisted `optimized_order`)
        only ever contains deliveries that survived the geocode filter, but
        the debounce's set-equality check used to run BEFORE that filter,
        against the raw active set. A delivery whose address can never be
        geocoded therefore stays in the raw active set forever while never
        landing in `optimized_order` — the two sets would never match, so
        the debounce would be bypassed on every `location_update` for the
        driver's entire shift. The comparison must run post-filter."""
        user, _person = driver
        d_good = _make_delivery(db, customer.id, user.id, "ORD-D-6a", 41.31, 69.27)
        d_bad = _make_delivery(db, customer.id, user.id, "ORD-D-6b", None, None)
        calls = []
        monkeypatch.setattr(
            "business_app.services.maps_service.MapsService.get_distance_matrix",
            _haversine_matrix_counting(calls),
        )
        # Geocoding permanently fails for the bad address — it never resolves.
        monkeypatch.setattr(
            "business_app.services.maps_service.MapsService.geocode_address",
            lambda self, address, city="Tashkent": (_ for _ in ()).throw(
                RuntimeError("geocode disabled in test")
            ),
        )
        with app.app_context():
            svc = RouteOptimizationService()
            r1 = svc.optimize_for_driver(user.id, trigger="location_update")
            assert r1 is not None
            assert r1.optimized_order == [d_good.id]
            assert d_bad.id not in r1.optimized_order

            first_calls = len(calls)
            # Immediately after, same spot: must be debounced despite the
            # permanently-ungeocodable delivery still sitting in the raw
            # active set (it will never appear in `optimized_order`).
            r2 = svc.optimize_for_driver(user.id, trigger="location_update")
            assert r2 is None
        assert len(calls) == first_calls  # no second matrix call

    def test_new_stop_bypasses_debounce_even_within_window_and_without_moving(
        self, app, db, driver, customer, monkeypatch
    ):
        """Review fix 1: the debounce must never withhold a genuinely new
        delivery from `optimized_order`. Scenario: a `delivery` solve stamps
        `last_optimized_at`/`last_driver_location` at the doorstep, the
        driver accepts a new pool order seconds later, and shares location
        from the SAME spot — well inside the debounce window and under the
        move threshold. The set changed (new stop present), so the debounce
        must be bypassed and the new stop must land in `optimized_order`."""
        user, _person = driver
        d1 = _make_delivery(db, customer.id, user.id, "ORD-D-5a", 41.31, 69.27)
        calls = []
        monkeypatch.setattr(
            "business_app.services.maps_service.MapsService.get_distance_matrix",
            _haversine_matrix_counting(calls),
        )
        with app.app_context():
            svc = RouteOptimizationService()
            r1 = svc.optimize_for_driver(user.id, trigger="delivery")
            assert r1 is not None
            assert set(r1.optimized_order) == {d1.id}

            # A new order is accepted into the pool and assigned — the active
            # set now differs from what's published. No time has passed and
            # the driver has not moved.
            d2 = _make_delivery(db, customer.id, user.id, "ORD-D-5b", 41.315, 69.275)

            r2 = svc.optimize_for_driver(user.id, trigger="location_update")
            assert r2 is not None  # NOT debounced away
            assert set(r2.optimized_order) == {d1.id, d2.id}
