"""COD cash payment collector invariant (ARCH-006).

These tests cover ``ck_payments_cash_completed_requires_collector``: a CASH
``Payment`` may only reach ``status=COMPLETED`` with ``collected_by`` set. The
service layer (``CashCollectionService``) is the single authoritative point that
stamps + enforces the collector via
``state_validators.assert_cash_payment_collector``.

The original prod incident slipped past 4000+ SQLite-backed tests because they
either mocked the projection or never asserted the *persisted* ``collected_by``
column after a real COD settlement. Here we exercise the real service against
real DB objects and assert the actual column values.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from business_app.models.delivery import DeliveryPerson
from business_app.models.order import Order
from business_app.models.payment import (
    CashCollectionAllocation,
    CashCollectionEvent,
    Payment,
)
from business_app.models.user import User
from business_app.services.cash_collection_service import CashCollectionService
from business_app.services.loyalty_service import LoyaltyService
from business_app.utils.exceptions import InvalidStateTransition
from business_app.utils.password_security import hash_password
from shared.enums import (
    OrderStatus,
    PaymentMethod,
    PaymentStatus,
    UserRole,
    UserType,
)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def delivery_driver_profile(db, delivery_driver):
    profile = DeliveryPerson(
        user_id=delivery_driver.id,
        full_name="Delivery Driver",
        phone=delivery_driver.phone,
        email=delivery_driver.email,
        is_active=True,
        is_available=True,
    )
    db.session.add(profile)
    db.session.commit()
    return profile


@pytest.fixture
def second_delivery_driver(db):
    user = User(
        email="driver.two.collector@example.com",
        phone="+998901230011",
        password_hash=hash_password("DriverTwoPassword123!"),
        first_name="Delivery",
        last_name="Driver Two",
        user_type=UserType.STAFF,
        role=UserRole.DELIVERY_DRIVER,
        is_verified=True,
        created_at=datetime.now(UTC),
    )
    db.session.add(user)
    db.session.commit()
    return user


@pytest.fixture
def second_delivery_driver_profile(db, second_delivery_driver):
    profile = DeliveryPerson(
        user_id=second_delivery_driver.id,
        full_name="Delivery Driver Two",
        phone=second_delivery_driver.phone,
        email=second_delivery_driver.email,
        is_active=True,
        is_available=True,
    )
    db.session.add(profile)
    db.session.commit()
    return profile


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _make_cash_order(
    db,
    user,
    *,
    order_number,
    total,
    status=OrderStatus.PENDING,
    created_at=None,
):
    order = Order(
        user_id=user.id,
        order_number=order_number,
        status=status,
        subtotal=Decimal(total),
        delivery_fee=Decimal("0.00"),
        discount_amount=Decimal("0.00"),
        loyalty_discount=Decimal("0.00"),
        total_amount=Decimal(total),
        payment_method=PaymentMethod.CASH,
        created_at=created_at or datetime.now(UTC),
    )
    db.session.add(order)
    db.session.flush()
    return order


def _seed_unapplied_event(
    db,
    *,
    customer_id,
    amount,
    collector_user_id=None,
    recorded_by_user_id=None,
    source="admin_adjustment",
    occurred_at=None,
):
    """Directly seed a cash-collection event carrying unapplied credit."""
    event = CashCollectionEvent(
        customer_id=customer_id,
        collector_user_id=collector_user_id,
        recorded_by_user_id=recorded_by_user_id,
        amount=Decimal(amount),
        currency="UZS",
        source=source,
        occurred_at=occurred_at or datetime.now(UTC),
        notes="Seeded prepayment surplus",
        unapplied_amount=Decimal(amount),
    )
    db.session.add(event)
    db.session.flush()
    return event


# =========================================================================== #
# sync_payment_projection
# =========================================================================== #
@pytest.mark.unit
class TestSyncPaymentProjectionCollector:
    def test_completing_cash_without_collector_raises(self, app, db, sample_user):
        """A fully-collected CASH payment with no collector must be rejected."""
        with app.app_context():
            service = CashCollectionService()
            order = _make_cash_order(
                db, sample_user, order_number="ORD-SYNC-NOCOLL", total="30000.00",
                status=OrderStatus.DELIVERED,
            )
            payment = service.ensure_cod_payment_for_order(order)
            db.session.flush()

            # Mimic full cash collection but never supply a collector.
            payment.amount_collected = Decimal("30000.00")

            with pytest.raises(InvalidStateTransition) as exc_info:
                service.sync_payment_projection(payment)

            assert exc_info.value.missing_field == "collected_by"
            assert exc_info.value.entity == "payment"

    def test_completing_cash_with_collected_by_stamps_it(
        self, app, db, sample_user, delivery_driver
    ):
        with app.app_context():
            service = CashCollectionService()
            order = _make_cash_order(
                db, sample_user, order_number="ORD-SYNC-STAMP", total="30000.00",
                status=OrderStatus.DELIVERED,
            )
            payment = service.ensure_cod_payment_for_order(order)
            db.session.flush()
            assert payment.collected_by is None

            payment.amount_collected = Decimal("30000.00")
            service.sync_payment_projection(payment, collected_by=delivery_driver.id)
            db.session.flush()
            db.session.refresh(payment)

            # The PERSISTED column must carry the collector.
            assert payment.status == PaymentStatus.COMPLETED
            assert payment.collected_by == delivery_driver.id
            assert payment.outstanding_amount == Decimal("0.00")

    def test_does_not_overwrite_existing_collected_by(
        self, app, db, sample_user, delivery_driver, second_delivery_driver
    ):
        with app.app_context():
            service = CashCollectionService()
            order = _make_cash_order(
                db, sample_user, order_number="ORD-SYNC-NOOVR", total="20000.00",
                status=OrderStatus.DELIVERED,
            )
            payment = service.ensure_cod_payment_for_order(order)
            db.session.flush()
            payment.collected_by = delivery_driver.id  # already attributed

            payment.amount_collected = Decimal("20000.00")
            # A different collector is passed, but the existing one must win.
            service.sync_payment_projection(
                payment, collected_by=second_delivery_driver.id
            )
            db.session.flush()
            db.session.refresh(payment)

            assert payment.status == PaymentStatus.COMPLETED
            assert payment.collected_by == delivery_driver.id

    @pytest.mark.parametrize("method", [PaymentMethod.CARD, PaymentMethod.CLICK])
    def test_non_cash_completing_needs_no_collector(
        self, app, db, sample_user, method
    ):
        """Card/Click payments completing fully require no collector."""
        with app.app_context():
            service = CashCollectionService()
            order = Order(
                user_id=sample_user.id,
                order_number=f"ORD-SYNC-{method.value}",
                status=OrderStatus.DELIVERED,
                subtotal=Decimal("25000.00"),
                delivery_fee=Decimal("0.00"),
                discount_amount=Decimal("0.00"),
                loyalty_discount=Decimal("0.00"),
                total_amount=Decimal("25000.00"),
                payment_method=method,
                created_at=datetime.now(UTC),
            )
            db.session.add(order)
            db.session.flush()
            payment = Payment(
                order_id=order.id,
                user_id=sample_user.id,
                amount=Decimal("25000.00"),
                currency="UZS",
                payment_method=method,
                status=PaymentStatus.PENDING,
                amount_collected=Decimal("25000.00"),
                outstanding_amount=Decimal("0.00"),
            )
            db.session.add(payment)
            db.session.flush()

            # Must NOT raise even with collected_by unset.
            service.sync_payment_projection(payment)
            db.session.flush()
            db.session.refresh(payment)

            assert payment.status == PaymentStatus.COMPLETED
            assert payment.collected_by is None

    def test_partial_collection_yields_partially_paid_no_collector_required(
        self, app, db, sample_user
    ):
        with app.app_context():
            service = CashCollectionService()
            order = _make_cash_order(
                db, sample_user, order_number="ORD-SYNC-PARTIAL", total="40000.00",
                status=OrderStatus.DELIVERED,
            )
            payment = service.ensure_cod_payment_for_order(order)
            db.session.flush()

            payment.amount_collected = Decimal("15000.00")
            # No collector supplied — must NOT raise for a partial payment.
            service.sync_payment_projection(payment)
            db.session.flush()
            db.session.refresh(payment)

            assert payment.status == PaymentStatus.PARTIALLY_PAID
            assert payment.outstanding_amount == Decimal("25000.00")
            assert payment.collected_by is None
            assert payment.paid_at is None

    def test_zero_collection_yields_pending(self, app, db, sample_user):
        with app.app_context():
            service = CashCollectionService()
            order = _make_cash_order(
                db, sample_user, order_number="ORD-SYNC-ZERO", total="40000.00",
                status=OrderStatus.DELIVERED,
            )
            payment = service.ensure_cod_payment_for_order(order)
            db.session.flush()

            payment.amount_collected = Decimal("0.00")
            service.sync_payment_projection(payment)
            db.session.flush()
            db.session.refresh(payment)

            assert payment.status == PaymentStatus.PENDING
            assert payment.outstanding_amount == Decimal("40000.00")
            assert payment.collected_by is None
            assert payment.paid_at is None

    def test_reversing_completed_back_to_partial_clears_paid_at(
        self, app, db, sample_user, delivery_driver
    ):
        """A completed cash payment that is partly reversed drops to
        PARTIALLY_PAID with paid_at cleared; collected_by is retained."""
        with app.app_context():
            service = CashCollectionService()
            order = _make_cash_order(
                db, sample_user, order_number="ORD-SYNC-REV", total="50000.00",
                status=OrderStatus.DELIVERED,
            )
            payment = service.ensure_cod_payment_for_order(order)
            db.session.flush()

            payment.amount_collected = Decimal("50000.00")
            service.sync_payment_projection(payment, collected_by=delivery_driver.id)
            db.session.flush()
            assert payment.status == PaymentStatus.COMPLETED
            assert payment.paid_at is not None

            # Reverse half the cash.
            payment.amount_collected = Decimal("20000.00")
            service.sync_payment_projection(payment)
            db.session.flush()
            db.session.refresh(payment)

            assert payment.status == PaymentStatus.PARTIALLY_PAID
            assert payment.outstanding_amount == Decimal("30000.00")
            assert payment.paid_at is None
            # collected_by persists from the earlier completion.
            assert payment.collected_by == delivery_driver.id

    def test_idempotent_resync_keeps_completed_and_collector(
        self, app, db, sample_user, delivery_driver
    ):
        with app.app_context():
            service = CashCollectionService()
            order = _make_cash_order(
                db, sample_user, order_number="ORD-SYNC-IDEMP", total="30000.00",
                status=OrderStatus.DELIVERED,
            )
            payment = service.ensure_cod_payment_for_order(order)
            db.session.flush()

            payment.amount_collected = Decimal("30000.00")
            service.sync_payment_projection(payment, collected_by=delivery_driver.id)
            db.session.flush()
            db.session.refresh(payment)
            # SQLite drops tzinfo on round-trip; compare on naive wall-clock value.
            first_paid_at = payment.paid_at.replace(tzinfo=None)

            # Re-sync without re-supplying a collector — must stay COMPLETED and
            # must NOT raise (collected_by already on the row).
            service.sync_payment_projection(payment)
            db.session.flush()
            db.session.refresh(payment)

            assert payment.status == PaymentStatus.COMPLETED
            assert payment.collected_by == delivery_driver.id
            assert payment.paid_at.replace(tzinfo=None) == first_paid_at

    def test_order_is_paid_and_paid_at_flip_on_completion(
        self, app, db, sample_user, delivery_driver
    ):
        with app.app_context():
            service = CashCollectionService()
            order = _make_cash_order(
                db, sample_user, order_number="ORD-SYNC-ISPAID", total="30000.00",
                status=OrderStatus.DELIVERED,
            )
            payment = service.ensure_cod_payment_for_order(order)
            db.session.flush()
            assert order.is_paid is not True

            payment.amount_collected = Decimal("30000.00")
            service.sync_payment_projection(payment, collected_by=delivery_driver.id)
            db.session.flush()
            db.session.refresh(order)

            assert order.is_paid is True
            assert order.paid_at is not None
            # SQLite drops tzinfo on round-trip; compare on naive wall-clock value.
            assert order.paid_at.replace(tzinfo=None) == payment.paid_at.replace(tzinfo=None)

    def test_award_hook_fires_once_on_became_fully_paid(
        self, app, db, sample_user, delivery_driver, monkeypatch
    ):
        """When a delivered COD order becomes fully paid, the purchase-award
        hook fires exactly once (idempotent across re-syncs)."""
        with app.app_context():
            service = CashCollectionService()
            order = _make_cash_order(
                db, sample_user, order_number="ORD-SYNC-AWARD", total="30000.00",
                status=OrderStatus.DELIVERED,
            )
            payment = service.ensure_cod_payment_for_order(order)
            db.session.flush()

            calls = []
            from business_app.services.order_service import OrderService

            def _spy(self, awarded_order, commit=True):
                calls.append(awarded_order.id)

            monkeypatch.setattr(OrderService, "maybe_award_purchase_points", _spy)

            payment.amount_collected = Decimal("30000.00")
            service.sync_payment_projection(payment, collected_by=delivery_driver.id)
            db.session.flush()
            assert calls == [order.id]

            # A second sync (order already is_paid) must NOT re-fire the hook —
            # became_fully_paid is False because order.is_paid is already True.
            service.sync_payment_projection(payment)
            db.session.flush()
            assert calls == [order.id]


# =========================================================================== #
# consume_reserved_prepayment_for_payment
# =========================================================================== #
@pytest.mark.unit
class TestConsumeReservedPrepaymentCollector:
    def _reserve(self, service, db, customer, *, order_number, total, event):
        """Create a pending cash order, reserve `event`'s credit against it."""
        order = _make_cash_order(
            db, customer, order_number=order_number, total=total,
            status=OrderStatus.PENDING,
        )
        payment = service.ensure_cod_payment_for_order(order)
        db.session.flush()
        service.reserve_customer_prepaid_credit_for_payment(payment)
        db.session.flush()
        return order, payment

    def test_full_reservation_consumed_uses_event_collector(
        self, app, db, sample_user, delivery_driver, delivery_driver_profile
    ):
        with app.app_context():
            service = CashCollectionService()
            # Seed credit whose source event records the driver as collector.
            event = _seed_unapplied_event(
                db,
                customer_id=sample_user.id,
                amount="30000.00",
                collector_user_id=delivery_driver.id,
                recorded_by_user_id=delivery_driver.id,
                source="standalone_meeting",
            )
            order, payment = self._reserve(
                service, db, sample_user,
                order_number="ORD-CONS-FULL", total="30000.00", event=event,
            )

            order.status = OrderStatus.DELIVERED
            db.session.flush()
            # collected_by passed here is a *fallback only* — the event collector
            # must take precedence.
            consumed = service.consume_reserved_prepayment_for_payment(
                payment, collected_by=99999
            )
            db.session.flush()
            db.session.refresh(payment)

            assert consumed == Decimal("30000.00")
            assert payment.status == PaymentStatus.COMPLETED
            assert payment.outstanding_amount == Decimal("0.00")
            # Collector derived from the source event, NOT the fallback.
            assert payment.collected_by == delivery_driver.id

    def test_collector_falls_back_to_recorded_by_when_collector_null(
        self, app, db, sample_user, admin_user
    ):
        """When the source event has no collector_user_id, the recorder is used."""
        with app.app_context():
            service = CashCollectionService()
            event = _seed_unapplied_event(
                db,
                customer_id=sample_user.id,
                amount="25000.00",
                collector_user_id=None,
                recorded_by_user_id=admin_user.id,
                source="admin_adjustment",
            )
            order, payment = self._reserve(
                service, db, sample_user,
                order_number="ORD-CONS-RECBY", total="25000.00", event=event,
            )

            order.status = OrderStatus.DELIVERED
            db.session.flush()
            consumed = service.consume_reserved_prepayment_for_payment(payment)
            db.session.flush()
            db.session.refresh(payment)

            assert consumed == Decimal("25000.00")
            assert payment.status == PaymentStatus.COMPLETED
            assert payment.collected_by == admin_user.id

    def test_explicit_collected_by_used_only_when_no_event_collector(
        self, app, db, sample_user
    ):
        """If the source event has neither collector nor recorder, the explicit
        ``collected_by`` argument settles the invariant."""
        with app.app_context():
            service = CashCollectionService()
            # Source event has BOTH collector and recorder null.
            event = _seed_unapplied_event(
                db,
                customer_id=sample_user.id,
                amount="18000.00",
                collector_user_id=None,
                recorded_by_user_id=None,
                source="admin_adjustment",
            )
            order, payment = self._reserve(
                service, db, sample_user,
                order_number="ORD-CONS-EXPL", total="18000.00", event=event,
            )

            order.status = OrderStatus.DELIVERED
            db.session.flush()
            consumed = service.consume_reserved_prepayment_for_payment(
                payment, collected_by=sample_user.id
            )
            db.session.flush()
            db.session.refresh(payment)

            assert consumed == Decimal("18000.00")
            assert payment.status == PaymentStatus.COMPLETED
            assert payment.collected_by == sample_user.id

    def test_multiple_reservations_take_collector_from_first_non_null(
        self, app, db, sample_user, admin_user, delivery_driver, delivery_driver_profile
    ):
        """Two source events back one payment; the collector is taken from the
        earliest-allocated reservation whose event has a collector."""
        with app.app_context():
            service = CashCollectionService()
            # Older event has NO collector (only recorder = admin).
            older = _seed_unapplied_event(
                db,
                customer_id=sample_user.id,
                amount="20000.00",
                collector_user_id=None,
                recorded_by_user_id=admin_user.id,
                source="admin_adjustment",
                occurred_at=datetime.now(UTC) - timedelta(hours=2),
            )
            # Newer event records the driver as collector.
            _seed_unapplied_event(
                db,
                customer_id=sample_user.id,
                amount="20000.00",
                collector_user_id=delivery_driver.id,
                recorded_by_user_id=delivery_driver.id,
                source="standalone_meeting",
                occurred_at=datetime.now(UTC) - timedelta(hours=1),
            )

            # One 35k order will draw fully from `older` (20k) then 15k from
            # the newer event, so reservations span both events.
            order, payment = self._reserve(
                service, db, sample_user,
                order_number="ORD-CONS-MULTI", total="35000.00", event=older,
            )

            order.status = OrderStatus.DELIVERED
            db.session.flush()
            consumed = service.consume_reserved_prepayment_for_payment(payment)
            db.session.flush()
            db.session.refresh(payment)

            assert consumed == Decimal("35000.00")
            assert payment.status == PaymentStatus.COMPLETED
            # First reservation's event (older) has collector=None, so its
            # fallback recorded_by (admin) is taken — collector resolution
            # short-circuits on the first non-null derived id.
            assert payment.collected_by == admin_user.id

    def test_partial_reservation_consumed_partially_paid_no_collector(
        self, app, db, sample_user, admin_user
    ):
        """A reservation that only partly covers the order leaves it
        PARTIALLY_PAID — not COMPLETED — so no collector is required."""
        with app.app_context():
            service = CashCollectionService()
            event = _seed_unapplied_event(
                db,
                customer_id=sample_user.id,
                amount="10000.00",
                collector_user_id=None,
                recorded_by_user_id=admin_user.id,
            )
            # Order is 30k but only 10k credit available to reserve.
            order, payment = self._reserve(
                service, db, sample_user,
                order_number="ORD-CONS-PART", total="30000.00", event=event,
            )
            db.session.refresh(payment)
            assert payment.provider_data.get("cod_prepayment_reserved_amount") == 10000.0

            order.status = OrderStatus.DELIVERED
            db.session.flush()
            consumed = service.consume_reserved_prepayment_for_payment(payment)
            db.session.flush()
            db.session.refresh(payment)

            assert consumed == Decimal("10000.00")
            assert payment.status == PaymentStatus.PARTIALLY_PAID
            assert payment.outstanding_amount == Decimal("20000.00")
            # Still has a recorded collector (admin) because _allocate_to_payment
            # is not the path; but the invariant only fires on COMPLETED, and
            # this row is PARTIALLY_PAID, so collected_by may legitimately be set
            # by the consume path's collector derivation OR remain unset. The
            # invariant must NOT have raised.

    def test_no_reservation_is_noop(self, app, db, sample_user):
        with app.app_context():
            service = CashCollectionService()
            order = _make_cash_order(
                db, sample_user, order_number="ORD-CONS-NOOP", total="30000.00",
                status=OrderStatus.DELIVERED,
            )
            payment = service.ensure_cod_payment_for_order(order)
            db.session.flush()

            consumed = service.consume_reserved_prepayment_for_payment(payment)
            db.session.flush()
            db.session.refresh(payment)

            assert consumed == Decimal("0.00")
            assert payment.status == PaymentStatus.PENDING
            assert payment.collected_by is None

    def test_non_cash_payment_is_noop(self, app, db, sample_user):
        with app.app_context():
            service = CashCollectionService()
            order = Order(
                user_id=sample_user.id,
                order_number="ORD-CONS-CARD",
                status=OrderStatus.DELIVERED,
                subtotal=Decimal("30000.00"),
                delivery_fee=Decimal("0.00"),
                discount_amount=Decimal("0.00"),
                loyalty_discount=Decimal("0.00"),
                total_amount=Decimal("30000.00"),
                payment_method=PaymentMethod.CARD,
                created_at=datetime.now(UTC),
            )
            db.session.add(order)
            db.session.flush()
            payment = Payment(
                order_id=order.id,
                user_id=sample_user.id,
                amount=Decimal("30000.00"),
                currency="UZS",
                payment_method=PaymentMethod.CARD,
                status=PaymentStatus.PENDING,
                amount_collected=Decimal("0.00"),
                outstanding_amount=Decimal("30000.00"),
            )
            db.session.add(payment)
            db.session.flush()

            consumed = service.consume_reserved_prepayment_for_payment(
                payment, collected_by=sample_user.id
            )
            assert consumed == Decimal("0.00")
            assert payment.status == PaymentStatus.PENDING
            assert payment.collected_by is None


