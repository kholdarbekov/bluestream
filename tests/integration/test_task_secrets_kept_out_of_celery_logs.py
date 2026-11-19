"""A task's secret arguments never reach Celery's logs, Flower or Sentry.

Celery prints a task's arguments wherever it reports the task: the worker's "Task ...[id] received" line and its
failure line (the message's `argsrepr`/`kwargsrepr` headers), the `task-sent` event Flower keeps (built from the same
`argsrepr` before any publish signal runs), and Sentry's Celery integration attaches them raw to every event raised
while the task runs. The "succeeded" line prints what the task returned. That put live password-reset and
email-verification links (enough to take over the account) and sign-up codes with full numbers in Loki.

Every flow starts at the endpoint the page calls, with Celery's real publish on its hermetic memory:// broker (the
conftest otherwise turns `.delay` into a no-op), and a message is then executed by Celery's own tracer, as the worker
that took it would. Only the providers (Brevo, Eskiz) are faked.
"""

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import redis
import sentry_sdk
from celery import current_app as current_celery_app
from celery.app import trace as celery_trace
from flask_jwt_extended import create_access_token, get_csrf_token
from sentry_sdk.integrations.celery import CeleryIntegration
from sentry_sdk.integrations.logging import LoggingIntegration

from business_app.services import notification_service as ns_module
from business_app.services.notification_service import NotificationService
from business_app.tasks import notification_tasks
from business_app.utils.sentry import before_send as production_before_send
from tests.integration.celery_task_harness import only_message_of

pytestmark = pytest.mark.integration

# conftest's `block_external_side_effects` swaps this for a stub; this is the real one
_REAL_SEND_SMS_TO_PHONE = NotificationService.send_sms_to_phone

PHONE = "+998901112288"
MASKED_PHONE = "+998***2288"
WEB_HEADERS = {"Content-Type": "application/json", "X-Platform": "web"}


def assert_shows_none_of(message, *secrets):
    for secret in secrets:
        assert secret not in message.shown, message.shown


def run_as_the_worker(message, retries=None):
    """Execute a published message with Celery's own tracer, as the worker that took it does: the request is the
    message's headers, so the tracer's log lines read the same `argsrepr`. Returns the tracer's result, whose `retstr`
    is the return value exactly as the "succeeded" line prints it."""
    args, kwargs, embed = message.body
    task = current_celery_app.tasks[message.name]
    request = {**message.headers, **embed, "delivery_info": {"exchange": "", "routing_key": "celery"}}
    if retries is not None:
        request["retries"] = retries
    trace = celery_trace.build_tracer(task.name, task, app=task.app, eager=False)
    return trace(request["id"], list(args), dict(kwargs), request)


def _phone_hash(phone):
    return hashlib.sha256(phone.encode()).hexdigest()[:16]


def _issued_token(app, kind):
    """The one verification token the auth service stored (`verification:<kind>:<token>`)."""
    keys = redis.from_url(app.config["REDIS_URL"]).keys(f"verification:{kind}:*")
    assert len(keys) == 1, keys
    return keys[0].decode().rsplit(":", 1)[1]


def ask_for_a_password_reset(app, client, user):
    """forgot-password.js: POST {identifier} to /api/v1/auth/forgot-password. Returns the emailed token."""
    response = client.post(
        "/api/v1/auth/forgot-password", headers=WEB_HEADERS, data=json.dumps({"identifier": user.email})
    )
    assert response.status_code == 200, response.get_json()
    return _issued_token(app, "password_reset")


def ask_for_a_new_verification_letter(app, user):
    """verify-email.js, signed in: POST /api/v1/auth/resend-email-verification with the session cookie and CSRF
    header the storefront login gave the browser. Returns the emailed token."""
    access = create_access_token(identity=str(user.id))
    browser = app.test_client()
    csrf = get_csrf_token(access)
    browser.set_cookie("access_token_cookie", access)
    browser.set_cookie("csrf_access_token", csrf)
    response = browser.post(
        "/api/v1/auth/resend-email-verification", headers={**WEB_HEADERS, "X-CSRF-TOKEN": csrf}, data="{}"
    )
    assert response.status_code == 200, response.get_json()
    return _issued_token(app, "email")


def sign_up_by_phone(app, client):
    """register.js: POST /api/v1/auth/phone/register/init. Returns the code it sent."""
    response = client.post("/api/v1/auth/phone/register/init", json={"phone": PHONE, "preferred_language": "ru"})
    assert response.status_code == 200, response.get_json()
    return redis.from_url(app.config["REDIS_URL"]).get(f"phone_reg_otp:{_phone_hash(PHONE)}").decode()


@pytest.fixture
def fake_eskiz(app, monkeypatch):
    """The real SMS send path down to a fake Eskiz client, which accepts every message."""
    eskiz = MagicMock()
    eskiz.send_sms.return_value = SimpleNamespace(status="waiting", id="msg-1")
    monkeypatch.setattr(NotificationService, "send_sms_to_phone", _REAL_SEND_SMS_TO_PHONE)
    monkeypatch.setattr(ns_module, "EskizSMS", lambda *a, **k: eskiz)
    monkeypatch.setitem(app.config, "ESKIZ_EMAIL", "sender@example.com")
    monkeypatch.setitem(app.config, "ESKIZ_PASSWORD", "test-password")
    return eskiz


