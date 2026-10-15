"""Shared world for the corporate-contract dual-pricing tests.

Spec: docs/superpowers/specs/2026-10-09-corporate-contract-dual-pricing-design.md.

Every price is distinct so an assertion can only pass for one reason:
catalogue 15 000 (conftest `sample_product.base_price`), bank transfer 12 000,
cash 13 500. QUANTITY 2 keeps every method above MIN_ORDER_AMOUNT (20 000).
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from flask_jwt_extended import create_access_token

from business_app.models.corporate import (
    CorporateContract,
    CorporateContractProductPrice,
    CorporateContractStatus,
    CorporatePrepaymentAccount,
    CorporatePrepaymentBalance,
)
from business_app.models.user import User, UserAddress
from business_app.utils.password_security import hash_password
from shared.enums import CorporateContractTrackingMode, EntitySubtype, UserRole, UserType

CATALOGUE = Decimal("15000.00")
BANK = Decimal("12000.00")
CASH = Decimal("13500.00")
QUANTITY = 2


def make_workplace(db, *, subtype=EntitySubtype.WORKPLACE):
    """A phone-verified legal entity with one in-zone address."""
    user = User(
        email=f"wp-{uuid4().hex[:8]}@example.com",
        phone=f"+99895{uuid4().int % 10000000:07d}",
        password_hash="x" * 60,
        first_name="Work",
        last_name="Place",
        user_type=UserType.ENTITY,
        entity_subtype=subtype,
        company_name="Dual Price Co",
        role=UserRole.CUSTOMER,
        is_verified=True,
        phone_verified_at=datetime.now(UTC),
    )
    db.session.add(user)
    db.session.flush()
    address = UserAddress(
        user_id=user.id,
        title="Office",
        full_address="Office 1",
        street_address="Office 1",
        city="Tashkent",
        latitude=41.31,
        longitude=69.28,
        is_default=True,
    )
    db.session.add(address)
    db.session.commit()
    return user, address


def make_contract(
    db,
    user,
    product,
    *,
    bank=BANK,
    cash=CASH,
    prepaid_units=Decimal("50.00"),
    tracking_mode=CorporateContractTrackingMode.UNITS,
    allows_debt=False,
):
    """An active contract covering `product` at `bank` / `cash` (either may be None)."""
    contract = CorporateContract(
        user_id=user.id,
        contract_number=f"C-{uuid4().hex[:10]}",
        name="Dual price contract",
        status=CorporateContractStatus.ACTIVE,
        start_date=datetime.now(UTC) - timedelta(days=1),
        currency="UZS",
        is_active=True,
        allows_debt=allows_debt,
        tracking_mode=tracking_mode,
    )
    db.session.add(contract)
    db.session.flush()
    row = CorporateContractProductPrice(
        contract_id=contract.id,
        product_id=product.id,
        business_account_unit_price=bank,
        cash_unit_price=cash,
        is_prepayment_eligible=True,
        is_active=True,
    )
    db.session.add(row)
    if tracking_mode == CorporateContractTrackingMode.UNITS:
        account = CorporatePrepaymentAccount(contract_id=contract.id, is_active=True)
        db.session.add(account)
        db.session.flush()
        db.session.add(
            CorporatePrepaymentBalance(
                account_id=account.id,
                product_id=product.id,
                prepaid_units=prepaid_units,
                reserved_units=Decimal("0.00"),
                consumed_units=Decimal("0.00"),
                is_active=True,
            )
        )
    db.session.commit()
    return contract, row


def make_staff(db, role, phone):
    user = User(
        email=f"staff-{uuid4().hex[:8]}@example.com",
        phone=phone,
        password_hash=hash_password("StaffPassword123!"),
        first_name=role.value,
        last_name="Staff",
        user_type=UserType.STAFF,
        role=role,
        is_verified=True,
    )
    db.session.add(user)
    db.session.commit()
    return user


def customer_headers(app, user):
    with app.app_context():
        token = create_access_token(identity=str(user.id))
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def admin_headers(app, admin):
    """`manager_or_higher_required` reads the ROLE CLAIM, not the DB row."""
    with app.app_context():
        token = create_access_token(identity=str(admin.id), additional_claims={"role": "admin"})
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