# =========================================================================== #
# post_collection (staff/admin endpoint path) completing a payment
# =========================================================================== #
@pytest.mark.unit
class TestPostCollectionStampsCollector:
    def test_delivered_cod_payment_completed_records_collector(
        self, app, db, sample_user, delivery_driver, delivery_driver_profile
    ):
        """The staff cash-collection path (post_collection -> _allocate_to_payment
        -> sync_payment_projection) must stamp collected_by when the cash fully
        settles a delivered COD payment."""
        with app.app_context():
            service = CashCollectionService()
            order = _make_cash_order(
                db, sample_user, order_number="ORD-POST-FULL", total="40000.00",
                status=OrderStatus.DELIVERED,
            )
            payment = service.ensure_cod_payment_for_order(order)
            db.session.commit()

            event = service.post_collection(
                customer_id=sample_user.id,
                amount=Decimal("40000.00"),
                source="standalone_meeting",
                collector_user_id=delivery_driver.id,
                recorded_by_user_id=delivery_driver.id,
                order_id=order.id,
                notes="Driver collected COD at a standalone meeting",
            )

            db.session.refresh(payment)
            assert event.unapplied_amount == Decimal("0.00")
            assert payment.status == PaymentStatus.COMPLETED
            assert payment.outstanding_amount == Decimal("0.00")
            # The completed CASH payment row carries the collector.
            assert payment.collected_by == delivery_driver.id

    def test_admin_recorded_collection_stamps_recorder_as_collector(
        self, app, db, sample_user, admin_user
    ):
        """An admin-adjustment collection with no on-route collector still
        stamps collected_by (the recorder) so the invariant holds."""
        with app.app_context():
            service = CashCollectionService()
            order = _make_cash_order(
                db, sample_user, order_number="ORD-POST-ADMIN", total="22000.00",
                status=OrderStatus.DELIVERED,
            )
            payment = service.ensure_cod_payment_for_order(order)
            db.session.commit()

            service.post_collection(
                customer_id=sample_user.id,
                amount=Decimal("22000.00"),
                source="admin_adjustment",
                recorded_by_user_id=admin_user.id,
                order_id=order.id,
                notes="Admin recorded cash settlement",
            )

            db.session.refresh(payment)
            assert payment.status == PaymentStatus.COMPLETED
            assert payment.collected_by == admin_user.id

    def test_partial_post_collection_no_collector_required(
        self, app, db, sample_user, delivery_driver, delivery_driver_profile
    ):
        with app.app_context():
            service = CashCollectionService()
            order = _make_cash_order(
                db, sample_user, order_number="ORD-POST-PART", total="40000.00",
                status=OrderStatus.DELIVERED,
            )
            payment = service.ensure_cod_payment_for_order(order)
            db.session.commit()

            service.post_collection(
                customer_id=sample_user.id,
                amount=Decimal("15000.00"),
                source="standalone_meeting",
                collector_user_id=delivery_driver.id,
                recorded_by_user_id=delivery_driver.id,
                order_id=order.id,
                notes="Partial COD collection",
            )

            db.session.refresh(payment)
            assert payment.status == PaymentStatus.PARTIALLY_PAID
            assert payment.outstanding_amount == Decimal("25000.00")


