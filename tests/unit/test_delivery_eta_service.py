"""DeliveryEtaService: the customer's "on the way" promise (honest-ETA Phase 1; spec §4.8 S2 and S3, §4.10, §4.12a).

The driver taps `in_transit` to a stop. The notification worker then calls `record_on_the_way(history_id)`. From a
driver position at most 10 minutes old at the tap, one Google Routes TRAFFIC_AWARE call gives the drive time G, and the
customer is promised "by tap + 5 off-road minutes + headroom x G". From an older position no call is made, and the
promise is "by tap + our own engine's minutes + an allowance". Both go in the SHOWN `google_tap_v1` row. Our own engine's
time for the same leg is written beside it as a HIDDEN `osrm_shadow_v1` row. `customer_eta(order, now=)` reads the
promise of the latest real tap back as the fields Track and the message publish.

Real rows throughout: the delivery, its order's address, the tap's history row and the driver's DeliveryPerson with its
position stamped relative to the tap. Faked, as external I/O only: Google's HTTP request
(`google_routes.request_with_retry`, an 18-minute drive), the cap's Redis counter (`FakeCallCounterRedis`, so no test
writes to the Redis DBs the suite shares) and the distance matrix (`MapsService.get_distance_matrix`: self-hosted
OSRM, 11 minutes).
"""

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from sqlalchemy import event
from sqlalchemy.dialects import postgresql

from business_app.models.delivery import Delivery, DeliveryPerson, DeliveryStatusHistory
from business_app.models.delivery_promise import MODEL_GOOGLE_TAP, MODEL_OSRM_SHADOW, DeliveryPromise
from business_app.models.order import Order
from business_app.models.user import UserAddress
from business_app.services.delivery_assignment_service import DeliveryAssignmentService
from business_app.services.delivery_service import DeliveryService
from business_app.services.delivery_eta_service import CustomerEta, DeliveryEtaService
from business_app.services.order_schedule_service import OrderScheduleService
from business_app.utils import google_routes
from business_app.utils.distance_matrix import STRAIGHT_LINE_SOURCE
from business_app.utils.timezone_utils import ensure_utc
from shared import business_config
from shared.enums import AssignmentSource, DeliveryStatus, OrderStatus
from tests.unit.test_google_routes_daily_cap import FakeCallCounterRedis

pytestmark = pytest.mark.unit

TAP = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)  # the driver taps "in transit": 14:00 in Tashkent
NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)  # the headroom and reading clock: 17:00 in Tashkent
DRIVER_AT = (41.300, 69.250)
DOOR_AT = (41.3111, 69.2797)
# The door of another stop of the driver.
FINISHED_AT = (41.3250, 69.2560)
# The warehouse, for tests that use the `depot` fixture.
DEPOT_AT = (41.0504, 69.1393)

# Days back, clear of the 29 hourly legs. `_recorded_legs` taps one driver's legs at NOW-1h ... NOW-29h. A long leg
# placed among them is interrupted by that driver's next hourly tap (spec §4.8 S2), and the rule a test pins is then
# masked. A test that needs one long leg alone records it this many days back, still inside headroom's four weeks.
CLEAR_OF_THE_HOURLY_LEGS = 2

# Headroom once a 30th leg, r = 4.33 against 29 of r = 1.0, is learned from: (30 x 1.0 + 20 x 1.3) / 50 = 1.12. The
# inclusive p90 of 29 x 1.0 and one 4.33 is 1.0. Without it, 29 legs leave the seed: 1.3.
THIRTY_LEGS = (30 * 1.0 + 20 * 1.3) / 50
SEED = 1.3

# The values the spec's worked numbers are stated in (§4.8 S2). Pinned per test so a deployed override cannot move
# them; the shipped defaults are pinned against these by the last test of the file.
SPEC_TUNABLES = {
    "ETA_POSITION_MAX_AGE_SECONDS": 600,
    "ETA_OFF_ROAD_MINUTES": 5,
    "ETA_HEADROOM_SEED": 1.3,
    "ETA_HEADROOM_MIN_LEGS": 30,
    "ETA_HEADROOM_PRIOR_LEGS": 20,
    "ETA_STALE_DOOR_ALLOWANCE_MINUTES": 30,
    "ETA_STALE_DEPOT_ALLOWANCE_MINUTES": 20,
}


class FakeGoogle:
    """`google_routes.request_with_retry`: records every request and answers an 18-minute drive, or a 503 once `down`.

    `counter` is the day's cap counter each call is counted on before it is sent. `while_asked` runs inside the
    request, as another worker might while this one waits for Google.
    """

    def __init__(self):
        self.requests = []
        self.counter = FakeCallCounterRedis()
        self.down = False
        self.while_asked = None

    def __call__(self, **kwargs):
        self.requests.append(kwargs)
        if self.while_asked is not None:
            self.while_asked()
        response = MagicMock()
        if self.down:
            response.status_code, response.text = 503, "Service Unavailable"
            return response
        response.status_code, response.text = 200, "ok"
        response.json.return_value = {"routes": [{"duration": "1080s", "distanceMeters": 7400}]}
        return response

    @property
    def calls_counted(self):
        return sum(self.counter.counts.values())


class OurEngine:
    """`MapsService.get_distance_matrix`: self-hosted OSRM says 11 minutes from the driver to the door.

    Records what it is asked. `source` is the label it answers with; `error` is raised instead of answering.
    """

    def __init__(self):
        self.asks = []
        self.source = "osrm_selfhosted"
        self.error = None

    def answer(self, points, traffic):
        self.asks.append((list(points), traffic))
        if self.error is not None:
            raise self.error
        zero = {"distance_km": 0.0, "duration_minutes": 0.0}
        leg = {"distance_km": 6.4, "duration_minutes": 11.0}
        return {(0, 0): zero, (0, 1): leg, (1, 0): leg, (1, 1): zero}, self.source


@pytest.fixture
def spec_tunables(app, monkeypatch):
    for name, value in SPEC_TUNABLES.items():
        monkeypatch.setitem(app.config, name, value)


@pytest.fixture
def depot(app, monkeypatch):
    """The warehouse at DEPOT_AT, which `StaffService.anchor_driver_at_warehouse` stamps on a driver who loads up."""
    monkeypatch.setitem(app.config, "WAREHOUSE_LATITUDE", DEPOT_AT[0])
    monkeypatch.setitem(app.config, "WAREHOUSE_LONGITUDE", DEPOT_AT[1])
    return DEPOT_AT


@pytest.fixture
def google(app, monkeypatch):
    """Google Routes with a key, behind an empty day counter."""
    fake = FakeGoogle()
    monkeypatch.setitem(app.config, "GOOGLE_ROUTES_API_KEY", "g-key")
    monkeypatch.setattr(google_routes, "redis_client", fake.counter)
    monkeypatch.setattr(google_routes, "request_with_retry", fake)
    return fake


@pytest.fixture
def matrix(monkeypatch):
    engine = OurEngine()

    def get_distance_matrix(maps_service, points, traffic=True, use_cache=True):  # MapsService's own signature
        return engine.answer(points, traffic)

    monkeypatch.setattr("business_app.services.maps_service.MapsService.get_distance_matrix", get_distance_matrix)
    return engine


def _order_to(db, customer, *, door=DOOR_AT, status=OrderStatus.OUT_FOR_DELIVERY, number="ETA-0001"):
    """An order to `door`; an address without coordinates when `door` is None."""
    latitude, longitude = door if door is not None else (None, None)
    address = UserAddress(
        user_id=customer.id,
        title="Home",
        full_address="1 Test St, Tashkent",
        street_address="1 Test St",
        city="Tashkent",
        latitude=latitude,
        longitude=longitude,
    )
    db.session.add(address)
    db.session.flush()
    order = Order(
        user_id=customer.id,
        order_number=number,
        status=status,
        subtotal=Decimal("30000"),
        total_amount=Decimal("30000"),
        delivery_address_id=address.id,
    )
    db.session.add(order)
    db.session.commit()
    return order


def _order_on_its_way(
    db,
    customer,
    driver,
    *,
    door=DOOR_AT,
    delivery_status=DeliveryStatus.IN_TRANSIT,
    order_status=OrderStatus.OUT_FOR_DELIVERY,
    number="ETA-0001",
):
    """An order to `door` whose delivery `driver` holds, in `delivery_status`. Its tracking number is spelled from
    `number`: the generated one (the minute and 24 random bits) can collide across the dozens a test builds."""
    order = _order_to(db, customer, door=door, status=order_status, number=number)
    delivery = Delivery(
        order_id=order.id,
        delivery_person_id=driver.id,
        status=delivery_status,
        scheduled_date=TAP,
        scheduled_time_slot="09:00-12:00",
        tracking_number=f"TRK-{number}",
    )
    db.session.add(delivery)
    db.session.commit()
    return delivery


