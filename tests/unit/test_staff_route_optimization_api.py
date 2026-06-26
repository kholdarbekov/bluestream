"""Route-level tests for the staff route-optimization endpoints.

Exercises:
  - GET  /api/v1/staff/delivery/active     (sorted + annotated payload)
  - POST /api/v1/staff/delivery/optimize-route   (manual re-optimization)

Auth contract is enforced via `require_staff_roles("delivery_driver")`. The
underlying Yandex matrix call is monkeypatched to a Haversine fixture so
tests stay deterministic and don't depend on the network.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from flask_jwt_extended import create_access_token

from business_app.models.bottle import BottleBalance
from business_app.models.delivery import Delivery, DeliveryPerson, DeliveryRoute
from business_app.models.order import Order
from business_app.models.user import User, UserAddress
from shared.enums import DeliveryStatus, OrderStatus, UserRole, UserType


def _auth_headers(app, user_id: int) -> dict:
    with app.app_context():
        token = create_access_token(identity=str(user_id))
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _haversine_matrix(self, points, traffic=True, use_cache=True):
    """Stand-in for `MapsService.get_distance_matrix` — pure Haversine."""
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


@pytest.fixture
def driver(db):
    user = User(
        email="api-driver@example.com",
        phone="+998900000001",
        password_hash="x",
        first_name="API",
        last_name="Driver",
        user_type=UserType.STAFF,
        role=UserRole.DELIVERY_DRIVER,
        is_verified=True,
    )
    db.session.add(user)
    db.session.commit()
    return user


@pytest.fixture
def driver_person(db, driver):
    person = DeliveryPerson(
        user_id=driver.id,
        full_name="API Driver",
        phone="+998900000001",
        current_location_lat=41.300,
        current_location_lng=69.250,
        last_location_update=datetime.now(UTC),
        is_active=True,
        is_available=True,
    )
    db.session.add(person)
    db.session.commit()
    return person


@pytest.fixture
def customer(db):
    user = User(
        email="api-cust@example.com",
        phone="+998900000099",
        password_hash="x",
        first_name="Cust",
        last_name="",
        user_type=UserType.INDIVIDUAL,
        role=UserRole.CUSTOMER,
        is_verified=True,
    )
    db.session.add(user)
    db.session.commit()
    return user


def _make_assigned_delivery(db, customer_id, driver_id, lat, lng, order_no):
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


@pytest.mark.unit
@pytest.mark.delivery
class TestGetActiveDeliveriesPayload:
    def test_returns_empty_list_when_no_active_deliveries(self, app, client, db, driver):
        response = client.get("/api/v1/staff/delivery/active", headers=_auth_headers(app, driver.id))
        assert response.status_code == 200
        body = response.get_json()
        assert body["data"]["items"] == []
        assert body["data"]["total"] == 0
        # No DeliveryPerson row → location_status == 'missing'.
        assert body["data"]["location_status"] == "missing"

    def test_payload_is_sorted_and_annotated(
        self, app, client, db, driver, driver_person, customer, monkeypatch
    ):
        # Three deliveries; persisted route forces a known sequence.
        d_far = _make_assigned_delivery(db, customer.id, driver.id, 41.300, 69.330, "ORD-far")
        d_close = _make_assigned_delivery(db, customer.id, driver.id, 41.300, 69.260, "ORD-close")
        d_mid = _make_assigned_delivery(db, customer.id, driver.id, 41.300, 69.290, "ORD-mid")

        route = DeliveryRoute(
            name="t",
            delivery_person_id=driver.id,
            start_location_lat=41.300,
            start_location_lng=69.250,
            route_date=datetime.now(UTC),
            optimized_order=[d_close.id, d_mid.id, d_far.id],
            status="planned",
        )
        db.session.add(route)
        db.session.commit()

        monkeypatch.setattr(
            "business_app.services.maps_service.MapsService.get_distance_matrix",
            _haversine_matrix,
        )

        response = client.get("/api/v1/staff/delivery/active", headers=_auth_headers(app, driver.id))
        assert response.status_code == 200
        items = response.get_json()["data"]["items"]
        assert [it["delivery_id"] for it in items] == [d_close.id, d_mid.id, d_far.id]
        assert items[0]["is_next"] is True
        assert items[0]["route_position"] == 0
        # Top item has next-leg ETA + km annotations.
        assert items[0]["distance_km_to_next"] is not None
        assert items[0]["eta_minutes_from_current_location"] is not None
        # Non-top items have no ETA annotations.
        assert items[1]["is_next"] is False
        assert items[1]["distance_km_to_next"] is None
        assert response.get_json()["data"]["location_status"] == "fresh"

    def test_location_status_stale_when_old_update(
        self, app, client, db, driver, customer, monkeypatch
    ):
        person = DeliveryPerson(
            user_id=driver.id,
            full_name="x",
            phone="x",
            current_location_lat=41.30,
            current_location_lng=69.25,
            last_location_update=datetime.now(UTC) - timedelta(hours=2),
        )
        db.session.add(person)
        _make_assigned_delivery(db, customer.id, driver.id, 41.31, 69.26, "ORD-1")
        monkeypatch.setattr(
            "business_app.services.maps_service.MapsService.get_distance_matrix",
            _haversine_matrix,
        )

        response = client.get("/api/v1/staff/delivery/active", headers=_auth_headers(app, driver.id))
        assert response.status_code == 200
        assert response.get_json()["data"]["location_status"] == "stale"


@pytest.mark.unit
@pytest.mark.delivery
class TestManualOptimizeRoute:
    def test_unauthenticated_returns_401(self, client):
        response = client.post("/api/v1/staff/delivery/optimize-route")
        assert response.status_code == 401

    def test_optimization_persists_route_and_returns_sorted_payload(
        self, app, client, db, driver, driver_person, customer, monkeypatch
    ):
        d_far = _make_assigned_delivery(db, customer.id, driver.id, 41.300, 69.330, "ORD-far")
        d_close = _make_assigned_delivery(db, customer.id, driver.id, 41.300, 69.260, "ORD-close")
        d_mid = _make_assigned_delivery(db, customer.id, driver.id, 41.300, 69.290, "ORD-mid")

        monkeypatch.setattr(
            "business_app.services.maps_service.MapsService.get_distance_matrix",
            _haversine_matrix,
        )

        response = client.post(
            "/api/v1/staff/delivery/optimize-route",
            headers=_auth_headers(app, driver.id),
        )
        assert response.status_code == 200
        items = response.get_json()["data"]["items"]
        assert [it["delivery_id"] for it in items] == [d_close.id, d_mid.id, d_far.id]
        assert items[0]["is_next"] is True

        # The route was actually persisted with trigger='manual'.
        route = (
            DeliveryRoute.query.filter_by(delivery_person_id=driver.id)
            .order_by(DeliveryRoute.created_at.desc())
            .first()
        )
        assert route is not None
        assert route.optimized_order == [d_close.id, d_mid.id, d_far.id]
        assert (route.extra_data or {}).get("trigger") == "manual"

    def test_returns_empty_when_driver_has_no_active_deliveries(
        self, app, client, db, driver, driver_person, monkeypatch
    ):
        monkeypatch.setattr(
            "business_app.services.maps_service.MapsService.get_distance_matrix",
            _haversine_matrix,
        )
        response = client.post(
            "/api/v1/staff/delivery/optimize-route",
            headers=_auth_headers(app, driver.id),
        )
        assert response.status_code == 200
        body = response.get_json()
        assert body["data"]["items"] == []
        assert body["data"]["total"] == 0

    def test_returns_412_LOCATION_REQUIRED_when_driver_has_not_shared_location(
        self, app, client, db, driver, customer, monkeypatch
    ):
        """Driver-current-location is a hard precondition. Without it the
        endpoint must refuse with 412 + a stable error_code so the bot can
        prompt the driver to share location instead of silently rendering
        a fake-optimal sequence."""
        # Driver has active deliveries but no DeliveryPerson row -> "missing".
        _make_assigned_delivery(db, customer.id, driver.id, 41.31, 69.27, "ORD-NL-1")

        matrix_calls = []
        monkeypatch.setattr(
            "business_app.services.maps_service.MapsService.get_distance_matrix",
            lambda self, points, traffic=True, use_cache=True: matrix_calls.append(points)
            or _haversine_matrix(self, points, traffic, use_cache),
        )

        response = client.post(
            "/api/v1/staff/delivery/optimize-route",
            headers=_auth_headers(app, driver.id),
        )

        assert response.status_code == 412
        body = response.get_json()
        assert body["error_code"] == "LOCATION_REQUIRED"
        assert body["success"] is False
        # The endpoint must refuse cleanly without doing any optimization work.
        assert matrix_calls == []


@pytest.mark.unit
@pytest.mark.delivery
class TestUpdateMyLocation:
    """The driver-level location endpoint that fixes the bug where shared
    locations were silently dropped before the first stop was picked up."""

    def test_unauthenticated_returns_401(self, client):
        response = client.post("/api/v1/staff/delivery/me/location")
        assert response.status_code == 401

    def test_invalid_coordinates_return_400(self, app, client, db, driver, driver_person):
        response = client.post(
            "/api/v1/staff/delivery/me/location",
            headers=_auth_headers(app, driver.id),
            json={"latitude": "not-a-number", "longitude": 69.25},
        )
        assert response.status_code == 400

    def test_updates_driver_location_runs_opt_and_returns_sorted_payload(
        self, app, client, db, driver, customer, monkeypatch
    ):
        """End-to-end happy path. Driver has no DeliveryPerson row at all
        when the request hits — the endpoint must still update the
        driver's location, run optimization on the new start point, and
        return the freshly-sorted active-deliveries payload."""
        from business_app.models.delivery import DeliveryPerson

        # The endpoint requires an existing DeliveryPerson profile (admins
        # provision drivers before they go online), but at this moment the
        # driver hasn't shared any location yet.
        person = DeliveryPerson(
            user_id=driver.id,
            full_name="x",
            phone="x",
            current_location_lat=None,
            current_location_lng=None,
            last_location_update=None,
            is_active=True,
            is_available=True,
        )
        db.session.add(person)
        db.session.commit()

        d_far = _make_assigned_delivery(db, customer.id, driver.id, 41.300, 69.330, "ORD-far")
        d_close = _make_assigned_delivery(db, customer.id, driver.id, 41.300, 69.260, "ORD-close")

        monkeypatch.setattr(
            "business_app.services.maps_service.MapsService.get_distance_matrix",
            _haversine_matrix,
        )

        response = client.post(
            "/api/v1/staff/delivery/me/location",
            headers=_auth_headers(app, driver.id),
            json={"latitude": 41.300, "longitude": 69.250},
        )

        assert response.status_code == 200
        body = response.get_json()
        items = body["data"]["items"]
        # Now that we have a real start point at (41.300, 69.250), close
        # comes before far.
        assert [it["delivery_id"] for it in items] == [d_close.id, d_far.id]
        # And location_status is now "fresh" because we just updated it.
        assert body["data"]["location_status"] == "fresh"

        # Persisted: DeliveryPerson location reflects the request.
        db.session.refresh(person)
        assert person.current_location_lat == 41.300
        assert person.current_location_lng == 69.250
        assert person.last_location_update is not None


def _delivery_with_address(db, customer_id, driver_id, order_no, lat=41.30, lng=69.26):
    """Create address + order + ASSIGNED delivery; return (delivery, address)."""
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
    return delivery, addr


@pytest.mark.unit
@pytest.mark.delivery
class TestActiveDeliveryBottleBalance:
    @pytest.fixture(autouse=True)
    def _patch_matrix(self, monkeypatch):
        monkeypatch.setattr(
            "business_app.services.maps_service.MapsService.get_distance_matrix",
            _haversine_matrix,
        )

    def _item_for(self, resp, delivery_id):
        assert resp.status_code == 200
        items = resp.get_json()["data"]["items"]
        return next(it for it in items if it["delivery_id"] == delivery_id)

    def test_balance_reflected_for_delivery_address(self, app, client, db, driver, customer):
        delivery, addr = _delivery_with_address(db, customer.id, driver.id, "ORD-bal")
        db.session.add(BottleBalance(user_id=customer.id, address_id=addr.id, balance=Decimal("7")))
        db.session.commit()

        resp = client.get("/api/v1/staff/delivery/active", headers=_auth_headers(app, driver.id))
        assert self._item_for(resp, delivery.id)["customer_bottle_balance"] == 7

    def test_zero_when_no_balance_row(self, app, client, db, driver, customer):
        delivery, _ = _delivery_with_address(db, customer.id, driver.id, "ORD-nobal")
        resp = client.get("/api/v1/staff/delivery/active", headers=_auth_headers(app, driver.id))
        assert self._item_for(resp, delivery.id)["customer_bottle_balance"] == 0

    def test_negative_balance_floored_to_zero(self, app, client, db, driver, customer):
        delivery, addr = _delivery_with_address(db, customer.id, driver.id, "ORD-neg")
        db.session.add(BottleBalance(user_id=customer.id, address_id=addr.id, balance=Decimal("-3")))
        db.session.commit()
        resp = client.get("/api/v1/staff/delivery/active", headers=_auth_headers(app, driver.id))
        assert self._item_for(resp, delivery.id)["customer_bottle_balance"] == 0

    def test_balance_scoped_to_delivery_address_only(self, app, client, db, driver, customer):
        delivery, _addr = _delivery_with_address(db, customer.id, driver.id, "ORD-scope")
        other = UserAddress(
            user_id=customer.id, title="Other", full_address="Other",
            street_address="Other", latitude=41.31, longitude=69.27,
        )
        db.session.add(other)
        db.session.flush()
        db.session.add(BottleBalance(user_id=customer.id, address_id=other.id, balance=Decimal("9")))
        db.session.commit()
        resp = client.get("/api/v1/staff/delivery/active", headers=_auth_headers(app, driver.id))
        # Balance lives under a different address → this delivery shows 0.
        assert self._item_for(resp, delivery.id)["customer_bottle_balance"] == 0
