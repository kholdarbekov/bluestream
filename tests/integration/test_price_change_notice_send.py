"""One queued price-change notice, sent (spec 2026-11-16 §5.3; price-change plan Task 43): `price_change.send_notice`.

The fan-out queues one row per person and publishes one task per row. The task re-checks the row at send time, sends its
notice on the customer-notice rail and records the channel: `telegram` / `email` with the send instant, `none` with why
(nobody can reach the person), or the row stays queued with its error (a bot outage once the retries are spent, a bot
that is not configured) for the republish. A refusal (no row, already sent, the change no longer announcing, outside
09:00-18:00) sends and writes nothing. Every test runs the task as a worker does (`SEND.run`) on a frozen clock; the
bot's HTTP hop and Brevo's POST are the only fakes.
"""

from datetime import timezone

import pytest
import requests
from celery.exceptions import Retry

from business_app.models.price_change import PriceChangeNotice, PriceChangeNoticeKind, PriceChangeStatus
from business_app.services.customer_notices import RetryableNoticeError
from business_app.services.price_change import notices
from business_app.services.price_change.switch import apply_due_price_changes
from business_app.services.reorder_reminders.settings import ReorderReminderSettingsService
from business_app.tasks import price_change_tasks
from business_app.utils.timezone_utils import ensure_utc_or_none
from shared.enums import SubscriptionStatus
from tests.integration.customer_notice_builders import (  # noqa: F401  (fixtures by their *_fixture name)
    BOT_BLOCKED,
    BOT_URL,
    CHAT_ID,
    ROUTE,
    bot_fixture,
    on_telegram_fixture,
)
from tests.integration.contract_pricing_factory import make_workplace
from tests.integration.price_change_builders import (
    APPLIED_AT,
    CAP_10,
    CAP_19,
    CHANGE_ON,
    NEW_10,
    NEW_19,
    active_contract,
    announced,
    bottles,
    local,
    make_admin,
    make_price_change,
    queued,
)
from tests.integration.reorder_reminder_builders import link, make_customer, subscription
from tests.notification_send_fakes import BREVO_URL, live_send_path
from tests.unit.test_delivery_service_business_rules import _task_spy
from tests.unit.test_price_change_beat import celery_app_module  # noqa: F401  (the fixture)

pytestmark = pytest.mark.integration

SEND = price_change_tasks.send_price_change_notice
# 09:05 Tashkent on the change's `announce_on` (2027-01-04): inside the send hours, four weeks before the change.
A = local(2027, 1, 4, 9, 5)
# The change's own day, inside the send hours: past the change instant (00:00 Tashkent on 2027-02-01).
CHANGE_DAY = local(2027, 2, 1, 9, 5)


@pytest.fixture(name="change")
def change_fixture(db):
    """The world's change (the 18.9 L to 20,000 and the 10 L to 12,000), its announcement started at A."""
    b = bottles(db)
    price_change = make_price_change(
        db,
        [(b.p19, NEW_19, CAP_19, None), (b.p10, NEW_10, CAP_10, None)],
        created_by=make_admin(db),
        status=PriceChangeStatus.SCHEDULED,
        announcement_started_at=None,
    )
    announced(db, price_change, A)
    db.session.commit()
    return price_change


@pytest.fixture(name="clock")
def clock_fixture(monkeypatch):
    """The wall clock the send reads (`notices.get_utc_now`), frozen at A; `clock(when)` moves it to `when`."""

    def freeze(when):
        frozen = when.astimezone(timezone.utc)
        monkeypatch.setattr(notices, "get_utc_now", lambda: frozen)
        return frozen

    freeze(A)
    return freeze


def _queue(db, change, user, kind="announcement"):
    """The row the fan-out commits before it publishes the send."""
    queued(db, change, user, kind, A)
    db.session.commit()


def _state(change, user, kind="announcement"):
    """The row's `(channel, sent_at, error)`, read afresh from the database."""
    row = PriceChangeNotice.query.filter_by(
        price_change_id=change.id, user_id=user.id, kind=PriceChangeNoticeKind(kind)
    ).one()
    return row.channel, ensure_utc_or_none(row.sent_at), row.error


def _run_as_retry(args, retries_so_far):
    """The worker's run of a delivered send that Celery has retried `retries_so_far` times (a real request, so
    Celery's own `retry` republishes)."""
    SEND.push_request(id="send-1", args=list(args), kwargs={}, retries=retries_so_far, called_directly=False)
    try:
        return SEND.run(*args)
    finally:
        SEND.pop_request()


def test_a_telegram_send_records_its_channel_and_instant(change, on_telegram, bot, clock, db):
    _queue(db, change, on_telegram)
    notice_id = f"price_change:{change.id}:announcement:{on_telegram.id}"

    assert SEND.run(change.id, on_telegram.id, "announcement") == "telegram"

    assert bot.calls == [
        {
            "endpoint": ROUTE,
            "request_id": notice_id,
            "payload": {
                "notice_id": notice_id,
                "kind": "price_change_announcement",
                "telegram_id": CHAT_ID,
                "language": "en",
                "price_change_id": change.id,
                "effective_date": CHANGE_ON.isoformat(),
                "prices": [
                    {"name": "Water 18.9 L", "new_price": 20000.0},
                    {"name": "Water 10 L", "new_price": 12000.0},
                ],
                "is_subscriber": False,
            },
        }
    ]
    assert notices.notice_id(change.id, on_telegram.id, "announcement") == notice_id
    db.session.rollback()  # only what the send committed is still there
    assert _state(change, on_telegram) == ("telegram", A, None)


