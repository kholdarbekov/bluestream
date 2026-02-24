"""End-to-end API journey tests for key customer flows."""

import pytest


@pytest.mark.integration
@pytest.mark.api
class TestCustomerJourneyE2E:
    def test_login_refresh_profile_products_orders_and_addresses(self, client, sample_user, sample_product):
        login = client.post(
            "/api/v1/auth/login",
            json={"identifier": sample_user.email, "password": "TestPassword123!"},
        )
        assert login.status_code == 200
        login_body = login.get_json()["data"]
        access_token = login_body["tokens"]["access_token"]
        refresh_token = login_body["tokens"]["refresh_token"]
        headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}

        profile = client.get("/api/v1/auth/profile", headers=headers)
        assert profile.status_code == 200
        assert sample_user.email in profile.get_data(as_text=True)

        refresh = client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
        assert refresh.status_code == 200
        assert "access_token" in refresh.get_json()["data"]

        products = client.get("/api/v1/products/")
        assert products.status_code == 200
        assert products.get_json()["success"] is True

        product_detail = client.get(f"/api/v1/products/{sample_product.id}")
        assert product_detail.status_code == 200
        assert product_detail.get_json()["data"]["product"]["id"] == sample_product.id

        orders = client.get("/api/v1/orders/", headers=headers)
        assert orders.status_code == 200
        assert orders.get_json()["success"] is True

        create_address = client.post(
            "/api/v1/addresses/",
            json={"full_address": "Yakkasaroy district, Tashkent", "title": "Home"},
            headers=headers,
        )
        assert create_address.status_code == 201
        address_id = create_address.get_json()["data"]["address"]["id"]

        addresses = client.get("/api/v1/addresses/", headers=headers)
        assert addresses.status_code == 200
        assert any(addr["id"] == address_id for addr in addresses.get_json()["data"]["addresses"])

    def test_register_login_update_profile_and_logout(self, client, db):
        register_payload = {
            "email": "e2e-register@example.com",
            "phone": "+998901239901",
            "password": "StrongPass123!",
            "first_name": "Flow",
            "last_name": "Tester",
        }
        register = client.post("/api/v1/auth/register", json=register_payload)
        assert register.status_code == 201
        assert register.get_json()["success"] is True

        login = client.post(
            "/api/v1/auth/login",
            json={"identifier": register_payload["email"], "password": register_payload["password"]},
        )
        assert login.status_code == 200
        login_data = login.get_json()["data"]
        access_token = login_data["tokens"]["access_token"]
        headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}

        update = client.put(
            "/api/v1/auth/profile",
            json={
                "first_name": "Updated",
                "last_name": "Profile",
                "phone": "+998901230000",
            },
            headers=headers,
        )
        assert update.status_code == 200
        update_body = update.get_json()["data"]["user"]
        assert update_body["first_name"] == "Updated"
        assert update_body["last_name"] == "Profile"
        # Direct phone changes are blocked on this endpoint.
        assert update_body["phone"] == register_payload["phone"]

        profile = client.get("/api/v1/auth/profile", headers=headers)
        assert profile.status_code == 200
        assert profile.get_json()["data"]["first_name"] == "Updated"

        logout = client.post("/api/v1/auth/logout", headers=headers)
        assert logout.status_code == 200
        assert logout.get_json()["success"] is True

    def test_address_lifecycle_and_default_switch(self, client, auth_headers):
        create_one = client.post(
            "/api/v1/addresses/",
            json={"full_address": "Mirobod district, Tashkent", "title": "Home"},
            headers=auth_headers,
        )
        assert create_one.status_code == 201
        addr_one = create_one.get_json()["data"]["address"]["id"]

        create_two = client.post(
            "/api/v1/addresses/",
            json={"full_address": "Yunusobod district, Tashkent", "title": "Office"},
            headers=auth_headers,
        )
        assert create_two.status_code == 201
        addr_two = create_two.get_json()["data"]["address"]["id"]

        set_default = client.post(f"/api/v1/addresses/{addr_two}/set-default", headers=auth_headers)
        assert set_default.status_code == 200

        addresses = client.get("/api/v1/addresses/", headers=auth_headers)
        assert addresses.status_code == 200
        all_addresses = addresses.get_json()["data"]["addresses"]
        default_addresses = [addr for addr in all_addresses if addr["is_default"]]
        assert len(default_addresses) == 1
        assert default_addresses[0]["id"] == addr_two

        delete_first = client.delete(f"/api/v1/addresses/{addr_one}", headers=auth_headers)
        assert delete_first.status_code == 200
