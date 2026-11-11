"""The customer is told about a prepayment return (spec 2026-11-10 P8, Task 10).

Drives the admin "Record collection" and payout apply/undo routes the screen calls; the Celery publish and the
bot's HTTP hop are the only fakes. The notice is the customer_notices pattern: Telegram through the bot route,
a blocked bot falls back to email, never SMS, one message per payout.
"""

import logging
from contextlib import contextmanager

import pytest

from business_app.models.payment import CustomerCreditPayout
from business_app.services import customer_notices, prepayment_payout_service
from business_app.tasks import notification_tasks
from business_app.utils.transactions import atomic_transaction as real_atomic_transaction
from tests.integration.customer_notice_builders import (  # noqa: F401  (fixtures by their *_fixture name)
    BOT_BLOCKED,
    BOT_DOWN,
    BOT_URL,
    CHAT_ID,
    CONTRACT_KEYS,
    ROUTE,
    bot_fixture,
    on_email_fixture,
    on_telegram_fixture,
)
from tests.integration._prepayment_payout_helpers import APPLY, UNDO, credit, return_office, unapplied
from tests.notification_send_fakes import BREVO_URL, live_send_path

pytestmark = pytest.mark.integration


@pytest.fixture(name="enqueued")
def enqueued_fixture(monkeypatch):
    """`send_prepayment_returned_notice_task.delay`, recorded with whether the payout transaction had committed."""
    state = {"committed": False, "calls": []}

    @contextmanager
    def tracked():
        state["committed"] = False
        with real_atomic_transaction():
            yield
        state["committed"] = True

    monkeypatch.setattr(prepayment_payout_service, "atomic_transaction", tracked)

    def delay(payout_id):
        state["calls"].append((payout_id, state["committed"]))

    monkeypatch.setattr(notification_tasks.send_prepayment_returned_notice_task, "delay", delay)
    return state


def _apply(client, headers, customer_id, key, **body):
    payload = {"paid_by": "office", "reason": "Customer moved away", "idempotency_key": key, **body}
    return client.post(APPLY.format(customer_id), json=payload, headers=headers)


def _returned(client, db, headers, customer_id, prepaid=50000, amount=35000):
    credit(client, headers, customer_id, prepaid)
    return return_office(client, headers, customer_id, key=f"n-{prepaid}-{amount}", amount=amount)


# --------------------------------------------------------------------------- the enqueue


def test_apply_enqueues_one_notice_after_commit(client, db, admin_claim_headers, sample_user, enqueued):
    credit(client, admin_claim_headers, sample_user.id, 50000)

    response = _apply(client, admin_claim_headers, sample_user.id, "k-1", amount=35000)

    assert response.status_code == 201, response.get_json()
    payout_id = response.get_json()["data"]["payout"]["id"]
    assert enqueued["calls"] == [(payout_id, True)]

    replay = _apply(client, admin_claim_headers, sample_user.id, "k-1", amount=35000)
    assert replay.status_code == 200
    blocked = _apply(client, admin_claim_headers, sample_user.id, "k-2", amount=999999)
    assert blocked.status_code == 400
    status = client.post(UNDO.format(payout_id), json={"reason": "Recorded by mistake"}, headers=admin_claim_headers)
    assert status.status_code == 200
    assert enqueued["calls"] == [(payout_id, True)]  # no replay, blocked apply or undo added one


def test_rollback_enqueues_nothing(client, db, admin_claim_headers, sample_user, enqueued, monkeypatch):
    credit(client, admin_claim_headers, sample_user.id, 50000)

    def boom(self, *args, **kwargs):
        raise RuntimeError("write step failed")

    monkeypatch.setattr(prepayment_payout_service.PrepaymentPayoutService, "_write", boom)

    response = _apply(client, admin_claim_headers, sample_user.id, "k-boom", amount=35000)

    assert response.status_code >= 500
    assert enqueued["calls"] == []
    assert CustomerCreditPayout.query.count() == 0


class _Records(logging.Handler):
    """The records a logger emitted (the app's logging setup keeps its loggers from propagating to caplog)."""

    def __init__(self):
        super().__init__(level=logging.ERROR)
        self.records = []

    def emit(self, record):
        self.records.append(record)


def test_a_broker_failure_after_the_commit_keeps_the_committed_return(
    client, db, admin_claim_headers, sample_user, monkeypatch
):
    """The notice is best effort: the return stands, the admin gets the 201 with the payout, the failure is logged."""
    event_id = credit(client, admin_claim_headers, sample_user.id, 50000)

    def broker_down(payout_id):
        raise ConnectionError("broker unreachable")

    monkeypatch.setattr(notification_tasks.send_prepayment_returned_notice_task, "delay", broker_down)

    errors = _Records()
    prepayment_payout_service.logger.addHandler(errors)
    try:
        response = _apply(client, admin_claim_headers, sample_user.id, "k-broker", amount=35000)
    finally:
        prepayment_payout_service.logger.removeHandler(errors)

    assert response.status_code == 201, response.get_json()
    payout_id = response.get_json()["data"]["payout"]["id"]
    db.session.expire_all()
    payout = db.session.get(CustomerCreditPayout, payout_id)
    assert (payout.voided_at, payout.idempotency_key) == (None, "k-broker")
    assert unapplied(db, event_id) == 15000
    [record] = errors.records
    assert record.getMessage() == f"Failed to publish the prepayment-returned notice for payout {payout_id}"
    assert isinstance(record.exc_info[1], ConnectionError)


