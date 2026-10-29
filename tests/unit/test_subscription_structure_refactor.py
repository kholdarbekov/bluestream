"""Static regression checks for subscription API/service boundary migration."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SUBSCRIPTIONS_API_FILE = ROOT / "business_app" / "api" / "subscriptions.py"
SUBSCRIPTION_SERVICE_FILE = ROOT / "business_app" / "services" / "subscription_service.py"


def test_subscriptions_api_no_longer_contains_direct_model_queries():
    text = SUBSCRIPTIONS_API_FILE.read_text(encoding="utf-8")

    assert "from business_app.models" not in text
    assert ".query" not in text
    assert "db.session.add(" not in text
    assert "db.session.commit(" not in text
    assert "get_subscription_service()." in text


def test_subscriptions_api_delegates_user_scoped_operations_to_service():
    text = SUBSCRIPTIONS_API_FILE.read_text(encoding="utf-8")

    delegated_methods = [
        "get_user_subscriptions_paginated(",
        "get_subscription_details_for_user(",
        "create_subscription_for_user(",
        "update_subscription_for_user(",
        "pause_subscription_for_user(",
        "resume_subscription_for_user(",
        "cancel_subscription_for_user(",
        "get_subscription_items_for_user(",
        "add_subscription_item_for_user(",
        "update_subscription_item_for_user(",
        "remove_subscription_item_for_user(",
        "subscription_history_for_user(",
        "replace_items_for_user(",
        "preview_changes_for_user(",
        "get_subscription_logs_paginated_for_user(",
        "get_subscription_statistics_for_user(",
        "skip_next_delivery_for_user(",
        "change_payment_method_for_user(",
    ]

    for method in delegated_methods:
        assert method in text


def test_subscription_service_contains_user_scoped_entrypoints():
    text = SUBSCRIPTION_SERVICE_FILE.read_text(encoding="utf-8")

    assert "def create_subscription_for_user(" in text
    assert "def update_subscription_for_user(" in text
    assert "def skip_next_delivery_for_user(" in text
    assert "def change_payment_method_for_user(" in text
    # The retry-billing route and its validator are gone (spec 2026-10-23 §4.7): nothing bills. So is the
    # always-empty billing history (spec 2026-10-26 §5 B6): the subscription's orders are its history.
    assert "def validate_retry_billing_for_user(" not in text
    assert "def get_subscription_billing_history_for_user(" not in text
