"""Static regression checks for notifications API/service boundary migration."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
NOTIFICATIONS_API_FILE = ROOT / "business_app" / "api" / "notifications.py"
NOTIFICATION_SERVICE_FILE = ROOT / "business_app" / "services" / "notification_service.py"


def _extract_function_block(text: str, function_name: str) -> str:
    start = text.index(f"def {function_name}(")
    next_route = text.find("\n\n@notifications_bp.route", start)
    if next_route == -1:
        next_route = len(text)
    return text[start:next_route]


def test_notifications_api_no_longer_contains_direct_model_queries():
    text = NOTIFICATIONS_API_FILE.read_text(encoding="utf-8")

    assert "from business_app.models" not in text
    assert ".query" not in text
    assert "db.session" not in text


def test_notifications_api_delegates_to_notification_service_entrypoints():
    text = NOTIFICATIONS_API_FILE.read_text(encoding="utf-8")

    delegated_methods = [
        "get_user_notifications_paginated(",
        "get_notification_for_user(",
        "mark_notification_read(",
        "mark_all_notifications_read(",
        "delete_notification_for_user(",
        "create_default_preferences(",
        "update_notification_preferences_for_user(",
        "register_push_token_for_user(",
        "unregister_push_token_for_user(",
        "get_active_templates(",
        "send_test_notification_from_template(",
        "get_notification_statistics_for_user(",
        "get_user_notification_channels(",
        "queue_bulk_notification(",
        "get_delivery_reports_paginated(",
    ]

    for method in delegated_methods:
        assert method in text


def test_notifications_api_targeted_routes_do_not_use_db_access_patterns():
    text = NOTIFICATIONS_API_FILE.read_text(encoding="utf-8")
    functions = [
        "get_notifications",
        "get_notification",
        "mark_notification_read",
        "mark_all_notifications_read",
        "delete_notification",
        "get_notification_preferences",
        "update_notification_preferences",
        "register_push_token",
        "unregister_push_token",
        "get_notification_templates",
        "send_test_notification",
        "get_notification_statistics",
        "get_notification_channels",
        "send_bulk_notification",
        "get_delivery_reports",
    ]

    for function_name in functions:
        block = _extract_function_block(text, function_name)
        assert ".query" not in block, function_name
        assert "db.session" not in block, function_name


def test_notification_service_contains_notifications_api_entrypoints():
    text = NOTIFICATION_SERVICE_FILE.read_text(encoding="utf-8")

    assert "def get_user_notifications_paginated(" in text
    assert "def update_notification_preferences_for_user(" in text
    assert "def queue_bulk_notification(" in text
    assert "def get_delivery_reports_paginated(" in text
