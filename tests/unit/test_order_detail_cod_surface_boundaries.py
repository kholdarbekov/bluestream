"""Regression tests for keeping customer-level COD statements out of order detail payloads."""

from unittest.mock import Mock

from business_app.utils.constants import PaymentMethod


def test_admin_order_details_stay_order_scoped_for_cash_orders(
    client,
    db,
    sample_order,
    sample_payment,
    admin_auth_headers,
    monkeypatch,
):
    sample_order.payment_method = PaymentMethod.CASH
    sample_payment.payment_method = PaymentMethod.CASH
    db.session.commit()

    timeline_mock = Mock(return_value={'payment_id': sample_payment.id, 'timeline': []})
    customer_statement_mock = Mock(return_value={'items': [{'payment_id': sample_payment.id}]})
    monkeypatch.setattr(
        'business_app.services.cash_collection_service.CashCollectionService.get_order_payment_timeline',
        timeline_mock,
    )
    monkeypatch.setattr(
        'business_app.services.cash_collection_service.CashCollectionService.get_customer_cod_statement',
        customer_statement_mock,
    )

    response = client.get(
        f'/api/v1/admin/orders/{sample_order.id}',
        headers=admin_auth_headers,
    )

    assert response.status_code == 200
    payload = response.get_json()
    order = payload['data']['order']
    assert order['payment_timeline'] == {'payment_id': sample_payment.id, 'timeline': []}
    assert 'customer_cod_statement' not in order
    customer_statement_mock.assert_not_called()


def test_customer_order_details_stay_order_scoped_for_cash_orders(
    client,
    db,
    sample_order,
    sample_payment,
    auth_headers,
    monkeypatch,
):
    sample_order.payment_method = PaymentMethod.CASH
    sample_payment.payment_method = PaymentMethod.CASH
    db.session.commit()

    timeline_mock = Mock(return_value={'payment_id': sample_payment.id, 'timeline': []})
    customer_statement_mock = Mock(return_value={'items': [{'payment_id': sample_payment.id}]})
    monkeypatch.setattr(
        'business_app.services.cash_collection_service.CashCollectionService.get_order_payment_timeline',
        timeline_mock,
    )
    monkeypatch.setattr(
        'business_app.services.cash_collection_service.CashCollectionService.get_customer_cod_statement',
        customer_statement_mock,
    )

    response = client.get(
        f'/api/v1/orders/{sample_order.id}',
        headers=auth_headers,
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload['data']['payment_timeline'] == {'payment_id': sample_payment.id, 'timeline': []}
    assert 'customer_cod_statement' not in payload['data']
    customer_statement_mock.assert_not_called()
