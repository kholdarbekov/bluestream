"""B3 — the backend PUBLISHES payability to the admin Orders page instead of letting it guess.

`order_is_payable_online(order, payment)` (payment_projection.py) is THE single
authority on "may money still be taken for this order online", the function the
Click PREPARE guard refuses on. Before B3 the admin Orders page re-derived it
from "we stored a payment_link once", which is wrong under the 2026-08-24
policy: case B — DELIVERED, unpaid, Click rail, payment still PENDING — is
payable BY DESIGN (the money can still arrive and the receipt still has to be
issued).

So the two admin payloads that feed `selectedOrder` in Orders.js publish two
fields, and this file drives them through HTTP:

* `payable_payment_link`  — the STORED link, non-null only when following it
                            would work. Gate and href collapse into one value,
                            so a button pointing at a dead link is not writable.
* `is_payable`            — the authority's answer verbatim.

`payment_link` stays published raw: the admin audit row has to be able to tell
"a link was issued and is now dead" from "no link was ever issued", which needs
both values.

The customer payloads carry neither field: the customer's Pay is the published
`customer_actions.pay` (spec 2026-10-27 §4.2), pinned with the retired fields in
tests/unit/test_retired_customer_surfaces.py.
"""

from decimal import Decimal

from flask_jwt_extended import create_access_token

from business_app.models.order import Order
from business_app.models.payment import Payment
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


def _admin_headers(app, admin_user):
    # manager_or_higher_required reads the role CLAIM; the shared fixture is a 403.
    with app.app_context():
        token = create_access_token(identity=str(admin_user.id), additional_claims={"role": "admin"})
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


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
