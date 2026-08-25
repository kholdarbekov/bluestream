"""CASE C — cash taken at the door, then the customer pays the Click link anyway.

Policy 2026-08-24. The customer has now paid twice. Owner ruling: restore the
Click rail so the order settles on the rail that actually processed the money,
issue the fiscal receipt Click is owed, and re-book the driver's banked cash as
the customer's prepaid credit.

🔴 THE LANDMINE. `open_receivable_clause`'s docstring documents it: when a
payment completes, `_sync_completed_prepayment_projection` forces
`amount_collected = amount`. If the driver's cash allocation is still attached
at that moment it is silently DESTROYED — the business loses banknotes it has
already banked. So the allocation must be reversed into customer credit BEFORE
the payment completes, and the reversal must use
`reverse_allocation_to_payment`, which never touches `event.amount` or
`driver_cash_session_id` (the driver really did hand over those notes).

The PREPARE guard (Phase 4A) refuses most of these at the card. This race
survives it: PREPARE passes before delivery, the customer's bank step takes a
while, and COMPLETE lands after the driver has settled at the door.
"""

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest

from business_app import db
from business_app.models.delivery import Delivery, DeliveryPerson
from business_app.models.order import Order
from business_app.models.payment import CashCollectionAllocation, CashCollectionEvent, Payment
from business_app.models.user import UserAddress
from shared.enums import DeliveryStatus, OrderStatus, PaymentMethod, PaymentStatus

from tests.integration.fake_gateways import TEST_CLICK_SHOP_SECRET_KEY, make_click_webhook_form

WEBHOOK_URL = "/api/v1/payments/webhook/click"


@pytest.fixture
def driver_profile(db, delivery_driver):
    profile = DeliveryPerson(
        user_id=delivery_driver.id,
        full_name="Test Driver",
        phone=delivery_driver.phone,
        email=delivery_driver.email,
        is_active=True,
        is_available=True,
    )
    db.session.add(profile)
    db.session.commit()
    return profile


def _click_order_at_the_door(db, order, driver):
    address = UserAddress(
        user_id=order.user_id,
        title="Home",
        full_address="123 Test Street, Tashkent",
        street_address="123 Test Street",
        city="Tashkent",
        is_default=True,
    )
    db.session.add(address)
    db.session.flush()

    order.payment_method = PaymentMethod.CLICK
    order.status = OrderStatus.OUT_FOR_DELIVERY
    order.delivery_address_id = address.id
    db.session.flush()

    payment = Payment(
        order_id=order.id,
        user_id=order.user_id,
        payment_method=PaymentMethod.CLICK,
        status=PaymentStatus.PENDING,
        amount=order.total_amount,
        amount_collected=Decimal("0.00"),
        outstanding_amount=order.total_amount,
        currency="UZS",
        payment_id=f"click_case_c_{order.id}",
        # handle_prepare stamps these on every real order. Their presence is what
        # made the old code resolve this as Case D ("-4 Already paid") and lose
        # the money; tests that omit them cannot see the bug.
        provider_data={"click": {"click_trans_id": "940001", "click_paydoc_id": "5231141285"}},
    )
    db.session.add(payment)

    delivery = Delivery(
        order_id=order.id,
        delivery_person_id=driver.id,
        status=DeliveryStatus.ARRIVED,
        scheduled_date=datetime.now(timezone.utc),
        scheduled_time_slot="09:00-12:00",
    )
    db.session.add(delivery)
    db.session.commit()
    return payment, delivery


def _deliver_with_cash(driver, delivery, amount):
    from business_app.services.staff_service import StaffService

    with patch("business_app.tasks.notification_tasks.send_delivery_update_task.delay"), \
         patch("business_app.tasks.delivery_tasks.optimize_driver_route_task.delay"):
        StaffService.update_delivery_status(
            delivery_id=delivery.id,
            new_status="delivered",
            staff_user_id=driver.id,
            metadata={"cash_collected": str(amount)},
        )


def _post_late_complete(client, order, payment):
    form = make_click_webhook_form(
        action="1",
        click_trans_id="940001",
        merchant_trans_id=order.order_number,
        amount=str(int(order.total_amount)),
        secret_key=TEST_CLICK_SHOP_SECRET_KEY,
        merchant_prepare_id=str(payment.id),
        error=0,
        click_paydoc_id="5231141285",
    )
    return client.post(WEBHOOK_URL, data=form, content_type="application/x-www-form-urlencoded")