def test_the_same_notice_is_sent_once(change, on_telegram, bot, clock, db):
    """A second copy of the task (a republish, a redelivery) finds the row sent: nothing goes out, nothing changes."""
    _queue(db, change, on_telegram)

    first = SEND.run(change.id, on_telegram.id, "announcement")
    clock(local(2027, 1, 4, 9, 40))
    second = SEND.run(change.id, on_telegram.id, "announcement")

    assert (first, second) == ("telegram", "skipped:already_sent")
    assert len(bot.calls) == 1
    assert _state(change, on_telegram) == ("telegram", A, None)


@pytest.mark.parametrize("reach", ["no_channel", "email_failed"])
def test_no_channel_is_recorded_as_unreachable(app, monkeypatch, change, sample_user, bot, clock, db, reach):
    """A phone-only customer (no bot, a placeholder email) is recorded `none` with `no_channel`; one whose only email
    Brevo did not take, `none` with `email_failed`. Neither has a send instant."""
    sample_user.telegram_id = None
    sample_user.is_bot_active = False
    if reach == "no_channel":
        sample_user.email = f"telegram_{sample_user.id}@bluestream.local"
    _queue(db, change, sample_user)

    with live_send_path(app, monkeypatch, bot_url=BOT_URL) as post:
        post.side_effect = requests.exceptions.ConnectionError("Brevo is unreachable")
        outcome = SEND.run(change.id, sample_user.id, "announcement")

    assert outcome == reach
    assert bot.calls == []
    assert [call.args[0] for call in post.call_args_list] == ([] if reach == "no_channel" else [BREVO_URL])
    db.session.rollback()
    assert _state(change, sample_user) == ("none", None, reach)


def test_a_blocked_bot_is_recorded_as_email(app, monkeypatch, change, on_telegram, bot, clock, db):
    """The customer blocked the bot: the rail falls through to one email in the same attempt, and the row says email."""
    bot.answer = BOT_BLOCKED
    _queue(db, change, on_telegram)

    with live_send_path(app, monkeypatch, bot_url=BOT_URL) as post:
        outcome = SEND.run(change.id, on_telegram.id, "announcement")

    assert outcome == "email"
    assert len(bot.calls) == 1
    assert [call.args[0] for call in post.call_args_list] == [BREVO_URL]
    db.session.rollback()  # only what the send committed is still there
    assert _state(change, on_telegram) == ("email", A, None)


def test_a_bot_that_is_not_configured_leaves_the_row_queued_with_its_error(change, on_telegram, bot, clock, db):
    """`trigger_bot_webhook`'s answer without a bot URL: a deployment fault a retry cannot fix, so the task does not
    retry, and the row stays queued for the republish, with the outcome as its error."""
    bot.answer = {"success": False, "message": "Bot webhook URL not configured"}
    _queue(db, change, on_telegram)

    assert SEND.run(change.id, on_telegram.id, "announcement") == "telegram_unconfigured"

    db.session.rollback()
    assert _state(change, on_telegram) == (None, None, "telegram_unconfigured")


@pytest.mark.parametrize(
    "status, is_subscriber", [(SubscriptionStatus.PAUSED, True), (SubscriptionStatus.CANCELLED, False)]
)
def test_the_subscriber_answer_is_read_across_linked_accounts_at_send_time(
    change, on_telegram, bot, clock, db, status, is_subscriber
):
    """The plan is on the person's other phone and was made after the row was queued: the send reads it then."""
    _queue(db, change, on_telegram)
    other_phone = make_customer(db)
    link(db, on_telegram, other_phone)
    subscription(db, other_phone, status=status)

    assert SEND.run(change.id, on_telegram.id, "announcement") == "telegram"

    assert [call["payload"]["is_subscriber"] for call in bot.calls] == [is_subscriber]


@pytest.mark.parametrize("ending", ["cancelled", "applied", "past_the_change_instant"])
def test_a_change_no_longer_announcing_sends_and_writes_nothing(
    app, change, on_telegram, bot, clock, db, admin_claim_headers, ending
):
    """An ADMIN cancelled it after the announcement (at A, through the admin screen's route); the switch applied it
    (sent on its day); its instant passed before the switch ran (still ANNOUNCED, sent on its day)."""
    _queue(db, change, on_telegram)
    if ending == "cancelled":
        response = app.test_client().post(
            f"/api/v1/admin/price-changes/{change.id}/cancel", headers=admin_claim_headers
        )
        assert response.status_code == 200, response.get_data(as_text=True)
    else:
        clock(CHANGE_DAY)
    if ending == "applied":
        assert apply_due_price_changes(APPLIED_AT) == [change.id]

    assert SEND.run(change.id, on_telegram.id, "announcement") == "skipped:not_announcing"

    assert bot.calls == []
    db.session.rollback()
    assert _state(change, on_telegram) == (None, None, None)


