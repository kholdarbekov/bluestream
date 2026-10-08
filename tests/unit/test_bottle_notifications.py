"""Who gets which bottle push, with which payload, and the task that carries it."""
import inspect

import pytest
from celery.exceptions import Retry

from business_app.models.bottle import DriverBottleSession, DriverBottleTransfer
from business_app.services import bottle_notifications
from business_app.tasks import staff_tasks
from shared.enums import DriverBottleSessionStatus
from tests.unit.test_bottle_transfer_and_join_requests import _driver
from tests.unit.test_sales_agent_tasks import _spy

pytestmark = pytest.mark.unit


def _transfer(db, sender, receiver, qty=4):
    session = DriverBottleSession(driver_user_id=sender.id, bottles_loaded=10, status=DriverBottleSessionStatus.OPEN)
    db.session.add(session)
    db.session.flush()
    transfer = DriverBottleTransfer(
        sender_session_id=session.id, sender_driver_id=sender.id,
        receiver_driver_id=receiver.id, declared_quantity=qty,
    )
    db.session.add(transfer)
    db.session.commit()
    return transfer, session


def test_the_receiver_is_pushed_the_transfer_with_the_senders_name(db, monkeypatch):
    pushes = _spy(monkeypatch, staff_tasks.push_bottle_event, "delay")
    sender = _driver(db, "+998901300001", "Ali")
    receiver = _driver(db, "+998901300002", "Vali")
    transfer, _ = _transfer(db, sender, receiver)

    bottle_notifications.notify_transfer_received(transfer)

    assert pushes == [((int(receiver.telegram_id), "transfer_received",
                        {"id": transfer.id, "declared_quantity": 4, "sender_name": "Ali Driver"}), {})]


def test_the_owner_is_pushed_the_request_and_the_requester_the_answer(db, monkeypatch):
    pushes = _spy(monkeypatch, staff_tasks.push_bottle_event, "delay")
    owner = _driver(db, "+998901300011", "Ali")
    requester = _driver(db, "+998901300012", "Vali")
    _, session = _transfer(db, owner, requester)

    bottle_notifications.notify_join_requested(session, requester)
    bottle_notifications.notify_join_answered(session, requester, approved=True)
    bottle_notifications.notify_join_answered(session, requester, approved=False)

    assert pushes == [
        ((int(owner.telegram_id), "join_requested",
          {"session_id": session.id, "requester_id": requester.id, "requester_name": "Vali Driver"}), {}),
        ((int(requester.telegram_id), "join_approved", {"session_id": session.id, "owner_name": "Ali Driver"}), {}),
        ((int(requester.telegram_id), "join_declined", {"session_id": session.id, "owner_name": "Ali Driver"}), {}),
    ]


def test_a_recipient_without_a_telegram_chat_is_skipped(db, monkeypatch):
    pushes = _spy(monkeypatch, staff_tasks.push_bottle_event, "delay")
    sender = _driver(db, "+998901300021", "Ali")
    receiver = _driver(db, "+998901300022", "Vali")
    receiver.telegram_id = None
    db.session.commit()
    transfer, _ = _transfer(db, sender, receiver)

    bottle_notifications.notify_transfer_received(transfer)

    assert pushes == []


def test_a_broker_that_refuses_the_publish_never_raises_over_committed_work(db, monkeypatch):
    sender = _driver(db, "+998901300031", "Ali")
    receiver = _driver(db, "+998901300032", "Vali")
    transfer, _ = _transfer(db, sender, receiver)

    def refuse(*_args, **_kwargs):
        raise ConnectionError("broker down")

    monkeypatch.setattr(staff_tasks.push_bottle_event, "delay", refuse)
    bottle_notifications.notify_transfer_received(transfer)  # must not raise


def test_the_task_posts_to_the_bottle_route_keyed_on_its_own_id(monkeypatch):
    sent = _spy(monkeypatch, staff_tasks, "_send_staff_webhook", result=True)
    payload = {"id": 31, "declared_quantity": 4, "sender_name": "Ali Driver"}

    staff_tasks.push_bottle_event.push_request(id="task-abc")
    try:
        staff_tasks.push_bottle_event.run(777000111, "transfer_received", payload)
    finally:
        staff_tasks.push_bottle_event.pop_request()

    assert sent == [(("/internal/bottle-event", {
        "telegram_id": 777000111, "event": "transfer_received", "payload": payload,
        "event_id": "bottle-event:task-abc",
    }), {})]


def test_the_task_retries_a_failed_webhook(monkeypatch):
    _spy(monkeypatch, staff_tasks, "_send_staff_webhook", result=False)
    retries = []

    def fake_retry(*args, **kwargs):
        inspect.signature(staff_tasks.push_bottle_event.retry).bind(*args, **kwargs)
        retries.append(kwargs)
        raise Retry()

    monkeypatch.setattr(staff_tasks.push_bottle_event, "retry", fake_retry)
    with pytest.raises(Retry):
        staff_tasks.push_bottle_event.run(777000111, "join_requested", {"session_id": 1, "requester_id": 2})
    assert len(retries) == 1 and "join_requested" in str(retries[0]["exc"])
    assert staff_tasks.push_bottle_event.max_retries == 3
