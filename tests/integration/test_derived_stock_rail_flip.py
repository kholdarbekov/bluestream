"""A legacy Payme order may move to Click only if the code pool can cover it.

Tasks 2-7 let a customer place and pay for a CASH order on a marking-code
product even when the pool is empty -- cash draws no code. A customer no longer
moves a cash order onto Click at all (spec 2026-10-27 §4.3, D4). The one rail
move left is a legacy Payme order paid through Click (D2), and Payme draws no
code either, so the move makes the order need one. `payment_service.
create_payment` refuses it with MARKING_CODES_POOL_SHORT when the pool cannot
cover the order, long before Click's PREPARE would notice, and the order stays
on Payme. Driven through `POST /orders/<id>/retry-payment`, the one customer
Pay route, with the body the clients send.

NOTE: `sample_order` (tests/conftest.py) creates the order with ZERO
OrderItem rows, so it never actually draws a marking code regardless of pool
size. Every test below attaches an explicit OrderItem for `sample_product` so
`pool_covers_order` has a code-consuming line to evaluate.
"""

from decimal import Decimal

from business_app import db
from business_app.models.order import Order, OrderItem
from business_app.models.payment import Payment
from business_app.models.product import Product, ProductFiscalProfile, ProductMarkingCode
from business_app.models.translation import Translation
from scripts.seed_backend_translations import BACKEND_TRANSLATIONS, _category_for
from shared.enums import MarkingCodeStatus, PaymentMethod, PaymentStatus

ORDER_ITEM_QUANTITY = 2
POOL_SHORT = "api.payments.error.marking_codes_pool_short"


def make_derived(app, product_id, available_codes=0):
    with app.app_context():
        db.session.add(
            ProductFiscalProfile(
                product_id=product_id,
                fiscalization_enabled=True,
                requires_marking_codes=True,
                spic="SPIC-FLIP",
            )
        )
        for index in range(available_codes):
            db.session.add(
                ProductMarkingCode(
                    product_id=product_id,
                    code=f"FLIP-{product_id}-{index}",
                    status=MarkingCodeStatus.AVAILABLE,
                )
            )
        db.session.commit()


def make_derived_with_reserved_codes(app, product_id, order_id, reserved_codes=ORDER_ITEM_QUANTITY):
    """A product whose pool is EMPTY of AVAILABLE codes, but which already
    holds `reserved_codes` RESERVED codes owned by `order_id` -- simulating a
    prior Click PREPARE having already reserved this exact order's codes."""
    with app.app_context():
        db.session.add(
            ProductFiscalProfile(
                product_id=product_id,
                fiscalization_enabled=True,
                requires_marking_codes=True,
                spic="SPIC-FLIP-RESERVED",
            )
        )
        for index in range(reserved_codes):
            db.session.add(
                ProductMarkingCode(
                    product_id=product_id,
                    order_id=order_id,
                    code=f"FLIP-RESERVED-{product_id}-{index}",
                    status=MarkingCodeStatus.RESERVED,
                )
            )
        db.session.commit()


def add_order_item(app, order_id, product_id, quantity=ORDER_ITEM_QUANTITY):
    """sample_order carries no line items by default; give it one so the
    pool guard has a code-consuming line to check."""
    with app.app_context():
        unit_price = Decimal("15000.00")
        db.session.add(
            OrderItem(
                order_id=order_id,
                product_id=product_id,
                quantity=quantity,
                unit_price=unit_price,
                total_price=unit_price * quantity,
            )
        )
        db.session.commit()


def put_on_payme(app, order_id):
    """The legacy Payme order: its rail and its payment row, nothing collected."""
    with app.app_context():
        order = Order.query.get(order_id)
        order.payment_method = PaymentMethod.PAYME
        db.session.add(
            Payment(
                order_id=order.id,
                user_id=order.user_id,
                payment_method=PaymentMethod.PAYME,
                amount=order.total_amount,
                currency="UZS",
                status=PaymentStatus.PENDING,
                payment_id=f"payme-{order.id}",
            )
        )
        db.session.commit()


def retry_payment(app, order_id, headers):
    """The customer's Pay, with the body the bot and the website send, in English."""
    with app.app_context():
        shown = str(Order.query.get(order_id).total_amount)
    return app.test_client().post(
        f"/api/v1/orders/{order_id}/retry-payment?lang=en",
        json={"channel": "web", "expected_amount": shown},
        headers=headers,
    )


def seed_sentence(app, key):
    """`key` in every language, as the seeder ships it."""
    with app.app_context():
        for language, value in BACKEND_TRANSLATIONS[key].items():
            db.session.add(Translation(key=key, language=language, value=value, category=_category_for(key)))
        db.session.commit()


def rails(app, order_id):
    with app.app_context():
        order = Order.query.get(order_id)
        return order.payment_method, order.payment.payment_method


