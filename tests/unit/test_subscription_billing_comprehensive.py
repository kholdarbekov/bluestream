"""Comprehensive regression tests for subscription billing / pause / resume /
notification after the Subscription model field divergence was fixed.

Background (prod incident): SubscriptionService and NotificationService were
written against an OLD model schema. The real ``Subscription`` model exposes:
    name, frequency, billing_amount, next_delivery_date, last_delivery_date,
    delivery_address_id + delivery_address (UserAddress) relationship,
    total_orders_generated, pause_start_date, pause_end_date, resume_date.
The code referenced phantom fields:
    plan.name, total_amount, billing_cycle_count, last_order_id,
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
from business_app.utils.local_windows import local_date
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
    frequency=SubscriptionFrequency.WEEKLY,
    billing_amount=Decimal("50000.00"),
    next_delivery_date=None,
    last_delivery_date=None,
    total_orders_generated=0,
    payment_method=PaymentMethod.CARD,
):
    sub = Subscription(
        subscription_number=number,
        user_id=user.id,
        status=status,
        name="Standard Weekly",
        frequency=frequency,
        billing_amount=billing_amount,
        next_delivery_date=next_delivery_date or (local_date() + timedelta(days=7)),
        last_delivery_date=last_delivery_date,
        delivery_address_id=addr.id,
        payment_method=payment_method,
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

    create_order (not the billing method) now stamps subscription_id /
    is_subscription_order atomically when called with ``subscription=...``
    (see OrderService.create_order). This stub mirrors that so tests that
    assert on the link still exercise the real invariant, just via the new
    division of responsibility.
    """

    def _create_order(uid, order_data, subscription=None, **kwargs):
        order = Order(
            user_id=uid,
            status=OrderStatus.PENDING,
            subtotal=Decimal("50000.00"),
            delivery_fee=Decimal("0.00"),
            discount_amount=Decimal("0.00"),
            loyalty_discount=Decimal("0.00"),
            total_amount=Decimal("50000.00"),
            subscription_id=subscription.id if subscription is not None else None,
            is_subscription_order=subscription is not None,
        )
        _db.session.add(order)
        _db.session.flush()
        return order

    return _create_order


