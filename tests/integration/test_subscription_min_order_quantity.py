"""A subscription item is a standing order line, so it must respect the product's
minimum order quantity.

`create_order` refuses any line below `products.min_order_quantity`. Every subscription
write path used to accept one, so the subscription was saved and then failed EVERY
billing cycle without producing an order or telling anyone (prod subscription 8,
2026-10-04: 1 x Aqua Element 18.9 l, minimum 2).

Driven through the HTTP endpoints the bot, the web constructor and the admin UI call,
with the payload shapes they send.
"""

import json
from unittest.mock import patch

import pytest

from business_app import db as _db
from business_app.models.order import Order
from business_app.models.product import Product
from business_app.models.subscription import Subscription, SubscriptionItem
from business_app.models.translation import Translation
from business_app.services.subscription_service import SubscriptionService

MIN_QTY_KEY = "api.subscriptions.error.below_min_order_quantity"


@pytest.fixture(autouse=True)
def _no_external_io():
    """Celery publish and customer notifications are external I/O."""
    with patch("business_app.tasks.subscription_tasks.create_subscription_delivery_task.delay"), patch(
        "business_app.api.subscriptions.get_notification_service"
    ):
        yield


@pytest.fixture
def min_two_product(db, sample_product):
    sample_product.min_order_quantity = 2
    db.session.commit()
    return sample_product


@pytest.fixture
def min_three_product(db, sample_category):
    product = Product(
        name="Cooler Water 10L",
        category_id=sample_category.id,
        size="10L",
        volume=10.0,
        volume_unit="L",
        base_price=12000,
        stock_quantity=100,
        is_active=True,
        min_order_quantity=3,
    )
    db.session.add(product)
    db.session.commit()
    return product


@pytest.fixture
def min_qty_translation(db):
    """The refusal copy, so assertions read what a customer reads."""
    db.session.add(
        Translation(
            key=MIN_QTY_KEY,
            language="en",
            value="{product}: at least {minimum} per delivery (you chose {quantity})",
            category="api",
            is_active=True,
        )
    )
    db.session.commit()


def _create_payload(product_id, quantity, address_id):
    """The body the Telegram bot posts (telegram_bot/handlers/subscriptions.py)."""
    return {
        "name": "Subscription - Weekly",
        "billing_cycle": "weekly",
        "delivery_frequency": "weekly",
        "delivery_address_id": address_id,
        "payment_method": "cash",
        "items": [{"product_id": product_id, "quantity": quantity}],
        "auto_renew": True,
        "delivery_time_slot_id": None,
        "discount_percentage": 0.0,
    }


def _create(client, auth_headers, product_id, quantity, address_id):
    return client.post(
        "/api/v1/subscriptions/",
        headers=auth_headers,
        json=_create_payload(product_id, quantity, address_id),
    )


def _body(resp) -> str:
    return json.dumps(resp.get_json(), ensure_ascii=False)


@pytest.mark.integration
class TestCustomerCreate:
    def test_below_minimum_is_refused_and_nothing_is_saved(
        self, client, db, auth_headers, user_address, min_two_product, min_qty_translation
    ):
        resp = _create(client, auth_headers, min_two_product.id, 1, user_address.id)

        assert resp.status_code == 400
        assert "Pure Water 19L: at least 2 per delivery (you chose 1)" in _body(resp)
        assert Subscription.query.count() == 0
        assert SubscriptionItem.query.count() == 0

    def test_at_minimum_is_accepted_and_its_billing_produces_an_order(
        self, app, client, db, auth_headers, user_address, min_two_product
    ):
        resp = _create(client, auth_headers, min_two_product.id, 2, user_address.id)
        assert resp.status_code == 201
        sub_id = resp.get_json()["data"]["subscription"]["id"]

        with app.app_context():
            result = SubscriptionService().process_subscription_billing(sub_id)

        assert result["success"] is True, result
        order = Order.query.filter_by(subscription_id=sub_id).one()
        assert [(i.product_id, i.quantity) for i in order.order_items] == [(min_two_product.id, 2)]

    def test_preview_below_minimum_is_refused_not_a_server_error(
        self, client, db, auth_headers, min_two_product, min_qty_translation
    ):
        resp = client.post(
            "/api/v1/subscriptions/preview",
            headers=auth_headers,
            json={
                "billing_cycle": "weekly",
                "delivery_frequency": "weekly",
                "items": [{"product_id": min_two_product.id, "quantity": 1}],
            },
        )

        assert resp.status_code == 400
        assert "Pure Water 19L: at least 2 per delivery (you chose 1)" in _body(resp)