@pytest.mark.integration
class TestCaseCLateClickAfterCashAtTheDoor:
    def test_the_driver_cash_becomes_customer_credit_and_is_not_destroyed(
        self, matrix_client, matrix_app, db, order_with_address, delivery_driver,
        driver_profile, no_fiscalization
    ):
        order = order_with_address
        payment, delivery = _click_order_at_the_door(db, order, driver_profile)
        total = order.total_amount

        _deliver_with_cash(delivery_driver, delivery, total)
        db.session.expire_all()

        # Case A happened: converted to cash, order settled by the driver.
        assert Payment.query.get(payment.id).payment_method == PaymentMethod.CASH

        resp = _post_late_complete(matrix_client, order, payment)
        assert resp.status_code == 200

        db.session.expire_all()
        payment = Payment.query.get(payment.id)
        order = Order.query.get(order.id)

        # The order settles on the rail that actually processed the money.
        assert payment.payment_method == PaymentMethod.CLICK, "the Click rail must be restored"
        assert payment.status == PaymentStatus.COMPLETED
        assert order.is_paid is True
        assert order.payment_method == PaymentMethod.CLICK

        # 🔴 The banknotes must survive as customer credit, not vanish.
        credit = (
            db.session.query(CashCollectionEvent)
            .filter(CashCollectionEvent.customer_id == order.user_id,
                    CashCollectionEvent.voided_at.is_(None))
            .all()
        )
        unapplied = sum(Decimal(str(e.unapplied_amount or 0)) for e in credit)
        assert unapplied == Decimal(str(total)), (
            "the driver's cash must become the customer's prepaid credit, "
            f"expected {total} got {unapplied}"
        )

        # And the allocation that pointed at the payment must be reversed, not deleted.
        allocations = CashCollectionAllocation.query.filter_by(payment_id=payment.id).all()
        assert allocations, "the original allocation row must be preserved as audit trail"
        assert all(a.reversed_at is not None for a in allocations)

    def test_the_driver_cash_session_is_left_intact(
        self, matrix_client, matrix_app, db, order_with_address, delivery_driver,
        driver_profile, no_fiscalization
    ):
        """The driver really did hand over banknotes. Re-booking the money as
        customer credit must not change what the driver owes the office."""
        order = order_with_address
        payment, delivery = _click_order_at_the_door(db, order, driver_profile)
        _deliver_with_cash(delivery_driver, delivery, order.total_amount)
        db.session.expire_all()

        before = [
            (e.id, e.amount, e.driver_cash_session_id)
            for e in CashCollectionEvent.query.filter(
                CashCollectionEvent.driver_cash_session_id.isnot(None)
            ).all()
        ]

        _post_late_complete(matrix_client, order, payment)
        db.session.expire_all()

        after = [
            (e.id, e.amount, e.driver_cash_session_id)
            for e in CashCollectionEvent.query.filter(
                CashCollectionEvent.driver_cash_session_id.isnot(None)
            ).all()
        ]
        assert before == after, "the driver's banked cash and session must be untouched"

    def test_fiscalization_is_driven_for_the_click_money(
        self, matrix_client, matrix_app, db, order_with_address, delivery_driver, driver_profile
    ):
        """Click processed real money, so a receipt is owed. Case A had released
        the marking codes, so this must re-reserve and fiscalize."""
        order = order_with_address
        payment, delivery = _click_order_at_the_door(db, order, driver_profile)
        _deliver_with_cash(delivery_driver, delivery, order.total_amount)
        db.session.expire_all()

        with patch(
            "business_app.services.payment_service.PaymentService.queue_click_fiscalization"
        ) as queue:
            _post_late_complete(matrix_client, order, payment)

        assert queue.called, "the Click debit must be fiscalized"

    def test_replaying_the_same_complete_is_idempotent(
        self, matrix_client, matrix_app, db, order_with_address, delivery_driver,
        driver_profile, no_fiscalization
    ):
        order = order_with_address
        payment, delivery = _click_order_at_the_door(db, order, driver_profile)
        total = order.total_amount
        _deliver_with_cash(delivery_driver, delivery, total)
        db.session.expire_all()

        _post_late_complete(matrix_client, order, payment)
        _post_late_complete(matrix_client, order, payment)
        db.session.expire_all()

        unapplied = sum(
            Decimal(str(e.unapplied_amount or 0))
            for e in CashCollectionEvent.query.filter(
                CashCollectionEvent.customer_id == order.user_id,
                CashCollectionEvent.voided_at.is_(None),
            ).all()
        )
        assert unapplied == Decimal(str(total)), "a replay must not double-credit the customer"
        assert Payment.query.get(payment.id).status == PaymentStatus.COMPLETED


@pytest.mark.integration
class TestCaseCReReserveIsAllOrNothing:
    """`_restore_click_rail_after_offline_settlement` wraps its re-reserve in a
    bare `except Exception` and carries on — deliberately, because the payment
    really was debited and `payment.status == COMPLETED` must survive.

    That swallow used to make this site WORSE than `handle_prepare`: the
    sequential reserve loop had already RESERVEd product A's code before
    product B's shortfall raised, `handle_click_webhook` committed, and because
    the payment ends COMPLETED the order-cancel release cascade can never fire
    to free it. The reservation is now all-or-nothing, so the swallow has
    nothing to swallow but a clean refusal.
    """

    def test_a_short_pool_leaves_no_reservation_but_the_payment_still_completes(
        self, matrix_client, matrix_app, db, order_with_address, sample_product,
        delivery_driver, driver_profile, no_fiscalization,
        two_line_order_with_one_short_pool,
    ):
        from business_app.models.order import OrderItemMarkingCodeAllocation
        from business_app.models.product import ProductMarkingCode
        from shared.enums import MarkingCodeStatus

        order = order_with_address
        product_a, _product_b = two_line_order_with_one_short_pool(order, sample_product)
        payment, delivery = _click_order_at_the_door(db, order, driver_profile)

        _deliver_with_cash(delivery_driver, delivery, order.total_amount)
        db.session.expire_all()

        resp = _post_late_complete(matrix_client, order, payment)
        assert resp.status_code == 200

        db.session.expire_all()
        payment = Payment.query.get(payment.id)

        # The swallow is load-bearing: real money moved on the Click rail.
        assert payment.status == PaymentStatus.COMPLETED
        assert payment.payment_method == PaymentMethod.CLICK

        # ...but the refused reservation must have left nothing behind.
        assert ProductMarkingCode.query.filter_by(
            product_id=product_a.id, status=MarkingCodeStatus.RESERVED
        ).count() == 0, (
            "product A's code must not be stranded RESERVED on a COMPLETED payment "
            "whose order can never drive the cancel-release cascade"
        )
        assert ProductMarkingCode.query.filter_by(
            product_id=product_a.id, status=MarkingCodeStatus.AVAILABLE
        ).count() == 1
        assert OrderItemMarkingCodeAllocation.query.filter_by(order_id=order.id).count() == 0