def _driver_last_seen(db, driver, at, position=DRIVER_AT):
    """The driver's DeliveryPerson, last placed at `position` at `at`; a driver who never shared a position at None."""
    person = DeliveryPerson(user_id=driver.id, full_name="Eta Driver", phone=driver.phone, is_active=True)
    if at is not None:
        person.current_location_lat, person.current_location_lng = position
        person.last_location_update = at
    db.session.add(person)
    db.session.commit()
    return person


def _finished_stop(
    db,
    customer,
    driver,
    *,
    at,
    by=None,
    old=DeliveryStatus.ARRIVED,
    new=DeliveryStatus.DELIVERED,
    stamped=True,
    number="ETA-0009",
):
    """Another stop of `driver`, its door at FINISHED_AT: `by` (the driver, unless given) moved it from `old` to `new`
    at `at`. The history row carries the stop's door, as `DeliveryService._capture_arrival_position` stamps every
    arrival and delivery, unless `stamped` is False."""
    stop = _order_on_its_way(db, customer, driver, door=FINISHED_AT, delivery_status=new, number=number)
    row = DeliveryStatusHistory(
        delivery_id=stop.id, old_status=old, new_status=new, changed_by=(by or driver).id, changed_at=at
    )
    if stamped:
        row.location_lat, row.location_lng = FINISHED_AT
    db.session.add(row)
    db.session.commit()
    return row


def _origin(request):
    """Where a recorded Google request starts the leg."""
    return request["json"]["origin"]["location"]["latLng"]


def _lat_lng(point):
    return {"latitude": point[0], "longitude": point[1]}


def _moved(db, delivery, old, new, *, at, by):
    """One status-history row of `delivery`: `by` moved it from `old` to `new` at `at`."""
    row = DeliveryStatusHistory(
        delivery_id=delivery.id, old_status=old, new_status=new, changed_by=by.id, changed_at=at
    )
    db.session.add(row)
    db.session.commit()
    return row


def _tap(db, delivery, driver, at=TAP):
    """The driver's own tap: picked up -> in transit."""
    return _moved(db, delivery, DeliveryStatus.PICKED_UP, DeliveryStatus.IN_TRANSIT, at=at, by=driver)


def _promise_of(db, tap, *, window_end):
    """The shown promise of `tap`, as `record_on_the_way` writes it from Google's 18 minutes at the tap."""
    tapped_at = ensure_utc(tap.changed_at)
    db.session.add(
        DeliveryPromise(
            delivery_id=tap.delivery_id,
            delivery_status_history_id=tap.id,
            computed_at=tapped_at,
            stage="on_the_way",
            expected_at=tapped_at + timedelta(minutes=18),
            window_end=window_end,
            model_version=MODEL_GOOGLE_TAP,
            source="google_traffic",
            shown=True,
        )
    )
    db.session.commit()


def _promises(model_version):
    return DeliveryPromise.query.filter_by(model_version=model_version).all()


