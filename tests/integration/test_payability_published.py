"""B3 — the backend PUBLISHES payability instead of letting five clients guess.

`order_is_payable_online(order, payment)` (payment_projection.py) is THE single
authority on "may money still be taken for this order online". Until B3 it had
exactly ONE caller — the Click PREPARE guard — so every client surface that had
to decide whether to offer a way to pay re-derived the answer from whatever it
could see: the admin Orders page from "we stored a payment_link once", the
customer bot from `order_status == 'pending'`.

Both copies are wrong under the 2026-08-24 policy. Case B — DELIVERED, unpaid,
Click rail, payment still PENDING — is payable BY DESIGN (the money can still
arrive and the receipt still has to be issued), and both copies say it is not.

So two fields are published, on every read surface the two clients actually
call, and this file drives them through HTTP:

* `is_payable`            — the authority's answer verbatim. A PERMISSION, for
                            surfaces whose action is "start a payment" (the
                            bot's Pay/Retry callbacks mint a fresh link
                            server-side, so there is no URL to hand them).
* `payable_payment_link`  — the STORED link, non-null only when following it
                            would work. For surfaces whose action is "open THIS
                            link". Gate and href collapse into one value, so a
                            button pointing at a dead link is not writable.

`payment_link` stays published raw: the admin audit row has to be able to tell
"a link was issued and is now dead" from "no link was ever issued", which needs
both values. That is also why one boolean cannot serve both questions.
"""

from decimal import Decimal

import pytest
from flask_jwt_extended import create_access_token

from business_app.models.order import Order
from business_app.models.payment import Payment
from business_app.utils.payment_projection import order_is_payable_online
from shared.enums import OrderStatus, PaymentMethod, PaymentStatus

CLICK_LINK = "https://my.click.uz/services/pay?id=1&t=CB"


def _click_order(
    db,
    user,
    *,
    order_number,
    order_status=OrderStatus.DELIVERED,
    payment_status=PaymentStatus.PENDING,
    payment_method=PaymentMethod.CLICK,
    is_paid=False,
    payment_link=CLICK_LINK,
):
    order = Order(
        user_id=user.id,
        order_number=order_number,
        status=order_status,
        subtotal=Decimal("15000.00"),
        delivery_fee=Decimal("3000.00"),
        discount_amount=Decimal("0.00"),
        loyalty_discount=Decimal("0.00"),
        total_amount=Decimal("18000.00"),
        payment_method=payment_method,
        is_paid=is_paid,
    )
    db.session.add(order)
    db.session.flush()
    payment = Payment(
        order_id=order.id,
        user_id=user.id,
        payment_method=payment_method,
        amount=order.total_amount,
        currency="UZS",
        status=payment_status,
        payment_id=f"pay-{order_number}",
        payment_link=payment_link,
        provider_data={"click": {"click_paydoc_id": "20240101000001"}},
    )
    db.session.add(payment)
    db.session.commit()
    return order, payment


def _customer_headers(app, user):
    with app.app_context():
        token = create_access_token(identity=str(user.id))
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _admin_headers(app, admin_user):
    # manager_or_higher_required reads the role CLAIM; the shared fixture is a 403.
    with app.app_context():
        token = create_access_token(identity=str(admin_user.id), additional_claims={"role": "admin"})
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


