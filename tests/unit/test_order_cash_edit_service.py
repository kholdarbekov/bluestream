from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from business_app import db
from business_app.models.delivery import Delivery, DeliveryPerson
from business_app.models.order import Order
from business_app.models.payment import DriverCashSession, Payment
from business_app.services.cash_collection_service import CashCollectionService
from business_app.services.driver_reconciliation_service import DriverReconciliationService
from business_app.services.order_cash_edit_service import OrderCashEditService
from business_app.utils.exceptions import ConflictError, ValidationError
from shared.enums import DeliveryStatus, OrderStatus, PaymentMethod


@pytest.fixture
def driver_profile(db, delivery_driver):
    profile = DeliveryPerson(
        user_id=delivery_driver.id, full_name="Driver", phone=delivery_driver.phone,
        email=delivery_driver.email, is_active=True, is_available=True,
    )
    db.session.add(profile)
    db.session.commit()
    return profile


@pytest.fixture
def delivered_cod(db, sample_order, delivery_driver):
    sample_order.status = OrderStatus.DELIVERED
    sample_order.payment_method = PaymentMethod.CASH
    sample_order.total_amount = Decimal("54000")
    sample_order.paid_at = datetime.now(timezone.utc)
    delivery = Delivery(
        order_id=sample_order.id, delivery_person_id=delivery_driver.id,
        status=DeliveryStatus.DELIVERED, scheduled_date=datetime.now(timezone.utc),
        scheduled_time_slot="09:00-12:00", actual_delivery_time=datetime.now(timezone.utc),
    )
    db.session.add(delivery)
    db.session.commit()
    return sample_order, delivery


def _seed(service, sample_user, delivery_driver, order, delivery, amount):
    service.ensure_cod_payment_for_order(order)
    return service.post_collection(
        customer_id=sample_user.id, amount=Decimal(amount), source="delivery_completion",
        collector_user_id=delivery_driver.id, recorded_by_user_id=delivery_driver.id,
        order_id=order.id, delivery_id=delivery.id, notes="seed",
    )


def test_preview_happy_upward(db, sample_user, delivery_driver, driver_profile, delivered_cod):
    order, delivery = delivered_cod
    svc = CashCollectionService()
    _seed(svc, sample_user, delivery_driver, order, delivery, "54000")
    plan = OrderCashEditService().preview(order_id=order.id, new_amount="60000")
    assert plan.is_editable
    assert plan.current_collected == Decimal("54000")
    assert plan.new_amount == Decimal("60000")
    assert plan.order_total == Decimal("54000")
    assert plan.applied_to_order == Decimal("54000")
    assert plan.projected_outstanding == Decimal("0")
    assert plan.projected_payment_status == "completed"
    assert plan.customer_credit_delta == Decimal("6000")


def test_preview_blocks_outside_window(db, sample_user, delivery_driver, driver_profile, delivered_cod):
    order, delivery = delivered_cod
    delivery.actual_delivery_time = datetime.now(timezone.utc) - timedelta(hours=73)
    order.paid_at = delivery.actual_delivery_time
    db.session.commit()
    svc = CashCollectionService()
    _seed(svc, sample_user, delivery_driver, order, delivery, "54000")
    plan = OrderCashEditService().preview(order_id=order.id, new_amount="60000")
    assert not plan.is_editable
    assert any(r.startswith("cash_edit_window_expired") for r in plan.blocking_reasons)


def test_preview_blocks_when_no_cash_event(db, sample_user, delivery_driver, driver_profile, delivered_cod):
    order, _ = delivered_cod
    plan = OrderCashEditService().preview(order_id=order.id, new_amount="60000")
    assert "no_cash_collection_recorded" in plan.blocking_reasons


def test_preview_downward_warns_below_total(db, sample_user, delivery_driver, driver_profile, delivered_cod):
    order, delivery = delivered_cod
    svc = CashCollectionService()
    _seed(svc, sample_user, delivery_driver, order, delivery, "54000")
    plan = OrderCashEditService().preview(order_id=order.id, new_amount="50000")
    assert plan.is_editable
    assert plan.projected_payment_status == "partially_paid"
    assert any("collected_below_order_total" in w for w in plan.warnings)


