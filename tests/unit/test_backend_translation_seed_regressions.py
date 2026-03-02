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