@pytest.mark.parametrize("at", [local(2027, 1, 5, 8, 59), local(2027, 1, 4, 18, 0)], ids=["08:59", "18:00"])
def test_outside_09_to_18_nothing_is_sent_or_written(change, on_telegram, bot, clock, db, at):
    _queue(db, change, on_telegram)
    clock(at)

    assert SEND.run(change.id, on_telegram.id, "announcement") == "skipped:outside_send_window"

    assert bot.calls == []
    db.session.rollback()
    assert _state(change, on_telegram) == (None, None, None)


def test_a_person_an_admin_switched_off_after_the_queue_is_not_sent_to(change, on_telegram, bot, clock, db, admin_user):
    """(Ledger T43) A row can stay queued for days (a bot outage, the 30-minute republish). An admin who switches the
    customer off meanwhile ("do not contact", usually after a complaint) is obeyed at send time: nothing is sent and
    nothing is written, as the recipient rule would have left them out."""
    _queue(db, change, on_telegram)
    ReorderReminderSettingsService.set(
        on_telegram.id, False, actor_kind="admin", via="admin_ui", reason="Complained", actor_user_id=admin_user.id
    )

    assert SEND.run(change.id, on_telegram.id, "announcement") == "skipped:excluded"

    assert bot.calls == []
    db.session.rollback()
    assert _state(change, on_telegram) == (None, None, None)


def test_a_person_whose_contract_started_after_the_queue_is_not_sent_to(change, bot, clock, db):
    """(Ledger T43) A business whose corporate contract came into force after its row was queued gets letters (spec
    §5.11), not this notice: the send reads the contract exclusion at its own instant."""
    business, _office = make_workplace(db)
    business.telegram_id, business.is_bot_active, business.preferred_language = CHAT_ID, True, "en"
    db.session.commit()
    _queue(db, change, business)
    active_contract(db, business, change.items[0].product)

    assert SEND.run(change.id, business.id, "announcement") == "skipped:excluded"

    assert bot.calls == []
    db.session.rollback()
    assert _state(change, business) == (None, None, None)


def test_a_notice_the_fan_out_never_queued_is_not_sent(change, on_telegram, bot, clock, db):
    """The announcement is queued for this person, the reminder never was; another customer has no row at all."""
    _queue(db, change, on_telegram)
    stranger = make_customer(db, telegram=True)

    assert SEND.run(change.id, on_telegram.id, "reminder") == "skipped:not_queued"
    assert SEND.run(change.id, stranger.id, "announcement") == "skipped:not_queued"

    assert bot.calls == []
    db.session.rollback()
    assert PriceChangeNotice.query.filter_by(price_change_id=change.id).count() == 1
    assert _state(change, on_telegram) == (None, None, None)


def test_a_bot_outage_retries_with_backoff_then_leaves_the_row_queued_with_its_error(
    change, on_telegram, bot, clock, db, monkeypatch
):
    """The bot cannot be reached (`trigger_bot_webhook`'s answer when the request itself fails; a long message here).
    Celery's own `retry` republishes the same send 60 s, 120 s and 240 s on, each attempt under the one notice id the
    bot sends once. Once the retries are spent the row stays queued, for the republish, its error cut to the column's
    255 characters."""
    _queue(db, change, on_telegram)
    bot.answer = {"success": False, "message": "Bot webhook failed: " + "x" * 300}
    republished = _task_spy(monkeypatch, SEND, "apply_async")
    args = (change.id, on_telegram.id, "announcement")

    retried = []
    for retries_so_far in range(3):
        with pytest.raises(Retry) as raised:
            _run_as_retry(args, retries_so_far)
        retried.append((type(raised.value.exc), raised.value.when))

    assert retried[:1] == [(RetryableNoticeError, 60)]
    assert retried == [(RetryableNoticeError, 60), (RetryableNoticeError, 120), (RetryableNoticeError, 240)]
    assert [(sent_args, options["countdown"]) for sent_args, _kwargs, options in republished] == [
        (args, 60),
        (args, 120),
        (args, 240),
    ]
    assert _state(change, on_telegram) == (None, None, None)

    outcome = _run_as_retry(args, 3)

    error = str(raised.value.exc)
    assert len(error) > 300
    assert outcome == f"unsent:{error}"
    assert [call["request_id"] for call in bot.calls] == [notices.notice_id(*args)] * 4
    assert republished[3:] == []
    db.session.rollback()  # only what `record_unsent` committed is still there
    channel, sent_at, stored = _state(change, on_telegram)
    assert (channel, sent_at, stored) == (None, None, error[:255])
    assert len(stored) <= 255


def test_sends_are_rate_limited_to_ten_a_second(celery_app_module):
    """About ten sends a second per worker, as the reorder reminders: well under Telegram's ~30 messages a second."""
    assert celery_app_module.celery.conf.task_annotations["price_change.send_notice"] == {"rate_limit": "10/s"}
    assert "price_change.send_notice" in celery_app_module.celery.tasks
