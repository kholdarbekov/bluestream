"""Auto-assign judges FREE FULL bottles, and a capacity gain re-offers the pool (spec A8, T4).

Driven through the routes the clients call: the customer's order endpoint, the staff bot's session routes (open, invite,
approve a join request). Faked, as external I/O only: Celery publishing (each `.delay` bound to a signature-enforcing
recorder, so the follow-up task runs here only when a test runs it) and the driver push / route rebuild.
"""

from datetime import UTC, datetime, timedelta
from typing import List, Tuple

import pytest

from business_app.models.bottle import DriverBottleSession
from business_app.models.delivery import Delivery
from business_app.models.order import Order
from business_app.services.bottle_tracking_service import BottleTrackingService
from business_app.services.delivery_service import DeliveryService
from business_app.tasks.delivery_monitoring_tasks import reenqueue_stale_pool_deliveries, reoffer_pool_deliveries_task
from business_app.tasks.delivery_tasks import auto_assign_delivery_task
from shared.enums import DeliveryStatus, DriverBottleSessionStatus, OrderStatus
from tests.integration.contract_pricing_factory import customer_headers
from tests.integration.eta_plan_builders import make_driver, returning_customer
from tests.integration.tier_discount_factory import post_order

pytestmark = pytest.mark.integration

OPEN_SESSION = "/api/v1/staff/bottles/session/open"
INVITE = "/api/v1/staff/bottles/session/invite"
APPROVE = "/api/v1/staff/bottles/session/join-request/approve"
ALL_DAY = ("00:00", "23:59")


@pytest.fixture
def reoffers(monkeypatch) -> List[Tuple]:
    """Every `reoffer_pool_deliveries_task.delay(...)`, as the args it was called with. The task takes none."""
    calls: List[Tuple] = []

    def delay():
        calls.append(())

    monkeypatch.setattr(reoffer_pool_deliveries_task, "delay", delay)
    return calls


@pytest.fixture
def queued_assigns(monkeypatch) -> List[int]:
    """Every delivery id `auto_assign_delivery_task.delay` was given."""
    queued: List[int] = []
    monkeypatch.setattr(auto_assign_delivery_task, "delay", lambda delivery_id: queued.append(delivery_id))
    return queued


@pytest.fixture(autouse=True)
def no_driver_push(monkeypatch):
    monkeypatch.setattr(DeliveryService, "_notify_driver", lambda self, delivery: None)
    monkeypatch.setattr(DeliveryService, "_optimize_driver_route", lambda self, *a, **k: None)


def _driver(db):
    return make_driver(db, roster=ALL_DAY)


def _loads(app, driver, bottles):
    return app.test_client().post(OPEN_SESSION, json={"bottles_loaded": bottles}, headers=customer_headers(app, driver))


def _orders(app, db, customer, address, product, quantity) -> Order:
    """The customer's confirmed cash order of `quantity` bottles, placed through `POST /api/v1/orders/`."""
    product.tracks_returnable_bottles = True
    product.returnable_bottles_per_unit = 1
    db.session.commit()
    returning_customer(db, customer, address)
    response = post_order(
        app, customer_headers(app, customer), product_id=product.id, address_id=address.id,
        payment_method="cash", quantity=quantity,
    )
    assert response.status_code == 201, response.get_data(as_text=True)
    db.session.expire_all()
    order = db.session.get(Order, response.get_json()["data"]["order"]["id"])
    assert (order.status, order.delivery.status) == (OrderStatus.CONFIRMED, DeliveryStatus.SCHEDULED)
    return order


def test_opening_a_session_reoffers_the_pool_once_after_commit(app, db, reoffers):
    driver = _driver(db)

    response = _loads(app, driver, 10)

    assert response.status_code == 201, response.get_data(as_text=True)
    assert reoffers == [()]
    assert DriverBottleSession.query.filter_by(driver_user_id=driver.id).one().status == DriverBottleSessionStatus.OPEN


def test_a_refused_open_reoffers_nothing(app, db, reoffers):
    driver = _driver(db)
    assert _loads(app, driver, 10).status_code == 201
    reoffers.clear()

    again = _loads(app, driver, 5)  # already open: refused and rolled back

    assert again.status_code == 409
    assert reoffers == []


