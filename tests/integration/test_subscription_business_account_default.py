"""A subscription for a qualifying workplace entity with no explicit payment
method must default to business_account."""

from datetime import UTC, datetime, timedelta
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
)
from business_app.models.subscription import Subscription
from business_app.models.user import User, UserAddress
from business_app.services.subscription_service import SubscriptionService
from shared.enums import (
    CorporateContractTrackingMode,
    EntitySubtype,
    PaymentMethod,
    UserRole,
    UserType,
)


@pytest.fixture
def workplace_user(db):
    user = User(
        email=f"sub-{uuid4().hex[:8]}@example.com",
        phone=f"+99897{uuid4().int % 10000000:07d}",
        password_hash="x" * 60,
        first_name="Sub",
        last_name="Workplace",
        user_type=UserType.ENTITY,
        entity_subtype=EntitySubtype.WORKPLACE,
        company_name="Sub Workplace",
        role=UserRole.CUSTOMER,
        is_verified=True,
    )
    db.session.add(user)
    db.session.commit()
    return user


@pytest.fixture
def covered_contract(db, workplace_user, sample_product):
    contract = CorporateContract(
        user_id=workplace_user.id,
        contract_number=f"S-{uuid4().hex[:10]}",
        name="Sub Coverage",
        status=CorporateContractStatus.ACTIVE,
        start_date=datetime.now(UTC) - timedelta(days=1),
        currency="UZS",
        is_active=True,
        tracking_mode=CorporateContractTrackingMode.UNITS,
    )
    db.session.add(contract)
    db.session.flush()
    db.session.add(
        CorporateContractProductPrice(
            contract_id=contract.id,
            product_id=sample_product.id,
            unit_price=Decimal("18000.00"),
            is_prepayment_eligible=True,
            is_active=True,
        )
    )
    account = CorporatePrepaymentAccount(contract_id=contract.id, is_active=True)
    db.session.add(account)
    db.session.flush()
    db.session.add(
        CorporatePrepaymentBalance(
            account_id=account.id,
            product_id=sample_product.id,
            prepaid_units=Decimal("100.00"),
            reserved_units=Decimal("0.00"),
            consumed_units=Decimal("0.00"),
            is_active=True,
        )
    )
    db.session.commit()
    return contract


@pytest.fixture
def sub_address(db, workplace_user):
    address = UserAddress(
        user_id=workplace_user.id,
        title="Office",
        full_address="Office 2",
        street_address="Office 2",
        city="Tashkent",
        latitude=41.31,
        longitude=69.28,
        is_default=True,
    )
    db.session.add(address)
    db.session.commit()
    return address


@pytest.mark.integration
def test_subscription_defaults_to_business_account_for_qualifying_workplace(
    app, db, workplace_user, sample_product, covered_contract, sub_address
):
    result = SubscriptionService().create_subscription(
        {
            "user_id": workplace_user.id,
            "delivery_address_id": sub_address.id,
            "billing_cycle": "monthly",
            "delivery_frequency": "weekly",
            # NO "payment_method"
        },
        [{"product_id": sample_product.id, "quantity": 2}],
    )

    subscription = Subscription.query.filter_by(subscription_number=result["subscription_number"]).first()
    assert subscription is not None
    assert subscription.payment_method == PaymentMethod.BUSINESS_ACCOUNT


@pytest.mark.integration
def test_subscription_explicit_cash_respected(
    app, db, workplace_user, sample_product, covered_contract, sub_address
):
    result = SubscriptionService().create_subscription(
        {
            "user_id": workplace_user.id,
            "delivery_address_id": sub_address.id,
            "billing_cycle": "monthly",
            "delivery_frequency": "weekly",
            "payment_method": PaymentMethod.CASH,
        },
        [{"product_id": sample_product.id, "quantity": 2}],
    )
    subscription = Subscription.query.filter_by(subscription_number=result["subscription_number"]).first()
    assert subscription is not None
    assert subscription.payment_method == PaymentMethod.CASH
