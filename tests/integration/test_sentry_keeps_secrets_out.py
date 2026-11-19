"""No secret leaves the process in a Sentry event or a performance trace.

Production reports with `attach_stacktrace=True`: an error that is only LOGGED (`logger.error`, no exception) gets the
logging thread's whole stack, every frame's locals included, so a code, a token or a password held by any caller up
the stack rides along. And Sentry's Redis integration writes each command's key into its span (description and tags)
and into the breadcrumb the command leaves on the scope; a password-reset or email-verification token is part of its
key (`verification:<kind>:<token>`), so a sampled request that issues or redeems one carried the live token.

A real Sentry client runs here with production's privacy options and business_app.utils.sentry's hooks; what it would
send is recorded by a transport that never leaves the process (the conftest strips every DSN as well).
"""

import json
import logging
import re
import secrets
from unittest.mock import MagicMock

import pytest
import redis
import sentry_sdk
from flask_jwt_extended import create_access_token
from sentry_sdk.integrations.celery import CeleryIntegration
from sentry_sdk.integrations.flask import FlaskIntegration
from sentry_sdk.integrations.redis import RedisIntegration
from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration
from sentry_sdk.transport import Transport

from business_app.services import notification_service as ns_module
from business_app.services.auth_service import AuthService
from business_app.services.notification_service import NotificationService
from business_app.utils.sentry import before_send, before_send_transaction

pytestmark = pytest.mark.integration

_REAL_SEND_SMS_TO_PHONE = NotificationService.send_sms_to_phone
WEB_HEADERS = {"Content-Type": "application/json", "X-Platform": "web"}
NEW_PHONE = "+998901112277"

logger = logging.getLogger(__name__)


class _RecordingTransport(Transport):
    """Keeps every envelope the client would send; sends nothing."""

    def __init__(self):
        super().__init__()
        self.envelopes = []

    def capture_envelope(self, envelope):
        self.envelopes.append(envelope)

    def flush(self, timeout, callback=None):
        return None

    def kill(self):
        return None

    def payloads(self, item_type):
        return [item.payload.json for envelope in self.envelopes for item in envelope.items if item.type == item_type]


@pytest.fixture
def sentry_sent():
    """A Sentry client with production's options (config/production.py): its integrations, a stack on every logged
    error with frame locals, no default PII, both business_app.utils.sentry hooks. Every request is traced here so the
    trace is never left to the 5% sample."""
    scope = sentry_sdk.get_global_scope()
    previous = scope.client
    transport = _RecordingTransport()
    sentry_sdk.init(
        transport=transport,
        integrations=[FlaskIntegration(), SqlalchemyIntegration(), RedisIntegration(), CeleryIntegration()],
        auto_enabling_integrations=False,
        traces_sample_rate=1.0,
        attach_stacktrace=True,
        send_default_pii=False,
        before_send=before_send,
        before_send_transaction=before_send_transaction,
    )
    assert sentry_sdk.get_client().dsn is None  # recorded, never sent
    yield transport
    built = scope.client
    scope.set_client(previous)
    built.close()


def _where(secret, payload, path="$"):
    """Every path in `payload` whose string holds `secret`, for a failure message that says where it leaked."""
    if isinstance(payload, str):
        return [path] if secret in payload else []
    if isinstance(payload, dict):
        found = []
        for key, value in payload.items():
            found += [f"{path}.{key}<key>"] if secret in str(key) else []
            found += _where(secret, value, f"{path}.{key}")
        return found
    if isinstance(payload, (list, tuple)):
        return [hit for index, value in enumerate(payload) for hit in _where(secret, value, f"{path}[{index}]")]
    return []


def assert_carries_none_of(payloads, *secrets):
    assert payloads, "nothing was reported"
    for secret in secrets:
        leaks = _where(secret, payloads)
        assert not leaks, f"{secret!r} reached Sentry at {leaks}"


def _frames(event):
    for holder in [
        event,
        *(event.get("threads") or {}).get("values", []),
        *(event.get("exception") or {}).get("values", []),
    ]:
        yield from ((holder or {}).get("stacktrace") or {}).get("frames") or []


