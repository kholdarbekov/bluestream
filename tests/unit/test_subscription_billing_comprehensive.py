"""Comprehensive regression tests for subscription billing / pause / resume /
notification after the Subscription model field divergence was fixed.

Background (prod incident): SubscriptionService and NotificationService were
written against an OLD model schema. The real ``Subscription`` model exposes:
    name, billing_cycle, billing_amount, next_billing_date, last_billing_date,
    delivery_address_id + delivery_address (UserAddress) relationship,
    total_orders_generated, pause_start_date, pause_end_date, resume_date.
The code referenced phantom fields:
    plan.name, frequency, total_amount, billing_cycle_count, last_order_id,
    pause_until, resumed_at, delivery_address_street.
On SQLite (the old test DB) setting a phantom attribute silently no-ops, and
reading one that the model never defines raises AttributeError only when the
exact branch executes — so the daily Celery billing task and every
subscription notification crashed in production while the suite stayed green.

These tests use REAL DB objects (sample_user + a real UserAddress + a real
Subscription with a real SubscriptionItem) and assert the ACTUAL PERSISTED
COLUMN VALUES after each operation, plus the exact ``order_data`` shape handed
to ``create_order``. ``OrderService.create_order`` / ``PaymentService`` are
patched at the subscription_service *import sites* (the methods do
``from .order_service import OrderService`` lazily), so the heavy order/payment
machinery is stubbed while everything subscription-side stays real.
"""

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from business_app import db as _db
from business_app.models.order import Order
from business_app.models.subscription import Subscription, SubscriptionItem
from business_app.models.user import UserAddress
from business_app.services.notification_service import NotificationService
from business_app.services.subscription_service import SubscriptionService
from business_app.utils.exceptions import NotFoundError, ValidationError
from shared.enums import (
    OrderStatus,
    PaymentMethod,
    SubscriptionFrequency,
    SubscriptionStatus,
)


# ---------------------------------------------------------------------------
# Helpers — build a real UserAddress + Subscription (+ optional real item).
# ---------------------------------------------------------------------------
def _make_address(db, user, *, instructions=None):
    addr = UserAddress(
        user_id=user.id,
        title="Home",
        full_address="Amir Temur 1, Tashkent",
        street_address="Amir Temur 1",
        city="Tashkent",
        latitude=41.311,
        longitude=69.279,
        delivery_instructions=instructions,
    )
    db.session.add(addr)
    db.session.flush()
    return addr


def _make_subscription(
    db,
    user,
    addr,
    *,
    status=SubscriptionStatus.ACTIVE,
    number,
    billing_cycle=SubscriptionFrequency.WEEKLY,
    billing_amount=Decimal("50000.00"),
    next_billing_date=None,
    last_billing_date=None,
    total_orders_generated=0,
    payment_method=PaymentMethod.CARD,
    payment_token=None,
):
    sub = Subscription(
        subscription_number=number,
        user_id=user.id,
        status=status,
        name="Standard Weekly",
        billing_cycle=billing_cycle,
        billing_amount=billing_amount,
        next_billing_date=next_billing_date or (datetime.now(UTC) + timedelta(days=7)),
        last_billing_date=last_billing_date,
        delivery_frequency=SubscriptionFrequency.WEEKLY,
        delivery_address_id=addr.id,
        payment_method=payment_method,
        payment_token=payment_token,
        start_date=datetime.now(UTC),
        total_orders_generated=total_orders_generated,
    )
    if status == SubscriptionStatus.PAUSED:
        sub.paused_at = datetime.now(UTC)
    db.session.add(sub)
    db.session.flush()
    return sub


