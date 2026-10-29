"""I4 — the rail lock was keyed on ONE operand, and B3 made both cells reachable.

`PaymentService.create_payment` (`payment_service.py:270-277`) computes

    rail_moves = payment is not None and payment.payment_method != payment_method

and consulted the rail lock only when `rail_moves`. But the rail
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

Since spec 2026-10-27 §4.3 (D4, D5) the one rail move a customer write may make
is from an online rail (Click, or legacy Payme) onto Click with nothing
collected; every other move is `RAIL_MOVE_NOT_ALLOWED`.
"""

from decimal import Decimal

import pytest

from business_app.models.order import Order
from business_app.models.payment import Payment
from business_app.utils.exceptions import ValidationError
from business_app.utils.payment_projection import open_receivable_amount
from shared.enums import OrderStatus, PaymentMethod, PaymentStatus

LEFT_DELIVERY = "Payment method cannot be changed once the order has left delivery"
DEAD_STATUSES = [OrderStatus.CANCELLED, OrderStatus.RETURNED]


def _dead_payme_order(db, user, *, order_number, status):
    """A legacy Payme order that is cancelled or returned, its Payme row as the real cancel leaves it
    (`OrderService._sync_payment_status_for_terminal_order_state`): CANCELLED, zeroed, owing nothing."""
    order, payment = _delivered_unpaid(
        db, user,
        order_number=order_number,
        order_method=PaymentMethod.PAYME,
        payment_method=PaymentMethod.PAYME,
    )
    order.status = status
    payment.status = PaymentStatus.CANCELLED
    payment.amount = Decimal("0.00")
    payment.amount_collected = Decimal("0.00")
    payment.outstanding_amount = Decimal("0.00")
    db.session.commit()
    return order, payment


def _left_as_it_was(db, order_id, payment_id):
    """What a refused move must not touch: both rails and the row's money and status."""
    db.session.expire_all()
    order = db.session.get(Order, order_id)
    payment = db.session.get(Payment, payment_id)
    return (
        order.payment_method,
        payment.payment_method,
        payment.status,
        payment.amount,
        payment.amount_collected,
        payment.outstanding_amount,
    )


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

    def test_a_live_cash_order_is_refused_a_move_to_click(self, app, db, sample_user):
        """No cash -> Click switch for the customer (spec 2026-10-27 §4.3, D4)."""
        from business_app.services.payment_service import PaymentService

        order, payment = _delivered_unpaid(
            db, sample_user,
            order_number="I4-live",
            order_method=PaymentMethod.CASH,
            payment_method=PaymentMethod.CASH,
        )
        order.status = OrderStatus.PENDING
        db.session.commit()
        order_id, payment_id = order.id, payment.id

        with pytest.raises(ValidationError) as exc:
            PaymentService().create_payment(order_id, PaymentMethod.CLICK, amount=order.total_amount)

        assert exc.value.error_code == "RAIL_MOVE_NOT_ALLOWED"
        db.session.expire_all()
        assert db.session.get(Order, order_id).payment_method is PaymentMethod.CASH
        assert db.session.get(Payment, payment_id).payment_method is PaymentMethod.CASH

    def test_a_live_cash_order_with_no_payment_row_is_refused_a_move_to_click(self, app, db, sample_user):
        """The order's rail alone is still a rail: with no payment row yet, moving a cash order onto Click is the
        same switch, and is refused the same way."""
        from business_app.services.payment_service import PaymentService

        order, payment = _delivered_unpaid(
            db, sample_user,
            order_number="I4-rowless",
            order_method=PaymentMethod.CASH,
            payment_method=PaymentMethod.CASH,
        )
        order.status = OrderStatus.PENDING
        db.session.delete(payment)
        db.session.commit()
        order_id = order.id

        with pytest.raises(ValidationError) as exc:
            PaymentService().create_payment(order_id, PaymentMethod.CLICK, amount=order.total_amount)

        assert exc.value.error_code == "RAIL_MOVE_NOT_ALLOWED"
        db.session.expire_all()
        assert db.session.get(Order, order_id).payment_method is PaymentMethod.CASH
        assert Payment.query.filter_by(order_id=order_id).count() == 0

    def test_an_online_move_after_money_was_collected_is_refused(self, app, db, sample_user):
        """Payme -> Click is the one move left, and only while nothing was collected: a later completion would
        zero `amount_collected`."""
        from business_app.services.payment_service import PaymentService

        order, payment = _delivered_unpaid(
            db, sample_user,
            order_number="I4-collected",
            order_method=PaymentMethod.PAYME,
            payment_method=PaymentMethod.PAYME,
        )
        payment.amount_collected = Decimal("1000.00")
        db.session.commit()
        order_id, payment_id = order.id, payment.id

        with pytest.raises(ValidationError) as exc:
            PaymentService().create_payment(order_id, PaymentMethod.CLICK, amount=order.total_amount)

        assert exc.value.message == "Payment method cannot be changed after cash has been collected"
        db.session.expire_all()
        assert db.session.get(Order, order_id).payment_method is PaymentMethod.PAYME
        assert db.session.get(Payment, payment_id).payment_method is PaymentMethod.PAYME

    @pytest.mark.parametrize("status", DEAD_STATUSES, ids=lambda s: s.value)
    def test_a_dead_payme_order_is_refused_a_move_to_click(self, app, db, sample_user, status):
        """Payme -> Click is allowed after delivery, but never on a dead order. Without this check the cancelled
        row is set back to PENDING at the order total, both rails move to Click, and the dead order reads as
        owing money (`open_receivable_amount`)."""
        from business_app.services.payment_service import PaymentService

        order, payment = _dead_payme_order(db, sample_user, order_number=f"I4-dead-{status.value}", status=status)
        order_id, payment_id = order.id, payment.id
        before = _left_as_it_was(db, order_id, payment_id)

        with pytest.raises(ValidationError) as exc:
            PaymentService().create_payment(order_id, PaymentMethod.CLICK, amount=Decimal("18000.00"))

        assert exc.value.message == LEFT_DELIVERY
        assert _left_as_it_was(db, order_id, payment_id) == before
        assert before[:3] == (PaymentMethod.PAYME, PaymentMethod.PAYME, PaymentStatus.CANCELLED)
        assert open_receivable_amount(db.session.get(Payment, payment_id)) == Decimal("0.00")


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

    def test_a_cancelled_payme_order_is_refused_a_move_to_click_over_http(
        self, client, app, db, sample_user, monkeypatch
    ):
        """The bot's route checks `is_paid` and not the order's status, so the dead-order check in
        `create_payment` is all that stands between a cancelled Payme order and a Click link: nothing moves,
        nothing is reopened, no link is minted."""
        order, payment = _dead_payme_order(
            db, sample_user, order_number="I4-http-dead", status=OrderStatus.CANCELLED
        )
        order_id, payment_id = order.id, payment.id
        before = _left_as_it_was(db, order_id, payment_id)
        minted = []
        monkeypatch.setattr(
            "business_app.services.payment_service.PaymentService.create_payment_link",
            lambda self, payment_id: minted.append(payment_id) or {"payment_url": "https://my.click.uz/x"},
        )

        resp = client.post(
            "/api/v1/payments/create",
            headers=self._headers(app, sample_user),
            json={"order_id": order_id, "payment_method": "click", "return_url": "https://t.me/x"},
        )

        assert resp.status_code == 400, resp.get_json()
        assert resp.get_json()["errors"] == [LEFT_DELIVERY]
        assert minted == []
        assert _left_as_it_was(db, order_id, payment_id) == before

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