def _providers_down(monkeypatch):
    """Brevo and Eskiz do not answer: the send raises inside the task, which then retries."""

    def _no_answer(*_args, **_kwargs):
        raise ConnectionError("the provider did not answer")

    monkeypatch.setattr(NotificationService, "send_notification", _no_answer)
    monkeypatch.setattr(NotificationService, "send_sms_to_phone", _no_answer)


# -------------------------------------------------------------------------------------------- what is published


def test_a_password_reset_message_shows_no_link_token(app, client, db, sample_user, published):
    token = ask_for_a_password_reset(app, client, sample_user)

    message = only_message_of(published, notification_tasks.send_password_reset_email_task)
    assert_shows_none_of(message, token)
    assert str(sample_user.id) in message.headers["argsrepr"]
    assert message.sent_event["args"] == message.headers["argsrepr"]
    # The worker still gets the real token to put in the letter
    args, kwargs, _embed = message.body
    assert list(args) == [sample_user.id, token] and kwargs == {}


def test_an_email_verification_message_shows_no_token(app, db, sample_user, published):
    token = ask_for_a_new_verification_letter(app, sample_user)

    message = only_message_of(published, notification_tasks.send_verification_email_task)
    assert_shows_none_of(message, token)
    assert str(sample_user.id) in message.headers["argsrepr"]
    args, kwargs, _embed = message.body
    assert list(args) == [sample_user.id, token] and kwargs == {}


@pytest.mark.parametrize("flow", ["password_reset", "sign_up_code"])
def test_a_retry_the_worker_sends_shows_no_secret_either(app, client, db, sample_user, published, monkeypatch, flow):
    """The worker re-sends a failed send itself (`self.retry`), and that copy is logged and evented like the first."""
    if flow == "password_reset":
        task = notification_tasks.send_password_reset_email_task
        secrets = [ask_for_a_password_reset(app, client, sample_user)]
    else:
        task = notification_tasks.send_registration_otp_task
        secrets = [sign_up_by_phone(app, client), PHONE, PHONE.lstrip("+")]
    first = only_message_of(published, task)
    published.clear()
    _providers_down(monkeypatch)

    run_as_the_worker(first)

    retried = only_message_of(published, task)
    assert retried.headers["retries"] == 1
    assert_shows_none_of(retried, *secrets)
    assert list(retried.body[0]) == list(first.body[0])  # the retry still carries the real arguments


def test_the_succeeded_line_of_a_sent_sign_up_code_shows_the_number_masked(app, client, db, published, fake_eskiz):
    code = sign_up_by_phone(app, client)

    result = run_as_the_worker(only_message_of(published, notification_tasks.send_registration_otp_task))

    assert fake_eskiz.send_sms.call_args.kwargs["mobile_phone"] == PHONE.lstrip("+")  # sent to the real number
    succeeded = result.retstr
    assert "'success': True" in succeeded, succeeded
    assert code not in succeeded and PHONE.lstrip("+") not in succeeded, succeeded
    assert MASKED_PHONE in succeeded, succeeded


# ------------------------------------------------------------------------------------------------------- Sentry


@pytest.fixture
def sentry_events():
    """What Sentry would send from this process, with production's options: the Celery and logging integrations,
    a stack on every error event, frame locals, and business_app.utils.sentry.before_send. Recorded, never sent (the
    conftest leaves every client without a DSN)."""
    scope = sentry_sdk.get_global_scope()
    previous = scope.client
    events = []

    def _record(event, hint):
        event = production_before_send(event, hint)
        if event is not None:
            events.append(event)
        return None

    sentry_sdk.init(
        integrations=[LoggingIntegration(), CeleryIntegration()],
        default_integrations=False,
        auto_enabling_integrations=False,
        attach_stacktrace=True,
        send_default_pii=False,
        before_send=_record,
    )
    yield events
    built = scope.client
    scope.set_client(previous)
    built.close()


@pytest.mark.parametrize("flow", ["password_reset", "sign_up_code"])
def test_sentry_gets_no_secret_from_a_task_that_fails(
    app, client, db, sample_user, published, monkeypatch, sentry_events, flow
):
    """The last retry fails: the error reaches Sentry with the task's arguments masked, and nothing else in the event
    (frame locals, the logged error, the letter's link in the task's template data) carries a secret."""
    if flow == "password_reset":
        task = notification_tasks.send_password_reset_email_task
        secrets = [ask_for_a_password_reset(app, client, sample_user)]
        shown = [str(sample_user.id)]
    else:
        task = notification_tasks.send_registration_otp_task
        secrets = [sign_up_by_phone(app, client), PHONE, PHONE.lstrip("+")]
        shown = [MASKED_PHONE, '"ru"']
    _providers_down(monkeypatch)

    run_as_the_worker(only_message_of(published, task), retries=task.max_retries)

    reported = json.dumps(sentry_events, default=str)
    for secret in secrets:
        assert secret not in reported, reported
    [failure] = [event for event in sentry_events if event.get("exception")]
    assert failure["exception"]["values"][-1]["type"] == "ConnectionError"
    job = failure["extra"]["celery-job"]
    assert job["task_name"] == task.name
    for value in shown:
        assert value in json.dumps(job), job
