"""Eskiz refused a token it had issued a tenth of a second earlier, and a new customer got no code (2026-11-15).

After every deploy the first OTP SMS from a container logs in to Eskiz and sends with the brand-new token at once.
Eskiz stamps the token's issue time in whole seconds, and the server that checked the send was still in the previous
second: it answered 401 "Token used before issued". The same token was accepted 41 minutes later. The send now waits
past that second and resends once, through the same client and with the same token. Nothing else is resent: any other
refusal, or a send whose answer was lost, may already have reached the handset or been billed.

Driven through the endpoints the bot and the website call, with the real NotificationService and the real eskiz_sms
client. Only Eskiz's HTTP API is replaced (an httpx.MockTransport that records every request), and the wait is
recorded instead of slept.
"""

import functools
import logging
import re
from urllib.parse import parse_qs

import httpx
import pytest
import redis
from eskiz_sms import request as eskiz_request

from business_app.models.user import User
from business_app.services import notification_service as ns_module
from business_app.services.notification_service import NotificationService
from business_app.tasks import notification_tasks
from business_app.utils.password_security import hash_password
from shared.enums import UserStatus
from tests.integration.celery_task_harness import patch_task

# conftest's `block_external_side_effects` swaps both for stubs; these are the real ones
_REAL_SEND_SMS_TO_PHONE = NotificationService.send_sms_to_phone
_REAL_HTTPX_CLIENT = httpx.Client

LOGIN_PATH = "/api/auth/login"
SEND_PATH = "/api/message/sms/send"
TOKEN = "tok-1"

ACCEPTED = (200, {"id": "m-1", "status": "waiting", "message": "Waiting for SMS provider"})
FRESH_TOKEN_REFUSED = (401, {"message": "Token used before issued"})

NEW_PHONE = "+998901112233"


class FakeEskiz:
    """Eskiz's HTTP API. Login issues TOKEN; each send gets the next of `send_answers` (the last one repeats)."""

    def __init__(self):
        self.send_answers = [ACCEPTED]
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        if request.url.path == LOGIN_PATH:
            return httpx.Response(200, json={"message": "token_generated", "data": {"token": TOKEN}})
        if request.url.path == SEND_PATH:
            answer = self.send_answers[min(len(self.sends), len(self.send_answers)) - 1]
            if isinstance(answer, Exception):
                raise answer
            status, body = answer
            return httpx.Response(status, json=body)
        return httpx.Response(404, json={"message": "Not Found"})

    @property
    def logins(self):
        return [r for r in self.requests if r.url.path == LOGIN_PATH]

    @property
    def sends(self):
        return [r for r in self.requests if r.url.path == SEND_PATH]

    @staticmethod
    def form(request):
        return {key: values[0] for key, values in parse_qs(request.content.decode()).items()}

    def code_in(self, request):
        """The code the customer reads off the SMS this send carried."""
        return re.search(r"(\d{4,8})\s*$", self.form(request)["message"]).group(1)


