"""The cart estimate is the ONE place a checkout total is quoted.

Two uncalled routes reach `CartService.calculate_cart_estimate`
(`business_app/api/orders.py:619` and `business_app/api/carts.py:106`). Both are
made to carry the tier quote here: leaving one behind leaves a second, divergent
quote surface live, and this project has already shipped one screen showing a
figure another screen charged.

FIXTURE DISCIPLINE. Production tier percentages differ from dev's. Nothing in
this file may reference 2 / 3 / 6 / 1500 / 5000 / 12000 — the tiers below are
seeded by the test and asserted against themselves.
"""

from decimal import Decimal

import pytest
from flask_jwt_extended import create_access_token

from business_app.models.loyalty import (
    LoyaltyPoints,
    LoyaltyProgram,
    LoyaltyReward,
    LoyaltyTierConfig,
    LoyaltyTransaction,
)
from business_app.utils.constants import LoyaltyTransactionType

QUANTITY = 2
TIER_RATE = 4.0          # invented for this file only
TIER_NAME = "Nimbus"
TIER_MIN_POINTS = 700


@pytest.fixture
def program(db):
    prog = LoyaltyProgram(
        name="Estimate quote program", description="d",
        is_active=True, is_default=True, uzs_per_point=250,
    )
    db.session.add(prog)
    db.session.commit()
    return prog


@pytest.fixture
def tiers(db, program):
    base = LoyaltyTierConfig(
        program_id=program.id, name="Ground", display_order=0,
        min_points=0, discount_percentage=0.0, is_active=True,
    )
    top = LoyaltyTierConfig(
        program_id=program.id, name=TIER_NAME, display_order=1,
        min_points=TIER_MIN_POINTS, discount_percentage=TIER_RATE, is_active=True,
    )
    db.session.add_all([base, top])
    db.session.commit()
    return {"base": base, "top": top}


@pytest.fixture
def reward(db, program):
    """A real, redeemable discount reward — used only to prove the guard
    fires on ANY promo_code + reward_id pairing, not to actually redeem it."""
    r = LoyaltyReward(
        program_id=program.id,
        name="Estimate quote test reward",
        reward_type="discount",
        points_cost=100,
        discount_type="fixed",
        discount_value=Decimal("1000.00"),
        is_active=True,
    )
    db.session.add(r)
    db.session.commit()
    return r


@pytest.fixture
def tiered_member(db, sample_user, program, tiers):
    """`sample_user`, sitting in the discounting tier by REAL qualifying points."""
    db.session.add(LoyaltyPoints(
        user_id=sample_user.id, program_id=program.id,
        total_earned=TIER_MIN_POINTS, current_balance=TIER_MIN_POINTS,
        current_tier=TIER_NAME, points_to_next_tier=0,
    ))
    db.session.add(LoyaltyTransaction(
        user_id=sample_user.id,
        transaction_type=LoyaltyTransactionType.EARNED,
        points=TIER_MIN_POINTS,
        description="qualifying",
        remaining_points=TIER_MIN_POINTS,
        is_expired=False,
    ))
    db.session.commit()
    return sample_user


def _headers(app, user):
    with app.app_context():
        return {"Authorization": f"Bearer {create_access_token(identity=str(user.id))}"}


def _payload(product, payment_method, address=None):
    """The payload the clients actually send."""
    body = {
        "items": [{"product_id": product.id, "quantity": QUANTITY}],
        "payment_method": payment_method,
    }
    if address is not None:
        body["delivery_address_id"] = address.id
    return body


