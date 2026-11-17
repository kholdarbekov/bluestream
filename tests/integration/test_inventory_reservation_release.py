"""Releasing (and sweeping) inventory reservations works on ANY InventoryService instance.

`release_reservations` and `cleanup_expired_reservations` used to test `self.redis_client` BEFORE the
lazy `_get_redis_client()` had filled it, so on an instance that had not itself reserved anything in
this process (a fresh `InventoryService()`, or the process-wide singleton in a worker that never
reserved) they returned "not available" and released NOTHING while Redis was up: a cancelled order's
stock stayed held until its TTL ran out. Found by the live dev drive (Task Y1).

The reservation is made by one instance and released through another, over the suite's real test
Redis (the autouse `reset_redis_state` fixture flushes it between tests).
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from flask_jwt_extended import create_access_token

from business_app.models.order import Order
from business_app.services.inventory_service import InventoryService
from business_app.utils.service_factory import ServiceFactory
from shared.enums import OrderStatus, PaymentMethod
from shared.redis_keyspace import RedisKeyspace

ORDER_ID = 987001


@pytest.fixture
def redis_client(app):
    client = InventoryService()._get_redis_client()
    assert client is not None, "the suite's test Redis must be reachable"
    return client


@pytest.fixture
def fresh_singleton(app):
    """The process-wide inventory singleton as a worker that never reserved would hold it."""
    ServiceFactory._instances.pop("inventory_service", None)
    yield
    ServiceFactory._instances.pop("inventory_service", None)


def _keys(redis_client, order_id):
    return (
        redis_client.keys(RedisKeyspace.inventory_reservation_pattern(order_id)),
        redis_client.keys(RedisKeyspace.reservation_details_pattern(order_id)),
    )


def _reserve_with_another_instance(redis_client, order_id, product):
    result = InventoryService().reserve_inventory(
        order_id=order_id, items=[{"product_id": product.id, "quantity": 2}], user_id=7
    )
    assert result["success"] is True, result
    holds, details = _keys(redis_client, order_id)
    assert len(holds) == 1 and len(details) == 1
    return result


def test_a_fresh_instance_releases_another_instances_reservations(app, redis_client, sample_product):
    _reserve_with_another_instance(redis_client, ORDER_ID, sample_product)

    result = InventoryService().release_reservations(ORDER_ID)

    assert result == {"success": True, "released_items": 1}
    assert _keys(redis_client, ORDER_ID) == ([], [])


def test_the_admin_release_route_releases_them(
    app, client, db, redis_client, sample_product, sample_user, admin_user, fresh_singleton
):
    order = Order(
        user_id=sample_user.id,
        order_number="ORD-Y1-ADMIN",
        status=OrderStatus.PENDING,
        payment_method=PaymentMethod.CASH,
        subtotal=Decimal("15000.00"),
        total_amount=Decimal("15000.00"),
    )
    db.session.add(order)
    db.session.commit()
    _reserve_with_another_instance(redis_client, order.id, sample_product)
    token = create_access_token(identity=str(admin_user.id), additional_claims={"role": "admin"})

    resp = client.post(
        f"/api/v1/admin/inventory/reservations/{order.id}/release",
        headers={"Authorization": f"Bearer {token}"},
        json={"reason": "Manual release by admin"},
    )

    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert _keys(redis_client, order.id) == ([], [])


def test_cancelling_an_order_releases_its_reservation(
    app, client, db, redis_client, sample_product, sample_user, fresh_singleton
):
    order = Order(
        user_id=sample_user.id,
        order_number="ORD-Y1-CANCEL",
        status=OrderStatus.PENDING,
        payment_method=PaymentMethod.CASH,
        is_paid=False,
        subtotal=Decimal("15000.00"),
        total_amount=Decimal("15000.00"),
    )
    db.session.add(order)
    db.session.commit()
    # The reservation `create_order` made in the web request that took the order.
    _reserve_with_another_instance(redis_client, order.id, sample_product)
    token = create_access_token(identity=str(sample_user.id))

    resp = client.post(
        f"/api/v1/orders/{order.id}/cancel",
        headers={"Authorization": f"Bearer {token}"},
        json={"expected_via": "cancel"},
    )

    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert _keys(redis_client, order.id) == ([], [])


def test_the_sweep_runs_on_a_fresh_instance(app, redis_client):
    order_id, product_id = 987002, 55
    hold = RedisKeyspace.inventory_reservation(order_id, product_id)
    details = RedisKeyspace.reservation_details(order_id, product_id)
    redis_client.set(hold, "2")
    redis_client.hset(
        details, mapping={"expires_at": (datetime.now(UTC) - timedelta(minutes=5)).isoformat(), "quantity": 2}
    )

    result = InventoryService().cleanup_expired_reservations()

    assert result == {"success": True, "cleaned_count": 1}
    assert not redis_client.exists(hold)
    assert not redis_client.exists(details)


def test_no_redis_is_still_reported_honestly(app, monkeypatch):
    service = InventoryService()
    monkeypatch.setattr(service, "_get_redis_client", lambda: None)

    released = service.release_reservations(ORDER_ID)
    swept = service.cleanup_expired_reservations()

    assert released == {"success": False, "reason": "Reservation system not available"}
    assert swept == {"success": False, "reason": "Redis not available"}
