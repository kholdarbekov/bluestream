"""/my-loyalty must show the tier discount's payment condition.

Source-level, because business_app/static/js has no JS runtime in the backend
test environment — that file is served as-is, and only admin_ui/ has a test
runner. The two properties pinned here are the ones that silently re-open the
"unconditional discount" promise:

1. The condition is READ FROM THE API RESPONSE, so the browser holds no second
   copy of the sentence that can drift from the backend rule or the language.
2. The condition is rendered inside the same block as the percentage — a
   percentage that ships without it is the bug this feature exists to close.
"""

from pathlib import Path

LOYALTY_JS = (
    Path(__file__).resolve().parents[2] / "business_app" / "static" / "js" / "pages" / "loyalty.js"
)


def _source() -> str:
    return LOYALTY_JS.read_text(encoding="utf-8")


def test_loyalty_js_exists():
    assert LOYALTY_JS.is_file(), f"{LOYALTY_JS} moved — update this guard rather than deleting it"


def test_condition_is_taken_from_the_tiers_response():
    src = _source()

    assert "renderMembershipTiers(result.data.tiers, result.data.tier_discount_condition)" in src
    assert "function renderMembershipTiers(tiers, discountCondition)" in src


def test_condition_is_rendered_inside_the_discount_block():
    src = _source()
    block = src.split("var discountBlock", 1)[1].split(": '';", 1)[0]

    assert "escapeHtml(discountCondition)" in block
    assert "tier.discount_percentage" in block


def test_the_sentence_is_not_re_stated_in_the_page_island():
    """One expression only. A copy in the render_page_data island would be a
    second, silently divergent statement of the same rule."""
    src = _source()

    assert "PAGE_DATA.i18n.cod" not in src
    assert "cash on delivery" not in src.lower()
    assert "cash-on-delivery" not in src.lower()


def test_discount_block_is_omitted_entirely_at_zero():
    """CHANGE 3 pin (surface: /my-loyalty, ~loyalty.js:73). A customer whose
    tier carries no discount — a 0% tier or a loyalty-ineligible entity, both
    of which the API already reports as `discount_percentage: 0` (this
    client function can only ever see the number, never why it is zero) —
    must see no tag icon, no percentage, no payment-condition sentence and no
    dangling wrapper `<div>`: the false branch of the ternary is the bare
    empty string, not a div with nothing in it."""
    src = _source()

    assert "var discountBlock = tier.discount_percentage > 0" in src, (
        "the discount block is no longer gated on a positive rate"
    )
    # Same slice this file's `test_condition_is_rendered_inside_the_discount_block`
    # uses to isolate the ternary's TRUE branch (everything up to the ": '';"
    # that starts its FALSE branch) — reused here to pin that the false arm
    # really is bare '', not a div with empty content.
    true_branch = src.split("var discountBlock", 1)[1].split(": '';", 1)[0]
    assert true_branch.rstrip().endswith("'</div>'"), (
        f"expected the true branch to end right before a bare '' false arm: {true_branch!r}"
    )
