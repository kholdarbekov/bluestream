"""Unit tests for admin adjust_event_amount workflow on cash collection events."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from business_app.models.delivery import Delivery, DeliveryPerson
from business_app.models.order import Order
from business_app.models.payment import CashCollectionEvent, DriverCashSession
from business_app.services.cash_collection_service import CashCollectionService
from business_app.services.driver_reconciliation_service import DriverReconciliationService
from business_app.utils.exceptions import NotFoundError, ValidationError
from shared.enums import (
    DeliveryStatus,
    DriverCashSessionStatus,
    OrderStatus,
    PaymentMethod,
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
def cod_order(db, sample_order):
    sample_order.status = OrderStatus.DELIVERED
    sample_order.payment_method = PaymentMethod.CASH
    sample_order.delivered_at = datetime.now(UTC)
    db.session.commit()
    return sample_order


@pytest.fixture
def cod_delivery(db, cod_order, delivery_driver):
    delivery = Delivery(
        order_id=cod_order.id,
        delivery_person_id=delivery_driver.id,
        status=DeliveryStatus.DELIVERED,
        scheduled_date=datetime.now(UTC),
        scheduled_time_slot="09:00-12:00",
        actual_delivery_time=datetime.now(UTC),
        delivered_at=datetime.now(UTC),
    )
    db.session.add(delivery)
    db.session.commit()
    return delivery


def _seed_collection(
    *,
    service,
    sample_user,
    delivery_driver,
    cod_order,
    cod_delivery,
    amount,
):
    service.ensure_cod_payment_for_order(cod_order)
    db_session_event = service.post_collection(
        customer_id=sample_user.id,
        amount=Decimal(amount),
        source="delivery_completion",
        collector_user_id=delivery_driver.id,
        recorded_by_user_id=delivery_driver.id,
        order_id=cod_order.id,
        delivery_id=cod_delivery.id,
        notes="Initial collection",
    )
    return db_session_event


def _submit_session(driver, amount):
    """Move the driver's open session into SUBMITTED so adjustments are allowed."""
    DriverReconciliationService().submit_session(
        driver_user_id=driver.id,
        declared_cash=Decimal(amount),
        notes="Driver handoff for test",
        submitted_by_user_id=driver.id,
    )