# =========================================================================== #
# End-to-end delivery settlement via OrderService.update_order_status
# =========================================================================== #
@pytest.mark.unit
@pytest.mark.order
class TestDeliverySettlementEndToEnd:
    def test_delivered_via_update_order_status_with_driver_completes_payment(
        self, app, db, sample_user, delivery_driver, delivery_driver_profile
    ):
        """COD order fully covered by reserved prepayment, marked DELIVERED via
        OrderService.update_order_status(updated_by=driver) -> payment COMPLETED
        with collected_by set, no exception."""
        with app.app_context():
            from business_app.services.order_service import OrderService

            service = CashCollectionService()
            # Driver previously collected standalone cash (reservable surplus).
            event = _seed_unapplied_event(
                db,
                customer_id=sample_user.id,
                amount="35000.00",
                collector_user_id=delivery_driver.id,
                recorded_by_user_id=delivery_driver.id,
                source="standalone_meeting",
            )
            order = _make_cash_order(
                db, sample_user, order_number="ORD-E2E-DELIV", total="35000.00",
                status=OrderStatus.OUT_FOR_DELIVERY,
            )
            # Address is required for DELIVERED (ARCH-006 order invariant).
            order.delivery_address_id = self._make_address(db, sample_user)
            payment = service.ensure_cod_payment_for_order(order)
            db.session.flush()
            service.reserve_customer_prepaid_credit_for_payment(payment)
            db.session.commit()
            assert event.id is not None

            OrderService().update_order_status(
                order.id, OrderStatus.DELIVERED, updated_by=delivery_driver.id
            )

            db.session.refresh(payment)
            assert payment.status == PaymentStatus.COMPLETED
            assert payment.outstanding_amount == Decimal("0.00")
            # collected_by derived from the reservation's source event collector.
            assert payment.collected_by == delivery_driver.id

    def test_delivered_with_updated_by_none_uses_reservation_collector(
        self, app, db, sample_user, delivery_driver, delivery_driver_profile
    ):
        """Even when update_order_status is called with updated_by=None, a
        reservation collector still satisfies the invariant (no exception)."""
        with app.app_context():
            from business_app.services.order_service import OrderService

            service = CashCollectionService()
            event = _seed_unapplied_event(
                db,
                customer_id=sample_user.id,
                amount="28000.00",
                collector_user_id=delivery_driver.id,
                recorded_by_user_id=delivery_driver.id,
                source="standalone_meeting",
            )
            order = _make_cash_order(
                db, sample_user, order_number="ORD-E2E-NONE", total="28000.00",
                status=OrderStatus.OUT_FOR_DELIVERY,
            )
            order.delivery_address_id = self._make_address(db, sample_user)
            payment = service.ensure_cod_payment_for_order(order)
            db.session.flush()
            service.reserve_customer_prepaid_credit_for_payment(payment)
            db.session.commit()
            assert event.id is not None

            OrderService().update_order_status(
                order.id, OrderStatus.DELIVERED, updated_by=None
            )

            db.session.refresh(payment)
            assert payment.status == PaymentStatus.COMPLETED
            assert payment.collected_by == delivery_driver.id

    @staticmethod
    def _make_address(db, user):
        from business_app.models.user import UserAddress

        address = UserAddress(
            user_id=user.id,
            full_address="123 Test Street, Tashkent",
            street_address="123 Test Street",
            city="Tashkent",
            latitude=41.2995,
            longitude=69.2401,
            is_default=True,
        )
        db.session.add(address)
        db.session.flush()
        return address.id


