"""Assignment judges FREE FULL bottles; only bot accept and auto-assign require a session (spec A10, T4).

Driven through the HTTP routes the staff bot and the admin pages call, with real JWTs. Only external I/O is faked.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from flask_jwt_extended import create_access_token

from business_app.models.bottle import DriverBottleSession, DriverBottleSessionOrder
from business_app.models.delivery import Delivery
from business_app.services.delivery_service import DeliveryService
from business_app.utils.exceptions import ValidationError
from shared.enums import DeliveryStatus, DriverBottleSessionStatus
from tests.unit.test_staff_error_endpoints import _assigned_delivery, _driver, _headers, _order_with_bottles

ACCEPT = "/api/v1/staff/delivery/accept/{}"
REASSIGN = "/api/v1/admin/staff/delivery/reassign/{}"
DISPATCH_MOVE = "/api/v1/admin/dispatch/stops/{}/assign"


@pytest.fixture
def strict(app, monkeypatch):
    monkeypatch.setitem(app.config, "BOTTLE_SESSION_ENFORCEMENT_STRICT", True)


def _session(db, driver, loaded, collected=0):
    session = DriverBottleSession(
        driver_user_id=driver.id,
        bottles_loaded=loaded,
        bottles_collected_from_customers=collected,
        status=DriverBottleSessionStatus.OPEN,
    )
    db.session.add(session)
    db.session.commit()
    return session


def _pool_delivery(db, order):
    delivery = Delivery(
        order_id=order.id,
        status=DeliveryStatus.SCHEDULED,
        scheduled_date=datetime.now(UTC) + timedelta(hours=1),
        scheduled_time_slot="09:00-12:00",
    )
    db.session.add(delivery)
    db.session.commit()
    return delivery


def _admin_headers(app, admin_user):
    with app.app_context():
        token = create_access_token(identity=str(admin_user.id), additional_claims={"role": "admin"})
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _bound_to_a(db, sample_user, sample_product, qty=2):
    """A delivery ASSIGNED to driver A, its order bound to A's session. Returns (delivery, order, a, a_session)."""
    driver_a = _driver(db, "+998901000801")
    a_session = _session(db, driver_a, loaded=10)
    order = _order_with_bottles(db, sample_user, sample_product, quantity=qty)
    delivery = _assigned_delivery(db, order, driver_a)
    db.session.add(DriverBottleSessionOrder(session_id=a_session.id, order_id=order.id, accepted_by_driver_id=driver_a.id))
    db.session.commit()
    return delivery, order, driver_a, a_session


@pytest.mark.integration
def test_bot_accept_refused_when_only_empties(app, client, db, strict, sample_user, sample_product):
    driver = _driver(db, "+998901000791")
    _session(db, driver, loaded=3, collected=15)  # 18 bottles on the truck, 3 of them full
    delivery = _pool_delivery(db, _order_with_bottles(db, sample_user, sample_product, quantity=8))

    response = client.post(ACCEPT.format(delivery.id), headers=_headers(app, driver.id))

    body = response.get_json()
    assert response.status_code == 400, body
    assert body["error_code"] == "BOTTLE_SESSION_CAPACITY_EXCEEDED"
    # The wire also carries the generic `validation_errors: []` of every ValidationError; the three numbers are ours.
    assert {k: body["details"][k] for k in ("available", "required", "shortfall")} == {
        "available": 3,
        "required": 8,
        "shortfall": 5,
    }
    assert Delivery.query.get(delivery.id).delivery_person_id is None


@pytest.mark.integration
def test_bot_accept_not_blocked_by_assigned_orders(app, client, db, strict, sample_user, sample_product):
    """ASSIGNED stops do not reserve (A10): loaded 10 with two 8-bottle orders held still accepts a third."""
    driver = _driver(db, "+998901000792")
    session = _session(db, driver, loaded=10)
    for _ in range(2):
        _assigned_delivery(db, _order_with_bottles(db, sample_user, sample_product, quantity=8), driver)
    delivery = _pool_delivery(db, _order_with_bottles(db, sample_user, sample_product, quantity=8))
    db.session.commit()

    response = client.post(ACCEPT.format(delivery.id), headers=_headers(app, driver.id))

    assert response.status_code == 200, response.get_json()
    refreshed = Delivery.query.get(delivery.id)
    assert refreshed.delivery_person_id == driver.id
    assert DriverBottleSessionOrder.query.filter_by(order_id=refreshed.order_id).one().session_id == session.id


