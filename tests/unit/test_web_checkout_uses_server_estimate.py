"""The web checkout must not price anything.

`renderOrderSummary` used to multiply `pricing.current_price` by a quantity in
client-side floats and add up the basket. Two independent bugs live in that:

* `POST /products/bulk` only personalises prices when an `Authorization` HEADER
  is present (`business_app/api/products.py:355`) and the web app authenticates
  by COOKIE, so contract customers were shown a non-personalised subtotal;
* a sixth term (the tier discount) exists only on the server, so any client-side
  sum is now wrong by construction for every tiered customer.

The page renders `POST /api/v1/orders/cart/estimate` instead — the same
`CartService` the order is built from. Source-level, because
`business_app/static/js/` is served as-is and only `admin_ui/` has a JS runner.
"""

import re
from pathlib import Path

CHECKOUT_JS = Path(__file__).resolve().parents[2] / "business_app" / "static" / "js" / "pages" / "checkout.js"
CHECKOUT_HTML = Path(__file__).resolve().parents[2] / "business_app" / "templates" / "frontend" / "checkout.html"


def _code_lines(src: str) -> str:
    """Comments name the trap, so they would defeat every assertion below."""
    return "\n".join(re.sub(r"//.*$", "", line) for line in src.splitlines())


def test_checkout_js_asks_the_server_for_the_quote():
    code = _code_lines(CHECKOUT_JS.read_text(encoding="utf-8"))
    assert "'/orders/cart/estimate'" in code, (
        "checkout.js no longer calls the one quote surface; whatever it renders "
        "instead is a second expression of the money"
    )
    assert "payment_method: selectedPaymentMethod" in code, (
        "the quote must carry the RAIL — the tier discount attaches to COD only"
    )


def test_checkout_js_never_multiplies_a_price_by_a_quantity():
    code = _code_lines(CHECKOUT_JS.read_text(encoding="utf-8"))
    for forbidden in ("price * cartItem.quantity", "subtotal += itemTotal"):
        assert forbidden not in code, (
            f"checkout.js sums the basket client-side again ({forbidden!r}); "
            "POST /products/bulk is contract-blind over cookie auth and knows "
            "nothing about the tier discount"
        )


def test_checkout_js_does_not_keep_a_second_delivery_fee_surface():
    code = _code_lines(CHECKOUT_JS.read_text(encoding="utf-8"))
    assert "/delivery/calculate-fee" not in code, (
        "the estimate already computes the delivery fee through "
        "CartService._calculate_delivery_fee; a second fee call can disagree"
    )


def test_the_summary_has_a_row_for_every_discount_the_quote_can_carry():
    html = CHECKOUT_HTML.read_text(encoding="utf-8")
    for element_id in ("summary-discount", "summary-reward", "summary-tier",
                       "summary-cod-savings"):
        assert f'id="{element_id}"' in html, (
            f"the quote can carry {element_id} and the page has nowhere to show "
            "it — the customer would see a total that does not add up"
        )


def test_render_discount_row_hides_the_row_and_writes_no_text_at_zero():
    """CHANGE 3 pin (surface: web checkout row, `renderDiscountRow`, which
    backs summary-tier among others). A customer whose tier carries no
    discount — a 0% tier or a loyalty-ineligible entity — reaches this page
    with `pricing.tier_discount` already zeroed server-side (both shapes are
    proven to zero it at the estimate endpoint in
    tests/integration/test_cart_estimate_quote_surface.py; this pure client
    function can only ever see the number, never why it is zero).

    Source-level, per this file's own convention (see module docstring):
    `business_app/static/js` has no JS runtime in the backend test
    environment.
    """
    code = _code_lines(CHECKOUT_JS.read_text(encoding="utf-8"))

    assert "row.style.display = value > 0 ? '' : 'none';" in code, (
        "renderDiscountRow no longer hides the row at zero — a 0%-tier or "
        "ineligible customer would see a dangling row"
    )
    assert "if (value > 0) {" in code, (
        "renderDiscountRow writes the label/amount unconditionally — even "
        "with the row hidden, stale text would leak through any future "
        "style change"
    )