@pytest.mark.usefixtures("spec_tunables")
class TestRecordingTheOnTheWayTap:
    def test_a_google_leg_promises_offroad_plus_headroom(self, db, sample_user, delivery_driver, google, matrix):
        delivery = _order_on_its_way(db, sample_user, delivery_driver)
        _driver_last_seen(db, delivery_driver, at=TAP - timedelta(minutes=2))
        tap = _tap(db, delivery, delivery_driver)

        shown = DeliveryEtaService().record_on_the_way(tap.id)

        # Nothing learned yet, so the seed headroom 1.3: by = tap + ceil(5 + 1.3 x 18) = tap + ceil(28.4) = tap + 29.
        assert ensure_utc(shown.window_end) == TAP + timedelta(minutes=29)
        assert ensure_utc(shown.expected_at) == TAP + timedelta(minutes=18)  # Google's own drive time, kept raw
        assert (shown.source, shown.shown, shown.stage, shown.model_version) == (
            "google_traffic",
            True,
            "on_the_way",
            "google_tap_v1",
        )
        assert (shown.delivery_id, shown.delivery_status_history_id) == (delivery.id, tap.id)
        assert _promises(MODEL_GOOGLE_TAP) == [shown]
        # One Google call, from where the driver was to the door, counted against the day's cap.
        assert len(google.requests) == 1
        route = google.requests[0]["json"]
        assert route["origin"]["location"]["latLng"] == {"latitude": 41.300, "longitude": 69.250}
        assert route["destination"]["location"]["latLng"] == {"latitude": 41.3111, "longitude": 69.2797}
        assert route["routingPreference"] == "TRAFFIC_AWARE"
        assert google.calls_counted == 1

    def test_our_own_engine_is_recorded_as_a_hidden_shadow(self, db, sample_user, delivery_driver, google, matrix):
        delivery = _order_on_its_way(db, sample_user, delivery_driver)
        _driver_last_seen(db, delivery_driver, at=TAP - timedelta(minutes=2))
        tap = _tap(db, delivery, delivery_driver)

        DeliveryEtaService().record_on_the_way(tap.id)

        [shadow] = _promises(MODEL_OSRM_SHADOW)
        assert shadow.shown is False
        assert ensure_utc(shadow.expected_at) == TAP + timedelta(minutes=11)
        assert shadow.window_end is None
        assert (shadow.source, shadow.stage, shadow.delivery_id, shadow.delivery_status_history_id) == (
            "osrm_selfhosted",
            "on_the_way",
            delivery.id,
            tap.id,
        )
        assert matrix.asks == [([DRIVER_AT, DOOR_AT], True)]

    def test_a_position_ten_minutes_old_is_still_fresh(self, db, sample_user, delivery_driver, google, matrix):
        delivery = _order_on_its_way(db, sample_user, delivery_driver)
        _driver_last_seen(db, delivery_driver, at=TAP - timedelta(minutes=10))
        tap = _tap(db, delivery, delivery_driver)

        shown = DeliveryEtaService().record_on_the_way(tap.id)

        assert shown.source == "google_traffic"
        assert _origin(google.requests[0]) == _lat_lng(DRIVER_AT)

    def test_a_stale_position_is_priced_by_our_own_engine_with_no_google_call(
        self, db, sample_user, delivery_driver, google, matrix
    ):
        """Over 10 minutes old at the tap, the position is not where the driver is, so Google's live traffic from there
        is not this leg, and no call is spent. The customer is told a time all the same: our own engine's 11 minutes
        from that position plus the 30-minute allowance. A stale position is wrong by an amount of time, not by a share
        of the route, so it takes an allowance and not a headroom. `expected_at` stays empty: it is Google's time."""
        delivery = _order_on_its_way(db, sample_user, delivery_driver)
        _driver_last_seen(db, delivery_driver, at=TAP - timedelta(minutes=11))
        tap = _tap(db, delivery, delivery_driver)

        shown = DeliveryEtaService().record_on_the_way(tap.id)

        assert (shown.source, shown.expected_at, shown.shown) == ("stale_estimate", None, True)
        assert ensure_utc(shown.window_end) == TAP + timedelta(minutes=41)
        assert google.requests == []
        assert google.calls_counted == 0  # not even counted against the day's cap
        assert matrix.asks == [([DRIVER_AT, DOOR_AT], True)]  # asked once: the shadow row below is the same answer

    def test_the_engines_own_time_is_kept_in_the_hidden_shadow(self, db, sample_user, delivery_driver, google, matrix):
        delivery = _order_on_its_way(db, sample_user, delivery_driver)
        _driver_last_seen(db, delivery_driver, at=TAP - timedelta(minutes=11))
        tap = _tap(db, delivery, delivery_driver)

        DeliveryEtaService().record_on_the_way(tap.id)

        [shadow] = _promises(MODEL_OSRM_SHADOW)
        assert (shadow.shown, shadow.source, shadow.window_end) == (False, "osrm_selfhosted", None)
        assert ensure_utc(shadow.expected_at) == TAP + timedelta(minutes=11)

    def test_a_stale_depot_stamp_takes_the_depot_allowance(
        self, db, sample_user, delivery_driver, google, matrix, depot
    ):
        """Loaded up 40 minutes ago: drivers mostly tap after they have already left, so a depot position needs less
        than a door does. 11 + 20."""
        delivery = _order_on_its_way(db, sample_user, delivery_driver)
        _driver_last_seen(db, delivery_driver, at=TAP - timedelta(minutes=40), position=DEPOT_AT)
        tap = _tap(db, delivery, delivery_driver)

        shown = DeliveryEtaService().record_on_the_way(tap.id)

        assert shown.source == "stale_estimate"
        assert ensure_utc(shown.window_end) == TAP + timedelta(minutes=31)
        assert matrix.asks == [([DEPOT_AT, DOOR_AT], True)]

    @pytest.mark.parametrize(
        "last_seen",
        [
            pytest.param(TAP - timedelta(days=1), id="a-door-from-yesterday"),
            pytest.param(None, id="never-shared"),
        ],
    )
    def test_no_position_from_today_starts_the_leg_at_the_depot(
        self, db, sample_user, delivery_driver, google, matrix, depot, last_seen
    ):
        """Yesterday's last door says nothing of today: a driver's day starts at the warehouse."""
        delivery = _order_on_its_way(db, sample_user, delivery_driver)
        _driver_last_seen(db, delivery_driver, at=last_seen)
        tap = _tap(db, delivery, delivery_driver)

        shown = DeliveryEtaService().record_on_the_way(tap.id)

        assert shown.source == "stale_estimate"
        assert ensure_utc(shown.window_end) == TAP + timedelta(minutes=31)
        assert matrix.asks == [([DEPOT_AT, DOOR_AT], True)]
        assert google.requests == []

    def test_a_driver_with_no_profile_starts_the_leg_at_the_depot(
        self, db, sample_user, delivery_driver, google, matrix, depot
    ):
        delivery = _order_on_its_way(db, sample_user, delivery_driver)
        tap = _tap(db, delivery, delivery_driver)

        shown = DeliveryEtaService().record_on_the_way(tap.id)

        assert ensure_utc(shown.window_end) == TAP + timedelta(minutes=31)
        assert matrix.asks == [([DEPOT_AT, DOOR_AT], True)]

    @pytest.mark.parametrize(
        "tap_at, last_seen, minutes, start",
        [
            # 01:30 on the 4th in Tashkent; the position is from 23:30 on the 3rd, the same UTC date.
            pytest.param(
                datetime(2026, 11, 3, 20, 30, tzinfo=UTC),
                datetime(2026, 11, 3, 18, 30, tzinfo=UTC),
                31,
                DEPOT_AT,
                id="same-utc-date-but-yesterday-in-tashkent",
            ),
            # 23:30 on the 3rd in Tashkent; the position is from 00:30 on the 3rd, the UTC date before.
            pytest.param(
                datetime(2026, 11, 3, 18, 30, tzinfo=UTC),
                datetime(2026, 11, 2, 19, 30, tzinfo=UTC),
                41,
                DRIVER_AT,
                id="another-utc-date-but-today-in-tashkent",
            ),
        ],
    )
    def test_today_is_the_tashkent_day(
        self, db, sample_user, delivery_driver, google, matrix, depot, tap_at, last_seen, minutes, start
    ):
        delivery = _order_on_its_way(db, sample_user, delivery_driver)
        _driver_last_seen(db, delivery_driver, at=last_seen)
        tap = _tap(db, delivery, delivery_driver, at=tap_at)

        shown = DeliveryEtaService().record_on_the_way(tap.id)

        assert ensure_utc(shown.window_end) == tap_at + timedelta(minutes=minutes)
        assert matrix.asks == [([start, DOOR_AT], True)]

    @pytest.mark.parametrize(
        "source, error",
        [
            pytest.param(STRAIGHT_LINE_SOURCE, None, id="a-straight-line-guess"),
            pytest.param("osrm_selfhosted", RuntimeError("matrix down"), id="the-lookup-failed"),
        ],
    )
    def test_a_stale_position_our_engine_cannot_price_gets_no_time(
        self, db, sample_user, delivery_driver, google, matrix, source, error
    ):
        """A straight line is not a drive time: it never becomes a promise. The customer sees "On the way"."""
        matrix.source, matrix.error = source, error
        delivery = _order_on_its_way(db, sample_user, delivery_driver)
        _driver_last_seen(db, delivery_driver, at=TAP - timedelta(minutes=11))
        tap = _tap(db, delivery, delivery_driver)

        shown = DeliveryEtaService().record_on_the_way(tap.id)

        assert (shown.source, shown.expected_at, shown.window_end) == ("stale_position", None, None)
        assert google.requests == []
        [shadow] = _promises(MODEL_OSRM_SHADOW)
        assert shadow.expected_at is None

    def test_an_engine_answer_without_the_leg_counts_as_no_answer(
        self, db, sample_user, delivery_driver, google, matrix, monkeypatch
    ):
        monkeypatch.setattr(matrix, "answer", lambda points, traffic: ({}, "osrm_selfhosted"))
        delivery = _order_on_its_way(db, sample_user, delivery_driver)
        _driver_last_seen(db, delivery_driver, at=TAP - timedelta(minutes=2))
        tap = _tap(db, delivery, delivery_driver)

        shown = DeliveryEtaService().record_on_the_way(tap.id)

        assert shown.source == "google_traffic"  # the fresh promise stands
        [shadow] = _promises(MODEL_OSRM_SHADOW)
        assert (shadow.source, shadow.expected_at) == ("error", None)

    def test_an_admins_mark_never_places_the_driver(self, db, sample_user, delivery_driver, admin_user, google, matrix):
        """An admin marks this driver's other stop arrived, through the real path, from the office. That says nothing of
        where the driver is, so the leg still goes by their position on record, 22 minutes old."""
        other_stop = _order_on_its_way(db, sample_user, delivery_driver, door=FINISHED_AT, number="ETA-OTHER")
        delivery = _order_on_its_way(db, sample_user, delivery_driver)
        _driver_last_seen(db, delivery_driver, at=TAP - timedelta(minutes=22))
        DeliveryService().mark_delivery_arrived(other_stop.id, actor_user_id=admin_user.id)
        tap = _tap(db, delivery, delivery_driver)

        shown = DeliveryEtaService().record_on_the_way(tap.id)

        assert shown.source == "stale_estimate"
        assert google.requests == []
        assert matrix.asks == [([DRIVER_AT, DOOR_AT], True)]

    def test_a_position_stamped_since_the_tap_counts_as_fresh(self, db, sample_user, delivery_driver, google, matrix):
        """The driver sends a pin right after the tap, before the worker runs. It is where they are."""
        delivery = _order_on_its_way(db, sample_user, delivery_driver)
        _driver_last_seen(db, delivery_driver, at=TAP + timedelta(seconds=30))
        tap = _tap(db, delivery, delivery_driver)

        shown = DeliveryEtaService().record_on_the_way(tap.id)

        assert shown.source == "google_traffic"
        assert _origin(google.requests[0]) == _lat_lng(DRIVER_AT)

    def test_google_down_gives_no_minutes(self, db, sample_user, delivery_driver, google, matrix):
        delivery = _order_on_its_way(db, sample_user, delivery_driver)
        _driver_last_seen(db, delivery_driver, at=TAP - timedelta(minutes=2))
        tap = _tap(db, delivery, delivery_driver)
        google.down = True

        shown = DeliveryEtaService().record_on_the_way(tap.id)

        assert (shown.source, shown.expected_at, shown.window_end) == ("google_unavailable", None, None)
        assert len(google.requests) == 1  # asked, and Google failed
        [shadow] = _promises(MODEL_OSRM_SHADOW)
        assert ensure_utc(shadow.expected_at) == TAP + timedelta(minutes=11)  # our own engine is still measured

    def test_an_address_without_coordinates_gives_no_minutes(self, db, sample_user, delivery_driver, google, matrix):
        delivery = _order_on_its_way(db, sample_user, delivery_driver, door=None)
        _driver_last_seen(db, delivery_driver, at=TAP - timedelta(minutes=2))
        tap = _tap(db, delivery, delivery_driver)

        shown = DeliveryEtaService().record_on_the_way(tap.id)

        assert (shown.source, shown.expected_at, shown.window_end) == ("no_destination", None, None)
        assert google.requests == [] and matrix.asks == []
        assert _promises(MODEL_OSRM_SHADOW) == []

    def test_recording_the_same_tap_twice_calls_google_once(self, db, sample_user, delivery_driver, google, matrix):
        """Review Focus #3: the notification worker re-running the same tap (Celery acks_late redelivery)."""
        delivery = _order_on_its_way(db, sample_user, delivery_driver)
        _driver_last_seen(db, delivery_driver, at=TAP - timedelta(minutes=2))
        tap_id = _tap(db, delivery, delivery_driver).id

        first_id = DeliveryEtaService().record_on_the_way(tap_id).id
        db.session.expire_all()  # the re-run reads what the first run stored
        again = DeliveryEtaService().record_on_the_way(tap_id)

        assert again.id == first_id
        assert len(_promises(MODEL_GOOGLE_TAP)) == 1
        assert len(_promises(MODEL_OSRM_SHADOW)) == 1
        assert len(google.requests) == 1 and google.calls_counted == 1
        assert len(matrix.asks) == 1

    def test_a_concurrent_run_of_the_same_tap_returns_the_promise_already_recorded(
        self, db, sample_user, delivery_driver, google, matrix
    ):
        """Two runs of the same tap both find nothing recorded; the other run commits while this one waits for
        Google. The unique (tap, model) index refuses this run's rows, and it hands back the other run's promise."""
        delivery = _order_on_its_way(db, sample_user, delivery_driver)
        _driver_last_seen(db, delivery_driver, at=TAP - timedelta(minutes=2))
        tap_id = _tap(db, delivery, delivery_driver).id
        delivery_id = delivery.id
        recorded_first = {}

        def the_other_run_commits_first():
            rows = [
                DeliveryPromise(
                    delivery_id=delivery_id,
                    delivery_status_history_id=tap_id,
                    stage="on_the_way",
                    model_version=model_version,
                    shown=model_version == MODEL_GOOGLE_TAP,
                    source=source,
                )
                for model_version, source in (
                    (MODEL_GOOGLE_TAP, "google_traffic"),
                    (MODEL_OSRM_SHADOW, "osrm_selfhosted"),
                )
            ]
            db.session.add_all(rows)
            db.session.commit()
            recorded_first["id"] = rows[0].id

        google.while_asked = the_other_run_commits_first

        shown = DeliveryEtaService().record_on_the_way(tap_id)

        assert shown.id == recorded_first["id"]
        assert len(_promises(MODEL_GOOGLE_TAP)) == 1
        assert len(_promises(MODEL_OSRM_SHADOW)) == 1

    def test_a_straight_line_shadow_records_no_time(self, db, sample_user, delivery_driver, google, matrix):
        """Our engine fell back to a straight line (spec 8.4): never recorded as a drive time, even a hidden one."""
        delivery = _order_on_its_way(db, sample_user, delivery_driver)
        _driver_last_seen(db, delivery_driver, at=TAP - timedelta(minutes=2))
        tap = _tap(db, delivery, delivery_driver)
        matrix.source = "haversine"

        shown = DeliveryEtaService().record_on_the_way(tap.id)

        [shadow] = _promises(MODEL_OSRM_SHADOW)
        assert (shadow.source, shadow.expected_at, shadow.shown) == ("haversine", None, False)
        assert ensure_utc(shown.window_end) == TAP + timedelta(minutes=29)  # the customer's promise is Google's

    def test_a_failed_shadow_lookup_never_costs_the_customer_the_promise(
        self, db, sample_user, delivery_driver, google, matrix
    ):
        """The shadow is never shown, so our engine failing must not take Google's answer away from the customer."""
        delivery = _order_on_its_way(db, sample_user, delivery_driver)
        _driver_last_seen(db, delivery_driver, at=TAP - timedelta(minutes=2))
        tap = _tap(db, delivery, delivery_driver)
        matrix.error = RuntimeError("matrix provider exploded")

        shown = DeliveryEtaService().record_on_the_way(tap.id)

        assert (shown.source, ensure_utc(shown.window_end)) == ("google_traffic", TAP + timedelta(minutes=29))
        [shadow] = _promises(MODEL_OSRM_SHADOW)
        assert (shadow.source, shadow.expected_at, shadow.shown) == ("error", None, False)

    def test_a_reassign_row_is_not_a_tap(self, db, sample_user, delivery_driver, google, matrix):
        """Moving an in-transit delivery to another driver keeps it in transit (in_transit -> in_transit)."""
        delivery = _order_on_its_way(db, sample_user, delivery_driver)
        _driver_last_seen(db, delivery_driver, at=TAP - timedelta(minutes=2))
        reassign = _moved(
            db, delivery, DeliveryStatus.IN_TRANSIT, DeliveryStatus.IN_TRANSIT, at=TAP, by=delivery_driver
        )

        assert DeliveryEtaService().record_on_the_way(reassign.id) is None
        assert DeliveryPromise.query.count() == 0
        assert google.requests == [] and matrix.asks == []

    def test_a_picked_up_row_is_not_a_tap(self, db, sample_user, delivery_driver, google, matrix):
        delivery = _order_on_its_way(db, sample_user, delivery_driver, delivery_status=DeliveryStatus.PICKED_UP)
        _driver_last_seen(db, delivery_driver, at=TAP - timedelta(minutes=2))
        picked_up = _moved(db, delivery, DeliveryStatus.ASSIGNED, DeliveryStatus.PICKED_UP, at=TAP, by=delivery_driver)

        assert DeliveryEtaService().record_on_the_way(picked_up.id) is None
        assert DeliveryPromise.query.count() == 0
        assert google.requests == [] and matrix.asks == []

    def test_a_tap_its_driver_did_not_make_earns_no_minutes(
        self, db, sample_user, delivery_driver, admin_user, google, matrix
    ):
        """Spec §4.8 S2: only the delivery's own driver's tap earns minutes. An admin marking the stop in transit says
        neither where the driver is nor that they set off to it, so Google is not asked and no shadow is kept. The
        shown row says why, and the customer is told "On the way" with no minutes. The position is fresh, so only the
        actor withholds the call."""
        delivery = _order_on_its_way(db, sample_user, delivery_driver)
        _driver_last_seen(db, delivery_driver, at=TAP - timedelta(minutes=2))
        tap = _tap(db, delivery, admin_user)

        shown = DeliveryEtaService().record_on_the_way(tap.id)

        assert (shown.source, shown.expected_at, shown.window_end, shown.shown) == ("not_driver_tap", None, None, True)
        assert _promises(MODEL_GOOGLE_TAP) == [shown]
        assert _promises(MODEL_OSRM_SHADOW) == []
        assert google.requests == [] and google.calls_counted == 0
        assert matrix.asks == []
        eta = DeliveryEtaService().customer_eta(delivery.order, now=TAP + timedelta(minutes=5))
        assert eta == CustomerEta(stage="on_the_way")


