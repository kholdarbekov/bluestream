"""PREPARE is the sole authority on payability (policy 2026-08-24, Phase 4A).

`handle_prepare` had NO status, method or order-state guard: it looked the
payment up, checked only the amount, and answered `error: 0` — which in the
Click protocol IS the authorisation to debit the card. Prod incident
TG_000413_26: a PREPARE arrived 28 hours after we had auto-cancelled the
payment, we said "Success, proceed", Click debited 54 000, and the money had
nowhere to go.

The payable window now runs from order creation until the order is SETTLED or
dead — deliberately THROUGH delivery, because a customer who took delivery
without paying cash keeps the Click rail and may pay the link afterwards.
"""

from decimal import Decimal

import pytest

from business_app import db
from business_app.models.payment import Payment
from shared.enums import OrderStatus, PaymentMethod, PaymentStatus

from tests.integration.fake_gateways import TEST_CLICK_SHOP_SECRET_KEY, make_click_webhook_form
from tests.integration.test_payment_matrix import _seed_click_payment

WEBHOOK_URL = "/api/v1/payments/webhook/click"


def _post_prepare(client, order, *, click_trans_id="930001", amount=None):
    form = make_click_webhook_form(
        action="0",
        click_trans_id=click_trans_id,
        merchant_trans_id=order.order_number,
        amount=str(int(amount if amount is not None else order.total_amount)),
        secret_key=TEST_CLICK_SHOP_SECRET_KEY,
        error=0,
        click_paydoc_id="5231141285",
    )
    return client.post(WEBHOOK_URL, data=form, content_type="application/x-www-form-urlencoded")


class TestPrepareRefusesUnpayableOrders:
    @pytest.mark.parametrize("order_status", [OrderStatus.CANCELLED, OrderStatus.RETURNED])
    def test_dead_order_is_refused(self, matrix_client, matrix_app, db, order_with_address,
                                   no_fiscalization, order_status):
        order = order_with_address
        order.status = order_status
        _seed_click_payment(db, order)
        db.session.commit()

        body = _post_prepare(matrix_client, order).get_json()

        assert body["error"] != 0, "a dead order must never authorise a card debit"

    def test_already_paid_order_is_refused(self, matrix_client, matrix_app, db, order_with_address,
                                           no_fiscalization):
        """Case C prevention: cash was taken at the door, so the link must stop working."""
        order = order_with_address
        order.status = OrderStatus.DELIVERED
        order.is_paid = True
        _seed_click_payment(db, order)
        db.session.commit()

        body = _post_prepare(matrix_client, order).get_json()

        assert body["error"] != 0

    @pytest.mark.parametrize("payment_status", [PaymentStatus.CANCELLED, PaymentStatus.FAILED,
                                                PaymentStatus.COMPLETED])
    def test_non_awaiting_payment_is_refused(self, matrix_client, matrix_app, db, order_with_address,
                                             no_fiscalization, payment_status):
        """The exact incident: a PREPARE on an auto-cancelled payment said error 0."""
        order = order_with_address
        payment = _seed_click_payment(db, order)
        payment.status = payment_status
        db.session.commit()

        body = _post_prepare(matrix_client, order).get_json()

        assert body["error"] != 0, (
            f"a {payment_status.value} payment must not authorise a debit"
        )

    def test_cash_rail_is_refused(self, matrix_client, matrix_app, db, order_with_address,
                                  no_fiscalization):
        order = order_with_address
        payment = _seed_click_payment(db, order)
        payment.payment_method = PaymentMethod.CASH
        order.payment_method = PaymentMethod.CASH
        db.session.commit()

        body = _post_prepare(matrix_client, order).get_json()

        assert body["error"] != 0

    def test_refusal_happens_before_the_amount_check(self, matrix_client, matrix_app, db,
                                                     order_with_address, no_fiscalization):
        """Answering -2 'Incorrect amount' for a cancelled order is a lie, and it
        tells the customer to retry with a different amount."""
        order = order_with_address
        order.status = OrderStatus.CANCELLED
        _seed_click_payment(db, order)
        db.session.commit()

        body = _post_prepare(matrix_client, order, amount=999).get_json()

        assert body["error"] != -2, "a dead order must be refused on its own merits"
        assert body["error"] != 0


