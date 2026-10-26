"""Shared by the customer-notice tests (plan 2026-10-24 Task 3): the HTTP hop to the customer bot replaced by
a recorder, a timeline that proves a notice is published only after the commit that wrote its row, and the
two customers a notice can reach (Telegram, email).

A test module imports a fixture by its `*_fixture` name (as with tests/integration/reorder_reminder_builders.py).
"""

from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

from business_app.models.order import Order
from business_app.models.subscription import SubscriptionLog
from shared.constants import DISPLAY_TIMEZONE

TASHKENT = ZoneInfo(DISPLAY_TIMEZONE)
CHAT_ID = "810000001"
ROUTE = "/internal/customer-notice"
BOT_URL = "https://t.me/aqua_test_bot"

# What `trigger_bot_webhook` hands back (business_app/utils/bot_webhook.py): the bot's own 200 body, and its
# own shape for a non-200, where only the status and the message survive.
BOT_DELIVERED = {"success": True, "message": "Notification sent"}
BOT_BLOCKED = {"success": False, "message": "Bot webhook failed: Bot was blocked by the user", "status_code": 410}
BOT_DOWN = {"success": False, "message": "Bot webhook failed: Internal server error", "status_code": 500}

# The contract, one key set per kind; tests/telegram_bot/test_webhook_customer_notice.py pins the bot's side.
ENVELOPE = {"notice_id", "kind", "telegram_id", "language"}
CONTRACT_KEYS = {
    "subscription_order_created": ENVELOPE
    | {
        "order_id",
        "order_number",
        "delivery_date",
        "window_start",
        "window_end",
        "items",
        "total",
        "payment",
        "amount_due",
        "prepaid_applied",
        "deadline",
    },
    "subscription_delivery_skipped": ENVELOPE | {"subscription_name", "skipped_dates", "reason", "next_delivery_date"},
    "subscription_paid_order_kept": ENVELOPE
    | {"order_id", "order_number", "delivery_date", "trigger", "next_delivery_date"},
    "order_payment_expired": ENVELOPE | {"order_id", "order_number", "deadline"},
}


@pytest.fixture(name="timeline")
def timeline_fixture(db):
    """Every flush of a SubscriptionLog or Order and every commit, per session, in order. A publish spy appends
    `("published", id)` to it, so a test can prove the commit that wrote a row came before the task that
    sends its notice."""
    events = []

    def after_flush(session, _context):
        for obj in list(session.new) + list(session.dirty):
            if isinstance(obj, (SubscriptionLog, Order)):
                events.append(("flushed", type(obj).__name__, obj.id, id(session)))

    def after_commit(session):
        events.append(("commit", id(session)))

    event.listen(Session, "after_flush", after_flush)
    event.listen(Session, "after_commit", after_commit)
    try:
        yield events
    finally:
        event.remove(Session, "after_flush", after_flush)
        event.remove(Session, "after_commit", after_commit)


def assert_published_after_commit(events, kind: str, obj_id: int) -> None:
    """The last flush of the row before its publish, and the publish, have a commit of that session between them."""
    published = events.index(("published", obj_id))
    flushes = [i for i, entry in enumerate(events[:published]) if entry[:3] == ("flushed", kind, obj_id)]
    assert flushes, f"{kind} {obj_id} was never flushed before it was published"
    last_flush = flushes[-1]
    between = events[last_flush:published]
    assert ("commit", events[last_flush][3]) in between, f"{kind} {obj_id} was published before its commit"


@pytest.fixture(name="bot")
def bot_fixture(monkeypatch):
    """The HTTP hop to the customer bot, replaced by a recorder with `trigger_bot_webhook`'s own signature.
    Each POST is recorded as `{"endpoint", "payload", "request_id"}` and answered with `answer`."""
    from business_app.services import customer_notices

    state = SimpleNamespace(calls=[], answer=BOT_DELIVERED)

    def trigger(endpoint, payload=None, *, request_id=None):
        state.calls.append({"endpoint": endpoint, "payload": dict(payload or {}), "request_id": request_id})
        return state.answer

    monkeypatch.setattr(customer_notices, "trigger_bot_webhook", trigger)
    return state


@pytest.fixture(name="on_telegram")
def on_telegram_fixture(db, sample_user):
    """The customer bot is connected: Telegram is the one channel."""
    sample_user.telegram_id = CHAT_ID
    sample_user.is_bot_active = True
    sample_user.preferred_language = "en"
    db.session.commit()
    return sample_user


@pytest.fixture(name="on_email")
def on_email_fixture(db, sample_user):
    """No bot: email is the one channel (`test@example.com` is a deliverable address)."""
    sample_user.telegram_id = None
    sample_user.is_bot_active = False
    sample_user.preferred_language = "en"
    db.session.commit()
    return sample_user
