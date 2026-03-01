"""Route-level regressions for admin staff delivery-person endpoints."""

from datetime import UTC, datetime

from flask_jwt_extended import create_access_token

from business_app.models.delivery import Delivery, DeliveryPerson
from business_app.utils.constants import DeliveryStatus


def _auth_headers(app, user_id: int) -> dict:
    with app.app_context():
        token = create_access_token(identity=str(user_id), additional_claims={"role": "admin"})
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def test_get_staff_delivery_persons_returns_live_active_delivery_counts(
    client,
    app,
    db,
    admin_user,
    delivery_driver,
    sample_order,
):
    profile = DeliveryPerson(
        user_id=delivery_driver.id,
        full_name=delivery_driver.full_name,
        phone=delivery_driver.phone,
        email=delivery_driver.email,
        is_active=True,
        is_available=True,
        max_concurrent_deliveries=3,
        current_active_deliveries=42,
    )
    delivery = Delivery(
        order_id=sample_order.id,
        delivery_person_id=delivery_driver.id,
        status=DeliveryStatus.ASSIGNED,
        scheduled_date=datetime.now(UTC),
        scheduled_time_slot="09:00-12:00",
    )
    db.session.add(profile)
    db.session.add(delivery)
    db.session.commit()

    response = client.get(
        "/api/v1/admin/staff/delivery-persons",
        headers=_auth_headers(app, admin_user.id),
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["data"]["items"][0]["current_active_deliveries"] == 1
    assert body["data"]["items"][0]["max_concurrent_deliveries"] == 3
