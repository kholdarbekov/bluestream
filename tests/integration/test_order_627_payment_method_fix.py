"""Headline reproduction of the order-627 payment-method fix (T1, cash collected).

Order 627: a workplace customer's DELIVERED cash order whose corporate prepaid
units were ALREADY reserved+consumed AND whose 72 000 was collected on delivery
(a live DELIVERY_COMPLETION cash event fully allocated to the order's payment).

Admin re-classifies it to business_account. The money end-state contract:
  * order + payment read business_account, order is paid / payment COMPLETED;
  * corporate units are consumed EXACTLY once (no double consume);
  * the previously-collected 72 000 becomes the customer's prepaid credit
    (get_customer_prepaid_balance increases by exactly the collected amount).

This is the only COD the customer has, so the whole 72 000 lands as credit.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from business_app import db as _db
from business_app.models.corporate import (
    CorporateContract,
    CorporateContractProductPrice,
    CorporateContractStatus,
    CorporatePrepaymentAccount,
    CorporatePrepaymentBalance,
    CorporatePrepaymentEventType,
    CorporatePrepaymentLedger,
)
from business_app.models.order import Order, OrderItem
from business_app.models.payment import (
    CashCollectionAllocation,
    CashCollectionEvent,
    Payment,
)
from business_app.models.user import User
from business_app.services.cash_collection_service import CashCollectionService
from business_app.services.order_payment_method_edit_service import (
    OrderPaymentMethodEditService,
)
from shared.enums import (
    CashCollectionSource,
    CorporateContractTrackingMode,
    EntitySubtype,
    OrderStatus,
    PaymentMethod,
    PaymentStatus,
    UserRole,
    UserType,
)

UNIT_PRICE = Decimal("18000.00")
QUANTITY = 4
COLLECTED = Decimal("72000.00")  # 4 units * 18 000


@pytest.fixture
def workplace_user(db):
    user = User(
        email=f"wp627-{uuid4().hex[:8]}@example.com",
        phone=f"+99895{uuid4().int % 10000000:07d}",
        password_hash="x" * 60,
        first_name="Order",
        last_name="627",
        user_type=UserType.ENTITY,
        entity_subtype=EntitySubtype.WORKPLACE,
        company_name="Workplace 627",
        role=UserRole.CUSTOMER,
        is_verified=True,
    )
    db.session.add(user)
    db.session.commit()
    return user


@pytest.fixture
def driver(db):
    user = User(
        email=f"drv627-{uuid4().hex[:8]}@example.com",
        phone=f"+99890{uuid4().int % 10000000:07d}",
        password_hash="x" * 60,
        first_name="Cash",
        last_name="Driver",
        user_type=UserType.STAFF,
        role=UserRole.DELIVERY_DRIVER,
        is_verified=True,
    )
    db.session.add(user)
    db.session.commit()
    return user


def _seed_order_627(db, workplace_user, sample_product, driver):
    """Reproduce order 627's exact DB state and return (order, contract, balance)."""
    contract = CorporateContract(
        user_id=workplace_user.id,
        contract_number=f"C627-{uuid4().hex[:10]}",
        name="Order 627 Contract",
        status=CorporateContractStatus.ACTIVE,
        start_date=datetime.now(timezone.utc) - timedelta(days=30),
        currency="UZS",
        is_active=True,
        tracking_mode=CorporateContractTrackingMode.UNITS,
    )
    db.session.add(contract)
    db.session.flush()
    price_row = CorporateContractProductPrice(
        contract_id=contract.id,
        product_id=sample_product.id,
        unit_price=UNIT_PRICE,
        is_prepayment_eligible=True,
        is_active=True,
    )
    db.session.add(price_row)
    account = CorporatePrepaymentAccount(contract_id=contract.id, is_active=True)
    db.session.add(account)
    db.session.flush()
    # prepaid 41 / consumed 37 → 4 units of headroom, exactly this order's need.
    balance = CorporatePrepaymentBalance(
        account_id=account.id,
        product_id=sample_product.id,
        prepaid_units=Decimal("41.00"),
        reserved_units=Decimal("0.00"),
        consumed_units=Decimal("37.00"),
        is_active=True,
    )
    db.session.add(balance)
    db.session.flush()

    order = Order(
        user_id=workplace_user.id,
        order_number=f"ORD-627-{uuid4().hex[:8]}",
        status=OrderStatus.DELIVERED,
        subtotal=COLLECTED,
        delivery_fee=Decimal("0.00"),
        discount_amount=Decimal("0.00"),
        loyalty_discount=Decimal("0.00"),
        total_amount=COLLECTED,
        payment_method=PaymentMethod.CASH,
        is_paid=True,
    )
    db.session.add(order)
    db.session.flush()
    item = OrderItem(
        order_id=order.id,
        product_id=sample_product.id,
        contract_id=contract.id,
        contract_product_price_id=price_row.id,
        quantity=QUANTITY,
        unit_price=UNIT_PRICE,
        total_price=UNIT_PRICE * QUANTITY,
    )
    db.session.add(item)
    db.session.flush()

    # --- units ALREADY reserved + consumed for this order (order 627 quirk) ---
    reserve = CorporatePrepaymentLedger(
        contract_id=contract.id,
        account_id=account.id,
        balance_id=balance.id,
        product_id=sample_product.id,
        order_id=order.id,
        order_item_id=item.id,
        event_type=CorporatePrepaymentEventType.RESERVE,
        units=Decimal(str(QUANTITY)),
        unit_price_snapshot=UNIT_PRICE,
        amount=UNIT_PRICE * QUANTITY,
        currency="UZS",
        idempotency_key=f"reserve:order_item:{item.id}",
    )
    db.session.add(reserve)
    db.session.flush()
    consume = CorporatePrepaymentLedger(
        contract_id=contract.id,
        account_id=account.id,
        balance_id=balance.id,
        product_id=sample_product.id,
        order_id=order.id,
        order_item_id=item.id,
        event_type=CorporatePrepaymentEventType.CONSUME,
        units=Decimal(str(QUANTITY)),
        unit_price_snapshot=UNIT_PRICE,
        amount=UNIT_PRICE * QUANTITY,
        currency="UZS",
        idempotency_key=f"consume:reserve:{reserve.id}",
    )
    db.session.add(consume)

    # --- COD payment fully collected on delivery ---
    payment = Payment(
        order_id=order.id,
        user_id=workplace_user.id,
        payment_method=PaymentMethod.CASH,
        amount=COLLECTED,
        amount_collected=COLLECTED,
        outstanding_amount=Decimal("0.00"),
        currency="UZS",
        status=PaymentStatus.COMPLETED,
        collected_by=driver.id,  # ARCH-006: completed cash needs a collector
        payment_id=f"pay-{uuid4().hex[:10]}",
    )
    db.session.add(payment)
    db.session.flush()

    event = CashCollectionEvent(
        customer_id=workplace_user.id,
        collector_user_id=driver.id,
        recorded_by_user_id=driver.id,
        order_id=order.id,
        amount=COLLECTED,
        currency="UZS",
        source=CashCollectionSource.DELIVERY_COMPLETION,
        unapplied_amount=Decimal("0.00"),  # fully allocated to this order
        occurred_at=datetime.now(timezone.utc),
    )
    db.session.add(event)
    db.session.flush()
    allocation = CashCollectionAllocation(
        cash_collection_event_id=event.id,
        payment_id=payment.id,
        order_id=order.id,
        allocated_amount=COLLECTED,
        allocation_order=1,
        allocation_mode="auto",
        allocation_metadata={"affects_payment_projection": True},
    )
    db.session.add(allocation)
    db.session.commit()
    return order, contract, balance


