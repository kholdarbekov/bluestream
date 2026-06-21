"""Unit tests for auto-reservation of over-collected COD prepayment.

Covers ``CashCollectionService.auto_reserve_against_pending_payments`` plus the
``post_collection`` hook and the enrichment of ``get_customer_cod_statement``.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from business_app.models.delivery import Delivery, DeliveryPerson
from business_app.models.order import Order
from business_app.models.payment import (
    CashCollectionAllocation,
    CashCollectionEvent,
    DriverCashSession,
    Payment,
)
from business_app.models.user import User
from business_app.services.cash_collection_service import CashCollectionService
from business_app.utils.password_security import hash_password
from shared.enums import (
    DeliveryStatus,
    OrderStatus,
    PaymentMethod,
    PaymentStatus,
    UserRole,
    UserType,
)


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
        email='driver.two.autoreserve@example.com',
        phone='+998901234599',
        password_hash=hash_password('DriverTwoPassword123!'),
        first_name='Delivery',
        last_name='Driver Two',
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


def _make_pending_cash_order(
    db,
    sample_user,
    *,
    order_number: str,
    total: str,
    created_at: datetime = None,
) -> Order:
    order = Order(
        user_id=sample_user.id,
        order_number=order_number,
        status=OrderStatus.PENDING,
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


@pytest.mark.unit
class TestAutoReserveAgainstPendingPayments:
    def test_returns_zero_when_no_unapplied_prepayment(self, app, db, sample_user):
        with app.app_context():
            service = CashCollectionService()
            reserved = service.auto_reserve_against_pending_payments(sample_user.id)
            assert reserved == Decimal("0.00")

    def test_reserves_against_single_pending_cash_payment(
        self,
        app,
        db,
        sample_user,
        admin_user,
        delivery_driver,
        delivery_driver_profile,
    ):
        with app.app_context():
            service = CashCollectionService()

            # Seed an unapplied prepayment of 50k by directly creating an event
            # with unapplied amount (mimicking surplus left behind after an
            # earlier over-collection).
            event = CashCollectionEvent(
                customer_id=sample_user.id,
                recorded_by_user_id=admin_user.id,
                amount=Decimal("50000.00"),
                currency="UZS",
                source="admin_adjustment",
                occurred_at=datetime.now(UTC),
                notes="Seeded prepayment surplus",
                unapplied_amount=Decimal("50000.00"),
            )
            db.session.add(event)
            db.session.flush()
            assert service.get_customer_prepaid_balance(sample_user.id) == Decimal("50000.00")

            # Pending cash order with 30k outstanding.
            pending_order = _make_pending_cash_order(
                db,
                sample_user,
                order_number="ORD-AUTO-RES-001",
                total="30000.00",
            )
            pending_payment = service.ensure_cod_payment_for_order(pending_order)
            db.session.flush()

            reserved = service.auto_reserve_against_pending_payments(
                sample_user.id,
                actor_user_id=admin_user.id,
            )
            db.session.flush()

            assert reserved == Decimal("30000.00")
            db.session.refresh(event)
            db.session.refresh(pending_payment)
            assert event.unapplied_amount == Decimal("20000.00")
            assert pending_payment.provider_data.get(
                "cod_prepayment_reserved_amount"
            ) == 30000.0
            # Payment projection unchanged because reservation does not
            # affect amount_collected/outstanding.
            assert pending_payment.amount_collected == Decimal("0.00")
            assert pending_payment.outstanding_amount == Decimal("30000.00")

    def test_skips_delivered_orders(
        self,
        app,
        db,
        sample_user,
        admin_user,
    ):
        with app.app_context():
            service = CashCollectionService()

            # Seed 25k unapplied prepayment.
            seed_event = CashCollectionEvent(
                customer_id=sample_user.id,
                recorded_by_user_id=admin_user.id,
                amount=Decimal("25000.00"),
                currency="UZS",
                source="admin_adjustment",
                occurred_at=datetime.now(UTC),
                notes="Seeded prepayment surplus",
                unapplied_amount=Decimal("25000.00"),
            )
            db.session.add(seed_event)
            db.session.flush()

            # A DELIVERED cash order with outstanding 10k — must be ignored
            # by the sweep (only non-delivered statuses are reservable).
            delivered_order = Order(
                user_id=sample_user.id,
                order_number="ORD-AUTO-RES-DEL-001",
                status=OrderStatus.DELIVERED,
                subtotal=Decimal("10000.00"),
                delivery_fee=Decimal("0.00"),
                discount_amount=Decimal("0.00"),
                loyalty_discount=Decimal("0.00"),
                total_amount=Decimal("10000.00"),
                payment_method=PaymentMethod.CASH,
                created_at=datetime.now(UTC) - timedelta(days=2),
            )
            db.session.add(delivered_order)
            db.session.flush()
            delivered_payment = service.ensure_cod_payment_for_order(delivered_order)

            # A PENDING cash order with 15k outstanding — eligible target.
            pending_order = _make_pending_cash_order(
                db,
                sample_user,
                order_number="ORD-AUTO-RES-PEND-001",
                total="15000.00",
            )
            pending_payment = service.ensure_cod_payment_for_order(pending_order)
            db.session.flush()

            reserved = service.auto_reserve_against_pending_payments(sample_user.id)
            db.session.flush()

            assert reserved == Decimal("15000.00")
            db.session.refresh(delivered_payment)
            db.session.refresh(pending_payment)
            assert pending_payment.provider_data.get(
                "cod_prepayment_reserved_amount"
            ) == 15000.0
            # Delivered payment must NOT receive any reservation.
            assert (
                delivered_payment.provider_data.get("cod_prepayment_reserved_amount", 0)
                == 0
            )
            # Remaining customer prepayment balance is 10k (25k - 15k).
            assert service.get_customer_prepaid_balance(sample_user.id) == Decimal(
                "10000.00"
            )

    def test_skips_cancelled_orders(
        self,
        app,
        db,
        sample_user,
        admin_user,
    ):
        with app.app_context():
            service = CashCollectionService()

            seed_event = CashCollectionEvent(
                customer_id=sample_user.id,
                recorded_by_user_id=admin_user.id,
                amount=Decimal("20000.00"),
                currency="UZS",
                source="admin_adjustment",
                occurred_at=datetime.now(UTC),
                notes="Seeded prepayment surplus",
                unapplied_amount=Decimal("20000.00"),
            )
            db.session.add(seed_event)
            db.session.flush()

            # Cancelled cash order: outstanding > 0 but status is CANCELLED.
            cancelled_order = Order(
                user_id=sample_user.id,
                order_number="ORD-AUTO-RES-CAN-001",
                status=OrderStatus.CANCELLED,
                subtotal=Decimal("12000.00"),
                delivery_fee=Decimal("0.00"),
                discount_amount=Decimal("0.00"),
                loyalty_discount=Decimal("0.00"),
                total_amount=Decimal("12000.00"),
                payment_method=PaymentMethod.CASH,
                created_at=datetime.now(UTC) - timedelta(days=1),
            )
            db.session.add(cancelled_order)
            db.session.flush()
            cancelled_payment = service.ensure_cod_payment_for_order(cancelled_order)

            pending_order = _make_pending_cash_order(
                db,
                sample_user,
                order_number="ORD-AUTO-RES-PEND-002",
                total="9000.00",
            )
            pending_payment = service.ensure_cod_payment_for_order(pending_order)
            db.session.flush()

            reserved = service.auto_reserve_against_pending_payments(sample_user.id)
            db.session.flush()

            assert reserved == Decimal("9000.00")
            db.session.refresh(cancelled_payment)
            db.session.refresh(pending_payment)
            assert pending_payment.provider_data.get(
                "cod_prepayment_reserved_amount"
            ) == 9000.0
            # Cancelled payment must not be touched.
            assert (
                cancelled_payment.provider_data.get(
                    "cod_prepayment_reserved_amount", 0
                )
                == 0
            )

    def test_reserves_oldest_pending_order_first(
        self,
        app,
        db,
        sample_user,
        admin_user,
    ):
        with app.app_context():
            service = CashCollectionService()

            # 25k surplus available; total pending demand will be 30k, so the
            # older order gets fully covered (20k) and the newer one gets
            # partially covered (5k).
            seed_event = CashCollectionEvent(
                customer_id=sample_user.id,
                recorded_by_user_id=admin_user.id,
                amount=Decimal("25000.00"),
                currency="UZS",
                source="admin_adjustment",
                occurred_at=datetime.now(UTC),
                notes="Seeded prepayment surplus",
                unapplied_amount=Decimal("25000.00"),
            )
            db.session.add(seed_event)
            db.session.flush()

            older_order = _make_pending_cash_order(
                db,
                sample_user,
                order_number="ORD-AUTO-RES-OLDER",
                total="20000.00",
                created_at=datetime.now(UTC) - timedelta(hours=2),
            )
            older_payment = service.ensure_cod_payment_for_order(older_order)

            newer_order = _make_pending_cash_order(
                db,
                sample_user,
                order_number="ORD-AUTO-RES-NEWER",
                total="10000.00",
                created_at=datetime.now(UTC),
            )
            newer_payment = service.ensure_cod_payment_for_order(newer_order)
            db.session.flush()

            reserved = service.auto_reserve_against_pending_payments(sample_user.id)
            db.session.flush()

            db.session.refresh(older_payment)
            db.session.refresh(newer_payment)

            assert reserved == Decimal("25000.00")
            assert older_payment.provider_data.get(
                "cod_prepayment_reserved_amount"
            ) == 20000.0
            assert newer_payment.provider_data.get(
                "cod_prepayment_reserved_amount"
            ) == 5000.0
            assert service.get_customer_prepaid_balance(sample_user.id) == Decimal(
                "0.00"
            )

    def test_idempotent_on_repeat_call(
        self,
        app,
        db,
        sample_user,
        admin_user,
    ):
        with app.app_context():
            service = CashCollectionService()

            seed_event = CashCollectionEvent(
                customer_id=sample_user.id,
                recorded_by_user_id=admin_user.id,
                amount=Decimal("30000.00"),
                currency="UZS",
                source="admin_adjustment",
                occurred_at=datetime.now(UTC),
                notes="Seeded prepayment surplus",
                unapplied_amount=Decimal("30000.00"),
            )
            db.session.add(seed_event)
            db.session.flush()

            pending_order = _make_pending_cash_order(
                db,
                sample_user,
                order_number="ORD-AUTO-RES-IDEMP",
                total="22000.00",
            )
            pending_payment = service.ensure_cod_payment_for_order(pending_order)
            db.session.flush()

            first = service.auto_reserve_against_pending_payments(sample_user.id)
            db.session.flush()
            assert first == Decimal("22000.00")
            db.session.refresh(pending_payment)
            first_snapshot = pending_payment.provider_data.get(
                "cod_prepayment_reserved_amount"
            )
            assert first_snapshot == 22000.0

            second = service.auto_reserve_against_pending_payments(sample_user.id)
            db.session.flush()
            db.session.refresh(pending_payment)

            assert second == Decimal("0.00")
            assert (
                pending_payment.provider_data.get("cod_prepayment_reserved_amount")
                == first_snapshot
            )
            # Customer prepaid balance unchanged after the no-op repeat call.
            assert service.get_customer_prepaid_balance(sample_user.id) == Decimal(
                "8000.00"
            )

    def test_post_collection_triggers_auto_reservation(
        self,
        app,
        db,
        sample_user,
        delivery_driver,
        delivery_driver_profile,
    ):
        with app.app_context():
            service = CashCollectionService()

            # Customer has one pending CASH order with 57K outstanding.
            pending_order = _make_pending_cash_order(
                db,
                sample_user,
                order_number="ORD-AUTO-RES-HOOK",
                total="57000.00",
            )
            pending_payment = service.ensure_cod_payment_for_order(pending_order)
            db.session.commit()

            # Driver records a standalone collection of 80K with no order_id
            # (e.g. a sidewalk hand-off). The new event has no order context,
            # _allocate_oldest_first finds no delivered debts, and unapplied
            # stays at 80K. The post_collection hook should then sweep this
            # surplus into a reservation against the pending 57K payment.
            event = service.post_collection(
                customer_id=sample_user.id,
                amount=Decimal("80000.00"),
                source="standalone_meeting",
                collector_user_id=delivery_driver.id,
                recorded_by_user_id=delivery_driver.id,
                notes="Standalone collection - customer paid in cash",
            )

            db.session.refresh(event)
            db.session.refresh(pending_payment)

            assert event.unapplied_amount == Decimal("23000.00")
            assert pending_payment.provider_data.get(
                "cod_prepayment_reserved_amount"
            ) == 57000.0
            # Reservation does not affect the actual payment projection.
            assert pending_payment.amount_collected == Decimal("0.00")
            assert pending_payment.outstanding_amount == Decimal("57000.00")

    def test_get_customer_cod_statement_exposes_reserved_and_net_fields(
        self,
        app,
        db,
        sample_user,
        admin_user,
    ):
        with app.app_context():
            service = CashCollectionService()

            seed_event = CashCollectionEvent(
                customer_id=sample_user.id,
                recorded_by_user_id=admin_user.id,
                amount=Decimal("40000.00"),
                currency="UZS",
                source="admin_adjustment",
                occurred_at=datetime.now(UTC),
                notes="Seeded prepayment surplus",
                unapplied_amount=Decimal("40000.00"),
            )
            db.session.add(seed_event)
            db.session.flush()

            pending_order = _make_pending_cash_order(
                db,
                sample_user,
                order_number="ORD-AUTO-RES-STMT",
                total="55000.00",
            )
            pending_payment = service.ensure_cod_payment_for_order(pending_order)
            db.session.flush()

            service.auto_reserve_against_pending_payments(sample_user.id)
            db.session.flush()
            db.session.refresh(pending_payment)
            assert pending_payment.provider_data.get(
                "cod_prepayment_reserved_amount"
            ) == 40000.0

            statement = service.get_customer_cod_statement(sample_user.id)

            # Top-level aggregates.
            assert statement["gross_outstanding_amount"] == 55000.0
            assert statement["reserved_prepayment_total"] == 40000.0
            assert statement["net_outstanding_amount"] == 15000.0
            assert statement["unreserved_prepayment_balance"] == 0.0
            # Backwards-compatible fields preserved.
            assert statement["total_outstanding_amount"] == 55000.0
            assert statement["available_prepayment_balance"] == 0.0

            # Per-item enrichment.
            assert len(statement["items"]) == 1
            item = statement["items"][0]
            assert item["payment_id"] == pending_payment.id
            assert item["outstanding_amount"] == 55000.0
            assert item["reserved_prepayment_amount"] == 40000.0
            assert item["net_outstanding_amount"] == 15000.0

    def test_cancellation_releases_auto_reserved_amount(
        self,
        app,
        db,
        sample_user,
        admin_user,
    ):
        with app.app_context():
            service = CashCollectionService()

            seed_event = CashCollectionEvent(
                customer_id=sample_user.id,
                recorded_by_user_id=admin_user.id,
                amount=Decimal("18000.00"),
                currency="UZS",
                source="admin_adjustment",
                occurred_at=datetime.now(UTC),
                notes="Seeded prepayment surplus",
                unapplied_amount=Decimal("18000.00"),
            )
            db.session.add(seed_event)
            db.session.flush()

            pending_order = _make_pending_cash_order(
                db,
                sample_user,
                order_number="ORD-AUTO-RES-REL",
                total="14000.00",
            )
            pending_payment = service.ensure_cod_payment_for_order(pending_order)
            db.session.flush()

            reserved = service.auto_reserve_against_pending_payments(sample_user.id)
            db.session.flush()
            assert reserved == Decimal("14000.00")
            assert service.get_customer_prepaid_balance(sample_user.id) == Decimal(
                "4000.00"
            )

            # Cancel the order, then release. release_reserved_prepayment_for_order
            # is what order_service.cancel_order calls under the hood.
            pending_order.status = OrderStatus.CANCELLED
            db.session.flush()
            released = service.release_reserved_prepayment_for_order(
                pending_order.id,
                actor_user_id=admin_user.id,
                reason="Order cancelled before delivery",
            )
            db.session.flush()
            db.session.refresh(pending_payment)
            db.session.refresh(seed_event)

            assert released == Decimal("14000.00")
            assert pending_payment.provider_data.get(
                "cod_prepayment_reserved_amount"
            ) == 0.0
            # The 14k went back to the seed event's unapplied amount.
            assert seed_event.unapplied_amount == Decimal("18000.00")
            assert service.get_customer_prepaid_balance(sample_user.id) == Decimal(
                "18000.00"
            )

    def test_delivery_completion_consumes_reservation_from_other_drivers_event(
        self,
        app,
        db,
        sample_user,
        delivery_driver,
        delivery_driver_profile,
        second_delivery_driver,
        second_delivery_driver_profile,
    ):
        """Driver B collects standalone cash that auto-reserves against a
        pending order that Driver A later delivers. The reservation must
        carry across drivers/sessions without double-counting cash in Driver
        B's session."""
        with app.app_context():
            service = CashCollectionService()

            # Customer has a pending CASH order with 57k outstanding.
            pending_order = _make_pending_cash_order(
                db,
                sample_user,
                order_number="ORD-AUTO-RES-CROSS",
                total="57000.00",
            )
            pending_payment = service.ensure_cod_payment_for_order(pending_order)
            db.session.commit()

            # Driver B (second_delivery_driver) collects 80k standalone. The
            # post_collection hook should reserve 57k against the pending
            # payment, leaving 23k unapplied in Driver B's event.
            event = service.post_collection(
                customer_id=sample_user.id,
                amount=Decimal("80000.00"),
                source="standalone_meeting",
                collector_user_id=second_delivery_driver.id,
                recorded_by_user_id=second_delivery_driver.id,
                notes="Driver B collected 80k standalone",
            )

            db.session.refresh(event)
            db.session.refresh(pending_payment)

            # Driver B's session shows the full 80k as gross collected, with
            # no offset for the reservation (reservations don't write events).
            driver_b_session = DriverCashSession.query.get(
                event.driver_cash_session_id
            )
            assert driver_b_session.driver_user_id == second_delivery_driver.id
            assert driver_b_session.gross_cash_collected == Decimal("80000.00")
            assert pending_payment.provider_data.get(
                "cod_prepayment_reserved_amount"
            ) == 57000.0

            pre_consume_b_gross = driver_b_session.gross_cash_collected

            # Capture the customer's CashCollectionEvent count BEFORE
            # consumption so we can assert no new event is written in the
            # delivering driver's session (consumption flips an existing
            # allocation only).
            events_before = CashCollectionEvent.query.filter_by(
                customer_id=sample_user.id
            ).count()

            # Driver A delivers the order. order_service marks the order
            # DELIVERED and calls consume_reserved_prepayment_for_payment.
            pending_order.status = OrderStatus.DELIVERED
            db.session.flush()

            consumed = service.consume_reserved_prepayment_for_payment(pending_payment)
            db.session.flush()
            db.session.refresh(pending_payment)
            db.session.refresh(driver_b_session)

            assert consumed == Decimal("57000.00")
            assert pending_payment.amount_collected == Decimal("57000.00")
            assert pending_payment.outstanding_amount == Decimal("0.00")
            assert pending_payment.status == PaymentStatus.COMPLETED
            # A COMPLETED cash payment must record WHO collected it (ARCH-006 /
            # ck_payments_cash_completed_requires_collector). Consumption of a
            # reservation derives the collector from the reservation's source
            # event — here Driver B, who physically collected the cash.
            assert pending_payment.collected_by == second_delivery_driver.id
            # No reservation remains; the marker is reset to 0.
            assert pending_payment.provider_data.get(
                "cod_prepayment_reserved_amount"
            ) == 0.0
            # Driver B's session gross_cash_collected MUST NOT have grown —
            # consumption flips an existing allocation, it does not write a
            # new cash collection event in any session.
            assert driver_b_session.gross_cash_collected == pre_consume_b_gross
            assert driver_b_session.gross_cash_collected == Decimal("80000.00")

            # And verify no new CashCollectionEvent row was written for the
            # customer during consumption — reservation consumption must
            # operate purely on the existing allocation/event ledger.
            events_after = CashCollectionEvent.query.filter_by(
                customer_id=sample_user.id
            ).count()
            assert events_after == events_before, (
                "consume_reserved_prepayment_for_payment must not write new CashCollectionEvent rows"
            )