def test_preview_flags_reopen_for_verified_session(
    db, sample_user, delivery_driver, driver_profile, delivered_cod
):
    order, delivery = delivered_cod
    svc = CashCollectionService()
    event = _seed(svc, sample_user, delivery_driver, order, delivery, "54000")
    session = DriverCashSession.query.get(event.driver_cash_session_id)
    session.status = "verified"
    db.session.commit()
    plan = OrderCashEditService().preview(order_id=order.id, new_amount="60000")
    assert plan.session_will_reopen is True
    assert plan.is_editable  # no conflicting active session


def test_apply_upward_settles_order_and_credits_surplus(
    db, sample_user, delivery_driver, driver_profile, delivered_cod
):
    order, delivery = delivered_cod
    svc = CashCollectionService()
    _seed(svc, sample_user, delivery_driver, order, delivery, "54000")

    result = OrderCashEditService().apply_edit(
        order_id=order.id, new_amount="60000", reason="driver collected 60k",
        actor_user_id=delivery_driver.id,
    )

    payment = Payment.query.filter_by(order_id=order.id).first()
    assert Decimal(str(payment.amount_collected)) == Decimal("54000")  # order settled at its total
    assert Decimal(str(payment.outstanding_amount)) == Decimal("0.00")
    # 6k surplus becomes the customer's prepaid balance.
    assert svc.get_customer_prepaid_balance(sample_user.id) == Decimal("6000")
    assert result.replacement_event_id is not None


def test_apply_downward_leaves_outstanding(
    db, sample_user, delivery_driver, driver_profile, delivered_cod
):
    order, delivery = delivered_cod
    svc = CashCollectionService()
    _seed(svc, sample_user, delivery_driver, order, delivery, "54000")

    OrderCashEditService().apply_edit(
        order_id=order.id, new_amount="50000", reason="driver only collected 50k",
        actor_user_id=delivery_driver.id,
    )
    payment = Payment.query.filter_by(order_id=order.id).first()
    assert Decimal(str(payment.amount_collected)) == Decimal("50000")
    assert Decimal(str(payment.outstanding_amount)) == Decimal("4000")


def test_apply_rejects_when_blocking(db, sample_user, delivery_driver, driver_profile, delivered_cod):
    order, _ = delivered_cod  # no cash event seeded
    with pytest.raises(ValidationError):
        OrderCashEditService().apply_edit(
            order_id=order.id, new_amount="60000", reason="should fail",
            actor_user_id=delivery_driver.id,
        )


def test_apply_requires_min_reason(db, sample_user, delivery_driver, driver_profile, delivered_cod):
    order, delivery = delivered_cod
    svc = CashCollectionService()
    _seed(svc, sample_user, delivery_driver, order, delivery, "54000")
    with pytest.raises(ValidationError):
        OrderCashEditService().apply_edit(
            order_id=order.id, new_amount="60000", reason="bad", actor_user_id=delivery_driver.id,
        )


def _make_session_verified(event):
    session = DriverCashSession.query.get(event.driver_cash_session_id)
    session.status = "verified"
    db.session.commit()
    return session


def test_apply_reopens_verified_session_then_adjusts(
    db, sample_user, delivery_driver, driver_profile, delivered_cod
):
    order, delivery = delivered_cod
    svc = CashCollectionService()
    event = _seed(svc, sample_user, delivery_driver, order, delivery, "54000")
    session = _make_session_verified(event)

    result = OrderCashEditService().apply_edit(
        order_id=order.id, new_amount="60000", reason="correct after verification",
        actor_user_id=delivery_driver.id,
    )

    reopened = DriverCashSession.query.get(session.id)
    assert getattr(reopened.status, "value", reopened.status) == "open"
    assert reopened.verified_at is None  # verification trail cleared
    # Driver re-submit notification queued post-commit.
    assert any(name == "notify_driver_session_reopened" for name, _a, _k in result.post_commit_dispatch)
    payment = Payment.query.filter_by(order_id=order.id).first()
    assert Decimal(str(payment.amount_collected)) == Decimal("54000")
    assert svc.get_customer_prepaid_balance(sample_user.id) == Decimal("6000")


