"""Static regressions for backend translation seed coverage."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SEED_SCRIPT = ROOT / "scripts" / "seed_backend_translations.py"


def test_seed_script_includes_loyalty_admin_navigation_and_page_keys():
    text = SEED_SCRIPT.read_text(encoding="utf-8")

    assert "'ui.nav.loyalty_members': {" in text
    assert "'ui.nav.loyalty_programs': {" in text
    assert "'ui.nav.loyalty_rewards': {" in text
    assert "'ui.loyalty.export_members': {" in text
    assert "'ui.loyalty.reward_details': {" in text
    assert "'ui.loyalty.search_rewards': {" in text
    assert "'ui.loyalty.tier_create_success': {" in text


def test_seed_script_includes_loyalty_analytics_keys():
    text = SEED_SCRIPT.read_text(encoding="utf-8")

    assert "'ui.analytics.loyalty': {" in text
    assert "'ui.analytics.loyalty_points_trend': {" in text
    assert "'ui.analytics.total_loyalty_members': {" in text
    assert "'ui.analytics.points_in_circulation': {" in text
    assert "'ui.analytics.top_rewards': {" in text


def test_seed_script_includes_orders_and_products_fiscalization_ui_catalogs():
    text = SEED_SCRIPT.read_text(encoding="utf-8")

    assert "ADMIN_UI_ORDER_TRANSLATIONS = {" in text
    assert "ADMIN_UI_PRODUCT_TRANSLATIONS = {" in text
    assert "'ui.orders.fiscalization': _ui_tr(" in text
    assert "'ui.orders.record_personal_card_payment': _ui_tr(" in text
    assert "'ui.orders.retry_fiscalization': _ui_tr(" in text
    assert "'ui.products.fiscal_profile': _ui_tr(" in text
    assert "'ui.products.marking_codes': _ui_tr(" in text
    assert "'ui.products.marking_code_import_issues': _ui_tr(" in text
    assert "'ui.products.marking_code_status_available': _ui_tr(" in text
    assert "'ui.products.marking_code_status_reserved': _ui_tr(" in text
    assert "'ui.products.marking_code_status_used': _ui_tr(" in text
    assert "'ui.products.marking_code_status_archived': _ui_tr(" in text


def test_seed_script_includes_marking_code_utilisation_filter_keys():
    text = SEED_SCRIPT.read_text(encoding="utf-8")

    assert "'ui.products.marking_code_status_available_unutilised': _ui_tr(" in text
    assert "'ui.products.marking_code_status_available_pre_utilised': _ui_tr(" in text


def test_seed_script_includes_order_money_breakdown_keys():
    """Orders.js passes an English fallback, so a missing row degrades silently
    to English for a ru/uz operator instead of failing loudly. Pin the rows."""
    text = SEED_SCRIPT.read_text(encoding="utf-8")

    assert "'ui.orders.money_breakdown': _ui_tr(" in text
    assert "'ui.orders.subtotal': _ui_tr(" in text
    assert "'ui.orders.subscription_discount': _ui_tr(" in text
    assert "'ui.orders.loyalty_discount': _ui_tr(" in text
    assert "'ui.orders.tier_discount': _ui_tr(" in text
    assert "'ui.orders.delivery_fee': _ui_tr(" in text


def test_seed_script_includes_the_tier_discount_condition_key():
    """GET /loyalty/tiers renders this key. get_translation returns the KEY
    itself when the row is missing, so an unseeded deploy publishes the literal
    string 'api.loyalty.tier_discount_condition' to every /my-loyalty visitor."""
    text = SEED_SCRIPT.read_text(encoding="utf-8")

    assert "'api.loyalty.tier_discount_condition': {" in text
