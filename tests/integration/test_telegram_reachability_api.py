"""`POST /api/v1/bot/telegram-reachability`: the customer bot's block / unblock reports (spec
2026-10-09 §7.2, D8, D19).

The bot signs the exact bytes it sends (`api_client._make_request(sign=True)`), so these tests do
too. `TelegramReachabilityService` is the only writer of `is_bot_active` and
`telegram_blocked_at`; each change of block state appends a `blocked` / `unblocked` event whose
`detail` is the source and whose payload is the reported instant.
"""

import hashlib
import hmac
import json
from datetime import UTC, datetime

import pytest

from business_app.models.reorder_reminder import ReorderReminderEvent
from business_app.models.user import User
from business_app.tasks.notification_tasks import send_delivery_rescheduled_notification_task
from business_app.utils.timezone_utils import ensure_utc
from tests.unit.test_delivery_rescheduled_notification import BREVO_URL, _dated_order, live_send  # noqa: F401

pytestmark = pytest.mark.integration

URL = "/api/v1/bot/telegram-reachability"
TELEGRAM_ID = "810000777"
EARLIER = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)
BETWEEN = datetime(2026, 10, 14, 9, 15, tzinfo=UTC)
BLOCKED_AT = datetime(2026, 10, 14, 9, 30, tzinfo=UTC)
LATER = datetime(2026, 10, 14, 10, 0, tzinfo=UTC)
INVALID = "TELEGRAM_REACHABILITY_INVALID"


def _signed_post(app, client, body):
    raw = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    signature = hmac.new(app.config["BOT_WEBHOOK_SECRET"].encode("utf-8"), raw, hashlib.sha256).hexdigest()
    return client.post(
        URL, data=raw, headers={"Content-Type": "application/json", "X-Bot-Webhook-Signature": signature}
    )


def _report(status, at, source="my_chat_member", telegram_id=TELEGRAM_ID):
    return {"telegram_id": telegram_id, "status": status, "at": at.isoformat(), "source": source}


@pytest.fixture
def connected(db, sample_user):
    """A customer whose bot works: the starting point of every block."""
    sample_user.telegram_id = TELEGRAM_ID
    sample_user.is_bot_active = True
    sample_user.preferred_language = "en"
    db.session.commit()
    return sample_user.id


def _reachability(db, user_id):
    db.session.expire_all()
    user = db.session.get(User, user_id)
    blocked_at = ensure_utc(user.telegram_blocked_at) if user.telegram_blocked_at is not None else None
    return user.is_bot_active, blocked_at


def _events(db, user_id):
    db.session.expire_all()
    rows = ReorderReminderEvent.query.filter_by(user_id=user_id).order_by(ReorderReminderEvent.id).all()
    return [(row.kind, row.detail, row.payload, row.is_dry_run, row.episode_id) for row in rows]


def _changed(response):
    assert response.status_code == 200, response.get_data(as_text=True)
    body = response.get_json()
    assert body["success"] is True
    return body["data"]["changed"]


def test_a_block_report_marks_the_customer_blocked_and_logs_it(app, client, db, connected):
    assert _changed(_signed_post(app, client, _report("blocked", BLOCKED_AT))) is True

    assert _reachability(db, connected) == (False, BLOCKED_AT)
    assert _events(db, connected) == [("blocked", "my_chat_member", {"at": "2026-10-14T09:30:00+00:00"}, False, None)]


def test_an_unsigned_report_is_refused_and_changes_nothing(client, db, connected):
    response = client.post(URL, json=_report("blocked", BLOCKED_AT))

    assert response.status_code == 401
    assert _reachability(db, connected) == (True, None)
    assert _events(db, connected) == []


def test_a_reachable_report_older_than_the_block_never_clears_it(app, client, db, connected):
    """§7.2 / §15.6: a late `reachable` (the recovery middleware reporting a message sent before
    the customer blocked) can never undo a newer block."""
    _signed_post(app, client, _report("blocked", BLOCKED_AT))

    assert _changed(_signed_post(app, client, _report("reachable", EARLIER, source="recovery"))) is False

    assert _reachability(db, connected) == (False, BLOCKED_AT)
    assert [kind for kind, *_ in _events(db, connected)] == ["blocked"]


def test_a_newer_reachable_report_unblocks_and_logs_its_source(app, client, db, connected):
    _signed_post(app, client, _report("blocked", BLOCKED_AT))

    assert _changed(_signed_post(app, client, _report("reachable", LATER, source="recovery"))) is True

    assert _reachability(db, connected) == (True, None)
    assert _events(db, connected) == [
        ("blocked", "my_chat_member", {"at": "2026-10-14T09:30:00+00:00"}, False, None),
        ("unblocked", "recovery", {"at": "2026-10-14T10:00:00+00:00"}, False, None),
    ]


def test_a_second_block_report_keeps_the_newest_block_instant(app, client, db, connected):
    """Already blocked: no second event, but the newest instant is kept, so a reachable report
    from between the two blocks is still recognised as stale."""
    _signed_post(app, client, _report("blocked", EARLIER))

    assert _changed(_signed_post(app, client, _report("blocked", BLOCKED_AT))) is False
    assert _changed(_signed_post(app, client, _report("reachable", BETWEEN, source="recovery"))) is False

    assert _reachability(db, connected) == (False, BLOCKED_AT)
    assert [kind for kind, *_ in _events(db, connected)] == ["blocked"]


def test_an_unknown_telegram_id_is_answered_not_failed(app, client, db, connected):
    """A brand-new user's first `my_chat_member` arrives before registration: nothing to record, and
    nothing for the bot to retry."""
    assert _changed(_signed_post(app, client, _report("reachable", LATER, telegram_id="899999999"))) is False

    assert ReorderReminderEvent.query.count() == 0


@pytest.mark.parametrize(
    "body",
    [
        _report("left", BLOCKED_AT),
        _report("blocked", BLOCKED_AT, source="webhook"),
        {"telegram_id": TELEGRAM_ID, "status": "blocked", "at": "2026-10-14T09:30:00", "source": "my_chat_member"},
        {"status": "blocked", "at": BLOCKED_AT.isoformat()},
    ],
    ids=["unknown-status", "unknown-source", "instant-without-offset", "no-telegram-id"],
)
def test_a_malformed_report_is_a_400_with_a_code(app, client, db, connected, body):
    response = _signed_post(app, client, body)

    assert response.status_code == 400, response.get_data(as_text=True)
    assert response.get_json()["data"]["error_code"] == INVALID
    assert _reachability(db, connected) == (True, None)
    assert _events(db, connected) == []


def test_a_blocked_customer_gets_service_notices_by_email(app, client, db, connected, live_send):
    """D8: once the block is known, the shared resolver sends the reschedule notice to the
    customer's real email instead of into a chat that takes no messages."""
    assert _changed(_signed_post(app, client, _report("blocked", BLOCKED_AT))) is True
    db.session.expire_all()
    order, _ = _dated_order(db, db.session.get(User, connected))

    result = send_delivery_rescheduled_notification_task.run(order.id)

    assert result == {"email": {"success": True, "message_id": "<brevo-501>", "status_code": 200}}
    assert live_send.call_count == 1
    assert live_send.call_args.args == (BREVO_URL,)
    assert live_send.call_args.kwargs["json"]["to"] == [{"email": "test@example.com", "name": "Test User"}]