def test_apply_blocked_when_driver_has_another_active_session(
    db, sample_user, delivery_driver, driver_profile, delivered_cod
):
    order, delivery = delivered_cod
    svc = CashCollectionService()
    event = _seed(svc, sample_user, delivery_driver, order, delivery, "54000")
    _make_session_verified(event)
    # Driver opens a *new* active session (conflicts with reopening the old one).
    DriverReconciliationService().get_or_create_session(driver_user_id=delivery_driver.id)
    db.session.commit()

    # Preview surfaces the conflict as a blocking reason → apply raises ValidationError.
    plan = OrderCashEditService().preview(order_id=order.id, new_amount="60000")
    assert any("cash_session_active_conflict" in r for r in plan.blocking_reasons)
    with pytest.raises(ValidationError):
        OrderCashEditService().apply_edit(
            order_id=order.id, new_amount="60000", reason="blocked by active session",
            actor_user_id=delivery_driver.id,
        )


def test_preview_warns_when_customer_has_other_unpaid_cod_orders(
    db, sample_user, delivery_driver, driver_profile, delivered_cod
):
    """When the customer has another outstanding COD payment, preview must include a multi-debt warning."""
    order, delivery = delivered_cod
    svc = CashCollectionService()
    _seed(svc, sample_user, delivery_driver, order, delivery, "54000")

    # Seed a second delivered COD order with an outstanding payment for the same customer.
    older_order = Order(
        user_id=sample_user.id,
        order_number="ORD-OLDER-COD-001",
        status=OrderStatus.DELIVERED,
        subtotal=Decimal("20000"),
        delivery_fee=Decimal("0"),
        discount_amount=Decimal("0"),
        loyalty_discount=Decimal("0"),
        total_amount=Decimal("20000"),
        payment_method=PaymentMethod.CASH,
        created_at=datetime.now(timezone.utc) - timedelta(days=2),
    )
    db.session.add(older_order)
    db.session.flush()
    older_payment = Payment(
        order_id=older_order.id,
        user_id=sample_user.id,
        payment_method=PaymentMethod.CASH,
        amount=Decimal("20000"),
        outstanding_amount=Decimal("20000"),
        currency="UZS",
        status="pending",
    )
    db.session.add(older_payment)
    db.session.commit()

    plan = OrderCashEditService().preview(order_id=order.id, new_amount="60000")

    assert plan.is_editable
    assert any(w.startswith("customer_has_other_unpaid_cod_orders") for w in plan.warnings)


def test_preview_no_multi_debt_warning_for_single_order_customer(
    db, sample_user, delivery_driver, driver_profile, delivered_cod
):
    """When the customer has only this one COD order the multi-debt warning must NOT appear."""
    order, delivery = delivered_cod
    svc = CashCollectionService()
    _seed(svc, sample_user, delivery_driver, order, delivery, "54000")

    plan = OrderCashEditService().preview(order_id=order.id, new_amount="60000")

    assert plan.is_editable
    assert not any(w.startswith("customer_has_other_unpaid_cod_orders") for w in plan.warnings)


@pytest.fixture
def card_settled_order(db, sample_user, delivery_driver, driver_profile, delivered_cod):
    """Order already settled by a personal card transfer, with no cash recorded by the driver.

    Mirrors prod TG_000251_26: the driver logged 0 cash ("Karta"), an admin then booked the
    90k as a personal card transfer, so the payment already sits at outstanding 0 before
    anyone opens the collected-cash edit.
    """
    order, delivery = delivered_cod
    order.total_amount = Decimal("90000")
    db.session.commit()
    svc = CashCollectionService()
    svc.ensure_cod_payment_for_order(order)
    svc.post_collection(
        customer_id=sample_user.id, amount=Decimal("0"), source="delivery_completion",
        collector_user_id=delivery_driver.id, recorded_by_user_id=delivery_driver.id,
        order_id=order.id, delivery_id=delivery.id, notes="Karta",
    )
    svc.post_collection(
        customer_id=sample_user.id, amount=Decimal("90000"), source="personal_card_transfer",
        recorded_by_user_id=sample_user.id, order_id=order.id, notes="tolandi",
    )
    return order, delivery