def _consume_rows(order_id):
    return CorporatePrepaymentLedger.query.filter_by(
        order_id=order_id, event_type=CorporatePrepaymentEventType.CONSUME
    ).count()


def test_order_627_cash_to_business_account_credits_customer(
    db, workplace_user, sample_product, driver
):
    order, contract, balance = _seed_order_627(db, workplace_user, sample_product, driver)
    cash = CashCollectionService()

    # Baseline: everything collected/applied — customer holds no prepaid credit yet.
    assert cash.get_customer_prepaid_balance(workplace_user.id) == Decimal("0.00")
    assert _consume_rows(order.id) == 1
    consumed_before = Decimal(str(balance.consumed_units))

    result = OrderPaymentMethodEditService().apply_edit(
        order_id=order.id,
        new_method="business_account",
        reason="reclassify order 627 to business account",
        actor_user_id=driver.id,
    )

    db.session.expire_all()
    order = Order.query.get(order.id)
    payment = order.payment
    balance = CorporatePrepaymentBalance.query.get(balance.id)

    # Order + payment now read business_account and are settled.
    assert order.payment_method == PaymentMethod.BUSINESS_ACCOUNT
    assert order.is_paid is True
    assert payment.payment_method == PaymentMethod.BUSINESS_ACCOUNT
    assert payment.status == PaymentStatus.COMPLETED

    # Units consumed EXACTLY once — no double consume.
    assert _consume_rows(order.id) == 1
    assert Decimal(str(balance.consumed_units)) == consumed_before

    # The previously-collected 72 000 is now the customer's prepaid credit.
    assert cash.get_customer_prepaid_balance(workplace_user.id) == COLLECTED

    assert result.corporate_action == "settled_business_account"
    assert result.money_action == "cash_credited"


