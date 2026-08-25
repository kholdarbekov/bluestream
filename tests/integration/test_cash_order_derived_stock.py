"""A marking-code product's stock_quantity is owned by the code pool.

No order-driven delta may touch it -- cash or card -- and a cash order is
neither gated by it nor moves it. A product WITHOUT marking codes keeps
today's behaviour exactly; every test here that asserts the new rule has a
twin asserting that boundary.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from business_app import db
from business_app.models.order import Order, OrderItem
from business_app.models.product import Product, ProductFiscalProfile, ProductMarkingCode
from shared.enums import MarkingCodeStatus, OrderStatus, PaymentMethod


def make_derived(app, product_id, available_codes=0):
    """Turn a product into a marking-code product with a pool of N available codes."""
    with app.app_context():
        db.session.add(
            ProductFiscalProfile(
                product_id=product_id,
                fiscalization_enabled=True,
                requires_marking_codes=True,
                spic="SPIC-DERIVED",
            )
        )
        for index in range(available_codes):
            db.session.add(
                ProductMarkingCode(
                    product_id=product_id,
                    code=f"DERIVED-{product_id}-{index}",
                    status=MarkingCodeStatus.AVAILABLE,
                )
            )
        db.session.commit()


def stock_of(app, product_id):
    with app.app_context():
        return Product.query.get(product_id).stock_quantity


def _make_order_with_item(app, user_id, product_id, unit_price, quantity):
    """Build an order with a single item for `product_id`.

    `sample_order` (tests/conftest.py:557-574) creates an Order row but never
    attaches any order_items, so `confirm_reservations` -- which iterates
    `order.order_items` -- would have nothing to act on. Per the task brief,
    the shared fixture is not widened for this one suite; the order is built
    inline here instead, following the same pattern as
    tests/integration/test_admin_order_edit.py's `_seed_order_with_item`.
    """
    with app.app_context():
        subtotal = unit_price * quantity
        order = Order(
            user_id=user_id,
            order_number=f"ORD-DERIVED-{product_id}-{quantity}",
            status=OrderStatus.PENDING,
            subtotal=subtotal,
            delivery_fee=Decimal("3000.00"),
            discount_amount=Decimal("0.00"),
            loyalty_discount=Decimal("0.00"),
            total_amount=subtotal + Decimal("3000.00"),
            delivery_notes="Test order",
            created_at=datetime.now(UTC),
        )
        db.session.add(order)
        db.session.flush()

        item = OrderItem(
            order_id=order.id,
            product_id=product_id,
            quantity=quantity,
            unit_price=unit_price,
            discount_amount=Decimal("0.00"),
            total_price=subtotal,
        )
        db.session.add(item)
        db.session.commit()
        return order.id


def test_confirm_reservations_does_not_decrement_derived_stock(app, db, sample_product, sample_user):
    """The pool owns the number; an order confirmation must not move it."""
    from business_app.services.inventory_service import InventoryService

    product_id = sample_product.id
    make_derived(app, product_id, available_codes=5)
    with app.app_context():
        product = Product.query.get(product_id)
        product.stock_quantity = 5
        db.session.commit()

    order_id = _make_order_with_item(app, sample_user.id, product_id, sample_product.base_price, quantity=2)

    # confirm_reservations() deliberately does not commit -- "the calling code
    # (payment processing) manages the transaction" (its own docstring). Flask-
    # SQLAlchemy 3.x scopes db.session per app-context id, so each `with
    # app.app_context()` block above got its own session; calling and
    # committing inside the SAME block here is what makes the mutation (or, in
    # this test, its absence) durable for stock_of()'s independent read below.
    with app.app_context():
        InventoryService().confirm_reservations(order_id)
        db.session.commit()

    assert stock_of(app, product_id) == 5


def test_confirm_reservations_still_decrements_non_derived_stock(app, db, sample_product, sample_user):
    """Scope boundary: a product without marking codes is unaffected by this change."""
    from business_app.services.inventory_service import InventoryService

    product_id = sample_product.id
    with app.app_context():
        before = Product.query.get(product_id).stock_quantity

    quantity = 2
    order_id = _make_order_with_item(app, sample_user.id, product_id, sample_product.base_price, quantity=quantity)

    with app.app_context():
        InventoryService().confirm_reservations(order_id)
        db.session.commit()

    assert stock_of(app, product_id) == before - quantity


def test_cancelling_an_order_does_not_restore_derived_stock(app, db, sample_product, sample_user):
    """Never give back what was never taken -- otherwise every cancel inflates the pool.

    Deliberately does NOT use the shared `sample_order` fixture: per
    `_make_order_with_item`'s docstring above, `sample_order` attaches zero
    OrderItem rows, so `_restore_stock_for_order`'s `for item in
    order.order_items:` loop would be empty and the guard under test would
    never run -- the test would pass whether or not the fix exists. Building
    the order through `_make_order_with_item` (same pattern as the two tests
    above) gives the loop a real item to restore stock for.
    """
    from business_app.services.order_service import OrderService

    product_id = sample_product.id
    make_derived(app, product_id, available_codes=5)
    with app.app_context():
        product = Product.query.get(product_id)
        product.stock_quantity = 5
        db.session.commit()

    order_id = _make_order_with_item(app, sample_user.id, product_id, sample_product.base_price, quantity=2)
    with app.app_context():
        order = db.session.get(Order, order_id)
        order.status = OrderStatus.CONFIRMED
        order.payment_method = PaymentMethod.CLICK
        db.session.commit()

    # A fresh context so cancel_order's own queries see the CONFIRMED/CLICK
    # commit above and the stock_quantity=5 commit further up, instead of a
    # stale Product/Order pulled from whatever identity map the test function's
    # ambient context would otherwise reuse from `sample_product`'s creation.
    with app.app_context():
        OrderService().cancel_order(order_id, reason="test")

    assert stock_of(app, product_id) == 5


def test_editing_a_delivered_cash_order_down_does_not_inflate_derived_stock(app, db, sample_product, sample_user):
    """order_edit_service.past_deduction assumes the DELIVERED-cash decrement ran.

    It no longer does, so the edit must not hand units back to the pool.

    Deliberately does NOT use the shared `sample_order` fixture -- per
    `_make_order_with_item`'s docstring above, `sample_order` attaches zero
    OrderItem rows, and `OrderEditService._build_plan` needs a real
    `order_item_id` to edit against.
    """
    from business_app.services.order_edit_service import OrderEditItemSpec, OrderEditService

    product_id = sample_product.id
    make_derived(app, product_id, available_codes=5)
    with app.app_context():
        Product.query.get(product_id).stock_quantity = 5
        db.session.commit()

    order_id = _make_order_with_item(app, sample_user.id, product_id, sample_product.base_price, quantity=3)
    with app.app_context():
        order = db.session.get(Order, order_id)
        order.status = OrderStatus.DELIVERED
        order.payment_method = PaymentMethod.CASH
        item = order.order_items[0]
        original_qty = item.quantity
        item_id = item.id
        db.session.commit()

    # OrderEditService.apply_edit() commits internally via atomic_transaction(),
    # but per the harness rule learned in Tasks 2/3, the call plus an explicit
    # commit still belong in the SAME app-context block as each other so the
    # write is durable before stock_of() opens its own independent context/
    # session to read it back.
    with app.app_context():
        OrderEditService().apply_edit(
            order_id=order_id,
            items=[
                OrderEditItemSpec(
                    product_id=product_id,
                    quantity=original_qty - 1,
                    order_item_id=item_id,
                )
            ],
            reason="driver over-delivered by one unit",
            actor_user_id=sample_user.id,
        )
        db.session.commit()

    assert stock_of(app, product_id) == 5


def test_editing_up_at_zero_derived_stock_is_allowed(app, db, sample_product, sample_user):
    """The insufficient_stock raise must not fire for a pool-owned product.

    Proves the guard sits BEFORE the insufficiency check: at zero pool stock,
    an edit that would otherwise drive stock negative must be allowed to go
    through (and must leave stock_quantity untouched), not raise.
    """
    from business_app.services.order_edit_service import OrderEditItemSpec, OrderEditService

    product_id = sample_product.id
    make_derived(app, product_id, available_codes=0)
    with app.app_context():
        Product.query.get(product_id).stock_quantity = 0
        db.session.commit()

    order_id = _make_order_with_item(app, sample_user.id, product_id, sample_product.base_price, quantity=3)
    with app.app_context():
        order = db.session.get(Order, order_id)
        order.status = OrderStatus.DELIVERED
        order.payment_method = PaymentMethod.CASH
        item = order.order_items[0]
        original_qty = item.quantity
        item_id = item.id
        db.session.commit()

    with app.app_context():
        OrderEditService().apply_edit(
            order_id=order_id,
            items=[
                OrderEditItemSpec(
                    product_id=product_id,
                    quantity=original_qty + 5,
                    order_item_id=item_id,
                )
            ],
            reason="customer requested five more units",
            actor_user_id=sample_user.id,
        )
        db.session.commit()

    assert stock_of(app, product_id) == 0


def test_availability_reads_the_code_pool_not_the_stale_column(app, db, sample_product):
    """A stale projection must not let a card order through the gate."""
    from business_app.services.inventory_service import InventoryService

    product_id = sample_product.id
    make_derived(app, product_id, available_codes=2)
    with app.app_context():
        Product.query.get(product_id).stock_quantity = 486  # stale, like prod
        db.session.commit()

    with app.app_context():
        result = InventoryService().check_product_availability(product_id, 10)

    assert result.available_quantity == 2
    assert result.is_available is False


def test_availability_ignores_min_stock_level_for_derived_products(app, db, sample_product):
    """You cannot hold back a reserve of legal codes; the breach arm has no meaning."""
    from business_app.services.inventory_service import InventoryService

    product_id = sample_product.id
    make_derived(app, product_id, available_codes=3)
    with app.app_context():
        product = Product.query.get(product_id)
        product.stock_quantity = 3
        product.min_stock_level = 10
        db.session.commit()

    with app.app_context():
        result = InventoryService().check_product_availability(product_id, 3)

    assert result.is_available is True


def test_availability_still_reads_the_column_for_non_derived_products(app, db, sample_product):
    """Scope boundary."""
    from business_app.services.inventory_service import InventoryService

    product_id = sample_product.id
    with app.app_context():
        Product.query.get(product_id).stock_quantity = 7
        db.session.commit()

    with app.app_context():
        result = InventoryService().check_product_availability(product_id, 7)

    assert result.available_quantity == 7


CASH_ORDER = "cash"


def _order_payload(product_id, address_id, payment_method, quantity=2):
    return {
        "items": [{"product_id": product_id, "quantity": quantity}],
        "delivery_address": {
            "delivery_address_id": address_id,
            "street": "1 Test St",
            "latitude": 41.3111,
            "longitude": 69.2797,
        },
        "payment_method": payment_method,
    }


def test_cash_order_succeeds_for_a_derived_product_with_an_empty_pool(
    app, db, sample_user, sample_product, user_address
):
    """The headline requirement.

    create_order() runs inside the SAME `with app.app_context()` as the
    stock_quantity mutation+commit, not the test function's ambient (`db`
    fixture) context -- per the harness rule, a bare call outside the `with`
    would run on the ambient session, which already cached `sample_product`
    (refreshed at the `product_id = sample_product.id` access below) and
    would never see this block's commit, exactly the "spurious 102 == 5"
    stale-identity-map trap Task 3 hit.
    """
    from business_app.services.order_service import OrderService

    product_id = sample_product.id
    make_derived(app, product_id, available_codes=0)
    with app.app_context():
        Product.query.get(product_id).stock_quantity = 0
        db.session.commit()

        order = OrderService().create_order(
            sample_user.id, _order_payload(product_id, user_address.id, CASH_ORDER)
        )

    assert order is not None
    assert stock_of(app, product_id) == 0


def test_card_order_is_still_refused_for_a_derived_product_with_an_empty_pool(
    app, db, sample_user, sample_product, user_address
):
    """create_order() shares the mutation's app_context(); see the sibling test's docstring."""
    from business_app.services.order_service import OrderService
    from business_app.utils.exceptions import ValidationError

    product_id = sample_product.id
    make_derived(app, product_id, available_codes=0)
    with app.app_context():
        Product.query.get(product_id).stock_quantity = 0
        db.session.commit()

        with pytest.raises(ValidationError) as exc:
            OrderService().create_order(
                sample_user.id, _order_payload(product_id, user_address.id, "click")
            )

    assert sample_product.name in str(exc.value)


def test_cash_order_is_still_refused_for_a_NON_derived_product_out_of_stock(
    app, db, sample_user, sample_product, user_address
):
    """Scope boundary: cash still consumes -- and is still gated by -- ordinary stock.

    create_order() shares the mutation's app_context(); see the first test's
    docstring above. This is the one test in the trio where the staleness
    trap is NOT hidden by an availability path that ignores stock_quantity
    (derived-cash filters the item out entirely; derived-card reads the code
    pool, not the column) -- a non-derived product's availability reads
    stock_quantity directly, so a bare call here would falsely pass.
    """
    from business_app.services.order_service import OrderService
    from business_app.utils.exceptions import ValidationError

    product_id = sample_product.id
    with app.app_context():
        Product.query.get(product_id).stock_quantity = 0
        db.session.commit()

        with pytest.raises(ValidationError):
            OrderService().create_order(
                sample_user.id, _order_payload(product_id, user_address.id, CASH_ORDER)
            )


def test_cash_order_at_empty_pool_succeeds_over_http(
    app, db, sample_user, sample_product, user_address, auth_headers
):
    """The customer's real path: POST /api/v1/orders/ with the body the UI sends.

    sample_user.is_verified=True, but @require_verification("phone") reads the
    phone_verified PROPERTY (business_app/models/user.py:157-160), derived
    from phone_verified_at, not is_verified. Set it here rather than on the
    shared fixture, per the task brief.

    Mutated directly on `sample_product` / `sample_user` -- the objects
    already living in the test's AMBIENT session -- and committed with no
    extra `with app.app_context()` wrapper. Flask's RequestContext.push()
    reuses whatever app context is already active for THIS app rather than
    pushing a fresh one (Werkzeug only pushes new when none is active, or it
    belongs to a different app), so `app.test_client().post()` below runs on
    this SAME ambient session, not an isolated one. A mutation made through a
    separate nested `with app.app_context()` (as the non-HTTP tests above use
    to dodge stale identity-mapped reads) would therefore land on a session
    the request never sees -- the inverse of those tests' fix.
    """
    product_id = sample_product.id
    make_derived(app, product_id, available_codes=0)

    sample_product.stock_quantity = 0
    sample_user.phone_verified_at = datetime.now(UTC)
    db.session.commit()

    # NOT `_order_payload()`: that builder's nested `delivery_address` object
    # matches the SERVICE's `order_data` shape (what `create_order()` reads
    # directly, used by the three tests above). The wire contract is
    # different -- `CreateOrderRequest` (business_app/serializers/
    # order_serializers.py:144-163) declares a top-level `delivery_address_id`
    # and has no `delivery_address` field at all, so the nested id is silently
    # dropped by Pydantic and the endpoint 400s on "address not found" before
    # ever reaching the code under test. This is the real body the UI sends.
    resp = app.test_client().post(
        "/api/v1/orders/",
        json={
            "items": [{"product_id": product_id, "quantity": 2}],
            "delivery_address_id": user_address.id,
            "payment_method": CASH_ORDER,
        },
        headers=auth_headers,
    )

    assert resp.status_code == 201, resp.get_json()


def test_omitted_payment_method_is_still_refused_for_a_derived_product_with_an_empty_pool(
    app, db, sample_user, sample_product, user_address
):
    """Regression pin for `_requested_method_is_cash`'s `== CASH` form.

    Global constraint: payment_method is nullable (dev carries three NULL
    rows), so the helper is deliberately `== CASH`, never
    `not in {CLICK, CARD, BUSINESS_ACCOUNT}`. The inverse form would treat an
    omitted/NULL method as "not one of those three named rails" and therefore
    -- wrongly -- as cash, filtering the derived line out of both gates and
    letting a free, invisible order through. An omitted method actually
    resolves to business_account (business_app/services/order_service.py's
    `_resolve_payment_method`, which runs AFTER this gate) -- never cash --
    so the gate must still see and refuse this item. If the helper is ever
    rewritten to the inverse form, this test flips from refused to accepted.

    No `payment_method` key at all in the payload (not even None): that is
    the shape an omitted field actually takes over the wire / in
    `order_data.get("payment_method")`.
    """
    from business_app.services.order_service import OrderService
    from business_app.utils.exceptions import ValidationError

    product_id = sample_product.id
    make_derived(app, product_id, available_codes=0)
    with app.app_context():
        Product.query.get(product_id).stock_quantity = 0
        db.session.commit()

        payload = {
            "items": [{"product_id": product_id, "quantity": 2}],
            "delivery_address": {
                "delivery_address_id": user_address.id,
                "street": "1 Test St",
                "latitude": 41.3111,
                "longitude": 69.2797,
            },
            # payment_method deliberately absent.
        }

        with pytest.raises(ValidationError) as exc:
            OrderService().create_order(sample_user.id, payload)

    assert sample_product.name in str(exc.value)


def test_cash_order_mixed_basket_only_filters_the_derived_line(
    app, db, sample_user, sample_product, user_address
):
    """`_stock_gated_items` must drop ONLY the cash+derived line, not the whole basket.

    Every prior test uses a single-line basket, where "drop this one line"
    and "drop the entire list" are indistinguishable -- both leave zero items
    to gate. A two-line cash basket -- one derived+empty-pool line (correctly
    exempt) and one ordinary out-of-stock line (must still gate) -- tells
    them apart: if the filter ever regresses to clearing the whole list once
    ANY line in the order is derived, the ordinary line's stock check is
    skipped too and this order would wrongly succeed.
    """
    from business_app.services.order_service import OrderService
    from business_app.utils.exceptions import ValidationError

    derived_product_id = sample_product.id
    make_derived(app, derived_product_id, available_codes=0)

    second_product_name = "Sparkling Water 5L"

    with app.app_context():
        Product.query.get(derived_product_id).stock_quantity = 0

        second_product = Product(
            name=second_product_name,
            description="Second product for the mixed-basket regression test",
            category_id=sample_product.category_id,
            size="5L",
            volume=5.0,
            volume_unit="L",
            base_price=Decimal("8000.00"),
            stock_quantity=0,
            min_stock_level=5,
            max_stock_level=200,
            is_active=True,
            created_at=datetime.now(UTC),
        )
        db.session.add(second_product)
        db.session.commit()
        second_product_id = second_product.id

        payload = {
            "items": [
                {"product_id": derived_product_id, "quantity": 2},
                {"product_id": second_product_id, "quantity": 1},
            ],
            "delivery_address": {
                "delivery_address_id": user_address.id,
                "street": "1 Test St",
                "latitude": 41.3111,
                "longitude": 69.2797,
            },
            "payment_method": CASH_ORDER,
        }

        with pytest.raises(ValidationError) as exc:
            OrderService().create_order(sample_user.id, payload)

    assert second_product_name in str(exc.value)


def test_customer_serializer_publishes_no_stock_cap_for_derived_products(app, db, sample_product):
    """The bot and the web JS both read this field to decide the quantity ceiling."""
    from business_app.serializers.product_serializers import serialize_product

    product_id = sample_product.id
    make_derived(app, product_id, available_codes=0)
    with app.app_context():
        Product.query.get(product_id).stock_quantity = 0
        db.session.commit()
        payload = serialize_product(Product.query.get(product_id))

    assert payload["inventory"]["stock_quantity"] is None
    assert payload["inventory"]["is_in_stock"] is True


def test_customer_serializer_still_publishes_stock_for_non_derived_products(app, db, sample_product):
    from business_app.serializers.product_serializers import serialize_product

    product_id = sample_product.id
    with app.app_context():
        Product.query.get(product_id).stock_quantity = 4
        db.session.commit()
        payload = serialize_product(Product.query.get(product_id))

    assert payload["inventory"]["stock_quantity"] == 4


def test_cart_accepts_a_derived_product_with_an_empty_pool(app, db, sample_user, sample_product):
    """`add_item_to_cart` runs on the test's AMBIENT session (no enclosing
    `with app.app_context()`), so per the harness rule the stock mutation is
    made directly on `sample_product` -- the object already living in that
    same ambient session -- rather than through a separate nested context,
    which would leave the ambient session's identity map holding the stale
    pre-mutation `stock_quantity` (the "spurious 102 == 5" trap).
    """
    from business_app.services.cart_service import CartService

    product_id = sample_product.id
    make_derived(app, product_id, available_codes=0)
    sample_product.stock_quantity = 0
    db.session.commit()

    CartService().add_item_to_cart(sample_user.id, product_id, 3)  # must not raise


def test_cart_still_refuses_a_NON_derived_product_out_of_stock(app, db, sample_user, sample_product):
    """Scope boundary: cash genuinely consumes ordinary stock, so the cart still gates it.

    Mutated directly on `sample_product` (ambient session), not through a
    nested `with app.app_context()` -- see the sibling test's docstring.
    """
    from business_app.services.cart_service import CartService
    from business_app.utils.exceptions import ValidationError

    product_id = sample_product.id
    sample_product.stock_quantity = 0
    db.session.commit()

    with pytest.raises(ValidationError):
        CartService().add_item_to_cart(sample_user.id, product_id, 3)


def test_cart_endpoint_accepts_a_derived_product_with_an_empty_pool(
    app, db, sample_user, sample_product, auth_headers
):
    """Mutated directly on `sample_product` (ambient session): the test client
    reuses the ambient app context rather than pushing a fresh one, so a
    nested-context mutation would be invisible to the request -- same
    reasoning as `test_cash_order_at_empty_pool_succeeds_over_http` above.
    """
    product_id = sample_product.id
    make_derived(app, product_id, available_codes=0)
    sample_product.stock_quantity = 0
    db.session.commit()

    resp = app.test_client().post(
        "/api/v1/cart/items",
        json={"product_id": product_id, "quantity": 3},
        headers=auth_headers,
    )

    assert resp.status_code in (200, 201), resp.get_json()


# ---------------------------------------------------------------------------
# Fix round 1 (coordinator review, Task 7): findings 1-3.
# ---------------------------------------------------------------------------


def test_quick_reorder_reports_in_stock_for_a_derived_product_with_an_empty_pool(
    app, db, sample_user, sample_product, auth_headers
):
    """Finding 1: `get_quick_reorder_suggestions`'s own `in_stock`
    (cart_service.py:372) is a FOURTH spelling of "is this in stock" -- read by
    the bot's quick-order shortcut (telegram_bot/handlers/quick_order.py:346) to
    decide whether to offer "your usual" at all. It must not drop a derived
    product with an empty pool from that shortcut.

    Mutated directly on `sample_product` (ambient session), not through a
    nested `with app.app_context()` -- the test client reuses the ambient
    context, same reasoning as the cart HTTP tests above.
    """
    product_id = sample_product.id
    make_derived(app, product_id, available_codes=0)
    sample_product.stock_quantity = 0
    db.session.commit()

    order_id = _make_order_with_item(app, sample_user.id, product_id, sample_product.base_price, quantity=2)
    with app.app_context():
        order = db.session.get(Order, order_id)
        order.status = OrderStatus.DELIVERED
        order.payment_method = PaymentMethod.CASH
        db.session.commit()

    resp = app.test_client().get("/api/v1/orders/quick-reorder", headers=auth_headers)

    assert resp.status_code == 200, resp.get_json()
    suggestions = resp.get_json()["data"]["quick_reorder_suggestions"]
    matching = [s for s in suggestions if s["product_id"] == product_id]
    assert matching, "derived product missing from quick-reorder suggestions"
    assert matching[0]["in_stock"] is True


def test_quick_reorder_still_reports_out_of_stock_for_a_NON_derived_product(
    app, db, sample_user, sample_product, auth_headers
):
    """Scope boundary for Finding 1: an ordinary out-of-stock product must still
    be reported as out of stock by the quick-reorder shortcut.
    """
    product_id = sample_product.id
    sample_product.stock_quantity = 0
    db.session.commit()

    order_id = _make_order_with_item(app, sample_user.id, product_id, sample_product.base_price, quantity=2)
    with app.app_context():
        order = db.session.get(Order, order_id)
        order.status = OrderStatus.DELIVERED
        order.payment_method = PaymentMethod.CASH
        db.session.commit()

    resp = app.test_client().get("/api/v1/orders/quick-reorder", headers=auth_headers)

    assert resp.status_code == 200, resp.get_json()
    suggestions = resp.get_json()["data"]["quick_reorder_suggestions"]
    matching = [s for s in suggestions if s["product_id"] == product_id]
    assert matching, "product missing from quick-reorder suggestions"
    assert matching[0]["in_stock"] is False


def test_in_stock_only_listing_includes_a_derived_product_at_empty_pool(app, db, sample_product):
    """Finding 2a: `product_service.py:108`'s `in_stock_only` filter (Step 3b)
    must not hide a marking-code product from a cash shopper just because its
    stale `stock_quantity` projection reads 0 -- the point of this whole task
    is that a cash order can still be placed against it. An ordinary
    out-of-stock product must still be excluded from the same listing (scope
    boundary), asserted in the same request so a filter that widened to admit
    EVERYTHING could not pass.
    """
    derived_id = sample_product.id
    make_derived(app, derived_id, available_codes=0)
    sample_product.stock_quantity = 0
    db.session.commit()

    with app.app_context():
        ordinary = Product(
            name="Ordinary Out Of Stock 5L",
            description="Ordinary product with real, exhausted stock",
            category_id=sample_product.category_id,
            size="5L",
            volume=5.0,
            volume_unit="L",
            base_price=Decimal("8000.00"),
            stock_quantity=0,
            min_stock_level=5,
            max_stock_level=200,
            is_active=True,
            created_at=datetime.now(UTC),
        )
        db.session.add(ordinary)
        db.session.commit()
        ordinary_id = ordinary.id

    resp = app.test_client().get("/api/v1/products/?in_stock_only=true&per_page=100")

    assert resp.status_code == 200, resp.get_json()
    ids = {item["id"] for item in resp.get_json()["data"]["items"]}
    assert derived_id in ids, "derived product at empty pool must stay findable to a cash shopper"
    assert ordinary_id not in ids, "an ordinary out-of-stock product must still be excluded"


def test_to_dict_reports_stock_does_not_cap_purchase_for_derived_products(app, db, sample_product):
    """Finding 2b: pins the whole web surface hand-verified in Step 11 --
    `Product.to_dict()["stock_caps_purchase"]` is what `product_detail.html`
    reads to decide the add-to-cart button and quantity ceiling.
    """
    product_id = sample_product.id
    make_derived(app, product_id, available_codes=0)
    with app.app_context():
        payload = Product.query.get(product_id).to_dict(language="en")

    assert payload["stock_caps_purchase"] is False


def test_to_dict_still_reports_stock_caps_purchase_for_non_derived_products(app, db, sample_product):
    """Scope boundary for Finding 2b."""
    product_id = sample_product.id
    with app.app_context():
        payload = Product.query.get(product_id).to_dict(language="en")

    assert payload["stock_caps_purchase"] is True


def test_customer_serializer_reports_derived_products_never_low_stock(app, db, sample_product):
    """Finding 2c: `is_product_low_stock`'s derived branch
    (product_serializers.py:600-603) was unpinned -- the earlier serializer
    test only asserts `is_in_stock`. At stock_quantity=0 with
    min_stock_level=10 this would read `is_low_stock=True` if the branch were
    removed; the pool has its own low-stock signal, not this stale column.
    """
    from business_app.serializers.product_serializers import serialize_product

    product_id = sample_product.id
    make_derived(app, product_id, available_codes=0)
    with app.app_context():
        product = Product.query.get(product_id)
        product.stock_quantity = 0
        product.min_stock_level = 10
        db.session.commit()
        payload = serialize_product(Product.query.get(product_id))

    assert payload["inventory"]["is_low_stock"] is False


def test_public_products_feed_reports_in_stock_for_a_derived_product_at_empty_pool(app, db, sample_product):
    """Finding 3: `/api/public/products.json` (routes.py:2030) is one of the
    two places the product-detail page's schema.org availability contradiction
    was published -- it must not advertise a derived product at an empty pool
    as OutOfStock to crawlers/AI assistants while the page itself sells it.
    """
    product_id = sample_product.id
    make_derived(app, product_id, available_codes=0)
    sample_product.stock_quantity = 0
    db.session.commit()

    resp = app.test_client().get("/api/public/products.json")

    assert resp.status_code == 200
    matching = [p for p in resp.get_json()["products"] if p["id"] == product_id]
    assert matching, "derived product missing from the public feed"
    assert matching[0]["offers"]["availability"] == "https://schema.org/InStock"


# ---------------------------------------------------------------------------
# Final review: I2 (track_inventory=False dead-end) and M1 (the cart chokepoint)
# ---------------------------------------------------------------------------


def test_untracked_product_is_available_regardless_of_the_column(app, db, sample_product):
    """I2: `track_inventory=False` means "do not count this"; the gate must agree.

    `serialize_product`, `Product.stock_caps_purchase` and
    `CartService._check_product_quantity_availability` all honour the flag, so a
    page invites, the cart accepts and checkout preview passes -- and only
    `check_product_availability` disagreed, turning the divergence into a 400 at
    apply time.
    """
    from business_app.services.inventory_service import InventoryService

    product_id = sample_product.id
    sample_product.track_inventory = False
    sample_product.stock_quantity = 0
    db.session.commit()

    result = InventoryService().check_product_availability(product_id, 5)

    assert result.is_available is True, result.reason


def test_untracked_product_order_is_accepted_over_http(
    app, db, sample_user, sample_product, user_address, auth_headers
):
    """I2, the real path: the preview/apply divergence was only visible at POST."""
    product_id = sample_product.id
    sample_product.track_inventory = False
    sample_product.stock_quantity = 0
    sample_user.phone_verified_at = datetime.now(UTC)
    db.session.commit()

    resp = app.test_client().post(
        "/api/v1/orders/",
        json={
            "items": [{"product_id": product_id, "quantity": 2}],
            "delivery_address_id": user_address.id,
            "payment_method": CASH_ORDER,
        },
        headers=auth_headers,
    )

    assert resp.status_code == 201, resp.get_json()


def test_cart_summary_reports_in_stock_for_a_derived_product_at_empty_pool(
    app, db, sample_user, sample_product
):
    """M1: `get_cart_summary` bypassed the chokepoint and published in_stock=False.

    Task 7's comment claims `_check_product_quantity_availability` relieves every
    cart surface; this is the surface that made the claim false.
    """
    from business_app.services.cart_service import CartService

    product_id = sample_product.id
    make_derived(app, product_id, available_codes=0)
    sample_product.stock_quantity = 0
    db.session.commit()

    service = CartService()
    service.add_item_to_cart(sample_user.id, product_id, 3)
    summary = service.get_cart_summary(sample_user.id)

    line = next(item for item in summary["items"] if item["product_id"] == product_id)
    assert line["in_stock"] is True


def test_cart_summary_still_reports_out_of_stock_for_a_NON_derived_product(
    app, db, sample_user, sample_product
):
    """Scope boundary: an ordinary product out of stock still reads in_stock=False."""
    from business_app.services.cart_service import CartService

    product_id = sample_product.id
    service = CartService()
    service.add_item_to_cart(sample_user.id, product_id, 3)

    sample_product.stock_quantity = 0
    db.session.commit()

    summary = service.get_cart_summary(sample_user.id)

    line = next(item for item in summary["items"] if item["product_id"] == product_id)
    assert line["in_stock"] is False