def test_payme_to_click_is_refused_when_the_pool_is_short(
    matrix_app, db, sample_user, sample_product, sample_order, auth_headers
):
    app = matrix_app
    add_order_item(app, sample_order.id, sample_product.id)
    make_derived(app, sample_product.id, available_codes=0)
    put_on_payme(app, sample_order.id)
    seed_sentence(app, POOL_SHORT)
    with app.app_context():
        product_name = Product.query.get(sample_product.id).name

    resp = retry_payment(app, sample_order.id, auth_headers)

    assert resp.status_code == 400, resp.get_json()
    assert resp.get_json()["data"]["error_code"] == "MARKING_CODES_POOL_SHORT"
    # The words, in `message`, which the bot and the website both show; never "Validation failed", and never
    # advice to pay in cash, which the customer cannot choose (D5).
    assert resp.get_json()["message"] == BACKEND_TRANSLATIONS[POOL_SHORT]["en"].format(product=product_name)
    assert rails(app, sample_order.id) == (PaymentMethod.PAYME, PaymentMethod.PAYME)


def test_payme_to_click_is_allowed_when_the_pool_covers_the_order(
    matrix_app, db, sample_user, sample_product, sample_order, auth_headers
):
    app = matrix_app
    add_order_item(app, sample_order.id, sample_product.id)
    with app.app_context():
        ordered = sum(item.quantity for item in Order.query.get(sample_order.id).order_items)
    make_derived(app, sample_product.id, available_codes=ordered)
    put_on_payme(app, sample_order.id)

    resp = retry_payment(app, sample_order.id, auth_headers)

    assert resp.status_code == 200, resp.get_json()
    assert rails(app, sample_order.id) == (PaymentMethod.CLICK, PaymentMethod.CLICK)


def test_retry_on_an_order_already_on_click_with_reserved_codes_still_succeeds(
    matrix_app, db, sample_user, sample_product, sample_order, auth_headers
):
    """C-1: the guard is a FLIP guard, and must credit codes this order
    already holds.

    An order already sitting on CLICK -- e.g. a prior PREPARE already
    reserved its codes, or the customer opened Click, abandoned, and is now
    tapping Retry -- must stay payable even though the shared AVAILABLE pool
    for that product now reads 0. PREPARE itself would succeed here, because
    it credits codes the payment already holds
    (PaymentFiscalizationService._codes_currently_held); the guard must not
    be stricter than PREPARE.
    """
    app = matrix_app
    add_order_item(app, sample_order.id, sample_product.id)
    with app.app_context():
        order = Order.query.get(sample_order.id)
        order.payment_method = PaymentMethod.CLICK
        db.session.commit()
    make_derived_with_reserved_codes(
        app, sample_product.id, sample_order.id, reserved_codes=ORDER_ITEM_QUANTITY
    )

    resp = retry_payment(app, sample_order.id, auth_headers)

    assert resp.status_code == 200, resp.get_json()


def test_releasing_codes_returns_them_and_re_derives_stock(app, db, sample_product, sample_order):
    """A provider error must hand the codes back and bring stock with them.

    Task 9: reserve drops derived stock, release (with a provider-error
    reason) must undo that exactly -- codes back to AVAILABLE, stock
    re-derived to the pre-reservation count. sample_order carries zero
    OrderItem rows (see module docstring), so an explicit item is attached
    first or reserve_required_marking_codes has nothing to reserve.
    """
    from business_app.services.payment_fiscalization_service import PaymentFiscalizationService

    product_id = sample_product.id
    make_derived(app, product_id, available_codes=4)
    add_order_item(app, sample_order.id, product_id, quantity=4)
    with app.app_context():
        order = Order.query.get(sample_order.id)
        order.payment_method = PaymentMethod.CLICK
        payment = Payment(
            order_id=order.id,
            user_id=order.user_id,
            payment_method=PaymentMethod.CLICK,
            amount=order.total_amount,
            payment_id="test-release-roundtrip",
        )
        db.session.add(payment)
        db.session.commit()
        payment_id = payment.id

    with app.app_context():
        service = PaymentFiscalizationService()
        service.reserve_required_marking_codes(Payment.query.get(payment_id))
        db.session.commit()
        reserved_stock = Product.query.get(product_id).stock_quantity

    with app.app_context():
        service = PaymentFiscalizationService()
        service.release_reserved_marking_codes(Payment.query.get(payment_id), reason="provider_error")
        db.session.commit()

    with app.app_context():
        assert Product.query.get(product_id).stock_quantity == 4
        assert reserved_stock < 4
        assert (
            ProductMarkingCode.query.filter_by(
                product_id=product_id, status=MarkingCodeStatus.AVAILABLE
            ).count()
            == 4
        )