def test_inviting_a_codriver_reoffers_the_pool_once(app, db, reoffers):
    owner, member = _driver(db), _driver(db)
    assert _loads(app, owner, 10).status_code == 201
    reoffers.clear()

    response = app.test_client().post(INVITE, json={"member_driver_id": member.id}, headers=customer_headers(app, owner))

    assert response.status_code == 201, response.get_data(as_text=True)
    assert reoffers == [()]


def test_approving_a_join_request_reoffers_the_pool_once(app, db, reoffers):
    owner, requester = _driver(db), _driver(db)
    assert _loads(app, owner, 10).status_code == 201
    session = DriverBottleSession.query.filter_by(driver_user_id=owner.id).one()
    reoffers.clear()

    response = app.test_client().post(
        APPROVE, json={"session_id": session.id, "requester_id": requester.id}, headers=customer_headers(app, owner)
    )

    assert response.status_code == 200, response.get_data(as_text=True)
    assert reoffers == [()]


def test_join_session_reoffers_pool(app, db, reoffers):
    owner, member = _driver(db), _driver(db)
    assert _loads(app, owner, 10).status_code == 201
    session = DriverBottleSession.query.filter_by(driver_user_id=owner.id).one()
    reoffers.clear()

    with app.test_request_context():
        BottleTrackingService().join_session(member.id, session.id)

    assert reoffers == [()]


def test_a_refused_join_reoffers_nothing(app, db, reoffers):
    owner, member = _driver(db), _driver(db)
    assert _loads(app, owner, 10).status_code == 201
    assert _loads(app, member, 4).status_code == 201  # has a session of their own: cannot join
    session = DriverBottleSession.query.filter_by(driver_user_id=owner.id).one()
    reoffers.clear()

    with app.test_request_context(), pytest.raises(Exception) as refused:
        BottleTrackingService().join_session(member.id, session.id)

    assert getattr(refused.value, "error_code", None) == "BOTTLE_SESSION_ALREADY_OPEN"
    assert reoffers == []


def test_reoffer_has_no_age_floor_and_returns_the_count(app, db, queued_assigns, sample_user, user_address, sample_product):
    fresh = _orders(app, db, sample_user, user_address, sample_product, quantity=2)
    held = Delivery.query.filter_by(order_id=fresh.id).one()

    count = reoffer_pool_deliveries_task.run()

    assert count == 1
    assert queued_assigns == [held.id]


def test_stale_sweep_keeps_its_age_floor(app, db, queued_assigns, sample_user, user_address, sample_product):
    _orders(app, db, sample_user, user_address, sample_product, quantity=2)  # created a moment ago

    result = reenqueue_stale_pool_deliveries.run()

    assert result == {"reenqueued_count": 0, "delivery_ids": []}
    assert queued_assigns == []


def test_a_lone_empties_driver_leaves_the_order_in_the_pool_until_someone_loads(
    app, db, reoffers, queued_assigns, sample_user, user_address, sample_product
):
    short = _driver(db)
    db.session.add(
        DriverBottleSession(
            driver_user_id=short.id, bottles_loaded=3, bottles_collected_from_customers=15,
            status=DriverBottleSessionStatus.OPEN, started_at=datetime.now(UTC) - timedelta(hours=1),
        )
    )
    db.session.commit()
    order = _orders(app, db, sample_user, user_address, sample_product, quantity=8)
    delivery_id = order.delivery.id

    refused = auto_assign_delivery_task.run(delivery_id)

    assert refused == {"success": False, "delivery_id": delivery_id, "error": "no_driver_with_enough_bottles"}
    db.session.expire_all()
    assert Delivery.query.get(delivery_id).delivery_person_id is None
    assert Delivery.query.get(delivery_id).status == DeliveryStatus.SCHEDULED

    loaded = _driver(db)
    assert _loads(app, loaded, 10).status_code == 201
    assert reoffers == [()]  # the open told the pool

    assert reoffer_pool_deliveries_task.run() == 1
    assert queued_assigns == [delivery_id]
    assigned = auto_assign_delivery_task.run(queued_assigns[0])

    assert assigned["success"] is True and assigned["driver_id"] == loaded.id
    db.session.expire_all()
    assert Delivery.query.get(delivery_id).delivery_person_id == loaded.id
