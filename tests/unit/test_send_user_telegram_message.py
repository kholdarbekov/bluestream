# tests/unit/test_send_user_telegram_message.py
import pytest
from unittest.mock import MagicMock
from business_app.services.notification_service import NotificationService


@pytest.mark.unit
def test_send_user_telegram_message_uses_customer_token(app):
    with app.app_context():
        svc = NotificationService()
        captured = {}

        def fake_send(**kwargs):
            captured.update(kwargs)
            return {"success": True, "message_id": 999}

        svc._send_telegram_notification = fake_send
        svc.telegram_bot_token = "CUSTOMER_TOKEN"

        user = MagicMock()
        user.preferred_language = "ru"
        result = svc.send_user_telegram_message(user, "Hello there")

        assert result == {"success": True, "message_id": 999}
        assert captured["bot_token"] == "CUSTOMER_TOKEN"
        assert captured["language"] == "ru"
        assert captured["template_override"].content == "Hello there"


@pytest.mark.unit
def test_send_user_telegram_message_rejects_empty(app):
    with app.app_context():
        svc = NotificationService()
        result = svc.send_user_telegram_message(MagicMock(), "")
        assert result["success"] is False


@pytest.mark.unit
def test_send_user_telegram_message_html_escapes_special_chars(app):
    with app.app_context():
        svc = NotificationService()
        captured = {}

        def fake_send(**kwargs):
            captured.update(kwargs)
            return {"success": True, "message_id": 42}

        svc._send_telegram_notification = fake_send
        svc.telegram_bot_token = "CUSTOMER_TOKEN"

        user = MagicMock()
        user.preferred_language = "en"
        result = svc.send_user_telegram_message(user, "price < 50k & free")

        assert result == {"success": True, "message_id": 42}
        assert captured["template_override"].content == "price &lt; 50k &amp; free"
        assert captured["template_override"].get_translated("content", "en") == "price &lt; 50k &amp; free"
