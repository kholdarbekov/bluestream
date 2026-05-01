"""Route tests for COD collection search and admin collection recording."""

from types import SimpleNamespace
from unittest.mock import Mock

from flask_jwt_extended import create_access_token

from shared.enums import UserRole
from business_app.utils.exceptions import ValidationError

def _auth_headers(app, user_id: int) -> dict:
    with app.app_context():
        token = create_access_token(identity=str(user_id))
    return {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}


def _manager_headers(app, user_id: int, role: str) -> dict:
    with app.app_context():
        token = create_access_token(identity=str(user_id), additional_claims={'role': role})
    return {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}


def test_delivery_driver_can_search_customers_for_cod_collection(
    client,
    app,
    delivery_driver,
    monkeypatch,
):
    mocked_search = Mock(return_value=[
        {
            'id': 11,
            'first_name': 'Alice',
            'last_name': 'Buyer',
            'phone': '+998901234500',
            'active_cod_debt_count': 2,
            'total_outstanding_amount': 25000,
            'cod_restricted': True,
        }
    ])
    monkeypatch.setattr(
        'business_app.services.staff_service.StaffService.search_customers_for_cod_collection',
        mocked_search,
    )

    response = client.get(
        '/api/v1/staff/customers/search?q=%2B998901234500&type=phone',
        headers=_auth_headers(app, delivery_driver.id),
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload['data']['total'] == 1
    assert payload['data']['items'][0]['active_cod_debt_count'] == 2
    mocked_search.assert_called_once_with('+998901234500', 'phone', only_with_open_cod=True)


def test_admin_can_record_standalone_cash_collection(
    client,
    app,
    admin_user,
    sample_user,
    monkeypatch,
):
    fake_event = SimpleNamespace(
        driver_cash_session_id=None,
        to_dict=lambda: {
            'id': 91,
            'customer_id': sample_user.id,
            'amount': 9000,
            'source': 'standalone_meeting',
        },
    )
    mocked_post_collection = Mock(return_value=fake_event)
    mocked_session_detail = Mock(return_value={'id': 12})
    monkeypatch.setattr(
        'business_app.services.cash_collection_service.CashCollectionService.post_collection',
        mocked_post_collection,
    )
    monkeypatch.setattr(
        'business_app.services.driver_reconciliation_service.DriverReconciliationService.get_session_detail',
        mocked_session_detail,
    )

    response = client.post(
        '/api/v1/admin/staff/cash-reconciliation/collections',
        headers=_manager_headers(app, admin_user.id, UserRole.ADMIN.value),
        json={
            'customer_id': sample_user.id,
            'amount': 9000,
            'source': 'standalone_meeting',
            'notes': 'Collected old COD debt in office.',
        },
    )

    assert response.status_code == 201
    payload = response.get_json()
    assert payload['data']['cash_collection_event']['id'] == 91
    assert payload['data']['driver_cash_session'] is None
    mocked_post_collection.assert_called_once()
    mocked_session_detail.assert_not_called()


def test_admin_can_record_personal_card_transfer_collection(
    client,
    app,
    admin_user,
    sample_user,
    monkeypatch,
):
    fake_event = SimpleNamespace(
        driver_cash_session_id=None,
        to_dict=lambda: {
            'id': 109,
            'customer_id': sample_user.id,
            'order_id': 456,
            'amount': 12000,
            'source': 'personal_card_transfer',
        },
    )
    mocked_post_collection = Mock(return_value=fake_event)
    monkeypatch.setattr(
        'business_app.services.cash_collection_service.CashCollectionService.post_collection',
        mocked_post_collection,
    )

    response = client.post(
        '/api/v1/admin/staff/cash-reconciliation/collections',
        headers=_manager_headers(app, admin_user.id, UserRole.ADMIN.value),
        json={
            'customer_id': sample_user.id,
            'order_id': 456,
            'amount': 12000,
            'source': 'personal_card_transfer',
            'notes': 'Customer paid to owner personal card.',
        },
    )

    assert response.status_code == 201
    payload = response.get_json()
    assert payload['data']['cash_collection_event']['source'] == 'personal_card_transfer'
    mocked_post_collection.assert_called_once()
    called_kwargs = mocked_post_collection.call_args.kwargs
    assert called_kwargs['source'] == 'personal_card_transfer'
    assert called_kwargs['order_id'] == 456


def test_admin_personal_card_transfer_validation_error_returns_400(
    client,
    app,
    admin_user,
    sample_user,
    monkeypatch,
):
    monkeypatch.setattr(
        'business_app.services.cash_collection_service.CashCollectionService.post_collection',
        Mock(side_effect=ValidationError('order_id is required for personal card transfer collections')),
    )

    response = client.post(
        '/api/v1/admin/staff/cash-reconciliation/collections',
        headers=_manager_headers(app, admin_user.id, UserRole.ADMIN.value),
        json={
            'customer_id': sample_user.id,
            'amount': 12000,
            'source': 'personal_card_transfer',
            'notes': 'Missing order should fail.',
        },
    )

    assert response.status_code == 400
    payload = response.get_json()
    assert 'order_id is required for personal card transfer collections' in payload.get('errors', [])


def test_manager_can_search_cod_collection_users_from_admin_surface(
    client,
    app,
    admin_user,
    monkeypatch,
):
    mocked_search = Mock(return_value=[
        {
            'id': 1,
            'first_name': 'Delivery',
            'last_name': 'person 1',
            'phone': '+998909150172',
            'active_cod_debt_count': 2,
            'total_outstanding_amount': 130000,
            'cod_restricted': True,
        }
    ])
    monkeypatch.setattr(
        'business_app.services.staff_service.StaffService.search_customers_for_cod_collection',
        mocked_search,
    )

    response = client.get(
        '/api/v1/admin/staff/cash-reconciliation/users/search?q=1&type=phone',
        headers=_manager_headers(app, admin_user.id, UserRole.ADMIN.value),
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload['data']['total'] == 1
    assert payload['data']['items'][0]['id'] == 1
    mocked_search.assert_called_once_with('1', 'phone', only_with_open_cod=True)


def test_manager_can_list_users_with_open_cod_debts_from_admin_surface(
    client,
    app,
    admin_user,
    monkeypatch,
):
    mocked_list = Mock(return_value=[
        {
            'id': 1,
            'first_name': 'Delivery',
            'last_name': 'person 1',
            'phone': '+998909150172',
            'role': 'admin',
            'user_type': 'staff',
            'active_cod_debt_count': 2,
            'total_outstanding_amount': 130000.0,
            'cod_restricted': True,
        }
    ])
    monkeypatch.setattr(
        'business_app.services.cash_collection_service.CashCollectionService.list_users_with_open_cod_debts',
        mocked_list,
    )

    response = client.get(
        '/api/v1/admin/staff/cash-reconciliation/users/with-open-cod',
        headers=_manager_headers(app, admin_user.id, UserRole.ADMIN.value),
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload['data']['total'] == 1
    assert payload['data']['items'][0]['id'] == 1
    mocked_list.assert_called_once_with(limit=200)
