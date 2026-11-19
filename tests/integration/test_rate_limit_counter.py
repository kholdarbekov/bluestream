"""The custom Redis rate limiters count every request atomically, always give their counter an expiry, and log
every refusal.

`rate_limit` and `rate_limit_by_telegram_id` used to GET the count and then SETEX or INCR it as a second step.
When the key expired between the two, INCR recreated it with no expiry, so that bucket never reset: once it
reached the limit it refused every request on it for good (with the customer bot's one container address, every
bot customer). And a refusal from plain `rate_limit` left no log line at all: the limiter sits outside
`handle_exceptions` and `log_request`, and the global WaterBusinessException handler does not log.

Driven through the real endpoints with the payloads the bot and the web page send. Only the SMS gateway
(`NotificationService.send_sms_to_phone`) is replaced, by an inbox that records the codes it was asked to send, and,
in the one test about a Redis outage, the limiter's Redis.
"""

import logging
import re
from types import SimpleNamespace

import pytest
import redis
from flask_jwt_extended import create_access_token

from business_app.services.notification_service import NotificationService
from business_app.utils import decorators
from tests.integration.redis_fakes import DownRedis

pytestmark = pytest.mark.integration

SEND_OTP = "/api/v1/auth/send-otp"
LOGIN = "/api/v1/auth/login"
CHECK_PHONE = "/api/v1/auth/check-phone-availability"
EXPORT = "/api/v1/auth/export-data"

# Every request of the test client comes from this address (the customer bot's requests all share theirs).
CLIENT_IP = "127.0.0.1"
TELEGRAM_ID = 880000777
# Numbers a customer may type one after another in Profile -> Phone: each new number gets its code at once.
NUMBERS = ["+998901230001", "+998901230002", "+998901230003", "+998901230004"]
SEND_OTP_LIMIT, SEND_OTP_WINDOW = 3, 60  # @rate_limit(3, 60) on /auth/send-otp


def _redis(app):
    return redis.from_url(app.config["REDIS_URL"])


def _ttl(app, key):
    # A plain int, so a failing assertion prints the number and not the client (whose repr holds the password).
    return _redis(app).ttl(key)


@pytest.fixture
def sms_inbox(monkeypatch):
    """Codes delivered per number, in the order they arrived."""
    inbox = {}

    def _send_sms_to_phone(_self, phone, notification_type, template_key, template_data, language="uz"):
        inbox.setdefault(phone, []).append(template_data["otp_code"])
        return {"success": True, "phone": phone}

    monkeypatch.setattr(NotificationService, "send_sms_to_phone", _send_sms_to_phone, raising=True)
    return inbox


@pytest.fixture
def bearer(app):
    def _headers(user):
        with app.app_context():
            return {"Authorization": f"Bearer {create_access_token(identity=str(user.id))}"}

    return _headers


@pytest.fixture
def limiter_warnings(app, caplog):
    """WARNING records the rate limiters write. The app logger does not propagate, so caplog's handler is put on it."""
    app.logger.addHandler(caplog.handler)
    caplog.set_level(logging.WARNING, logger=app.logger.name)
    try:
        yield lambda: [
            record for record in caplog.records if record.module == "decorators" and record.levelno == logging.WARNING
        ]
    finally:
        app.logger.removeHandler(caplog.handler)


def _refusal_line(name, identifier_type, limit, window):
    """The one shape of a refusal line: the function, the kind of key, the limit and the wait, never the key."""
    return re.compile(
        rf"^Rate limit refused {name} \(per {identifier_type}\): limit {limit} per {window}s, retry after (\d+)s$"
    )


# --------------------------------------------------------------------------- the counter always expires


def _post_login(client, signed_bot_post):
    # The web login form's payload; nobody has this address, so the route answers 401 after counting the attempt.
    return client.post(LOGIN, json={"identifier": "nobody-here@example.com", "password": "Wrong-password-1"})


def _post_check_phone(client, signed_bot_post):
    # telegram_bot/api_client.py check_phone_availability, signed as the bot signs it.
    return signed_bot_post(CHECK_PHONE, {"telegram_id": TELEGRAM_ID, "phone": NUMBERS[0]})


@pytest.mark.parametrize(
    "key, window, call",
    [
        (f"rate_limit:login:{CLIENT_IP}", 3600, _post_login),  # @rate_limit(20, 3600), per address
        (f"rate_limit:check_phone_availability:telegram:{TELEGRAM_ID}", 3600, _post_check_phone),  # per telegram id
    ],
    ids=["rate_limit", "rate_limit_by_telegram_id"],
)
def test_a_counter_left_without_expiry_gets_one(app, db, client, signed_bot_post, key, window, call):
    """The state the old GET-then-INCR race left behind: a counter with no expiry, which would never reset."""
    _redis(app).set(key, 1)
    assert _ttl(app, key) == -1

    resp = call(client, signed_bot_post)

    assert resp.status_code != 429, resp.get_json()
    count, ttl = int(_redis(app).get(key)), _ttl(app, key)
    assert count == 2  # the request was counted
    assert 0 < ttl <= window  # and the bucket ends again


# --------------------------------------------------------------------------- the limit itself is unchanged


