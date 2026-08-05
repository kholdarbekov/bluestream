"""Task 8 — the COD cap's PLACE arm must actually fire at every enforcement point.

Task 6 built the two-armed cap in ``CashCollectionService`` (cap by PERSON *or*
by PLACE); nothing passed the delivery address, so the place arm was dead code.
These tests pin the plumbing: order creation, staff phone orders, both
payment-method menus, subscription billing, the switch-to-cash edit path, and
the documented PERSONAL_CARD_TRANSFER exemption.

Every "blocked" test is paired with an "ungrouped address behaves exactly as
before" regression guard.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from business_app.services.cash_collection_service import CashCollectionService
from business_app.services.order_payment_method_edit_service import (
    OrderPaymentMethodEditService,
)
from business_app.utils.exceptions import ValidationError
from shared.enums import PaymentMethod
from tests.unit._scope_money_helpers import (
    delivered_cod_order,
    make_address,
    make_place_group,
    make_user,
)


def _make_operator(db):
    """A staff user whose JWT satisfies ``require_staff_roles("operator")``."""
    operator = make_user(db)
    operator.staff_roles = ["operator"]
    db.session.commit()
    return operator


def _cap_the_place(db, address):
    """Group ``address`` with two coworkers who each carry an open office debt."""
    u1, u2 = make_user(db), make_user(db)
    a1, a2 = make_address(db, u1), make_address(db, u2)
    make_place_group(db, a1, a2, address)
    delivered_cod_order(db, u1, address=a1)
    delivered_cod_order(db, u2, address=a2)


def _fill_place_cap(db):
    """Two coworkers with open office debts + a clean third coworker."""
    u3 = make_user(db)
    a3 = make_address(db, u3)
    _cap_the_place(db, a3)
    return u3, a3


@pytest.mark.unit
class TestOrderCreationPlaceCap:
    def test_resolve_payment_method_blocks_cash_to_capped_place(self, db):
        from business_app.services.order_service import OrderService

        u3, a3 = _fill_place_cap(db)
        order_data = {
            "payment_method": "cash",
            "delivery_address": {"delivery_address_id": a3.id},
        }
        with pytest.raises(ValidationError) as exc:
            OrderService()._resolve_payment_method(
                order_data,
                user=u3,
                order_items=[],
                bypass_cod_check=False,
            )
        assert exc.value.error_code == "COD_DEBT_LIMIT_REACHED"

    def test_bypass_still_works(self, db):
        from business_app.services.order_service import OrderService

        u3, a3 = _fill_place_cap(db)
        order_data = {
            "payment_method": "cash",
            "delivery_address": {"delivery_address_id": a3.id},
        }
        method = OrderService()._resolve_payment_method(
            order_data, user=u3, order_items=[], bypass_cod_check=True
        )
        assert method == PaymentMethod.CASH

    def test_ungrouped_address_still_allowed(self, db):
        """Regression guard: an ungrouped address must behave exactly as before —
        the place arm is never evaluated, so a clean customer still gets CASH."""
        from business_app.services.order_service import OrderService

        u = make_user(db)
        addr = make_address(db, u)
        method = OrderService()._resolve_payment_method(
            {"payment_method": "cash", "delivery_address": {"delivery_address_id": addr.id}},
            user=u,
            order_items=[],
            bypass_cod_check=False,
        )
        assert method == PaymentMethod.CASH

    def test_absent_or_null_delivery_address_does_not_crash(self, db):
        """``order_data`` with no delivery_address (or an explicit None) must not
        explode — the cap simply degrades to the person arm."""
        from business_app.services.order_service import OrderService

        u = make_user(db)
        assert (
            OrderService()._resolve_payment_method(
                {"payment_method": "cash"}, user=u, order_items=[], bypass_cod_check=False
            )
            == PaymentMethod.CASH
        )
        assert (
            OrderService()._resolve_payment_method(
                {"payment_method": "cash", "delivery_address": None},
                user=u,
                order_items=[],
                bypass_cod_check=False,
            )
            == PaymentMethod.CASH
        )

    def test_create_order_end_to_end_blocked_by_place_cap(
        self, db, app, sample_user, sample_product, user_address
    ):
        """Full create_order path, not just the private resolver."""
        from business_app.services.order_service import OrderService

        _cap_the_place(db, user_address)
        with pytest.raises(ValidationError) as exc:
            OrderService().create_order(
                sample_user.id,
                {
                    "items": [{"product_id": sample_product.id, "quantity": 2}],
                    "delivery_address": {
                        "delivery_address_id": user_address.id,
                        "street": "1 Test St",
                        "latitude": 41.3111,
                        "longitude": 69.2797,
                    },
                    "payment_method": "cash",
                },
            )
        assert exc.value.error_code == "COD_DEBT_LIMIT_REACHED"


@pytest.mark.unit
class TestStaffPhoneOrderPlaceCap:
    def test_phone_order_blocked_by_place_cap(self, db, app, sample_user, sample_product, user_address):
        from business_app.services.staff_service import StaffService

        _cap_the_place(db, user_address)
        operator = make_user(db)
        with pytest.raises(ValidationError) as exc:
            StaffService.create_phone_order(
                operator_id=operator.id,
                client_id=sample_user.id,
                order_data={
                    "items": [{"product_id": sample_product.id, "quantity": 2}],
                    "delivery_address_id": user_address.id,
                    "payment_method": "cash",
                },
            )
        assert exc.value.error_code == "COD_DEBT_LIMIT_REACHED"

    def test_phone_order_to_ungrouped_address_unaffected(
        self, db, app, sample_user, sample_product, user_address
    ):
        """Regression guard: no place group ⇒ no new rejection."""
        from business_app.services.staff_service import StaffService

        operator = make_user(db)
        order = StaffService.create_phone_order(
            operator_id=operator.id,
            client_id=sample_user.id,
            order_data={
                "items": [{"product_id": sample_product.id, "quantity": 2}],
                "delivery_address_id": user_address.id,
                "payment_method": "cash",
            },
        )
        assert order.payment_method == PaymentMethod.CASH


@pytest.mark.unit
class TestPaymentMenusPlaceCap:
    def test_payment_service_menu_hides_cash_for_capped_place(self, db, app):
        from business_app.services.payment_service import PaymentService

        u3, a3 = _fill_place_cap(db)
        methods = PaymentService().get_available_payment_methods(
            u3, delivery_address_id=a3.id
        )
        assert all(m["method"] != PaymentMethod.CASH.value for m in methods)

    def test_payment_service_menu_keeps_cash_without_address(self, db, app):
        """No address ⇒ person arm only ⇒ byte-identical to today."""
        from business_app.services.payment_service import PaymentService

        u3, _a3 = _fill_place_cap(db)
        methods = PaymentService().get_available_payment_methods(u3)
        assert any(m["method"] == PaymentMethod.CASH.value for m in methods)

    def test_staff_menu_hides_cash_for_capped_place(self, db, app):
        from business_app.services.staff_service import StaffService

        u3, a3 = _fill_place_cap(db)
        payload = StaffService.get_client_payment_methods(u3.id, delivery_address_id=a3.id)
        assert all(
            m["method"] != PaymentMethod.CASH.value for m in payload["available_methods"]
        )
        assert payload["payment_restrictions"]["restriction_scope"] == "place"

    def test_staff_menu_keeps_cash_without_address(self, db, app):
        from business_app.services.staff_service import StaffService

        u3, _a3 = _fill_place_cap(db)
        payload = StaffService.get_client_payment_methods(u3.id)
        assert any(
            m["method"] == PaymentMethod.CASH.value for m in payload["available_methods"]
        )
        assert payload["payment_restrictions"]["restriction_scope"] is None


def _auth(app, user_id, role=None):
    from flask_jwt_extended import create_access_token

    claims = {"role": role} if role else None
    token = create_access_token(identity=str(user_id), additional_claims=claims)
    return {"Authorization": f"Bearer {token}"}


def _group_under_cap(db, address):
    """Group ``address`` with ONE coworker carrying a single open debt.

    The place is grouped but below the cap, so the place arm is *evaluated*
    (count is an int, not None) without blocking — the exact observable that
    separates a place-aware payload from an address-less one.
    """
    coworker = make_user(db)
    coworker_addr = make_address(db, coworker)
    make_place_group(db, coworker_addr, address)
    delivered_cod_order(db, coworker, address=coworker_addr)


@pytest.mark.unit
class TestCheckoutPaymentRestrictionsPayload:
    def test_methods_endpoint_is_place_aware(self, db, app, client):
        """GET /payments/methods?delivery_address_id=N must reach BOTH the menu
        builder and the payment_restrictions context with the address."""
        u3, a3 = _fill_place_cap(db)
        resp = client.get(
            f"/api/v1/payments/methods?delivery_address_id={a3.id}",
            headers=_auth(app, u3.id),
        )
        assert resp.status_code == 200
        body = resp.get_json()["data"]
        assert body["payment_restrictions"]["restriction_scope"] == "place"
        assert all(m["method"] != PaymentMethod.CASH.value for m in body["available_methods"])

    def test_methods_endpoint_without_address_unchanged(self, db, app, client):
        """Regression guard: omitting the param keeps today's person-only answer."""
        u3, _a3 = _fill_place_cap(db)
        resp = client.get("/api/v1/payments/methods", headers=_auth(app, u3.id))
        assert resp.status_code == 200
        body = resp.get_json()["data"]
        assert body["payment_restrictions"]["restriction_scope"] is None
        assert body["payment_restrictions"]["place_active_cod_debt_count"] is None
        assert any(m["method"] == PaymentMethod.CASH.value for m in body["available_methods"])

    def test_methods_endpoint_own_grouped_address_is_place_aware(self, db, app, client):
        """Sanity re-check of the feature this parameter exists for: the caller's
        OWN grouped address still produces the place-aware payload."""
        u3, a3 = _fill_place_cap(db)
        resp = client.get(
            f"/api/v1/payments/methods?delivery_address_id={a3.id}",
            headers=_auth(app, u3.id),
        )
        assert resp.status_code == 200
        body = resp.get_json()["data"]
        assert body["payment_restrictions"]["place_active_cod_debt_count"] == 2
        assert body["payment_restrictions"]["restriction_scope"] == "place"

    def test_methods_endpoint_rejects_stranger_address(self, db, app, client):
        """A customer must not be able to read another place's COD-debt count by
        passing an address id they don't own (information disclosure)."""
        _owner, capped_address = _fill_place_cap(db)
        stranger = make_user(db)
        resp = client.get(
            f"/api/v1/payments/methods?delivery_address_id={capped_address.id}",
            headers=_auth(app, stranger.id),
        )
        assert resp.status_code != 500
        # The leak this closes: the stranger's response must not carry the
        # capped place's debt count under any status code.
        body = resp.get_json()
        assert "place_active_cod_debt_count" not in (body.get("data") or {}).get("payment_restrictions", {}) or (
            body["data"]["payment_restrictions"]["place_active_cod_debt_count"] is None
        )
        if resp.status_code == 200:
            data = body["data"]
            assert data["payment_restrictions"]["restriction_scope"] is None
            assert any(m["method"] == PaymentMethod.CASH.value for m in data["available_methods"])
        else:
            assert resp.status_code == 400

    def test_methods_endpoint_nonexistent_address_no_500(self, db, app, client):
        """A bogus/non-existent address id must not crash the endpoint."""
        u3 = make_user(db)
        resp = client.get(
            "/api/v1/payments/methods?delivery_address_id=999999999",
            headers=_auth(app, u3.id),
        )
        assert resp.status_code != 500

    def test_staff_methods_endpoint_is_place_aware(self, db, app, client, sample_user):
        from business_app.models.user import UserAddress

        address = UserAddress(
            user_id=sample_user.id, full_address="Office", city="Tashkent", latitude=41.31, longitude=69.28
        )
        db.session.add(address)
        db.session.commit()
        _cap_the_place(db, address)

        operator = _make_operator(db)
        resp = client.get(
            f"/api/v1/staff/operator/users/{sample_user.id}/payment-methods"
            f"?delivery_address_id={address.id}",
            headers=_auth(app, operator.id),
        )
        assert resp.status_code == 200
        body = resp.get_json()["data"]
        assert body["payment_restrictions"]["restriction_scope"] == "place"
        assert all(m["method"] != PaymentMethod.CASH.value for m in body["available_methods"])

    def test_admin_methods_endpoint_is_place_aware(self, db, app, client, sample_user, admin_user):
        """The admin order-builder menu shares StaffService.get_client_payment_methods
        with the operator route and must take the same optional address."""
        from business_app.models.user import UserAddress

        address = UserAddress(
            user_id=sample_user.id, full_address="Office", city="Tashkent", latitude=41.31, longitude=69.28
        )
        db.session.add(address)
        db.session.commit()
        _cap_the_place(db, address)

        resp = client.get(
            f"/api/v1/admin/users/{sample_user.id}/payment-methods?delivery_address_id={address.id}",
            headers=_auth(app, admin_user.id, role="admin"),
        )
        assert resp.status_code == 200
        body = resp.get_json()["data"]
        assert body["payment_restrictions"]["restriction_scope"] == "place"
        assert all(m["method"] != PaymentMethod.CASH.value for m in body["available_methods"])

    def test_admin_methods_endpoint_without_address_unchanged(
        self, db, app, client, sample_user, admin_user
    ):
        from business_app.models.user import UserAddress

        address = UserAddress(
            user_id=sample_user.id, full_address="Office", city="Tashkent", latitude=41.31, longitude=69.28
        )
        db.session.add(address)
        db.session.commit()
        _cap_the_place(db, address)

        resp = client.get(
            f"/api/v1/admin/users/{sample_user.id}/payment-methods",
            headers=_auth(app, admin_user.id, role="admin"),
        )
        assert resp.status_code == 200
        body = resp.get_json()["data"]
        assert body["payment_restrictions"]["restriction_scope"] is None
        assert any(m["method"] == PaymentMethod.CASH.value for m in body["available_methods"])

    def test_order_create_response_restrictions_are_place_aware(
        self, db, app, client, sample_user, sample_product, user_address
    ):
        """api/orders.py builds ``payment_restrictions`` from the ORDER's address:
        a grouped destination makes the place arm observable in the payload."""
        _group_under_cap(db, user_address)
        sample_user.phone_verified_at = datetime.now(timezone.utc)
        db.session.commit()
        resp = client.post(
            "/api/v1/orders/",
            json={
                "items": [{"product_id": sample_product.id, "quantity": 2}],
                "delivery_address_id": user_address.id,
                "payment_method": "cash",
            },
            headers=_auth(app, sample_user.id),
        )
        assert resp.status_code == 201, resp.get_json()
        restrictions = resp.get_json()["data"]["payment_restrictions"]
        assert restrictions["place_active_cod_debt_count"] == 1
        assert restrictions["cod_restricted"] is False

    def test_retry_with_cash_response_restrictions_are_place_aware(
        self, db, app, client, sample_user, sample_product, user_address
    ):
        """The rescue path deliberately BYPASSES the cap, but its response
        payload must still report the place arm truthfully."""
        from business_app.models.order import Order, OrderItem, OrderStatusHistory
        from shared.enums import OrderStatus

        _cap_the_place(db, user_address)

        cancelled = Order(
            user_id=sample_user.id,
            order_number="ORD-T8-RESCUE",
            status=OrderStatus.CANCELLED,
            subtotal=Decimal("30000.00"),
            delivery_fee=Decimal("0.00"),
            discount_amount=Decimal("0.00"),
            loyalty_discount=Decimal("0.00"),
            total_amount=Decimal("30000.00"),
            payment_method=PaymentMethod.CLICK,
            delivery_address_id=user_address.id,
        )
        db.session.add(cancelled)
        db.session.flush()
        db.session.add(
            OrderItem(
                order_id=cancelled.id,
                product_id=sample_product.id,
                quantity=2,
                unit_price=sample_product.base_price,
                total_price=sample_product.base_price * 2,
            )
        )
        db.session.add(
            OrderStatusHistory(
                order_id=cancelled.id,
                old_status=OrderStatus.PENDING,
                new_status=OrderStatus.CANCELLED,
                notes="tax_committee_unavailable",
            )
        )
        db.session.commit()

        resp = client.post(
            f"/api/v1/orders/{cancelled.id}/retry-with-cash",
            headers=_auth(app, sample_user.id),
        )
        assert resp.status_code == 201, resp.get_json()
        restrictions = resp.get_json()["data"]["payment_restrictions"]
        assert restrictions["restriction_scope"] == "place"
        assert restrictions["place_active_cod_debt_count"] >= 2


