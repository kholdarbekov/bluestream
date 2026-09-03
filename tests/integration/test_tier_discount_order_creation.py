"""POST /api/v1/orders/ — the tier discount is granted on the COD rail only.

Driven over HTTP with the body the customer clients actually send. Nothing is
mocked: inventory reservation, the payment row and the corporate prepayment
reservation all run for real, because the defect class this guards against is
a payload/ordering assumption, not a unit-level arithmetic slip.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from flask_jwt_extended import create_access_token

from business_app import db as _db
from business_app.models.corporate import (
    CorporateContract,
    CorporateContractProductPrice,
    CorporateContractStatus,
    CorporatePrepaymentAccount,
    CorporatePrepaymentBalance,
)
from business_app.models.order import Order
from business_app.models.user import User, UserAddress
from business_app.services.loyalty_service import LoyaltyService
from business_app.utils.order_totals import compute_order_total
from shared.enums import (
    CorporateContractTrackingMode,
    EntitySubtype,
    PaymentMethod,
    UserRole,
    UserType,
)
from tests.integration.tier_discount_factory import (
    post_order,
    seed_program,
    seed_tier,
    verify_phone,
)

TIER_RATE = Decimal("7")


def _headers(app, user_id):
    with app.app_context():
        token = create_access_token(identity=str(user_id))
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


@pytest.fixture
def program(db):
    return seed_program(db)


@pytest.fixture
def customer(db, app, sample_user):
    verify_phone(db, sample_user)
    return sample_user


def _reload(order_id):
    _db.session.expire_all()
    return Order.query.get(order_id)


def test_cash_order_carries_the_tier_discount(app, db, customer, sample_product, user_address, program):
    seed_tier(db, program, name="Base", rate=TIER_RATE)

    resp = post_order(
        app,
        _headers(app, customer.id),
        product_id=sample_product.id,
        address_id=user_address.id,
        payment_method="cash",
    )

    assert resp.status_code == 201, resp.get_json()
    order = _reload(resp.get_json()["data"]["order"]["id"])

    expected = LoyaltyService().quote_tier_discount(customer, order.subtotal, PaymentMethod.CASH).amount
    assert expected > Decimal("0.00")  # the feature is actually doing something
    assert Decimal(str(order.tier_discount)) == expected
    assert Decimal(str(order.total_amount)) == compute_order_total(
        subtotal=Decimal(str(order.subtotal)),
        discount_amount=Decimal(str(order.discount_amount or 0)),
        delivery_fee=Decimal(str(order.delivery_fee or 0)),
        loyalty_discount=Decimal(str(order.loyalty_discount or 0)),
        tier_discount=Decimal(str(order.tier_discount)),
    )
    assert Decimal(str(order.payment.amount)) == Decimal(str(order.total_amount))


def test_click_order_carries_no_tier_discount(app, db, customer, sample_product, user_address, program):
    seed_tier(db, program, name="Base", rate=TIER_RATE)

    resp = post_order(
        app,
        _headers(app, customer.id),
        product_id=sample_product.id,
        address_id=user_address.id,
        payment_method="click",
    )

    assert resp.status_code == 201, resp.get_json()
    order = _reload(resp.get_json()["data"]["order"]["id"])
    assert Decimal(str(order.tier_discount)) == Decimal("0.00")
    assert Decimal(str(order.total_amount)) == Decimal(str(order.subtotal)) + Decimal(str(order.delivery_fee or 0))


def test_payme_is_refused_outright_so_can_never_carry_one(
    app, db, customer, sample_product, user_address, program
):
    """payme is not in CUSTOMER_SELECTABLE_METHODS: create_order refuses it, so
    no payme order exists to carry a discount in the first place."""
    seed_tier(db, program, name="Base", rate=TIER_RATE)

    resp = post_order(
        app,
        _headers(app, customer.id),
        product_id=sample_product.id,
        address_id=user_address.id,
        payment_method="payme",
    )

    assert resp.status_code == 400, resp.get_json()
    assert Order.query.filter_by(user_id=customer.id).count() == 0


def test_zero_percent_tier_grants_nothing(app, db, customer, sample_product, user_address, program):
    seed_tier(db, program, name="Base", rate=Decimal("0"))

    resp = post_order(
        app,
        _headers(app, customer.id),
        product_id=sample_product.id,
        address_id=user_address.id,
        payment_method="cash",
    )

    assert resp.status_code == 201, resp.get_json()
    order = _reload(resp.get_json()["data"]["order"]["id"])
    assert Decimal(str(order.tier_discount)) == Decimal("0.00")


def test_ineligible_entity_gets_nothing_even_on_cash(
    app, db, sample_user, sample_product, user_address, program
):
    seed_tier(db, program, name="Base", rate=TIER_RATE)
    sample_user.user_type = UserType.ENTITY
    sample_user.entity_subtype = EntitySubtype.WORKPLACE
    db.session.add(
        CorporateContract(
            user_id=sample_user.id,
            contract_number=f"C-{uuid4().hex[:10]}",
            name="No loyalty",
            status=CorporateContractStatus.ACTIVE,
            start_date=datetime.now(UTC) - timedelta(days=1),
            currency="UZS",
            is_active=True,
            is_loyalty_points_eligible=False,
        )
    )
    verify_phone(db, sample_user)

    resp = post_order(
        app,
        _headers(app, sample_user.id),
        product_id=sample_product.id,
        address_id=user_address.id,
        payment_method="cash",
    )

    assert resp.status_code == 201, resp.get_json()
    order = _reload(resp.get_json()["data"]["order"]["id"])
    assert Decimal(str(order.tier_discount)) == Decimal("0.00")


@pytest.fixture
def loyalty_eligible_workplace(db, sample_product):
    """A workplace entity that IS loyalty-eligible and whose lines are fully
    contract-covered — so only the RAIL, not eligibility, blocks the discount."""
    user = User(
        email=f"wp-{uuid4().hex[:8]}@example.com",
        phone=f"+99895{uuid4().int % 10000000:07d}",
        password_hash="x" * 60,
        first_name="Work",
        last_name="Place",
        user_type=UserType.ENTITY,
        entity_subtype=EntitySubtype.WORKPLACE,
        company_name="Tier Co",
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
    contract = CorporateContract(
        user_id=user.id,
        contract_number=f"C-{uuid4().hex[:10]}",
        name="Loyalty-eligible coverage",
        status=CorporateContractStatus.ACTIVE,
        start_date=datetime.now(UTC) - timedelta(days=1),
        currency="UZS",
        is_active=True,
        is_loyalty_points_eligible=True,
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
            prepaid_units=Decimal("50.00"),
            reserved_units=Decimal("0.00"),
            consumed_units=Decimal("0.00"),
            is_active=True,
        )
    )
    db.session.commit()
    return user, address


def test_business_account_grants_nothing_even_when_the_user_is_loyalty_eligible(
    app, db, loyalty_eligible_workplace, sample_product, program
):
    seed_tier(db, program, name="Base", rate=TIER_RATE)
    user, address = loyalty_eligible_workplace
    assert LoyaltyService().is_user_loyalty_eligible(user) is True  # eligibility is NOT the gate here

    resp = post_order(
        app,
        _headers(app, user.id),
        product_id=sample_product.id,
        address_id=address.id,
        payment_method="business_account",
    )

    assert resp.status_code == 201, resp.get_json()
    order = _reload(resp.get_json()["data"]["order"]["id"])
    assert Decimal(str(order.tier_discount)) == Decimal("0.00")
