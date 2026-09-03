"""The "your COD is restricted" notification must say what the engine enforces.

PRE-EXISTING BUG (b): the gate compared raw debt counts against the limit and
skipped the exemption check the other five cap sites apply, so an admin-exempt
or grocery-store customer could be told their cash rail was closed when it never
was. It also knew nothing about the amount arm, so two 280-sum shortfalls fired
a lockout warning for a customer who was never locked out.
"""

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import patch

import pytest

from business_app.services.cash_collection_service import CashCollectionService
from shared.business_config import COD_ACTIVE_DEBT_LIMIT
from tests.unit._scope_money_helpers import delivered_cod_order, make_address


def _cash_delivery(db, order, driver, *, address):
    from business_app.models.delivery import Delivery, DeliveryPerson
    from shared.enums import DeliveryStatus, OrderStatus, PaymentMethod

    profile = DeliveryPerson(
        user_id=driver.id,
        full_name="Breach Driver",
        phone=driver.phone,
        email=driver.email,
        is_active=True,
        is_available=True,
    )
    db.session.add(profile)

    order.payment_method = PaymentMethod.CASH
    order.status = OrderStatus.OUT_FOR_DELIVERY
    order.delivery_address_id = address.id
    db.session.flush()
    CashCollectionService().ensure_cod_payment_for_order(order)

    delivery = Delivery(
        order_id=order.id,
        delivery_person_id=driver.id,
        status=DeliveryStatus.ARRIVED,
        scheduled_date=datetime.now(UTC),
        scheduled_time_slot="09:00-12:00",
    )
    db.session.add(delivery)
    db.session.commit()
    return delivery


def _deliver_with_no_cash(delivery, driver):
    from business_app.services.staff_service import StaffService

    with patch("business_app.tasks.notification_tasks.send_delivery_update_task.delay"), patch(
        "business_app.tasks.delivery_tasks.optimize_driver_route_task.delay"
    ), patch.object(StaffService, "_notify_customer_cod_debt_limit") as notify:
        StaffService.update_delivery_status(
            delivery_id=delivery.id,
            new_status="delivered",
            staff_user_id=driver.id,
            metadata={"cash_collected": "0.00", "notes": "Customer will pay later"},
        )
    return notify


@pytest.mark.unit
class TestBreachNotificationHonoursTheSsot:
    def test_exempt_customer_is_never_warned(self, db, sample_order, sample_user, delivery_driver):
        """BUG (b): an admin-exempt customer can never be capped, so must never
        be told they have been."""
        sample_user.cod_debt_check_exempt = True
        db.session.commit()
        for _ in range(COD_ACTIVE_DEBT_LIMIT - 1):
            delivered_cod_order(db, sample_user, total=Decimal("15000.00"))
        address = make_address(db, sample_user)

        delivery = _cash_delivery(db, sample_order, delivery_driver, address=address)
        notify = _deliver_with_no_cash(delivery, delivery_driver)

        notify.assert_not_called()

    def test_tiny_debts_do_not_warn(self, db, sample_order, sample_user, delivery_driver):
        """The amount arm: crossing the count limit on shortfalls of a few
        hundred sum restricts nobody, so it must warn nobody."""
        sample_order.subtotal = Decimal("280.00")
        sample_order.delivery_fee = Decimal("0.00")
        sample_order.total_amount = Decimal("280.00")
        for _ in range(COD_ACTIVE_DEBT_LIMIT - 1):
            delivered_cod_order(db, sample_user, total=Decimal("280.00"))
        db.session.commit()
        address = make_address(db, sample_user)

        delivery = _cash_delivery(db, sample_order, delivery_driver, address=address)
        notify = _deliver_with_no_cash(delivery, delivery_driver)

        assert CashCollectionService().is_customer_cod_restricted(sample_user.id) is False
        notify.assert_not_called()

    def test_real_breach_still_warns(self, db, sample_order, sample_user, delivery_driver):
        """Regression guard: the notification still fires when it should."""
        for _ in range(COD_ACTIVE_DEBT_LIMIT - 1):
            delivered_cod_order(db, sample_user, total=Decimal("15000.00"))
        address = make_address(db, sample_user)

        delivery = _cash_delivery(db, sample_order, delivery_driver, address=address)
        notify = _deliver_with_no_cash(delivery, delivery_driver)

        assert CashCollectionService().is_customer_cod_restricted(sample_user.id) is True
        notify.assert_called_once_with(sample_user.id)

    def test_already_restricted_customer_is_not_warned_again(
        self, db, sample_order, sample_user, delivery_driver
    ):
        """The gate is an EDGE, not a state: already over the cap => no re-warn."""
        for _ in range(COD_ACTIVE_DEBT_LIMIT):
            delivered_cod_order(db, sample_user, total=Decimal("15000.00"))
        address = make_address(db, sample_user)

        delivery = _cash_delivery(db, sample_order, delivery_driver, address=address)
        notify = _deliver_with_no_cash(delivery, delivery_driver)

        notify.assert_not_called()
