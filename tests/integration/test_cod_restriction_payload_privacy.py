"""Task 30A privacy pin: a customer's ``payment_restrictions`` payload must
never carry a coworker's money.

``get_cod_restriction_context`` publishes ``place_net_open_cod_debt_total`` —
a money total aggregated over OTHER people's debts at a shared workplace. In
a two-member address group that figure is one coworker's EXACT outstanding
balance. Spec §7 lets only the place-scope COUNT cross that boundary; this
test seeds exactly the shape where a leak is loud and unmistakable: the
caller's own account is completely clean (zero debts, zero money) and a
single coworker at the same place carries a real, non-zero, identifiable
total. A regression exposes that coworker's exact number, not a coincidental
zero.

Covers the three customer JWT-authenticated surfaces that return
``get_cod_restriction_context()`` as ``payment_restrictions``:
  * GET  /api/v1/payments/methods            (business_app/api/payments.py)
  * POST /api/v1/orders/                     (business_app/api/orders.py)
  * POST /api/v1/orders/<id>/retry-with-cash (business_app/api/orders.py)

The debts are kept UNDER ``COD_DEBT_AMOUNT_THRESHOLD`` on purpose: the place
arm must stay UNRESTRICTED (cash still offered, order creation still
succeeds) so every scenario actually reaches the code path that builds
``payment_restrictions`` — a restricted place would 400 the cash order before
ever exercising the leak.
"""

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import Mock

import pytest
from flask_jwt_extended import create_access_token

from business_app.models.order import Order
from business_app.models.user import UserAddress
from shared.business_config import COD_ACTIVE_DEBT_LIMIT
from shared.enums import OrderStatus, PaymentMethod
from tests.unit._scope_money_helpers import (
    delivered_cod_order,
    make_address,
    make_place_group,
    make_user,
)

CASH = "cash"


@pytest.fixture
def orderer(db, sample_user):
    """sample_user, phone-verified so POST /orders passes @require_verification."""
    sample_user.phone_verified_at = datetime.now(UTC)
    db.session.commit()
    return sample_user


def _cap_the_place_with_coworker_debt(db, address, *, each=Decimal("280.00")):
    """Group ``address`` with ONE coworker who alone carries
    ``COD_ACTIVE_DEBT_LIMIT`` delivered-unpaid debts. Kept under
    ``COD_DEBT_AMOUNT_THRESHOLD`` (280 UZS shortfalls, the plan's own
    motivating case) so the place arm does NOT restrict — cash stays offered
    and orders succeed. The orderer never carries any debt of their own: any
    money figure the response carries for this place can only be the
    coworker's."""
    coworker = make_user(db)
    coworker_address = make_address(db, coworker)
    make_place_group(db, coworker_address, address)
    for _ in range(COD_ACTIVE_DEBT_LIMIT):
        delivered_cod_order(db, coworker, address=coworker_address, total=each)
    return coworker


@pytest.mark.integration
class TestPaymentMethodsNeverLeaksPlaceScopeMoney:
    def test_get_payment_methods_carries_the_count_never_the_money(
        self, app, db, client, orderer, user_address, auth_headers
    ):
        _cap_the_place_with_coworker_debt(db, user_address)

        resp = client.get(
            f"/api/v1/payments/methods?delivery_address_id={user_address.id}",
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.get_json()
        restrictions = resp.get_json()["data"]["payment_restrictions"]

        # Sanity: the place arm is genuinely in play (a real seeded debt, a
        # real count) and NOT restricted -- this isn't a degenerate all-zero
        # case, and cash is still on offer so this is a realistic checkout.
        assert restrictions["place_active_cod_debt_count"] == COD_ACTIVE_DEBT_LIMIT
        assert restrictions["cod_restricted"] is False

        # Spec §7: only the COUNT may cross this boundary -- the coworker's
        # money must never be in this payload, not even as null/zero. The key
        # itself must be gone.
        assert "place_net_open_cod_debt_total" not in restrictions
        # The caller's OWN money is fine to publish -- proves this is a
        # targeted strip, not a blanket filter that also breaks the person arm.
        assert restrictions["cluster_net_open_cod_debt_total"] == 0.0


@pytest.mark.integration
class TestCreateOrderNeverLeaksPlaceScopeMoney:
    def test_post_orders_payment_restrictions_carries_the_count_never_the_money(
        self, app, db, client, orderer, sample_product, user_address, auth_headers
    ):
        _cap_the_place_with_coworker_debt(db, user_address)

        resp = client.post(
            "/api/v1/orders/",
            json={
                "items": [{"product_id": sample_product.id, "quantity": 2}],
                "delivery_address_id": user_address.id,
                "payment_method": CASH,
            },
            headers=auth_headers,
        )
        assert resp.status_code == 201, resp.get_json()
        restrictions = resp.get_json()["data"]["payment_restrictions"]

        assert restrictions["place_active_cod_debt_count"] == COD_ACTIVE_DEBT_LIMIT
        assert "place_net_open_cod_debt_total" not in restrictions
        assert restrictions["cluster_net_open_cod_debt_total"] == 0.0


@pytest.mark.integration
class TestRetryOrderWithCashNeverLeaksPlaceScopeMoney:
    def test_retry_with_cash_payment_restrictions_carries_the_count_never_the_money(
        self, app, db, client, sample_user, monkeypatch
    ):
        """business_app/api/orders.py's retry-with-cash route builds
        ``payment_restrictions`` unconditionally (no payment-method gate), so
        it is exercised with the real ``CashCollectionService`` while
        ``order_service`` itself is mocked out -- the same technique
        tests/unit/test_orders_api_routes.py uses for this route."""
        address = UserAddress(
            user_id=sample_user.id,
            title="Home",
            full_address="Street 1",
            street_address="Street 1",
            city="Tashkent",
            latitude=41.31,
            longitude=69.28,
            is_default=True,
        )
        db.session.add(address)
        db.session.flush()
        order = Order(
            order_number="ORD-PRIV-1",
            user_id=sample_user.id,
            status=OrderStatus.PENDING,
            subtotal=Decimal("10000"),
            delivery_fee=Decimal("0"),
            total_amount=Decimal("10000"),
            delivery_address_id=address.id,
            payment_method=PaymentMethod.CASH,
            order_source="web",
            created_at=datetime.now(UTC),
        )
        db.session.add(order)
        db.session.commit()

        _cap_the_place_with_coworker_debt(db, address)

        service = Mock()
        service.rescue_order_after_psp_failure.return_value = order
        monkeypatch.setattr("business_app.api.orders.get_order_service", lambda: service)
        monkeypatch.setattr("business_app.api.orders.get_cart_service", lambda: Mock())

        with app.app_context():
            token = create_access_token(identity=str(sample_user.id))

        resp = client.post(
            f"/api/v1/orders/{order.id}/retry-with-cash",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 201, resp.get_json()
        restrictions = resp.get_json()["data"]["payment_restrictions"]

        assert restrictions["place_active_cod_debt_count"] == COD_ACTIVE_DEBT_LIMIT
        assert "place_net_open_cod_debt_total" not in restrictions
        assert restrictions["cluster_net_open_cod_debt_total"] == 0.0