def _as_utc(dt):
    """Normalize a datetime to tz-aware UTC.

    The DateTime(timezone=True) columns are tz-aware in prod (Postgres), but the
    SQLite test backend strips tzinfo on read. Tests that compare persisted
    timestamps against ``datetime.now(UTC)`` must normalize first, otherwise we'd
    hit a naive-vs-aware TypeError that is purely a SQLite artefact, not a bug.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _add_item(db, sub, product, *, quantity=2):
    item = SubscriptionItem(
        subscription_id=sub.id,
        product_id=product.id,
        quantity=quantity,
        unit_price=product.base_price,
    )
    item.calculate_total()
    db.session.add(item)
    db.session.flush()
    return item


def _real_order_factory(user_id):
    """Return a fn that creates+persists a real Order, mimicking create_order.

    The billing path does ``order.subscription_id = subscription.id`` AFTER
    create_order returns, so the returned object must be a real, flushed Order
    whose subscription_id assignment persists and is queryable.
    """

    def _create_order(uid, order_data, **kwargs):
        order = Order(
            user_id=uid,
            status=OrderStatus.PENDING,
            subtotal=Decimal("50000.00"),
            delivery_fee=Decimal("0.00"),
            discount_amount=Decimal("0.00"),
            loyalty_discount=Decimal("0.00"),
            total_amount=Decimal("50000.00"),
        )
        _db.session.add(order)
        _db.session.flush()
        return order

    return _create_order


# ===========================================================================
# process_subscription_billing
# ===========================================================================
@pytest.mark.unit
class TestProcessSubscriptionBilling:
    def test_due_active_subscription_creates_order_with_address_from_fk(
        self, app, db, sample_user, sample_product
    ):
        """The real-world happy path: a due ACTIVE subscription bills, building
        order_data['delivery_address'] from the UserAddress relationship and
        carrying delivery_address_id from the FK (create_order subscripts it)."""
        with app.app_context():
            addr = _make_address(db, sample_user)
            sub = _make_subscription(db, sample_user, addr, number="SUB-B1")
            _add_item(db, sub, sample_product, quantity=3)
            db.session.commit()

            captured = {}

            def _capture_create_order(uid, order_data, **kwargs):
                captured["user_id"] = uid
                captured["order_data"] = order_data
                return _real_order_factory(uid)(uid, order_data, **kwargs)

            with (
                patch("business_app.services.order_service.OrderService") as order_cls,
                patch("business_app.services.payment_service.PaymentService") as payment_cls,
            ):
                order_cls.return_value.create_order.side_effect = _capture_create_order
                payment_cls.return_value.create_payment.return_value = MagicMock(id=1)

                result = SubscriptionService().process_subscription_billing(sub.id)

            # create_order received the subscription owner + an order_data dict
            # whose delivery_address is built from the UserAddress relationship.
            assert captured["user_id"] == sample_user.id
            od = captured["order_data"]
            assert od["items"] == [{"product_id": sample_product.id, "quantity": 3}]
            da = od["delivery_address"]
            assert da["delivery_address_id"] == addr.id
            assert da["street"] == "Amir Temur 1"
            assert da["city"] == "Tashkent"
            assert da["latitude"] == 41.311
            assert da["longitude"] == 69.279

            # Result: success + float amount + ISO next_billing_date.
            assert result["success"] is True
            assert result["amount"] == 50000.0
            assert isinstance(result["amount"], float)
            assert isinstance(result["next_billing_date"], str)

    def test_billing_advances_counters_and_dates_on_real_row(
        self, app, db, sample_user, sample_product
    ):
        """Assert the PERSISTED column values after billing: total_orders_generated
        increments, last_billing_date set, next_billing_date moved forward."""
        with app.app_context():
            addr = _make_address(db, sample_user)
            old_next = datetime.now(UTC)
            sub = _make_subscription(
                db,
                sample_user,
                addr,
                number="SUB-B2",
                next_billing_date=old_next,
                total_orders_generated=4,
            )
            _add_item(db, sub, sample_product)
            db.session.commit()

            with (
                patch("business_app.services.order_service.OrderService") as order_cls,
                patch("business_app.services.payment_service.PaymentService") as payment_cls,
            ):
                order_cls.return_value.create_order.side_effect = _real_order_factory(
                    sample_user.id
                )
                payment_cls.return_value.create_payment.return_value = MagicMock(id=1)

                SubscriptionService().process_subscription_billing(sub.id)

            refreshed = Subscription.query.get(sub.id)
            # total_orders_generated (NOT phantom billing_cycle_count) advanced.
            assert refreshed.total_orders_generated == 5
            assert refreshed.last_billing_date is not None
            # WEEKLY → next billing moves forward by ~7 days from now.
            assert _as_utc(refreshed.next_billing_date) > old_next

    def test_generated_order_is_linked_back_to_subscription(
        self, app, db, sample_user, sample_product
    ):
        """The order must persist order.subscription_id (NOT a phantom
        subscription.last_order_id) — the link is via Order.subscription_id."""
        with app.app_context():
            addr = _make_address(db, sample_user)
            sub = _make_subscription(db, sample_user, addr, number="SUB-B3")
            _add_item(db, sub, sample_product)
            db.session.commit()

            created_ids = {}

            def _create(uid, order_data, **kwargs):
                order = _real_order_factory(uid)(uid, order_data, **kwargs)
                created_ids["order_id"] = order.id
                return order

            with (
                patch("business_app.services.order_service.OrderService") as order_cls,
                patch("business_app.services.payment_service.PaymentService") as payment_cls,
            ):
                order_cls.return_value.create_order.side_effect = _create
                payment_cls.return_value.create_payment.return_value = MagicMock(id=1)

                result = SubscriptionService().process_subscription_billing(sub.id)

            linked = Order.query.get(created_ids["order_id"])
            assert linked.subscription_id == sub.id
            assert result["order_id"] == created_ids["order_id"]
            # And the relationship resolves from the subscription side.
            refreshed = Subscription.query.get(sub.id)
            assert created_ids["order_id"] in [o.id for o in refreshed.orders]

    def test_active_subscription_creates_payment_with_billing_amount(
        self, app, db, sample_user, sample_product
    ):
        """ACTIVE (non-trial) subscription charges create_payment with the
        subscription's billing_amount and its preferred payment method."""
        with app.app_context():
            addr = _make_address(db, sample_user)
            sub = _make_subscription(
                db,
                sample_user,
                addr,
                number="SUB-B4",
                billing_amount=Decimal("73000.00"),
                payment_method=PaymentMethod.CARD,
            )
            _add_item(db, sub, sample_product)
            db.session.commit()

            with (
                patch("business_app.services.order_service.OrderService") as order_cls,
                patch("business_app.services.payment_service.PaymentService") as payment_cls,
            ):
                order_cls.return_value.create_order.side_effect = _real_order_factory(
                    sample_user.id
                )
                create_payment = payment_cls.return_value.create_payment
                create_payment.return_value = MagicMock(id=1)

                SubscriptionService().process_subscription_billing(sub.id)

            create_payment.assert_called_once()
            args = create_payment.call_args.args
            # signature: create_payment(order_id, payment_method, amount)
            assert args[1] == PaymentMethod.CARD
            assert args[2] == Decimal("73000.00")

    def test_payment_token_present_triggers_auto_payment(
        self, app, db, sample_user, sample_product
    ):
        """When a payment_token is stored, the auto-payment path runs."""
        with app.app_context():
            addr = _make_address(db, sample_user)
            sub = _make_subscription(
                db,
                sample_user,
                addr,
                number="SUB-B5",
                payment_token="tok_stored_123",
            )
            _add_item(db, sub, sample_product)
            db.session.commit()

            svc = SubscriptionService()
            with (
                patch("business_app.services.order_service.OrderService") as order_cls,
                patch("business_app.services.payment_service.PaymentService") as payment_cls,
                patch.object(svc, "_process_auto_payment", return_value=True) as auto,
            ):
                order_cls.return_value.create_order.side_effect = _real_order_factory(
                    sample_user.id
                )
                payment = MagicMock(id=1)
                payment_cls.return_value.create_payment.return_value = payment

                result = svc.process_subscription_billing(sub.id)

            auto.assert_called_once()
            assert auto.call_args.args[0] is payment
            assert auto.call_args.args[1] == "tok_stored_123"
            assert result["success"] is True

    def test_trial_subscription_skips_payment_and_flips_to_active(
        self, app, db, sample_user, sample_product
    ):
        """A TRIAL subscription bills (creates the order) but does NOT create a
        payment and is promoted to ACTIVE."""
        with app.app_context():
            addr = _make_address(db, sample_user)
            sub = _make_subscription(
                db,
                sample_user,
                addr,
                status=SubscriptionStatus.TRIAL,
                number="SUB-TRIAL",
            )
            _add_item(db, sub, sample_product)
            db.session.commit()

            with (
                patch("business_app.services.order_service.OrderService") as order_cls,
                patch("business_app.services.payment_service.PaymentService") as payment_cls,
            ):
                order_cls.return_value.create_order.side_effect = _real_order_factory(
                    sample_user.id
                )
                create_payment = payment_cls.return_value.create_payment

                result = SubscriptionService().process_subscription_billing(sub.id)

            create_payment.assert_not_called()
            refreshed = Subscription.query.get(sub.id)
            assert refreshed.status == SubscriptionStatus.ACTIVE
            assert result["success"] is True

    def test_delivery_instructions_from_address_propagated(
        self, app, db, sample_user, sample_product
    ):
        """Address-level delivery_instructions must flow into order_data."""
        with app.app_context():
            addr = _make_address(db, sample_user, instructions="Leave at door, ring twice")
            sub = _make_subscription(db, sample_user, addr, number="SUB-B6")
            _add_item(db, sub, sample_product)
            db.session.commit()

            captured = {}

            def _capture(uid, order_data, **kwargs):
                captured["order_data"] = order_data
                return _real_order_factory(uid)(uid, order_data, **kwargs)

            with (
                patch("business_app.services.order_service.OrderService") as order_cls,
                patch("business_app.services.payment_service.PaymentService") as payment_cls,
            ):
                order_cls.return_value.create_order.side_effect = _capture
                payment_cls.return_value.create_payment.return_value = MagicMock(id=1)

                SubscriptionService().process_subscription_billing(sub.id)

            assert captured["order_data"]["delivery_instructions"] == "Leave at door, ring twice"

    def test_already_billed_this_cycle_is_skipped_idempotently(
        self, app, db, sample_user, sample_product
    ):
        """If last_billing_date is already within today's UTC window, billing is
        skipped (CEL-002 idempotency) and NO order is created."""
        with app.app_context():
            addr = _make_address(db, sample_user)
            sub = _make_subscription(
                db,
                sample_user,
                addr,
                number="SUB-IDEM",
                last_billing_date=datetime.now(UTC),
                total_orders_generated=2,
            )
            _add_item(db, sub, sample_product)
            db.session.commit()

            with (
                patch("business_app.services.order_service.OrderService") as order_cls,
                patch("business_app.services.payment_service.PaymentService"),
            ):
                create_order = order_cls.return_value.create_order
                result = SubscriptionService().process_subscription_billing(sub.id)

            create_order.assert_not_called()
            assert result["success"] is True
            assert result["skipped"] is True
            assert result["reason"] == "already_billed_this_cycle"
            # Counter must not advance on a skip.
            assert Subscription.query.get(sub.id).total_orders_generated == 2

    def test_cancelled_subscription_raises_validation_error(
        self, app, db, sample_user, sample_product
    ):
        with app.app_context():
            addr = _make_address(db, sample_user)
            sub = _make_subscription(
                db,
                sample_user,
                addr,
                status=SubscriptionStatus.CANCELLED,
                number="SUB-CANCEL",
            )
            _add_item(db, sub, sample_product)
            db.session.commit()

            with pytest.raises(ValidationError):
                SubscriptionService().process_subscription_billing(sub.id)

    def test_paused_subscription_raises_validation_error(
        self, app, db, sample_user, sample_product
    ):
        with app.app_context():
            addr = _make_address(db, sample_user)
            sub = _make_subscription(
                db,
                sample_user,
                addr,
                status=SubscriptionStatus.PAUSED,
                number="SUB-PAUSED-BILL",
            )
            _add_item(db, sub, sample_product)
            db.session.commit()

            with pytest.raises(ValidationError):
                SubscriptionService().process_subscription_billing(sub.id)

    def test_missing_subscription_raises_not_found(self, app, db):
        with app.app_context():
            with pytest.raises(NotFoundError):
                SubscriptionService().process_subscription_billing(999999)

    def test_order_data_does_not_reference_phantom_fields(
        self, app, db, sample_user, sample_product
    ):
        """Regression guard: order_data must use the NEW keys (delivery_address_id,
        street/city/latitude/longitude under delivery_address) and must NOT carry
        the OLD phantom keys (delivery_address_street, total_amount, frequency)."""
        with app.app_context():
            addr = _make_address(db, sample_user)
            sub = _make_subscription(db, sample_user, addr, number="SUB-PHANTOM")
            _add_item(db, sub, sample_product)
            db.session.commit()

            captured = {}

            def _capture(uid, order_data, **kwargs):
                captured["order_data"] = order_data
                return _real_order_factory(uid)(uid, order_data, **kwargs)

            with (
                patch("business_app.services.order_service.OrderService") as order_cls,
                patch("business_app.services.payment_service.PaymentService") as payment_cls,
            ):
                order_cls.return_value.create_order.side_effect = _capture
                payment_cls.return_value.create_payment.return_value = MagicMock(id=1)

                SubscriptionService().process_subscription_billing(sub.id)

            od = captured["order_data"]
            assert "delivery_address_street" not in od
            assert "delivery_address_street" not in od["delivery_address"]
            assert "total_amount" not in od
            assert "frequency" not in od
            # Positive: the new contract is present.
            assert "delivery_address_id" in od["delivery_address"]


