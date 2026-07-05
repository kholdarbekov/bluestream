"""order_qualifies_for_business_account — non-raising mirror of
validate_business_account_order used by the default-payment-method resolver."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from business_app import db
from business_app.models.corporate import (
    CorporateContract,
    CorporateContractProductPrice,
    CorporateContractStatus,
    CorporatePrepaymentAccount,
    CorporatePrepaymentBalance,
)
from business_app.models.product import Product, ProductCategory
from business_app.models.user import User
from business_app.services.corporate_contract_service import CorporateContractService
from shared.enums import (
    CorporateContractTrackingMode,
    EntitySubtype,
    UserRole,
    UserType,
)


def _make_user(user_type, entity_subtype=None) -> User:
    user = User(
        email=f"u-{uuid4().hex[:8]}@example.com",
        phone=f"+99890{uuid4().int % 10000000:07d}",
        password_hash="x" * 60,
        first_name="Test",
        last_name="User",
        user_type=user_type,
        entity_subtype=entity_subtype,
        company_name="Co" if user_type == UserType.ENTITY else None,
        role=UserRole.CUSTOMER,
        is_verified=True,
    )
    db.session.add(user)
    db.session.commit()
    return user


def _make_product() -> Product:
    category = ProductCategory(name=f"Cat-{uuid4().hex[:6]}", is_active=True)
    db.session.add(category)
    db.session.flush()
    product = Product(
        name=f"Water-{uuid4().hex[:6]}",
        category_id=category.id,
        base_price=Decimal("20000.00"),
        is_active=True,
        stock_quantity=100,
        min_stock_level=1,
        max_stock_level=500,
        size="19L",
        volume=19.0,
        volume_unit="L",
    )
    db.session.add(product)
    db.session.commit()
    return product


def _make_units_contract(user_id, product, *, prepaid_units, allows_debt=False):
    contract = CorporateContract(
        user_id=user_id,
        contract_number=f"Q-{uuid4().hex[:10]}",
        name="Qualify Test Contract",
        status=CorporateContractStatus.ACTIVE,
        start_date=datetime.now(UTC) - timedelta(days=1),
        currency="UZS",
        is_active=True,
        allows_debt=allows_debt,
        tracking_mode=CorporateContractTrackingMode.UNITS,
    )
    db.session.add(contract)
    db.session.flush()
    price_row = CorporateContractProductPrice(
        contract_id=contract.id,
        product_id=product.id,
        unit_price=Decimal("18000.00"),
        is_prepayment_eligible=True,
        is_active=True,
    )
    db.session.add(price_row)
    account = CorporatePrepaymentAccount(contract_id=contract.id, is_active=True)
    db.session.add(account)
    db.session.flush()
    db.session.add(
        CorporatePrepaymentBalance(
            account_id=account.id,
            product_id=product.id,
            prepaid_units=Decimal(str(prepaid_units)),
            reserved_units=Decimal("0.00"),
            consumed_units=Decimal("0.00"),
            is_active=True,
        )
    )
    db.session.commit()
    return contract, price_row


def _items(product, price_row, contract, quantity):
    return [
        {
            "product_id": product.id,
            "contract_id": contract.id,
            "contract_product_price_id": price_row.id,
            "quantity": quantity,
        }
    ]


def test_workplace_all_covered_sufficient_balance_qualifies(db):
    product = _make_product()
    user = _make_user(UserType.ENTITY, EntitySubtype.WORKPLACE)
    contract, price_row = _make_units_contract(user.id, product, prepaid_units=10)
    items = _items(product, price_row, contract, 3)
    assert CorporateContractService().order_qualifies_for_business_account(user, items) is True


def test_individual_does_not_qualify(db):
    product = _make_product()
    user = _make_user(UserType.INDIVIDUAL)
    items = [{"product_id": product.id, "contract_id": None, "contract_product_price_id": None, "quantity": 1}]
    assert CorporateContractService().order_qualifies_for_business_account(user, items) is False


def test_grocery_does_not_qualify(db):
    product = _make_product()
    user = _make_user(UserType.ENTITY, EntitySubtype.GROCERY_STORE)
    items = [{"product_id": product.id, "contract_id": None, "contract_product_price_id": None, "quantity": 1}]
    assert CorporateContractService().order_qualifies_for_business_account(user, items) is False


def test_uncovered_line_does_not_qualify(db):
    product = _make_product()
    user = _make_user(UserType.ENTITY, EntitySubtype.WORKPLACE)
    _make_units_contract(user.id, product, prepaid_units=10)
    # Line carries NO contract linkage → not covered.
    items = [{"product_id": product.id, "contract_id": None, "contract_product_price_id": None, "quantity": 1}]
    assert CorporateContractService().order_qualifies_for_business_account(user, items) is False


def test_insufficient_balance_without_debt_does_not_qualify(db):
    product = _make_product()
    user = _make_user(UserType.ENTITY, EntitySubtype.WORKPLACE)
    contract, price_row = _make_units_contract(user.id, product, prepaid_units=2, allows_debt=False)
    items = _items(product, price_row, contract, 5)  # 5 requested > 2 prepaid
    assert CorporateContractService().order_qualifies_for_business_account(user, items) is False


def test_insufficient_balance_with_debt_qualifies(db):
    product = _make_product()
    user = _make_user(UserType.ENTITY, EntitySubtype.WORKPLACE)
    contract, price_row = _make_units_contract(user.id, product, prepaid_units=2, allows_debt=True)
    items = _items(product, price_row, contract, 5)
    assert CorporateContractService().order_qualifies_for_business_account(user, items) is True