def _recorded_leg(
    db,
    customer,
    driver,
    i,
    *,
    google_minutes=18.0,
    drive_minutes=23.0,
    recorded_days_ago=None,
    arrived_by=None,
    arrival_from=DeliveryStatus.IN_TRANSIT,
    model_version=MODEL_GOOGLE_TAP,
    shown=True,
    address=None,
):
    """Leg `i`: the driver's in-transit tap, the promise Google's `google_minutes` gave it (none when None), and the
    `arrived` tap `drive_minutes` later. With the defaults the leg's ratio is (23 - 5 off-road) / 18 = 1.0. Returns
    the tap, flushed: its `delivery` is the leg's own. `address` is the order's: Postgres requires one on a delivered
    order (ck_orders_address_required_after_pending), SQLite does not."""
    tap_at = NOW - (timedelta(days=recorded_days_ago) if recorded_days_ago else timedelta(hours=i + 1))
    order = Order(
        user_id=customer.id,
        order_number=f"LEG-{i:04d}",
        status=OrderStatus.DELIVERED,
        subtotal=Decimal("30000"),
        total_amount=Decimal("30000"),
        delivery_address_id=None if address is None else address.id,
    )
    delivery = Delivery(
        order=order,
        delivery_person_id=driver.id,
        status=DeliveryStatus.DELIVERED,
        scheduled_date=tap_at,
        scheduled_time_slot="09:00-12:00",
        tracking_number=f"TRK-LEG-{i:04d}",  # never the generated one: see `_order_on_its_way`
    )
    tap = DeliveryStatusHistory(
        delivery=delivery,
        old_status=DeliveryStatus.PICKED_UP,
        new_status=DeliveryStatus.IN_TRANSIT,
        changed_by=driver.id,
        changed_at=tap_at,
    )
    arrival = DeliveryStatusHistory(
        delivery=delivery,
        old_status=arrival_from,
        new_status=DeliveryStatus.ARRIVED,
        changed_by=(arrived_by or driver).id,
        changed_at=tap_at + timedelta(minutes=drive_minutes),
    )
    db.session.add_all([order, delivery, tap, arrival])
    db.session.flush()
    db.session.add(
        DeliveryPromise(
            delivery_id=delivery.id,
            delivery_status_history_id=tap.id,
            computed_at=tap_at,
            stage="on_the_way",
            expected_at=None if google_minutes is None else tap_at + timedelta(minutes=google_minutes),
            model_version=model_version,
            source="google_traffic",
            shown=shown,
        )
    )
    return tap


def _recorded_legs(db, customer, driver, count, *, start=0, **leg):
    """Legs `start` to `start + count - 1`, committed. Returns their taps."""
    taps = [_recorded_leg(db, customer, driver, i, **leg) for i in range(start, start + count)]
    db.session.commit()
    return taps