@pytest.mark.unit
class TestSwitchToCashEnforcement:
    def _ba_order(self, db, user, address=None):
        order, payment = delivered_cod_order(db, user, address=address)
        order.payment_method = PaymentMethod.BUSINESS_ACCOUNT
        payment.payment_method = PaymentMethod.BUSINESS_ACCOUNT
        db.session.commit()
        return order

    def test_unwind_to_cash_blocked_when_over_cap(self, db):
        u = make_user(db)
        delivered_cod_order(db, u)
        delivered_cod_order(db, u)  # cluster at the limit
        order = self._ba_order(db, u)
        plan = OrderPaymentMethodEditService().preview(order_id=order.id, new_method="cash")
        assert any(r.startswith("cod_debt_limit_reached") for r in plan.blocking_reasons)

    def test_unwind_to_cash_bypass_override(self, db):
        u = make_user(db)
        delivered_cod_order(db, u)
        delivered_cod_order(db, u)
        order = self._ba_order(db, u)
        plan = OrderPaymentMethodEditService().preview(
            order_id=order.id, new_method="cash", bypass_cod_check=True
        )
        assert not any(r.startswith("cod_debt_limit_reached") for r in plan.blocking_reasons)

    def test_unwind_to_cash_blocked_by_place_arm(self, db):
        """The PLACE arm reaches the switch-to-cash path via the ORDER's own
        delivery address — the orderer's own cluster owes nothing here."""
        u3, a3 = _fill_place_cap(db)
        order = self._ba_order(db, u3, address=a3)
        plan = OrderPaymentMethodEditService().preview(order_id=order.id, new_method="cash")
        assert any(r.startswith("cod_debt_limit_reached") for r in plan.blocking_reasons)

    def test_clean_customer_switch_to_cash_unaffected(self, db):
        """Regression guard: no debts, ungrouped address ⇒ no new blocking reason
        and the target is still offered by get_edit_metadata."""
        from business_app.models.order import Order

        u = make_user(db)
        order = self._ba_order(db, u, address=make_address(db, u))
        plan = OrderPaymentMethodEditService().preview(order_id=order.id, new_method="cash")
        assert not any(r.startswith("cod_debt_limit_reached") for r in plan.blocking_reasons)
        metadata = OrderPaymentMethodEditService().get_edit_metadata(Order.query.get(order.id))
        assert "cash" in metadata["allowed_target_methods"]

    def test_edit_metadata_drops_cash_for_capped_customer(self, db):
        from business_app.models.order import Order

        u = make_user(db)
        delivered_cod_order(db, u)
        delivered_cod_order(db, u)
        order = self._ba_order(db, u)
        metadata = OrderPaymentMethodEditService().get_edit_metadata(Order.query.get(order.id))
        assert "cash" not in metadata["allowed_target_methods"]

    def test_apply_edit_refuses_when_over_cap(self, db):
        u = make_user(db)
        delivered_cod_order(db, u)
        delivered_cod_order(db, u)
        order = self._ba_order(db, u)
        with pytest.raises(ValidationError) as exc:
            OrderPaymentMethodEditService().apply_edit(
                order_id=order.id,
                new_method="cash",
                reason="operator correction",
                actor_user_id=u.id,
            )
        assert "cod_debt_limit_reached" in str(exc.value)

    def test_apply_edit_bypass_is_audited(self, db, monkeypatch):
        """The override must be recorded on the unwind's audit event."""
        from business_app.utils import audit_logger as audit_module

        events = []
        monkeypatch.setattr(
            audit_module.audit_logger,
            "log_event",
            lambda **kwargs: events.append(kwargs),
        )

        u = make_user(db)
        delivered_cod_order(db, u)
        delivered_cod_order(db, u)
        order = self._ba_order(db, u)
        OrderPaymentMethodEditService().apply_edit(
            order_id=order.id,
            new_method="cash",
            reason="operator correction",
            actor_user_id=u.id,
            bypass_cod_check=True,
        )
        unwind_events = [
            e
            for e in events
            if e.get("action") == "order_payment_method_changed"
            and e.get("additional_data", {}).get("to_method") == "cash"
        ]
        assert unwind_events, "unwind audit event not emitted"
        assert unwind_events[-1]["additional_data"]["bypass_cod_check"] is True