class TestPrepareStillAllowsGenuinePayment:
    @pytest.mark.parametrize("order_status", [
        OrderStatus.PENDING,
        OrderStatus.CONFIRMED,
        OrderStatus.PREPARING,
        OrderStatus.OUT_FOR_DELIVERY,
    ])
    def test_live_unpaid_order_is_payable(self, matrix_client, matrix_app, db, order_with_address,
                                          no_fiscalization, order_status):
        order = order_with_address
        order.status = order_status
        _seed_click_payment(db, order)
        db.session.commit()

        body = _post_prepare(matrix_client, order).get_json()

        assert body["error"] == 0
        assert str(body["merchant_prepare_id"]) == str(Payment.query.filter_by(order_id=order.id).first().id)

    def test_delivered_but_unpaid_order_stays_payable(self, matrix_client, matrix_app, db,
                                                      order_with_address, no_fiscalization):
        """CASE B — the customer took delivery without paying cash. The order keeps
        the Click rail and the link must keep working, or the money can never
        arrive and the receipt can never be issued."""
        order = order_with_address
        order.status = OrderStatus.DELIVERED
        order.is_paid = False
        _seed_click_payment(db, order)
        db.session.commit()

        body = _post_prepare(matrix_client, order).get_json()

        assert body["error"] == 0, "case B must remain payable after delivery"