# ===========================================================================
# create_next_order (was process_subscription_billing). These rows' next delivery
# is a week out, so the calls below are the admin's "Create next order now"
# (`due_only=False`) unless a test says otherwise.
# ===========================================================================
@pytest.mark.unit
class TestCreateNextOrder:
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
            due = sub.next_delivery_date

            captured = {}

            def _capture_create_order(uid, order_data, **kwargs):
                captured["user_id"] = uid
                captured["order_data"] = order_data
                return _real_order_factory(uid)(uid, order_data, **kwargs)

            with (
                patch("business_app.services.order_service.OrderService") as order_cls,
                patch("business_app.services.payment_service.PaymentService") as payment_cls,
            ):
                order_cls.customer_order_refusal.return_value = None  # this customer may order
                order_cls.return_value.create_order.side_effect = _capture_create_order
                payment_cls.return_value.create_payment.return_value = MagicMock(id=1)

                result = SubscriptionService().create_next_order(sub.id, due_only=False)

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

            # Dated for the next delivery, no window (no slot), and the clock moves one cycle.
            assert od["delivery_date"] == due
            assert (od["delivery_window_start"], od["delivery_window_end"]) == (None, None)
            assert result["success"] is True
            assert result["amount"] == 50000.0
            assert isinstance(result["amount"], float)
            assert result["next_delivery_date"] == (od["delivery_date"] + timedelta(weeks=1)).isoformat()

    def test_billing_advances_counters_and_dates_on_real_row(
        self, app, db, sample_user, sample_product
    ):
        """Assert the PERSISTED column values after billing: total_orders_generated
        increments, last_delivery_date set, next_delivery_date moved one cycle."""
        with app.app_context():
            addr = _make_address(db, sample_user)
            old_next = local_date()  # due today
            sub = _make_subscription(
                db,
                sample_user,
                addr,
                number="SUB-B2",
                next_delivery_date=old_next,
                total_orders_generated=4,
            )
            _add_item(db, sub, sample_product)
            db.session.commit()

            with (
                patch("business_app.services.order_service.OrderService") as order_cls,
                patch("business_app.services.payment_service.PaymentService") as payment_cls,
            ):
                order_cls.customer_order_refusal.return_value = None  # this customer may order
                order_cls.return_value.create_order.side_effect = _real_order_factory(
                    sample_user.id
                )
                payment_cls.return_value.create_payment.return_value = MagicMock(id=1)

                SubscriptionService().create_next_order(sub.id)

            refreshed = Subscription.query.get(sub.id)
            # total_orders_generated (NOT phantom billing_cycle_count) advanced.
            assert refreshed.total_orders_generated == 5
            assert refreshed.last_delivery_date is not None
            # WEEKLY → the clock moves from the delivery just created to one week later.
            assert refreshed.next_delivery_date == refreshed.last_delivery_date + timedelta(weeks=1)
            assert refreshed.next_delivery_date > old_next

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
                order_cls.customer_order_refusal.return_value = None  # this customer may order
                order_cls.return_value.create_order.side_effect = _create
                payment_cls.return_value.create_payment.return_value = MagicMock(id=1)

                result = SubscriptionService().create_next_order(sub.id, due_only=False)

            linked = Order.query.get(created_ids["order_id"])
            assert linked.subscription_id == sub.id
            assert result["order_id"] == created_ids["order_id"]
            # And the relationship resolves from the subscription side.
            refreshed = Subscription.query.get(sub.id)
            assert created_ids["order_id"] in [o.id for o in refreshed.orders]

    def test_active_subscription_threads_payment_method_and_subscription_into_create_order(
        self, app, db, sample_user, sample_product
    ):
        """Billing no longer calls create_payment itself — create_order owns the
        single Payment row for order.total_amount (see
        tests/unit/test_subscription_order_parity.py::TestSubscriptionBillingCollapse).
        What billing must still get right is threading the subscription's
        payment_method into order_data (so create_order can resolve it) and
        passing the subscription row itself (so create_order can stamp
        subscription_id / apply the discount)."""
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

            captured = {}

            def _capture(uid, order_data, **kwargs):
                captured["order_data"] = order_data
                captured["kwargs"] = kwargs
                return _real_order_factory(uid)(uid, order_data, **kwargs)

            with (
                patch("business_app.services.order_service.OrderService") as order_cls,
                patch("business_app.services.payment_service.PaymentService") as payment_cls,
            ):
                order_cls.customer_order_refusal.return_value = None  # this customer may order
                order_cls.return_value.create_order.side_effect = _capture

                SubscriptionService().create_next_order(sub.id, due_only=False)

            assert captured["order_data"]["payment_method"] == PaymentMethod.CARD.value
            assert captured["kwargs"]["subscription"] is sub
            payment_cls.return_value.create_payment.assert_not_called()

    def test_trial_subscription_is_rejected_like_any_non_active_status(
        self, app, db, sample_user, sample_product
    ):
        """TRIAL was dead code: create_subscription hardcodes status=ACTIVE, so
        no row is ever created with TRIAL. The old trial-to-active conversion
        branch (and the payment_token auto-charge stub it guarded) is removed;
        TRIAL now fails the same active-only gate as PAUSED/CANCELLED instead
        of silently billing and self-promoting."""
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

            with pytest.raises(ValidationError):
                SubscriptionService().create_next_order(sub.id)

    def test_delivery_instructions_from_address_propagated(
        self, app, db, sample_user, sample_product
    ):
        """Address-level delivery_instructions must flow into order_data under
        the 'delivery_notes' key — the only key create_order actually reads
        (order_service.py). The dead 'delivery_instructions' key create_order
        never consumed is removed by the same change (task 13)."""
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
                order_cls.customer_order_refusal.return_value = None  # this customer may order
                order_cls.return_value.create_order.side_effect = _capture
                payment_cls.return_value.create_payment.return_value = MagicMock(id=1)

                SubscriptionService().create_next_order(sub.id, due_only=False)

            assert captured["order_data"]["delivery_notes"] == "Leave at door, ring twice"

    def test_a_live_order_for_the_date_is_never_created_twice(self, app, db, sample_user, sample_product):
        """The idempotency guard (spec 2026-10-23 §4.3, replacing CEL-002's same-day check): a
        non-cancelled order of this subscription for the next delivery date means NO second order."""
        with app.app_context():
            addr = _make_address(db, sample_user)
            sub = _make_subscription(db, sample_user, addr, number="SUB-IDEM", total_orders_generated=2)
            _add_item(db, sub, sample_product)
            db.session.commit()
            existing = _real_order_factory(sample_user.id)(sample_user.id, {}, subscription=sub)
            existing.delivery_date = sub.next_delivery_date
            db.session.commit()

            with (
                patch("business_app.services.order_service.OrderService") as order_cls,
                patch("business_app.services.payment_service.PaymentService"),
            ):
                create_order = order_cls.return_value.create_order
                result = SubscriptionService().create_next_order(sub.id, due_only=False)

            create_order.assert_not_called()
            assert (result["success"], result["skipped"], result["reason"]) == (True, True, "already_created")
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
                SubscriptionService().create_next_order(sub.id)

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
                SubscriptionService().create_next_order(sub.id)

    def test_missing_subscription_raises_not_found(self, app, db):
        with app.app_context():
            with pytest.raises(NotFoundError):
                SubscriptionService().create_next_order(999999)

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
                order_cls.customer_order_refusal.return_value = None  # this customer may order
                order_cls.return_value.create_order.side_effect = _capture
                payment_cls.return_value.create_payment.return_value = MagicMock(id=1)

                SubscriptionService().create_next_order(sub.id, due_only=False)

            od = captured["order_data"]
            assert "delivery_address_street" not in od
            assert "delivery_address_street" not in od["delivery_address"]
            assert "total_amount" not in od
            assert "frequency" not in od
            # Positive: the new contract is present.
            assert "delivery_address_id" in od["delivery_address"]


