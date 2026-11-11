"""The overdue and reminder Celery tasks after a "from his current trip's cash" return (spec 2026-11-10 §4.3).

Both tasks judge a session by the cash it COLLECTED (`_session_has_cash_activity` reads gross), not by
what the driver still holds. A return that leaves the driver nothing to hand in therefore has to close
his session (the zero close); left OPEN, it would be marked overdue and reminded for cash he no longer
has, while the bot offers him nothing to submit. A return that leaves cash to hand in changes nothing
here: that session still goes overdue.

The money lands through the admin routes, the session is back-dated 8 days the way
`test_warning_due_session_is_visible_without_blocking_cod` does it, and the tasks run as Celery runs
them (`.run()`). Only the outbound notification is faked, with a recorder bound to the real signature.
"""

import inspect
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from business_app.models.payment import DriverCashSession
from business_app.services.notification_service import NotificationService
from business_app.tasks import payment_tasks
from shared.enums import DriverCashSessionStatus

pytestmark = pytest.mark.unit

BASE = "/api/v1/admin/staff/cash-reconciliation"
COLLECTIONS = BASE + "/collections"
APPLY = BASE + "/customers/{}/prepayment-payouts"


@pytest.fixture
def notified(monkeypatch):
    """(user id, template data) of every notification the tasks send, each call bound against the real signature."""
    sent = []
    real_send = NotificationService.send_notification
    real_telegram = NotificationService.send_staff_telegram_message

    def send_notification(self, *args, **kwargs):
        bound = inspect.signature(real_send).bind(self, *args, **kwargs)
        sent.append((bound.arguments["user_id"], bound.arguments.get("template_data")))
        return {"success": True}

    def send_staff_telegram_message(self, *args, **kwargs):
        bound = inspect.signature(real_telegram).bind(self, *args, **kwargs)
        sent.append((bound.arguments["user"].id, None))
        return {"success": True}

    monkeypatch.setattr(NotificationService, "send_notification", send_notification)
    monkeypatch.setattr(NotificationService, "send_staff_telegram_message", send_staff_telegram_message)
    return sent


def _collect_on_trip(client, headers, customer_id, driver_id, amount):
    response = client.post(
        COLLECTIONS,
        json={
            "customer_id": customer_id,
            "amount": amount,
            "source": "standalone_meeting",
            "collector_user_id": driver_id,
            "notes": "Paid ahead to the driver",
        },
        headers=headers,
    )
    assert response.status_code == 201, response.get_json()
    return response.get_json()["data"]["cash_collection_event"]["driver_cash_session_id"]


def _backdate(db, session_id):
    """Eight days old and past its warning: what the overdue sweep looks for."""
    session = db.session.get(DriverCashSession, session_id)
    session.session_started_at = datetime.now(UTC) - timedelta(days=8)
    session.warning_due_at = datetime.now(UTC) - timedelta(minutes=5)
    session.submission_due_at = session.warning_due_at
    session.last_cash_activity_at = datetime.now(UTC) - timedelta(days=8)
    db.session.commit()


def _return_from_trip(client, headers, customer_id, driver_id, amount):
    response = client.post(
        APPLY.format(customer_id),
        json={
            "amount": amount,
            "paid_by": "driver_current_session",
            "driver_user_id": driver_id,
            "reason": "Customer moved away",
            "idempotency_key": f"overdue-{amount}",
        },
        headers=headers,
    )
    assert response.status_code == 201, response.get_json()
    return response.get_json()["data"]


def _status(db, session_id):
    db.session.expire_all()
    return db.session.get(DriverCashSession, session_id).status


def test_zero_closed_session_is_neither_marked_overdue_nor_reminded(
    app, client, db, admin_user, admin_claim_headers, sample_user, second_delivery_driver, notified
):
    driver = second_delivery_driver
    session_id = _collect_on_trip(client, admin_claim_headers, sample_user.id, driver.id, 108000)
    _backdate(db, session_id)

    data = _return_from_trip(client, admin_claim_headers, sample_user.id, driver.id, 108000)

    assert data["driver_cash_session"]["status"] == "submitted"
    assert _status(db, session_id) == DriverCashSessionStatus.SUBMITTED

    marked = payment_tasks.mark_overdue_cod_reconciliation_sessions.run()
    reminded = payment_tasks.send_cod_reconciliation_reminders.run()

    assert marked == {"updated_sessions": 0, "manager_notifications": 0}
    assert reminded == {"sent": 0, "failed": 0, "manager_notifications": 0}
    assert _status(db, session_id) == DriverCashSessionStatus.SUBMITTED
    assert notified == []


def test_partly_paid_out_open_session_still_goes_overdue(
    app, client, db, admin_user, admin_claim_headers, sample_user, second_delivery_driver, notified
):
    driver = second_delivery_driver
    session_id = _collect_on_trip(client, admin_claim_headers, sample_user.id, driver.id, 108000)
    _backdate(db, session_id)

    data = _return_from_trip(client, admin_claim_headers, sample_user.id, driver.id, 50000)

    assert (data["driver_cash_session"]["status"], data["driver_cash_session"]["expected_cash_on_hand"]) == (
        "open",
        58000.0,
    )

    marked = payment_tasks.mark_overdue_cod_reconciliation_sessions.run()

    assert marked["updated_sessions"] == 1
    assert _status(db, session_id) == DriverCashSessionStatus.OVERDUE

    reminded = payment_tasks.send_cod_reconciliation_reminders.run()

    assert (reminded["sent"], reminded["failed"]) == (1, 0)
    reminders = [template for user_id, template in notified if user_id == driver.id]
    # One reminder, naming what he still holds (58 000), not what he collected (108 000).
    assert reminders == [
        {
            "session_started_at": reminders[0]["session_started_at"],
            "expected_cash_on_hand": 58000.0,
            "reminder_stage": "overdue",
        }
    ]
    session = db.session.get(DriverCashSession, session_id)
    assert (Decimal(str(session.expected_cash)), Decimal(str(session.expected_cash_on_hand))) == (
        Decimal("108000.00"),
        Decimal("58000.00"),
    )
