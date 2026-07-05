from datetime import datetime, timedelta, timezone
from decimal import Decimal
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
from business_app.models.order import Order, OrderItem
from business_app.models.payment import Payment
from business_app.models.user import User
from business_app.services.order_payment_method_edit_service import OrderPaymentMethodEditService
from shared.enums import (
    CorporateContractTrackingMode,
    EntitySubtype,
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
