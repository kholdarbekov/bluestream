"""A web sign-up's SMS code never reaches Celery's logs or Flower.

The worker logs every message it takes ("Task ...send_registration_otp_task[...] received") with the message's
`argsrepr`, and again when the task fails; Flower keeps the same text from the task-sent event. That put live sign-up
codes and full phone numbers in Loki (2026-11-15). The message now shows the code hidden and the number masked; the
task still receives the real ones.

Driven through the endpoints register.js calls, with Celery's real publish on its hermetic memory:// broker (the
conftest otherwise turns `.delay` into a no-op): what is asserted is the message a worker would take and the event
Flower would store. The capture is shared with the other secret-carrying tasks
(test_task_secrets_kept_out_of_celery_logs.py) through the `published` fixture in tests/integration/conftest.py.
"""

import hashlib

import pytest
import redis

from business_app.tasks import notification_tasks
from business_app.utils.password_security import hash_password
from business_app.models.user import User
from shared.enums import UserStatus
from tests.integration.celery_task_harness import only_message_of

PHONE = "+998901112288"
MASKED_PHONE = "+998***2288"


def _phone_hash(phone):
    return hashlib.sha256(phone.encode()).hexdigest()[:16]


def _assert_logged_without_code_or_number(message, code):
    logged = message.shown
    assert code not in logged, logged
    assert PHONE not in logged and PHONE.lstrip("+") not in logged, logged
    assert MASKED_PHONE in message.headers["argsrepr"] + message.headers["kwargsrepr"], logged
    assert (message.sent_event["args"], message.sent_event["kwargs"]) == (
        message.headers["argsrepr"],
        message.headers["kwargsrepr"],
    )


@pytest.mark.integration
@pytest.mark.parametrize("step", ["init", "resend"])
def test_the_signup_code_message_logs_no_code_and_a_masked_number(client, app, db, published, step):
    store = redis.from_url(app.config["REDIS_URL"])
    resp = client.post("/api/v1/auth/phone/register/init", json={"phone": PHONE, "preferred_language": "ru"})
    if step == "resend":
        assert resp.status_code == 200, resp.get_json()
        store.delete(f"phone_otp_cooldown:{_phone_hash(PHONE)}")
        published.clear()
        resp = client.post("/api/v1/auth/phone/resend-otp", json={"phone": PHONE})

    assert resp.status_code == 200, resp.get_json()
    code = store.get(f"phone_reg_otp:{_phone_hash(PHONE)}").decode()
    message = only_message_of(published, notification_tasks.send_registration_otp_task)
    _assert_logged_without_code_or_number(message, code)
    assert "'ru'" in message.headers["argsrepr"]
    # Still sent through Celery, and the worker gets the real number and code to send
    args, kwargs, _embed = message.body
    assert list(args) == [PHONE, code, "ru"] and kwargs == {}


@pytest.mark.integration
@pytest.mark.parametrize("as_keyword", [False, True], ids=["positional", "keyword"])
def test_a_verification_code_message_logs_no_code_and_a_masked_number(db, published, as_keyword):
    """send_verification_sms_task carries a code too; it runs in-process today, and would log none if queued."""
    user = User(email="otp-log@example.com", password_hash=hash_password("TestPassword123!"), status=UserStatus.ACTIVE)
    db.session.add(user)
    db.session.commit()
    task = notification_tasks.send_verification_sms_task

    if as_keyword:
        task.delay(user.id, otp_code="482913", phone_number=PHONE)
    else:
        task.delay(user.id, "482913", PHONE)

    message = only_message_of(published, task)
    _assert_logged_without_code_or_number(message, "482913")
    assert str(user.id) in message.headers["argsrepr"]