def test_order_627_apply_twice_is_idempotent(db, workplace_user, sample_product, driver):
    order, contract, balance = _seed_order_627(db, workplace_user, sample_product, driver)
    cash = CashCollectionService()
    svc = OrderPaymentMethodEditService()

    svc.apply_edit(
        order_id=order.id,
        new_method="business_account",
        reason="reclassify order 627 to business account",
        actor_user_id=driver.id,
    )
    db.session.expire_all()
    credit_after_first = cash.get_customer_prepaid_balance(workplace_user.id)
    consume_after_first = _consume_rows(order.id)
    assert credit_after_first == COLLECTED
    assert consume_after_first == 1

    # Order is already business_account: the transition is no longer allowed, so a
    # second apply is rejected and neither the credit nor the consume doubles.
    from business_app.utils.exceptions import ValidationError

    with pytest.raises(ValidationError):
        svc.apply_edit(
            order_id=order.id,
            new_method="business_account",
            reason="attempt a second reclassify",
            actor_user_id=driver.id,
        )

    db.session.expire_all()
    assert cash.get_customer_prepaid_balance(workplace_user.id) == COLLECTED
    assert _consume_rows(order.id) == 1


def test_order_627_reclassification_does_not_notify_customer(
    db, workplace_user, sample_product, driver
):
    """An admin payment-method reclassification of an already-delivered/paid
    order must NOT fire a customer 'payment successful' notification — nothing
    new was paid, the customer already has their delivery."""
    from unittest.mock import patch

    order, contract, balance = _seed_order_627(db, workplace_user, sample_product, driver)

    with patch(
        "business_app.tasks.notification_tasks.send_payment_confirmation_task.delay"
    ) as mock_notify:
        OrderPaymentMethodEditService().apply_edit(
            order_id=order.id,
            new_method="business_account",
            reason="reclassify order 627 to business account",
            actor_user_id=driver.id,
        )

    mock_notify.assert_not_called()

    # ...and the settlement still landed correctly.
    db.session.expire_all()
    order = Order.query.get(order.id)
    assert order.payment_method == PaymentMethod.BUSINESS_ACCOUNT
    assert order.is_paid is True
