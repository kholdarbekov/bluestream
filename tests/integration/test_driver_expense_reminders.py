"""The cash-session reminder while a driver's expense claim waits (spec 2026-11-17).

The sweep marks a due session OVERDUE as before, but a driver whose whole balance waits for the office has nothing
to hand in, so he is not nagged: the skip is `awaits_office_only`, the same answer the bot's
`await_expense_decision` comes from. When something is left the reminder names "Hand in now" beside what waits.
The sessions are collected through the admin route, the claims inserted as setup, the session back-dated the way
`tests/unit/test_payout_session_overdue_tasks.py` does it, and the task run as Celery runs it (`.run()`). Only the
outbound notification is faked, with a recorder bound to the real signatures that keeps the Telegram text.
"""

import importlib.util
import inspect
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from business_app.models.payment import DriverCashSession
from business_app.models.translation import Translation
from business_app.services.notification_service import NotificationService
from business_app.tasks import payment_tasks
from shared.enums import DriverCashSessionStatus
from tests.integration._driver_expense_helpers import insert_expense
from tests.integration._prepayment_payout_helpers import collect_on_trip, session_detail, stored

pytestmark = pytest.mark.integration

SEED_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "seed_staff_translations.py"
WAITING_KEY = "staff.notification.reconciliation_reminder_overdue_expenses_waiting"
TODAYS_KEY = "staff.notification.reconciliation_reminder_overdue"
SUBJECT_KEY = "staff.notification.subject.driver_cash_reconciliation"


class _Sent:
    """What the sweep sent: each in-app notification and each Telegram message."""

    def __init__(self):
        self.in_app = []
        self.telegram = []


@pytest.fixture
def notified(monkeypatch):
    """The in-app `template_data` and the Telegram `{"user_id", "body"}` of every send, bound to the real signatures."""
    sent = _Sent()
    real_send = NotificationService.send_notification
    real_telegram = NotificationService.send_staff_telegram_message

    def send_notification(self, *args, **kwargs):
        bound = inspect.signature(real_send).bind(self, *args, **kwargs)
        template = bound.arguments["template_override"]
        sent.in_app.append(
            {
                "user_id": bound.arguments["user_id"],
                "template_data": bound.arguments.get("template_data"),
                "body": template.get_translated("content", "en"),
            }
        )
        return {"success": True}

    def send_staff_telegram_message(self, *args, **kwargs):
        bound = inspect.signature(real_telegram).bind(self, *args, **kwargs)
        sent.telegram.append({"user_id": bound.arguments["user"].id, "body": bound.arguments["message"]})
        return {"success": True}

    monkeypatch.setattr(NotificationService, "send_notification", send_notification)
    monkeypatch.setattr(NotificationService, "send_staff_telegram_message", send_staff_telegram_message)
    return sent


@pytest.fixture
def shipped_copy(db):
    """The reminder copy the seed ships (`en`, category `staff_bot`; the reminder keys sit in `EXTRA_TRANSLATIONS`): the text the driver reads comes from it."""
    spec = importlib.util.spec_from_file_location("seed_staff_translations", SEED_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for key in (WAITING_KEY, TODAYS_KEY, SUBJECT_KEY):
        db.session.add(
            Translation(
                key=key, language="en", value=module.EXTRA_TRANSLATIONS[key]["en"], category="staff_bot", is_active=True
            )
        )
    db.session.commit()


@pytest.fixture
def driver(db, second_delivery_driver):
    second_delivery_driver.telegram_id = "104933915"
    second_delivery_driver.preferred_language = "en"
    db.session.commit()
    return second_delivery_driver


def _backdate(db, session_id):
    """Eight days old and past its warning: what the overdue sweep looks for."""
    session = db.session.get(DriverCashSession, session_id)
    session.session_started_at = datetime.now(UTC) - timedelta(days=8)
    session.warning_due_at = datetime.now(UTC) - timedelta(minutes=5)
    session.submission_due_at = session.warning_due_at
    session.last_cash_activity_at = datetime.now(UTC) - timedelta(days=8)
    db.session.commit()


def test_no_reminder_while_everything_he_holds_waits_for_the_office(
    app, client, db, admin_claim_headers, sample_user, driver, notified, shipped_copy
):
    sid = collect_on_trip(client, admin_claim_headers, sample_user.id, driver.id, 150000)
    insert_expense(db, sid, driver.id, 150000)
    _backdate(db, sid)

    reminded = payment_tasks.send_cod_reconciliation_reminders.run()

    assert (reminded["sent"], reminded["failed"]) == (0, 0)
    assert notified.telegram == []
    assert [n for n in notified.in_app if n["user_id"] == driver.id] == []
    # Still overdue for the managers; only the driver is spared.
    assert stored(db, sid).status == DriverCashSessionStatus.OVERDUE
    # Both readers agree: the bot's next action is the same answer as the skip.
    assert "await_expense_decision" in session_detail(client, admin_claim_headers, sid)["next_actions"]


def test_the_reminder_names_hand_in_now_when_something_is_left(
    app, client, db, admin_claim_headers, sample_user, driver, notified, shipped_copy
):
    sid = collect_on_trip(client, admin_claim_headers, sample_user.id, driver.id, 420000)
    insert_expense(db, sid, driver.id, 150000)
    _backdate(db, sid)

    reminded = payment_tasks.send_cod_reconciliation_reminders.run()

    assert (reminded["sent"], reminded["failed"]) == (1, 0)
    assert len(notified.telegram) == 1 and notified.telegram[0]["user_id"] == driver.id
    body = notified.telegram[0]["body"]
    assert body.endswith("Hand in now: 270,000 UZS · 150,000 UZS waiting for approval.")
    assert "Expected on-hand cash: 420,000 UZS" in body
    # The in-app notification carries the very same text.
    assert [n["body"] for n in notified.in_app if n["user_id"] == driver.id] == [body]
    assert "submit_reconciliation" in session_detail(client, admin_claim_headers, sid)["next_actions"]


def test_without_claims_the_reminder_is_todays(
    app, client, db, admin_claim_headers, sample_user, driver, notified, shipped_copy
):
    sid = collect_on_trip(client, admin_claim_headers, sample_user.id, driver.id, 420000)
    _backdate(db, sid)

    reminded = payment_tasks.send_cod_reconciliation_reminders.run()

    assert (reminded["sent"], reminded["failed"]) == (1, 0)
    body = notified.telegram[0]["body"]
    assert body.endswith("Expected on-hand cash: 420,000 UZS.")
    assert "Hand in now" not in body
    assert [n["body"] for n in notified.in_app if n["user_id"] == driver.id] == [body]
