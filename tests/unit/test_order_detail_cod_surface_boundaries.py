"""Regression tests for keeping customer-level COD statements out of order detail payloads."""

from decimal import Decimal
from unittest.mock import Mock

from shared.enums import PaymentMethod, PaymentStatus
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


def test_order_details_normalize_completed_prepaid_projection(
    client,
    db,
    sample_order,
    sample_payment,
    admin_auth_headers,
    auth_headers,
):
    sample_order.payment_method = PaymentMethod.CARD
    sample_payment.payment_method = PaymentMethod.PAYME
    sample_payment.status = PaymentStatus.COMPLETED
    sample_payment.amount_collected = Decimal("0.00")
    sample_payment.outstanding_amount = sample_order.total_amount
    db.session.commit()

    admin_response = client.get(
        f'/api/v1/admin/orders/{sample_order.id}',
        headers=admin_auth_headers,
    )
    assert admin_response.status_code == 200
    admin_order = admin_response.get_json()['data']['order']
    assert admin_order['amount_collected'] == float(sample_order.total_amount)
    assert admin_order['outstanding_amount'] == 0.0
    assert admin_order['payment_timeline']['amount_collected'] == float(sample_order.total_amount)
    assert admin_order['payment_timeline']['outstanding_amount'] == 0.0

    customer_response = client.get(
        f'/api/v1/orders/{sample_order.id}',
        headers=auth_headers,
    )
    assert customer_response.status_code == 200
    customer_payload = customer_response.get_json()['data']
    assert customer_payload['order']['payment_info']['amount_collected'] == float(sample_order.total_amount)
    assert customer_payload['order']['payment_info']['outstanding_amount'] == 0.0
    assert customer_payload['payment_timeline']['amount_collected'] == float(sample_order.total_amount)
    assert customer_payload['payment_timeline']['outstanding_amount'] == 0.0
