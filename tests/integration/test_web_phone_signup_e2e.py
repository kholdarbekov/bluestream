"""Web phone signup must work end to end, on the moderated OTP text.

This flow (static/js/pages/register.js -> /auth/phone/register/init) had its
own `sms.registration.otp` text which never passed Eskiz moderation, so every
send was rejected with HTTP 400 and no customer ever received a code. It now
uses the same moderated `sms.verification.otp` text as every other OTP.

The end-to-end test deliberately reads the code out of the captured SMS rather
than out of Redis: that is what a real customer does, and it proves the code
that reaches the handset is the one the verify endpoint accepts.
"""

import hashlib
import re

import pytest
import redis
from types import SimpleNamespace
from unittest.mock import MagicMock

from business_app.models.user import User
from business_app.services import notification_service as ns_module
from business_app.services.notification_service import NotificationService

MODERATED = {
    "ru": "Kod dlya podtverjdeniya vashego nomera telefona na platforme Aqua Element: ",
    "uz": "Aqua Element platformasida telefon raqamingizni tasdiqlash uchun kod: ",
    "en": "Code to verify your phone number on the Aqua Element platform: ",
}

_REAL_SEND_SMS_TO_PHONE = NotificationService.send_sms_to_phone


@pytest.fixture
def captured_sms(app, monkeypatch):
    """Real send path, fake provider.

    Two conftest containment measures have to be lifted for this flow to run
    at all, both scoped as narrowly as possible:
      * `block_external_side_effects` swaps send_sms_to_phone for an
        always-succeeds stub — restored, since the send is what we assert on.
      * the same fixture turns Task.delay into a no-op, so the OTP task would
        never execute. Only this one task is made to run inline; unrelated
        background work stays blocked.
    """
    from business_app.tasks import notification_tasks

    eskiz = MagicMock()
    eskiz.send_sms.return_value = SimpleNamespace(status="waiting", id="msg-1")

    monkeypatch.setattr(NotificationService, "send_sms_to_phone", _REAL_SEND_SMS_TO_PHONE)
    monkeypatch.setattr(ns_module, "EskizSMS", lambda *a, **k: eskiz)
    monkeypatch.setattr(
        notification_tasks.send_registration_otp_task,
        "delay",
        lambda *a, **kw: notification_tasks.send_registration_otp_task.run(*a, **kw),
    )
    app.config["ESKIZ_EMAIL"] = "test@example.com"
    app.config["ESKIZ_PASSWORD"] = "test-password"
    return eskiz


def _sent_message(eskiz):
    assert eskiz.send_sms.called, "no SMS was dispatched"
    return eskiz.send_sms.call_args.kwargs["message"]


def _clear_resend_cooldown(app, phone):
    phone_hash = hashlib.sha256(phone.encode()).hexdigest()[:16]
    redis.from_url(app.config["REDIS_URL"]).delete(f"phone_otp_cooldown:{phone_hash}")


@pytest.mark.integration
@pytest.mark.parametrize("language", ["uz", "ru", "en"])
def test_web_signup_init_sends_moderated_latin_otp(client, db, captured_sms, app, language):
    response = client.post(
        "/api/v1/auth/phone/register/init",
        json={"phone": "+998901112233", "preferred_language": language},
    )

    assert response.status_code == 200, response.get_json()
    message = _sent_message(captured_sms)
    assert message.startswith(MODERATED[language]), message
    assert not any("Ѐ" <= ch <= "ӿ" for ch in message), message


@pytest.mark.integration
def test_web_signup_resend_sends_moderated_latin_otp(client, db, captured_sms, app):
    phone = "+998901112244"
    client.post(
        "/api/v1/auth/phone/register/init",
        json={"phone": phone, "preferred_language": "ru"},
    )
    captured_sms.send_sms.reset_mock()
    _clear_resend_cooldown(app, phone)

    response = client.post("/api/v1/auth/phone/resend-otp", json={"phone": phone})

    assert response.status_code == 200, response.get_json()
    assert _sent_message(captured_sms).startswith(MODERATED["ru"])


@pytest.mark.integration
def test_web_signup_creates_account_using_the_code_from_the_sms(client, db, captured_sms):
    """The whole point: a customer reads the code off their phone and it works."""
    phone = "+998901112255"

    init = client.post(
        "/api/v1/auth/phone/register/init",
        json={"phone": phone, "preferred_language": "ru"},
    )
    assert init.status_code == 200, init.get_json()

    otp_code = re.search(r"(\d{4,8})\s*$", _sent_message(captured_sms)).group(1)

    verify = client.post(
        "/api/v1/auth/phone/register/verify",
        json={
            "phone": phone,
            "otp_code": otp_code,
            "first_name": "Web",
            "last_name": "Signup",
            "password": "StrongPass123!",
        },
    )

    assert verify.status_code in (200, 201), verify.get_json()
    body = verify.get_json()["data"]
    assert body["tokens"]["access_token"]

    created = User.query.filter_by(phone=phone).first()
    assert created is not None
    assert created.first_name == "Web"
    assert created.registration_method == "phone"
    assert created.is_verified is True


@pytest.mark.integration
def test_web_signup_rejects_a_wrong_code(client, db, captured_sms):
    phone = "+998901112266"
    client.post(
        "/api/v1/auth/phone/register/init",
        json={"phone": phone, "preferred_language": "uz"},
    )
    sent_code = re.search(r"(\d{4,8})\s*$", _sent_message(captured_sms)).group(1)
    wrong = "000000" if sent_code != "000000" else "111111"

    verify = client.post(
        "/api/v1/auth/phone/register/verify",
        json={
            "phone": phone,
            "otp_code": wrong,
            "first_name": "Web",
            "last_name": "Signup",
            "password": "StrongPass123!",
        },
    )

    assert verify.status_code == 400, verify.get_json()
    assert User.query.filter_by(phone=phone).first() is None