def _frame_of(event, function):
    [frame] = [frame for frame in _frames(event) if frame.get("function") == function]
    return frame


# ----------------------------------------------------------------------------------- a logged error's frame locals


def _holds_secrets_and_logs(otp, reset_token, new_password, sign_string):
    status_code = 502  # the SDK sends a frame's first ten locals only
    form = {"otp": otp, "new_password": new_password, "key": "phone", "phone": "+998901112233"}
    otp_code = code = otp
    verification_token = reset_token
    signature = sign_string
    logger.error("The SMS provider refused the phone")


def test_a_logged_error_carries_no_frame_local_secret(app, sentry_sent):
    """No exception: the error is only logged, so Sentry adds the logging thread's stack, every frame's locals with
    it. The secrets are held by name in the logging frame and passed down from the test's own frame."""
    # Made at run time: a literal would be in the source lines Sentry sends around each frame
    otp, reset_token = f"{secrets.randbelow(10**6):06d}", secrets.token_urlsafe(24)
    new_password, sign_string = "Pa5-" + secrets.token_urlsafe(12), secrets.token_hex(16)

    _holds_secrets_and_logs(otp, reset_token, new_password, sign_string)

    [event] = sentry_sent.payloads("event")
    assert_carries_none_of(event, otp, reset_token, new_password, sign_string)
    frame = _frame_of(event, "_holds_secrets_and_logs")
    names = frame["vars"]
    # What is kept: every name, and the value of whatever is not a secret
    assert {"otp", "otp_code", "code", "reset_token", "verification_token", "signature", "form"} <= set(names)
    assert names["status_code"] == "502"
    assert names["form"]["phone"] == "'+998901112233'"
    # A plain word under a secret name (`key`) is hidden there only
    assert names["form"]["key"] == "[REDACTED]"
    assert event["logentry"]["message"] == "The SMS provider refused the phone"


# --------------------------------------------------------------------- the sign-up code an unsent SMS leaves behind


@pytest.fixture
def eskiz_down(app, monkeypatch):
    """The real SMS path down to Eskiz, which does not answer."""
    eskiz = MagicMock()
    eskiz.send_sms.side_effect = ConnectionError("Eskiz did not answer")
    monkeypatch.setattr(NotificationService, "send_sms_to_phone", _REAL_SEND_SMS_TO_PHONE)
    monkeypatch.setattr(ns_module, "EskizSMS", lambda *a, **k: eskiz)
    monkeypatch.setitem(app.config, "ESKIZ_EMAIL", "sender@example.com")
    monkeypatch.setitem(app.config, "ESKIZ_PASSWORD", "test-password")
    return eskiz


def test_a_code_the_provider_did_not_take_reaches_sentry_nowhere(app, client, db, sample_user, eskiz_down, sentry_sent):
    """The account page asks for a code to a new number: POST /api/v1/auth/send-otp, sent in-process, and Eskiz is
    down. Every error that request reports (the task's own, and the auth service's "was not sent") carries no code."""
    token = create_access_token(identity=str(sample_user.id))

    response = client.post(
        "/api/v1/auth/send-otp",
        headers={**WEB_HEADERS, "Authorization": f"Bearer {token}"},
        data=json.dumps({"phone": NEW_PHONE}),
    )

    assert response.status_code == 500, response.get_json()
    # The code, read from the text Eskiz was handed; nothing of the test's holds it while the request runs
    [otp] = re.findall(r"\b\d{6}\b", eskiz_down.send_sms.call_args.kwargs["message"])
    events = sentry_sent.payloads("event")
    assert any(event.get("threads") or event.get("exception") for event in events), events
    assert_carries_none_of(events + sentry_sent.payloads("transaction"), otp)


# ------------------------------------------------------------------------- tokens inside Redis keys, in traces


def _issued_token(app, kind):
    keys = redis.from_url(app.config["REDIS_URL"]).keys(f"verification:{kind}:*")
    assert len(keys) == 1, keys
    return keys[0].decode().rsplit(":", 1)[1]


def _redis_spans(transaction):
    return [span for span in transaction.get("spans", []) if span.get("op") == "db.redis"]