# ===========================================================================
# pause_subscription
# ===========================================================================
@pytest.mark.unit
class TestPauseSubscription:
    def test_active_pause_sets_pause_end_date_and_start_date(
        self, app, db, sample_user
    ):
        with app.app_context():
            addr = _make_address(db, sample_user)
            sub = _make_subscription(db, sample_user, addr, number="SUB-PA1")
            db.session.commit()
            until = datetime.now(UTC) + timedelta(days=10)

            SubscriptionService().pause_subscription(sub.id, pause_until=until)

            refreshed = Subscription.query.get(sub.id)
            assert refreshed.status == SubscriptionStatus.PAUSED
            # pause_end_date (NOT phantom pause_until) carries the until value.
            assert refreshed.pause_end_date is not None
            assert refreshed.pause_start_date is not None
            assert refreshed.paused_at is not None

    def test_pause_without_until_sets_end_date_none(self, app, db, sample_user):
        with app.app_context():
            addr = _make_address(db, sample_user)
            sub = _make_subscription(db, sample_user, addr, number="SUB-PA2")
            db.session.commit()

            SubscriptionService().pause_subscription(sub.id)

            refreshed = Subscription.query.get(sub.id)
            assert refreshed.status == SubscriptionStatus.PAUSED
            assert refreshed.pause_end_date is None
            # Pause start was still recorded.
            assert refreshed.pause_start_date is not None

    def test_pause_non_active_raises_validation_error(self, app, db, sample_user):
        with app.app_context():
            addr = _make_address(db, sample_user)
            sub = _make_subscription(
                db,
                sample_user,
                addr,
                status=SubscriptionStatus.PAUSED,
                number="SUB-PA3",
            )
            db.session.commit()

            with pytest.raises(ValidationError):
                SubscriptionService().pause_subscription(sub.id)

    def test_pause_respects_user_scope(self, app, db, sample_user):
        """Pausing with a wrong user_id raises NotFound (ownership guard)."""
        with app.app_context():
            addr = _make_address(db, sample_user)
            sub = _make_subscription(db, sample_user, addr, number="SUB-PA4")
            db.session.commit()

            with pytest.raises(NotFoundError):
                SubscriptionService().pause_subscription(sub.id, user_id=sample_user.id + 999)


