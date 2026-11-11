"""The session payloads carry the one capacity number (spec A8), on the endpoints the apps call."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from flask_jwt_extended import create_access_token

from business_app.models.bottle import DriverBottleSession, DriverSessionMembership
from business_app.models.delivery import Delivery
from business_app.models.order import Order, OrderItem
from business_app.models.product import Product, ProductSizeEnum
from business_app.models.user import User, UserAddress
from shared.enums import (
    DeliveryStatus,
    DriverBottleSessionStatus,
    DriverSessionMembershipStatus,
    OrderStatus,
    UserRole,
    UserType,
)

FIVE_KEYS = {
    "free_full_bottles": 5,
    "full_bottles_on_board": 10,
    "empties_on_board": 0,
    "transferable_full": 10,
    "transferable_empties": 0,
}


@pytest.fixture
def loaded_session(db, sample_category, delivery_driver):
    """Loaded 10; ASSIGNED 3, PICKED_UP 2, IN_TRANSIT 4, ARRIVED 1 -> free 5."""
    product = Product(
        name="19 litrlik suv",
        category_id=sample_category.id,
        size=ProductSizeEnum.SIZE_19L,
        volume=19.0,
        volume_unit="L",
        base_price=Decimal("15000.00"),
        stock_quantity=500,
        is_active=True,
        tracks_returnable_bottles=True,
        returnable_bottles_per_unit=Decimal("1.00"),
    )
    customer = User(
        phone="+998900000778",
        password_hash="x",
        first_name="C",
        last_name="",
        user_type=UserType.INDIVIDUAL,
        role=UserRole.CUSTOMER,
        is_verified=True,
    )
    db.session.add_all([product, customer])
    db.session.flush()
    session = DriverBottleSession(
        driver_user_id=delivery_driver.id, bottles_loaded=10, status=DriverBottleSessionStatus.OPEN
    )
    db.session.add(session)
    for i, (status, qty) in enumerate(
        [
            (DeliveryStatus.ASSIGNED, 3),
            (DeliveryStatus.PICKED_UP, 2),
            (DeliveryStatus.IN_TRANSIT, 4),
            (DeliveryStatus.ARRIVED, 1),
        ]
    ):
        addr = UserAddress(user_id=customer.id, title="Stop", full_address=f"S{i}", street_address=f"S{i}")
        db.session.add(addr)
        db.session.flush()
        order = Order(
            user_id=customer.id,
            order_number=f"FFA_{i:05d}",
            status=OrderStatus.CONFIRMED,
            subtotal=Decimal("15000") * qty,
            total_amount=Decimal("15000") * qty,
            delivery_address_id=addr.id,
            delivery_date=datetime.now(UTC) + timedelta(hours=2),
        )
        db.session.add(order)
        db.session.flush()
        db.session.add(
            OrderItem(
                order_id=order.id,
                product_id=product.id,
                quantity=qty,
                unit_price=Decimal("15000.00"),
                total_price=Decimal("15000.00") * qty,
            )
        )
        db.session.add(
            Delivery(
                order_id=order.id,
                delivery_person_id=delivery_driver.id,
                status=status,
                scheduled_date=datetime.now(UTC),
                scheduled_time_slot="09:00-12:00",
            )
        )
    db.session.commit()
    return session


def _payload(resp):
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    return body.get("data", body)


@pytest.mark.integration
def test_staff_current_session_carries_free_full(client, driver_auth_headers, loaded_session):
    data = _payload(client.get("/api/v1/staff/bottles/session/current", headers=driver_auth_headers))
    assert {k: data[k] for k in FIVE_KEYS} == FIVE_KEYS
    assert data["current_inventory"] == 10


@pytest.mark.integration
def test_admin_session_detail_carries_free_full(app, client, admin_user, loaded_session):
    token = create_access_token(identity=str(admin_user.id), additional_claims={"role": "admin"})
    data = _payload(
        client.get(
            f"/api/v1/admin/bottles/sessions/{loaded_session.id}",
            headers={"Authorization": f"Bearer {token}"},
        )
    )
    assert {k: data[k] for k in FIVE_KEYS} == FIVE_KEYS


@pytest.mark.integration
def test_joinable_and_membership_payloads_carry_free_full(
    app, client, db, loaded_session, second_delivery_driver
):
    token = create_access_token(identity=str(second_delivery_driver.id), additional_claims={"role": "delivery_driver"})
    headers = {"Authorization": f"Bearer {token}"}
    joinable = _payload(client.get("/api/v1/staff/bottles/sessions/joinable", headers=headers))
    row = next(r for r in joinable if r["session_id"] == loaded_session.id)
    assert {k: row[k] for k in FIVE_KEYS} == FIVE_KEYS

    db.session.add(
        DriverSessionMembership(
            session_id=loaded_session.id,
            session_owner_id=loaded_session.driver_user_id,
            member_driver_id=second_delivery_driver.id,
            status=DriverSessionMembershipStatus.ACTIVE,
        )
    )
    db.session.commit()
    info = _payload(client.get("/api/v1/staff/bottles/session/membership", headers=headers))
    assert {k: info[k] for k in FIVE_KEYS} == FIVE_KEYS


@pytest.mark.integration
def test_closed_session_publishes_zero_free_full_in_history_lists(app, client, db, admin_user, driver_auth_headers, loaded_session):
    loaded_session.status = DriverBottleSessionStatus.CLOSED
    loaded_session.closed_at = datetime.now(UTC)
    db.session.commit()
    assert loaded_session.full_bottles_on_board > 0

    staff = _payload(client.get("/api/v1/staff/bottles/sessions", headers=driver_auth_headers))
    row = next(r for r in staff["items"] if r["id"] == loaded_session.id)
    assert row["free_full_bottles"] == 0

    token = create_access_token(identity=str(admin_user.id), additional_claims={"role": "admin"})
    admin = _payload(client.get("/api/v1/admin/bottles/sessions", headers={"Authorization": f"Bearer {token}"}))
    row = next(r for r in admin["items"] if r["id"] == loaded_session.id)
    assert row["free_full_bottles"] == 0


@pytest.mark.integration
def test_an_order_returned_on_the_orders_page_stops_counting_against_free_full(
    app, client, db, admin_user, driver_auth_headers, loaded_session
):
    """An admin sets the 4-bottle On-the-way stop's order to Returned on the Orders page. The delivery stays In transit
    (only a cancel moves it), but the order is no longer one a driver holds (`DRIVER_HELD_ORDER_STATUSES`, the ETA
    engine's own set), so its bottles stop being promised: free full goes from 5 to 9."""
    on_the_way = Delivery.query.filter_by(status=DeliveryStatus.IN_TRANSIT).one()
    on_the_way.order.status = OrderStatus.OUT_FOR_DELIVERY
    db.session.commit()
    order_id = on_the_way.order_id
    current = "/api/v1/staff/bottles/session/current"
    assert _payload(client.get(current, headers=driver_auth_headers))["free_full_bottles"] == 5

    token = create_access_token(identity=str(admin_user.id), additional_claims={"role": "admin"})
    returned = client.put(
        f"/api/v1/admin/orders/{order_id}/status",
        json={"status": "returned", "notes": "", "reason": "Customer refused at the door"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert returned.status_code == 200, returned.get_json()
    assert returned.get_json()["data"]["order"]["status"] == "returned"
    db.session.expire_all()
    assert Delivery.query.filter_by(order_id=order_id).one().status is DeliveryStatus.IN_TRANSIT
    data = _payload(client.get(current, headers=driver_auth_headers))
    assert (data["free_full_bottles"], data["full_bottles_on_board"]) == (9, 10)
