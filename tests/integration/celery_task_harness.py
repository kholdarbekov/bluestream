"""Running and publishing Celery tasks for real in a test.

conftest's `block_external_side_effects` turns every task's `.delay`/`.apply_async` into a no-op. `patch_task` lifts
that for one task, and `publish_for_real` (the `published` fixture in tests/integration/conftest.py) lets every task
publish through Celery's real path onto its hermetic memory:// broker and records each message.
"""

from contextlib import contextmanager
from dataclasses import dataclass
from types import SimpleNamespace

from celery.app.task import Task
from celery.events.dispatcher import EventDispatcher
from celery.signals import after_task_publish

from business_app.tasks import notification_tasks, payment_tasks

# conftest's `block_external_side_effects` swaps these for stubs; these are the real ones
_REAL_TASK_DELAY = Task.delay
_REAL_TASK_APPLY_ASYNC = Task.apply_async

# The tasks whose arguments are secret (`business_app/tasks/secret_arguments.py`), which the tests publish for real
SECRET_TASKS = (
    notification_tasks.send_registration_otp_task,
    notification_tasks.send_verification_sms_task,
    notification_tasks.send_verification_email_task,
    notification_tasks.send_password_reset_email_task,
    payment_tasks.handle_payment_webhook,
    payment_tasks.process_payment_webhook,
)


def patch_task(monkeypatch, task, name, value):
    """`task.<name>` (`task` being the Celery Proxy a tasks module exports) is `value` for this test only, and nothing
    is left behind.

    Set on the task's own class (Celery builds one per task), which monkeypatch restores by deleting what it added.
    Set on the task, or on the Proxy that forwards the write to it, monkeypatch would put back the bound method it read
    first, as an attribute of the task itself that shadows `Task.<name>` for every later test in the xdist worker.
    """
    monkeypatch.setattr(type(task._get_current_object()), name, staticmethod(value))


@dataclass
class Message:
    name: str
    headers: dict
    body: tuple
    sent_event: dict

    @property
    def shown(self):
        """Everything Celery prints of the arguments: the headers the worker logs and the task-sent event."""
        return " ".join(
            (self.headers["argsrepr"], self.headers["kwargsrepr"], self.sent_event["args"], self.sent_event["kwargs"])
        )


@contextmanager
def publish_for_real(monkeypatch):
    """Every task message published inside the block, with the task-sent event Celery emitted for it.

    The business app sends a task-sent event with every message (`task_send_sent_event` in celery_app.py), as do the
    worker's retries; the default app the test process publishes through does not, so it is switched on here.
    """
    monkeypatch.setattr(Task, "apply_async", _REAL_TASK_APPLY_ASYNC)
    monkeypatch.setattr(Task, "delay", _REAL_TASK_DELAY)
    for proxy in SECRET_TASKS:
        task = proxy._get_current_object()
        # A test elsewhere that patched `delay` on the task itself may have left it there, shadowing the real one
        # set on Task above: set aside for the block, and put back as found
        monkeypatch.delitem(vars(task), "delay", raising=False)
        monkeypatch.setitem(task.app.conf, "task_send_sent_event", True)

    records, sent_events = [], []

    def _record(sender=None, headers=None, body=None, **_kwargs):
        records.append((sender, dict(headers), body))

    real_publish = EventDispatcher.publish

    def _publish_event(self, type, fields, *args, **kwargs):
        sent_events.append((type, dict(fields)))
        return real_publish(self, type, fields, *args, **kwargs)

    monkeypatch.setattr(EventDispatcher, "publish", _publish_event)
    after_task_publish.connect(_record, weak=False)

    def messages():
        sent = {fields["uuid"]: fields for type, fields in sent_events if type == "task-sent"}
        return [Message(name, headers, body, sent[headers["id"]]) for name, headers, body in records]

    try:
        yield SimpleNamespace(messages=messages, clear=lambda: (records.clear(), sent_events.clear()))
    finally:
        after_task_publish.disconnect(_record)


def messages_of(published, task):
    return [message for message in published.messages() if message.name == task.name]


def only_message_of(published, task):
    found = messages_of(published, task)
    assert len(found) == 1, published.messages()
    return found[0]