@pytest.mark.unit
class TestConvertElectronicExemption:
    def test_pct_conversion_exempt_from_cap(self, db):
        """convert_electronic_order_to_cash inside the PCT resolution creates a
        debt settled by the same transfer in the same transaction — no cap check."""
        u, admin = make_user(db), make_user(db)
        delivered_cod_order(db, u)
        delivered_cod_order(db, u)  # over the cap
        order, payment = delivered_cod_order(db, u, total=Decimal("5000.00"))
        order.payment_method = PaymentMethod.CLICK
        payment.payment_method = PaymentMethod.CLICK
        db.session.commit()
        event = CashCollectionService().post_collection(
            customer_id=u.id,
            amount=Decimal("5000.00"),
            source="personal_card_transfer",
            order_id=order.id,
            recorded_by_user_id=admin.id,
            notes="card transfer",
        )
        assert event is not None
        db.session.refresh(order)
        assert order.payment_method == PaymentMethod.CASH


@pytest.mark.unit
class TestSubscriptionPlaceCapSkip:
    def test_billing_skip_error_code_contract(self, db, app):
        """The place cap reaches subscription billing through create_order.
        ``subscription_service`` branches ``_skip_cycle_for_cod_debt`` on this
        exact error_code — asserting it here pins the contract."""
        from business_app.services.order_service import OrderService

        u3, a3 = _fill_place_cap(db)
        with pytest.raises(ValidationError) as exc:
            OrderService()._resolve_payment_method(
                {
                    "payment_method": "cash",
                    "delivery_address": {"delivery_address_id": a3.id},
                },
                user=u3,
                order_items=[],
                bypass_cod_check=False,
            )
        assert exc.value.error_code == "COD_DEBT_LIMIT_REACHED"

    def _subscription(self, db, user, product, address):
        from business_app.models.subscription import Subscription, SubscriptionItem
        from shared.enums import SubscriptionFrequency, SubscriptionStatus

        subscription = Subscription(
            user_id=user.id,
            name="Weekly Water",
            status=SubscriptionStatus.ACTIVE,
            billing_cycle=SubscriptionFrequency.WEEKLY,
            delivery_frequency=SubscriptionFrequency.WEEKLY,
            delivery_address_id=address.id,
            payment_method=PaymentMethod.CASH,
            auto_renew=True,
            billing_amount=Decimal("0.00"),
            start_date=datetime.now(timezone.utc),
            next_billing_date=datetime.now(timezone.utc) - timedelta(minutes=1),
        )
        db.session.add(subscription)
        db.session.flush()
        db.session.add(
            SubscriptionItem(
                subscription_id=subscription.id,
                product_id=product.id,
                quantity=2,
                unit_price=product.base_price,
                total_price=product.base_price * 2,
            )
        )
        db.session.commit()
        return subscription

    def test_billing_skips_cycle_under_place_cap_with_exact_shape(
        self, db, app, sample_user, sample_product, user_address
    ):
        """End-to-end: a subscription delivering to a capped PLACE skips the
        cycle with the exact documented shape, leaves the subscription ACTIVE,
        never increments failed_payment_count, and creates no order."""
        from business_app.models.order import Order
        from business_app.models.subscription import Subscription
        from business_app.services.subscription_service import SubscriptionService
        from shared.enums import SubscriptionStatus

        _cap_the_place(db, user_address)
        subscription = self._subscription(db, sample_user, sample_product, user_address)
        before_failures = subscription.failed_payment_count or 0
        before_next = subscription.next_billing_date

        result = SubscriptionService().process_subscription_billing(subscription.id)

        assert result["success"] is False
        assert result["skipped"] is True
        assert result["reason"] == "cod_debt_limit"

        refreshed = Subscription.query.get(subscription.id)
        assert refreshed.status is SubscriptionStatus.ACTIVE
        assert (refreshed.failed_payment_count or 0) == before_failures
        assert refreshed.next_billing_date > before_next
        assert Order.query.filter_by(subscription_id=subscription.id).count() == 0

    def test_billing_to_ungrouped_address_still_succeeds(
        self, db, app, sample_user, sample_product, user_address
    ):
        """Regression guard: with no place group the cycle bills normally."""
        from business_app.services.subscription_service import SubscriptionService

        subscription = self._subscription(db, sample_user, sample_product, user_address)
        result = SubscriptionService().process_subscription_billing(subscription.id)
        assert result["success"] is True
        assert "order_id" in result
