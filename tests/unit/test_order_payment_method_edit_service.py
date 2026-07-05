from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

import pytest

from business_app import db
from business_app.models.corporate import (
    CorporateContract,
    CorporateContractProductPrice,
    CorporateContractStatus,
    CorporatePrepaymentAccount,
    CorporatePrepaymentBalance,
    CorporatePrepaymentEventType,
    CorporatePrepaymentLedger,
)
from business_app.models.order import Order, OrderItem, OrderItemMarkingCodeAllocation
from business_app.models.payment import (
    CashCollectionAllocation,
    CashCollectionEvent,
    Payment,
    PaymentFiscalization,
)
from business_app.models.product import ProductMarkingCode
from business_app.models.user import User
from business_app.services.cash_collection_service import CashCollectionService
from business_app.services.order_payment_method_edit_service import OrderPaymentMethodEditService
from business_app.utils.exceptions import ValidationError
from shared.enums import (
    CashCollectionSource,
    CorporateContractTrackingMode,
    EntitySubtype,
    FiscalizationStatus,
    MarkingCodeLedgerEventType,
    MarkingCodeStatus,
    OrderStatus,
    PaymentMethod,
    PaymentStatus,
    UserRole,
    UserType,
)


@pytest.fixture
def workplace_user(db):
    user = User(
        email=f"wp-{uuid4().hex[:8]}@example.com",
        phone=f"+99895{uuid4().int % 10000000:07d}",
        password_hash="x" * 60,
        first_name="Work",
        last_name="Place",
        user_type=UserType.ENTITY,
        entity_subtype=EntitySubtype.WORKPLACE,
        company_name="Test Workplace",
        role=UserRole.CUSTOMER,
        is_verified=True,
    )
    db.session.add(user)
    db.session.commit()
    return user


@pytest.fixture
def covered_contract(db, workplace_user, sample_product):
    """Workplace entity + active contract + prepaid balance covering sample_product (50 units)."""
    contract = CorporateContract(
        user_id=workplace_user.id,
        contract_number=f"C-{uuid4().hex[:10]}",
        name="Coverage Contract",
        status=CorporateContractStatus.ACTIVE,
        start_date=datetime.now(timezone.utc) - timedelta(days=1),
        currency="UZS",
        is_active=True,
        tracking_mode=CorporateContractTrackingMode.UNITS,
    )
    db.session.add(contract)
    db.session.flush()
    price_row = CorporateContractProductPrice(
        contract_id=contract.id,
        product_id=sample_product.id,
        unit_price=Decimal("18000.00"),
        is_prepayment_eligible=True,
        is_active=True,
    )
    db.session.add(price_row)
    account = CorporatePrepaymentAccount(contract_id=contract.id, is_active=True)
    db.session.add(account)
    db.session.flush()
    balance = CorporatePrepaymentBalance(
        account_id=account.id,
        product_id=sample_product.id,
        prepaid_units=Decimal("50.00"),
        reserved_units=Decimal("0.00"),
        consumed_units=Decimal("0.00"),
        is_active=True,
    )
    db.session.add(balance)
    db.session.commit()
    return contract, price_row, account, balance


def _make_order(user, status, payment_method, total=Decimal("36000.00")):
    order = Order(
        user_id=user.id,
        order_number=f"ORD-{uuid4().hex[:10]}",
        status=status,
        subtotal=total,
        delivery_fee=Decimal("0.00"),
        discount_amount=Decimal("0.00"),
        loyalty_discount=Decimal("0.00"),
        total_amount=total,
        payment_method=payment_method,
    )
    db.session.add(order)
    db.session.commit()
    return order


def _add_contract_item(order, product, contract, price_row, quantity=2, unit_price=Decimal("18000.00")):
    item = OrderItem(
        order_id=order.id,
        product_id=product.id,
        contract_id=contract.id,
        contract_product_price_id=price_row.id,
        quantity=quantity,
        unit_price=unit_price,
        total_price=unit_price * quantity,
    )
    db.session.add(item)
    db.session.commit()
    return item


