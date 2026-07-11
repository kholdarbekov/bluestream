"""Regression tests for subscription task date filtering."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from business_app import db
from business_app.models.order import Order
from business_app.models.payment import Payment
from business_app.models.subscription import Subscription, SubscriptionItem
from business_app.models.user import UserAddress
from business_app.services.subscription_service import SubscriptionService
from business_app.tasks.subscription_tasks import (
    cancel_subscription_deliveries_task,
    create_subscription_delivery_task,
    generate_subscription_churn_prediction,
    handle_failed_subscription_payments,
    process_daily_subscription_billing,
    process_subscription_billing,
    send_renewal_reminders,
)
from shared.enums import OrderStatus, PaymentMethod, PaymentStatus, SubscriptionFrequency, SubscriptionStatus

# Reused rather than re-declared: an ACTIVE cash subscription for 2 units of
# sample_product, 10% discount, past next_billing_date, one item. See the
# gotchas in that module's docstring for why the fixtures duplicate this
# exact shape rather than a subtly different one.
from tests.unit.test_subscription_order_parity import sample_subscription  # noqa: F401
def _create_address(sample_user):
    address = UserAddress(
        user_id=sample_user.id,
        title="Home",
        full_address="Test address",
        city="Tashkent",
        country="Uzbekistan",
        is_default=True,
    )
    db.session.add(address)
    db.session.commit()
    return address


def _create_subscription(sample_user, address, subscription_number, next_billing_date):
    subscription = Subscription(
        subscription_number=subscription_number,
        user_id=sample_user.id,
        status=SubscriptionStatus.ACTIVE,
        name="Weekly Water",
        description="Scheduled delivery",
        billing_cycle=SubscriptionFrequency.MONTHLY,
        billing_amount=Decimal("10000.00"),
        next_billing_date=next_billing_date,
        delivery_frequency=SubscriptionFrequency.WEEKLY,
        delivery_day_of_week=1,
        delivery_address_id=address.id,
        next_delivery_date=next_billing_date,
        start_date=datetime.now(timezone.utc) - timedelta(days=7),
        payment_method=PaymentMethod.CARD,
    )
    db.session.add(subscription)
    db.session.commit()
    return subscription


def test_process_daily_subscription_billing_uses_end_of_day_cutoff(db, sample_user):
    address = _create_address(sample_user)
    due_today = _create_subscription(
        sample_user,
        address,
        "SUB-DUE-TODAY",
        datetime.now(timezone.utc).replace(hour=23, minute=0, second=0, microsecond=0),
    )
    _create_subscription(
        sample_user,
        address,
        "SUB-DUE-TOMORROW",
        (datetime.now(timezone.utc) + timedelta(days=1)).replace(hour=1, minute=0, second=0, microsecond=0),
    )

    with patch("business_app.tasks.subscription_tasks.SubscriptionService") as subscription_service_cls:
        subscription_service_cls.return_value.process_subscription_billing.return_value = {"success": True, "order_id": 1}
        result = process_daily_subscription_billing()

    assert result["successful"] == 1
    subscription_service_cls.return_value.process_subscription_billing.assert_called_once_with(due_today.id)


def test_send_renewal_reminders_uses_billing_fields_and_day_range(db, sample_user):
    address = _create_address(sample_user)
    reminder_target = (datetime.now(timezone.utc) + timedelta(days=3)).replace(hour=12, minute=0, second=0, microsecond=0)
    due_for_reminder = _create_subscription(
        sample_user,
        address,
        "SUB-REMINDER",
        reminder_target,
    )
    _create_subscription(
        sample_user,
        address,
        "SUB-NO-REMINDER",
        reminder_target + timedelta(days=1),
    )

    with patch("business_app.tasks.subscription_tasks.NotificationService") as notification_service_cls:
        result = send_renewal_reminders()

    assert result["sent_count"] == 1
    notification_service_cls.return_value.send_notification.assert_called_once()
    call_args = notification_service_cls.return_value.send_notification.call_args
    assert call_args.args[0] == due_for_reminder.user_id
    assert call_args.args[1] == "subscription_renewal_reminder"
    template_data = call_args.kwargs["template_data"]
    assert template_data["plan_name"] == due_for_reminder.name
    assert template_data["amount"] == due_for_reminder.billing_amount
    assert template_data["frequency"] == due_for_reminder.billing_cycle.value


class TestCreateSubscriptionDeliveryTask:
    """Delivery creation belongs to the CONFIRMED status transition
    (order_service._handle_status_change_actions), exactly as it does for an
    ordinary order — not to this task. See docs/superpowers/sdd/task-7-brief.md.
    """

    def test_task_does_not_create_a_delivery_itself(
        self, app, db, sample_user, sample_product, user_address, sample_subscription
    ):
        # sample_user has no DELIVERED order history, so this is a first-time
        # COD customer: the order stays PENDING and nothing should try to
        # create a delivery for it — least of all the task's own body.
        with app.app_context(), patch(
            "business_app.services.delivery_service.DeliveryService.create_delivery"
        ) as create_delivery:
            result = create_subscription_delivery_task(sample_subscription.id)

        assert result["success"] is True
        create_delivery.assert_not_called()

    def test_skipped_billing_returns_without_key_error(self, app, db, sample_user):
        # The already-billed skip returns success=True with NO order_id. Reading
        # billing_result["order_id"] unconditionally raises KeyError.
        with app.app_context(), patch.object(
            SubscriptionService,
            "process_subscription_billing",
            return_value={"success": True, "skipped": True, "reason": "already_billed_this_cycle"},
        ):
            result = create_subscription_delivery_task(1)

        assert result == {"success": True, "skipped": True, "reason": "already_billed_this_cycle"}

    def test_returning_customer_cod_order_is_confirmed_with_a_delivery_and_the_task_does_not_raise(
        self, app, db, sample_user, sample_product, user_address, sample_subscription
    ):
        # THE regression test. A returning customer's COD order instant-confirms
        # inside create_order, and the CONFIRMED handler creates the delivery.
        # Before the fix, this task's own unconditional create_delivery(order_id)
        # call then raised ValidationError("Delivery already exists for this
        # order"), which the task's except-block turned into self.retry(exc).
        with app.app_context():
            prior_order = Order(
                user_id=sample_user.id,
                status=OrderStatus.DELIVERED,
                subtotal=Decimal("50000.00"),
                total_amount=Decimal("50000.00"),
                delivery_address_id=user_address.id,
                payment_method=PaymentMethod.CASH,
            )
            db.session.add(prior_order)
            db.session.commit()

            result = create_subscription_delivery_task(sample_subscription.id)

            assert result["success"] is True
            order = Order.query.get(result["order_id"])
            assert order.status is OrderStatus.CONFIRMED
            assert order.delivery is not None
            assert result["delivery_id"] == order.delivery.id


def _make_subscription(db, sample_user, sample_product, address):
    """A minimal ACTIVE cash subscription. Mirrors the `sample_subscription`
    fixture in test_subscription_order_parity.py — same shape, deliberately
    not reused as a fixture here because we need two independent instances
    (A and B) per test, not the shared fixture's single instance.
    """
    subscription = Subscription(
        user_id=sample_user.id,
        name="Weekly Water",
        status=SubscriptionStatus.ACTIVE,
        billing_cycle=SubscriptionFrequency.WEEKLY,
        delivery_frequency=SubscriptionFrequency.WEEKLY,
        delivery_address_id=address.id,
        payment_method=PaymentMethod.CASH,
        auto_renew=True,
        discount_percentage=10.0,
        billing_amount=Decimal("0.00"),
        start_date=datetime.now(timezone.utc),
        next_billing_date=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    db.session.add(subscription)
    db.session.flush()
    db.session.add(
        SubscriptionItem(
            subscription_id=subscription.id,
            product_id=sample_product.id,
            quantity=2,
            unit_price=sample_product.base_price,
            total_price=sample_product.base_price * 2,
        )
    )
    db.session.commit()
    return subscription


def _make_order(db, sample_user, address, *, subscription_id):
    """A bare PENDING cash order stamped with subscription_id.

    No OrderItems/Delivery/Payment are needed for `OrderService.cancel_order`
    to run cleanly on this row: CASH+PENDING skips stock restore entirely
    (goes to inventory_service.release_reservations, which is Redis-only and
    doesn't touch order.items), and the loyalty/corporate release paths both
    no-op when their lookups find nothing for this order_id.
    """
    order = Order(
        user_id=sample_user.id,
        status=OrderStatus.PENDING,
        payment_method=PaymentMethod.CASH,
        subtotal=Decimal("15000.00"),
        total_amount=Decimal("15000.00"),
        delivery_address_id=address.id,
        subscription_id=subscription_id,
    )
    db.session.add(order)
    db.session.commit()
    return order


class TestCancelSubscriptionDeliveriesTask:
    """`Order.subscription_id` is stamped atomically inside `create_order`
    (Task 3) and is the sole authoritative link between a subscription and
    its orders. The task used to filter on
    `Order.notes.contains(f"Subscription order #{subscription_id}")` instead
    — a SQL LIKE that would also match sibling ids sharing a numeric prefix
    (#1 matching #10, #11, #100).

    IMPORTANT DISCREPANCY vs. the task brief: `Order` has NO `notes` column
    (only `delivery_notes`) and never has — grep/git-log confirm it back to
    the "new architecture" commit. `Order.notes.contains(...)` therefore
    raises `AttributeError` while *building* the query, unconditionally,
    the instant this task runs for any existing subscription — regardless
    of what any order's data looks like. So the bug is not merely "matches
    too broadly", it's "always crashes" — this task has never completed
    successfully. `create_order` also never persisted the write-side
    `order_data["notes"]` payload (grep confirms it's read nowhere), so
    the notes-matching mechanism was dead on both ends. Both tests below
    therefore go RED against the old code via the same AttributeError, not
    via a wrong-but-successful cancellation. The fix (trust subscription_id,
    drop notes entirely) resolves both the crash and the never-realized
    collision risk.
    """

    def test_cancels_only_orders_belonging_to_the_subscription(
        self, app, db, sample_user, sample_product, user_address
    ):
        with app.app_context():
            sub_a = _make_subscription(db, sample_user, sample_product, user_address)
            sub_b = _make_subscription(db, sample_user, sample_product, user_address)

            order_a = _make_order(db, sample_user, user_address, subscription_id=sub_a.id)
            order_b = _make_order(db, sample_user, user_address, subscription_id=sub_b.id)

            result = cancel_subscription_deliveries_task(sub_a.id)

            refreshed_a = Order.query.get(order_a.id)
            refreshed_b = Order.query.get(order_b.id)

            assert refreshed_a.status is OrderStatus.CANCELLED
            assert refreshed_b.status is OrderStatus.PENDING, "sibling subscription's order was collateral damage"
            assert result["cancelled_deliveries"] == 1

    def test_cancelling_a_sibling_subscription_matches_zero_orders(self, app, db, sample_user, sample_product, user_address):
        """Direct pin: the task must key off `Order.subscription_id` alone.

        B's order is the only order in the DB and it genuinely belongs to B
        (subscription_id=sub_b.id). Cancelling A must not touch it — there
        is nothing resembling a notes-based match possible here (see class
        docstring for why "notes" isn't even a real column), which is
        exactly the point: subscription_id is the only signal that may be
        trusted, full stop.
        """
        with app.app_context():
            sub_a = _make_subscription(db, sample_user, sample_product, user_address)
            sub_b = _make_subscription(db, sample_user, sample_product, user_address)

            order_b = _make_order(db, sample_user, user_address, subscription_id=sub_b.id)

            result = cancel_subscription_deliveries_task(sub_a.id)

            refreshed_b = Order.query.get(order_b.id)
            assert refreshed_b.status is OrderStatus.PENDING
            assert result["cancelled_deliveries"] == 0


class TestPhantomTotalAmountReads:
    """Task 20 (descoped): ``Subscription`` has no ``total_amount`` column —
    only ``billing_amount`` (last/estimated charge) and ``total_amount_billed``
    (running total). Several call sites read ``subscription.total_amount``
    anyway; each raises ``AttributeError`` the instant its branch runs against
    a real Subscription row. Every occurrence below is wrapped by a
    try/except that swallows the error and ``continue``s, so the bug never
    surfaced as a crash — it silently dropped the affected subscription /
    zeroed out the counter instead.
    """

    def test_handle_failed_subscription_payments_uses_billing_amount_not_phantom_total_amount(
        self, app, db, sample_user, sample_product, user_address
    ):
        """Pre-fix: template_data={"amount": subscription.total_amount} raises
        AttributeError while building the success-notification payload, which
        is swallowed by the per-subscription try/except — so retry_count is
        never incremented even though the retry genuinely succeeded.
        """
        subscription = _make_subscription(db, sample_user, sample_product, user_address)
        subscription.failed_payment_count = 1
        subscription.last_billing_date = datetime.now(timezone.utc)
        subscription.billing_amount = Decimal("15000.00")
        db.session.commit()

        with (
            patch("business_app.tasks.subscription_tasks.SubscriptionService") as subscription_service_cls,
            patch("business_app.tasks.subscription_tasks.NotificationService") as notification_service_cls,
        ):
            subscription_service_cls.return_value.process_subscription_billing.return_value = {"success": True}

            result = handle_failed_subscription_payments()

        assert result["retry_count"] == 1
        template_data = notification_service_cls.return_value.send_notification.call_args.kwargs["template_data"]
        assert template_data["amount"] == 15000.0

    def test_generate_subscription_churn_prediction_uses_billing_amount_not_phantom_total_amount(
        self, app, db, sample_user
    ):
        """Pre-fix: "monthly_value": subscription.total_amount raises
        AttributeError while building the at-risk-customer dict, swallowed by
        the per-subscription try/except — so the at-risk subscription never
        made it into ``predictions``.

        Two other, pre-existing bugs live in this same task body —
        ``subscription.plan`` (Subscription has no ``plan`` relationship at
        all, business_app/models/subscription.py) and
        ``AnalyticsService.store_subscription_churn_predictions`` (method does
        not exist on AnalyticsService) — both unrelated to the total_amount
        fix and explicitly out of scope for Task 20. They are stubbed/patched
        out below purely to isolate the total_amount -> billing_amount
        regression under test; see the Task 20 report for the discovered-bug
        writeup.
        """
        stub_subscription = SimpleNamespace(
            id=1,
            user_id=sample_user.id,
            failed_payment_count=3,
            last_billing_date=datetime.now(timezone.utc) - timedelta(days=40),
            plan=None,
            billing_amount=Decimal("20000.00"),
            user=sample_user,
        )

        with (
            patch("business_app.tasks.subscription_tasks.Subscription") as subscription_model,
            patch(
                "business_app.services.analytics_service.AnalyticsService.store_subscription_churn_predictions",
                return_value=None,
                create=True,
            ),
        ):
            subscription_model.query.filter_by.return_value.all.return_value = [stub_subscription]

            result = generate_subscription_churn_prediction()

        assert "error" not in result
        assert len(result["predictions"]) == 1
        assert result["predictions"][0]["monthly_value"] == 20000.0


class TestSubscriptionBilledNotificationPayload:
    """Task 20 Part 2: the ``subscription_billed`` notification payload gains
    ``order_number`` and ``payment_action_required`` — forward-looking data
    for a future notification-template follow-up (the template itself is
    explicitly descoped). Every access is guarded: neither the order nor its
    payment is guaranteed to exist.
    """

    def test_flags_payment_action_required_for_unpaid_electronic_order(
        self, app, db, sample_user, sample_product, user_address
    ):
        subscription = _make_subscription(db, sample_user, sample_product, user_address)

        order = Order(
            user_id=sample_user.id,
            status=OrderStatus.PENDING,
            payment_method=PaymentMethod.CLICK,
            subtotal=Decimal("15000.00"),
            total_amount=Decimal("15000.00"),
            delivery_address_id=user_address.id,
            subscription_id=subscription.id,
        )
        db.session.add(order)
        db.session.commit()

        payment = Payment(
            order_id=order.id,
            user_id=sample_user.id,
            payment_method=PaymentMethod.CLICK,
            amount=Decimal("15000.00"),
            currency="UZS",
            status=PaymentStatus.PENDING,
            payment_id="sub_billed_test_click_unpaid",
        )
        db.session.add(payment)
        db.session.commit()

        with (
            patch("business_app.tasks.subscription_tasks.SubscriptionService") as subscription_service_cls,
            patch("business_app.tasks.subscription_tasks.NotificationService") as notification_service_cls,
        ):
            subscription_service_cls.return_value.process_subscription_billing.return_value = {
                "success": True,
                "order_id": order.id,
                "amount": 15000.0,
                "next_billing_date": subscription.next_billing_date.isoformat(),
            }

            process_subscription_billing(subscription.id)

        call_args = notification_service_cls.return_value.send_notification.call_args
        assert call_args.args[1] == "subscription_billed"
        template_data = call_args.kwargs["template_data"]
        assert template_data["order_number"] == order.order_number
        assert template_data["payment_action_required"] is True

    def test_no_payment_action_required_for_cash_order_with_no_payment_row(
        self, app, db, sample_user, sample_product, user_address
    ):
        subscription = _make_subscription(db, sample_user, sample_product, user_address)

        order = Order(
            user_id=sample_user.id,
            status=OrderStatus.PENDING,
            payment_method=PaymentMethod.CASH,
            subtotal=Decimal("15000.00"),
            total_amount=Decimal("15000.00"),
            delivery_address_id=user_address.id,
            subscription_id=subscription.id,
        )
        db.session.add(order)
        db.session.commit()

        with (
            patch("business_app.tasks.subscription_tasks.SubscriptionService") as subscription_service_cls,
            patch("business_app.tasks.subscription_tasks.NotificationService") as notification_service_cls,
        ):
            subscription_service_cls.return_value.process_subscription_billing.return_value = {
                "success": True,
                "order_id": order.id,
                "amount": 15000.0,
                "next_billing_date": subscription.next_billing_date.isoformat(),
            }

            process_subscription_billing(subscription.id)

        template_data = notification_service_cls.return_value.send_notification.call_args.kwargs["template_data"]
        assert template_data["order_number"] == order.order_number
        assert template_data["payment_action_required"] is False

    def test_guards_against_a_missing_order(self, app, db, sample_user, sample_product, user_address):
        subscription = _make_subscription(db, sample_user, sample_product, user_address)

        with (
            patch("business_app.tasks.subscription_tasks.SubscriptionService") as subscription_service_cls,
            patch("business_app.tasks.subscription_tasks.NotificationService") as notification_service_cls,
        ):
            subscription_service_cls.return_value.process_subscription_billing.return_value = {
                "success": True,
                "order_id": 999999999,
                "amount": 15000.0,
                "next_billing_date": subscription.next_billing_date.isoformat(),
            }

            result = process_subscription_billing(subscription.id)

        assert result["success"] is True
        template_data = notification_service_cls.return_value.send_notification.call_args.kwargs["template_data"]
        assert template_data["order_number"] is None
        assert template_data["payment_action_required"] is False
