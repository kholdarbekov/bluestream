"""Subscription Celery tasks: the daily job and the first-order task (spec 2026-10-23 §4.3)."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from business_app.models.delivery import Delivery
from business_app.models.order import Order
from business_app.models.subscription import Subscription, SubscriptionItem
from business_app.services.subscription_service import SubscriptionService
from business_app.tasks.subscription_tasks import (
    create_subscription_delivery_task,
    generate_subscription_churn_prediction,
    process_daily_subscription_billing,
)
from business_app.utils.local_windows import local_date
from shared.enums import OrderStatus, PaymentMethod, SubscriptionFrequency, SubscriptionStatus

# Reused rather than re-declared: an ACTIVE cash subscription for 2 units of
# sample_product, 10% discount, past next_delivery_date, one item. See the
# gotchas in that module's docstring for why the fixtures duplicate this
# exact shape rather than a subtly different one.
from tests.unit.test_subscription_order_parity import sample_subscription  # noqa: F401


def test_the_daily_job_creates_the_orders_due_within_the_lead_and_no_others(
    app, db, sample_user, sample_product, user_address
):
    """`next_delivery_date <= today + lead` (one day): tomorrow's order is created today, the day
    after's is not."""
    today = local_date()
    due = _make_subscription(
        db, sample_user, sample_product, user_address, next_delivery_date=today + timedelta(days=1)
    )
    later = _make_subscription(
        db, sample_user, sample_product, user_address, next_delivery_date=today + timedelta(days=2)
    )

    result = process_daily_subscription_billing()

    assert (result["total_processed"], result["successful"], result["failed"]) == (1, 1, 0)
    assert [o.delivery_date for o in Order.query.filter_by(subscription_id=due.id)] == [today + timedelta(days=1)]
    assert Order.query.filter_by(subscription_id=later.id).count() == 0


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

    def test_a_subscription_paused_before_the_worker_runs_gets_no_order_and_no_retry(
        self, app, db, sample_user, sample_product, user_address, sample_subscription
    ):
        with app.app_context():
            subscription = Subscription.query.get(sample_subscription.id)
            subscription.status = SubscriptionStatus.PAUSED
            db.session.commit()

            result = create_subscription_delivery_task(sample_subscription.id)

            assert result == {
                "success": False,
                "subscription_id": sample_subscription.id,
                # The `create_next_order` guard's key (spec 2026-10-26 B1): the same one admin is published.
                "error": "api.subscriptions.action.create_next_order.not_active",
            }
            assert Order.query.filter_by(subscription_id=sample_subscription.id).count() == 0

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
            # Released now or held until its day (OrderScheduleService), but never two deliveries.
            assert Delivery.query.filter_by(order_id=order.id).count() <= 1


def _make_subscription(db, sample_user, sample_product, address, next_delivery_date=None):
    """A minimal ACTIVE cash subscription. Mirrors the `sample_subscription`
    fixture in test_subscription_order_parity.py — same shape, deliberately
    not reused as a fixture here because we need two independent instances
    (A and B) per test, not the shared fixture's single instance.
    """
    subscription = Subscription(
        user_id=sample_user.id,
        name="Weekly Water",
        status=SubscriptionStatus.ACTIVE,
        frequency=SubscriptionFrequency.WEEKLY,
        delivery_address_id=address.id,
        payment_method=PaymentMethod.CASH,
        auto_renew=True,
        discount_percentage=10.0,
        billing_amount=Decimal("0.00"),
        start_date=datetime.now(timezone.utc),
        next_delivery_date=next_delivery_date or local_date(),
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


class TestCancelTouchesOnlyItsOwnOrders:
    """`Order.subscription_id` is the sole authoritative link between a subscription and its orders
    (it once matched on a `notes` LIKE, which would also hit sibling ids sharing a numeric prefix).
    Cancelling a subscription cancels its own unpaid upcoming orders and nothing else (§4.6)."""

    def test_cancels_only_orders_belonging_to_the_subscription(
        self, app, db, sample_user, sample_product, user_address
    ):
        with app.app_context():
            sub_a = _make_subscription(db, sample_user, sample_product, user_address)
            sub_b = _make_subscription(db, sample_user, sample_product, user_address)

            order_a = _make_order(db, sample_user, user_address, subscription_id=sub_a.id)
            order_b = _make_order(db, sample_user, user_address, subscription_id=sub_b.id)

            outcome = SubscriptionService().cancel_subscription_for_user(
                sub_a.id, sample_user.id, "Moving", expected_state=SubscriptionService.state_token(sub_a)
            )

            assert Order.query.get(order_a.id).status is OrderStatus.CANCELLED
            assert Order.query.get(order_b.id).status is OrderStatus.PENDING, "sibling's order was collateral damage"
            assert [o["id"] for o in outcome["cancelled_orders"]] == [order_a.id]

    def test_cancelling_a_sibling_subscription_matches_zero_orders(
        self, app, db, sample_user, sample_product, user_address
    ):
        with app.app_context():
            sub_a = _make_subscription(db, sample_user, sample_product, user_address)
            sub_b = _make_subscription(db, sample_user, sample_product, user_address)

            order_b = _make_order(db, sample_user, user_address, subscription_id=sub_b.id)

            outcome = SubscriptionService().cancel_subscription_for_user(
                sub_a.id, sample_user.id, "Moving", expected_state=SubscriptionService.state_token(sub_a)
            )

            assert Order.query.get(order_b.id).status is OrderStatus.PENDING
            assert outcome["cancelled_orders"] == []


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
            last_delivery_date=(datetime.now(timezone.utc) - timedelta(days=40)).date(),
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