@pytest.mark.unit
class TestAdjustEventAmount:
    def test_voids_original_and_cross_links_via_entry_metadata(
        self,
        app,
        db,
        sample_user,
        admin_user,
        delivery_driver,
        delivery_driver_profile,
        cod_order,
        cod_delivery,
    ):
        with app.app_context():
            service = CashCollectionService()
            original = _seed_collection(
                service=service,
                sample_user=sample_user,
                delivery_driver=delivery_driver,
                cod_order=cod_order,
                cod_delivery=cod_delivery,
                amount="18000.00",
            )
            _submit_session(delivery_driver, "18000.00")

            replacement = service.adjust_event_amount(
                original.id,
                new_amount=Decimal("10000.00"),
                adjusted_by_user_id=admin_user.id,
                reason="Driver typo - intended 10k",
            )

            db.session.refresh(original)
            assert original.voided_at is not None
            assert original.voided_by_user_id == admin_user.id
            assert original.entry_metadata["adjusted_replacement_event_id"] == replacement.id
            assert original.entry_metadata["adjustment_reason"] == "Driver typo - intended 10k"

            assert replacement.id != original.id
            assert replacement.amount == Decimal("10000.00")
            assert replacement.entry_metadata["original_event_id"] == original.id
            assert replacement.entry_metadata["adjusted_by_user_id"] == admin_user.id
            assert replacement.entry_metadata["original_amount"] == 18000.0

    def test_increase_creates_prepayment_surplus(
        self,
        app,
        db,
        sample_user,
        admin_user,
        delivery_driver,
        delivery_driver_profile,
        cod_order,
        cod_delivery,
    ):
        with app.app_context():
            service = CashCollectionService()
            original = _seed_collection(
                service=service,
                sample_user=sample_user,
                delivery_driver=delivery_driver,
                cod_order=cod_order,
                cod_delivery=cod_delivery,
                amount="18000.00",
            )
            assert original.unapplied_amount == Decimal("0.00")
            _submit_session(delivery_driver, "18000.00")

            replacement = service.adjust_event_amount(
                original.id,
                new_amount=Decimal("25000.00"),
                adjusted_by_user_id=admin_user.id,
                reason="Customer actually paid 25k",
            )

            db.session.refresh(replacement)
            assert replacement.amount == Decimal("25000.00")
            assert replacement.unapplied_amount == Decimal("7000.00")
            assert service.get_customer_prepaid_balance(sample_user.id) == Decimal("7000.00")

    def test_decrease_restores_downstream_payment_after_prepayment_consumed(
        self,
        app,
        db,
        sample_user,
        admin_user,
        delivery_driver,
        delivery_driver_profile,
        cod_order,
        cod_delivery,
        sample_product,
    ):
        with app.app_context():
            service = CashCollectionService()
            existing_payment = service.ensure_cod_payment_for_order(cod_order)

            original = service.post_collection(
                customer_id=sample_user.id,
                amount=Decimal("30000.00"),
                source="delivery_completion",
                collector_user_id=delivery_driver.id,
                recorded_by_user_id=delivery_driver.id,
                order_id=cod_order.id,
                delivery_id=cod_delivery.id,
                notes="Over-collected; surplus is prepayment.",
            )
            db.session.refresh(existing_payment)
            assert existing_payment.outstanding_amount == Decimal("0.00")
            assert original.unapplied_amount == Decimal("12000.00")

            next_order = Order(
                user_id=sample_user.id,
                order_number="ORD-NEXT-PREPAY-001",
                status=OrderStatus.PENDING,
                subtotal=Decimal("9000.00"),
                delivery_fee=Decimal("1000.00"),
                discount_amount=Decimal("0.00"),
                loyalty_discount=Decimal("0.00"),
                total_amount=Decimal("10000.00"),
                payment_method=PaymentMethod.CASH,
                created_at=datetime.now(UTC),
            )
            db.session.add(next_order)
            db.session.flush()
            next_payment = service.ensure_cod_payment_for_order(next_order)
            service.apply_customer_prepaid_credit_to_payment(next_payment)
            db.session.commit()
            db.session.refresh(next_payment)
            assert next_payment.amount_collected == Decimal("10000.00")
            assert next_payment.outstanding_amount == Decimal("0.00")
            _submit_session(delivery_driver, "30000.00")

            service.adjust_event_amount(
                original.id,
                new_amount=Decimal("18000.00"),
                adjusted_by_user_id=admin_user.id,
                reason="Reduced to actual collection",
            )

            db.session.refresh(next_payment)
            db.session.refresh(existing_payment)
            assert existing_payment.outstanding_amount == Decimal("0.00")
            assert next_payment.outstanding_amount == Decimal("10000.00")
            assert next_payment.amount_collected == Decimal("0.00")
            assert service.get_customer_prepaid_balance(sample_user.id) == Decimal("0.00")

    def test_refreshes_session_expected_cash_after_adjustment(
        self,
        app,
        db,
        sample_user,
        admin_user,
        delivery_driver,
        delivery_driver_profile,
        cod_order,
        cod_delivery,
    ):
        with app.app_context():
            service = CashCollectionService()
            original = _seed_collection(
                service=service,
                sample_user=sample_user,
                delivery_driver=delivery_driver,
                cod_order=cod_order,
                cod_delivery=cod_delivery,
                amount="18000.00",
            )
            session_id = original.driver_cash_session_id
            assert session_id is not None
            session = DriverCashSession.query.get(session_id)
            assert session.expected_cash == Decimal("18000.00")
            _submit_session(delivery_driver, "18000.00")

            service.adjust_event_amount(
                original.id,
                new_amount=Decimal("8000.00"),
                adjusted_by_user_id=admin_user.id,
                reason="Corrected after recount",
            )

            db.session.refresh(session)
            assert session.expected_cash == Decimal("8000.00")
            assert session.gross_cash_collected == Decimal("8000.00")

    def test_blocked_on_verified_session(
        self,
        app,
        db,
        sample_user,
        admin_user,
        delivery_driver,
        delivery_driver_profile,
        cod_order,
        cod_delivery,
    ):
        with app.app_context():
            service = CashCollectionService()
            recon_service = DriverReconciliationService()
            original = _seed_collection(
                service=service,
                sample_user=sample_user,
                delivery_driver=delivery_driver,
                cod_order=cod_order,
                cod_delivery=cod_delivery,
                amount="18000.00",
            )

            session_id = original.driver_cash_session_id
            recon_service.submit_session(
                driver_user_id=delivery_driver.id,
                declared_cash=Decimal("18000.00"),
                notes="Driver handoff",
                submitted_by_user_id=delivery_driver.id,
            )
            recon_service.verify_session(
                session_id=session_id,
                verified_cash=Decimal("18000.00"),
                actor_user_id=admin_user.id,
                reason_code="cash_count_matched",
            )
            session = DriverCashSession.query.get(session_id)
            assert session.status == DriverCashSessionStatus.VERIFIED

            with pytest.raises(ValidationError, match="status 'verified'"):
                service.adjust_event_amount(
                    original.id,
                    new_amount=Decimal("10000.00"),
                    adjusted_by_user_id=admin_user.id,
                    reason="Should be blocked",
                )

    def test_allowed_on_mismatch_session(
        self,
        app,
        db,
        sample_user,
        admin_user,
        delivery_driver,
        delivery_driver_profile,
        cod_order,
        cod_delivery,
    ):
        with app.app_context():
            service = CashCollectionService()
            recon_service = DriverReconciliationService()
            original = _seed_collection(
                service=service,
                sample_user=sample_user,
                delivery_driver=delivery_driver,
                cod_order=cod_order,
                cod_delivery=cod_delivery,
                amount="18000.00",
            )

            session_id = original.driver_cash_session_id
            recon_service.submit_session(
                driver_user_id=delivery_driver.id,
                declared_cash=Decimal("18000.00"),
                notes="Driver handoff",
                submitted_by_user_id=delivery_driver.id,
            )
            recon_service.verify_session(
                session_id=session_id,
                verified_cash=Decimal("12000.00"),
                actor_user_id=admin_user.id,
                reason_code="cash_count_short",
                notes="Cash short by 6k.",
            )
            session = DriverCashSession.query.get(session_id)
            assert session.status == DriverCashSessionStatus.MISMATCH

            replacement = service.adjust_event_amount(
                original.id,
                new_amount=Decimal("12000.00"),
                adjusted_by_user_id=admin_user.id,
                reason="Reconciled to verified count",
            )
            assert replacement.amount == Decimal("12000.00")

    def test_requires_reason(
        self,
        app,
        db,
        sample_user,
        admin_user,
        delivery_driver,
        delivery_driver_profile,
        cod_order,
        cod_delivery,
    ):
        with app.app_context():
            service = CashCollectionService()
            original = _seed_collection(
                service=service,
                sample_user=sample_user,
                delivery_driver=delivery_driver,
                cod_order=cod_order,
                cod_delivery=cod_delivery,
                amount="18000.00",
            )

            with pytest.raises(ValidationError, match="reason is required"):
                service.adjust_event_amount(
                    original.id,
                    new_amount=Decimal("10000.00"),
                    adjusted_by_user_id=admin_user.id,
                    reason="   ",
                )

    def test_rejects_negative_amount(
        self,
        app,
        db,
        sample_user,
        admin_user,
        delivery_driver,
        delivery_driver_profile,
        cod_order,
        cod_delivery,
    ):
        # 0 is a valid correction ("no cash collected"); only negatives are rejected.
        with app.app_context():
            service = CashCollectionService()
            original = _seed_collection(
                service=service,
                sample_user=sample_user,
                delivery_driver=delivery_driver,
                cod_order=cod_order,
                cod_delivery=cod_delivery,
                amount="18000.00",
            )

            with pytest.raises(ValidationError, match="cannot be negative"):
                service.adjust_event_amount(
                    original.id,
                    new_amount=Decimal("-1.00"),
                    adjusted_by_user_id=admin_user.id,
                    reason="negative amount",
                )

    def test_cannot_adjust_already_voided_event(
        self,
        app,
        db,
        sample_user,
        admin_user,
        delivery_driver,
        delivery_driver_profile,
        cod_order,
        cod_delivery,
    ):
        with app.app_context():
            service = CashCollectionService()
            original = _seed_collection(
                service=service,
                sample_user=sample_user,
                delivery_driver=delivery_driver,
                cod_order=cod_order,
                cod_delivery=cod_delivery,
                amount="18000.00",
            )
            service.reverse_collection_event(
                original.id,
                reversed_by_user_id=admin_user.id,
                reason="Manual void",
            )

            with pytest.raises(ValidationError, match="voided"):
                service.adjust_event_amount(
                    original.id,
                    new_amount=Decimal("10000.00"),
                    adjusted_by_user_id=admin_user.id,
                    reason="Tried to adjust voided event",
                )

    def test_missing_event_raises_not_found(self, app, admin_user):
        with app.app_context():
            service = CashCollectionService()
            with pytest.raises(NotFoundError):
                service.adjust_event_amount(
                    999999,
                    new_amount=Decimal("5000.00"),
                    adjusted_by_user_id=admin_user.id,
                    reason="Event does not exist",
                )


