"""Static regression checks for orders API/service boundary migration."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
ORDERS_API_FILE = ROOT / "business_app" / "api" / "orders.py"
ORDER_SERVICE_FILE = ROOT / "business_app" / "services" / "order_service.py"


def _extract_function_block(text: str, function_name: str) -> str:
    start = text.index(f"def {function_name}(")
    next_route = text.find("\n\n@orders_bp.route", start)
    if next_route == -1:
        next_route = len(text)
    return text[start:next_route]


def test_orders_api_no_longer_imports_models_or_uses_direct_queries():
    text = ORDERS_API_FILE.read_text(encoding="utf-8")

    assert "from business_app.models" not in text
    assert ".query" not in text


def test_orders_api_delegates_migrated_flows_to_order_service():
    text = ORDERS_API_FILE.read_text(encoding="utf-8")

    delegated_methods = [
        "get_user_orders_paginated(",
        "get_order_details_for_user(",
        "validate_user_emergency_order_access(",
        "get_user_order_statistics(",
        "submit_order_feedback_for_user(",
        "get_user_and_address_for_order(",
        "repeat_order_for_user(",
        "get_order_tracking_for_user(",
        "perform_bulk_action(",
        "export_orders(",
        "create_subscription_order(",
    ]

    for method in delegated_methods:
        assert method in text


def test_orders_api_targeted_routes_do_not_contain_query_calls():
    text = ORDERS_API_FILE.read_text(encoding="utf-8")

    functions = [
        "get_orders",
        "get_order",
        "create_emergency_order",
        "get_order_statistics",
        "submit_order_feedback",
        "create_order",
        "cancel_order",
        "repeat_order",
        "track_order",
        "bulk_order_action",
        "export_orders",
        "create_subscription_order",
    ]

    for function_name in functions:
        block = _extract_function_block(text, function_name)
        assert ".query" not in block, function_name


def test_order_service_contains_orders_api_entrypoints():
    text = ORDER_SERVICE_FILE.read_text(encoding="utf-8")

    assert "def get_user_orders_paginated(" in text
    assert "def get_user_order_statistics(" in text
    assert "def submit_order_feedback_for_user(" in text
    assert "def repeat_order_for_user(" in text
    assert "def create_subscription_order(" in text