# 1. cash delivered order → allowed_target_methods includes "business_account" ONLY when qualifying.
def test_cash_delivered_order_allows_business_account_when_qualifying(
    db, workplace_user, sample_product, covered_contract
):
    contract, price_row, account, balance = covered_contract
    order = _make_order(workplace_user, OrderStatus.DELIVERED, PaymentMethod.CASH)
    _add_contract_item(order, sample_product, contract, price_row)

    svc = OrderPaymentMethodEditService()
    metadata = svc.get_edit_metadata(order)
    assert metadata["is_payment_method_editable"] is True
    assert "business_account" in metadata["allowed_target_methods"]

    plan = svc.preview(order_id=order.id, new_method="business_account")
    assert plan.is_editable
    assert plan.blocking_reasons == []


# 2. completed CLICK order → not editable (blocking reason "completed_online_payment_terminal").
def test_completed_click_order_not_editable(db, sample_user):
    order = _make_order(sample_user, OrderStatus.DELIVERED, PaymentMethod.CLICK)
    payment = Payment(
        order_id=order.id,
        user_id=sample_user.id,
        payment_method=PaymentMethod.CLICK,
        amount=order.total_amount,
        currency="UZS",
        status=PaymentStatus.COMPLETED,
        payment_id=f"pay-{uuid4().hex[:8]}",
    )
    db.session.add(payment)
    db.session.commit()

    svc = OrderPaymentMethodEditService()
    metadata = svc.get_edit_metadata(order)
    assert metadata["is_payment_method_editable"] is False

    plan = svc.preview(order_id=order.id, new_method="business_account")
    assert not plan.is_editable
    assert "completed_online_payment_terminal" in plan.blocking_reasons


# 3. disallowed transition (cash → click) → blocking "transition_not_allowed".
def test_disallowed_transition_cash_to_click_blocked(db, sample_user):
    order = _make_order(sample_user, OrderStatus.DELIVERED, PaymentMethod.CASH)
    plan = OrderPaymentMethodEditService().preview(order_id=order.id, new_method="click")
    assert not plan.is_editable
    assert any(r.startswith("transition_not_allowed") for r in plan.blocking_reasons)


# 4. CANCELLED order → blocking "order_not_editable_status".
def test_cancelled_order_blocked(db, sample_user):
    order = _make_order(sample_user, OrderStatus.CANCELLED, PaymentMethod.CASH)
    plan = OrderPaymentMethodEditService().preview(order_id=order.id, new_method="business_account")
    assert not plan.is_editable
    assert any(r.startswith("order_not_editable_status") for r in plan.blocking_reasons)


# 5. business_account order → allowed targets include {"cash", "click"}.
def test_business_account_order_allows_cash_and_click_targets(db, sample_user):
    order = _make_order(sample_user, OrderStatus.DELIVERED, PaymentMethod.BUSINESS_ACCOUNT)
    metadata = OrderPaymentMethodEditService().get_edit_metadata(order)
    assert metadata["is_payment_method_editable"] is True
    assert "cash" in metadata["allowed_target_methods"]
    assert "click" in metadata["allowed_target_methods"]


# 6. target business_account but cart not qualifying → blocking "not_business_account_eligible".
def test_target_business_account_not_qualifying_blocked(db, sample_user):
    order = _make_order(sample_user, OrderStatus.DELIVERED, PaymentMethod.CASH)
    svc = OrderPaymentMethodEditService()
    plan = svc.preview(order_id=order.id, new_method="business_account")
    assert not plan.is_editable
    assert "not_business_account_eligible" in plan.blocking_reasons

    # get_edit_metadata must not diverge from preview: a target that preview
    # blocks (here the only allowed target, business_account) must be filtered
    # out of the offered methods, and the order reported as not editable.
    metadata = svc.get_edit_metadata(order)
    assert "business_account" not in metadata["allowed_target_methods"]
    assert metadata["is_payment_method_editable"] is False