# ===========================================================================
# resume_subscription
# ===========================================================================
@pytest.mark.unit
class TestResumeSubscription:
    def test_paused_resume_recalculates_next_billing_without_crashing(
        self, app, db, sample_user
    ):
        """Pre-fix this crashed reading subscription.frequency. It must produce a
        real datetime next_billing_date and clear pause_end_date."""
        with app.app_context():
            addr = _make_address(db, sample_user)
            sub = _make_subscription(
                db,
                sample_user,
                addr,
                status=SubscriptionStatus.PAUSED,
                number="SUB-RE1",
            )
            sub.pause_end_date = datetime.now(UTC) + timedelta(days=5)
            db.session.commit()

            SubscriptionService().resume_subscription(sub.id)

            refreshed = Subscription.query.get(sub.id)
            assert refreshed.status == SubscriptionStatus.ACTIVE
            # resume_date (NOT phantom resumed_at) is stamped.
            assert refreshed.resume_date is not None
            # pause_end_date cleared.
            assert refreshed.pause_end_date is None
            assert refreshed.paused_at is None
            # next_billing_date recomputed to a real future datetime.
            assert isinstance(refreshed.next_billing_date, datetime)
            assert _as_utc(refreshed.next_billing_date) > datetime.now(UTC)

    def test_resume_uses_billing_cycle_for_recalculation(self, app, db, sample_user):
        """WEEKLY billing_cycle → next billing ~7 days out, computed from
        billing_cycle (the real field), not the phantom frequency attribute."""
        with app.app_context():
            addr = _make_address(db, sample_user)
            sub = _make_subscription(
                db,
                sample_user,
                addr,
                status=SubscriptionStatus.PAUSED,
                number="SUB-RE2",
                billing_cycle=SubscriptionFrequency.WEEKLY,
            )
            db.session.commit()

            before = datetime.now(UTC)
            SubscriptionService().resume_subscription(sub.id)

            refreshed = Subscription.query.get(sub.id)
            delta = _as_utc(refreshed.next_billing_date) - before
            # ~7 days for weekly (allow generous slack for test runtime).
            assert timedelta(days=6) < delta < timedelta(days=8)

    def test_resume_non_paused_raises_validation_error(self, app, db, sample_user):
        with app.app_context():
            addr = _make_address(db, sample_user)
            sub = _make_subscription(db, sample_user, addr, number="SUB-RE3")
            db.session.commit()

            with pytest.raises(ValidationError):
                SubscriptionService().resume_subscription(sub.id)

    def test_pause_then_resume_roundtrip_is_consistent(self, app, db, sample_user):
        """Full real-world cycle: active → pause(until) → resume."""
        with app.app_context():
            addr = _make_address(db, sample_user)
            sub = _make_subscription(db, sample_user, addr, number="SUB-RT1")
            db.session.commit()
            svc = SubscriptionService()

            svc.pause_subscription(sub.id, pause_until=datetime.now(UTC) + timedelta(days=3))
            paused = Subscription.query.get(sub.id)
            assert paused.status == SubscriptionStatus.PAUSED
            assert paused.pause_end_date is not None

            svc.resume_subscription(sub.id)
            resumed = Subscription.query.get(sub.id)
            assert resumed.status == SubscriptionStatus.ACTIVE
            assert resumed.pause_end_date is None
            assert resumed.resume_date is not None