# ===========================================================================
# pause / resume (the live, user-scoped methods; the legacy uncalled copies are gone)
# ===========================================================================
@pytest.mark.unit
class TestPauseSubscription:
    def test_active_pause_records_the_pause(self, app, db, sample_user):
        with app.app_context():
            addr = _make_address(db, sample_user)
            sub = _make_subscription(db, sample_user, addr, number="SUB-PA1")
            db.session.commit()

            outcome = SubscriptionService().pause_subscription_for_user(
                sub.id, sample_user.id, "Holiday", expected_state=SubscriptionService.state_token(sub)
            )

            refreshed = Subscription.query.get(sub.id)
            assert refreshed.status == SubscriptionStatus.PAUSED
            assert (refreshed.pause_reason, refreshed.resume_date) == ("Holiday", None)
            assert refreshed.paused_at is not None
            assert (outcome["cancelled_orders"], outcome["kept_orders"]) == ([], [])

    def test_pause_non_active_raises_validation_error(self, app, db, sample_user):
        with app.app_context():
            addr = _make_address(db, sample_user)
            sub = _make_subscription(db, sample_user, addr, status=SubscriptionStatus.PAUSED, number="SUB-PA3")
            db.session.commit()

            with pytest.raises(ValidationError):
                SubscriptionService().pause_subscription_for_user(sub.id, sample_user.id, "x")

    def test_pause_respects_user_scope(self, app, db, sample_user):
        """Pausing with a wrong user_id raises NotFound (ownership guard)."""
        with app.app_context():
            addr = _make_address(db, sample_user)
            sub = _make_subscription(db, sample_user, addr, number="SUB-PA4")
            db.session.commit()

            with pytest.raises(NotFoundError):
                SubscriptionService().pause_subscription_for_user(sub.id, sample_user.id + 999, "x")


@pytest.mark.unit
class TestResumeSubscription:
    def test_paused_resume_dates_the_next_delivery_by_the_rule(self, app, db, sample_user):
        """Pre-fix this crashed reading subscription.frequency. The next delivery is the first one the
        lead allows (spec 2026-10-23 §4.6), and the pause is cleared."""
        from business_app.services import subscription_schedule
        from business_app.utils.local_windows import local_date

        with app.app_context():
            addr = _make_address(db, sample_user)
            sub = _make_subscription(db, sample_user, addr, status=SubscriptionStatus.PAUSED, number="SUB-RE1")
            sub.pause_end_date = datetime.now(UTC) + timedelta(days=5)
            db.session.commit()

            with patch("business_app.tasks.subscription_tasks.create_subscription_delivery_task.delay"):
                SubscriptionService().resume_subscription_for_user(sub.id, sample_user.id)

            refreshed = Subscription.query.get(sub.id)
            assert refreshed.status == SubscriptionStatus.ACTIVE
            assert (refreshed.pause_end_date, refreshed.paused_at) == (None, None)
            assert refreshed.next_delivery_date == subscription_schedule.earliest_delivery_date(local_date())

    def test_resume_non_paused_raises_validation_error(self, app, db, sample_user):
        with app.app_context():
            addr = _make_address(db, sample_user)
            sub = _make_subscription(db, sample_user, addr, number="SUB-RE3")
            db.session.commit()

            with pytest.raises(ValidationError):
                SubscriptionService().resume_subscription_for_user(sub.id, sample_user.id)


# ===========================================================================
# notification_service.send_subscription_notification
# ===========================================================================
@pytest.mark.unit
class TestSendSubscriptionNotification:
    def test_template_data_built_from_real_fields(self, app, db, sample_user):
        """plan_name←name, frequency←frequency.value, total_amount←
        float(billing_amount) — all REAL model fields (no phantom plan.name)."""
        with app.app_context():
            addr = _make_address(db, sample_user)
            sub = _make_subscription(
                db,
                sample_user,
                addr,
                number="SUB-NOT1",
                frequency=SubscriptionFrequency.MONTHLY,
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

    def test_next_delivery_date_serialized_isoformat(self, app, db, sample_user):
        with app.app_context():
            addr = _make_address(db, sample_user)
            sub = _make_subscription(db, sample_user, addr, number="SUB-NOT3")
            db.session.commit()

            svc = NotificationService()
            with patch.object(svc, "send_notification", return_value={}) as send:
                svc.send_subscription_notification(sub.id, "renewed")

            template = send.call_args.args[3]
            stored = Subscription.query.get(sub.id).next_delivery_date
            assert template["next_delivery_date"] == stored.isoformat()
            assert "next_billing_date" not in template