@pytest.mark.integration
def test_auto_entry_point_requires_session_when_asked(app, db, sample_user, sample_product):
    driver = _driver(db, "+998901000793")  # no session
    delivery = _pool_delivery(db, _order_with_bottles(db, sample_user, sample_product, quantity=2))

    with app.test_request_context(), patch.object(DeliveryService, "_notify_driver", lambda self, d: None), patch.object(
        DeliveryService, "_optimize_driver_route", lambda self, *a, **k: None
    ):
        with pytest.raises(ValidationError) as exc:
            DeliveryService().assign_delivery_driver(delivery.id, driver.id, require_session=True)
        assert exc.value.error_code == "BOTTLE_SESSION_REQUIRED"
        assigned = DeliveryService().assign_delivery_driver(delivery.id, driver.id)  # the default stays lenient

    assert assigned.delivery_person_id == driver.id


@pytest.mark.integration
def test_reassign_to_short_driver_unbinds_previous_session(app, client, db, admin_user, sample_user, sample_product):
    delivery, order, _driver_a, _a_session = _bound_to_a(db, sample_user, sample_product)
    driver_b = _driver(db, "+998901000802")  # no session

    response = client.put(
        REASSIGN.format(delivery.id), headers=_admin_headers(app, admin_user), json={"new_delivery_person_id": driver_b.id}
    )

    assert response.status_code == 200, response.get_json()
    assert Delivery.query.get(delivery.id).delivery_person_id == driver_b.id
    assert DriverBottleSessionOrder.query.filter_by(order_id=order.id).first() is None


@pytest.mark.integration
def test_reassign_to_covering_driver_rebinds(app, client, db, admin_user, sample_user, sample_product):
    delivery, order, _driver_a, _a_session = _bound_to_a(db, sample_user, sample_product)
    driver_b = _driver(db, "+998901000803")
    b_session = _session(db, driver_b, loaded=10)

    response = client.put(
        REASSIGN.format(delivery.id), headers=_admin_headers(app, admin_user), json={"new_delivery_person_id": driver_b.id}
    )

    assert response.status_code == 200, response.get_json()
    assert DriverBottleSessionOrder.query.filter_by(order_id=order.id).one().session_id == b_session.id


@pytest.mark.integration
def test_reassign_to_driver_whose_free_full_falls_short_unbinds(app, client, db, admin_user, sample_user, sample_product):
    """A session exists but its free full cannot cover the order: not bound, and A's old binding does not linger."""
    delivery, order, _driver_a, _a_session = _bound_to_a(db, sample_user, sample_product, qty=5)
    driver_b = _driver(db, "+998901000804")
    _session(db, driver_b, loaded=2, collected=20)

    response = client.put(
        REASSIGN.format(delivery.id), headers=_admin_headers(app, admin_user), json={"new_delivery_person_id": driver_b.id}
    )

    assert response.status_code == 200, response.get_json()
    assert DriverBottleSessionOrder.query.filter_by(order_id=order.id).first() is None


@pytest.mark.integration
def test_dispatch_move_never_refuses_on_bottles(app, client, db, admin_user, sample_user, sample_product):
    delivery, order, _driver_a, _a_session = _bound_to_a(db, sample_user, sample_product)
    driver_b = _driver(db, "+998901000805")  # no session

    response = client.post(
        DISPATCH_MOVE.format(delivery.id), headers=_admin_headers(app, admin_user), json={"driver_id": driver_b.id}
    )

    assert response.status_code == 200, response.get_json()
    assert Delivery.query.get(delivery.id).delivery_person_id == driver_b.id
    assert DriverBottleSessionOrder.query.filter_by(order_id=order.id).first() is None


@pytest.mark.integration
@pytest.mark.parametrize("target_has_cover", [False, True])
def test_reassign_of_delivered_delivery_keeps_closed_session_binding(
    app, client, db, admin_user, sample_user, sample_product, target_has_cover
):
    """A finished delivery's binding is the closed trip's historical tally; reassign must not drop or move it."""
    from business_app.models.order import OrderStatus

    delivery, order, _driver_a, a_session = _bound_to_a(db, sample_user, sample_product)
    a_session.status = DriverBottleSessionStatus.CLOSED
    delivery.status = DeliveryStatus.DELIVERED
    order.status = OrderStatus.DELIVERED
    db.session.commit()
    driver_b = _driver(db, "+998901000803")
    if target_has_cover:
        _session(db, driver_b, loaded=10)

    response = client.put(
        REASSIGN.format(delivery.id), headers=_admin_headers(app, admin_user), json={"new_delivery_person_id": driver_b.id}
    )

    assert response.status_code == 200, response.get_json()
    assert Delivery.query.get(delivery.id).delivery_person_id == driver_b.id
    binding = DriverBottleSessionOrder.query.filter_by(order_id=order.id).one()
    assert binding.session_id == a_session.id
