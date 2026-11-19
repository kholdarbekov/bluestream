"""The payment-webhook tasks never print what a gateway sent to prove itself.

`handle_payment_webhook(webhook_metadata, webhook_source)` carries the request's headers (Payme's `Authorization:
Basic <merchant key>`) with its payload, and `process_payment_webhook(payment_id, webhook_data)` carries Click's
payload with its `sign_string`. Nothing publishes either today, but Celery prints a task's arguments wherever it
reports one (the worker's "received" and failure lines from the message's `argsrepr`/`kwargsrepr`, Flower's task-sent
event), so both declare those parameters secret (`business_app/tasks/secret_arguments.py`) and any publish shows them
masked.

Each task is published through Celery's real `.delay` / `.apply_async` on the test app's hermetic broker (the conftest
otherwise turns them into no-ops; `published`, tests/integration/celery_task_harness.py, lifts that) and the message is
read as the worker would log it, or run as the worker would run it, with Sentry recording what it would send.
"""

import copy
import json

import pytest

from business_app.tasks import payment_tasks
from business_app.tasks.secret_arguments import HIDDEN
from tests.integration import test_task_secrets_kept_out_of_celery_logs as task_secrets_tests
from tests.integration.celery_task_harness import only_message_of

pytestmark = pytest.mark.integration

# What Sentry would send, with production's options (a fixture, shared)
sentry_events = task_secrets_tests.sentry_events

MERCHANT_KEY_HEADER = "Basic UGF5Y29tOnNlY3JldC1tZXJjaGFudC1rZXk="
CLICK_SIGN_STRING = "5d41402abc4b2a76b9719d911017c592"

PAYME_METADATA = {
    "provider": "payme",
    "webhook_data": {"id": 7, "method": "CheckPerformTransaction", "params": {"amount": 1500000}},
    "headers": {"Authorization": MERCHANT_KEY_HEADER, "Content-Type": "application/json"},
    "remote_addr": "185.234.113.1",
}
CLICK_DATA = {"click_trans_id": "2345678", "merchant_trans_id": "TG_000095_26", "sign_string": CLICK_SIGN_STRING}


@pytest.mark.parametrize("how", ["delay", "apply_async_kwargs"])
def test_a_published_payme_webhook_shows_no_merchant_key(published, how):
    task = payment_tasks.handle_payment_webhook
    if how == "delay":
        task.delay(PAYME_METADATA, "payme")
    else:
        task.apply_async(kwargs={"webhook_metadata": PAYME_METADATA, "webhook_source": "payme"})

    message = only_message_of(published, task)
    shown = message.shown
    assert MERCHANT_KEY_HEADER not in shown and "UGF5Y29t" not in shown, shown
    assert HIDDEN in shown and "'payme'" in shown, shown  # masked, and the provider still readable
    # The worker still gets the real arguments
    args, kwargs, _embed = message.body
    assert (list(args), kwargs) in (
        ([PAYME_METADATA, "payme"], {}),
        ([], {"webhook_metadata": PAYME_METADATA, "webhook_source": "payme"}),
    )


@pytest.mark.parametrize("how", ["delay", "apply_async_kwargs"])
def test_a_published_click_webhook_shows_no_sign_string(published, how):
    task = payment_tasks.process_payment_webhook
    if how == "delay":
        task.delay(4242, CLICK_DATA)
    else:
        task.apply_async(kwargs={"payment_id": 4242, "webhook_data": CLICK_DATA})

    message = only_message_of(published, task)
    shown = message.shown
    assert CLICK_SIGN_STRING not in shown, shown
    assert HIDDEN in shown and "4242" in shown, shown  # masked, and the payment still readable
    args, kwargs, _embed = message.body
    assert (list(args), kwargs) in (([4242, CLICK_DATA], {}), ([], {"payment_id": 4242, "webhook_data": CLICK_DATA}))


# ------------------------------------------------------------------------------------------------------- Sentry


@pytest.fixture
def events_as_the_task_leaves_them(monkeypatch):
    """Each event as Sentry's event processors hand it to `before_send`, the task's own scrubber
    (`business_app/tasks/secret_arguments.py`) the last of them. A secret argument's values must be gone there already,
    whatever name they sit under, before the by-name rule of `business_app/utils/sentry.py` runs; `sentry_events`
    still records what that rule then sends."""
    raw = []
    send = task_secrets_tests.production_before_send

    def _keep_and_send(event, hint):
        raw.append(copy.deepcopy(event))
        return send(event, hint)

    monkeypatch.setattr(task_secrets_tests, "production_before_send", _keep_and_send)
    return raw


def test_sentry_gets_no_click_sign_string_from_the_webhook_task(
    app, db, published, sentry_events, events_as_the_task_leaves_them
):
    """Click's payload arrives for a payment that is not there: the error the task logs reaches Sentry without the
    payload's sign_string, the task and the payment still readable."""
    task = payment_tasks.process_payment_webhook
    task.delay(4242, CLICK_DATA)

    task_secrets_tests.run_as_the_worker(only_message_of(published, task))

    for events in (events_as_the_task_leaves_them, sentry_events):
        reported = json.dumps(events, default=str)
        assert CLICK_SIGN_STRING not in reported, reported
    [logged] = sentry_events
    assert logged["logentry"]["message"] == "Payment 4242 not found", logged
    job = logged["extra"]["celery-job"]
    assert job["task_name"] == task.name and job["args"][0] == 4242, job


def test_sentry_gets_no_merchant_key_from_a_payme_webhook_task_that_fails(
    app, db, published, sentry_events, events_as_the_task_leaves_them
):
    """A malformed Payme request (CheckTransaction with a list for params) fails inside the handler: the error reaches
    Sentry with neither the merchant key nor its base64, while the task and the provider, a short word of the payload
    too, stay readable."""
    task = payment_tasks.handle_payment_webhook
    request = {"id": 7, "method": "CheckTransaction", "params": ["not", "an", "object"]}
    task.delay({**PAYME_METADATA, "webhook_data": request}, "payme")

    task_secrets_tests.run_as_the_worker(only_message_of(published, task))

    for events in (events_as_the_task_leaves_them, sentry_events):
        reported = json.dumps(events, default=str)
        assert MERCHANT_KEY_HEADER not in reported and "UGF5Y29t" not in reported, reported
    [failure] = [event for event in sentry_events if event.get("exception")]
    assert failure["logentry"]["message"] == "Payme webhook error", failure
    assert failure["exception"]["values"][-1]["type"] == "AttributeError", failure
    job = failure["extra"]["celery-job"]
    assert job["task_name"] == task.name and job["args"][1] == "payme", job
