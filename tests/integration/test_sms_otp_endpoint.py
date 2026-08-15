"""End-to-end proof that the OTP a customer actually receives is Latin.

This is the exact path a new customer takes: the Telegram bot POSTs to
/api/v1/auth/send-otp (see telegram_bot/api_client.py), which is the only
flow that has ever successfully delivered an SMS on this Eskiz account.

Driving it through HTTP rather than calling NotificationService directly is
deliberate — the service-level suite was green while production was sending
Cyrillic and rejecting registration texts for a month.
"""

import pytest
from types import SimpleNamespace
from unittest.mock import MagicMock

from business_app.services import notification_service as ns_module
from business_app.services.notification_service import NotificationService

MODERATED_RU = "Kod dlya podtverjdeniya vashego nomera telefona na platforme Aqua Element: "

# See tests/unit/test_sms_otp_only.py — conftest's autouse fixture stubs these
# out, and this test needs the production implementations.
_REAL_SEND_SMS_TO_PHONE = NotificationService.send_sms_to_phone


@pytest.fixture
def captured_sms(app, monkeypatch):
    """Give every NotificationService a fake Eskiz client and capture sends."""
    eskiz = MagicMock()
    eskiz.send_sms.return_value = SimpleNamespace(status="waiting", id="msg-1")

    monkeypatch.setattr(NotificationService, "send_sms_to_phone", _REAL_SEND_SMS_TO_PHONE)
    monkeypatch.setattr(ns_module, "EskizSMS", lambda *a, **k: eskiz)
    app.config["ESKIZ_EMAIL"] = "test@example.com"
    app.config["ESKIZ_PASSWORD"] = "test-password"
    return eskiz


@pytest.mark.integration
@pytest.mark.parametrize(
    "language, expected_prefix",
    [
        ("ru", MODERATED_RU),
        ("uz", "Aqua Element platformasida telefon raqamingizni tasdiqlash uchun kod: "),
        ("en", "Code to verify your phone number on the Aqua Element platform: "),
    ],
)
def test_send_otp_endpoint_delivers_moderated_latin_text(
    client, db, sample_user, auth_headers, captured_sms, language, expected_prefix
):
    sample_user.preferred_language = language
    db.session.commit()

    response = client.post(
        "/api/v1/auth/send-otp",
        json={"phone": "+998901112233"},
        headers=auth_headers,
    )

    assert response.status_code == 200, response.get_json()
    captured_sms.send_sms.assert_called_once()
    message = captured_sms.send_sms.call_args.kwargs["message"]

    assert message.startswith(expected_prefix), message
    # The code itself is the only variable part.
    assert message[len(expected_prefix):].isdigit(), message
    # No Cyrillic anywhere — that is what doubles the bill.
    assert not any("Ѐ" <= ch <= "ӿ" for ch in message), message
