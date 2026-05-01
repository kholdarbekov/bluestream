"""Integration tests for address management API endpoints."""

from datetime import datetime, UTC, timedelta
from decimal import Decimal

import pytest

from business_app.models.subscription import Subscription
from business_app.models.user import UserAddress
from shared.enums import PaymentMethod, SubscriptionFrequency
def _create_address(client, headers, **overrides):
    payload = {
        "title": "Home",
        "full_address": "Amir Temur street 10, Tashkent",
        "city": "Tashkent",
        "district": "Yunusobod",
        "is_default": False,
    }
    payload.update(overrides)
    return client.post("/api/v1/addresses/", json=payload, headers=headers)


@pytest.mark.integration
@pytest.mark.api
class TestAddressesAPI:
    def test_addresses_require_authentication(self, app):
        isolated = app.test_client(use_cookies=False)
        response = isolated.get("/api/v1/addresses/")
        assert response.status_code == 401

    def test_create_and_list_addresses(self, client, auth_headers, db):
        create_response = _create_address(client, auth_headers)
        assert create_response.status_code == 201
        create_body = create_response.get_json()
        assert create_body["success"] is True
        assert create_body["data"]["address"]["full_address"].startswith("Amir Temur")
        assert create_body["data"]["address"]["is_default"] is True  # first address is forced default

        list_response = client.get("/api/v1/addresses/", headers=auth_headers)
        assert list_response.status_code == 200
        list_body = list_response.get_json()
        assert list_body["success"] is True
        assert len(list_body["data"]["addresses"]) == 1

    def test_create_requires_address_or_coordinates(self, client, auth_headers):
        response = client.post("/api/v1/addresses/", json={"title": "Invalid"}, headers=auth_headers)
        assert response.status_code == 400
        assert response.get_json()["success"] is False

    def test_get_update_and_set_default_address(self, client, auth_headers, db):
        first = _create_address(client, auth_headers, title="Home", is_default=False).get_json()["data"]["address"]
        second = _create_address(client, auth_headers, title="Work", is_default=False).get_json()["data"]["address"]

        update_response = client.put(
            f"/api/v1/addresses/{second['id']}",
            json={"full_address": "Updated address 22", "is_default": True},
            headers=auth_headers,
        )
        assert update_response.status_code == 200
        assert update_response.get_json()["data"]["address"]["is_default"] is True

        set_default_response = client.post(f"/api/v1/addresses/{first['id']}/set-default", headers=auth_headers)
        assert set_default_response.status_code == 200
        assert set_default_response.get_json()["data"]["address"]["id"] == first["id"]

        get_one = client.get(f"/api/v1/addresses/{first['id']}", headers=auth_headers)
        assert get_one.status_code == 200
        assert get_one.get_json()["data"]["address"]["id"] == first["id"]

        with db.session.no_autoflush:
            addresses = UserAddress.query.order_by(UserAddress.id.asc()).all()
        defaults = [addr for addr in addresses if addr.is_default]
        assert len(defaults) == 1
        assert defaults[0].id == first["id"]

    def test_delete_only_address_is_blocked_and_delete_with_multiple_is_allowed(self, client, auth_headers):
        created = _create_address(client, auth_headers).get_json()["data"]["address"]

        delete_only = client.delete(f"/api/v1/addresses/{created['id']}", headers=auth_headers)
        assert delete_only.status_code == 400
        assert delete_only.get_json()["success"] is False

        second = _create_address(client, auth_headers, title="Secondary", full_address="Other 1")
        assert second.status_code == 201

        delete_response = client.delete(f"/api/v1/addresses/{created['id']}", headers=auth_headers)
        assert delete_response.status_code == 200
        assert delete_response.get_json()["success"] is True

    def test_delete_address_used_by_subscription_is_blocked(self, client, auth_headers, db, sample_user):
        _create_address(client, auth_headers, title="Primary", full_address="Main 1")
        linked = _create_address(
            client,
            auth_headers,
            title="Linked",
            full_address="Subscription street 2",
        ).get_json()["data"]["address"]

        subscription = Subscription(
            subscription_number='SUB-ADDR-TEST-1',
            user_id=sample_user.id,
            name='Address lock test',
            billing_cycle=SubscriptionFrequency.WEEKLY,
            billing_amount=Decimal('15000.00'),
            next_billing_date=datetime.now(UTC) + timedelta(days=7),
            delivery_frequency=SubscriptionFrequency.WEEKLY,
            delivery_address_id=linked["id"],
            start_date=datetime.now(UTC),
            payment_method=PaymentMethod.CARD,
        )
        db.session.add(subscription)
        db.session.commit()

        delete_response = client.delete(f"/api/v1/addresses/{linked['id']}", headers=auth_headers)
        assert delete_response.status_code == 400
        assert delete_response.get_json()["success"] is False

    def test_not_found_paths(self, client, auth_headers):
        get_missing = client.get("/api/v1/addresses/999999", headers=auth_headers)
        assert get_missing.status_code == 404

        update_missing = client.put("/api/v1/addresses/999999", json={"title": "x"}, headers=auth_headers)
        assert update_missing.status_code == 404

        delete_missing = client.delete("/api/v1/addresses/999999", headers=auth_headers)
        assert delete_missing.status_code == 404
