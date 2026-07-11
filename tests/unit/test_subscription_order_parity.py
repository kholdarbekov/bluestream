"""Subscription-generated orders must be ordinary orders that carry a
subscription_id. See docs/superpowers/specs/2026-07-10-subscription-order-parity-design.md
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest

from business_app.models.order import Order
from business_app.models.payment import Payment
from business_app.models.subscription import Subscription, SubscriptionItem
from business_app.models.user import UserAddress
from business_app.serializers.subscription_serializers import (
    AdminCreateSubscriptionRequest,
    CreateSubscriptionRequest,
)
from business_app.services.cash_collection_service import CashCollectionService
from business_app.services.order_service import OrderService
from business_app.services.subscription_service import SubscriptionService
from business_app.utils.exceptions import ValidationError
from shared.enums import (
    CashCollectionSource,
    OrderStatus,
    PaymentMethod,
    SubscriptionFrequency,
    SubscriptionStatus,
)


def _order_data(sample_product, address_id, **overrides):
    data = {
        "items": [{"product_id": sample_product.id, "quantity": 2}],
        "delivery_address": {
            "delivery_address_id": address_id,
            "street": "1 Test St",
            "latitude": 41.3111,
            "longitude": 69.2797,
        },
    }
    data.update(overrides)
    return data


class TestResolvePaymentMethod:
    def test_card_is_normalized_to_click_on_the_order_row(self, app, db, sample_user, sample_product, user_address):
        with app.app_context():
            order = OrderService().create_order(
                sample_user.id,
                _order_data(sample_product, user_address.id, payment_method="card"),
            )
            assert order.payment_method is PaymentMethod.CLICK

    def test_missing_payment_method_raises_instead_of_persisting_null(
        self, app, db, sample_user, sample_product, user_address
    ):
        with app.app_context():
            with pytest.raises(ValidationError, match="payment_method"):
                OrderService().create_order(sample_user.id, _order_data(sample_product, user_address.id))

    def test_unknown_payment_method_raises_instead_of_persisting_null(
        self, app, db, sample_user, sample_product, user_address
    ):
        with app.app_context():
            with pytest.raises(ValidationError, match="payment method"):
                OrderService().create_order(
                    sample_user.id,
                    _order_data(sample_product, user_address.id, payment_method="bitcoin"),
                )

    def test_loyalty_points_is_rejected(self, app, db, sample_user, sample_product, user_address):
        with app.app_context():
            with pytest.raises(ValidationError):
                OrderService().create_order(
                    sample_user.id,
                    _order_data(sample_product, user_address.id, payment_method="loyalty_points"),
                )

    def test_payme_is_rejected_for_new_orders(self, app, db, sample_user, sample_product, user_address):
        with app.app_context():
            with pytest.raises(ValidationError):
                OrderService().create_order(
                    sample_user.id,
                    _order_data(sample_product, user_address.id, payment_method="payme"),
                )

    def test_cash_order_gets_exactly_one_payment_for_the_order_total(
        self, app, db, sample_user, sample_product, user_address
    ):
        with app.app_context():
            order = OrderService().create_order(
                sample_user.id,
                _order_data(sample_product, user_address.id, payment_method="cash"),
            )
            db.session.refresh(order)
            assert order.payment is not None
            assert order.payment.payment_method is PaymentMethod.CASH
            assert Decimal(order.payment.amount) == Decimal(order.total_amount)


class TestRepeatOrderLegacyMethods:
    def _delivered_order(self, db, user, product, address, method):
        from business_app.models.order import Order, OrderItem
        from shared.enums import OrderStatus

        order = Order(
            user_id=user.id,
            status=OrderStatus.DELIVERED,
            subtotal=Decimal("50000.00"),
            total_amount=Decimal("50000.00"),
            delivery_address_id=address.id,
            payment_method=method,
        )
        db.session.add(order)
        db.session.flush()
        # quantity=2: repeat_order_for_user recomputes the repeat's subtotal
        # from the product's CURRENT base_price (sample_product = 15000), not
        # this row's historical unit_price. quantity=1 would price the repeat
        # at 15000 — under MIN_ORDER_AMOUNT (20000) — and trip an unrelated
        # "Minimum order amount" ValidationError before payment-method
        # resolution is ever reached. Matches the quantity=2 already used by
        # TestResolvePaymentMethod in this same module for the same reason.
        db.session.add(
            OrderItem(
                order_id=order.id,
                product_id=product.id,
                quantity=2,
                unit_price=Decimal("25000.00"),
                total_price=Decimal("50000.00"),
            )
        )
        db.session.commit()
        return order

    def test_repeating_a_legacy_payme_order_produces_a_click_order(
        self, app, db, sample_user, sample_product, user_address
    ):
        with app.app_context():
            original = self._delivered_order(db, sample_user, sample_product, user_address, PaymentMethod.PAYME)
            repeated = OrderService().repeat_order_for_user(original.id, sample_user.id)
            assert repeated.payment_method is PaymentMethod.CLICK

    def test_repeating_a_cash_order_stays_cash(self, app, db, sample_user, sample_product, user_address):
        with app.app_context():
            original = self._delivered_order(db, sample_user, sample_product, user_address, PaymentMethod.CASH)
            repeated = OrderService().repeat_order_for_user(original.id, sample_user.id)
            assert repeated.payment_method is PaymentMethod.CASH

    def test_repeating_a_legacy_null_method_order_raises_a_clear_error(
        self, app, db, sample_user, sample_product, user_address
    ):
        with app.app_context():
            original = self._delivered_order(db, sample_user, sample_product, user_address, None)
            with pytest.raises(ValidationError) as excinfo:
                OrderService().repeat_order_for_user(original.id, sample_user.id)
            # Assert on the machine-readable code, not the translated copy —
            # the message text changes once the translation is seeded.
            assert excinfo.value.error_code == "REPEAT_LEGACY_ORDER_UNSUPPORTED"


def test_dead_reorder_method_is_gone():
    # `reorder` had zero callers, omitted payment_method AND delivery_address_id,
    # and raised KeyError inside create_order. `repeat_order_for_user` is the
    # live implementation.
    assert not hasattr(OrderService, "reorder")
    assert hasattr(OrderService, "repeat_order_for_user")


@pytest.fixture
def sample_subscription(db, sample_user, sample_product, user_address):
    """An ACTIVE cash subscription for 2 units of sample_product, 10% discount."""
    subscription = Subscription(
        user_id=sample_user.id,
        name="Weekly Water",
        status=SubscriptionStatus.ACTIVE,
        billing_cycle=SubscriptionFrequency.WEEKLY,
        delivery_frequency=SubscriptionFrequency.WEEKLY,
        delivery_address_id=user_address.id,
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
            # total_price is NOT NULL with no server default; the brief's
            # snippet omitted it. SubscriptionItem.calculate_total() derives
            # it, but nothing calls that for us here, so set it explicitly.
            total_price=sample_product.base_price * 2,
        )
    )
    db.session.commit()
    return subscription


class TestCreateOrderWithSubscription:
    def test_subscription_kwarg_stamps_origin_atomically(
        self, app, db, sample_user, sample_product, user_address, sample_subscription
    ):
        with app.app_context():
            order = OrderService().create_order(
                sample_user.id,
                _order_data(sample_product, user_address.id, payment_method="cash"),
                subscription=sample_subscription,
            )
            assert order.subscription_id == sample_subscription.id
            assert order.is_subscription_order is True
            assert order.payment_method is PaymentMethod.CASH

    def test_subscription_discount_lands_on_discount_amount_and_total(
        self, app, db, sample_user, sample_product, user_address, sample_subscription
    ):
        with app.app_context():
            order = OrderService().create_order(
                sample_user.id,
                _order_data(sample_product, user_address.id, payment_method="cash"),
                subscription=sample_subscription,
            )
            expected_discount = (Decimal(order.subtotal) * Decimal("10") / Decimal("100")).quantize(Decimal("0.01"))
            assert Decimal(order.discount_amount) == expected_discount
            assert Decimal(order.total_amount) == (
                Decimal(order.subtotal) - expected_discount + Decimal(order.delivery_fee)
            )

    def test_payment_amount_equals_discounted_order_total(
        self, app, db, sample_user, sample_product, user_address, sample_subscription
    ):
        # The whole point: Payment.amount can no longer diverge from the order.
        with app.app_context():
            order = OrderService().create_order(
                sample_user.id,
                _order_data(sample_product, user_address.id, payment_method="cash"),
                subscription=sample_subscription,
            )
            db.session.refresh(order)
            assert Decimal(order.payment.amount) == Decimal(order.total_amount)

    def test_ordinary_order_has_zero_discount_and_no_subscription(
        self, app, db, sample_user, sample_product, user_address
    ):
        with app.app_context():
            order = OrderService().create_order(
                sample_user.id,
                _order_data(sample_product, user_address.id, payment_method="cash"),
            )
            assert Decimal(order.discount_amount) == Decimal("0.00")
            assert order.subscription_id is None
            assert order.is_subscription_order is False

    def test_min_order_amount_is_checked_on_gross_not_post_discount(
        self, app, db, sample_user, sample_product, user_address, sample_subscription
    ):
        # A basket above MIN_ORDER_AMOUNT must not be rejected merely because a
        # subscription discount pushes the net below the floor.
        # `_order_data` orders quantity=2, so subtotal = 2 * base_price.
        # gross    = 42000  (>= MIN_ORDER_AMOUNT 20000, so creation is allowed)
        # discount = 37800  (90%)
        # total    =  4200  (< MIN_ORDER_AMOUNT — which must NOT cause a raise)
        from shared.business_config import MIN_ORDER_AMOUNT

        with app.app_context():
            sample_product.base_price = Decimal(str(MIN_ORDER_AMOUNT + 1000))
            sample_subscription.discount_percentage = 90.0
            db.session.commit()

            order = OrderService().create_order(
                sample_user.id,
                _order_data(sample_product, user_address.id, payment_method="cash"),
                subscription=sample_subscription,
            )
            assert Decimal(order.subtotal) == Decimal("42000.00")
            assert Decimal(order.total_amount) == Decimal("4200.00")
            assert Decimal(order.total_amount) < Decimal(str(MIN_ORDER_AMOUNT))

    def test_a_hundred_percent_discount_is_rejected_because_the_total_would_be_zero(
        self, app, db, sample_user, sample_product, user_address, sample_subscription
    ):
        # Delivery is free (DEFAULT_DELIVERY_FEE=0), so a 100% discount yields a
        # zero-total order. There is nothing to pay and nothing to collect, and
        # initialize_order_payment cannot mint a zero-amount Payment. Reject it
        # at the door with a clear message rather than persisting a degenerate row.
        with app.app_context():
            sample_subscription.discount_percentage = 100.0
            db.session.commit()
            with pytest.raises(ValidationError, match="positive"):
                OrderService().create_order(
                    sample_user.id,
                    _order_data(sample_product, user_address.id, payment_method="cash"),
                    subscription=sample_subscription,
                )


class TestPersonalCardTransferOnSubscriptionOrder:
    """THE regression test. Before this change the collection raised
    ValidationError("Only COD orders can be targeted for COD collections")
    because Order.payment_method was NULL."""

    def test_admin_can_record_personal_card_transfer_on_a_subscription_cod_order(
        self, app, db, sample_user, admin_user, sample_product, user_address, sample_subscription
    ):
        with app.app_context():
            result = SubscriptionService().process_subscription_billing(sample_subscription.id)
            assert result["success"] is True

            order = Order.query.get(result["order_id"])
            assert order.payment_method is PaymentMethod.CASH, "subscription order must carry a real method"
            assert order.subscription_id == sample_subscription.id
            assert order.status is not OrderStatus.CANCELLED

            event = CashCollectionService().post_collection(
                customer_id=sample_user.id,
                amount=Decimal(order.total_amount),
                source=CashCollectionSource.PERSONAL_CARD_TRANSFER.value,
                recorded_by_user_id=admin_user.id,
                order_id=order.id,
                notes="Customer transferred to owner personal card.",
            )

            assert event.id is not None
            assert event.source is CashCollectionSource.PERSONAL_CARD_TRANSFER
            assert event.driver_cash_session_id is None

            db.session.refresh(order)
            assert order.is_paid is True


class TestSubscriptionBillingCollapse:
    def test_billing_creates_exactly_one_payment_for_the_order_total(
        self, app, db, sample_user, sample_product, user_address, sample_subscription
    ):
        with app.app_context():
            result = SubscriptionService().process_subscription_billing(sample_subscription.id)
            order = Order.query.get(result["order_id"])

            payments = Payment.query.filter_by(order_id=order.id).all()
            assert len(payments) == 1
            assert Decimal(payments[0].amount) == Decimal(order.total_amount)
            assert payments[0].payment_method is PaymentMethod.CASH

    def test_billing_amount_is_refreshed_to_the_actual_charge(
        self, app, db, sample_user, sample_product, user_address, sample_subscription
    ):
        with app.app_context():
            result = SubscriptionService().process_subscription_billing(sample_subscription.id)
            order = Order.query.get(result["order_id"])
            # Re-query rather than db.session.refresh(sample_subscription): this
            # `with app.app_context()` pushes a second, distinct Flask app context,
            # and Flask-SQLAlchemy 3.x scopes db.session per app-context id
            # (flask_sqlalchemy.session._app_ctx_id), so `db.session` here is a
            # different Session than the one `sample_subscription` was loaded
            # under — refresh() would raise "not persistent within this Session".
            # Matches the query-by-id idiom already used throughout
            # test_subscription_billing_comprehensive.py for the same reason.
            refreshed = Subscription.query.get(sample_subscription.id)
            assert Decimal(refreshed.billing_amount) == Decimal(order.total_amount)

    def test_total_amount_billed_accumulates(
        self, app, db, sample_user, sample_product, user_address, sample_subscription
    ):
        # Regression: total_amount_billed was never incremented, so the
        # "total savings" calculation always read zero.
        with app.app_context():
            result = SubscriptionService().process_subscription_billing(sample_subscription.id)
            order = Order.query.get(result["order_id"])
            refreshed = Subscription.query.get(sample_subscription.id)
            assert Decimal(refreshed.total_amount_billed) == Decimal(order.total_amount)

    def test_counters_advance(self, app, db, sample_user, sample_product, user_address, sample_subscription):
        with app.app_context():
            before = sample_subscription.next_billing_date
            SubscriptionService().process_subscription_billing(sample_subscription.id)
            refreshed = Subscription.query.get(sample_subscription.id)
            assert refreshed.total_orders_generated == 1
            assert refreshed.last_billing_date is not None
            assert refreshed.next_billing_date > before


def test_auto_charge_dead_code_is_gone():
    # payment_token is never written anywhere; _process_auto_payment was an
    # unreachable stub that returned True. Real auto-charging is its own spec.
    assert not hasattr(SubscriptionService, "_process_auto_payment")
    assert not hasattr(SubscriptionService, "_handle_payment_failure")


class TestBillingIdempotency:
    def test_crash_after_create_order_does_not_duplicate_on_retry(
        self, app, db, sample_user, sample_product, user_address, sample_subscription
    ):
        with app.app_context():
            service = SubscriptionService()
            result = service.process_subscription_billing(sample_subscription.id)
            assert result["success"] is True

            # Simulate the crash window: the order committed, the counters did
            # not. Mutate a session-local row rather than the `sample_subscription`
            # fixture object directly — that object was loaded under a different
            # Flask-SQLAlchemy session (see the `db.session.refresh` gotcha
            # elsewhere in this file) and assigning to it here is silently
            # inert: `db.session.commit()` has nothing of the fixture's in its
            # unit of work to flush, so the row in the DB is untouched and the
            # test would "pass" against the un-fixed guard too.
            subscription = Subscription.query.get(sample_subscription.id)
            subscription.last_billing_date = None
            db.session.commit()

            retry = service.process_subscription_billing(sample_subscription.id)
            assert retry.get("skipped") is True
            assert retry["reason"] == "already_billed_this_cycle"
            assert Order.query.filter_by(subscription_id=sample_subscription.id).count() == 1

    def test_a_cancelled_order_this_cycle_does_not_trigger_a_rebill(
        self, app, db, sample_user, sample_product, user_address, sample_subscription
    ):
        # An abandoned click order cancelled by cancel_abandoned_orders must not
        # cause an immediate replacement — the customer waits for the next cycle.
        with app.app_context():
            service = SubscriptionService()
            result = service.process_subscription_billing(sample_subscription.id)
            order = Order.query.get(result["order_id"])
            order.status = OrderStatus.CANCELLED

            # See the comment in the test above: mutate the session-local row,
            # not the fixture object, or the reset never reaches the database.
            subscription = Subscription.query.get(sample_subscription.id)
            subscription.last_billing_date = None
            db.session.commit()

            retry = service.process_subscription_billing(sample_subscription.id)
            assert retry.get("skipped") is True
            assert Order.query.filter_by(subscription_id=sample_subscription.id).count() == 1


class TestCodDebtBlock:
    def _block_cod(self):
        return patch(
            "business_app.services.cash_collection_service.CashCollectionService.validate_customer_can_use_cod",
            side_effect=ValidationError(
                "Customer has reached the maximum number of active cash on delivery debts.",
                error_code="COD_DEBT_LIMIT_REACHED",
            ),
        )

    def test_cod_block_skips_the_cycle_without_creating_an_order(
        self, app, db, sample_user, sample_product, user_address, sample_subscription
    ):
        with app.app_context(), self._block_cod():
            result = SubscriptionService().process_subscription_billing(sample_subscription.id)

            assert result["success"] is False
            assert result["skipped"] is True
            assert result["reason"] == "cod_debt_limit"
            assert Order.query.filter_by(subscription_id=sample_subscription.id).count() == 0

    def test_cod_block_leaves_subscription_active_and_untouched_failure_count(
        self, app, db, sample_user, sample_product, user_address, sample_subscription
    ):
        with app.app_context(), self._block_cod():
            before_failures = sample_subscription.failed_payment_count or 0
            SubscriptionService().process_subscription_billing(sample_subscription.id)

            # Re-query rather than db.session.refresh(sample_subscription): this
            # `with app.app_context()` pushes a second, distinct Flask app context,
            # and Flask-SQLAlchemy 3.x scopes db.session per app-context id, so
            # `db.session` here is a different Session than the one
            # `sample_subscription` was loaded under. Same idiom used throughout
            # this file (see TestSubscriptionBillingCollapse).
            refreshed = Subscription.query.get(sample_subscription.id)

            assert refreshed.status is SubscriptionStatus.ACTIVE
            assert (refreshed.failed_payment_count or 0) == before_failures

    def test_cod_block_advances_next_billing_date(
        self, app, db, sample_user, sample_product, user_address, sample_subscription
    ):
        with app.app_context(), self._block_cod():
            before = sample_subscription.next_billing_date
            SubscriptionService().process_subscription_billing(sample_subscription.id)
            refreshed = Subscription.query.get(sample_subscription.id)
            assert refreshed.next_billing_date > before

    def test_cod_block_notifies_the_customer(
        self, app, db, sample_user, sample_product, user_address, sample_subscription
    ):
        with app.app_context(), self._block_cod(), patch(
            "business_app.services.notification_service.NotificationService.send_notification"
        ) as send:
            SubscriptionService().process_subscription_billing(sample_subscription.id)

            send.assert_called_once()
            args, kwargs = send.call_args
            assert args[0] == sample_user.id
            assert args[1] == "subscription_billing_skipped_cod_debt"
            assert kwargs["template_data"]["subscription_name"] == sample_subscription.name

    def test_cod_block_still_skips_and_advances_date_when_notification_raises(
        self, app, db, sample_user, sample_product, user_address, sample_subscription
    ):
        # The notification is best-effort: a failure in the notify step must
        # not roll back or skip the already-committed next_billing_date advance.
        with app.app_context(), self._block_cod(), patch(
            "business_app.services.notification_service.NotificationService.send_notification",
            side_effect=RuntimeError("notification backend is down"),
        ):
            before = sample_subscription.next_billing_date
            result = SubscriptionService().process_subscription_billing(sample_subscription.id)

            assert result["success"] is False
            assert result["skipped"] is True
            assert result["reason"] == "cod_debt_limit"

            refreshed = Subscription.query.get(sample_subscription.id)
            assert refreshed.next_billing_date > before
            assert refreshed.status is SubscriptionStatus.ACTIVE
            assert Order.query.filter_by(subscription_id=sample_subscription.id).count() == 0


class TestSubscriptionPaymentMethodValidation:
    """SubscriptionService._validated_payment_method closes the hole where a
    subscription could be created/updated with loyalty_points or payme.
    create_order rejects those methods at billing time (TestResolvePaymentMethod
    above), so an unvalidated subscription would fail every single billing
    cycle forever — this is the actual bug the plan's brief singles out for
    the ADMIN creation path in particular."""

    @pytest.mark.parametrize("bad", ["loyalty_points", "payme", "bitcoin", ""])
    def test_create_rejects_non_selectable_methods(self, app, db, sample_user, user_address, sample_product, bad):
        with app.app_context():
            request_data = CreateSubscriptionRequest(
                name="Bad Sub",
                billing_cycle="weekly",
                delivery_frequency="weekly",
                delivery_address_id=user_address.id,
                payment_method=bad,
                items=[{"product_id": sample_product.id, "quantity": 1}],
            )
            with pytest.raises(ValidationError):
                SubscriptionService().create_subscription_for_user(sample_user.id, request_data)

    def test_create_normalizes_card_to_click(self, app, db, sample_user, user_address, sample_product):
        with app.app_context():
            request_data = CreateSubscriptionRequest(
                name="Card Sub",
                billing_cycle="weekly",
                delivery_frequency="weekly",
                delivery_address_id=user_address.id,
                payment_method="card",
                items=[{"product_id": sample_product.id, "quantity": 1}],
            )
            result = SubscriptionService().create_subscription_for_user(sample_user.id, request_data)
            subscription = Subscription.query.filter_by(subscription_number=result["subscription_number"]).first()
            assert subscription.payment_method is PaymentMethod.CLICK

    def test_admin_create_rejects_non_selectable_methods(
        self, app, db, admin_user, sample_user, user_address, sample_product
    ):
        # The exact scenario the brief calls out: "today an admin can create a
        # subscription that then fails every cycle."
        with app.app_context():
            request_data = AdminCreateSubscriptionRequest(
                user_id=sample_user.id,
                name="Admin Bad Sub",
                billing_cycle="weekly",
                delivery_frequency="weekly",
                delivery_address_id=user_address.id,
                payment_method="loyalty_points",
                items=[{"product_id": sample_product.id, "quantity": 1}],
            )
            with pytest.raises(ValidationError):
                SubscriptionService().admin_create_subscription(request_data, actor_user_id=admin_user.id)

    def test_field_update_rejects_non_selectable_method(self, app, db, sample_user, sample_subscription):
        with app.app_context():
            with pytest.raises(ValidationError):
                SubscriptionService().update_subscription_for_user(
                    sample_subscription.id, sample_user.id, {"payment_method": "loyalty_points"}
                )

    def test_admin_field_update_rejects_non_selectable_method(self, app, db, admin_user, sample_subscription):
        with app.app_context():
            with pytest.raises(ValidationError):
                SubscriptionService().admin_update_subscription(
                    sample_subscription.id, {"payment_method": "payme"}, admin_user.id
                )

    def test_change_payment_method_rejects_non_selectable_method(self, app, db, sample_user, sample_subscription):
        with app.app_context():
            with pytest.raises(ValidationError):
                SubscriptionService().change_payment_method_for_user(
                    sample_subscription.id, sample_user.id, "payme"
                )


def test_dead_subscription_columns_are_gone():
    from business_app.models.subscription import Subscription

    assert not hasattr(Subscription, "auto_payment")
    assert not hasattr(Subscription, "payment_token")
    assert hasattr(Subscription, "auto_renew"), "auto_renew is live — it gates next-cycle scheduling"


def test_subscription_preview_no_longer_advertises_a_trial(app, db, sample_user, sample_product):
    """SubscriptionStatus.TRIAL is never assigned anywhere, so the preview's
    trial_days / trial_available promised customers a free trial that never
    happened. The bot rendered it at handlers/subscriptions.py:374-375."""
    with app.app_context():
        preview = SubscriptionService().calculate_subscription_preview(
            user_id=sample_user.id,
            billing_cycle="weekly",
            delivery_frequency="weekly",
            items=[{"product_id": sample_product.id, "quantity": 1}],
        )
        assert "trial_days" not in preview
        assert "trial_available" not in preview


class TestSubscriptionBillingDeliveryNotes:
    def test_address_delivery_instructions_land_on_order_delivery_notes(
        self, app, db, sample_user, sample_product, user_address, sample_subscription
    ):
        """process_subscription_billing's order_data built a dead
        'delivery_instructions' key that create_order never read (it reads
        'delivery_notes'). Give the address a real instruction string so a
        None -> None pass would not make this vacuously true."""
        with app.app_context():
            # Re-query rather than mutate the `user_address` fixture object:
            # this nested app_context is a distinct Flask-SQLAlchemy session
            # (see the db.session.refresh gotcha noted throughout this file),
            # so writes to the fixture object itself would never reach the DB.
            address = UserAddress.query.get(user_address.id)
            address.delivery_instructions = "Leave at the gate"
            db.session.commit()

            result = SubscriptionService().process_subscription_billing(sample_subscription.id)
            order = Order.query.get(result["order_id"])
            assert order.delivery_notes == "Leave at the gate"
