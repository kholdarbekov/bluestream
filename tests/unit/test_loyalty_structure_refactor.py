"""Static regression checks for loyalty API/service boundary migration."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
LOYALTY_API_FILE = ROOT / "business_app" / "api" / "loyalty.py"
LOYALTY_SERVICE_FILE = ROOT / "business_app" / "services" / "loyalty_service.py"


def _extract_function_block(text: str, function_name: str) -> str:
    start = text.index(f"def {function_name}(")
    next_route = text.find("\n\n@loyalty_bp.route", start)
    if next_route == -1:
        next_route = len(text)
    return text[start:next_route]


def test_loyalty_api_no_longer_imports_models_or_uses_direct_queries():
    text = LOYALTY_API_FILE.read_text(encoding="utf-8")

    assert "from business_app.models" not in text
    assert ".query" not in text
    assert "db.session" not in text


def test_loyalty_api_delegates_migrated_flows_to_loyalty_service():
    text = LOYALTY_API_FILE.read_text(encoding="utf-8")

    delegated_methods = [
        "get_points_summary_for_user(",
        "get_account_dashboard_for_user(",
        "get_loyalty_history_for_user(",
        "get_profile_for_user(",
        "get_filtered_points_history_for_user(",
        "get_rewards_for_user(",
        "get_reward_details_for_user(",
        "get_redemption_history_for_user(",
        "get_active_programs(",
        "get_referral_info_for_user(",
        "get_statistics_for_user(",
        "get_tier_benefits_for_user(",
        "gift_points_by_phone(",
    ]

    for method in delegated_methods:
        assert method in text


def test_loyalty_api_targeted_routes_do_not_contain_query_calls():
    text = LOYALTY_API_FILE.read_text(encoding="utf-8")

    functions = [
        "get_loyalty_points",
        "get_loyalty_account",
        "get_loyalty_points_history",
        "get_loyalty_profile",
        "get_points_history",
        "get_available_rewards",
        "get_reward_details",
        "get_redemption_history",
        "get_loyalty_programs",
        "get_referral_info",
        "get_loyalty_statistics",
        "get_tier_benefits",
        "gift_points",
    ]

    for function_name in functions:
        block = _extract_function_block(text, function_name)
        assert ".query" not in block, function_name
        assert "db.session" not in block, function_name


def test_loyalty_service_contains_loyalty_api_entrypoints():
    text = LOYALTY_SERVICE_FILE.read_text(encoding="utf-8")

    assert "def get_points_summary_for_user(" in text
    assert "def get_account_dashboard_for_user(" in text
    assert "def get_profile_for_user(" in text
    assert "def get_filtered_points_history_for_user(" in text
    assert "def gift_points_by_phone(" in text