def test_preview_credits_full_amount_when_order_settled_elsewhere(
    db, sample_user, delivery_driver, driver_profile, card_settled_order
):
    """Nothing is left to pay, so the whole entry becomes credit — preview must say so.

    Prod TG_000251_26 promised "+10,000 credit" (100k - 90k order total) while the
    allocator, seeing outstanding 0, credited the full 100k.
    """
    order, _ = card_settled_order
    plan = OrderCashEditService().preview(order_id=order.id, new_amount="100000")
    assert plan.customer_credit_delta == Decimal("100000")
    assert plan.applied_to_order == Decimal("0")


def test_preview_warns_when_order_settled_elsewhere(
    db, sample_user, delivery_driver, driver_profile, card_settled_order
):
    order, _ = card_settled_order
    plan = OrderCashEditService().preview(order_id=order.id, new_amount="100000")
    assert any(w.startswith("order_already_settled_by_other_source") for w in plan.warnings)


def test_preview_keeps_completed_status_when_order_settled_elsewhere(
    db, sample_user, delivery_driver, driver_profile, card_settled_order
):
    """Booking the 10k actually collected must not read as an 80k shortfall on a paid order."""
    order, _ = card_settled_order
    plan = OrderCashEditService().preview(order_id=order.id, new_amount="10000")
    assert plan.projected_outstanding == Decimal("0")
    assert plan.projected_payment_status == "completed"
    assert plan.customer_credit_delta == Decimal("10000")
    assert not any("collected_below_order_total" in w for w in plan.warnings)


def test_preview_reports_amount_applied_to_order_for_normal_overcollection(
    db, sample_user, delivery_driver, driver_profile, delivered_cod
):
    """Regression: the ordinary surplus case still settles the order and credits only the excess."""
    order, delivery = delivered_cod
    svc = CashCollectionService()
    _seed(svc, sample_user, delivery_driver, order, delivery, "54000")

    plan = OrderCashEditService().preview(order_id=order.id, new_amount="60000")

    assert plan.applied_to_order == Decimal("54000")
    assert plan.customer_credit_delta == Decimal("6000")
    assert not any(w.startswith("order_already_settled_by_other_source") for w in plan.warnings)


def test_preview_summary_drops_order_total_delta_when_order_settled_elsewhere(
    db, sample_user, delivery_driver, driver_profile, card_settled_order
):
    """Comparing the entry against the order total is meaningless once the payment is
    settled from another source — it reads as a shortfall on an already-paid order, so the
    summary must not carry it at all."""
    order, _ = card_settled_order
    summary = OrderCashEditService().preview(order_id=order.id, new_amount="12000").to_summary()
    assert "surplus_or_shortfall" not in summary
    assert summary["applied_to_order"] == 0.0
    assert summary["projected_outstanding"] == 0.0
    assert summary["customer_credit_delta"] == 12000.0


def test_edit_metadata_exposes_event_amount_not_payment_collected(
    db, sample_user, delivery_driver, driver_profile, card_settled_order
):
    """The form must seed from the event being adjusted, not from payment.amount_collected,
    which here is funded by the card transfer rather than by cash."""
    order, _ = card_settled_order
    meta = OrderCashEditService().get_edit_metadata(order)
    assert meta["collected_cash_event_amount"] == 0.0
    payment = Payment.query.filter_by(order_id=order.id).first()
    assert Decimal(str(payment.amount_collected)) == Decimal("90000")


def test_apply_on_card_settled_order_credits_only_the_cash_collected(
    db, sample_user, delivery_driver, driver_profile, card_settled_order
):
    """Remediation path: booking the true 10k leaves the card settlement intact."""
    order, _ = card_settled_order
    svc = CashCollectionService()

    OrderCashEditService().apply_edit(
        order_id=order.id, new_amount="10000", reason="driver collected only 10k cash",
        actor_user_id=delivery_driver.id,
    )

    payment = Payment.query.filter_by(order_id=order.id).first()
    assert Decimal(str(payment.amount_collected)) == Decimal("90000")  # card transfer untouched
    assert Decimal(str(payment.outstanding_amount)) == Decimal("0.00")
    assert svc.get_customer_prepaid_balance(sample_user.id) == Decimal("10000")
