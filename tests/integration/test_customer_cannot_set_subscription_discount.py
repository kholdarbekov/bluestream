"""A customer must never be able to set their own subscription discount.

`CreateSubscriptionRequest` and `SubscriptionPreviewRequest` used to accept
`discount_percentage` (0-100) and `create_subscription` applied it, so any logged-in
customer could POST 99 and be billed 1% of the basket on every cycle. Subscriptions give
no discount; only the admin path may set one.

Driven through the HTTP endpoints the bot, the web constructor and the admin UI call.
"""

from decimal import Decimal
from unittest.mock import patch

import pytest
from flask_jwt_extended import create_access_token

from business_app.models.order import Order
from business_app.models.subscription import Subscription
from business_app.services.subscription_service import SubscriptionService

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _no_external_io():
    """Celery publish and customer notifications are external I/O."""
    with patch("business_app.tasks.subscription_tasks.create_subscription_delivery_task.delay"), patch(
        "business_app.api.subscriptions.get_notification_service"
    ):
        yield


def _bot_body(product_id, address_id, **extra):
    """The body the Telegram bot posts (telegram_bot/handlers/subscriptions.py)."""
    return {
        "name": "Subscription - Weekly",
        "frequency": "weekly",
        "delivery_address_id": address_id,
        "payment_method": "cash",
        "items": [{"product_id": product_id, "quantity": 2}],
        "auto_renew": True,
        "delivery_time_slot_id": None,
        **extra,
    }


def _bill(client_app, subscription_id):
    """"Create next order now": the order the subscription's next delivery gets."""
    with client_app.app_context():
        result = SubscriptionService().create_next_order(subscription_id, due_only=False)
        assert result["success"] is True, result
        return Order.query.get(result["order_id"])


def test_customer_sending_99_percent_gets_zero_and_is_billed_full_price(
    app, client, db, auth_headers, user_address, sample_product
):
    resp = client.post(
        "/api/v1/subscriptions/",
        headers=auth_headers,
        json=_bot_body(sample_product.id, user_address.id, discount_percentage=99),
    )
    assert resp.status_code == 201, resp.get_json()

    sub_id = resp.get_json()["data"]["subscription"]["id"]
    full_price = Decimal(str(sample_product.base_price)) * 2
    sub = Subscription.query.get(sub_id)
    assert float(sub.discount_percentage or 0) == 0
    assert Decimal(str(sub.billing_amount)) == full_price

    order = _bill(app, sub_id)
    assert Decimal(order.discount_amount or 0) == 0
    assert Decimal(order.total_amount) == Decimal(order.subtotal) + Decimal(order.delivery_fee or 0)
    assert Decimal(order.subtotal) == full_price


def test_preview_ignores_a_customer_discount(client, db, auth_headers, sample_product):
    resp = client.post(
        "/api/v1/subscriptions/preview",
        headers=auth_headers,
        json={
            "frequency": "weekly",
            "items": [{"product_id": sample_product.id, "quantity": 2}],
            "discount_percentage": 50,
        },
    )
    assert resp.status_code == 200, resp.get_json()
    preview = resp.get_json()["data"]["preview"]
    full_price = float(sample_product.base_price) * 2
    assert preview["discount_amount"] == 0
    assert preview["total_amount"] == full_price


def test_admin_can_still_set_a_discount_and_billing_honours_it(
    app, client, db, admin_user, sample_user, user_address, sample_product
):
    """Positive control: the fix did not just zero every discount."""
    token = create_access_token(identity=str(admin_user.id), additional_claims={"role": "admin"})
    resp = client.post(
        "/api/v1/admin/subscriptions",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={
            "user_id": sample_user.id,
            "name": "Admin granted",
            "frequency": "weekly",
            "delivery_address_id": user_address.id,
            "payment_method": "cash",
            "items": [{"product_id": sample_product.id, "quantity": 2}],
            "discount_percentage": 10,
        },
    )
    assert resp.status_code == 201, resp.get_json()
    sub_id = resp.get_json()["data"]["subscription"]["id"]
    assert float(Subscription.query.get(sub_id).discount_percentage) == 10

    order = _bill(app, sub_id)
    subtotal = Decimal(order.subtotal)
    assert subtotal > 0
    assert Decimal(order.discount_amount) == subtotal * 10 / 100


def test_the_templates_route_that_advertised_prices_is_gone(client):
    """Its hard-coded "templates" quoted monthly costs no rule produced; the route is deleted
    (spec 2026-10-23 §4.7), so nothing can advertise a discount from it."""
    resp = client.get("/api/v1/subscriptions/templates")
    assert resp.status_code == 404