@pytest.mark.usefixtures("spec_tunables")
class TestHeadroom:
    def test_headroom_uses_the_seed_below_thirty_legs(self, db, sample_user, delivery_driver):
        _recorded_legs(db, sample_user, delivery_driver, 29)  # learned, it would be (29 x 1.0 + 20 x 1.3) / 49

        assert DeliveryEtaService().headroom(now=NOW) == pytest.approx(1.3)

    def test_headroom_learns_from_recorded_legs(self, db, sample_user, delivery_driver):
        _recorded_legs(db, sample_user, delivery_driver, 40)  # every leg took exactly its Google time: r = 1.0

        assert DeliveryEtaService().headroom(now=NOW) == pytest.approx((40 * 1.0 + 20 * 1.3) / 60)  # 1.1

    @pytest.mark.parametrize(
        "slow_legs, learned",
        [
            # The inclusive p90 of 27 x 1.0 and 3 x 2.0 sits at position 0.9 x 29 = 26.1 of the sorted ratios, between
            # 1.0 and 2.0: 1.1. The median and p80 would learn 1.0 (headroom 1.12); p95, the maximum and the exclusive
            # p90 would learn 2.0 or 1.9 (1.72, 1.66).
            pytest.param(3, 1.1, id="three-slow-legs"),
            # The mean ratio of three slow legs is 1.1 too, so one more slow leg tells it apart: position 26 is now a
            # slow leg, so p90 learns 2.0, where the mean would learn 1.13 (headroom 1.2).
            pytest.param(4, 2.0, id="four-slow-legs"),
        ],
    )
    def test_headroom_learns_the_90th_percentile(self, db, sample_user, delivery_driver, slow_legs, learned):
        """30 legs. Most took Google's time (23 minutes against its 18: r = 1.0); `slow_legs` took twice it (41
        minutes: (41 - 5) / 18 = 2.0). Tapped an hour apart, every drive ends before the driver's next tap, so no leg
        is interrupted. The 90th percentile is learned, interpolated as Postgres's percentile_cont(0.9) is, and blended
        with the seed: (30 x learned + 20 x 1.3) / 50."""
        for i in range(30):
            _recorded_legs(db, sample_user, delivery_driver, 1, start=i, drive_minutes=41.0 if i < slow_legs else 23.0)

        assert DeliveryEtaService().headroom(now=NOW) == pytest.approx((30 * learned + 20 * 1.3) / 50)  # 1.18, 1.72

    def test_headroom_never_drops_below_one(self, db, sample_user, delivery_driver):
        # Every drive took half of Google's time: (14 - 5) / 18 = 0.5, so the blend is (200 x 0.5 + 20 x 1.3) / 220.
        _recorded_legs(db, sample_user, delivery_driver, 200, drive_minutes=14.0)

        assert DeliveryEtaService().headroom(now=NOW) == 1.0

    def test_a_stale_estimate_does_not_teach_headroom(self, db, sample_user, delivery_driver, google, matrix):
        """Thirty stale legs, recorded by the real writer, each a real 23-minute drive that an hour's gap leaves whole.
        Headroom is the factor on Google's time, and a stale leg has none: our own engine priced it, from a position that
        was no longer the driver's. Taught from the engine's 11 minutes, headroom would rise to 1.5."""
        _driver_last_seen(db, delivery_driver, at=NOW - timedelta(days=3))
        for i in range(30):
            delivery = _order_on_its_way(db, sample_user, delivery_driver, number=f"STALE-{i:04d}")
            tapped_at = NOW - timedelta(hours=i + 1)
            shown = DeliveryEtaService().record_on_the_way(_tap(db, delivery, delivery_driver, at=tapped_at).id)
            assert shown.source == "stale_estimate"
            _moved(
                db,
                delivery,
                DeliveryStatus.IN_TRANSIT,
                DeliveryStatus.ARRIVED,
                at=tapped_at + timedelta(minutes=23),
                by=delivery_driver,
            )

        assert DeliveryEtaService().headroom(now=NOW) == SEED

    def test_taps_pressed_together_do_not_teach_headroom(self, db, sample_user, delivery_driver):
        """`arrived` tapped half a minute after `in_transit` is not a drive: it would have been the 30th leg."""
        _recorded_legs(db, sample_user, delivery_driver, 29)
        _recorded_legs(db, sample_user, delivery_driver, 1, start=29, drive_minutes=0.5)

        assert DeliveryEtaService().headroom(now=NOW) == pytest.approx(1.3)

    @pytest.mark.parametrize(
        "second_trip_promised, expected",
        [
            pytest.param(False, 1.3, id="the-later-trip-made-no-promise"),
            pytest.param(True, (30 * 1.0 + 20 * 1.3) / 50, id="the-later-trip-teaches-its-own-leg"),
        ],
    )
    def test_an_arrival_from_a_later_trip_does_not_teach_headroom(
        self, db, sample_user, delivery_driver, second_trip_promised, expected
    ):
        """Out with Google's 18 minutes, failed, out again an hour later, and arrived 23 minutes after that. The
        arrival ends the second trip, so it may teach the second tap's leg (r = 1.0) but never the first tap's
        (83 minutes, r = 4.3). Paired with the first tap, it would be the 30th leg, or the 31st."""
        _recorded_legs(db, sample_user, delivery_driver, 29)
        delivery = _order_on_its_way(db, sample_user, delivery_driver, delivery_status=DeliveryStatus.ARRIVED)
        out_at = NOW - timedelta(days=CLEAR_OF_THE_HOURLY_LEGS)  # else a clean leg's tap masks the next-tap rule
        first_tap = _tap(db, delivery, delivery_driver, at=out_at)
        _promise_of(db, first_tap, window_end=out_at + timedelta(minutes=29))
        for old, new, minutes in (
            (DeliveryStatus.IN_TRANSIT, DeliveryStatus.FAILED, 20),
            (DeliveryStatus.FAILED, DeliveryStatus.ASSIGNED, 40),  # re-dispatched
            (DeliveryStatus.ASSIGNED, DeliveryStatus.PICKED_UP, 50),
        ):
            _moved(db, delivery, old, new, at=out_at + timedelta(minutes=minutes), by=delivery_driver)
        second_tap = _tap(db, delivery, delivery_driver, at=out_at + timedelta(minutes=60))
        if second_trip_promised:
            _promise_of(db, second_tap, window_end=out_at + timedelta(minutes=89))
        _moved(
            db,
            delivery,
            DeliveryStatus.IN_TRANSIT,
            DeliveryStatus.ARRIVED,
            at=out_at + timedelta(minutes=83),
            by=delivery_driver,
        )

        assert DeliveryEtaService().headroom(now=NOW) == pytest.approx(expected)

    @pytest.mark.parametrize(
        "thirtieth_leg, expected",
        [
            pytest.param({}, (30 * 1.0 + 20 * 1.3) / 50, id="a-real-leg-counts"),
            pytest.param({"closed_by_someone_else": True}, 1.3, id="arrival-tapped-by-someone-else"),
            pytest.param({"arrival_from": DeliveryStatus.ARRIVED}, 1.3, id="only-a-reassign-arrived-row"),
            pytest.param(  # clear of the hourly legs, whose taps would interrupt it and mask the 180-minute bound
                {"drive_minutes": 181.0, "recorded_days_ago": CLEAR_OF_THE_HOURLY_LEGS}, 1.3, id="over-three-hours"
            ),
            pytest.param({"google_minutes": 0.5}, 1.3, id="google-under-a-minute"),
            pytest.param({"google_minutes": None}, 1.3, id="a-promise-without-minutes"),
            pytest.param({"recorded_days_ago": 29}, 1.3, id="older-than-four-weeks"),
            pytest.param({"model_version": MODEL_OSRM_SHADOW, "shown": False}, 1.3, id="the-hidden-shadow"),
        ],
    )
    def test_only_real_recorded_legs_teach_headroom(
        self, db, sample_user, delivery_driver, admin_user, thirtieth_leg, expected
    ):
        """29 real legs and one more: only a shown Google promise, recorded in the last four weeks, with the
        delivery's own driver's real arrival 1 to 180 minutes after the tap, is the 30th leg that starts learning."""
        leg = dict(thirtieth_leg)
        if leg.pop("closed_by_someone_else", False):
            leg["arrived_by"] = admin_user
        _recorded_legs(db, sample_user, delivery_driver, 29)
        _recorded_legs(db, sample_user, delivery_driver, 1, start=29, **leg)

        assert DeliveryEtaService().headroom(now=NOW) == pytest.approx(expected)

    @pytest.mark.parametrize(
        "stop, by, old_status, new_status, minutes_after_the_tap, expected",
        [
            pytest.param(
                "another stop of its driver",
                "its driver",
                DeliveryStatus.PICKED_UP,
                DeliveryStatus.IN_TRANSIT,
                3,
                SEED,
                id="same-driver-sets-off-elsewhere",
            ),
            pytest.param(
                "its own delivery",
                "an admin",
                DeliveryStatus.IN_TRANSIT,
                DeliveryStatus.IN_TRANSIT,
                3,
                SEED,
                id="hand-over-inside-the-leg",
            ),
            pytest.param(
                "its own delivery",
                "an admin",
                DeliveryStatus.IN_TRANSIT,
                DeliveryStatus.IN_TRANSIT,
                90,
                THIRTY_LEGS,
                id="hand-over-after-the-arrival",
            ),
            pytest.param(
                "a stop of another driver",
                "another driver",
                DeliveryStatus.PICKED_UP,
                DeliveryStatus.IN_TRANSIT,
                3,
                THIRTY_LEGS,
                id="another-driver-sets-off",
            ),
            pytest.param(
                "another stop of its driver",
                "its driver",
                DeliveryStatus.PICKED_UP,
                DeliveryStatus.IN_TRANSIT,
                90,
                THIRTY_LEGS,
                id="same-driver-sets-off-after-the-arrival",
            ),
            pytest.param(
                "another stop of its driver",
                "its driver",
                DeliveryStatus.PICKED_UP,
                DeliveryStatus.IN_TRANSIT,
                0,
                THIRTY_LEGS,
                id="same-driver-sets-off-at-the-same-instant",
            ),
            pytest.param(
                "another stop of its driver",
                "its driver",
                DeliveryStatus.PICKED_UP,
                DeliveryStatus.IN_TRANSIT,
                83,
                THIRTY_LEGS,
                id="same-driver-sets-off-at-the-arrival-instant",
            ),
            # Stamped with the driver as its actor, so only "a real move into in_transit" keeps it out.
            pytest.param(
                "another stop of its driver",
                "its driver",
                DeliveryStatus.IN_TRANSIT,
                DeliveryStatus.IN_TRANSIT,
                3,
                THIRTY_LEGS,
                id="a-reassign-row-on-another-stop",
            ),
            pytest.param(
                "another stop of its driver",
                "its driver",
                DeliveryStatus.IN_TRANSIT,
                DeliveryStatus.ARRIVED,
                3,
                THIRTY_LEGS,
                id="same-driver-arrives-at-another-stop",
            ),
            pytest.param(
                "another stop of its driver",
                "an admin",
                DeliveryStatus.PICKED_UP,
                DeliveryStatus.IN_TRANSIT,
                3,
                THIRTY_LEGS,
                id="an-admin-marks-another-stop-of-its-driver",
            ),
        ],
    )
    def test_an_interrupted_leg_does_not_teach_headroom(
        self,
        db,
        sample_user,
        delivery_driver,
        second_delivery_driver,
        admin_user,
        stop,
        by,
        old_status,
        new_status,
        minutes_after_the_tap,
        expected,
    ):
        """Spec §4.8 S2: a leg interrupted between its tap and its arrival was not one drive. 29 real legs, and a 30th
        that arrived 83 minutes after its tap against Google's 18: r = (83 - 5) / 18 = 4.33. Learned from, it is the
        30th leg and headroom becomes THIRTY_LEGS, 1.12. Interrupted, it teaches nothing, and the SEED, 1.3, stands.

        Each case adds one status row, `minutes_after_the_tap` after the 30th leg's tap (it arrived at +83), made by
        `by`. The row goes on the leg's own delivery, on another stop of its driver, or on a stop of another driver.
        Another stop has no promise of its own, so it never adds a leg. Interrupting: a real in_transit tap by the
        leg's driver on another stop, or a hand-over of the leg's own delivery, strictly between the tap and the
        arrival."""
        _recorded_legs(db, sample_user, delivery_driver, 29)
        [tap] = _recorded_legs(
            db,
            sample_user,
            delivery_driver,
            1,
            start=29,
            drive_minutes=83.0,
            recorded_days_ago=CLEAR_OF_THE_HOURLY_LEGS,
        )
        stops = {  # an unknown key raises: a typo cannot quietly build some other stop
            "its own delivery": lambda: tap.delivery,
            "another stop of its driver": lambda: _order_on_its_way(db, sample_user, delivery_driver),
            "a stop of another driver": lambda: _order_on_its_way(db, sample_user, second_delivery_driver),
        }
        people = {"its driver": delivery_driver, "another driver": second_delivery_driver, "an admin": admin_user}
        at = ensure_utc(tap.changed_at) + timedelta(minutes=minutes_after_the_tap)
        _moved(db, stops[stop](), old_status, new_status, at=at, by=people[by])

        assert DeliveryEtaService().headroom(now=NOW) == pytest.approx(expected)


