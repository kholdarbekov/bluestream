"""StaffService.create_phone_order — default unspecified payment method to
business_account for qualifying workplace corporate orders (same server-side
default as OrderService.create_order)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from business_app.models.corporate import (
    CorporateContract,
    CorporateContractProductPrice,
    CorporateContractStatus,
    CorporatePrepaymentAccount,
    CorporatePrepaymentBalance,
)
from business_app.models.user import User, UserAddress
from business_app.services.staff_service import StaffService
from business_app.utils.password_security import hash_password
from shared.enums import CorporateContractTrackingMode, EntitySubtype, PaymentMethod, UserRole, UserType


def _create_operator_user():
    operator = User(
        email='operator.phonedefault@example.com',
        phone='+998901119901',
        password_hash=hash_password('OperatorPassword123!'),
        first_name='Operator',
        last_name='Default',
        user_type=UserType.STAFF,
        role=UserRole.OPERATOR,
        is_verified=True,
        created_at=datetime.now(UTC),
    )
    return operator


def _make_units_contract(db, user_id, product, *, prepaid_units):
    contract = CorporateContract(
        user_id=user_id,
        contract_number='Q-PHONE-DEFAULT-1',
        name='Phone Default Contract',
        status=CorporateContractStatus.ACTIVE,
        start_date=datetime.now(UTC) - timedelta(days=1),
        currency='UZS',
        is_active=True,
        allows_debt=False,
        tracking_mode=CorporateContractTrackingMode.UNITS,
    )
    db.session.add(contract)
    db.session.flush()

    price_row = CorporateContractProductPrice(
        contract_id=contract.id,
        product_id=product.id,
        unit_price=Decimal('13000.00'),
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
            reserved_units=Decimal('0.00'),
            consumed_units=Decimal('0.00'),
            is_active=True,
        )
    )
    db.session.commit()
    return contract, price_row


def test_phone_order_without_method_defaults_to_business_account(
    app,
    db,
    sample_user,
    sample_product,
):
    sample_user.user_type = 'entity'
    sample_user.entity_subtype = EntitySubtype.WORKPLACE
    db.session.commit()

    # Ordered quantity (2) must not exceed prepaid_units for qualification.
    _make_units_contract(db, sample_user.id, sample_product, prepaid_units=10)

    operator = _create_operator_user()
    db.session.add(operator)

    address = UserAddress(
        user_id=sample_user.id,
        title='Corporate Office',
        full_address='Corporate Street 1',
        street_address='Corporate Street 1',
        city='Tashkent',
        latitude=41.31,
        longitude=69.28,
        is_default=True,
    )
    db.session.add(address)
    db.session.commit()

    order = StaffService.create_phone_order(
        operator_id=operator.id,
        client_id=sample_user.id,
        order_data={
            'items': [{'product_id': sample_product.id, 'quantity': 2}],
            'delivery_notes': 'Corporate delivery',
            'delivery_address_id': address.id,
            # NO payment_method key — operator did not specify a method.
        },
    )

    assert order.payment_method == PaymentMethod.BUSINESS_ACCOUNT
    assert order.order_items[0].contract_id is not None
    assert order.order_items[0].contract_product_price_id is not None
