import pytest
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from business_app import db as _db
from business_app.models.subscription import Subscription, SubscriptionItem, SubscriptionLog
from business_app.models.user import UserAddress
from shared.enums import SubscriptionFrequency, SubscriptionStatus, PaymentMethod


def _make_address(db, user):
    addr = UserAddress(
        user_id=user.id, title="Home", full_address="Amir Temur 1, Tashkent",
        street_address="Amir Temur 1", city="Tashkent", latitude=41.311, longitude=69.279,
    )
    db.session.add(addr)
    db.session.commit()
    return addr


def _make_subscription(db, user, addr, product, *, number, status=SubscriptionStatus.ACTIVE):
    sub = Subscription(
        subscription_number=number, user_id=user.id, status=status, name="Standard",
        billing_cycle=SubscriptionFrequency.MONTHLY, billing_amount=Decimal("30000.00"),
        next_billing_date=datetime.now(UTC) + timedelta(days=7),
        delivery_frequency=SubscriptionFrequency.WEEKLY, delivery_address_id=addr.id,
        payment_method=PaymentMethod.CASH, start_date=datetime.now(UTC),
    )
    db.session.add(sub)
    db.session.flush()
    item = SubscriptionItem(
        subscription_id=sub.id, product_id=product.id, quantity=2, unit_price=product.base_price
    )
    item.calculate_total()
    db.session.add(item)
    db.session.commit()
    return sub