@pytest.mark.usefixtures("spec_tunables")
class TestWhatTheCustomerIsTold:
    def test_on_the_way_counts_down_to_the_by_time(self, db, sample_user, delivery_driver, google, matrix):
        """Recorded by the real writer, read four minutes after the tap: by = tap + 29 = now + 25 minutes."""
        delivery = _order_on_its_way(db, sample_user, delivery_driver)
        _driver_last_seen(db, delivery_driver, at=TAP - timedelta(minutes=2))
        DeliveryEtaService().record_on_the_way(_tap(db, delivery, delivery_driver).id)

        eta = DeliveryEtaService().customer_eta(delivery.order, now=TAP + timedelta(minutes=4))

        assert eta == CustomerEta(
            stage="on_the_way",
            by=TAP + timedelta(minutes=29),
            by_time="14:29",
            minutes_left=25,
            attribution="google_maps",
        )

    def test_a_stale_estimate_counts_down_with_no_google_credit(self, db, sample_user, delivery_driver, google, matrix):
        """Recorded by the real writer, read four minutes after the tap: by = tap + 41 = now + 37 minutes. Google gave
        none of it, so nothing credits Google."""
        delivery = _order_on_its_way(db, sample_user, delivery_driver)
        _driver_last_seen(db, delivery_driver, at=TAP - timedelta(minutes=20))
        DeliveryEtaService().record_on_the_way(_tap(db, delivery, delivery_driver).id)

        eta = DeliveryEtaService().customer_eta(delivery.order, now=TAP + timedelta(minutes=4))

        assert eta == CustomerEta(
            stage="on_the_way",
            by=TAP + timedelta(minutes=41),
            by_time="14:41",
            minutes_left=37,
            attribution=None,
        )

    def test_a_passed_by_time_reads_zero_minutes(self, db, sample_user, delivery_driver, google, matrix):
        delivery = _order_on_its_way(db, sample_user, delivery_driver)
        _driver_last_seen(db, delivery_driver, at=TAP - timedelta(minutes=2))
        DeliveryEtaService().record_on_the_way(_tap(db, delivery, delivery_driver).id)

        eta = DeliveryEtaService().customer_eta(delivery.order, now=TAP + timedelta(minutes=35))

        assert (eta.stage, eta.by_time, eta.minutes_left) == ("on_the_way", "14:29", 0)

    def test_an_earlier_trips_promise_never_shows_on_a_new_trip(self, db, sample_user, delivery_driver):
        """Review Focus #1: out once with a promise, failed, out again. The new trip's tap has no promise yet, and
        the old trip's must not stand in for it."""
        delivery = _order_on_its_way(db, sample_user, delivery_driver)
        first_trip = _tap(db, delivery, delivery_driver, at=NOW - timedelta(hours=3))
        _promise_of(db, first_trip, window_end=NOW - timedelta(hours=3) + timedelta(minutes=29))
        _moved(
            db,
            delivery,
            DeliveryStatus.IN_TRANSIT,
            DeliveryStatus.FAILED,
            at=NOW - timedelta(hours=2),
            by=delivery_driver,
        )
        _moved(
            db,
            delivery,
            DeliveryStatus.FAILED,
            DeliveryStatus.ASSIGNED,
            at=NOW - timedelta(minutes=30),
            by=delivery_driver,
        )
        _moved(
            db,
            delivery,
            DeliveryStatus.ASSIGNED,
            DeliveryStatus.PICKED_UP,
            at=NOW - timedelta(minutes=20),
            by=delivery_driver,
        )
        _tap(db, delivery, delivery_driver, at=NOW - timedelta(minutes=10))

        eta = DeliveryEtaService().customer_eta(delivery.order, now=NOW)

        assert eta == CustomerEta(stage="on_the_way")

    def test_a_handover_to_another_driver_drops_the_minutes(
        self, app, db, sample_user, delivery_driver, second_delivery_driver, admin_user
    ):
        """An admin hands the delivery to another driver while it is on its way, through the one assign primitive.
        It stays in transit (an in_transit -> in_transit row), and the promise worked out from the first driver's
        position no longer stands: "on the way", with no minutes."""
        delivery = _order_on_its_way(db, sample_user, delivery_driver)
        tapped_at = datetime.now(UTC) - timedelta(minutes=10)  # the hand-over is stamped with the real clock
        tap = _tap(db, delivery, delivery_driver, at=tapped_at)
        _promise_of(db, tap, window_end=tapped_at + timedelta(minutes=29))
        before = DeliveryEtaService().customer_eta(delivery.order, now=tapped_at + timedelta(minutes=5))
        assert (before.minutes_left, before.attribution) == (24, "google_maps")  # stands until the hand-over

        with app.test_request_context():
            DeliveryAssignmentService.assign_driver(
                delivery.id,
                driver_user_id=second_delivery_driver.id,
                actor_id=admin_user.id,
                source=AssignmentSource.REASSIGN,
                allow_in_progress=True,
            )

        handover = DeliveryStatusHistory.query.filter_by(delivery_id=delivery.id, changed_by=admin_user.id).one()
        assert (handover.old_status, handover.new_status) == (DeliveryStatus.IN_TRANSIT, DeliveryStatus.IN_TRANSIT)
        eta = DeliveryEtaService().customer_eta(delivery.order, now=tapped_at + timedelta(minutes=12))
        assert eta == CustomerEta(stage="on_the_way")

    def test_a_tap_on_another_stop_ends_this_stops_promise(self, db, sample_user, delivery_driver):
        """Spec §4.8 S2: the driver set off to stop B while stop A was still on its way. Drivers reach the newer stop
        first (prod: 90-97% of the time), so A's promise no longer stands: "on the way", with no minutes. B's own
        promise stands."""
        stop_a = _order_on_its_way(db, sample_user, delivery_driver)
        _promise_of(db, _tap(db, stop_a, delivery_driver), window_end=TAP + timedelta(minutes=29))
        reading = TAP + timedelta(minutes=5)
        assert DeliveryEtaService().customer_eta(stop_a.order, now=reading).minutes_left == 24  # stands until B's tap

        stop_b = _order_on_its_way(db, sample_user, delivery_driver, number="ETA-0002")
        tap_b = _tap(db, stop_b, delivery_driver, at=TAP + timedelta(minutes=3))
        _promise_of(db, tap_b, window_end=TAP + timedelta(minutes=3 + 29))

        assert DeliveryEtaService().customer_eta(stop_a.order, now=reading) == CustomerEta(stage="on_the_way")
        eta_b = DeliveryEtaService().customer_eta(stop_b.order, now=reading)
        assert (eta_b.minutes_left, eta_b.attribution) == (27, "google_maps")

    def test_another_drivers_tap_leaves_the_promise(self, db, sample_user, delivery_driver, second_delivery_driver):
        """Another driver setting off to a stop of their own says nothing about this one's arrival."""
        stop_a = _order_on_its_way(db, sample_user, delivery_driver)
        _promise_of(db, _tap(db, stop_a, delivery_driver), window_end=TAP + timedelta(minutes=29))
        their_stop = _order_on_its_way(db, sample_user, second_delivery_driver, number="ETA-0002")
        _tap(db, their_stop, second_delivery_driver, at=TAP + timedelta(minutes=3))

        eta = DeliveryEtaService().customer_eta(stop_a.order, now=TAP + timedelta(minutes=5))

        assert (eta.minutes_left, eta.attribution) == (24, "google_maps")

    def test_an_earlier_tap_on_another_stop_leaves_the_promise(self, db, sample_user, delivery_driver):
        """The driver set off to stop B ten minutes BEFORE stop A. A is the newer stop, the one the driver goes to
        first, so its promise stands."""
        stop_b = _order_on_its_way(db, sample_user, delivery_driver, number="ETA-0002")
        _tap(db, stop_b, delivery_driver, at=TAP - timedelta(minutes=10))
        stop_a = _order_on_its_way(db, sample_user, delivery_driver)
        _promise_of(db, _tap(db, stop_a, delivery_driver), window_end=TAP + timedelta(minutes=29))

        eta = DeliveryEtaService().customer_eta(stop_a.order, now=TAP + timedelta(minutes=5))

        assert eta.minutes_left == 24

    def test_an_ended_promise_stays_ended_when_the_other_stop_changes_hands(
        self, app, db, sample_user, delivery_driver, second_delivery_driver, admin_user
    ):
        """What ends a promise is the driver's own tap on another stop: a history row, which never changes. Driver X
        sets off to A, then to C, so A's promise ends. An admin then hands C, still on its way, to driver Y, through the
        one assign primitive. A's promise stays ended. Matching C by its current driver instead would bring A's stale
        minutes back."""
        tapped_at = datetime.now(UTC) - timedelta(minutes=10)  # the hand-over is stamped with the real clock
        stop_a = _order_on_its_way(db, sample_user, delivery_driver)
        _promise_of(db, _tap(db, stop_a, delivery_driver, at=tapped_at), window_end=tapped_at + timedelta(minutes=29))
        stop_c = _order_on_its_way(db, sample_user, delivery_driver, number="ETA-0002")
        _tap(db, stop_c, delivery_driver, at=tapped_at + timedelta(minutes=3))
        reading = tapped_at + timedelta(minutes=5)
        assert DeliveryEtaService().customer_eta(stop_a.order, now=reading) == CustomerEta(stage="on_the_way")

        with app.test_request_context():
            DeliveryAssignmentService.assign_driver(
                stop_c.id,
                driver_user_id=second_delivery_driver.id,
                actor_id=admin_user.id,
                source=AssignmentSource.REASSIGN,
                allow_in_progress=True,
            )

        assert db.session.get(Delivery, stop_c.id).delivery_person_id == second_delivery_driver.id
        assert DeliveryEtaService().customer_eta(stop_a.order, now=reading) == CustomerEta(stage="on_the_way")

    def test_an_admin_marking_the_drivers_other_stop_leaves_the_promise(
        self, db, sample_user, delivery_driver, admin_user
    ):
        """Only the driver's own tap ends a promise. An admin marking another of the driver's stops in transit, which
        no prod tap has done, leaves this one standing: the known cost of anchoring on who tapped."""
        stop_a = _order_on_its_way(db, sample_user, delivery_driver)
        _promise_of(db, _tap(db, stop_a, delivery_driver), window_end=TAP + timedelta(minutes=29))
        stop_c = _order_on_its_way(db, sample_user, delivery_driver, number="ETA-0002")
        _moved(
            db,
            stop_c,
            DeliveryStatus.PICKED_UP,
            DeliveryStatus.IN_TRANSIT,
            at=TAP + timedelta(minutes=3),
            by=admin_user,
        )

        eta = DeliveryEtaService().customer_eta(stop_a.order, now=TAP + timedelta(minutes=5))

        assert (eta.minutes_left, eta.attribution) == (24, "google_maps")

    def test_the_drivers_tap_elsewhere_ends_a_promise_an_admin_set_off(
        self, db, sample_user, delivery_driver, admin_user
    ):
        """The interrupting tap is matched against the stop's driver, not against whoever tapped the stop. An admin
        marked A in transit for its driver; the driver's own tap on another stop ends A's promise all the same."""
        stop_a = _order_on_its_way(db, sample_user, delivery_driver)
        _promise_of(db, _tap(db, stop_a, admin_user), window_end=TAP + timedelta(minutes=29))
        stop_c = _order_on_its_way(db, sample_user, delivery_driver, number="ETA-0002")
        _tap(db, stop_c, delivery_driver, at=TAP + timedelta(minutes=3))

        eta = DeliveryEtaService().customer_eta(stop_a.order, now=TAP + timedelta(minutes=5))

        assert eta == CustomerEta(stage="on_the_way")

    def test_a_hand_over_before_the_tap_leaves_the_promise(self, db, sample_user, delivery_driver, admin_user):
        """Only rows after the latest tap can end its promise. A hand-over before it, on an earlier trip, cannot."""
        stop_a = _order_on_its_way(db, sample_user, delivery_driver)
        _moved(
            db,
            stop_a,
            DeliveryStatus.IN_TRANSIT,
            DeliveryStatus.IN_TRANSIT,
            at=TAP - timedelta(minutes=10),
            by=admin_user,
        )
        _promise_of(db, _tap(db, stop_a, delivery_driver), window_end=TAP + timedelta(minutes=29))

        eta = DeliveryEtaService().customer_eta(stop_a.order, now=TAP + timedelta(minutes=5))

        assert eta.minutes_left == 24

    def test_arriving_at_another_stop_leaves_the_promise(self, db, sample_user, delivery_driver):
        """The driver set off to stop B, then to stop A, and reached B first, which happens 3-10% of the time. Arriving
        at B is not setting off anywhere, so A's promise stands."""
        stop_b = _order_on_its_way(db, sample_user, delivery_driver, number="ETA-0002")
        _tap(db, stop_b, delivery_driver, at=TAP - timedelta(minutes=10))
        stop_a = _order_on_its_way(db, sample_user, delivery_driver)
        _promise_of(db, _tap(db, stop_a, delivery_driver), window_end=TAP + timedelta(minutes=29))
        _moved(
            db,
            stop_b,
            DeliveryStatus.IN_TRANSIT,
            DeliveryStatus.ARRIVED,
            at=TAP + timedelta(minutes=3),
            by=delivery_driver,
        )

        eta = DeliveryEtaService().customer_eta(stop_a.order, now=TAP + timedelta(minutes=5))

        assert eta.minutes_left == 24

    def test_arrived_publishes_the_arrived_stage_only(self, db, sample_user, delivery_driver):
        delivery = _order_on_its_way(db, sample_user, delivery_driver, delivery_status=DeliveryStatus.ARRIVED)
        tap = _tap(db, delivery, delivery_driver, at=NOW - timedelta(minutes=20))
        _promise_of(db, tap, window_end=NOW + timedelta(minutes=9))
        _moved(db, delivery, DeliveryStatus.IN_TRANSIT, DeliveryStatus.ARRIVED, at=NOW, by=delivery_driver)

        eta = DeliveryEtaService().customer_eta(delivery.order, now=NOW)

        assert eta == CustomerEta(stage="arrived")

    @pytest.mark.parametrize(
        "delivery_status, order_status",
        [
            pytest.param(None, OrderStatus.CONFIRMED, id="no-delivery-yet"),
            pytest.param(DeliveryStatus.SCHEDULED, OrderStatus.CONFIRMED, id="scheduled"),
            pytest.param(DeliveryStatus.ASSIGNED, OrderStatus.CONFIRMED, id="assigned"),
            pytest.param(DeliveryStatus.PICKED_UP, OrderStatus.OUT_FOR_DELIVERY, id="picked-up"),
            pytest.param(DeliveryStatus.DELIVERED, OrderStatus.DELIVERED, id="delivered"),
            pytest.param(DeliveryStatus.CANCELLED, OrderStatus.CANCELLED, id="cancelled"),
        ],
    )
    def test_no_time_before_the_driver_sets_off(self, db, sample_user, delivery_driver, delivery_status, order_status):
        """No time is shown outside the trip, even with an earlier tap's promise on record."""
        if delivery_status is None:  # a dated order not released to drivers yet has no delivery row
            order = _order_to(db, sample_user, status=order_status)
        else:
            delivery = _order_on_its_way(
                db, sample_user, delivery_driver, delivery_status=delivery_status, order_status=order_status
            )
            tap = _tap(db, delivery, delivery_driver, at=NOW - timedelta(minutes=20))
            _promise_of(db, tap, window_end=NOW + timedelta(minutes=9))
            order = delivery.order

        eta = DeliveryEtaService().customer_eta(order, now=NOW)

        assert eta == CustomerEta()

    def test_awaiting_a_new_date_shows_no_time(self, db, sample_user, delivery_driver):
        """F15: a failed delivery waits for a staff-picked date, and nothing is on its way until then."""
        delivery = _order_on_its_way(db, sample_user, delivery_driver, delivery_status=DeliveryStatus.FAILED)
        tap = _tap(db, delivery, delivery_driver, at=NOW - timedelta(minutes=20))
        _promise_of(db, tap, window_end=NOW + timedelta(minutes=9))
        _moved(
            db,
            delivery,
            DeliveryStatus.IN_TRANSIT,
            DeliveryStatus.FAILED,
            at=NOW - timedelta(minutes=5),
            by=delivery_driver,
        )
        assert OrderScheduleService.awaiting_new_date(delivery.order) is True

        eta = DeliveryEtaService().customer_eta(delivery.order, now=NOW)

        assert eta == CustomerEta()

    @pytest.mark.parametrize(
        "stored_by",
        [
            pytest.param(datetime(2026, 11, 3, 21, 30, tzinfo=UTC), id="aware"),
            pytest.param(datetime(2026, 11, 3, 21, 30), id="naive-as-sqlite-returns-it"),
        ],
    )
    def test_by_time_is_tashkent_clock_across_midnight(self, db, sample_user, delivery_driver, stored_by):
        """21:30 UTC on 3 November is 02:30 on 4 November in Tashkent (UTC+5)."""
        delivery = _order_on_its_way(db, sample_user, delivery_driver)
        tap = _tap(db, delivery, delivery_driver, at=datetime(2026, 11, 3, 21, 1, tzinfo=UTC))
        _promise_of(db, tap, window_end=stored_by)

        eta = DeliveryEtaService().customer_eta(delivery.order, now=datetime(2026, 11, 3, 21, 10, tzinfo=UTC))

        assert eta.by_time == "02:30"
        assert eta.by == datetime(2026, 11, 3, 21, 30, tzinfo=UTC)
        assert eta.by.utcoffset() == timedelta(0)  # published as aware UTC, whatever the column handed back
        assert eta.minutes_left == 20


