from decimal import Decimal

from business_app import db
from business_app.models.corporate import (
    CorporatePrepaymentBalance,
    CorporatePrepaymentEventType,
    CorporatePrepaymentLedger,
)
from business_app.services.corporate_contract_service import CorporateContractService
from shared.enums import PaymentMethod
# import the shared helpers from the sibling test module
from tests.unit.test_corporate_contract_service import (
    _create_contract_and_account,
    _create_contract_price,
    _create_order_with_item,
    _create_product,
    _get_product_balance,
)


def _seed_reserved_order(sample_user, *, deliver: bool):
    contract, account = _create_contract_and_account(sample_user.id)
    product = _create_product("Reverse Bottles", Decimal("20000.00"))
    price_row = _create_contract_price(contract.id, product.id, Decimal("18000.00"))
    db.session.add(
        CorporatePrepaymentBalance(
            account_id=account.id, product_id=product.id,
            prepaid_units=Decimal("10.00"), reserved_units=Decimal("0.00"),
            consumed_units=Decimal("0.00"), is_active=True,
        )
    )
    db.session.commit()
    order = _create_order_with_item(
        sample_user.id, product.id, 3, Decimal("18000.00"),
        contract_id=contract.id, contract_product_price_id=price_row.id,
        payment_method=PaymentMethod.BUSINESS_ACCOUNT,
    )
    svc = CorporateContractService()
    svc.reserve_for_order(order.id)
    if deliver:
        svc.consume_for_order(order.id)
    return account, product, order, svc


def test_reverse_after_consume_returns_units_to_availability(db, sample_user):
    account, product, order, svc = _seed_reserved_order(sample_user, deliver=True)
    balance = _get_product_balance(account.id, product.id)
    assert balance.consumed_units == Decimal("3.00")
    assert balance.available_units == Decimal("7.00")

    svc.reverse_order_prepayment(order.id, reason="payment method changed to cash")

    db.session.refresh(balance)
    assert balance.consumed_units == Decimal("0.00")
    assert balance.reserved_units == Decimal("0.00")
    assert balance.available_units == Decimal("10.00")
    assert CorporatePrepaymentLedger.query.filter(
        CorporatePrepaymentLedger.order_id == order.id,
        CorporatePrepaymentLedger.event_type == CorporatePrepaymentEventType.ADJUSTMENT,
    ).count() == 1


def test_reverse_before_delivery_releases_open_reserve(db, sample_user):
    account, product, order, svc = _seed_reserved_order(sample_user, deliver=False)
    balance = _get_product_balance(account.id, product.id)
    assert balance.reserved_units == Decimal("3.00")

    svc.reverse_order_prepayment(order.id, reason="cash")

    db.session.refresh(balance)
    assert balance.reserved_units == Decimal("0.00")
    assert balance.available_units == Decimal("10.00")


def test_reverse_is_idempotent(db, sample_user):
    account, product, order, svc = _seed_reserved_order(sample_user, deliver=True)
    svc.reverse_order_prepayment(order.id, reason="cash")
    svc.reverse_order_prepayment(order.id, reason="cash")  # second call must be a no-op
    balance = _get_product_balance(account.id, product.id)
    assert balance.consumed_units == Decimal("0.00")
    assert balance.available_units == Decimal("10.00")


def test_reverse_noop_when_no_reserve(db, sample_user):
    # Order with no corporate reserve entries → empty result, no error.
    from tests.unit.test_corporate_contract_service import _create_product as _mk
    product = _mk("Plain", Decimal("20000.00"))
    order = _create_order_with_item(sample_user.id, product.id, 1, Decimal("20000.00"))
    assert CorporateContractService().reverse_order_prepayment(order.id) == []
