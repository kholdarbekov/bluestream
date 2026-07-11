"""
Integration tests for GET /payments/methods.

Task 12: the handler must delegate fully to
``PaymentService.get_available_payment_methods`` and accept a ``?context=``
query param (order|subscription), rejecting anything else with 400.
"""


def test_methods_endpoint_never_returns_payme(client, auth_headers):
    response = client.get("/api/v1/payments/methods", headers=auth_headers)
    assert response.status_code == 200
    methods = {m["method"] for m in response.get_json()["data"]["available_methods"]}
    assert "payme" not in methods


def test_methods_endpoint_accepts_subscription_context(client, auth_headers):
    response = client.get("/api/v1/payments/methods?context=subscription", headers=auth_headers)
    assert response.status_code == 200


def test_methods_endpoint_rejects_unknown_context(client, auth_headers):
    response = client.get("/api/v1/payments/methods?context=banana", headers=auth_headers)
    assert response.status_code == 400
