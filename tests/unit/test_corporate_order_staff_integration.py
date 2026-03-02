"""Integration-style unit tests for corporate hooks in order/staff services."""

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from business_app.models.user import User, UserAddress
from business_app.services.order_service import OrderService
from business_app.services.staff_service import StaffService
from business_app.utils.exceptions import ValidationError
from business_app.utils.constants import PaymentMethod, UserRole, UserType
from business_app.utils.password_security import hash_password


def _create_operator_user():
    operator = User(
        email='operator.corp@example.com',
        phone='+998901119900',
        password_hash=hash_password('OperatorPassword123!'),
        first_name='Operator',
        last_name='Corp',
        user_type=UserType.STAFF,
        role=UserRole.OPERATOR,
        is_verified=True,
        created_at=datetime.now(UTC),
    )
    return operator


def test_order_service_create_order_supports_business_account_and_reserve(
    app,
    db,
    sample_user,
    sample_product,
    mock_inventory_service,
):
    sample_user.user_type = "entity"
    db.session.commit()

    address = UserAddress(
        user_id=sample_user.id,
        title='Office',
        full_address='Office Street 1',
        street_address='Office Street 1',
        city='Tashkent',
        latitude=41.31,
        longitude=69.28,
        is_default=True,
    )
    db.session.add(address)
    db.session.commit()

    mock_inventory_service.check_multiple_products_availability.return_value = [
        SimpleNamespace(
            product_id=sample_product.id,
            requested_quantity=2,
            available_quantity=100,
            reserved_quantity=0,
            is_available=True,
            reason='Available',
        )
    ]
    mock_inventory_service.reserve_inventory.return_value = {'success': True, 'expires_at': None}

    service = OrderService(inventory_service=mock_inventory_service)

    with patch(
        'business_app.services.corporate_contract_service.CorporateContractService.resolve_contract_pricing_for_user_product',
        return_value={
            'unit_price': Decimal("14000.00"),
            'contract': SimpleNamespace(id=91),
            'contract_price_row': SimpleNamespace(id=901),
        },
    ) as resolve_price, patch(
        'business_app.services.corporate_contract_service.CorporateContractService.validate_business_account_order',
        return_value=None,
    ) as validate_business_account_order, patch(
        'business_app.services.corporate_contract_service.CorporateContractService.reserve_for_order',
        return_value=None,
    ) as reserve_for_order, patch(
        'business_app.services.payment_service.PaymentService.initialize_order_payment',
        return_value=None,
    ) as initialize_payment:
        order = service.create_order(
            sample_user.id,
            {
                'items': [{'product_id': sample_product.id, 'quantity': 2}],
                'delivery_address': {
                    'delivery_address_id': address.id,
                    'street': address.street_address,
                    'latitude': address.latitude,
                    'longitude': address.longitude,
                },
                'payment_method': 'business_account',
            },
        )

    assert order.payment_method == PaymentMethod.BUSINESS_ACCOUNT
    assert resolve_price.called
    validate_business_account_order.assert_called_once()
    assert order.order_items[0].contract_id == 91
    assert order.order_items[0].contract_product_price_id == 901
    reserve_for_order.assert_called_once_with(order.id)
    initialize_payment.assert_called_once_with(order.id)


def test_staff_service_create_phone_order_supports_business_account_and_reserve(
    app,
    db,
    sample_user,
    sample_product,
):
    sample_user.user_type = "entity"
    operator = _create_operator_user()
    db.session.add(operator)
    db.session.commit()

    with patch(
        'business_app.services.corporate_contract_service.CorporateContractService.resolve_contract_pricing_for_user_product',
        return_value={
            'unit_price': Decimal("13000.00"),
            'contract': SimpleNamespace(id=77),
            'contract_price_row': SimpleNamespace(id=707),
        },
    ) as resolve_price, patch(
        'business_app.services.corporate_contract_service.CorporateContractService.validate_business_account_order',
        return_value=None,
    ) as validate_business_account_order, patch(
        'business_app.services.corporate_contract_service.CorporateContractService.reserve_for_order',
        return_value=None,
    ) as reserve_for_order, patch(
        'business_app.services.payment_service.PaymentService.initialize_order_payment',
        return_value=None,
    ) as initialize_payment:
        order = StaffService.create_phone_order(
            operator_id=operator.id,
            client_id=sample_user.id,
            order_data={
                'items': [{'product_id': sample_product.id, 'quantity': 2}],
                'payment_method': 'business_account',
                'delivery_notes': 'Corporate delivery',
                'delivery_address_id': None,
            },
        )

    assert order.payment_method == PaymentMethod.BUSINESS_ACCOUNT
    assert resolve_price.called
    validate_business_account_order.assert_called_once()
    assert order.order_items[0].contract_id == 77
    assert order.order_items[0].contract_product_price_id == 707
    reserve_for_order.assert_called_once_with(order.id, actor_user_id=operator.id)
    initialize_payment.assert_called_once_with(order.id, actor_user_id=operator.id)


def test_order_service_business_account_requires_applicable_contract(
    app,
    db,
    sample_user,
    sample_product,
    mock_inventory_service,
):
    sample_user.user_type = "entity"
    db.session.commit()

    address = UserAddress(
        user_id=sample_user.id,
        title='Office',
        full_address='Office Street 1',
        street_address='Office Street 1',
        city='Tashkent',
        latitude=41.31,
        longitude=69.28,
        is_default=True,
    )
    db.session.add(address)
    db.session.commit()

    mock_inventory_service.check_multiple_products_availability.return_value = [
        SimpleNamespace(
            product_id=sample_product.id,
            requested_quantity=2,
            available_quantity=100,
            reserved_quantity=0,
            is_available=True,
            reason='Available',
        )
    ]

    service = OrderService(inventory_service=mock_inventory_service)

    with patch(
        'business_app.services.corporate_contract_service.CorporateContractService.resolve_contract_pricing_for_user_product',
        return_value={
            'unit_price': Decimal("18000.00"),
            'contract': None,
            'contract_price_row': None,
        },
    ):
        try:
            service.create_order(
                sample_user.id,
                {
                    'items': [{'product_id': sample_product.id, 'quantity': 2}],
                    'delivery_address': {
                        'delivery_address_id': address.id,
                        'street': address.street_address,
                        'latitude': address.latitude,
                        'longitude': address.longitude,
                    },
                    'payment_method': 'business_account',
                },
            )
            assert False, "Expected ValidationError"
        except ValidationError as exc:
            assert "active corporate contract" in str(exc)


def test_staff_service_business_account_requires_applicable_contract(
    app,
    db,
    sample_user,
    sample_product,
):
    sample_user.user_type = "entity"
    operator = _create_operator_user()
    db.session.add(operator)
    db.session.commit()

    with patch(
        'business_app.services.corporate_contract_service.CorporateContractService.resolve_contract_pricing_for_user_product',
        return_value={
            'unit_price': Decimal("18000.00"),
            'contract': None,
            'contract_price_row': None,
        },
    ):
        try:
            StaffService.create_phone_order(
                operator_id=operator.id,
                client_id=sample_user.id,
                order_data={
                    'items': [{'product_id': sample_product.id, 'quantity': 2}],
                    'payment_method': 'business_account',
                    'delivery_notes': 'Corporate delivery',
                    'delivery_address_id': None,
                },
            )
            assert False, "Expected ValidationError"
        except ValidationError as exc:
            assert "active corporate contract" in str(exc)