def test_adjust_default_still_rejects_open_session(
    db, sample_user, delivery_driver, delivery_driver_profile, cod_order, cod_delivery
):
    service = CashCollectionService()
    event = _seed_collection(
        service=service, sample_user=sample_user, delivery_driver=delivery_driver,
        cod_order=cod_order, cod_delivery=cod_delivery, amount="54000",
    )
    # Session is OPEN (no submit). Default guard must still reject — the live
    # reconciliation endpoint relies on this.
    with pytest.raises(ValidationError):
        service.adjust_event_amount(
            event.id, new_amount=Decimal("60000"), adjusted_by_user_id=delivery_driver.id,
            reason="correction attempt",
        )


def test_adjust_allows_open_session_when_status_set_widened(
    db, sample_user, delivery_driver, delivery_driver_profile, cod_order, cod_delivery
):
    service = CashCollectionService()
    event = _seed_collection(
        service=service, sample_user=sample_user, delivery_driver=delivery_driver,
        cod_order=cod_order, cod_delivery=cod_delivery, amount="54000",
    )
    replacement = service.adjust_event_amount(
        event.id, new_amount=Decimal("60000"), adjusted_by_user_id=delivery_driver.id,
        reason="driver actually collected 60k",
        allowed_session_statuses=frozenset({"open", "submitted", "partial", "mismatch", "overdue"}),
    )
    assert Decimal(str(replacement.amount)) == Decimal("60000")
    # Original is voided; replacement is live.
    original = CashCollectionEvent.query.get(event.id)
    assert original.voided_at is not None


