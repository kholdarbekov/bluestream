"""/my-loyalty must show the tier discount's payment condition.

Since the 2026-11-14 redesign the levels are rendered by the server (templates/frontend/loyalty.html, from
get_loyalty_handbook_context) instead of loyalty.js; the rendered behaviour is driven end to end in
tests/integration/test_web_account_loyalty.py. These source pins keep the properties that silently re-open the
"unconditional discount" promise:

1. The condition is the BACKEND'S SENTENCE (`api.loyalty.tier_discount_condition`, the one GET /loyalty/tiers also
   publishes), so the page holds no second copy that can drift from the rule or the language.
2. It is rendered inside the same block as the percentages it qualifies.
3. Neither loyalty.js nor the page-data island restates it.
4. Without a discounted level there is no condition, no marker and no empty wrapper.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "business_app" / "templates" / "frontend" / "loyalty.html"
LOYALTY_JS = ROOT / "business_app" / "static" / "js" / "pages" / "loyalty.js"


def _template() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


def _levels_block() -> str:
    src = _template()
    return src.split('id="membershipTiersContainer"', 1)[1].split("</section>", 1)[0]


def test_the_page_files_exist():
    assert TEMPLATE.is_file(), f"{TEMPLATE} moved — update this guard rather than deleting it"
    assert LOYALTY_JS.is_file(), f"{LOYALTY_JS} moved — update this guard rather than deleting it"


def test_condition_is_the_backend_sentence():
    assert "'api.loyalty.tier_discount_condition'|t" in _levels_block()


def test_condition_is_rendered_inside_the_levels_block_with_the_percentage():
    block = _levels_block()

    assert "tier.discount_percentage" in block
    assert block.index("tier.discount_percentage") < block.index("api.loyalty.tier_discount_condition")


def test_the_sentence_is_not_re_stated_in_the_script_or_the_page_island():
    """One expression only. A copy in loyalty.js or the render_page_data island would be a second, silently divergent
    statement of the same rule."""
    script = LOYALTY_JS.read_text(encoding="utf-8")
    island = _template().split("render_page_data(", 1)[1]

    assert "tier_discount_condition" not in script
    assert "tier_discount_condition" not in island
    for text in (script.lower(), island.lower()):
        assert "cash on delivery" not in text and "cash-on-delivery" not in text


def test_discount_block_is_omitted_entirely_at_zero():
    """A level without a discount shows no percentage and no `*` marker; a program without any discounted level shows
    no condition sentence (and no footnote wrapper)."""
    block = _levels_block()

    assert "{% if tier.discount_percentage > 0 %}" in block
    assert "handbook.tiers|selectattr('discount_percentage', 'gt', 0)|list" in _template()
    footnote = block.split("{% if has_discount %}", 1)[1].split("{% endif %}", 1)[0]
    assert "api.loyalty.tier_discount_condition" in footnote and 'id="acl-discount-note"' in footnote