@pytest.fixture
def eskiz(app, monkeypatch, tmp_path):
    """The real send path down to the real eskiz_sms client, against FakeEskiz.

    Run in tmp_path: the library keeps its token in a '.env' in the working directory, as it does in /app in
    production, and a new directory is what a freshly deployed container has (no token yet, so the first send logs in).
    """
    fake = FakeEskiz()
    monkeypatch.setattr(NotificationService, "send_sms_to_phone", _REAL_SEND_SMS_TO_PHONE)
    monkeypatch.setattr(
        eskiz_request.httpx, "Client", functools.partial(_REAL_HTTPX_CLIENT, transport=httpx.MockTransport(fake))
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setitem(app.config, "ESKIZ_EMAIL", "sms@example.com")
    monkeypatch.setitem(app.config, "ESKIZ_PASSWORD", "eskiz-test-password")
    return fake


@pytest.fixture
def waits(monkeypatch):
    """Every wait the send path asks for, in seconds; none is slept."""
    recorded = []
    monkeypatch.setattr(ns_module, "sleep", recorded.append, raising=False)
    return recorded


@pytest.fixture
def logs(caplog):
    """INFO and above from the SMS send path and the OTP tasks (their task loggers do not reach caplog's root)."""
    loggers = [ns_module.logger, notification_tasks.logger]
    for logger in loggers:
        logger.addHandler(caplog.handler)
    caplog.set_level(logging.INFO, logger=ns_module.logger.name)
    caplog.set_level(logging.INFO, logger=notification_tasks.logger.name)
    yield caplog
    for logger in loggers:
        logger.removeHandler(caplog.handler)


def _make_user(db, **fields):
    user = User(
        password_hash=hash_password("TestPassword123!"), first_name="Kamola", status=UserStatus.ACTIVE, **fields
    )
    db.session.add(user)
    db.session.commit()
    return user


def _assert_one_login_then_the_same_sms_twice_with_its_token(eskiz):
    assert len(eskiz.logins) == 1
    first, second = eskiz.sends
    assert first.headers["Authorization"] == second.headers["Authorization"] == f"Bearer {TOKEN}"
    assert eskiz.form(first) == eskiz.form(second)
    return second


@pytest.mark.integration
def test_bot_send_otp_resends_once_when_eskiz_says_token_used_before_issued(
    client, db, sample_user, auth_headers, eskiz, waits, logs
):
    """POST /auth/send-otp as the bot sends it (telegram_bot/api_client.py send_phone_verification)."""
    eskiz.send_answers = [FRESH_TOKEN_REFUSED, ACCEPTED]

    resp = client.post("/api/v1/auth/send-otp", json={"phone": NEW_PHONE}, headers=auth_headers)

    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["data"]["phone_masked"]
    delivered = _assert_one_login_then_the_same_sms_twice_with_its_token(eskiz)
    assert eskiz.form(delivered)["mobile_phone"] == "998901112233"
    assert len(waits) == 1 and waits[0] >= 1.0

    [warning] = [r for r in logs.records if "Eskiz fresh-token race; retrying once" in r.getMessage()]
    assert warning.levelno == logging.WARNING
    assert TOKEN not in warning.getMessage()
    assert "+998***2233" in warning.getMessage() and "901112233" not in warning.getMessage()

    # The customer types the code the SMS brought, and the number is theirs
    resp = client.post("/api/v1/auth/verify-phone", json={"otp": eskiz.code_in(delivered)}, headers=auth_headers)
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["data"] == {"phone": NEW_PHONE, "phone_verified": True}
    db.session.refresh(sample_user)
    assert sample_user.phone == NEW_PHONE


@pytest.mark.integration
def test_web_signup_delivers_code_after_token_used_before_issued(client, db, eskiz, waits, monkeypatch):
    """POST /auth/phone/register/init as register.js sends it; its Celery task is run inline for this test only."""
    task = notification_tasks.send_registration_otp_task
    patch_task(monkeypatch, task, "delay", lambda *args, **kwargs: task.apply(args=args, kwargs=kwargs, throw=True))
    eskiz.send_answers = [FRESH_TOKEN_REFUSED, ACCEPTED]
    phone = "+998901112255"

    init = client.post("/api/v1/auth/phone/register/init", json={"phone": phone, "preferred_language": "ru"})

    assert init.status_code == 200, init.get_json()
    delivered = _assert_one_login_then_the_same_sms_twice_with_its_token(eskiz)
    assert len(waits) == 1 and waits[0] >= 1.0

    verify = client.post(
        "/api/v1/auth/phone/register/verify",
        json={
            "phone": phone,
            "otp_code": eskiz.code_in(delivered),
            "first_name": "Web",
            "last_name": "Signup",
            "password": "StrongPass123!",
            "referral_code": None,
        },
    )
    assert verify.status_code in (200, 201), verify.get_json()
    created = User.query.filter_by(phone=phone).one()
    assert created.registration_method == "phone"


@pytest.mark.integration
def test_link_phone_send_otp_survives_token_used_before_issued(db, eskiz, waits, signed_bot_post):
    """POST /auth/link-phone-account/send-otp as the bot sends it (api_client.link_phone_send_otp), then the code."""
    telegram_user = _make_user(
        db, email="telegram_515151@bot.internal", phone=None, telegram_id="515151", registration_source="telegram"
    )
    web_user = _make_user(db, email="link-target@example.com", phone="+998907770011")
    eskiz.send_answers = [FRESH_TOKEN_REFUSED, ACCEPTED]

    resp = signed_bot_post(
        "/api/v1/auth/link-phone-account/send-otp",
        {"telegram_id": int(telegram_user.telegram_id), "phone": web_user.phone},
    )

    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["data"]["phone_masked"]
    delivered = _assert_one_login_then_the_same_sms_twice_with_its_token(eskiz)
    assert eskiz.form(delivered)["mobile_phone"] == "998907770011"
    assert len(waits) == 1

    resp = signed_bot_post(
        "/api/v1/auth/link-phone-account/verify",
        {"telegram_id": int(telegram_user.telegram_id), "otp": eskiz.code_in(delivered)},
    )
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["data"]["user"]["id"] == web_user.id


@pytest.mark.integration
def test_token_used_before_issued_retry_is_bounded(client, app, db, sample_user, auth_headers, eskiz, waits):
    """Eskiz refuses the resend too: one resend only, then today's failure, and the code that reached nobody is gone."""
    eskiz.send_answers = [FRESH_TOKEN_REFUSED]

    resp = client.post("/api/v1/auth/send-otp", json={"phone": NEW_PHONE}, headers=auth_headers)

    assert resp.status_code == 500, resp.get_json()
    _assert_one_login_then_the_same_sms_twice_with_its_token(eskiz)
    assert len(waits) == 1
    store = redis.from_url(app.config["REDIS_URL"])
    assert store.get(f"sms_verification:{sample_user.id}") is None
    assert store.get(f"sms_verification_phone:{sample_user.id}") is None


@pytest.mark.integration
@pytest.mark.parametrize(
    "answer",
    [
        pytest.param((400, {"message": "Этот смс текст еще не прошёл модерацию"}), id="not-moderated"),
        pytest.param((401, {"message": "Unauthenticated"}), id="other-401"),
        pytest.param((500, {"message": "Server Error"}), id="server-error"),
        pytest.param(httpx.ReadTimeout("answer lost"), id="answer-lost"),
    ],
)
def test_other_provider_errors_are_not_retried(client, db, sample_user, auth_headers, eskiz, waits, answer):
    """Any other refusal, or a send whose answer never came, is not resent: it may have reached the handset or
    been billed already. The customer gets today's failure."""
    eskiz.send_answers = [answer, ACCEPTED]

    resp = client.post("/api/v1/auth/send-otp", json={"phone": NEW_PHONE}, headers=auth_headers)

    assert resp.status_code == 500, resp.get_json()
    assert len(eskiz.sends) == 1
    assert waits == []


@pytest.mark.integration
def test_a_code_the_provider_refused_is_not_logged_as_sent(client, db, sample_user, auth_headers, eskiz, logs):
    """send_verification_sms_task said "SMS verification sent successfully" right after Eskiz refused the send."""
    eskiz.send_answers = [(400, {"message": "Этот смс текст еще не прошёл модерацию"})]

    resp = client.post("/api/v1/auth/send-otp", json={"phone": NEW_PHONE}, headers=auth_headers)

    assert resp.status_code == 500, resp.get_json()
    task_records = [r for r in logs.records if r.name == notification_tasks.logger.name]
    assert not [r for r in task_records if "sent successfully" in r.getMessage()]
    [not_sent] = [r for r in task_records if "was not sent" in r.getMessage()]
    assert not_sent.levelno == logging.WARNING
    assert f"user {sample_user.id}" in not_sent.getMessage()


@pytest.mark.integration
def test_a_code_the_provider_took_is_still_logged_as_sent(client, db, sample_user, auth_headers, eskiz, logs):
    resp = client.post("/api/v1/auth/send-otp", json={"phone": NEW_PHONE}, headers=auth_headers)

    assert resp.status_code == 200, resp.get_json()
    task_records = [r for r in logs.records if r.name == notification_tasks.logger.name]
    assert [r.levelno for r in task_records if "SMS verification sent successfully" in r.getMessage()] == [logging.INFO]
    assert not [r for r in task_records if "was not sent" in r.getMessage()]