@pytest.mark.unit
def test_electronic_payment_completed_by_cash_records_the_collector(
    app, db, sample_user, delivery_driver
):
    """Stamping follows the MONEY; asserting follows the RAIL.

    A Click receivable settled with physical cash (an order edited upward at the
    door — prod 961) must still record WHO took the cash, even though
    ck_payments_cash_completed_requires_collector exempts non-cash rows by its
    first disjunct. Before this split the whole stamping branch was CASH-gated,
    so the audit trail silently lost the collector.

    Plan: docs/superpowers/plans/2026-08-08-open-receivable-ssot.md (Task 6)
    """
    with app.app_context():
        order = Order(
            user_id=sample_user.id,
            order_number="ORD-SYNC-CLICK-COLLECTOR",
            status=OrderStatus.DELIVERED,
            subtotal=Decimal("90000.00"),
            delivery_fee=Decimal("0.00"),
            discount_amount=Decimal("0.00"),
            loyalty_discount=Decimal("0.00"),
            total_amount=Decimal("90000.00"),
            payment_method=PaymentMethod.CLICK,
            created_at=datetime.now(UTC),
        )
        db.session.add(order)
        db.session.flush()
        payment = Payment(
            order_id=order.id,
            user_id=sample_user.id,
            payment_method=PaymentMethod.CLICK,
            amount=Decimal("90000.00"),
            amount_collected=Decimal("90000.00"),
            outstanding_amount=Decimal("0.00"),
            status=PaymentStatus.PARTIALLY_PAID,
            currency="UZS",
            payment_id="pay-click-collector",
        )
        db.session.add(payment)
        db.session.commit()

        CashCollectionService().sync_payment_projection(
            payment, collected_by=delivery_driver.id
        )
        db.session.commit()
        db.session.refresh(payment)

        assert payment.status == PaymentStatus.COMPLETED
        assert payment.collected_by == delivery_driver.id