def _orders_route(client, app, user, product, payment_method, address=None):
    resp = client.post(
        "/api/v1/orders/cart/estimate",
        headers=_headers(app, user),
        json=_payload(product, payment_method, address),
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return resp.get_json()["data"]["pricing"]


def _cart_route(client, app, user, product, payment_method, address=None):
    resp = client.post(
        "/api/v1/cart/estimate",
        headers=_headers(app, user),
        json=dict(_payload(product, payment_method, address), cart_items=[
            {"product_id": product.id, "quantity": QUANTITY}
        ]),
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return resp.get_json()["data"]["estimate"]["pricing"]


@pytest.mark.integration
def test_a_cash_quote_carries_the_tier_discount_and_a_total_net_of_it(
    app, db, client, tiered_member, sample_product
):
    pricing = _orders_route(client, app, tiered_member, sample_product, "cash")

    subtotal = Decimal(str(pricing["items_subtotal"]))
    expected = (subtotal * Decimal(str(TIER_RATE)) / Decimal("100")).quantize(Decimal("0.01"))

    assert pricing["tier_name"] == TIER_NAME
    assert Decimal(str(pricing["tier_discount_percentage"])) == Decimal(str(TIER_RATE))
    assert Decimal(str(pricing["tier_discount"])) == expected
    assert Decimal(str(pricing["final_total"])) == (
        subtotal + Decimal(str(pricing["delivery_fee"])) - expected
    )
    assert Decimal(str(pricing["cod_savings"])) == Decimal("0"), (
        "the customer is already on the COD rail; there is nothing to switch to"
    )


@pytest.mark.integration
@pytest.mark.parametrize("rail", ["card", "click", "business_account"])
def test_a_fiscalized_quote_carries_no_discount_but_names_what_cash_would_save(
    app, db, client, tiered_member, sample_product, rail
):
    """`cod_savings` is what drives the bot's motivator line without a second
    round trip. It must be the amount the CASH rail would have granted."""
    pricing = _orders_route(client, app, tiered_member, sample_product, rail)
    cash = _orders_route(client, app, tiered_member, sample_product, "cash")

    assert Decimal(str(pricing["tier_discount"])) == Decimal("0")
    assert Decimal(str(pricing["final_total"])) == (
        Decimal(str(pricing["items_subtotal"])) + Decimal(str(pricing["delivery_fee"]))
    )
    assert Decimal(str(pricing["cod_savings"])) == Decimal(str(cash["tier_discount"]))
    assert pricing["tier_name"] == TIER_NAME, (
        "the tier still has to be nameable so the motivator can be written"
    )


@pytest.mark.integration
def test_a_member_of_a_zero_rate_tier_gets_no_discount_and_no_savings_claim(
    app, db, client, sample_user, program, tiers, sample_product
):
    """No LoyaltyPoints row, no qualifying points: the base tier's rate is 0, so
    every figure must be silent rather than zero-but-present."""
    pricing = _orders_route(client, app, sample_user, sample_product, "cash")

    assert Decimal(str(pricing["tier_discount"])) == Decimal("0")
    assert Decimal(str(pricing["cod_savings"])) == Decimal("0")
    assert Decimal(str(pricing["final_total"])) == (
        Decimal(str(pricing["items_subtotal"])) + Decimal(str(pricing["delivery_fee"]))
    )


@pytest.mark.integration
def test_an_ineligible_entity_gets_no_discount_and_no_savings_claim(
    app, db, client, sample_user, program, tiers, sample_product
):
    """CHANGE 3 (loyalty-tier-cod-discount UI pin): the OTHER non-eligible
    shape — a real, qualifying-points member of the discounting tier, but
    excluded at gate 2 of `LoyaltyService.quote_tier_discount` because they
    are an entity user with no active loyalty-eligible corporate contract.
    `test_tier_discount_order_creation.py::test_ineligible_entity_gets_nothing_even_on_cash`
    already proves this at order CREATION; every UI surface that reads a
    pre-purchase quote (the bot's payment picker/confirmation, the web
    checkout page) reads THIS estimate endpoint instead, so the same zeroing
    has to be proven here too — otherwise those surfaces' "renders nothing
    at zero" pins would be vacuous for this shape."""
    from datetime import UTC, datetime, timedelta
    from uuid import uuid4

    from business_app.models.corporate import CorporateContract, CorporateContractStatus
    from shared.enums import EntitySubtype, UserType

    db.session.add(LoyaltyPoints(
        user_id=sample_user.id, program_id=program.id,
        total_earned=TIER_MIN_POINTS, current_balance=TIER_MIN_POINTS,
        current_tier=TIER_NAME, points_to_next_tier=0,
    ))
    db.session.add(LoyaltyTransaction(
        user_id=sample_user.id,
        transaction_type=LoyaltyTransactionType.EARNED,
        points=TIER_MIN_POINTS,
        description="qualifying",
        remaining_points=TIER_MIN_POINTS,
        is_expired=False,
    ))
    sample_user.user_type = UserType.ENTITY
    sample_user.entity_subtype = EntitySubtype.WORKPLACE
    db.session.add(CorporateContract(
        user_id=sample_user.id,
        contract_number=f"C-{uuid4().hex[:10]}",
        name="No loyalty",
        status=CorporateContractStatus.ACTIVE,
        start_date=datetime.now(UTC) - timedelta(days=1),
        currency="UZS",
        is_active=True,
        is_loyalty_points_eligible=False,
    ))
    db.session.commit()

    pricing = _orders_route(client, app, sample_user, sample_product, "cash")

    assert Decimal(str(pricing["tier_discount"])) == Decimal("0")
    assert Decimal(str(pricing["tier_discount_percentage"])) == Decimal("0")
    assert Decimal(str(pricing["cod_savings"])) == Decimal("0")
    assert Decimal(str(pricing["final_total"])) == (
        Decimal(str(pricing["items_subtotal"])) + Decimal(str(pricing["delivery_fee"]))
    )


@pytest.mark.integration
def test_both_uncalled_routes_quote_the_same_money(
    app, db, client, tiered_member, sample_product
):
    """🔴 Whichever route a client adopts, the other must not drift. A second
    quote surface answering differently is the defect this task exists to
    prevent, and it is invisible until a client switches routes."""
    money = ("items_subtotal", "delivery_fee", "discount_amount", "loyalty_discount",
             "tier_discount", "tier_discount_percentage", "cod_savings", "final_total")

    for rail in ("cash", "card"):
        a = _orders_route(client, app, tiered_member, sample_product, rail)
        b = _cart_route(client, app, tiered_member, sample_product, rail)
        assert [Decimal(str(a[k])) for k in money] == [Decimal(str(b[k])) for k in money], (
            f"the two estimate routes disagree on rail {rail!r}: {a} vs {b}"
        )
        assert a["tier_name"] == b["tier_name"]


@pytest.mark.integration
def test_the_quote_equals_the_charge(app, db, client, tiered_member, sample_product, user_address):
    """The entire point of this task: whatever the estimate says a COD order
    costs must be what `create_order` actually persists for the SAME cart,
    address and tier state. Not 'close' — byte-for-byte equal Decimal money.
    """
    from tests.integration.tier_discount_factory import post_order, verify_phone

    verify_phone(db, tiered_member)  # POST /api/v1/orders/ requires it

    quoted = _orders_route(client, app, tiered_member, sample_product, "cash", user_address)

    resp = post_order(
        app,
        _headers(app, tiered_member),
        product_id=sample_product.id,
        address_id=user_address.id,
        payment_method="cash",
        quantity=QUANTITY,
    )
    assert resp.status_code == 201, resp.get_json()

    from business_app import db as _db
    from business_app.models.order import Order

    _db.session.expire_all()
    order = Order.query.get(resp.get_json()["data"]["order"]["id"])

    assert Decimal(str(order.tier_discount)) == Decimal(str(quoted["tier_discount"])) > Decimal("0"), (
        "the feature must actually be doing something for this check to mean anything"
    )
    assert Decimal(str(order.total_amount)) == Decimal(str(quoted["final_total"]))
    assert Decimal(str(order.subtotal)) == Decimal(str(quoted["items_subtotal"]))
    assert Decimal(str(order.delivery_fee)) == Decimal(str(quoted["delivery_fee"]))


# A promo code no real campaign will ever match. Deliberate: `_apply_promo_code`
# swallows an unknown code into `ValidationError("Invalid promotional code")`
# *before* ever reaching `validate_promo_code`'s campaign-validity date
# comparison, so these tests exercise the real reward/promo-code-presence
# guard and the real API-to-service wiring without depending on a seeded
# `PromotionalCampaign` row or its downstream discount arithmetic — neither
# of which F3 touches.
_UNMATCHED_PROMO_CODE = "F3-NO-SUCH-CAMPAIGN"


@pytest.mark.integration
def test_promo_code_sent_to_the_quote_surface_never_reaches_the_pricing_call(
    app, db, client, tiered_member, sample_product
):
    """F3 (2026-08-27): `promo_code` was a reachable input on both estimate
    routes (`@jwt_required` only -- any authenticated customer may set it),
    threaded straight into `CartService.calculate_cart_estimate`'s
    `discount_amount` slot. But `OrderService.create_order` never applies a
    promo code to a created order, so a customer could quote a
    promo-discounted total here and then be charged MORE at
    `POST /api/v1/orders/` for the identical cart -- quoted lower than
    charged. The fix removes the field from `CartEstimateRequest` and from
    both API call sites entirely, so `promo_code` sent in the request body
    must never reach the service call at all -- not "reach it and be priced
    at zero", but never arrive as a kwarg in the first place.
    """
    from unittest.mock import patch

    from business_app.services.cart_service import CartService

    payload = {
        "items": [{"product_id": sample_product.id, "quantity": QUANTITY}],
        "payment_method": "cash",
        "promo_code": _UNMATCHED_PROMO_CODE,
    }
    headers = _headers(app, tiered_member)

    with patch.object(
        CartService, "calculate_cart_estimate", autospec=True, side_effect=CartService.calculate_cart_estimate,
    ) as spy:
        resp = client.post("/api/v1/orders/cart/estimate", headers=headers, json=payload)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    spy.assert_called_once()
    assert "promo_code" not in spy.call_args.kwargs, (
        f"promo_code reached CartService.calculate_cart_estimate via /api/v1/orders/cart/estimate: "
        f"{spy.call_args.kwargs}"
    )

    cart_payload = dict(payload, cart_items=payload["items"])
    with patch.object(
        CartService, "calculate_cart_estimate", autospec=True, side_effect=CartService.calculate_cart_estimate,
    ) as spy2:
        resp2 = client.post("/api/v1/cart/estimate", headers=headers, json=cart_payload)
    assert resp2.status_code == 200, resp2.get_data(as_text=True)
    spy2.assert_called_once()
    assert "promo_code" not in spy2.call_args.kwargs, (
        f"promo_code reached CartService.calculate_cart_estimate via /api/v1/cart/estimate: "
        f"{spy2.call_args.kwargs}"
    )

    # Sanity: even an unmatched code priced no discount either way.
    assert Decimal(str(resp.get_json()["data"]["pricing"]["discount_amount"])) == Decimal("0")


@pytest.mark.integration
def test_a_promo_and_reward_together_no_longer_reaches_the_mutual_exclusion_guard(
    app, db, client, tiered_member, sample_product, reward
):
    """`OrderService.create_order` still refuses a promo code stacked with a
    redeemed reward on the same order (order_service.py:118-119) -- that
    guard is unchanged. `CartService.calculate_cart_estimate`'s mirror of it
    (cart_service.py:188-189, `if reward_id and promo_code`) is deliberately
    LEFT IN PLACE by the F3 fix even though `promo_code` can no longer reach
    it through either HTTP route: a promo code sent alongside a reward_id is
    now just ignored, like a promo code sent alone -- the guard fires on mere
    PRESENCE of both, not on the code resolving to a real campaign, so an
    unmatched code is enough to prove it. This is the fix working as
    intended, not a regression -- pinned here so nobody "fixes" the
    now-unreachable guard back into a 400.
    """
    resp = client.post(
        "/api/v1/orders/cart/estimate",
        headers=_headers(app, tiered_member),
        json={
            "items": [{"product_id": sample_product.id, "quantity": QUANTITY}],
            "payment_method": "cash",
            "promo_code": _UNMATCHED_PROMO_CODE,
            "reward_id": reward.id,
        },
    )

    assert resp.status_code == 200, resp.get_data(as_text=True)
    pricing = resp.get_json()["data"]["pricing"]
    assert Decimal(str(pricing["discount_amount"])) == Decimal("0")
