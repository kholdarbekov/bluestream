"""🔴 The quoted total and the charged total are ONE decision.

`checkout.js` now renders `POST /api/v1/orders/cart/estimate` and then posts
`POST /api/v1/orders/`. Those are two code paths — `CartService` and
`OrderService` — over the same basket, and the tier discount is CLAMPED in both
(design §4.4). Two clamps mean two expressions, and this file is what keeps them
from drifting: it never re-implements either, it compares one against the other
over HTTP with the payloads the page actually sends.

The basket deliberately carries NO PriceRule. `CartService._get_best_price_rule`
applies volume rules that `OrderService._process_order_items` does not (design
§10, out of scope) — a rule here would make this file fail for a reason that has
nothing to do with the tier discount.
"""

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from flask_jwt_extended import create_access_token

from business_app.models.loyalty import (
    LoyaltyPoints,
    LoyaltyProgram,
    LoyaltyTierConfig,
    LoyaltyTransaction,
)
from business_app.models.order import Order
from business_app.utils.constants import LoyaltyTransactionType

QUANTITY = 2
TIER_NAME = "Cirrus"
TIER_MIN_POINTS = 400


@pytest.fixture
def tiered_member(db, sample_user, request):
    """`sample_user` in a tier whose rate this test invented.

    Parametrised so the SAME comparison runs at a rate that discounts and at a
    rate high enough to hit the clamp.
    """
    rate = getattr(request, "param", 5.0)
    program = LoyaltyProgram(
        name="Web parity program", description="d",
        is_active=True, is_default=True, uzs_per_point=250,
    )
    db.session.add(program)
    db.session.flush()
    db.session.add_all([
        LoyaltyTierConfig(program_id=program.id, name="Base", display_order=0,
                          min_points=0, discount_percentage=0.0, is_active=True),
        LoyaltyTierConfig(program_id=program.id, name=TIER_NAME, display_order=1,
                          min_points=TIER_MIN_POINTS, discount_percentage=rate,
                          is_active=True),
    ])
    db.session.add(LoyaltyPoints(
        user_id=sample_user.id, program_id=program.id,
        total_earned=TIER_MIN_POINTS, current_balance=TIER_MIN_POINTS,
        current_tier=TIER_NAME, points_to_next_tier=0,
    ))
    db.session.add(LoyaltyTransaction(
        user_id=sample_user.id,
        transaction_type=LoyaltyTransactionType.EARNED,
        points=TIER_MIN_POINTS, description="qualifying",
        remaining_points=TIER_MIN_POINTS, is_expired=False,
    ))
    sample_user.phone_verified_at = datetime.now(timezone.utc)
    db.session.commit()
    return sample_user


def _headers(app, user):
    with app.app_context():
        return {"Authorization": f"Bearer {create_access_token(identity=str(user.id))}"}


@pytest.mark.integration
@pytest.mark.parametrize("tiered_member", [5.0, 140.0], indirect=True)
def test_the_quote_the_page_showed_is_the_total_the_order_charges(
    app, db, client, tiered_member, sample_product, user_address, monkeypatch
):
    """One rate that discounts, one absurd rate that must clamp. In both cases
    the two independent code paths have to land on the same number.

    DEFAULT_DELIVERY_FEE is 0 in this environment (dev/test both run delivery
    free). At the 140% rate the clamp binds at exactly `headroom == subtotal`
    (design's documented behaviour — clamp_tier_discount only guarantees
    non-negative, never positive), and with a $0 delivery fee that lands the
    total on EXACTLY zero. `create_order` then refuses a non-positive total —
    by design, already pinned by
    tests/integration/test_tier_discount_stacking.py::
    test_rates_past_100_percent_are_refused_rather_than_written_negative
    ("With a free delivery fee the clamped total lands on zero, and
    create_order refuses a non-positive total"). That refusal is correct and
    must not be touched. This pin's job is different — comparing the quote
    against the charge when both AGREE an order is placeable — so it borrows
    the sibling file's own idiom (`test_clamp_holds_the_discount_to_the_
    headroom_the_others_left`) for exercising a bound clamp without hitting
    that already-covered refusal boundary: give delivery a nonzero fee so the
    clamped total lands on the delivery fee instead of on zero. Both
    CartService and OrderService resolve delivery_fee through the same live
    DeliveryService/app.config, so the override applies identically to the
    quote and the charge — nothing about what the two clamps compute changes.
    """
    monkeypatch.setitem(app.config, "DEFAULT_DELIVERY_FEE", 3000)
    headers = _headers(app, tiered_member)

    quote = client.post(
        "/api/v1/orders/cart/estimate",
        headers=headers,
        json={
            "items": [{"product_id": sample_product.id, "quantity": QUANTITY}],
            "delivery_address_id": user_address.id,
            "delivery_date": None,
            "payment_method": "cash",
        },
    )
    assert quote.status_code == 200, quote.get_data(as_text=True)
    pricing = quote.get_json()["data"]["pricing"]

    placed = client.post(
        "/api/v1/orders/",
        headers=headers,
        json={
            "items": [{"product_id": sample_product.id, "quantity": QUANTITY}],
            "delivery_address_id": user_address.id,
            "payment_method": "cash",
            "source": "web",
        },
    )
    assert placed.status_code in (200, 201), placed.get_data(as_text=True)

    with app.app_context():
        order = Order.query.get(placed.get_json()["data"]["order"]["id"])
        assert Decimal(str(pricing["tier_discount"])) == Decimal(str(order.tier_discount)), (
            "the discount the page showed is not the discount the order carries"
        )
        assert Decimal(str(pricing["final_total"])) == Decimal(str(order.total_amount)), (
            f"quoted {pricing['final_total']} vs charged {order.total_amount} — "
            "the two clamps have drifted apart"
        )
        assert Decimal(str(order.total_amount)) > 0, (
            "the clamp must keep the total positive at any configured rate"
        )


@pytest.mark.integration
def test_a_web_order_on_a_fiscalized_rail_is_quoted_and_charged_full_price(
    app, db, client, tiered_member, sample_product, user_address
):
    """`build_click_fiscalization_payload` asserts
    `received_card == to_tiyin(order.total_amount)` while filling per-item
    Discount from `loyalty_discount` alone. A tier discount on this rail is a
    tax-committee reconciliation failure, not a display bug."""
    headers = _headers(app, tiered_member)

    quote = client.post(
        "/api/v1/orders/cart/estimate",
        headers=headers,
        json={
            "items": [{"product_id": sample_product.id, "quantity": QUANTITY}],
            "delivery_address_id": user_address.id,
            "payment_method": "click",
        },
    )
    assert quote.status_code == 200, quote.get_data(as_text=True)
    pricing = quote.get_json()["data"]["pricing"]
    assert Decimal(str(pricing["tier_discount"])) == Decimal("0")
    assert Decimal(str(pricing["cod_savings"])) > 0

    placed = client.post(
        "/api/v1/orders/",
        headers=headers,
        json={
            "items": [{"product_id": sample_product.id, "quantity": QUANTITY}],
            "delivery_address_id": user_address.id,
            "payment_method": "click",
            "source": "web",
        },
    )
    assert placed.status_code in (200, 201), placed.get_data(as_text=True)

    with app.app_context():
        order = Order.query.get(placed.get_json()["data"]["order"]["id"])
        assert Decimal(str(order.tier_discount)) == Decimal("0")
        assert Decimal(str(pricing["final_total"])) == Decimal(str(order.total_amount))