class TestPrepareFailsClosedOnMarkingCodes:
    def test_marking_code_shortfall_returns_a_protocol_error_not_a_500(
        self, matrix_client, matrix_app, db, order_with_address, monkeypatch
    ):
        """`reserve_required_marking_codes` RAISES ValidationError when the pool is
        short. handle_prepare did not catch it, so Click received a 5xx instead of
        a protocol code and retried forever."""
        from business_app.services.payment_fiscalization_service import PaymentFiscalizationService
        from business_app.utils.exceptions import ValidationError

        order = order_with_address
        _seed_click_payment(db, order)
        db.session.commit()

        def boom(self, payment, **kwargs):
            raise ValidationError("Not enough marking codes for product Water")

        monkeypatch.setattr(PaymentFiscalizationService, "reserve_required_marking_codes", boom)

        resp = _post_prepare(matrix_client, order)

        assert resp.status_code == 200, "Click must get a protocol answer, never a 5xx"
        assert resp.get_json()["error"] != 0, "we must not authorise a debit we cannot fiscalize"

    def test_shortfall_on_one_item_leaves_an_earlier_items_code_stuck_reserved(
        self, matrix_client, matrix_app, db, order_with_address, sample_product
    ):
        """Multi-item order, one payment: product A's pool covers its line,
        product B's pool is empty. A's code is reserved before B's shortfall
        raises -- the refused PREPARE must not leave it behind.

        Not just a leak: Phase 4D ownership policy means A's code is not
        unrecoverable -- it stays tied to this still-open order and would be
        freed by the order_service.py:1793 cancel-cascade if the order is
        later cancelled/returned. But nothing drives that on a plain refused
        PREPARE, so the code sits RESERVED and stock desynced indefinitely
        against a payment attempt Click itself was told was cancelled.
        """
        from business_app.models.order import OrderItem, OrderItemMarkingCodeAllocation
        from business_app.models.product import Product, ProductFiscalProfile, ProductMarkingCode
        from business_app.services.product_fiscal_service import ProductFiscalService
        from shared.enums import MarkingCodeStatus

        order = order_with_address
        product_a = sample_product
        product_b = Product(
            name="Product B (empty pool)",
            category_id=product_a.category_id,
            size="19L",
            volume=19.0,
            volume_unit="L",
            base_price=Decimal("15000.00"),
            stock_quantity=0,
            is_active=True,
        )
        db.session.add(product_b)
        db.session.commit()

        db.session.add_all(
            [
                ProductFiscalProfile(
                    product_id=product_a.id,
                    fiscalization_enabled=True,
                    requires_marking_codes=True,
                    spic="SPIC-A",
                ),
                ProductFiscalProfile(
                    product_id=product_b.id,
                    fiscalization_enabled=True,
                    requires_marking_codes=True,
                    spic="SPIC-B",
                ),
            ]
        )
        # Product A has one AVAILABLE code; product B's pool is empty.
        db.session.add(
            ProductMarkingCode(
                product_id=product_a.id, code=f"A-CODE-{product_a.id}", status=MarkingCodeStatus.AVAILABLE
            )
        )
        db.session.commit()
        # Derive A's stock the same way production code does, so the
        # pre-attempt state genuinely reflects "1 code, 1 unit of stock" —
        # not a hand-set number unrelated to the pool.
        ProductFiscalService.sync_stock_from_marking_codes(product_a)
        db.session.commit()

        # ORDERING DEPENDENCY — load-bearing, and it is NOT insertion order.
        # `_plan_marking_code_reservation` sorts the lines by `(product_id, id)`
        # (payment_fiscalization_service.py:1379) to keep the FOR UPDATE lock
        # order deterministic. So A is planned before B only because A's product
        # row was created first and therefore holds the lower product_id. If
        # that ever flips, B would be planned first, nothing would ever be
        # planned for A, and this test would keep passing while pinning nothing.
        assert product_a.id < product_b.id, (
            "product A must sort first under the planner's (product_id, id) order, "
            "or the shortfall raises before A's line is ever planned and this test "
            "stops exercising the leak it exists to pin"
        )
        db.session.add_all(
            [
                OrderItem(
                    order_id=order.id,
                    product_id=product_a.id,
                    quantity=1,
                    unit_price=Decimal("15000.00"),
                    total_price=Decimal("15000.00"),
                ),
                OrderItem(
                    order_id=order.id,
                    product_id=product_b.id,
                    quantity=1,
                    unit_price=Decimal("15000.00"),
                    total_price=Decimal("15000.00"),
                ),
            ]
        )
        db.session.commit()

        _seed_click_payment(db, order)
        db.session.commit()

        resp = _post_prepare(matrix_client, order)
        body = resp.get_json()
        assert body["error"] != 0, "must refuse -- product B's pool is empty"

        # What SHOULD be true after a refused PREPARE: product A's code was
        # never actually spent by an attempt Click itself was told failed.
        assert (
            ProductMarkingCode.query.filter_by(
                product_id=product_a.id, status=MarkingCodeStatus.AVAILABLE
            ).count()
            == 1
        ), "product A's code must not be left RESERVED by a refused multi-item PREPARE"
        assert Product.query.get(product_a.id).stock_quantity == 1, (
            "product A's derived stock must still equal its true AVAILABLE-code count"
        )
        # The ledger is where an all-or-nothing plan differs from a compensating
        # release: a scoped release would leave a RESERVED+RELEASED pair behind.
        assert OrderItemMarkingCodeAllocation.query.filter_by(order_id=order.id).count() == 0, (
            "a refused PREPARE must leave no marking-code ledger row at all -- not a "
            "RESERVED row, and not a RESERVED+RELEASED compensating pair"
        )

    def test_single_item_shortfall_refuses_without_reserving_or_resyncing(
        self, matrix_client, matrix_app, db, order_with_address, sample_product, monkeypatch
    ):
        """The unchanged half of the contract. A one-item order whose pool is
        empty must behave EXACTLY as before the plan-then-mutate restructure:
        ValidationError inside reserve, `-9` on the wire, HTTP 200, nothing
        written, and the `on_empty` replenish still fired (a deliberate
        cross-system effect that survives the raise)."""
        from business_app.models.order import OrderItem, OrderItemMarkingCodeAllocation
        from business_app.models.product import Product, ProductFiscalProfile, ProductMarkingCode
        from business_app.services.payment_fiscalization_service import PaymentFiscalizationService
        from shared.enums import MarkingCodeStatus

        order = order_with_address
        product = sample_product
        product.stock_quantity = 7  # legacy drift: NOT the AVAILABLE-code count
        db.session.add(
            ProductFiscalProfile(
                product_id=product.id,
                fiscalization_enabled=True,
                requires_marking_codes=True,
                spic="SPIC-SOLO",
            )
        )
        db.session.add(
            OrderItem(
                order_id=order.id,
                product_id=product.id,
                quantity=1,
                unit_price=Decimal("15000.00"),
                total_price=Decimal("15000.00"),
            )
        )
        db.session.commit()

        _seed_click_payment(db, order)
        db.session.commit()

        replenish_calls = []
        monkeypatch.setattr(
            PaymentFiscalizationService,
            "_safe_trigger_replenish",
            lambda self, product_id, run_kind: replenish_calls.append((product_id, run_kind)),
        )

        resp = _post_prepare(matrix_client, order, click_trans_id="930099")

        assert resp.status_code == 200, "Click must get a protocol answer, never a 5xx"
        assert resp.get_json()["error"] == -9, "an unfiscalizable order must be cancelled, not authorised"

        db.session.expire_all()
        assert ProductMarkingCode.query.filter_by(status=MarkingCodeStatus.RESERVED).count() == 0
        assert OrderItemMarkingCodeAllocation.query.filter_by(order_id=order.id).count() == 0
        assert Product.query.get(product.id).stock_quantity == 7, (
            "a refused webhook must not silently rewrite unrelated legacy stock drift"
        )
        assert replenish_calls == [(product.id, "on_empty")], (
            "the empty-pool replenish is a deliberate cross-system effect and must survive the raise"
        )
