"""Prepaid COD reservations must net out identically on every surface.

A pending CASH order can hold a ``prepaid_reservation`` allocation: cash the
customer already handed over, parked against this order and consumed at
delivery. Two invariants govern it, and both used to be violated:

1. **One arithmetic everywhere.** "How much can still be collected on this
   payment" is ``open receivable − reserved prepayment``. The driver's screen
   and the customer COD statement netted the reservation; the admin order-detail
   modal, the personal-card-transfer preview/apply and the ring allocator did
   not. So the same order read 90 000 in one place and 85 000 in another, and
   the surfaces that MOVE money used the larger figure.

2. **A reservation can never be consumed into thin air.** Consumption used to
   add the reserved amount to ``amount_collected`` uncapped, after which
   ``sync_payment_projection`` clamped it back to ``payment.amount``. Any
   reservation that no longer fit — because the payment was settled from another
   source first, or the order was edited down — was silently destroyed: the
   allocation was stamped applied, the funding event's ``unapplied_amount`` was
   never restored, and the customer's credit simply vanished.

Prod case: order AD_000630_26 (order 1028 / payment 1132), a 90 000 COD order
carrying a 5 000 reservation. Recording the customer's 100 000 card transfer
against it destroyed the 5 000.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from business_app.models.order import Order
from business_app.models.payment import CashCollectionAllocation, CashCollectionEvent
from business_app.services.cash_collection_service import CashCollectionService
from business_app.utils.payment_projection import (
    get_payment_projection,
    net_open_receivable_amount,
    reserved_prepayment_amount,
)
from shared.enums import OrderStatus, PaymentMethod


def _make_pending_cash_order(db, user, *, order_number: str, total: str) -> Order:
    order = Order(
        user_id=user.id,
        order_number=order_number,
        status=OrderStatus.CONFIRMED,
        subtotal=Decimal(total),
        delivery_fee=Decimal("0.00"),
        discount_amount=Decimal("0.00"),
        loyalty_discount=Decimal("0.00"),
        total_amount=Decimal(total),
        payment_method=PaymentMethod.CASH,
        created_at=datetime.now(UTC),
    )
    db.session.add(order)
    db.session.flush()
    return order


def _seed_prepaid(db, customer, collector, amount: str) -> CashCollectionEvent:
    """Seed an unapplied COD prepayment surplus collected by ``collector``."""
    event = CashCollectionEvent(
        customer_id=customer.id,
        collector_user_id=collector.id,
        recorded_by_user_id=collector.id,
        amount=Decimal(amount),
        currency="UZS",
        source="standalone_meeting",
        occurred_at=datetime.now(UTC),
        notes="Seeded prepayment surplus",
        unapplied_amount=Decimal(amount),
    )
    db.session.add(event)
    db.session.flush()
    return event


def _reserved_order(db, service, user, driver, *, order_number, total, credit):
    """A pending CASH order carrying a partial prepaid reservation."""
    event = _seed_prepaid(db, user, driver, credit)
    order = _make_pending_cash_order(db, user, order_number=order_number, total=total)
    payment = service.ensure_cod_payment_for_order(order)
    db.session.flush()
    service.reserve_customer_prepaid_credit_for_payment(payment, actor_user_id=user.id)
    db.session.flush()
    # Order creation and every later settlement are separate transactions in
    # prod; expire so `order.payment` resolves like a fresh request would.
    db.session.expire(order)
    return order, payment, event


def _live_applied_total(payment_id: int) -> Decimal:
    """Sum of live allocations that claim to affect the payment projection."""
    total = Decimal("0.00")
    for row in CashCollectionAllocation.query.filter_by(payment_id=payment_id, reversed_at=None).all():
        meta = row.allocation_metadata or {}
        if meta.get("affects_payment_projection", row.allocation_mode != "prepaid_reservation"):
            total += Decimal(str(row.allocated_amount))
    return total


@pytest.mark.unit
@pytest.mark.payment
class TestNetReceivableIsOneArithmetic:
    def test_helper_nets_reservation_off_the_open_receivable(self, app, db, sample_user, delivery_driver):
        with app.app_context():
            service = CashCollectionService()
            _order, payment, _event = _reserved_order(
                db, service, sample_user, delivery_driver,
                order_number="NET-HELPER", total="90000.00", credit="5000.00",
            )

            assert reserved_prepayment_amount(payment) == Decimal("5000.00")
            assert net_open_receivable_amount(payment) == Decimal("85000.00")

    def test_helper_never_reports_more_reserved_than_is_owed(self, app, db, sample_user, delivery_driver):
        """A reservation stranded above the live receivable must not go negative."""
        with app.app_context():
            service = CashCollectionService()
            _order, payment, _event = _reserved_order(
                db, service, sample_user, delivery_driver,
                order_number="NET-CLAMP", total="90000.00", credit="5000.00",
            )
            payment.amount = Decimal("3000.00")
            db.session.flush()

            assert reserved_prepayment_amount(payment) == Decimal("3000.00")
            assert net_open_receivable_amount(payment) == Decimal("0.00")

    def test_order_detail_projection_carries_the_same_net_figure(self, app, db, sample_user, delivery_driver):
        """The admin order modal reads this projection; it must agree with the
        driver screen and the COD statement rather than quoting the gross."""
        with app.app_context():
            service = CashCollectionService()
            _order, payment, _event = _reserved_order(
                db, service, sample_user, delivery_driver,
                order_number="NET-PROJECTION", total="90000.00", credit="5000.00",
            )

            projection = get_payment_projection(payment)

            assert projection["outstanding_amount"] == Decimal("90000.00")
            assert projection["reserved_prepayment_amount"] == Decimal("5000.00")
            assert projection["net_outstanding_amount"] == Decimal("85000.00")

    def test_statement_and_driver_screen_agree_with_the_helper(self, app, db, sample_user, delivery_driver):
        from business_app.services.staff_service import StaffService

        with app.app_context():
            service = CashCollectionService()
            order, payment, _event = _reserved_order(
                db, service, sample_user, delivery_driver,
                order_number="NET-AGREE", total="90000.00", credit="5000.00",
            )

            projection = StaffService.get_cod_collection_projection(order)
            statement = service.get_customer_cod_statement(sample_user.id)
            row = next(item for item in statement["items"] if item["payment_id"] == payment.id)

            expected = float(net_open_receivable_amount(payment))
            assert projection["expected_cash_to_collect"] == expected
            assert row["net_outstanding_amount"] == expected


@pytest.mark.unit
@pytest.mark.payment
class TestReservationIsNeverConsumedIntoThinAir:
    def test_consume_caps_at_live_receivable_and_refunds_the_remainder(
        self, app, db, sample_user, delivery_driver
    ):
        """Payment already settled elsewhere: nothing to consume, credit returns."""
        with app.app_context():
            service = CashCollectionService()
            _order, payment, event = _reserved_order(
                db, service, sample_user, delivery_driver,
                order_number="CAP-SETTLED", total="90000.00", credit="5000.00",
            )
            # Someone else settles the payment in full first.
            payment.amount_collected = Decimal("90000.00")
            service.sync_payment_projection(payment, collected_by=delivery_driver.id)
            db.session.flush()

            consumed = service.consume_reserved_prepayment_for_payment(
                payment, collected_by=delivery_driver.id
            )
            db.session.flush()
            db.session.refresh(payment)
            db.session.refresh(event)

            assert consumed == Decimal("0.00")
            assert payment.amount_collected == Decimal("90000.00")
            # The 5 000 went back to the customer instead of evaporating.
            assert event.unapplied_amount == Decimal("5000.00")
            assert service.get_customer_prepaid_balance(sample_user.id) == Decimal("5000.00")
            assert payment.provider_data.get("cod_prepayment_reserved_amount") == 0.0

    def test_consume_takes_the_part_that_fits_and_refunds_the_rest(
        self, app, db, sample_user, delivery_driver
    ):
        """Order edited down below the reservation: consume 3 000, refund 2 000."""
        with app.app_context():
            service = CashCollectionService()
            _order, payment, event = _reserved_order(
                db, service, sample_user, delivery_driver,
                order_number="CAP-PARTIAL", total="90000.00", credit="5000.00",
            )
            payment.amount = Decimal("3000.00")
            service.sync_payment_projection(payment)
            db.session.flush()

            consumed = service.consume_reserved_prepayment_for_payment(
                payment, collected_by=delivery_driver.id
            )
            db.session.flush()
            db.session.refresh(payment)
            db.session.refresh(event)

            assert consumed == Decimal("3000.00")
            assert payment.amount_collected == Decimal("3000.00")
            assert payment.outstanding_amount == Decimal("0.00")
            assert _live_applied_total(payment.id) == Decimal("3000.00")
            assert event.unapplied_amount == Decimal("2000.00")

    def test_full_coverage_consume_is_unchanged(self, app, db, sample_user, delivery_driver):
        """The happy path must not regress: a reservation that fits is consumed whole."""
        with app.app_context():
            service = CashCollectionService()
            _order, payment, event = _reserved_order(
                db, service, sample_user, delivery_driver,
                order_number="CAP-HAPPY", total="90000.00", credit="5000.00",
            )

            consumed = service.consume_reserved_prepayment_for_payment(
                payment, collected_by=delivery_driver.id
            )
            db.session.flush()
            db.session.refresh(payment)
            db.session.refresh(event)

            assert consumed == Decimal("5000.00")
            assert payment.amount_collected == Decimal("5000.00")
            assert payment.outstanding_amount == Decimal("85000.00")
            assert event.unapplied_amount == Decimal("0.00")
            allocation = CashCollectionAllocation.query.filter_by(payment_id=payment.id, reversed_at=None).one()
            assert allocation.allocation_mode == "prepaid_credit"


@pytest.mark.unit
@pytest.mark.payment
class TestPersonalCardTransferRespectsTheReservation:
    def test_preview_quotes_the_net_the_customer_actually_owes(self, app, db, sample_user, delivery_driver):
        with app.app_context():
            service = CashCollectionService()
            order, _payment, _event = _reserved_order(
                db, service, sample_user, delivery_driver,
                order_number="PCT-PREVIEW", total="90000.00", credit="5000.00",
            )

            plan = service.preview_personal_card_transfer(order_id=order.id, amount=Decimal("100000.00"))

            assert plan.order_outstanding_before == Decimal("85000.00")
            assert plan.applied_to_order == Decimal("85000.00")
            assert plan.order_outstanding_after == Decimal("0.00")
            assert plan.remaining_as_credit == Decimal("15000.00")

    def test_transfer_settles_the_net_and_preserves_the_customers_credit(
        self, app, db, sample_user, delivery_driver
    ):
        """The prod case end to end: 5 000 credit + 100 000 card on a 90 000 order."""
        with app.app_context():
            service = CashCollectionService()
            order, payment, _event = _reserved_order(
                db, service, sample_user, delivery_driver,
                order_number="PCT-APPLY", total="90000.00", credit="5000.00",
            )
            db.session.expire_all()

            service.post_collection(
                customer_id=sample_user.id,
                amount=Decimal("100000.00"),
                source="personal_card_transfer",
                recorded_by_user_id=delivery_driver.id,
                order_id=order.id,
                notes="karta",
                commit=False,
            )
            db.session.flush()
            db.session.refresh(payment)

            assert payment.amount_collected == Decimal("85000.00")
            assert payment.outstanding_amount == Decimal("5000.00")

            # Delivery consumes the still-intact reservation.
            consumed = service.consume_reserved_prepayment_for_payment(
                payment, collected_by=delivery_driver.id
            )
            db.session.flush()
            db.session.refresh(payment)

            assert consumed == Decimal("5000.00")
            assert payment.amount_collected == Decimal("90000.00")
            assert payment.outstanding_amount == Decimal("0.00")
            assert _live_applied_total(payment.id) == Decimal("90000.00")
            # 5 000 + 100 000 in, 90 000 consumed by the order.
            assert service.get_customer_prepaid_balance(sample_user.id) == Decimal("15000.00")


@pytest.mark.unit
@pytest.mark.payment
class TestSpillCannotReabsorbTheReservedSlice:
    def test_transfer_residual_leaves_the_reservation_intact(self, app, db, sample_user, delivery_driver):
        """A card transfer settles its target first, then spills the residual
        through the ring allocator. If the ring quoted the GROSS receivable the
        residual would immediately refill the 5 000 the target-first step just
        left for the reservation — undoing the fix one line later."""
        with app.app_context():
            service = CashCollectionService()
            order, payment, _event = _reserved_order(
                db, service, sample_user, delivery_driver,
                order_number="SPILL-GUARD", total="90000.00", credit="5000.00",
            )
            db.session.expire_all()

            service.post_collection(
                customer_id=sample_user.id,
                amount=Decimal("100000.00"),
                source="personal_card_transfer",
                recorded_by_user_id=delivery_driver.id,
                order_id=order.id,
                notes="karta",
                commit=False,
            )
            db.session.flush()
            db.session.refresh(payment)

            assert payment.amount_collected == Decimal("85000.00")
            assert reserved_prepayment_amount(payment) == Decimal("5000.00")
            assert net_open_receivable_amount(payment) == Decimal("0.00")
            reservation = CashCollectionAllocation.query.filter_by(
                payment_id=payment.id, allocation_mode="prepaid_reservation", reversed_at=None
            ).one()
            assert reservation.allocated_amount == Decimal("5000.00")


@pytest.mark.unit
@pytest.mark.payment
class TestDoorCollectionAfterDeliveryStillConserves:
    def test_gross_collection_after_delivery_consumed_the_reservation(
        self, app, db, sample_user, delivery_driver
    ):
        """The real driver ordering: DELIVERED consumes the reservation first,
        then the door cash posts. Collecting the gross 90 000 when only 85 000
        was due must leave 5 000 of credit, not silently absorb it."""
        from business_app.models.delivery import Delivery
        from shared.enums import DeliveryStatus

        with app.app_context():
            service = CashCollectionService()
            order, payment, _event = _reserved_order(
                db, service, sample_user, delivery_driver,
                order_number="DOOR-GROSS", total="90000.00", credit="5000.00",
            )
            delivery = Delivery(
                order_id=order.id,
                delivery_person_id=delivery_driver.id,
                status=DeliveryStatus.IN_TRANSIT,
                scheduled_date=datetime.now(UTC),
                scheduled_time_slot="09:00-12:00",
            )
            db.session.add(delivery)
            db.session.flush()

            service.consume_reserved_prepayment_for_payment(payment, collected_by=delivery_driver.id)
            order.status = OrderStatus.DELIVERED
            db.session.flush()
            db.session.expire_all()

            service.post_collection(
                customer_id=sample_user.id,
                amount=Decimal("90000.00"),
                source="delivery_completion",
                collector_user_id=delivery_driver.id,
                order_id=order.id,
                delivery_id=delivery.id,
                notes="collected at door",
                commit=False,
            )
            db.session.flush()
            db.session.refresh(payment)

            assert payment.amount_collected == Decimal("90000.00")
            assert payment.outstanding_amount == Decimal("0.00")
            assert _live_applied_total(payment.id) == Decimal("90000.00")
            # 5 000 prepaid + 90 000 at the door − 90 000 order = 5 000 credit.
            assert service.get_customer_prepaid_balance(sample_user.id) == Decimal("5000.00")
