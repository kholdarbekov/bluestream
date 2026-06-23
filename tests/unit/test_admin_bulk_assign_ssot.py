import pytest
from datetime import datetime, UTC, timedelta
from business_app.models.delivery import Delivery, DeliveryPerson
from business_app.models.bottle import DriverBottleSession, DriverBottleSessionOrder
from business_app.services.admin_bulk_action_service import AdminBulkActionService
from shared.enums import DeliveryStatus, DriverBottleSessionStatus, UserRole, UserType
from business_app.utils.password_security import hash_password


def _driver(db, phone):
    from business_app.models.user import User
    u = User(phone=phone, first_name="D", last_name="R", password_hash=hash_password("TestPassword123!"),
             role=UserRole.DELIVERY_DRIVER, user_type=UserType.STAFF, is_verified=True, created_at=datetime.now(UTC))
    db.session.add(u); db.session.flush()
    # Force DeliveryPerson.id != user_id so the PK/user_id bug would surface.
    db.session.add(DeliveryPerson(user_id=u.id, full_name="D R", phone=phone, is_active=True, is_available=True,
                                  working_hours_start="00:00", working_hours_end="23:59"))
    db.session.flush()
    return u


@pytest.mark.unit
def test_bulk_assign_uses_user_id_and_binds(db, app, sample_user, sample_product):
    from tests.unit.test_bottle_session_integration import _make_order_with_bottles
    # make a throwaway driver first so the real driver's profile PK != user_id
    _driver(db, "+998901800000")
    driver = _driver(db, "+998901800001")
    s = DriverBottleSession(driver_user_id=driver.id, bottles_loaded=20, status=DriverBottleSessionStatus.OPEN)
    db.session.add(s); db.session.flush()
    order = _make_order_with_bottles(db, sample_user, sample_product, quantity=2)
    d = Delivery(order_id=order.id, status=DeliveryStatus.SCHEDULED,
                 scheduled_date=datetime.now(UTC)+timedelta(hours=1), scheduled_time_slot="09:00-12:00")
    db.session.add(d); db.session.flush()
    db.session.commit()

    with app.test_request_context():
        res = AdminBulkActionService._bulk_action_deliveries(
            "assign_driver", [d.id], {"driver_id": driver.id}, "bulk", admin_id=1)

    assert res["success_count"] == 1
    refreshed = Delivery.query.get(d.id)
    assert refreshed.delivery_person_id == driver.id           # user_id, not DeliveryPerson.id
    assert refreshed.status == DeliveryStatus.ASSIGNED
    assert DriverBottleSessionOrder.query.filter_by(order_id=order.id).first().session_id == s.id
