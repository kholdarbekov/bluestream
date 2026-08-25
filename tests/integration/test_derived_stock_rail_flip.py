"""A cash order may move to an online rail only if the code pool can cover it.

Tasks 2-7 let a customer place and pay for a CASH order on a marking-code
product even when the pool is empty -- cash draws no code. This closes the
resulting hole at the one place the order's rail actually moves:
`payment_service.create_payment` flips `order.payment_method` to CLICK the
moment a payment link is created, long before Click's PREPARE would notice
the pool is short. The requirement is that the order stays on cash when the
pool cannot cover it.

NOTE: `sample_order` (tests/conftest.py) creates the order with ZERO
OrderItem rows, so it never actually draws a marking code regardless of pool
size. Both tests below attach an explicit OrderItem for `sample_product` so
`pool_covers_order` has a code-consuming line to evaluate.
"""

from decimal import Decimal

from business_app import db
from business_app.models.order import Order, OrderItem
from business_app.models.product import Product, ProductFiscalProfile, ProductMarkingCode
from shared.enums import MarkingCodeStatus, PaymentMethod

ORDER_ITEM_QUANTITY = 2


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


def test_flip_to_click_is_refused_when_the_pool_is_short(
    app, db, sample_user, sample_product, sample_order, auth_headers
):
    add_order_item(app, sample_order.id, sample_product.id)
    make_derived(app, sample_product.id, available_codes=0)
    with app.app_context():
        order = Order.query.get(sample_order.id)
        order.payment_method = PaymentMethod.CASH
        db.session.commit()

    resp = app.test_client().post(
        "/api/v1/payments/create",
        json={"order_id": sample_order.id, "payment_method": "click"},
        headers=auth_headers,
    )

    assert resp.status_code == 400, resp.get_json()
    with app.app_context():
        assert Order.query.get(sample_order.id).payment_method == PaymentMethod.CASH


def test_flip_to_click_is_allowed_when_the_pool_covers_the_order(
    app, db, sample_user, sample_product, sample_order, auth_headers
):
    add_order_item(app, sample_order.id, sample_product.id)
    with app.app_context():
        ordered = sum(item.quantity for item in Order.query.get(sample_order.id).order_items)
    make_derived(app, sample_product.id, available_codes=ordered)
    with app.app_context():
        order = Order.query.get(sample_order.id)
        order.payment_method = PaymentMethod.CASH
        db.session.commit()

    resp = app.test_client().post(
        "/api/v1/payments/create",
        json={"order_id": sample_order.id, "payment_method": "click"},
        headers=auth_headers,
    )

    assert resp.status_code in (200, 201), resp.get_json()


def test_retry_on_an_order_already_on_click_with_reserved_codes_still_succeeds(
    app, db, sample_user, sample_product, sample_order, auth_headers
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
    add_order_item(app, sample_order.id, sample_product.id)
    with app.app_context():
        order = Order.query.get(sample_order.id)
        order.payment_method = PaymentMethod.CLICK
        db.session.commit()
    make_derived_with_reserved_codes(
        app, sample_product.id, sample_order.id, reserved_codes=ORDER_ITEM_QUANTITY
    )

    resp = app.test_client().post(
        "/api/v1/payments/create",
        json={"order_id": sample_order.id, "payment_method": "click"},
        headers=auth_headers,
    )

    assert resp.status_code in (200, 201), resp.get_json()


def test_releasing_codes_returns_them_and_re_derives_stock(app, db, sample_product, sample_order):
    """A provider error must hand the codes back and bring stock with them.

    Task 9: reserve drops derived stock, release (with a provider-error
    reason) must undo that exactly -- codes back to AVAILABLE, stock
    re-derived to the pre-reservation count. sample_order carries zero
    OrderItem rows (see module docstring), so an explicit item is attached
    first or reserve_required_marking_codes has nothing to reserve.
    """
    from business_app.models.payment import Payment
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
