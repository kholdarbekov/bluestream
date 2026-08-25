"""I3 — the WEB retry-payment endpoint was the sixth surface guessing at payability.

`business_app/static/js/pages/payment-cancelled.js:10` fires
`POST /api/v1/orders/<id>/retry-payment` from an ungated button on
`templates/frontend/payment_cancelled.html`, then redirects the browser to the
returned `payment_url`. The endpoint checked only `is_paid` and the rail — NO
order-status test.

Two consequences on a CANCELLED order, and the second is a MONEY bug:

1. the customer lands on Click checkout and PREPARE answers -9;
2. `PaymentService.create_payment` first rewrites the row —
   `payment.status = PENDING` and `payment.amount = order.total_amount`
   (`payment_service.py:282, 292-293`) — undoing the zeroing
   `_sync_payment_status_for_terminal_order_state` applied when the order died
   (`order_service.py:2047` reduces `amount` to `amount_collected` so the
   receivable stays 0). A CANCELLED order therefore RE-APPEARS as owing money to
   `open_receivable_amount`, on every debtor list and toward the COD cap.

The bot refuses this since B3. This closes the web half, at the ENDPOINT — the
template button is hidden too, but a hidden button is not a guard.
"""

from decimal import Decimal

import pytest
from flask_jwt_extended import create_access_token

from business_app.models.order import Order
from business_app.models.payment import Payment
from business_app.utils.payment_projection import open_receivable_amount
from shared.enums import OrderStatus, PaymentMethod, PaymentStatus


def _order_with_payment(
    db,
    user,
    *,
    order_number,
    order_status,
    payment_status=PaymentStatus.PENDING,
    is_paid=False,
    amount=Decimal("18000.00"),
    payment_amount=None,
):
    order = Order(
        user_id=user.id,
        order_number=order_number,
        status=order_status,
        subtotal=amount,
        delivery_fee=Decimal("0.00"),
        discount_amount=Decimal("0.00"),
        loyalty_discount=Decimal("0.00"),
        total_amount=amount,
        payment_method=PaymentMethod.CLICK,
        is_paid=is_paid,
    )
    db.session.add(order)
    db.session.flush()
    payment = Payment(
        order_id=order.id,
        user_id=user.id,
        payment_method=PaymentMethod.CLICK,
        amount=payment_amount if payment_amount is not None else amount,
        currency="UZS",
        status=payment_status,
        payment_id=f"pay-{order_number}",
        provider_data={"click": {"click_paydoc_id": "20240101000001"}},
    )
    db.session.add(payment)
    db.session.commit()
    return order, payment


def _headers(app, user):
    with app.app_context():
        token = create_access_token(identity=str(user.id))
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


class TestWebRetryRefusesADeadOrder:
    @pytest.mark.parametrize(
        "order_status", [OrderStatus.CANCELLED, OrderStatus.RETURNED], ids=["cancelled", "returned"]
    )
    def test_it_refuses_and_hands_back_no_payment_url(self, client, app, db, sample_user, order_status):
        order, _payment = _order_with_payment(
            db,
            sample_user,
            order_number=f"W3-{order_status.value}",
            order_status=order_status,
            payment_status=PaymentStatus.CANCELLED,
            # the zeroed shape a dead order is left in
            payment_amount=Decimal("0.00"),
        )

        resp = client.post(
            f"/api/v1/orders/{order.id}/retry-payment", headers=_headers(app, sample_user), json={}
        )

        assert resp.status_code >= 400, "a dead order must not be handed a checkout URL"
        assert not (resp.get_json().get("data") or {}).get("payment_url")

    def test_it_does_not_resurrect_a_dead_order_s_receivable(self, client, app, db, sample_user):
        """The money half. `create_payment` rewrites amount back to the order
        total; that is what makes a cancelled order owe money again."""
        order, payment = _order_with_payment(
            db,
            sample_user,
            order_number="W3-money",
            order_status=OrderStatus.CANCELLED,
            payment_status=PaymentStatus.CANCELLED,
            payment_amount=Decimal("0.00"),
        )
        payment_id = payment.id
        with app.app_context():
            assert open_receivable_amount(Payment.query.get(payment_id)) == Decimal("0.00")

        client.post(
            f"/api/v1/orders/{order.id}/retry-payment", headers=_headers(app, sample_user), json={}
        )

        with app.app_context():
            after = Payment.query.get(payment_id)
            assert open_receivable_amount(after) == Decimal("0.00"), (
                "retrying a cancelled order re-inflated payment.amount and the "
                "order re-appeared on every debtor list"
            )
            assert after.status == PaymentStatus.CANCELLED

    def test_it_refuses_an_order_already_settled_on_its_payment(self, client, app, db, sample_user):
        """M4's server twin: retrying would REWRITE a COMPLETED payment to
        PENDING and mint a second link."""
        order, payment = _order_with_payment(
            db,
            sample_user,
            order_number="W3-settled",
            order_status=OrderStatus.PENDING,
            payment_status=PaymentStatus.COMPLETED,
        )
        payment_id = payment.id

        resp = client.post(
            f"/api/v1/orders/{order.id}/retry-payment", headers=_headers(app, sample_user), json={}
        )

        assert resp.status_code >= 400
        with app.app_context():
            assert Payment.query.get(payment_id).status == PaymentStatus.COMPLETED