def test_a_traced_password_reset_request_carries_no_live_token(app, client, db, sample_user, sentry_sent):
    """forgot-password.js: POST {identifier}. The token is stored as `verification:password_reset:<token>` (SETEX),
    so the trace's Redis span named it while the emailed link was still live."""
    response = client.post(
        "/api/v1/auth/forgot-password", headers=WEB_HEADERS, data=json.dumps({"identifier": sample_user.email})
    )
    assert response.status_code == 200, response.get_json()
    token = _issued_token(app, "password_reset")

    [transaction] = sentry_sent.payloads("transaction")
    assert_carries_none_of(transaction, token)
    # The span still says which key it touched
    [stored] = [span for span in _redis_spans(transaction) if span["description"].startswith("SETEX")]
    assert "verification:password_reset:" in stored["description"]


def test_a_traced_password_reset_carries_neither_token_nor_new_password(app, client, db, sample_user, sentry_sent):
    """reset-password.js: POST {token, new_password} from the emailed link. The token is read and deleted under its
    key; the new password is in the request body Sentry attaches to the trace."""
    client.post("/api/v1/auth/forgot-password", headers=WEB_HEADERS, data=json.dumps({"identifier": sample_user.email}))
    token = _issued_token(app, "password_reset")
    new_password = "Fresh-Pass-2026!"

    response = client.post(
        "/api/v1/auth/reset-password",
        headers=WEB_HEADERS,
        data=json.dumps({"token": token, "new_password": new_password}),
    )

    assert response.status_code == 200, response.get_json()
    transactions = sentry_sent.payloads("transaction")
    assert_carries_none_of(transactions, token, new_password)


def test_an_error_after_a_token_lookup_carries_no_token_in_its_breadcrumbs(app, db, sample_user, sentry_sent):
    """Each Redis command leaves a breadcrumb on the scope with its key, and the next error event carries it."""
    token = AuthService()._generate_verification_token(sample_user.id, "email")

    with sentry_sdk.isolation_scope():
        assert AuthService()._verify_verification_token(token, "email") == sample_user.id
        logger.error("Email verification could not be completed")

    [event] = sentry_sent.payloads("event")
    assert_carries_none_of(event, token)
    crumbs = [crumb for crumb in event["breadcrumbs"]["values"] if crumb.get("category") == "redis"]
    assert any("verification:email:" in crumb["message"] for crumb in crumbs), crumbs


# ------------------------------------------------------------------------------ tokens inside the emailed links


def test_traced_link_pages_carry_no_token(app, client, db, sentry_sent):
    """The emailed links open pages: /verify-email?token=<token> and /reset-password/<token>. Each page view is
    traced, its URL and query string on the trace."""
    verify_token, reset_token = secrets.token_urlsafe(24), secrets.token_urlsafe(24)

    for link in (f"/verify-email?token={verify_token}", f"/reset-password/{reset_token}"):
        assert client.get(link).status_code == 200, link

    transactions = sentry_sent.payloads("transaction")
    assert len(transactions) == 2
    assert_carries_none_of(transactions, verify_token, reset_token)
    assert [transaction["request"]["url"] for transaction in transactions] == [
        "http://localhost/verify-email",
        "http://localhost/reset-password/[REDACTED]",
    ]
    assert transactions[0]["request"]["query_string"] == "token=[REDACTED]"


def test_a_logged_error_carries_no_token_a_link_holds(app, sentry_sent):
    """A frame holds the link, never the token by name: the token goes by the link's shape, the rest of it stays."""
    links = [
        f"https://bluestream.uz/verify-email?lang=ru&token={secrets.token_urlsafe(24)}",
        f"https://bluestream.uz/reset-password/{secrets.token_urlsafe(24)}",
    ]

    logger.error("The letter could not be sent")

    [event] = sentry_sent.payloads("event")
    assert_carries_none_of(event, *(re.split(r"[=/]", link)[-1] for link in links))
    shown = _frame_of(event, "test_a_logged_error_carries_no_token_a_link_holds")["vars"]["links"]
    assert shown == [
        "'https://bluestream.uz/verify-email?lang=ru&token=[REDACTED]'",
        "'https://bluestream.uz/reset-password/[REDACTED]'",
    ]