def test_refusal_comes_one_past_the_limit_and_keeps_the_window(app, db, client, sample_user, bearer, sms_inbox):
    """The bot's Profile -> Phone: three numbers in a minute get their codes, the fourth is refused, and so is the
    next try; a refused try sends nothing and does not stretch the wait past the window."""
    key = f"rate_limit:send_otp:{CLIENT_IP}"

    for number in NUMBERS[:SEND_OTP_LIMIT]:
        resp = client.post(SEND_OTP, json={"phone": number}, headers=bearer(sample_user))
        assert resp.status_code == 200, resp.get_json()
        ttl = _ttl(app, key)
        assert 0 < ttl <= SEND_OTP_WINDOW
    assert sorted(sms_inbox) == sorted(NUMBERS[:SEND_OTP_LIMIT])

    refused = client.post(SEND_OTP, json={"phone": NUMBERS[3]}, headers=bearer(sample_user))
    again = client.post(SEND_OTP, json={"phone": NUMBERS[3]}, headers=bearer(sample_user))

    for resp in (refused, again):
        assert resp.status_code == 429, resp.get_json()
        body = resp.get_json()
        assert body["error"] == "RATE_LIMIT_EXCEEDED"
        retry_after = body["details"]["retry_after"]
        assert 0 < retry_after <= SEND_OTP_WINDOW
        assert body["details"]["limit"] == SEND_OTP_LIMIT
        assert body["message"] == f"Rate limit exceeded. Try again in {retry_after} seconds."
    assert NUMBERS[3] not in sms_inbox
    ttl = _ttl(app, key)
    assert 0 < ttl <= SEND_OTP_WINDOW


# --------------------------------------------------------------------------- every refusal leaves one line


def test_a_refused_request_is_logged(app, db, client, sample_user, bearer, sms_inbox, limiter_warnings):
    """The bot's Profile -> Phone again: the fourth number in a minute is refused, and the refusal is logged with
    the function, the kind of key, the limit and the wait, and without the address, a number or a code."""
    for number in NUMBERS[:SEND_OTP_LIMIT]:
        assert client.post(SEND_OTP, json={"phone": number}, headers=bearer(sample_user)).status_code == 200
    assert limiter_warnings() == []

    refused = client.post(SEND_OTP, json={"phone": NUMBERS[3]}, headers=bearer(sample_user))

    assert refused.status_code == 429, refused.get_json()
    [record] = limiter_warnings()
    line = record.getMessage()
    match = _refusal_line("send_otp", "ip", SEND_OTP_LIMIT, SEND_OTP_WINDOW).match(line)
    assert match, line
    assert int(match.group(1)) == refused.get_json()["details"]["retry_after"]
    codes = [code for sent in sms_inbox.values() for code in sent]
    for secret in [CLIENT_IP, *NUMBERS, *(n.lstrip("+") for n in NUMBERS), *(n[-9:] for n in NUMBERS), *codes]:
        assert secret not in line


def _refuse_export(app, client, signed_bot_post, user, bearer):
    # The cabinet's "Download my account data": @rate_limit(2, 3600, per="user"), already spent this hour.
    _redis(app).set(f"rate_limit:export_account_data:{user.id}", 2, ex=3600)
    return client.post(EXPORT, headers=bearer(user))


def _refuse_check_phone(app, client, signed_bot_post, user, bearer):
    # @rate_limit_by_telegram_id(30, 3600), already spent this hour by this Telegram user.
    _redis(app).set(f"rate_limit:check_phone_availability:telegram:{TELEGRAM_ID}", 30, ex=3600)
    return _post_check_phone(client, signed_bot_post)


@pytest.mark.parametrize(
    "refuse, expected_line",
    [
        (_refuse_export, _refusal_line("export_account_data", "user", 2, 3600)),
        (_refuse_check_phone, _refusal_line("check_phone_availability", "telegram", 30, 3600)),
    ],
    ids=["per_user", "per_telegram_id"],
)
def test_every_kind_of_key_logs_its_refusal_the_same_way(
    app, db, client, signed_bot_post, sample_user, bearer, limiter_warnings, refuse, expected_line
):
    refused = refuse(app, client, signed_bot_post, sample_user, bearer)

    assert refused.status_code == 429, refused.get_json()
    [record] = limiter_warnings()
    line = record.getMessage()
    match = expected_line.match(line)
    assert match, line
    assert int(match.group(1)) == refused.get_json()["details"]["retry_after"]
    assert str(TELEGRAM_ID) not in line and CLIENT_IP not in line and NUMBERS[0] not in line


# --------------------------------------------------------------------------- a Redis outage never locks anyone out


def test_the_limiter_lets_requests_through_while_redis_is_down(
    app, db, client, sample_user, bearer, sms_inbox, limiter_warnings, monkeypatch
):
    """Fails open, as before: a fourth number in a minute still gets its code when nothing can be counted."""
    monkeypatch.setattr(
        decorators, "redis", SimpleNamespace(from_url=lambda url: DownRedis(), RedisError=redis.RedisError)
    )

    for number in NUMBERS:
        resp = client.post(SEND_OTP, json={"phone": number}, headers=bearer(sample_user))
        assert resp.status_code == 200, resp.get_json()

    assert sorted(sms_inbox) == sorted(NUMBERS)
    lines = [record.getMessage() for record in limiter_warnings()]
    assert lines == ["Redis unavailable for rate limiting: Connection refused"] * len(NUMBERS)