class TestWebRetryStillServesTheLivePopulation:
    def test_a_live_unpaid_click_order_is_still_retryable(self, client, app, db, sample_user, monkeypatch):
        order, _payment = _order_with_payment(
            db, sample_user, order_number="W3-live", order_status=OrderStatus.PENDING
        )
        monkeypatch.setattr(
            "business_app.services.payment_service.PaymentService.create_payment_link",
            lambda self, payment_id: {"payment_url": "https://my.click.uz/services/pay?id=1"},
        )

        resp = client.post(
            f"/api/v1/orders/{order.id}/retry-payment", headers=_headers(app, sample_user), json={}
        )

        assert resp.status_code == 200, resp.get_json()
        assert resp.get_json()["data"]["payment_url"]

    def test_a_case_b_delivered_unpaid_order_is_still_retryable(
        self, client, app, db, sample_user, monkeypatch
    ):
        """The whole point of B3 — do not close this door while closing the others."""
        order, _payment = _order_with_payment(
            db, sample_user, order_number="W3-caseb", order_status=OrderStatus.DELIVERED
        )
        monkeypatch.setattr(
            "business_app.services.payment_service.PaymentService.create_payment_link",
            lambda self, payment_id: {"payment_url": "https://my.click.uz/services/pay?id=2"},
        )

        resp = client.post(
            f"/api/v1/orders/{order.id}/retry-payment", headers=_headers(app, sample_user), json={}
        )

        assert resp.status_code == 200, resp.get_json()
        assert resp.get_json()["data"]["payment_url"]


class TestThePaymeCellThatJustifiedThePartialPredicate:
    """The guard uses `order_is_resolved`, not the full `order_is_payable_online`.

    The full predicate's payment half requires the rail to be in
    `FISCALIZED_RAILS`, which excludes PAYME **by construction** (that exclusion
    is the payme carve-out for the 2026-08-24 no-reversal rule) — while this
    endpoint's own whitelist admits `payme`. Gating on the full predicate would
    therefore have silently deleted Payme retry, the same shape of narrowing the
    cash rail would have suffered. `order_is_resolved` is rail-BLIND, so a dead
    payme order is refused and a live one is not. Both directions pinned here,
    because a future "tidy-up" to the full predicate would break exactly one.
    """

    @staticmethod
    def _payme_order(db, user, *, order_number, order_status, payment_status):
        order = Order(
            user_id=user.id,
            order_number=order_number,
            status=order_status,
            subtotal=Decimal("18000.00"),
            delivery_fee=Decimal("0.00"),
            discount_amount=Decimal("0.00"),
            loyalty_discount=Decimal("0.00"),
            total_amount=Decimal("18000.00"),
            payment_method=PaymentMethod.PAYME,
            is_paid=False,
        )
        db.session.add(order)
        db.session.flush()
        db.session.add(
            Payment(
                order_id=order.id,
                user_id=user.id,
                payment_method=PaymentMethod.PAYME,
                amount=order.total_amount,
                currency="UZS",
                status=payment_status,
                payment_id=f"pay-{order_number}",
            )
        )
        db.session.commit()
        return order

    def test_a_live_payme_order_is_still_retryable(self, client, app, db, sample_user, monkeypatch):
        order = self._payme_order(
            db, sample_user,
            order_number="W3-payme-live",
            order_status=OrderStatus.PENDING,
            payment_status=PaymentStatus.PENDING,
        )
        monkeypatch.setattr(
            "business_app.services.payment_service.PaymentService.create_payment_link",
            lambda self, payment_id: {"payment_url": "https://checkout.paycom.uz/abc"},
        )

        resp = client.post(
            f"/api/v1/orders/{order.id}/retry-payment", headers=_headers(app, sample_user), json={}
        )

        assert resp.status_code == 200, resp.get_json()
        assert resp.get_json()["data"]["payment_url"]

    def test_a_cancelled_payme_order_is_refused_all_the_same(self, client, app, db, sample_user):
        order = self._payme_order(
            db, sample_user,
            order_number="W3-payme-dead",
            order_status=OrderStatus.CANCELLED,
            payment_status=PaymentStatus.CANCELLED,
        )

        resp = client.post(
            f"/api/v1/orders/{order.id}/retry-payment", headers=_headers(app, sample_user), json={}
        )

        assert resp.status_code >= 400
        assert not (resp.get_json().get("data") or {}).get("payment_url")
