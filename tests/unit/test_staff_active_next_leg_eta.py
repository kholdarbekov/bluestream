"""GET /api/v1/staff/delivery/active — the next-leg ETA (spec 8.4) and its honesty states.

Drives the real endpoint with a driver JWT. External I/O only is mocked:
the distance matrix via MapsService.get_distance_matrix (established
pattern), and Google via request_with_retry inside google_routes only to
prove it is never reached (owner rule 2026-11-03: the route card reads the
matrix alone; Google serves the customer's "on the way" estimate).
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from flask_jwt_extended import create_access_token

from business_app.models.delivery import Delivery, DeliveryPerson
from business_app.models.order import Order
from business_app.models.user import User, UserAddress
from shared.enums import DeliveryStatus, OrderStatus, UserRole, UserType


def _auth_headers(app, user_id: int) -> dict:
    with app.app_context():
        token = create_access_token(identity=str(user_id))
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


@pytest.fixture
def driver(db):
    user = User(
        email="eta-driver@example.com",
        phone="+998900000077",
        password_hash="x",
        first_name="Eta",
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
        full_name="Eta Driver",
        phone="+998900000077",
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
        email="eta-cust@example.com",
        phone="+998900000078",
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


def _real_source_matrix(self, points, traffic=True, use_cache=True):
    m = {}
    for i in range(len(points)):
        for j in range(len(points)):
            m[(i, j)] = (
                {"distance_km": 0.0, "duration_minutes": 0.0}
                if i == j
                else {"distance_km": 3.0, "duration_minutes": 7.0}
            )
    return m, "osrm_selfhosted"


@pytest.mark.unit
@pytest.mark.delivery
class TestNextLegEtaTiering:
    def test_the_route_card_never_calls_google(
        self, app, client, db, driver, driver_person, customer, monkeypatch
    ):
        """Owner rule 2026-11-03: Google's paid Routes API serves the customer's "on the way" estimate and nothing
        else. The route card is rebuilt on every driver location update, so one Google call here is a bill per
        update. With a key configured and a driver whose position is fresh (the case that used to ask Google
        first), the card still publishes the matrix leg and `request_with_retry` is never reached."""
        _make_assigned_delivery(db, customer.id, driver.id, 41.310, 69.270, "ORD-G1")
        monkeypatch.setattr(
            "business_app.services.maps_service.MapsService.get_distance_matrix",
            _real_source_matrix,
        )

        def google_is_off_limits(*args, **kw):
            # pytest.fail is a BaseException, so no `except Exception` on the card's path can swallow it.
            pytest.fail("the route card called Google Routes")

        # Both doors: the HTTP request itself, and the capped client in front of it. A card call that the cap
        # happened to refuse never reaches the request, yet it would still count against the day's budget.
        monkeypatch.setattr("business_app.utils.google_routes.request_with_retry", google_is_off_limits)
        monkeypatch.setattr("business_app.utils.google_routes.get_traffic_aware_leg", google_is_off_limits)
        monkeypatch.setitem(app.config, "GOOGLE_ROUTES_API_KEY", "g-key")

        response = client.get("/api/v1/staff/delivery/active", headers=_auth_headers(app, driver.id))
        assert response.status_code == 200
        top = response.get_json()["data"]["items"][0]
        assert top["eta_minutes_from_current_location"] == 7
        assert top["distance_km_to_next"] == 3.0
        assert top["eta_source"] == "osrm_selfhosted"
        assert top["eta_suppressed"] is False

    @pytest.mark.parametrize(
        "position, location_status",
        [
            pytest.param(
                {
                    "current_location_lat": 41.300,
                    "current_location_lng": 69.250,
                    "last_location_update": datetime.now(UTC) - timedelta(hours=2),
                },
                "stale",
                id="old-position",
            ),
            pytest.param({}, "missing", id="never-shared"),
        ],
    )
    def test_without_a_fresh_position_nothing_is_attempted(
        self, app, client, db, driver, customer, monkeypatch, position, location_status
    ):
        """State 1 of the card's next-leg block ("not attempted"), unchanged: a driver with no fresh position gets
        no minutes, no km, no provenance and no suppression flag, and neither the matrix nor Google is asked."""
        db.session.add(
            DeliveryPerson(
                user_id=driver.id,
                full_name="Eta Driver",
                phone="+998900000077",
                is_active=True,
                is_available=True,
                **position,
            )
        )
        db.session.commit()
        _make_assigned_delivery(db, customer.id, driver.id, 41.310, 69.270, "ORD-N1")

        def nobody_is_asked(*args, **kw):
            pytest.fail("the card asked for an ETA without a fresh driver position")

        monkeypatch.setattr("business_app.services.maps_service.MapsService.get_distance_matrix", nobody_is_asked)
        monkeypatch.setattr("business_app.utils.google_routes.request_with_retry", nobody_is_asked)
        monkeypatch.setitem(app.config, "GOOGLE_ROUTES_API_KEY", "g-key")

        response = client.get("/api/v1/staff/delivery/active", headers=_auth_headers(app, driver.id))
        assert response.status_code == 200
        data = response.get_json()["data"]
        top = data["items"][0]
        assert data["location_status"] == location_status
        assert (
            top["eta_minutes_from_current_location"],
            top["distance_km_to_next"],
            top["eta_source"],
            top["eta_suppressed"],
        ) == (None, None, None, False)

    def test_falls_back_to_matrix_duration_when_google_unconfigured(
        self, app, client, db, driver, driver_person, customer, monkeypatch
    ):
        _make_assigned_delivery(db, customer.id, driver.id, 41.310, 69.270, "ORD-G2")
        monkeypatch.setattr(
            "business_app.services.maps_service.MapsService.get_distance_matrix",
            _real_source_matrix,
        )
        monkeypatch.setitem(app.config, "GOOGLE_ROUTES_API_KEY", None)

        response = client.get("/api/v1/staff/delivery/active", headers=_auth_headers(app, driver.id))
        assert response.status_code == 200
        top = response.get_json()["data"]["items"][0]
        assert top["eta_minutes_from_current_location"] == 7
        assert top["distance_km_to_next"] == 3.0
        assert top["eta_source"] == "osrm_selfhosted"


def _haversine_source_matrix(self, points, traffic=True, use_cache=True):
    m = {}
    for i in range(len(points)):
        for j in range(len(points)):
            m[(i, j)] = (
                {"distance_km": 0.0, "duration_minutes": 0.0}
                if i == j
                else {"distance_km": 3.0, "duration_minutes": 7.0}
            )
    return m, "haversine"


@pytest.mark.unit
@pytest.mark.delivery
class TestEtaHonesty:
    """Spec 8.4: never present a straight-line estimate as a measured ETA.
    The DECISION is published as `eta_suppressed`; the bot only reads it
    (SSOT — CLAUDE.md: never leave two places deciding the same thing)."""

    def test_haversine_source_suppresses_and_publishes_the_decision(
        self, app, client, db, driver, driver_person, customer, monkeypatch
    ):
        _make_assigned_delivery(db, customer.id, driver.id, 41.310, 69.270, "ORD-H1")
        monkeypatch.setattr(
            "business_app.services.maps_service.MapsService.get_distance_matrix",
            _haversine_source_matrix,
        )
        monkeypatch.setitem(app.config, "GOOGLE_ROUTES_API_KEY", None)

        response = client.get("/api/v1/staff/delivery/active", headers=_auth_headers(app, driver.id))
        assert response.status_code == 200
        top = response.get_json()["data"]["items"][0]
        assert top["eta_suppressed"] is True
        # Suppression is enacted server-side: the numbers are gone, not
        # merely flagged — no client can show a straight-line ETA.
        assert top["eta_minutes_from_current_location"] is None
        assert top["distance_km_to_next"] is None
        assert top["eta_source"] is None

    def test_real_source_publishes_values_and_not_suppressed(
        self, app, client, db, driver, driver_person, customer, monkeypatch
    ):
        _make_assigned_delivery(db, customer.id, driver.id, 41.310, 69.270, "ORD-H2")
        monkeypatch.setattr(
            "business_app.services.maps_service.MapsService.get_distance_matrix",
            _real_source_matrix,
        )
        monkeypatch.setitem(app.config, "GOOGLE_ROUTES_API_KEY", None)

        response = client.get("/api/v1/staff/delivery/active", headers=_auth_headers(app, driver.id))
        top = response.get_json()["data"]["items"][0]
        assert top["eta_suppressed"] is False
        assert top["eta_minutes_from_current_location"] == 7

    def test_every_item_carries_the_field(
        self, app, client, db, driver, driver_person, customer, monkeypatch
    ):
        _make_assigned_delivery(db, customer.id, driver.id, 41.310, 69.270, "ORD-H3")
        _make_assigned_delivery(db, customer.id, driver.id, 41.320, 69.280, "ORD-H4")
        monkeypatch.setattr(
            "business_app.services.maps_service.MapsService.get_distance_matrix",
            _real_source_matrix,
        )
        response = client.get("/api/v1/staff/delivery/active", headers=_auth_headers(app, driver.id))
        items = response.get_json()["data"]["items"]
        assert all("eta_suppressed" in it for it in items)
        assert items[1]["eta_suppressed"] is False


def _cache_source_matrix(self, points, traffic=True, use_cache=True):
    m = {}
    for i in range(len(points)):
        for j in range(len(points)):
            m[(i, j)] = (
                {"distance_km": 0.0, "duration_minutes": 0.0}
                if i == j
                else {"distance_km": 3.0, "duration_minutes": 7.0}
            )
    return m, "cache"


@pytest.mark.unit
@pytest.mark.delivery
class TestEtaSourceProvenance:
    """Final review round, I3: `eta_source` is a provenance field a
    not-yet-written Plan 3 will consume. "cache" is a TIER label, not a
    provider — it must never leak through as if it were one."""

    def test_cache_hit_publishes_the_recovered_provider_not_the_tier_label(
        self, app, client, db, driver, driver_person, customer, monkeypatch
    ):
        _make_assigned_delivery(db, customer.id, driver.id, 41.310, 69.270, "ORD-P1")
        monkeypatch.setattr(
            "business_app.services.maps_service.MapsService.get_distance_matrix",
            _cache_source_matrix,
        )
        monkeypatch.setattr(
            "business_app.services.maps_service.MapsService.get_cached_matrix_source",
            lambda self, points, traffic=True: "osrm_selfhosted",
        )
        monkeypatch.setitem(app.config, "GOOGLE_ROUTES_API_KEY", None)

        response = client.get("/api/v1/staff/delivery/active", headers=_auth_headers(app, driver.id))
        assert response.status_code == 200
        top = response.get_json()["data"]["items"][0]
        assert top["eta_source"] == "osrm_selfhosted"
        assert top["eta_minutes_from_current_location"] == 7

    def test_cache_hit_with_unrecoverable_provenance_says_so_explicitly(
        self, app, client, db, driver, driver_person, customer, monkeypatch
    ):
        """The static-tier entry predates this fix (no stashed `source`), or
        expired between the two lookups — `get_cached_matrix_source` returns
        None. Must publish an explicit non-provider sentinel, never guess."""
        _make_assigned_delivery(db, customer.id, driver.id, 41.310, 69.270, "ORD-P2")
        monkeypatch.setattr(
            "business_app.services.maps_service.MapsService.get_distance_matrix",
            _cache_source_matrix,
        )
        monkeypatch.setattr(
            "business_app.services.maps_service.MapsService.get_cached_matrix_source",
            lambda self, points, traffic=True: None,
        )
        monkeypatch.setitem(app.config, "GOOGLE_ROUTES_API_KEY", None)

        response = client.get("/api/v1/staff/delivery/active", headers=_auth_headers(app, driver.id))
        assert response.status_code == 200
        top = response.get_json()["data"]["items"][0]
        assert top["eta_source"] == "cache_unknown_provider"
        assert top["eta_source"] != "cache"


@pytest.mark.unit
@pytest.mark.delivery
class TestEtaErrorStateDistinguishable:
    """Final review round, I4: a caught exception previously left
    `(eta_suppressed=False, eta_source=None)` — indistinguishable from
    "nothing was attempted" (e.g. driver has no fresh GPS). Plan 3 needs to
    tell "attempted and failed" apart from "not attempted"."""

    def test_matrix_lookup_exception_publishes_a_distinguishable_error_state(
        self, app, client, db, driver, driver_person, customer, monkeypatch
    ):
        def boom(self, points, traffic=True, use_cache=True):
            raise RuntimeError("matrix provider exploded")

        _make_assigned_delivery(db, customer.id, driver.id, 41.310, 69.270, "ORD-E1")
        monkeypatch.setattr(
            "business_app.services.maps_service.MapsService.get_distance_matrix", boom
        )
        monkeypatch.setitem(app.config, "GOOGLE_ROUTES_API_KEY", None)

        response = client.get("/api/v1/staff/delivery/active", headers=_auth_headers(app, driver.id))
        assert response.status_code == 200
        top = response.get_json()["data"]["items"][0]
        # Must NOT collapse to the "nothing computed" state:
        # (eta_suppressed=False, eta_source=None).
        assert top["eta_source"] == "error"
        assert top["eta_suppressed"] is False
        assert top["eta_minutes_from_current_location"] is None
        assert top["distance_km_to_next"] is None

    def test_exception_after_values_were_already_populated_still_nulls_them(
        self, app, client, db, driver, driver_person, customer, monkeypatch
    ):
        """Residuals round, item 4: the I3 provenance lookup
        (`get_cached_matrix_source`) runs INSIDE the same `try`, after both
        `eta_minutes_from_current_location`/`distance_km_to_next` are
        already assigned from a "cache" source. If it raises, the handler
        must still null the values it already wrote — otherwise
        `eta_source="error"` could ship alongside a real (partial) ETA, a
        fourth, undocumented state outside the published three."""
        _make_assigned_delivery(db, customer.id, driver.id, 41.310, 69.270, "ORD-E2")
        monkeypatch.setattr(
            "business_app.services.maps_service.MapsService.get_distance_matrix",
            _cache_source_matrix,
        )

        def boom(self, points, traffic=True):
            raise RuntimeError("redis exploded mid-lookup")

        monkeypatch.setattr(
            "business_app.services.maps_service.MapsService.get_cached_matrix_source", boom
        )
        monkeypatch.setitem(app.config, "GOOGLE_ROUTES_API_KEY", None)

        response = client.get("/api/v1/staff/delivery/active", headers=_auth_headers(app, driver.id))
        assert response.status_code == 200
        top = response.get_json()["data"]["items"][0]
        assert top["eta_source"] == "error"
        assert top["eta_suppressed"] is False
        assert top["eta_minutes_from_current_location"] is None
        assert top["distance_km_to_next"] is None
