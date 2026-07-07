"""Integration coverage for GET /api/v1/delivery/driver/assignments.

Regression test for the broken ``order_by`` at ``business_app/api/delivery.py:370``:
``Delivery.order`` is a relationship attribute (not the joined ``Order`` class), so
``Delivery.order.is_urgent`` raised ``AttributeError`` at query-build time on every
call, swallowed by the broad ``except Exception`` and surfaced as a generic 500.
"""

from datetime import UTC, datetime

from flask_jwt_extended import create_access_token

from business_app.models.delivery import Delivery
from business_app.models.order import Order
from shared.enums import DeliveryStatus, OrderStatus


def _driver_headers(app, user_id: int) -> dict:
    with app.app_context():
        token = create_access_token(identity=str(user_id))
    return {"Authorization": f"Bearer {token}"}


def _make_order(db, sample_user, *, order_number, is_urgent):
    order = Order(
        user_id=sample_user.id,
        order_number=order_number,
        status=OrderStatus.CONFIRMED,
        subtotal=15000,
        total_amount=18000,
        is_urgent=is_urgent,
    )
    db.session.add(order)
    db.session.commit()
    return order


def _make_delivery(db, *, order_id, delivery_person_id):
    delivery = Delivery(
        order_id=order_id,
        delivery_person_id=delivery_person_id,
        status=DeliveryStatus.ASSIGNED,
        scheduled_date=datetime.now(UTC),
        scheduled_time_slot="09:00-12:00",
    )
    db.session.add(delivery)
    db.session.commit()
    return delivery


class TestDriverAssignmentsEndpoint:
    def test_returns_200_and_sorts_urgent_deliveries_first(
        self, client, app, db, delivery_driver, sample_user
    ):
        normal_order = _make_order(db, sample_user, order_number="ORD-NORMAL-1", is_urgent=False)
        urgent_order = _make_order(db, sample_user, order_number="ORD-URGENT-1", is_urgent=True)

        # Create the normal (non-urgent) delivery first so a naive/no-op sort
        # (e.g. primary-key/insertion order) would put it first — only a
        # correct is_urgent-desc sort puts the urgent delivery first.
        _make_delivery(db, order_id=normal_order.id, delivery_person_id=delivery_driver.id)
        urgent_delivery = _make_delivery(db, order_id=urgent_order.id, delivery_person_id=delivery_driver.id)

        response = client.get(
            "/api/v1/delivery/driver/assignments",
            headers=_driver_headers(app, delivery_driver.id),
        )

        assert response.status_code == 200
        payload = response.get_json()

        assert payload["summary"]["total_assignments"] == 2
        assert payload["summary"]["urgent_assignments"] == 1

        assignments = payload["assignments"]
        assert len(assignments) == 2
        # The urgent delivery must sort first.
        assert assignments[0]["id"] == urgent_delivery.id
        assert assignments[0]["order_id"] == urgent_order.id