class TestCustomerOrderDetailPublishesPayability:
    """GET /api/v1/orders/<id> — the endpoint `api_client.get_order` calls."""

    def test_case_b_delivered_unpaid_click_order_is_published_as_payable(
        self, client, app, db, sample_user
    ):
        order, payment = _click_order(db, sample_user, order_number="B3-C-001")

        resp = client.get(f"/api/v1/orders/{order.id}", headers=_customer_headers(app, sample_user))

        assert resp.status_code == 200
        payment_info = resp.get_json()["data"]["order"]["payment_info"]
        assert payment_info["is_payable"] is True, (
            "case B is payable BY DESIGN — the bot cannot offer the pay button without this"
        )
        assert payment_info["payable_payment_link"] == CLICK_LINK

    def test_published_field_is_the_authority_not_a_second_opinion(
        self, client, app, db, sample_user
    ):
        order, payment = _click_order(db, sample_user, order_number="B3-C-002")

        resp = client.get(f"/api/v1/orders/{order.id}", headers=_customer_headers(app, sample_user))
        payment_info = resp.get_json()["data"]["order"]["payment_info"]

        with app.app_context():
            live_order = Order.query.get(order.id)
            expected = order_is_payable_online(live_order, live_order.payment)
        assert payment_info["is_payable"] is expected

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"order_status": OrderStatus.CANCELLED},
            {"order_status": OrderStatus.RETURNED},
            {"is_paid": True},
            {"payment_status": PaymentStatus.COMPLETED},
            {"payment_method": PaymentMethod.CASH},
        ],
        ids=["cancelled", "returned", "paid", "payment-completed", "cash-rail"],
    )
    def test_unpayable_shapes_publish_false_and_withhold_the_link(
        self, client, app, db, sample_user, kwargs
    ):
        order, _payment = _click_order(db, sample_user, order_number="B3-C-003", **kwargs)

        resp = client.get(f"/api/v1/orders/{order.id}", headers=_customer_headers(app, sample_user))
        payment_info = resp.get_json()["data"]["order"]["payment_info"]

        assert payment_info["is_payable"] is False
        assert payment_info["payable_payment_link"] is None
        # The raw link stays published: "issued then died" must stay
        # distinguishable from "never issued".
        assert payment_info["payment_link"] == CLICK_LINK

    def test_order_list_publishes_it_too(self, client, app, db, sample_user):
        """The bot's My Orders list renders from GET /api/v1/orders."""
        _click_order(db, sample_user, order_number="B3-C-004")

        resp = client.get("/api/v1/orders/", headers=_customer_headers(app, sample_user))

        assert resp.status_code == 200
        rows = resp.get_json()["data"]["orders"]
        assert rows, "expected the seeded order in the customer's list"
        assert rows[0]["payment_info"]["is_payable"] is True


class TestAdminOrderSurfacesPublishPayability:
    """Both admin payloads that feed `selectedOrder` in Orders.js.

    The detail modal starts as the LIST row (`serialize_order_admin`) and is
    then replaced by the DETAIL payload. Publish on only one and the two
    payment-link buttons flip meaning halfway through opening the modal.
    """

    def test_admin_list_row_carries_both_fields(self, client, app, db, sample_user, admin_user):
        _click_order(db, sample_user, order_number="B3-A-001")

        resp = client.get("/api/v1/admin/orders", headers=_admin_headers(app, admin_user))

        assert resp.status_code == 200
        row = next(r for r in resp.get_json()["data"]["items"] if r["order_number"] == "B3-A-001")
        assert row["is_payable"] is True
        assert row["payable_payment_link"] == CLICK_LINK

    def test_admin_detail_payload_carries_both_fields(self, client, app, db, sample_user, admin_user):
        order, _payment = _click_order(db, sample_user, order_number="B3-A-002")

        resp = client.get(f"/api/v1/admin/orders/{order.id}", headers=_admin_headers(app, admin_user))

        assert resp.status_code == 200
        body = resp.get_json()["data"]["order"]
        assert body["is_payable"] is True
        assert body["payable_payment_link"] == CLICK_LINK

    def test_a_dead_order_offers_no_link_but_still_shows_one_was_issued(
        self, client, app, db, sample_user, admin_user
    ):
        order, _payment = _click_order(
            db, sample_user, order_number="B3-A-003", order_status=OrderStatus.CANCELLED
        )

        resp = client.get(f"/api/v1/admin/orders/{order.id}", headers=_admin_headers(app, admin_user))
        body = resp.get_json()["data"]["order"]

        assert body["is_payable"] is False
        assert body["payable_payment_link"] is None
        assert body["payment_link"] == CLICK_LINK

    def test_payment_less_order_defaults_both_fields_rather_than_omitting_them(
        self, client, app, db, sample_user, admin_user
    ):
        """The else-branch matters: `undefined` flips every JS truthiness test."""
        order = Order(
            user_id=sample_user.id,
            order_number="B3-A-004",
            status=OrderStatus.PENDING,
            subtotal=Decimal("10000.00"),
            delivery_fee=Decimal("0.00"),
            discount_amount=Decimal("0.00"),
            loyalty_discount=Decimal("0.00"),
            total_amount=Decimal("10000.00"),
            payment_method=PaymentMethod.CASH,
            is_paid=False,
        )
        db.session.add(order)
        db.session.commit()

        resp = client.get("/api/v1/admin/orders", headers=_admin_headers(app, admin_user))
        row = next(r for r in resp.get_json()["data"]["items"] if r["order_number"] == "B3-A-004")

        assert "is_payable" in row and row["is_payable"] is False
        assert "payable_payment_link" in row and row["payable_payment_link"] is None