# ===========================================================================
# notification_service.send_subscription_notification
# ===========================================================================
@pytest.mark.unit
class TestSendSubscriptionNotification:
    def test_template_data_built_from_real_fields(self, app, db, sample_user):
        """plan_name←name, frequency←billing_cycle.value, total_amount←
        float(billing_amount) — all REAL model fields (no phantom plan.name)."""
        with app.app_context():
            addr = _make_address(db, sample_user)
            sub = _make_subscription(
                db,
                sample_user,
                addr,
                number="SUB-NOT1",
                billing_cycle=SubscriptionFrequency.MONTHLY,
                billing_amount=Decimal("120000.00"),
            )
            db.session.commit()

            svc = NotificationService()
            with patch.object(svc, "send_notification", return_value={"success": True}) as send:
                result = svc.send_subscription_notification(sub.id, "paused")

            assert result == {"success": True}
            template = send.call_args.args[3]
            assert template["plan_name"] == "Standard Weekly"
            assert template["frequency"] == SubscriptionFrequency.MONTHLY.value
            assert template["total_amount"] == 120000.0
            assert isinstance(template["total_amount"], float)
            assert template["event_type"] == "paused"
            assert template["subscription_id"] == sub.id

    def test_total_amount_is_json_safe_float_not_decimal(self, app, db, sample_user):
        """Regression: total_amount must be a float (Decimal would break JSON)."""
        with app.app_context():
            addr = _make_address(db, sample_user)
            sub = _make_subscription(
                db, sample_user, addr, number="SUB-NOT2", billing_amount=Decimal("9999.99")
            )
            db.session.commit()

            svc = NotificationService()
            with patch.object(svc, "send_notification", return_value={"ok": True}) as send:
                svc.send_subscription_notification(sub.id, "resumed")

            template = send.call_args.args[3]
            assert template["total_amount"] == 9999.99
            assert not isinstance(template["total_amount"], Decimal)

    def test_unknown_subscription_raises_notification_error(self, app, db):
        from business_app.utils.exceptions import NotificationError

        with app.app_context():
            svc = NotificationService()
            with pytest.raises(NotificationError):
                svc.send_subscription_notification(123456, "cancelled")

    def test_next_billing_date_serialized_isoformat(self, app, db, sample_user):
        with app.app_context():
            addr = _make_address(db, sample_user)
            nbd = datetime.now(UTC) + timedelta(days=7)
            sub = _make_subscription(
                db, sample_user, addr, number="SUB-NOT3", next_billing_date=nbd
            )
            db.session.commit()

            svc = NotificationService()
            with patch.object(svc, "send_notification", return_value={}) as send:
                svc.send_subscription_notification(sub.id, "renewed")

            template = send.call_args.args[3]
            # Serialized to ISO 8601 from the stored next_billing_date. (The
            # SQLite backend drops tzinfo on read, so compare against the value
            # actually persisted rather than the pre-store aware datetime.)
            stored = Subscription.query.get(sub.id).next_billing_date
            assert template["next_billing_date"] == stored.isoformat()
            assert _as_utc(stored) == nbd