def test_adjust_commit_false_does_not_persist_after_rollback(
    db, sample_user, delivery_driver, delivery_driver_profile, cod_order, cod_delivery
):
    # NOTE: audit_logger uses a separate Session(db.engine) that issues its own
    # commit(). With SQLite in-memory + SingletonThreadPool, that separate
    # session shares the same underlying connection as the main session, so its
    # commit() would flush the main session's pending work before we can roll it
    # back — defeating the test intent. We mock audit_logger to prevent that
    # accidental cross-session commit and isolate the commit=False behaviour.
    # This affects audit calls in reverse_collection_event and post_collection
    # (called with commit=False), which fire unconditionally in both methods.
    from unittest.mock import patch
    from business_app import db as _db
    service = CashCollectionService()
    event = _seed_collection(
        service=service, sample_user=sample_user, delivery_driver=delivery_driver,
        cod_order=cod_order, cod_delivery=cod_delivery, amount="54000",
    )
    with patch("business_app.services.cash_collection_service.audit_logger"):
        replacement = service.adjust_event_amount(
            event.id, new_amount=Decimal("60000"), adjusted_by_user_id=delivery_driver.id,
            reason="commit false test", commit=False,
            allowed_session_statuses=frozenset({"open", "submitted", "partial", "mismatch", "overdue"}),
        )
    replacement_id = replacement.id
    _db.session.rollback()
    # With commit=False the change was only flushed; rollback discards it.
    assert CashCollectionEvent.query.get(replacement_id) is None
