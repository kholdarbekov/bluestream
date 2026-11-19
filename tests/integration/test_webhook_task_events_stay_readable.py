"""A payment-webhook task's Sentry events hide what the gateway sent to prove itself, and nothing else of its payload.

`secret_arguments` hides a secret argument's values wherever an event raised while the task runs shows them. Of a dict
argument (a webhook's payload and request headers) those are the strings under a secret name (shared/secret_scrub.py):
Click's `sign_string`, Payme's `Authorization`. The rest of the payload is what the event is read for and stays: the
order number, Payme's method, the content type, the caller's address.

Each task is published through Celery's real `.delay` on the test app's hermetic broker (`published`) and run as the
worker runs it, with Sentry recording what it would send with production's options (`sentry_events`).
"""

import json
import secrets

import pytest

from business_app.tasks import payment_tasks
from tests.integration import test_task_secrets_kept_out_of_celery_logs as task_secrets_tests
from tests.integration.celery_task_harness import only_message_of

pytestmark = pytest.mark.integration

# What Sentry would send, with production's options (a fixture, shared)
sentry_events = task_secrets_tests.sentry_events

ORDER_NUMBER = "TG_000095_26"


def _click_payload(proof):
    return {"click_trans_id": "2345678", "merchant_trans_id": ORDER_NUMBER, "sign_string": proof}


def _run_click_webhook(published, payload):
    task = payment_tasks.process_payment_webhook
    task.delay(4242, payload)
    task_secrets_tests.run_as_the_worker(only_message_of(published, task))
    return task


def test_a_click_webhook_tasks_event_keeps_the_order_number(app, db, published, sentry_events):
    """Click's payload for a payment that is not there: the logged error hides the sign_string and shows the order."""
    proof = secrets.token_hex(16)

    task = _run_click_webhook(published, _click_payload(proof))

    assert proof not in json.dumps(sentry_events, default=str)
    [logged] = sentry_events
    assert logged["logentry"]["message"] == "Payment 4242 not found", logged
    job = logged["extra"]["celery-job"]
    assert job["task_name"] == task.name, job
    payload = job["args"][1]
    assert (payload["merchant_trans_id"], payload["click_trans_id"]) == (ORDER_NUMBER, "2345678"), payload
    assert payload["sign_string"] == "[REDACTED]", payload


def test_a_payme_webhook_tasks_event_keeps_the_method_the_content_type_and_the_address(
    app, db, published, sentry_events
):
    """A malformed Payme request (CheckTransaction with a list for params) fails inside the handler: the event hides
    the merchant key and shows what was asked, how and from where."""
    basic = "Basic " + secrets.token_urlsafe(30)
    metadata = {
        "provider": "payme",
        "webhook_data": {"id": 7, "method": "CheckTransaction", "params": ["not", "an", "object"]},
        "headers": {"Authorization": basic, "Content-Type": "application/json"},
        "remote_addr": "185.234.113.1",
    }
    task = payment_tasks.handle_payment_webhook
    task.delay(metadata, "payme")

    task_secrets_tests.run_as_the_worker(only_message_of(published, task))

    assert basic.split()[1] not in json.dumps(sentry_events, default=str)
    [failure] = [event for event in sentry_events if event.get("exception")]
    shown = failure["extra"]["celery-job"]["args"][0]
    # Sentry writes the strings this deep in an extra as their repr
    assert shown["webhook_data"]["method"].strip("'") == "CheckTransaction", shown
    assert shown["headers"]["Content-Type"].strip("'") == "application/json", shown
    assert shown["headers"]["Authorization"] == "[REDACTED]", shown
    assert shown["remote_addr"] == "185.234.113.1", shown


def test_a_payload_secret_of_plus_signs_leaves_the_event_readable(app, db, published, sentry_events):
    """The payload is whatever the caller sent. A `sign_string` of plus signs, with the plus taken off, is empty: an
    empty text "found" between every two characters of the event would bury it."""
    task = _run_click_webhook(published, _click_payload("+" * 12))

    [logged] = sentry_events
    assert logged["logentry"]["message"] == "Payment 4242 not found", logged
    assert logged["extra"]["celery-job"]["task_name"] == task.name, logged
