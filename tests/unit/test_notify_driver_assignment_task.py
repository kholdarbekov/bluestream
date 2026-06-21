"""Regression: notify_driver_assignment_task must use real Delivery columns.

Fixing the auto-assign showstopper *unmasks* this task (it never ran before,
because assignment failed first). It referenced phantom attributes —
``delivery.driver`` / ``delivery.driver_id`` / ``delivery.tracking_code`` /
``delivery.delivery_address_street`` — none of which exist on the model, so the
driver-assignment notification crashed with AttributeError on every run.
"""

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from business_app.models.delivery import Delivery, DeliveryPerson
from business_app.models.order import Order
from business_app.models.user import User, UserAddress
from business_app.tasks import notification_tasks
from business_app.tasks.notification_tasks import notify_driver_assignment_task
from shared.enums import DeliveryStatus, OrderStatus, UserRole, UserType

_run = notify_driver_assignment_task.run.__func__


@pytest.mark.unit
@pytest.mark.delivery
def test_notify_driver_assignment_uses_real_columns(app, db):
    with app.app_context():
        driver = User(
            email="ndriver@example.com", phone="+998901234500", password_hash="x",
            first_name="N", last_name="D", user_type=UserType.STAFF,
            role=UserRole.DELIVERY_DRIVER, is_verified=True,
        )
        customer = User(
            email="ncust@example.com", phone="+998901234599", password_hash="x",
            first_name="Cu", last_name="St", user_type=UserType.INDIVIDUAL,
            role=UserRole.CUSTOMER, is_verified=True,
        )
        db.session.add_all([driver, customer])
        db.session.commit()
        db.session.add(DeliveryPerson(user_id=driver.id, full_name="N D", phone="+998901234500", is_active=True))
        addr = UserAddress(
            user_id=customer.id, title="h", full_address="12 Main St",
            street_address="12 Main St", latitude=41.3, longitude=69.25,
        )
        db.session.add(addr)
        db.session.flush()
        order = Order(
            user_id=customer.id, order_number="ORD-NOTIFY-1", status=OrderStatus.CONFIRMED,
            subtotal=Decimal("0"), total_amount=Decimal("0"), delivery_address_id=addr.id,
        )
        db.session.add(order)
        db.session.flush()
        delivery = Delivery(
            order_id=order.id, delivery_person_id=driver.id, status=DeliveryStatus.ASSIGNED,
            scheduled_date=datetime.now(UTC), scheduled_time_slot="09:00-12:00",
        )
        db.session.add(delivery)
        db.session.commit()
        delivery_id, driver_id = delivery.id, driver.id

        fake_service = MagicMock()
        fake_service.send_notification.return_value = {"success": True}
        mock_self = MagicMock()
        mock_self.retry.side_effect = AssertionError("must not retry on a healthy assignment")

        with patch.object(notification_tasks, "NotificationService", return_value=fake_service):
            with app.app_context():
                result = _run(mock_self, delivery_id)

        assert result == {"success": True}
        # Notification is addressed to the real person column.
        assert fake_service.send_notification.call_args.args[0] == driver_id
        # Template carries the real tracking number + address.
        template = fake_service.send_notification.call_args.args[3]
        assert template["tracking_code"] == delivery.tracking_number
        assert template["delivery_address"] == "12 Main St"