# 7. round-trip guard: order with a reverse:* ledger row + target business_account
#    → blocking "corporate_settlement_previously_reversed".
def test_roundtrip_guard_blocks_target_business_account(db, workplace_user, sample_product, covered_contract):
    contract, price_row, account, balance = covered_contract
    order = _make_order(workplace_user, OrderStatus.DELIVERED, PaymentMethod.CASH)
    _add_contract_item(order, sample_product, contract, price_row)

    ledger_row = CorporatePrepaymentLedger(
        contract_id=contract.id,
        account_id=account.id,
        balance_id=balance.id,
        product_id=sample_product.id,
        order_id=order.id,
        event_type=CorporatePrepaymentEventType.RELEASE,
        units=Decimal("2.00"),
        idempotency_key=f"reverse:reserve:{uuid4().hex[:8]}",
    )
    db.session.add(ledger_row)
    db.session.commit()

    svc = OrderPaymentMethodEditService()
    plan = svc.preview(order_id=order.id, new_method="business_account")
    assert not plan.is_editable
    assert "corporate_settlement_previously_reversed" in plan.blocking_reasons

    # The round-trip guard must also exclude business_account from the metadata
    # dropdown, matching the preview block.
    metadata = svc.get_edit_metadata(order)
    assert "business_account" not in metadata["allowed_target_methods"]


# --------------------------------------------------------------------------- #
# apply_edit — into business_account (T1 cash, T2 online)
# --------------------------------------------------------------------------- #

_COLLECTED = Decimal("72000.00")  # 4 units * 18 000


def _consume_rows(order_id):
    return CorporatePrepaymentLedger.query.filter_by(
        order_id=order_id, event_type=CorporatePrepaymentEventType.CONSUME
    ).count()


def _seed_cash_collected_order(db, user, product, contract, price_row, account, balance, driver):
    """Delivered cash order, 72 000 collected on delivery AND units already
    reserved+consumed (the order-627 shape)."""
    balance.consumed_units = Decimal("4.00")  # this order's units already drawn
    order = _make_order(user, OrderStatus.DELIVERED, PaymentMethod.CASH, total=_COLLECTED)
    item = _add_contract_item(order, product, contract, price_row, quantity=4)

    reserve = CorporatePrepaymentLedger(
        contract_id=contract.id,
        account_id=account.id,
        balance_id=balance.id,
        product_id=product.id,
        order_id=order.id,
        order_item_id=item.id,
        event_type=CorporatePrepaymentEventType.RESERVE,
        units=Decimal("4.00"),
        idempotency_key=f"reserve:order_item:{item.id}",
    )
    db.session.add(reserve)
    db.session.flush()
    consume = CorporatePrepaymentLedger(
        contract_id=contract.id,
        account_id=account.id,
        balance_id=balance.id,
        product_id=product.id,
        order_id=order.id,
        order_item_id=item.id,
        event_type=CorporatePrepaymentEventType.CONSUME,
        units=Decimal("4.00"),
        idempotency_key=f"consume:reserve:{reserve.id}",
    )
    db.session.add(consume)

    payment = Payment(
        order_id=order.id,
        user_id=user.id,
        payment_method=PaymentMethod.CASH,
        amount=_COLLECTED,
        amount_collected=_COLLECTED,
        outstanding_amount=Decimal("0.00"),
        currency="UZS",
        status=PaymentStatus.COMPLETED,
        collected_by=driver.id,
        payment_id=f"pay-{uuid4().hex[:10]}",
    )
    db.session.add(payment)
    db.session.flush()
    event = CashCollectionEvent(
        customer_id=user.id,
        collector_user_id=driver.id,
        recorded_by_user_id=driver.id,
        order_id=order.id,
        amount=_COLLECTED,
        currency="UZS",
        source=CashCollectionSource.DELIVERY_COMPLETION,
        unapplied_amount=Decimal("0.00"),
        occurred_at=datetime.now(timezone.utc),
    )
    db.session.add(event)
    db.session.flush()
    db.session.add(
        CashCollectionAllocation(
            cash_collection_event_id=event.id,
            payment_id=payment.id,
            order_id=order.id,
            allocated_amount=_COLLECTED,
            allocation_order=1,
            allocation_mode="auto",
            allocation_metadata={"affects_payment_projection": True},
        )
    )
    db.session.commit()
    return order


