"""Regression coverage for ledger-backed admin financial summary reporting."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from business_app import db
from business_app.models.delivery import Delivery
from business_app.models.order import OrderStatus
from business_app.services.admin_report_service import AdminReportService
from business_app.services.cash_collection_service import CashCollectionService
from shared.enums import DeliveryStatus, PaymentMethod
@pytest.mark.unit
def test_financial_summary_uses_cash_collection_ledger_for_cod(app, sample_order, sample_user, delivery_driver):
    with app.app_context():
        sample_order.status = OrderStatus.DELIVERED
        sample_order.payment_method = PaymentMethod.CASH
        delivery = Delivery(
            order_id=sample_order.id,
            delivery_person_id=delivery_driver.id,
            status=DeliveryStatus.DELIVERED,
            scheduled_date=datetime.now(UTC),
            scheduled_time_slot="09:00-12:00",
            actual_delivery_time=datetime.now(UTC),
            delivered_at=datetime.now(UTC),
        )
        db.session.add(delivery)
        db.session.commit()

        cash_service = CashCollectionService()
        cash_service.ensure_cod_payment_for_order(sample_order)
        cash_service.post_collection(
            customer_id=sample_user.id,
            amount=Decimal("5000.00"),
            source="delivery_completion",
            collector_user_id=delivery_driver.id,
            recorded_by_user_id=delivery_driver.id,
            order_id=sample_order.id,
            delivery_id=delivery.id,
            notes="Collected part of the COD balance",
            occurred_at=datetime.now(UTC),
        )

        start_dt = datetime.now(UTC) - timedelta(days=1)
        end_dt = datetime.now(UTC) + timedelta(days=1)
        report = AdminReportService.generate("financial_summary", start_dt, end_dt, {})

        assert report["summary"]["total_cash_collected"] == 5000.0
        assert report["summary"]["total_revenue"] == 5000.0
        assert report["summary"]["delivered_order_revenue"] == float(sample_order.total_amount)
        assert report["summary"]["outstanding_cod_total"] == float(sample_order.total_amount - Decimal("5000.00"))
        assert any(item["method"] == "cash" and item["amount"] == 5000.0 for item in report["payment_method_breakdown"])
        assert report["cash_collection_source_breakdown"][0]["source"] == "delivery_completion"


def _delivered_order_with_payment(sample_order, sample_user, delivery_driver, *, method, status, total, collected):
    """Delivered order + Delivery row + payment, inside the report window.

    The report keys DELIVERED off `Delivery.status`, not `Order.status`, so the
    Delivery row is required for the outstanding query to see the payment.
    """
    from business_app.models.payment import Payment
    from shared.enums import PaymentStatus

    sample_order.status = OrderStatus.DELIVERED
    sample_order.payment_method = method
    sample_order.total_amount = Decimal(str(total))
    delivery = Delivery(
        order_id=sample_order.id,
        delivery_person_id=delivery_driver.id,
        status=DeliveryStatus.DELIVERED,
        scheduled_date=datetime.now(UTC),
        scheduled_time_slot="09:00-12:00",
        actual_delivery_time=datetime.now(UTC),
        delivered_at=datetime.now(UTC),
    )
    db.session.add(delivery)

    payment = sample_order.payment or Payment(
        order_id=sample_order.id,
        user_id=sample_user.id,
        payment_method=method,
        amount=Decimal(str(total)),
        currency="UZS",
        payment_id=f"pay-report-{method.value}-{status.value}",
    )
    payment.payment_method = method
    payment.amount = Decimal(str(total))
    payment.amount_collected = Decimal(str(collected))
    payment.outstanding_amount = Decimal(str(total - collected))
    payment.status = status
    payment.paid_at = datetime.now(UTC)
    if sample_order.payment is None:
        db.session.add(payment)
    db.session.commit()
    return payment


def _report():
    return AdminReportService.generate(
        "financial_summary",
        datetime.now(UTC) - timedelta(days=1),
        datetime.now(UTC) + timedelta(days=1),
        {},
    )


@pytest.mark.unit
class TestRepricedElectronicOrderAccounting:
    """Prod order 961: a Click order edited upward after settlement.

    Money must be counted exactly once and never lost. Before this change the
    reprice flipped the payment COMPLETED -> PARTIALLY_PAID, which deleted the
    already-collected amount from `electronic_total` (it filtered on
    status == COMPLETED) while the unpaid delta never entered
    `outstanding_cod_total` (it filtered on payment_method == CASH). The report
    lost money on BOTH sides of the same order.
    """

    def test_partially_paid_click_keeps_its_collected_amount_in_revenue(
        self, app, sample_order, sample_user, delivery_driver
    ):
        from shared.enums import PaymentStatus

        with app.app_context():
            _delivered_order_with_payment(
                sample_order,
                sample_user,
                delivery_driver,
                method=PaymentMethod.CLICK,
                status=PaymentStatus.PARTIALLY_PAID,
                total=90000,
                collected=60000,
            )
            report = _report()
            assert report["summary"]["total_electronic_collected"] == 60000.0

    def test_partially_paid_click_delta_enters_outstanding(
        self, app, sample_order, sample_user, delivery_driver
    ):
        from shared.enums import PaymentStatus

        with app.app_context():
            _delivered_order_with_payment(
                sample_order,
                sample_user,
                delivery_driver,
                method=PaymentMethod.CLICK,
                status=PaymentStatus.PARTIALLY_PAID,
                total=90000,
                collected=60000,
            )
            report = _report()
            assert report["summary"]["outstanding_cod_total"] == 30000.0
            assert report["summary"]["outstanding_cod_count"] == 1

    def test_settled_click_is_revenue_but_not_outstanding(
        self, app, sample_order, sample_user, delivery_driver
    ):
        from shared.enums import PaymentStatus

        with app.app_context():
            _delivered_order_with_payment(
                sample_order,
                sample_user,
                delivery_driver,
                method=PaymentMethod.CLICK,
                status=PaymentStatus.COMPLETED,
                total=90000,
                collected=90000,
            )
            report = _report()
            assert report["summary"]["total_electronic_collected"] == 90000.0
            assert report["summary"]["outstanding_cod_total"] == 0.0


@pytest.mark.unit
def test_cash_settling_an_electronic_receivable_is_not_counted_twice(
    app, db, sample_order, sample_user, delivery_driver
):
    """Money settled in CASH onto an ELECTRONIC payment belongs to the cash bucket only.

    Settle-in-place raises `payment.amount_collected` on a Click row, and the
    cash figure is built from CashCollectionAllocation rows with no payment-method
    filter — so the same 30,000 would appear in BOTH `total_cash_collected` and
    `total_electronic_collected`, inflating `total_revenue`.

    Plan: docs/superpowers/plans/2026-08-08-open-receivable-ssot.md
    """
    from business_app.services.cash_collection_service import CashCollectionService
    from shared.enums import PaymentStatus

    with app.app_context():
        payment = _delivered_order_with_payment(
            sample_order,
            sample_user,
            delivery_driver,
            method=PaymentMethod.CLICK,
            status=PaymentStatus.PARTIALLY_PAID,
            total=90000,
            collected=60000,
        )

        CashCollectionService().post_collection(
            customer_id=sample_user.id,
            amount=Decimal("30000.00"),
            source="personal_card_transfer",
            recorded_by_user_id=sample_user.id,
            order_id=sample_order.id,
            notes="customer transferred the delta",
        )

        report = _report()
        summary = report["summary"]
        # The gateway took 60,000; the driver/admin took 30,000 in cash.
        assert summary["total_electronic_collected"] == 60000.0
        assert summary["total_cash_collected"] == 30000.0
        assert summary["total_revenue"] == 90000.0


@pytest.mark.unit
def test_refunded_electronic_payment_is_not_counted_as_revenue(
    app, db, sample_order, sample_user, delivery_driver
):
    """🔴 Adversarial-review finding, 2026-08-08.

    Swapping the electronic filter from `status == COMPLETED` to
    `amount_collected > 0` dropped the only guard against refunded/cancelled
    rows: `PaymentService.process_refund` sets status CANCELLED (or
    PARTIALLY_REFUNDED) and never resets `amount_collected`, and leaves
    `paid_at` in place — so a fully refunded card payment kept reporting its
    full value as revenue forever.
    """
    from shared.enums import PaymentStatus

    with app.app_context():
        _delivered_order_with_payment(
            sample_order,
            sample_user,
            delivery_driver,
            method=PaymentMethod.CLICK,
            status=PaymentStatus.CANCELLED,
            total=100000,
            collected=100000,
        )
        report = _report()
        assert report["summary"]["total_electronic_collected"] == 0.0


@pytest.mark.unit
class TestCustomerCreditRebookIsNotCountedTwice:
    """B4a §8: money the business already holds, re-booked as customer credit.

    `total_revenue` is `electronic_total + cash_collected_total`. Those two are
    computed from different sources — `Payment.amount_collected` on one side,
    live `CashCollectionAllocation` rows on the other — and `cash_on_payment`
    only nets allocations landing on the SAME payment. So when a credit event
    born of already-counted money is later allocated to a DIFFERENT payment, the
    same som is counted twice.

    Two flows create such an event, and both are pinned here:

    * `order_cancel_prepaid_credit` — B4a. A cancelled card/Click order's payment
      stays COMPLETED (the owner's rule: the gateway is never reversed), so its
      `amount_collected` keeps counting as electronic revenue; the credit it
      mints must therefore not count again as cash.
    * `order_edit_refund` — `OrderEditService._cascade_cash`. The SAME bug,
      already in production and undetected: an edit-down leaves
      `payment.amount` at the original collected figure on purpose, so the
      original charge is still counted in full.
    """

    def _cod_debt(self, db, user, driver, amount):
        """A delivered COD order carrying an open receivable for the credit to land on."""
        from business_app.models.order import Order
        from business_app.models.payment import Payment
        from shared.enums import PaymentStatus

        order = Order(
            user_id=user.id,
            order_number=f"ORD-CREDIT-SINK-{amount}",
            status=OrderStatus.DELIVERED,
            subtotal=Decimal(str(amount)),
            delivery_fee=Decimal("0.00"),
            total_amount=Decimal(str(amount)),
            payment_method=PaymentMethod.CASH,
        )
        db.session.add(order)
        db.session.flush()
        db.session.add(
            Delivery(
                order_id=order.id,
                delivery_person_id=driver.id,
                status=DeliveryStatus.DELIVERED,
                scheduled_date=datetime.now(UTC),
                scheduled_time_slot="09:00-12:00",
                actual_delivery_time=datetime.now(UTC),
                delivered_at=datetime.now(UTC),
            )
        )
        db.session.add(
            Payment(
                order_id=order.id,
                user_id=user.id,
                payment_method=PaymentMethod.CASH,
                amount=Decimal(str(amount)),
                amount_collected=Decimal("0.00"),
                outstanding_amount=Decimal(str(amount)),
                currency="UZS",
                status=PaymentStatus.PENDING,
                payment_id=f"pay-credit-sink-{amount}",
            )
        )
        db.session.commit()
        return order

    @pytest.mark.parametrize(
        "flow,idempotency_key",
        [
            ("order_cancel_prepaid_credit", "order-cancel-credit:9999"),
            ("order_edit_refund", "order_edit_refund:9999:40000.00"),
        ],
    )
    def test_spending_the_credit_does_not_double_count_it(
        self, app, db, sample_order, sample_user, delivery_driver, flow, idempotency_key
    ):
        from shared.enums import CashCollectionSource, PaymentStatus

        with app.app_context():
            # 40,000 really taken by the gateway, still counted as electronic.
            _delivered_order_with_payment(
                sample_order,
                sample_user,
                delivery_driver,
                method=PaymentMethod.CLICK,
                status=PaymentStatus.COMPLETED,
                total=40000,
                collected=40000,
            )
            # Deliberately LARGER than the credit: a fully-settled COD payment
            # would flip to COMPLETED and trip
            # `ck_payments_cash_completed_requires_collector`, and this test is
            # about the report, not the collector rule.
            self._cod_debt(db, sample_user, delivery_driver, 60000)

            # That same 40,000, re-booked as customer credit and spent on the
            # COD debt. `post_collection` auto-allocates it oldest-first.
            CashCollectionService().post_collection(
                customer_id=sample_user.id,
                amount=Decimal("40000.00"),
                source=CashCollectionSource.BACKFILL,
                notes="re-booked, not new money",
                proof_data={"flow": flow},
                idempotency_key=idempotency_key,
            )

            summary = _report()["summary"]

        assert summary["total_electronic_collected"] == 40000.0, "the gateway money is ours; keep counting it"
        assert summary["total_cash_collected"] == 0.0, (
            "the credit spend is re-booked money, not cash taken in"
        )
        assert summary["total_revenue"] == 40000.0, "40,000 arrived once and must be reported once"

    def test_ordinary_door_cash_is_still_counted(self, app, db, sample_user, delivery_driver):
        """The exclusion must be surgical. A plain door collection carries no
        flow marker and no re-booking idempotency key, and NULL columns must not
        make the exclusion swallow it."""
        from shared.enums import CashCollectionSource

        with app.app_context():
            order = self._cod_debt(db, sample_user, delivery_driver, 12000)
            CashCollectionService().post_collection(
                customer_id=sample_user.id,
                amount=Decimal("12000.00"),
                source=CashCollectionSource.DELIVERY_COMPLETION,
                collector_user_id=delivery_driver.id,
                recorded_by_user_id=delivery_driver.id,
                order_id=order.id,
                delivery_id=order.delivery.id,
                notes="real cash, at the door",
            )
            summary = _report()["summary"]

        assert summary["total_cash_collected"] == 12000.0
        assert summary["total_revenue"] == 12000.0


@pytest.mark.unit
def test_revenue_trend_agrees_with_the_summary_it_sits_under(app, db, sample_user, delivery_driver):
    """I2 — `revenue_trend.daily_collected` is the same total sliced by day.

    It did NOT get the re-booked-credit exclusion that `cash_allocations_query`
    got, one row lower in the same file: the summary netted the credit out and
    the chart beside it still double-counted, including the pre-existing
    `order_edit_refund` leak. Asserting the two agree pins them together rather
    than restating the exclusion a third time.
    """
    from shared.enums import CashCollectionSource, PaymentStatus

    with app.app_context():
        helper = TestCustomerCreditRebookIsNotCountedTwice()
        order = helper._cod_debt(db, sample_user, delivery_driver, 60000)
        CashCollectionService().post_collection(
            customer_id=sample_user.id,
            amount=Decimal("12000.00"),
            source=CashCollectionSource.DELIVERY_COMPLETION,
            collector_user_id=delivery_driver.id,
            recorded_by_user_id=delivery_driver.id,
            order_id=order.id,
            delivery_id=order.delivery.id,
            notes="real cash, at the door",
        )
        CashCollectionService().post_collection(
            customer_id=sample_user.id,
            amount=Decimal("40000.00"),
            source=CashCollectionSource.BACKFILL,
            notes="re-booked, not new money",
            proof_data={"flow": "order_cancel_prepaid_credit"},
            idempotency_key="order-cancel-credit:4242",
        )

        report = _report()

    trend_total = sum(Decimal(str(point["revenue"])) for point in report["revenue_trend"])
    assert float(trend_total) == report["summary"]["total_cash_collected"], (
        "the chart and the summary above it must report the same money"
    )
    assert float(trend_total) == 12000.0
