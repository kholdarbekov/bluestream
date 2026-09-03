"""The COD cap's amount arm, driven over HTTP on the real customer path.

The read surface that OFFERS cash (GET /api/v1/payments/methods) and the write
guard that ACCEPTS it (POST /api/v1/orders/) are ONE decision. Every case here
asserts both, because a widened offer is a promise the write path must honour.

Money figures are chosen relative to the config SSOT, never hardcoded against
the threshold's current value.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from flask_jwt_extended import create_access_token

from tests.unit._scope_money_helpers import delivered_cod_order, make_user
from shared.business_config import COD_ACTIVE_DEBT_LIMIT

CASH = "cash"


def _auth(app, user_id):
    with app.app_context():
        token = create_access_token(identity=str(user_id))
    return {"Authorization": f"Bearer {token}"}


def _cash_is_offered(client, app, user_id):
    resp = client.get("/api/v1/payments/methods", headers=_auth(app, user_id))
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()["data"]
    offered = any(m["method"] == CASH for m in body["available_methods"])
    return offered, body["payment_restrictions"]


def _post_cash_order(app, product_id, address_id, headers):
    return app.test_client().post(
        "/api/v1/orders/",
        json={
            "items": [{"product_id": product_id, "quantity": 2}],
            "delivery_address_id": address_id,
            "payment_method": CASH,
        },
        headers=headers,
    )


@pytest.fixture
def orderer(db, sample_user):
    """sample_user, phone-verified so POST /orders passes @require_verification."""
    sample_user.phone_verified_at = datetime.now(UTC)
    db.session.commit()
    return sample_user


@pytest.mark.integration
class TestPersonArmOverHttp:
    def test_two_tiny_debts_do_not_restrict(
        self, app, db, client, orderer, sample_product, user_address, auth_headers
    ):
        """The motivating case: two 280-sum shortfalls, 560 total."""
        for _ in range(COD_ACTIVE_DEBT_LIMIT):
            delivered_cod_order(db, orderer, total=Decimal("280.00"))

        offered, restrictions = _cash_is_offered(client, app, orderer.id)
        assert restrictions["active_cod_debt_count"] == COD_ACTIVE_DEBT_LIMIT
        assert restrictions["cod_restricted"] is False
        assert offered is True

        resp = _post_cash_order(app, sample_product.id, user_address.id, auth_headers)
        assert resp.status_code == 201, resp.get_json()

    def test_two_real_debts_restrict_on_both_surfaces(
        self, app, db, client, orderer, sample_product, user_address, auth_headers
    ):
        """2 debts of 6 000 = 12 000, over the amount floor."""
        for _ in range(COD_ACTIVE_DEBT_LIMIT):
            delivered_cod_order(db, orderer, total=Decimal("6000.00"))

        offered, restrictions = _cash_is_offered(client, app, orderer.id)
        assert restrictions["cod_restricted"] is True
        assert restrictions["restriction_scope"] == "person"
        assert offered is False

        resp = _post_cash_order(app, sample_product.id, user_address.id, auth_headers)
        assert resp.status_code == 400, resp.get_json()

    def test_one_large_debt_does_not_restrict(
        self, app, db, client, orderer, sample_product, user_address, auth_headers
    ):
        """50 000 owed, but only one debt: the count arm is not met."""
        delivered_cod_order(db, orderer, total=Decimal("50000.00"))

        offered, restrictions = _cash_is_offered(client, app, orderer.id)
        assert restrictions["cod_restricted"] is False
        assert offered is True

        resp = _post_cash_order(app, sample_product.id, user_address.id, auth_headers)
        assert resp.status_code == 201, resp.get_json()

    def test_context_publishes_the_net_total_it_gated_on(self, db, orderer):
        from business_app.services.cash_collection_service import CashCollectionService

        for _ in range(COD_ACTIVE_DEBT_LIMIT):
            delivered_cod_order(db, orderer, total=Decimal("280.00"))

        ctx = CashCollectionService().get_cod_restriction_context(orderer.id)
        assert ctx["cluster_net_open_cod_debt_total"] == 560.0


@pytest.mark.integration
class TestExemptionsStillTakePrecedence:
    def test_admin_exempt_customer_is_never_restricted(self, db):
        from business_app.services.cash_collection_service import CashCollectionService

        u = make_user(db, exempt=True)
        for _ in range(COD_ACTIVE_DEBT_LIMIT):
            delivered_cod_order(db, u, total=Decimal("60000.00"))

        ctx = CashCollectionService().get_cod_restriction_context(u.id)
        assert ctx["cod_restricted"] is False
        assert ctx["cod_restriction_reason"] == "customer_is_cod_exempt"

    def test_grocery_store_customer_is_never_restricted(self, db):
        from business_app.services.cash_collection_service import CashCollectionService

        u = make_user(db, grocery=True)
        for _ in range(COD_ACTIVE_DEBT_LIMIT):
            delivered_cod_order(db, u, total=Decimal("60000.00"))

        svc = CashCollectionService()
        assert svc.get_cod_restriction_context(u.id)["cod_restricted"] is False
        assert svc.is_customer_cod_restricted(u.id) is False


def _cap_the_place(db, address, *, each):
    """Group ``address`` with two coworkers, each carrying one office debt of ``each``."""
    from tests.unit._scope_money_helpers import make_address, make_place_group

    u1, u2 = make_user(db), make_user(db)
    a1, a2 = make_address(db, u1), make_address(db, u2)
    make_place_group(db, a1, a2, address)
    delivered_cod_order(db, u1, address=a1, total=each)
    delivered_cod_order(db, u2, address=a2, total=each)


@pytest.mark.integration
class TestPlaceArmOverHttp:
    def test_two_tiny_place_debts_do_not_restrict(
        self, app, db, client, orderer, sample_product, user_address, auth_headers
    ):
        """The orderer is clean; their office owes 560 across two coworkers."""
        _cap_the_place(db, user_address, each=Decimal("280.00"))

        resp = client.get(
            f"/api/v1/payments/methods?delivery_address_id={user_address.id}",
            headers=_auth(app, orderer.id),
        )
        assert resp.status_code == 200, resp.get_json()
        body = resp.get_json()["data"]
        assert body["payment_restrictions"]["place_active_cod_debt_count"] == COD_ACTIVE_DEBT_LIMIT
        assert body["payment_restrictions"]["cod_restricted"] is False
        assert body["payment_restrictions"]["restriction_scope"] is None
        assert any(m["method"] == CASH for m in body["available_methods"])

        created = _post_cash_order(app, sample_product.id, user_address.id, auth_headers)
        assert created.status_code == 201, created.get_json()

    def test_two_real_place_debts_restrict_on_both_surfaces(
        self, app, db, client, orderer, sample_product, user_address, auth_headers
    ):
        _cap_the_place(db, user_address, each=Decimal("6000.00"))

        resp = client.get(
            f"/api/v1/payments/methods?delivery_address_id={user_address.id}",
            headers=_auth(app, orderer.id),
        )
        assert resp.status_code == 200, resp.get_json()
        body = resp.get_json()["data"]
        assert body["payment_restrictions"]["cod_restricted"] is True
        assert body["payment_restrictions"]["restriction_scope"] == "place"
        assert all(m["method"] != CASH for m in body["available_methods"])

        created = _post_cash_order(app, sample_product.id, user_address.id, auth_headers)
        assert created.status_code == 400, created.get_json()

    def test_one_large_place_debt_does_not_restrict(
        self, app, db, client, orderer, sample_product, user_address, auth_headers
    ):
        from tests.unit._scope_money_helpers import make_address, make_place_group

        coworker = make_user(db)
        coworker_addr = make_address(db, coworker)
        make_place_group(db, coworker_addr, user_address)
        delivered_cod_order(db, coworker, address=coworker_addr, total=Decimal("50000.00"))

        resp = client.get(
            f"/api/v1/payments/methods?delivery_address_id={user_address.id}",
            headers=_auth(app, orderer.id),
        )
        body = resp.get_json()["data"]
        assert body["payment_restrictions"]["cod_restricted"] is False
        assert any(m["method"] == CASH for m in body["available_methods"])

        created = _post_cash_order(app, sample_product.id, user_address.id, auth_headers)
        assert created.status_code == 201, created.get_json()

    def test_context_publishes_the_place_net_total_it_gated_on(self, db, orderer, user_address):
        from business_app.services.cash_collection_service import CashCollectionService

        _cap_the_place(db, user_address, each=Decimal("280.00"))
        ctx = CashCollectionService().get_cod_restriction_context(
            orderer.id, delivery_address_id=user_address.id
        )
        assert ctx["place_net_open_cod_debt_total"] == 560.0

    def test_place_net_total_is_none_when_the_arm_is_not_evaluated(self, db, orderer):
        from business_app.services.cash_collection_service import CashCollectionService

        ctx = CashCollectionService().get_cod_restriction_context(orderer.id)
        assert ctx["place_active_cod_debt_count"] is None
        assert ctx["place_net_open_cod_debt_total"] is None


@pytest.mark.integration
class TestBatchDecisionAgreesWithTheSingleUserPath:
    def test_batch_flags_match_for_tiny_and_real_debts(self, db):
        from business_app.services.cash_collection_service import CashCollectionService

        tiny, real = make_user(db), make_user(db)
        for _ in range(COD_ACTIVE_DEBT_LIMIT):
            delivered_cod_order(db, tiny, total=Decimal("280.00"))
            delivered_cod_order(db, real, total=Decimal("6000.00"))

        svc = CashCollectionService()
        flags = svc.get_cod_restricted_flags([tiny.id, real.id])
        assert flags[tiny.id] is False
        assert flags[real.id] is True
        assert flags == {uid: svc.is_customer_cod_restricted(uid) for uid in (tiny.id, real.id)}