@pytest.mark.integration
class TestCustomerItemEdits:
    @pytest.fixture
    def subscription_id(self, client, auth_headers, user_address, min_two_product):
        resp = _create(client, auth_headers, min_two_product.id, 3, user_address.id)
        assert resp.status_code == 201, resp.get_json()
        return resp.get_json()["data"]["subscription"]["id"]

    def test_adding_an_item_below_its_minimum_is_refused(
        self, client, db, auth_headers, subscription_id, min_three_product, min_qty_translation
    ):
        resp = client.post(
            f"/api/v1/subscriptions/{subscription_id}/items",
            headers=auth_headers,
            json={"product_id": min_three_product.id, "quantity": 2},
        )

        assert resp.status_code == 400
        assert "Cooler Water 10L: at least 3 per delivery (you chose 2)" in _body(resp)
        assert SubscriptionItem.query.filter_by(product_id=min_three_product.id).count() == 0

    def test_adding_an_item_at_its_minimum_is_accepted(
        self, client, db, auth_headers, subscription_id, min_three_product
    ):
        resp = client.post(
            f"/api/v1/subscriptions/{subscription_id}/items",
            headers=auth_headers,
            json={"product_id": min_three_product.id, "quantity": 3},
        )

        assert resp.status_code == 201
        assert SubscriptionItem.query.filter_by(product_id=min_three_product.id).one().quantity == 3

    def test_lowering_an_item_below_its_minimum_is_refused_and_the_quantity_stays(
        self, client, db, auth_headers, subscription_id, min_two_product, min_qty_translation
    ):
        item = SubscriptionItem.query.filter_by(subscription_id=subscription_id).one()

        resp = client.put(
            f"/api/v1/subscriptions/{subscription_id}/items/{item.id}",
            headers=auth_headers,
            json={"quantity": 1},
        )

        assert resp.status_code == 400
        assert "Pure Water 19L: at least 2 per delivery (you chose 1)" in _body(resp)
        _db.session.expire_all()
        assert SubscriptionItem.query.get(item.id).quantity == 3

    def test_lowering_an_item_to_its_minimum_is_accepted(
        self, client, db, auth_headers, subscription_id
    ):
        item = SubscriptionItem.query.filter_by(subscription_id=subscription_id).one()

        resp = client.put(
            f"/api/v1/subscriptions/{subscription_id}/items/{item.id}",
            headers=auth_headers,
            json={"quantity": 2},
        )

        assert resp.status_code == 200
        _db.session.expire_all()
        assert SubscriptionItem.query.get(item.id).quantity == 2


@pytest.mark.integration
class TestAdmin:
    """The admin UI posts these bodies (admin_ui/src/pages/Subscriptions.js)."""

    def _admin_create(self, client, headers, user, address, product_id, quantity):
        return client.post(
            "/api/v1/admin/subscriptions",
            headers=headers,
            json={
                "user_id": user.id,
                "name": "Weekly Water",
                "billing_cycle": "monthly",
                "delivery_frequency": "weekly",
                "delivery_address_id": address.id,
                "payment_method": "cash",
                "items": [{"product_id": product_id, "quantity": quantity}],
            },
        )

    def test_create_below_minimum_is_refused_and_nothing_is_saved(
        self, client, db, admin_auth_headers, sample_user, user_address, min_two_product, min_qty_translation
    ):
        resp = self._admin_create(client, admin_auth_headers, sample_user, user_address, min_two_product.id, 1)

        assert resp.status_code == 400
        assert "Pure Water 19L: at least 2 per delivery (you chose 1)" in _body(resp)
        assert Subscription.query.count() == 0

    def test_add_and_update_item_below_minimum_are_refused(
        self,
        client,
        db,
        admin_auth_headers,
        sample_user,
        user_address,
        min_two_product,
        min_three_product,
        min_qty_translation,
    ):
        created = self._admin_create(client, admin_auth_headers, sample_user, user_address, min_two_product.id, 2)
        assert created.status_code == 201, created.get_json()
        sub_id = created.get_json()["data"]["subscription"]["id"]
        item = SubscriptionItem.query.filter_by(subscription_id=sub_id).one()

        added = client.post(
            f"/api/v1/admin/subscriptions/{sub_id}/items",
            headers=admin_auth_headers,
            json={"product_id": min_three_product.id, "quantity": 1},
        )
        updated = client.put(
            f"/api/v1/admin/subscriptions/{sub_id}/items/{item.id}",
            headers=admin_auth_headers,
            json={"quantity": 1},
        )

        assert added.status_code == 400
        assert "Cooler Water 10L: at least 3 per delivery (you chose 1)" in _body(added)
        assert updated.status_code == 400
        assert "Pure Water 19L: at least 2 per delivery (you chose 1)" in _body(updated)
        _db.session.expire_all()
        assert SubscriptionItem.query.filter_by(subscription_id=sub_id).count() == 1
        assert SubscriptionItem.query.get(item.id).quantity == 2
