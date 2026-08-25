"""I4 — the rail lock was keyed on ONE operand, and B3 made both cells reachable.

`PaymentService.create_payment` (`payment_service.py:270-277`) computes

    rail_moves = payment is not None and payment.payment_method != payment_method

and consults `_RAIL_LOCKED_ORDER_STATUSES` only when `rail_moves`. But the rail
is TWO columns — the service's own comment says so: "`orders.payment_method` is
what every settlement gate reads; `payments.payment_method` is what the
allocator reads ... move both or neither". Keying the guard on the payment
column alone gets both halves wrong, and B3 newly routes a customer tap at each:

(a) FALSE POSITIVE. `PaymentMethod.CARD` is a LEGACY ALIAS of CLICK
    (`shared/payment_methods.py:55`: `PAYMENT_METHOD_ALIASES = {"card": CLICK}`,
    "never written again"). A delivered unpaid order on a legacy CARD row is
    `is_payable` — CARD is in `FISCALIZED_RAILS` — so B3 draws the Pay button;
    the bot normalizes to 'click'; the raw-enum comparison calls that a rail
    move and the lock raises. A rendered button that can only fail, forever.

(b) FALSE NEGATIVE, and this one hides money. When `order.payment_method` is
    CASH while `payment.payment_method` is CLICK (the desync commit 6c2951d
    addressed), a requested CLICK is "no move" by the payment column, so the
    lock is SKIPPED — and `:310-311` then rewrites `order.payment_method` to
    CLICK on a DELIVERED order. That is verbatim what the lock's own comment
    forbids: "moving a delivered COD debt onto a gateway rail hides it from the
    receivable ledger".

The fix compares CANONICAL rails on BOTH columns. It is not a loosening: (a)
stops firing because card IS click, and (b) starts firing because cash is not.
"""

from decimal import Decimal

import pytest

from business_app.models.order import Order
from business_app.models.payment import Payment
from business_app.utils.exceptions import ValidationError
from shared.enums import OrderStatus, PaymentMethod, PaymentStatus


def _delivered_unpaid(db, user, *, order_number, order_method, payment_method):
    order = Order(
        user_id=user.id,
        order_number=order_number,
        status=OrderStatus.DELIVERED,
        subtotal=Decimal("18000.00"),
        delivery_fee=Decimal("0.00"),
        discount_amount=Decimal("0.00"),
        loyalty_discount=Decimal("0.00"),
        total_amount=Decimal("18000.00"),
        payment_method=order_method,
        is_paid=False,
    )
    db.session.add(order)
    db.session.flush()
    payment = Payment(
        order_id=order.id,
        user_id=user.id,
        payment_method=payment_method,
        amount=order.total_amount,
        currency="UZS",
        status=PaymentStatus.PENDING,
        payment_id=f"pay-{order_number}",
    )
    db.session.add(payment)
    db.session.commit()
    return order, payment


class TestTheLegacyCardAliasIsNotARailMove:
    """(a) — the button B3 draws for this population must be able to work."""

    def test_a_delivered_card_row_may_still_be_paid_as_click(self, app, db, sample_user):
        from business_app.services.payment_service import PaymentService

        order, payment = _delivered_unpaid(
            db, sample_user,
            order_number="I4-card",
            order_method=PaymentMethod.CLICK,
            payment_method=PaymentMethod.CARD,
        )

        result = PaymentService().create_payment(
            order_id=order.id,
            payment_method=PaymentMethod.CLICK,
            amount=order.total_amount,
        )

        assert result.id == payment.id
        assert result.payment_method == PaymentMethod.CLICK, (
            "card is a legacy alias of click; normalising it is not a rail move"
        )


class TestADeliveredCodDebtCannotBeMovedOntoAGatewayRail:
    """(b) — the cell the single-operand guard silently let through."""

    def test_a_desynced_cash_order_is_refused(self, app, db, sample_user):
        from business_app.services.payment_service import PaymentService

        order, _payment = _delivered_unpaid(
            db, sample_user,
            order_number="I4-desync",
            order_method=PaymentMethod.CASH,     # what every settlement gate reads
            payment_method=PaymentMethod.CLICK,  # what the allocator reads
        )
        order_id = order.id

        with pytest.raises(ValidationError):
            PaymentService().create_payment(
                order_id=order_id,
                payment_method=PaymentMethod.CLICK,
                amount=order.total_amount,
            )

        with app.app_context():
            assert Order.query.get(order_id).payment_method == PaymentMethod.CASH, (
                "a delivered COD debt was re-railed onto Click and vanished from "
                "the receivable ledger"
            )