def _statements_sent_by(db, call):
    """The ORM statements `call` sends, as SQLAlchemy objects: what each reader really runs, to compile for any
    dialect."""
    sent = []

    def record(execute_state):
        sent.append(execute_state.statement)

    session = db.session()
    event.listen(session, "do_orm_execute", record)
    try:
        call()
    finally:
        event.remove(session, "do_orm_execute", record)
    return sent


def _outside_parentheses(sql, separator):
    """`sql` split at each `separator` that stands outside every parenthesis."""
    parts, depth, start, at = [], 0, 0, 0
    while at < len(sql):
        if sql[at] == "(":
            depth += 1
        elif sql[at] == ")":
            depth -= 1
        elif depth == 0 and sql.startswith(separator, at):
            parts.append(sql[start:at])
            at += len(separator)
            start = at
            continue
        at += 1
    parts.append(sql[start:])
    return parts


# One conjunct of the rule: a NOT EXISTS over one history alias, with the FROM it ranges over.
_NOT_EXISTS = re.compile(r"NOT \(EXISTS \(SELECT (?P<alias>\w+)\.id FROM (?P<froms>.+?) WHERE ")


@pytest.mark.usefixtures("spec_tunables")
class TestThePostgresPlanShape:
    """The rule's SQL shape matters on Postgres alone, and the SQLite suite cannot see it. These tests compile both
    readers' real statements for Postgres.

    Shape. Postgres turns a NOT EXISTS that is a top-level conjunct of a WHERE into an anti-join, keyed on the
    subquery's equality with the outer row. One EXISTS of (a) OR (b), or a NOT over an OR of two EXISTS, is not turned
    into one: it is checked leg by leg against in-transit rows.

    Correlation (F3). Each subquery's FROM is its history alias alone; the tap and its delivery come from the
    enclosing query. A `deliveries` of the subquery's own would range over every delivery, so (b) would no longer be
    scoped to the tapped delivery's driver. The rule's first draft carried one, a tapped `deliveries` inside an OR, and
    planned as a 192M-row hash on seven times prod's data."""

    def test_both_readers_send_the_rule_as_two_not_exists_joined_by_and(self, db, sample_user, delivery_driver):
        delivery = _order_on_its_way(db, sample_user, delivery_driver)
        _promise_of(db, _tap(db, delivery, delivery_driver), window_end=TAP + timedelta(minutes=29))

        sent = _statements_sent_by(
            db,
            lambda: (
                DeliveryEtaService().customer_eta(delivery.order, now=TAP),
                DeliveryEtaService().headroom(now=NOW),
            ),
        )

        compiled = [" ".join(str(statement.compile(dialect=postgresql.dialect())).split()) for statement in sent]
        latest_tap, legs = [sql for sql in compiled if "elsewhere" in sql]
        # customer_eta: the rule is the latest tap's `uninterrupted` column.
        select_list = _outside_parentheses(latest_tap.removeprefix("SELECT "), " FROM ")[0]
        [column] = [item for item in _outside_parentheses(select_list, ", ") if item.endswith(" AS uninterrupted")]
        rule_on_track = _outside_parentheses(column.removesuffix(" AS uninterrupted"), " AND ")
        # _leg_ratios: the rule's two conjuncts stand at the top of the legs query's WHERE, beside its own filters.
        [_, where] = _outside_parentheses(legs, " WHERE ")
        rule_in_headroom = [part for part in _outside_parentheses(where, " AND ") if part.startswith("NOT (EXISTS")]
        for rule, statement in ((rule_on_track, latest_tap), (rule_in_headroom, legs)):
            assert " OR " not in statement, statement
            shapes = [_NOT_EXISTS.match(conjunct) for conjunct in rule]
            assert len(rule) == 2 and all(shapes), rule
            # Each subquery ranges over the history alone: the tap, and its delivery's driver, are correlated.
            assert {shape["alias"]: shape["froms"] for shape in shapes} == {
                "handover": "delivery_status_history AS handover",
                "elsewhere": "delivery_status_history AS elsewhere",
            }


def test_as_payload_publishes_five_fields():
    on_the_way = CustomerEta(
        stage="on_the_way",
        by=datetime(2026, 11, 3, 9, 29, tzinfo=UTC),
        by_time="14:29",
        minutes_left=25,
        attribution="google_maps",
    )

    assert on_the_way.as_payload() == {
        "eta_stage": "on_the_way",
        "eta_by": "2026-11-03T09:29:00+00:00",
        "eta_by_time": "14:29",
        "eta_minutes_left": 25,
        "eta_attribution": "google_maps",
    }
    assert CustomerEta().as_payload() == {
        "eta_stage": None,
        "eta_by": None,
        "eta_by_time": None,
        "eta_minutes_left": None,
        "eta_attribution": None,
    }


def test_the_eta_tunables_default_to_the_spec_values(app):
    """Defined once, in business_config (with its type: a seed of 1.3 read through `_int` would not load), and only
    derived by the app config. No `spec_tunables` here: it would set the very keys this test reads."""
    for name, value in SPEC_TUNABLES.items():
        default = getattr(business_config, name)
        assert (default, type(default)) == (value, type(value)), name
        assert app.config[name] == default, name