# --------------------------------------------------------------------------- the send


def test_telegram_payload_is_exact(client, db, admin_claim_headers, on_telegram, bot):
    on_telegram.preferred_language = "uz"
    db.session.commit()
    payout_id = _returned(client, db, admin_claim_headers, on_telegram.id)

    assert notification_tasks.send_prepayment_returned_notice_task(payout_id) == "telegram"

    assert bot.calls == [
        {
            "endpoint": ROUTE,
            "request_id": f"prepayment_returned:{payout_id}",
            "payload": {
                "notice_id": f"prepayment_returned:{payout_id}",
                "kind": "prepayment_returned",
                "telegram_id": CHAT_ID,
                "language": "uz",
                "payout_id": payout_id,
                "amount": 35000.0,
                "balance": 15000.0,
            },
        }
    ]
    assert set(bot.calls[0]["payload"]) == CONTRACT_KEYS["prepayment_returned"]


def test_blocked_bot_falls_back_to_email(app, monkeypatch, client, db, admin_claim_headers, on_telegram, bot):
    payout_id = _returned(client, db, admin_claim_headers, on_telegram.id)
    bot.answer = BOT_BLOCKED

    with live_send_path(app, monkeypatch, bot_url=BOT_URL) as post:
        assert notification_tasks.send_prepayment_returned_notice_task(payout_id) == "email"

    [call] = post.call_args_list
    assert call.args[0] == BREVO_URL
    body = call.kwargs["json"]
    assert body["subject"] == "Prepaid balance returned - Aqua Element"
    assert "35,000" in body["htmlContent"] and "15,000" in body["htmlContent"]


def test_email_prints_a_zero_balance(app, monkeypatch, client, db, admin_claim_headers, on_email, bot):
    payout_id = _returned(client, db, admin_claim_headers, on_email.id, prepaid=35000, amount=35000)

    with live_send_path(app, monkeypatch, bot_url=BOT_URL) as post:
        assert notification_tasks.send_prepayment_returned_notice_task(payout_id) == "email"

    assert bot.calls == []
    html = post.call_args_list[0].kwargs["json"]["htmlContent"]
    assert "35,000" in html and "Prepaid balance now: <strong>0</strong>" in html


def test_a_bot_outage_retries_the_task_and_emails_nothing(
    app, monkeypatch, client, db, admin_claim_headers, on_telegram, bot
):
    """Only a bot failure retries (the bot's dedup on notice_id keeps it one message); email would double-send."""
    payout_id = _returned(client, db, admin_claim_headers, on_telegram.id)
    bot.answer = BOT_DOWN
    task = notification_tasks.send_prepayment_returned_notice_task
    retried = []

    class Retried(Exception):
        pass

    def retry(exc=None, **options):
        retried.append(exc)
        raise Retried()

    monkeypatch.setattr(task, "retry", retry)

    with live_send_path(app, monkeypatch, bot_url=BOT_URL) as post, pytest.raises(Retried):
        task(payout_id)

    post.assert_not_called()
    assert [type(exc) for exc in retried] == [customer_notices.RetryableNoticeError]
    assert [call["endpoint"] for call in bot.calls] == [ROUTE]


def test_no_channel_sends_nothing(client, db, admin_claim_headers, sample_user, bot, monkeypatch):
    payout_id = _returned(client, db, admin_claim_headers, sample_user.id)
    sample_user.telegram_id = None
    sample_user.is_bot_active = False
    sample_user.email = f"telegram_{sample_user.id}@bluestream.local"
    db.session.commit()

    assert notification_tasks.send_prepayment_returned_notice_task(payout_id) == "no_channel"
    assert bot.calls == []


def test_undone_before_send_is_skipped(client, db, admin_claim_headers, on_telegram, bot):
    payout_id = _returned(client, db, admin_claim_headers, on_telegram.id)
    undone = client.post(UNDO.format(payout_id), json={"reason": "Recorded by mistake"}, headers=admin_claim_headers)
    assert undone.status_code == 200

    assert customer_notices.send_prepayment_returned(payout_id) == "skipped:payout_undone"
    assert bot.calls == []


def test_a_missing_payout_is_skipped(db, bot):
    assert customer_notices.send_prepayment_returned(987654) == "skipped:payout_missing"
    assert bot.calls == []