def test_apply_t1_cash_collected_credits_customer(
    db, workplace_user, sample_product, covered_contract, delivery_driver
):
    contract, price_row, account, balance = covered_contract
    order = _seed_cash_collected_order(
        db, workplace_user, sample_product, contract, price_row, account, balance, delivery_driver
    )
    cash = CashCollectionService()
    assert cash.get_customer_prepaid_balance(workplace_user.id) == Decimal("0.00")

    result = OrderPaymentMethodEditService().apply_edit(
        order_id=order.id,
        new_method="business_account",
        reason="reclassify to business account",
        actor_user_id=delivery_driver.id,
    )

    db.session.expire_all()
    order = Order.query.get(order.id)
    assert order.payment_method == PaymentMethod.BUSINESS_ACCOUNT
    assert order.is_paid is True
    assert order.payment.status == PaymentStatus.COMPLETED
    assert order.payment.payment_method == PaymentMethod.BUSINESS_ACCOUNT
    # units consumed exactly once (idempotent no double-consume)
    assert _consume_rows(order.id) == 1
    # collected cash becomes customer prepaid credit
    assert cash.get_customer_prepaid_balance(workplace_user.id) == _COLLECTED
    assert result.money_action == "cash_credited"
    assert result.corporate_action == "settled_business_account"


def test_apply_t1_cash_not_collected_no_credit(
    db, workplace_user, sample_product, covered_contract, delivery_driver
):
    contract, price_row, account, balance = covered_contract
    order = _make_order(workplace_user, OrderStatus.DELIVERED, PaymentMethod.CASH, total=_COLLECTED)
    _add_contract_item(order, sample_product, contract, price_row, quantity=4)
    payment = Payment(
        order_id=order.id,
        user_id=workplace_user.id,
        payment_method=PaymentMethod.CASH,
        amount=_COLLECTED,
        amount_collected=Decimal("0.00"),
        outstanding_amount=_COLLECTED,
        currency="UZS",
        status=PaymentStatus.PENDING,
        payment_id=f"pay-{uuid4().hex[:10]}",
    )
    db.session.add(payment)
    db.session.commit()

    cash = CashCollectionService()
    result = OrderPaymentMethodEditService().apply_edit(
        order_id=order.id,
        new_method="business_account",
        reason="reclassify uncollected cod",
        actor_user_id=delivery_driver.id,
    )

    db.session.expire_all()
    order = Order.query.get(order.id)
    assert order.payment_method == PaymentMethod.BUSINESS_ACCOUNT
    assert order.is_paid is True
    assert order.payment.payment_method == PaymentMethod.BUSINESS_ACCOUNT
    assert order.payment.status == PaymentStatus.COMPLETED
    # a clean cash order draws down units on settlement (reserve + consume rows)
    assert _consume_rows(order.id) == 1
    # no cash was collected → no customer credit created
    assert cash.get_customer_prepaid_balance(workplace_user.id) == Decimal("0.00")
    assert result.money_action == "cod_cancelled"


def test_apply_t2_click_releases_reserved_marking_codes(
    db, workplace_user, sample_product, covered_contract, delivery_driver
):
    contract, price_row, account, balance = covered_contract
    order = _make_order(workplace_user, OrderStatus.DELIVERED, PaymentMethod.CLICK, total=_COLLECTED)
    item = _add_contract_item(order, sample_product, contract, price_row, quantity=4)
    payment = Payment(
        order_id=order.id,
        user_id=workplace_user.id,
        payment_method=PaymentMethod.CLICK,
        amount=_COLLECTED,
        currency="UZS",
        status=PaymentStatus.PENDING,
        consume_marking_codes=True,
        payment_id=f"pay-{uuid4().hex[:10]}",
    )
    db.session.add(payment)
    db.session.flush()

    code = ProductMarkingCode(
        product_id=sample_product.id,
        order_id=order.id,
        code=f"MC-{uuid4().hex}",
        status=MarkingCodeStatus.RESERVED,
        reserved_at=datetime.now(timezone.utc),
    )
    db.session.add(code)
    db.session.flush()
    db.session.add(
        OrderItemMarkingCodeAllocation(
            order_item_id=item.id,
            order_id=order.id,
            payment_id=payment.id,
            product_marking_code_id=code.id,
            action=MarkingCodeLedgerEventType.RESERVED,
        )
    )
    db.session.commit()

    result = OrderPaymentMethodEditService().apply_edit(
        order_id=order.id,
        new_method="business_account",
        reason="reclassify pending click order",
        actor_user_id=delivery_driver.id,
    )

    db.session.expire_all()
    order = Order.query.get(order.id)
    assert order.payment_method == PaymentMethod.BUSINESS_ACCOUNT
    assert order.is_paid is True
    assert order.payment.payment_method == PaymentMethod.BUSINESS_ACCOUNT
    assert order.payment.status == PaymentStatus.COMPLETED
    # the pending online payment's reserved marking code is released back to stock
    assert ProductMarkingCode.query.get(code.id).status == MarkingCodeStatus.AVAILABLE
    assert result.money_action == "online_cancelled"