@pytest.mark.integration
class TestAdminCreateUpdateEndpoints:
    def test_create_subscription_returns_201(
        self, client, db, admin_auth_headers, sample_user, sample_product
    ):
        addr = _make_address(db, sample_user)
        resp = client.post(
            "/api/v1/admin/subscriptions",
            headers=admin_auth_headers,
            json={
                "user_id": sample_user.id,
                "name": "Weekly Water",
                "billing_cycle": "monthly",
                "delivery_frequency": "weekly",
                "delivery_address_id": addr.id,
                "payment_method": "cash",
                "items": [{"product_id": sample_product.id, "quantity": 2}],
            },
        )
        assert resp.status_code == 201
        sub = resp.get_json()["data"]["subscription"]
        assert sub["subscription_number"].startswith("SUB")

    def test_create_requires_admin_auth_401(self, client, db, sample_user):
        resp = client.post("/api/v1/admin/subscriptions", json={})
        assert resp.status_code == 401

    def test_create_forbidden_for_customer_403(
        self, client, db, auth_headers, sample_user, sample_product
    ):
        addr = _make_address(db, sample_user)
        resp = client.post(
            "/api/v1/admin/subscriptions",
            headers=auth_headers,  # customer JWT
            json={
                "user_id": sample_user.id, "name": "Weekly Water", "billing_cycle": "monthly",
                "delivery_frequency": "weekly", "delivery_address_id": addr.id,
                "payment_method": "cash", "items": [{"product_id": sample_product.id, "quantity": 2}],
            },
        )
        assert resp.status_code == 403

    def test_create_invalid_payload_400(self, client, db, admin_auth_headers):
        resp = client.post(
            "/api/v1/admin/subscriptions", headers=admin_auth_headers, json={"name": "x"}
        )
        assert resp.status_code == 400

    def test_update_subscription_name(
        self, client, db, admin_auth_headers, sample_user, sample_product
    ):
        addr = _make_address(db, sample_user)
        sub = _make_subscription(db, sample_user, addr, sample_product, number="SUB-E1")
        resp = client.put(
            f"/api/v1/admin/subscriptions/{sub.id}",
            headers=admin_auth_headers,
            json={"name": "Renamed by admin"},
        )
        assert resp.status_code == 200
        assert Subscription.query.get(sub.id).name == "Renamed by admin"

    def test_update_manual_billing_amount_requires_override(
        self, client, db, admin_auth_headers, sample_user, sample_product
    ):
        addr = _make_address(db, sample_user)
        sub = _make_subscription(db, sample_user, addr, sample_product, number="SUB-E2")
        # Without the override flag → billing_amount unchanged (item-derived 30000).
        client.put(
            f"/api/v1/admin/subscriptions/{sub.id}",
            headers=admin_auth_headers,
            json={"billing_amount": 12345.0},
        )
        assert float(Subscription.query.get(sub.id).billing_amount) == 30000.0
        # With the override flag → applied.
        client.put(
            f"/api/v1/admin/subscriptions/{sub.id}",
            headers=admin_auth_headers,
            json={"billing_amount": 12345.0, "override_manual_billing_amount": True},
        )
        assert float(Subscription.query.get(sub.id).billing_amount) == 12345.0

    def test_list_filters_by_billing_cycle(
        self, client, db, admin_auth_headers, sample_user, sample_product
    ):
        addr = _make_address(db, sample_user)
        _make_subscription(db, sample_user, addr, sample_product, number="SUB-E3")
        resp = client.get(
            "/api/v1/admin/subscriptions?billing_cycle=monthly", headers=admin_auth_headers
        )
        assert resp.status_code == 200
        numbers = [s["subscription_number"] for s in resp.get_json()["data"]["items"]]
        assert "SUB-E3" in numbers
        resp2 = client.get(
            "/api/v1/admin/subscriptions?billing_cycle=daily", headers=admin_auth_headers
        )
        numbers2 = [s["subscription_number"] for s in resp2.get_json()["data"]["items"]]
        assert "SUB-E3" not in numbers2

    def test_get_subscription_detail_returns_200(
        self, client, db, admin_auth_headers, sample_user, sample_product
    ):
        addr = _make_address(db, sample_user)
        sub = _make_subscription(db, sample_user, addr, sample_product, number="SUB-E4")
        resp = client.get(f"/api/v1/admin/subscriptions/{sub.id}", headers=admin_auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()["data"]["subscription"]
        assert data["subscription_number"] == "SUB-E4"
        assert isinstance(data["status"], str)
        assert isinstance(data["payment_method"], str)

    def test_get_subscription_detail_with_time_slot_serializes(
        self, client, db, admin_auth_headers, sample_user, sample_product
    ):
        from business_app.models.delivery import DeliveryTimeSlot

        slot = DeliveryTimeSlot(name="Morning", start_time="09:00", end_time="12:00", is_active=True)
        db.session.add(slot)
        db.session.flush()
        addr = _make_address(db, sample_user)
        sub = _make_subscription(db, sample_user, addr, sample_product, number="SUB-E5")
        sub.delivery_time_slot_id = slot.id
        db.session.commit()
        resp = client.get(f"/api/v1/admin/subscriptions/{sub.id}", headers=admin_auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()["data"]["subscription"]
        assert data["delivery_time_slot_id"] == slot.id
        assert data["delivery_time_slot"]["id"] == slot.id

    def test_get_subscription_detail_with_orders_serializes(
        self, client, db, admin_auth_headers, sample_user, sample_product
    ):
        from decimal import Decimal
        from business_app.models.order import Order
        from shared.enums import OrderStatus

        addr = _make_address(db, sample_user)
        sub = _make_subscription(db, sample_user, addr, sample_product, number="SUB-E6")
        order = Order(
            user_id=sample_user.id,
            subscription_id=sub.id,
            order_number="ORD-SUB-E6",
            status=OrderStatus.PENDING,
            subtotal=Decimal("15000.00"),
            delivery_fee=Decimal("0.00"),
            discount_amount=Decimal("0.00"),
            loyalty_discount=Decimal("0.00"),
            total_amount=Decimal("15000.00"),
        )
        db.session.add(order)
        db.session.commit()
        resp = client.get(f"/api/v1/admin/subscriptions/{sub.id}", headers=admin_auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()["data"]["subscription"]
        assert len(data["recent_orders"]) == 1
        assert isinstance(data["recent_orders"][0]["status"], str)


@pytest.mark.integration
class TestAdminItemEndpoints:
    def test_update_item_changes_quantity_and_billing(
        self, client, db, admin_auth_headers, sample_user, sample_product
    ):
        addr = _make_address(db, sample_user)
        sub = _make_subscription(db, sample_user, addr, sample_product, number="SUB-IT1")
        item_id = sub.subscription_items[0].id
        resp = client.put(
            f"/api/v1/admin/subscriptions/{sub.id}/items/{item_id}",
            headers=admin_auth_headers,
            json={"quantity": 5},
        )
        assert resp.status_code == 200
        assert SubscriptionItem.query.get(item_id).quantity == 5
        # 15000 * 5 = 75000
        assert float(Subscription.query.get(sub.id).billing_amount) == 75000.0

    def test_remove_last_item_rejected_400(
        self, client, db, admin_auth_headers, sample_user, sample_product
    ):
        addr = _make_address(db, sample_user)
        sub = _make_subscription(db, sample_user, addr, sample_product, number="SUB-IT2")
        item_id = sub.subscription_items[0].id
        resp = client.delete(
            f"/api/v1/admin/subscriptions/{sub.id}/items/{item_id}",
            headers=admin_auth_headers,
        )
        assert resp.status_code == 400

    def test_add_duplicate_item_conflict_409(
        self, client, db, admin_auth_headers, sample_user, sample_product
    ):
        addr = _make_address(db, sample_user)
        sub = _make_subscription(db, sample_user, addr, sample_product, number="SUB-IT3")
        resp = client.post(
            f"/api/v1/admin/subscriptions/{sub.id}/items",
            headers=admin_auth_headers,
            json={"product_id": sample_product.id, "quantity": 1},
        )
        assert resp.status_code == 409


@pytest.mark.integration
class TestAdminLifecycleEndpointsAudit:
    def test_pause_writes_audit_log_with_admin_actor(
        self, client, db, admin_auth_headers, admin_user, sample_user, sample_product
    ):
        addr = _make_address(db, sample_user)
        sub = _make_subscription(db, sample_user, addr, sample_product, number="SUB-LC1")
        resp = client.post(
            f"/api/v1/admin/subscriptions/{sub.id}/pause",
            headers=admin_auth_headers,
            json={"pause_reason": "ops hold"},
        )
        assert resp.status_code == 200
        assert Subscription.query.get(sub.id).status == SubscriptionStatus.PAUSED
        log = SubscriptionLog.query.filter_by(subscription_id=sub.id, action="paused").first()
        assert log is not None
        assert log.user_id == admin_user.id

    def test_cancel_writes_cancelled_log(
        self, client, db, admin_auth_headers, admin_user, sample_user, sample_product
    ):
        addr = _make_address(db, sample_user)
        sub = _make_subscription(db, sample_user, addr, sample_product, number="SUB-LC2")
        resp = client.post(
            f"/api/v1/admin/subscriptions/{sub.id}/cancel",
            headers=admin_auth_headers,
            json={"cancellation_reason": "customer request"},
        )
        assert resp.status_code == 200
        assert Subscription.query.get(sub.id).status == SubscriptionStatus.CANCELLED
        assert SubscriptionLog.query.filter_by(
            subscription_id=sub.id, action="cancelled"
        ).first() is not None