class TestTheLockStillFiresAndStillStandsAside:
    def test_a_genuine_delivered_rail_move_is_still_refused(self, app, db, sample_user):
        from business_app.services.payment_service import PaymentService

        order, _payment = _delivered_unpaid(
            db, sample_user,
            order_number="I4-genuine",
            order_method=PaymentMethod.CASH,
            payment_method=PaymentMethod.CASH,
        )

        with pytest.raises(ValidationError):
            PaymentService().create_payment(
                order_id=order.id,
                payment_method=PaymentMethod.CLICK,
                amount=order.total_amount,
            )

    def test_a_live_order_may_still_flip_cash_to_click(self, app, db, sample_user):
        from business_app.services.payment_service import PaymentService

        order, _payment = _delivered_unpaid(
            db, sample_user,
            order_number="I4-live",
            order_method=PaymentMethod.CASH,
            payment_method=PaymentMethod.CASH,
        )
        order.status = OrderStatus.PENDING
        db.session.commit()

        result = PaymentService().create_payment(
            order_id=order.id,
            payment_method=PaymentMethod.CLICK,
            amount=order.total_amount,
        )

        assert result.payment_method == PaymentMethod.CLICK


class TestThroughTheEndpointTheCustomerActuallyHits:
    """The repo's rule is "test the endpoint, not the service".

    It matters here specifically: `POST /api/v1/payments/create` runs its OWN
    flip guard (`api/payments.py:311`) and an existing-PENDING-payment
    short-circuit BEFORE `PaymentService.create_payment` is ever called, so a
    service-level test cannot see which populations reach the rail lock at all.
    """

    @staticmethod
    def _headers(app, user):
        from flask_jwt_extended import create_access_token

        with app.app_context():
            token = create_access_token(identity=str(user.id))
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    def test_a_delivered_legacy_card_order_gets_a_link_over_http(
        self, client, app, db, sample_user, monkeypatch
    ):
        """I4(a) end to end — the button B3 draws for this population works."""
        order, _payment = _delivered_unpaid(
            db, sample_user,
            order_number="I4-http-card",
            order_method=PaymentMethod.CLICK,
            payment_method=PaymentMethod.CARD,
        )
        monkeypatch.setattr(
            "business_app.services.payment_service.PaymentService.create_payment_link",
            lambda self, payment_id: {"payment_url": "https://my.click.uz/services/pay?id=9"},
        )

        resp = client.post(
            "/api/v1/payments/create",
            headers=self._headers(app, sample_user),
            json={"order_id": order.id, "payment_method": "click", "return_url": "https://t.me/x"},
        )

        assert resp.status_code in (200, 201), resp.get_json()
        assert resp.get_json()["data"]["payment_link"]["payment_url"]

    def test_a_legacy_card_order_is_not_treated_as_a_rail_flip(
        self, client, app, db, sample_user, monkeypatch
    ):
        """NEW-2 — the endpoint's OWN flip guard was the third raw-operand copy.

        `pool_covers_order` must not even be consulted: the rail is not moving,
        and its own comment says an order already on the requested rail stays
        payable however short the shared pool reads.
        """
        order, _payment = _delivered_unpaid(
            db, sample_user,
            order_number="I4-http-noflip",
            order_method=PaymentMethod.CARD,
            payment_method=PaymentMethod.CARD,
        )
        monkeypatch.setattr(
            "business_app.services.payment_service.PaymentService.create_payment_link",
            lambda self, payment_id: {"payment_url": "https://my.click.uz/services/pay?id=10"},
        )
        consulted = []
        from business_app.services.product_fiscal_service import ProductFiscalService

        monkeypatch.setattr(
            ProductFiscalService,
            "pool_covers_order",
            lambda self, o, m: (consulted.append(m), (False, "Bottle 19L"))[1],
        )

        resp = client.post(
            "/api/v1/payments/create",
            headers=self._headers(app, sample_user),
            json={"order_id": order.id, "payment_method": "click", "return_url": "https://t.me/x"},
        )

        assert consulted == [], "card->click is not a flip; the pool guard must stand aside"
        assert resp.status_code in (200, 201), resp.get_json()

    def test_a_null_rail_order_does_not_crash_the_flip_guard(
        self, client, app, db, sample_user, monkeypatch
    ):
        """Legacy subscription rows carry a NULL `orders.payment_method`.
        `canonical_rail` is TOTAL for exactly this reason — a normalizer that
        raised here would 500 the endpoint instead of guarding it."""
        order, _payment = _delivered_unpaid(
            db, sample_user,
            order_number="I4-http-null",
            order_method=PaymentMethod.CLICK,
            payment_method=PaymentMethod.CLICK,
        )
        order.status = OrderStatus.PENDING
        order.payment_method = None
        db.session.commit()
        monkeypatch.setattr(
            "business_app.services.payment_service.PaymentService.create_payment_link",
            lambda self, payment_id: {"payment_url": "https://my.click.uz/services/pay?id=11"},
        )

        resp = client.post(
            "/api/v1/payments/create",
            headers=self._headers(app, sample_user),
            json={"order_id": order.id, "payment_method": "click", "return_url": "https://t.me/x"},
        )

        assert resp.status_code != 500, resp.get_json()