def test_apply_rejects_short_reason(db, workplace_user, sample_product, covered_contract):
    contract, price_row, account, balance = covered_contract
    order = _make_order(workplace_user, OrderStatus.DELIVERED, PaymentMethod.CASH)
    _add_contract_item(order, sample_product, contract, price_row)
    with pytest.raises(ValidationError):
        OrderPaymentMethodEditService().apply_edit(
            order_id=order.id, new_method="business_account", reason="no", actor_user_id=1
        )


# --------------------------------------------------------------------------- #
# apply_edit — out of business_account (T3 cash, T4 click)
# --------------------------------------------------------------------------- #


def _seed_business_account_settled_order(
    db, user, product, contract, price_row, account, balance, *, consumed_marking_codes=False
):
    """Delivered business_account order: units reserved+consumed, payment
    COMPLETED via BA (mirrors order-627 shape after T1 settlement)."""
    balance.consumed_units = Decimal("4.00")
    order = _make_order(user, OrderStatus.DELIVERED, PaymentMethod.BUSINESS_ACCOUNT, total=_COLLECTED)
    item = _add_contract_item(order, product, contract, price_row, quantity=4)

    reserve = CorporatePrepaymentLedger(
        contract_id=contract.id,
        account_id=account.id,
        balance_id=balance.id,
        product_id=product.id,
        order_id=order.id,
        order_item_id=item.id,
        event_type=CorporatePrepaymentEventType.RESERVE,
        units=Decimal("4.00"),
        idempotency_key=f"reserve:order_item:{item.id}",
    )
    db.session.add(reserve)
    db.session.flush()
    consume = CorporatePrepaymentLedger(
        contract_id=contract.id,
        account_id=account.id,
        balance_id=balance.id,
        product_id=product.id,
        order_id=order.id,
        order_item_id=item.id,
        event_type=CorporatePrepaymentEventType.CONSUME,
        units=Decimal("4.00"),
        idempotency_key=f"consume:reserve:{reserve.id}",
    )
    db.session.add(consume)

    payment = Payment(
        order_id=order.id,
        user_id=user.id,
        payment_method=PaymentMethod.BUSINESS_ACCOUNT,
        amount=_COLLECTED,
        amount_collected=_COLLECTED,
        outstanding_amount=Decimal("0.00"),
        currency="UZS",
        status=PaymentStatus.COMPLETED,
        paid_at=datetime.now(timezone.utc),
        consume_marking_codes=consumed_marking_codes,
        payment_id=f"pay-{uuid4().hex[:10]}",
    )
    db.session.add(payment)
    db.session.flush()

    if consumed_marking_codes:
        code = ProductMarkingCode(
            product_id=product.id,
            order_id=order.id,
            code=f"MC-{uuid4().hex}",
            status=MarkingCodeStatus.USED,
        )
        db.session.add(code)
        db.session.flush()
        db.session.add(
            OrderItemMarkingCodeAllocation(
                order_item_id=item.id,
                order_id=order.id,
                payment_id=payment.id,
                product_marking_code_id=code.id,
                action=MarkingCodeLedgerEventType.USED,
            )
        )
        db.session.add(
            PaymentFiscalization(
                payment_id=payment.id,
                status=FiscalizationStatus.COMPLETED,
                provider_name="business_account",
                completed_at=datetime.now(timezone.utc),
            )
        )

    db.session.commit()
    return order


def _reversal_rows(order_id):
    return CorporatePrepaymentLedger.query.filter(
        CorporatePrepaymentLedger.order_id == order_id,
        CorporatePrepaymentLedger.event_type == CorporatePrepaymentEventType.ADJUSTMENT,
        CorporatePrepaymentLedger.idempotency_key.like("reverse:%"),
    ).all()


def test_apply_t3_business_account_to_cash_returns_units_and_resets_payment(
    db, workplace_user, sample_product, covered_contract, delivery_driver
):
    contract, price_row, account, balance = covered_contract
    order = _seed_business_account_settled_order(
        db, workplace_user, sample_product, contract, price_row, account, balance
    )

    result = OrderPaymentMethodEditService().apply_edit(
        order_id=order.id,
        new_method="cash",
        reason="reclassify BA order to cash",
        actor_user_id=delivery_driver.id,
    )

    db.session.expire_all()
    order = Order.query.get(order.id)
    balance = CorporatePrepaymentBalance.query.get(balance.id)

    # units returned to availability: consumed_units decreased, reversal ledger exists
    assert balance.consumed_units == Decimal("0.00")
    assert len(_reversal_rows(order.id)) == 1

    assert order.payment_method == PaymentMethod.CASH
    assert order.is_paid is False
    payment = order.payment
    assert payment.payment_method == PaymentMethod.CASH
    assert payment.status == PaymentStatus.PENDING
    assert payment.amount_collected == Decimal("0.00")
    assert payment.outstanding_amount == order.total_amount
    assert payment.paid_at is None

    assert result.corporate_action == "reversed_prepayment"
    assert result.money_action == "cod_obligation_created"
    assert "business_account_marking_codes_consumed_manual_review" not in result.warnings


def test_apply_t3_flags_consumed_marking_codes_for_manual_review(
    db, workplace_user, sample_product, covered_contract, delivery_driver
):
    contract, price_row, account, balance = covered_contract
    order = _seed_business_account_settled_order(
        db, workplace_user, sample_product, contract, price_row, account, balance, consumed_marking_codes=True
    )

    result = OrderPaymentMethodEditService().apply_edit(
        order_id=order.id,
        new_method="cash",
        reason="reclassify BA order with consumed codes",
        actor_user_id=delivery_driver.id,
    )

    assert "business_account_marking_codes_consumed_manual_review" in result.warnings
    # out of scope: codes are NOT auto-un-used
    assert ProductMarkingCode.query.filter_by(order_id=order.id).first().status == MarkingCodeStatus.USED


def test_apply_t4_business_account_to_click_returns_units_and_creates_link(
    db, workplace_user, sample_product, covered_contract, delivery_driver
):
    contract, price_row, account, balance = covered_contract
    order = _seed_business_account_settled_order(
        db, workplace_user, sample_product, contract, price_row, account, balance, consumed_marking_codes=True
    )

    fake_link = {"payment_url": "https://click.example/pay/abc", "reference": "abc", "expires_at": "2099-01-01T00:00:00"}
    with patch(
        "business_app.services.payment_service.PaymentService.create_payment_link", return_value=fake_link
    ) as mocked_link:
        result = OrderPaymentMethodEditService().apply_edit(
            order_id=order.id,
            new_method="click",
            reason="reclassify BA order to click",
            actor_user_id=delivery_driver.id,
        )

    db.session.expire_all()
    order = Order.query.get(order.id)
    balance = CorporatePrepaymentBalance.query.get(balance.id)

    assert balance.consumed_units == Decimal("0.00")
    assert len(_reversal_rows(order.id)) == 1

    assert order.payment_method == PaymentMethod.CLICK
    assert order.is_paid is False
    payment = order.payment
    assert payment.payment_method == PaymentMethod.CLICK
    assert payment.status == PaymentStatus.PENDING
    assert payment.amount_collected == Decimal("0.00")
    assert payment.outstanding_amount == order.total_amount
    assert payment.paid_at is None

    mocked_link.assert_called_once_with(payment.id)
    assert result.corporate_action == "reversed_prepayment"
    assert result.money_action == "online_payment_link_created"
    assert result.payment_link == fake_link
    assert "business_account_marking_codes_consumed_manual_review" in result.warnings
