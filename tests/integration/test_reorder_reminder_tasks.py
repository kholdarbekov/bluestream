"""The reorder-reminder Celery tasks (spec §12): the daily run behind its lock, and the send task's
re-check at send time (master Review Focus #1 and #4)."""

from datetime import timezone
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from celery.exceptions import Retry

from business_app.services.notification_service import NotificationService
from business_app.services.reorder_reminders import engine as engine_module
from business_app.services.reorder_reminders import facts as facts_module
from business_app.services.reorder_reminders import messaging
from business_app.services.reorder_reminders.actions import ReorderReminderActions
from business_app.services.reorder_reminders.messaging import ChannelResult, RetryableSendError
from business_app.services.reorder_reminders.tokens import read_unsubscribe_token, unsubscribe_headers
from business_app.services.sales.outlet_service import OutletService
from business_app.services.telegram_reachability_service import TelegramReachabilityService
from business_app.tasks import reorder_reminder_tasks
from business_app.utils import reorder_reminder_dispatch
from business_app.utils.constants import NotificationChannel, NotificationType
from business_app.utils.timezone_utils import ensure_utc
from shared.redis_keyspace import RedisKeyspace
# `*_fixture` are the bot_webhook, email_claims, lock, clock, mode, sends and store fixtures.
from tests.integration.reorder_reminder_builders import (  # noqa: F401
    BOT_BLOCKED,
    BOT_DEDUPLICATED,
    BOT_DELIVERED,
    WEEKLY,
    at,
    bot_webhook_fixture,
    captured_logs,
    clock_fixture,
    customer_with_deliveries,
    delivered_order,
    email_claims_fixture,
    event_rows,
    lapses,
    live_order,
    lock_fixture,
    make_episode,
    mode_fixture,
    only_lapse,
    reminder_copy_keys,
    run_at,
    seed_translations,
    sends_fixture,
    store_fixture,
)
from tests.unit.test_delivery_service_business_rules import _patch_task_publish, _task_spy

RUN = reorder_reminder_tasks.run_daily_reminders
SEND = reorder_reminder_tasks.send_reorder_reminder
LOCK = RedisKeyspace.reorder_reminders_run_lock()


@pytest.fixture
def telegram(monkeypatch):
    """The Telegram adapter, replaced by a recorder with its exact seam signature (contract
    amendment 11, `now` keyword-only) that reports success."""
    calls = []

    def send(episode, customer, evaluation, step, cfg, *, now):
        calls.append((episode.id, customer.key, evaluation.recipient.user_id, evaluation.channel.channel, step, now))
        return ChannelResult(ok=True)

    monkeypatch.setattr(messaging, "_send_telegram", send)
    return calls


def _telegram_502(episode, customer, evaluation, step, cfg, *, now):
    """The Telegram adapter's transient failure (a bot-route 5xx), with the seam's exact signature."""
    return ChannelResult(ok=False, detail="telegram_502", retryable=True)


def _record_retries(monkeypatch):
    """Replace `SEND.retry` (through the task's instance dict, never setattr) with a recorder that
    raises Celery's `Retry`, as the real one does. Returns the keyword arguments of each call."""
    retries = []

    def fake_retry(*args, **kwargs):
        retries.append(kwargs)
        raise Retry()

    _patch_task_publish(monkeypatch, SEND, "retry", fake_retry)
    return retries


def _reminded_on_day_11(db, clock, mode, sends, **customer_fields):
    """A weekly customer reminded by the 10:00 run on day 11 (on Telegram unless fields say otherwise)."""
    mode("on")
    clock(run_at(11))
    ali, orders = customer_with_deliveries(db, WEEKLY, **(customer_fields or {"telegram": True}))
    RUN.run()
    row = only_lapse(orders[-1])
    assert sends == [(row.id, 1)]
    return ali, orders, row


def test_the_daily_run_takes_the_lock_runs_once_and_releases_it(db, lock, clock, mode, sends):
    mode("on")
    clock(run_at(11))
    ali, orders = customer_with_deliveries(db, WEEKLY, telegram=True)

    result = RUN.run()

    assert (result["mode"], result["opened"], result["queued"], result["errors"]) == ("on", 1, 1, 0)
    assert (lock.store, lock.expiries) == ({}, {LOCK: 1800})
    assert sends == [(only_lapse(orders[-1]).id, 1)]
    assert ensure_utc(only_lapse(orders[-1]).created_at) == run_at(11)  # the task's one `now` reached the engine


def test_a_run_that_finds_the_lock_taken_does_nothing(db, lock, clock, mode, monkeypatch):
    mode("on")
    clock(run_at(11))
    lock.set(LOCK, "another-run", nx=True, ex=1800)
    started = []
    monkeypatch.setattr(engine_module.ReorderReminderEngine, "run", lambda self: started.append(self))

    assert RUN.run() == {"skipped": "locked"}
    assert (started, lock.get(LOCK)) == ([], b"another-run")


def test_a_run_that_outlived_its_lock_leaves_the_next_runs_lock_alone(db, lock, clock, mode, monkeypatch):
    """The lock is deleted by its holder only. A run that outlived its TTL finds the key taken by the
    next run (overwritten mid-run here) and must not drop that run's lock."""
    mode("on")
    clock(run_at(11))

    def run_past_the_ttl(self):
        lock.store[LOCK] = b"next-run"
        return engine_module.RunSummary(mode=self.cfg.mode)

    monkeypatch.setattr(engine_module.ReorderReminderEngine, "run", run_past_the_ttl)

    assert RUN.run()["mode"] == "on"
    assert lock.get(LOCK) == b"next-run"


def test_the_run_lock_is_released_when_the_engine_raises(db, lock, clock, mode, monkeypatch):
    mode("on")
    clock(run_at(11))

    def crash(self):
        raise RuntimeError("engine crashed")

    monkeypatch.setattr(engine_module.ReorderReminderEngine, "run", crash)

    with pytest.raises(RuntimeError, match="engine crashed"):
        RUN.run()
    assert lock.store == {}


def test_a_run_without_redis_is_skipped_rather_than_run_unlocked(db, monkeypatch, caplog):
    class DownRedis:
        def set(self, key, value, nx=False, ex=None):
            raise ConnectionError("redis is down")

    monkeypatch.setattr(reorder_reminder_tasks, "redis_client", DownRedis())

    with captured_logs(caplog, reorder_reminder_tasks.logger):
        assert RUN.run() == {"skipped": "lock_unavailable"}
    assert "redis is down" in caplog.text


def test_one_customers_facts_raising_is_logged_with_its_key_and_the_run_finishes(
    db, lock, clock, mode, sends, monkeypatch, caplog
):
    """Review Focus #4: one customer's facts raise (a deleted outlet user here; a corrupt address
    group raises the same way from the place read). The raise is in a FACTS-level helper, not in the
    engine (contract amendment 17). The error is logged with the customer key, that customer is
    skipped, every other customer is still processed, and the run summary counts the error."""
    mode("on")
    clock(run_at(11))
    first, first_orders = customer_with_deliveries(db, WEEKLY, telegram=True, first_name="First")
    broken, broken_orders = customer_with_deliveries(db, WEEKLY, telegram=True, first_name="Broken")
    last, last_orders = customer_with_deliveries(db, WEEKLY, telegram=True, first_name="Last")
    real_outlet_facts = OutletService.outlet_facts_by_user

    def outlet_facts_or_fail(user_ids):
        if broken.id in user_ids:
            raise RuntimeError("deleted outlet user")
        return real_outlet_facts(user_ids)

    monkeypatch.setattr(OutletService, "outlet_facts_by_user", staticmethod(outlet_facts_or_fail))

    with captured_logs(caplog, facts_module.logger):
        result = RUN.run()

    assert (result["errors"], result["opened"], result["queued"]) == (1, 2, 2)
    assert lapses(broken_orders[-1]) == []
    assert [len(lapses(orders[-1])) for orders in (first_orders, last_orders)] == [1, 1]
    assert f"u:{broken.id}" in caplog.text and "deleted outlet user" in caplog.text
    assert lock.store == {}


def test_an_order_placed_after_the_run_stops_the_send_as_paused(db, lock, clock, mode, sends, telegram):
    """Review Focus #1: the customer orders between the 10:00 run's commit and the send."""
    ali, orders, row = _reminded_on_day_11(db, clock, mode, sends)
    live_order(db, ali, at(11, 10, 3))
    clock(at(11, 10, 5))

    assert SEND.run(row.id, 1) == "failed:paused"

    assert telegram == []
    assert event_rows(episode=row)[-1] == ("reminder_failed", "paused", 1, None)
    assert only_lapse(orders[-1]).reminders_sent == 0


def test_a_delivery_since_the_run_refuses_the_send_by_the_close_rule_and_the_next_run_closes_it(
    db, lock, clock, mode, sends, telegram
):
    """R1 is queued, then the customer reorders and that order is delivered before the send runs (a
    backed-up worker, or a re-published copy): the lapse's pair has moved. The send asks the
    engine's own close rule (`close_decision`) and refuses as `pending_close`; nothing is sent, and
    the next run closes the lapse `ordered`."""
    ali, orders, row = _reminded_on_day_11(db, clock, mode, sends)
    delivered_order(db, ali, at(11, 14))
    clock(at(11, 15))

    assert SEND.run(row.id, 1) == "failed:pending_close"

    assert telegram == []
    assert event_rows(episode=row)[-1] == ("reminder_failed", "pending_close", 1, None)
    assert only_lapse(orders[-1]).reminders_sent == 0
    clock(run_at(12))
    RUN.run()
    closed = only_lapse(orders[-1])
    assert (closed.status, closed.close_reason) == ("closed", "ordered")


def test_a_send_that_passes_the_re_check_goes_out_once_on_the_run_time_channel(db, lock, clock, mode, sends, telegram):
    ali, orders, row = _reminded_on_day_11(db, clock, mode, sends)
    clock(at(11, 10, 5))

    assert SEND.run(row.id, 1) == "sent"

    assert telegram == [(row.id, f"u:{ali.id}", ali.id, "telegram", 1, at(11, 10, 5))]  # `now=` is the send's clock
    sent = only_lapse(orders[-1])
    assert (sent.reminders_sent, sent.last_channel, ensure_utc(sent.last_reminder_at)) == (1, "telegram", at(11, 10, 5))
    assert event_rows(episode=row)[-1] == ("reminder_sent", None, 1, "telegram")
    assert SEND.run(row.id, 1) == "failed:already_sent"  # a redelivered task never messages twice
    assert len(telegram) == 1


def test_a_copy_racing_the_send_leaves_one_reminder_sent_for_the_step(db, lock, clock, mode, sends, monkeypatch):
    """At most one `reminder_sent` per (episode, step). A re-published copy passes the
    `already_sent` check while the original is still sending; here, single-threaded, the copy runs
    inside the original's adapter call and commits first. The original then finds the copy's
    `reminder_sent` under the episode row lock and records nothing. Both copies reached the
    adapter: the customer-facing dedupe is the bot route's request id (Task 15a). On Postgres the
    row lock (`SELECT ... FOR UPDATE`) is what serialises two workers; SQLite has no row locks, so
    this pins the re-check under the lock, not the lock itself."""
    ali, orders, row = _reminded_on_day_11(db, clock, mode, sends)
    clock(at(11, 10, 5))
    adapter_calls, copy_results = [], []

    def send(episode, customer, evaluation, step, cfg, *, now):
        adapter_calls.append(step)
        if len(adapter_calls) == 1:  # the original is mid-send: the racing copy runs and commits now
            copy_results.append(SEND.run(episode.id, step))
        return ChannelResult(ok=True)

    monkeypatch.setattr(messaging, "_send_telegram", send)

    assert SEND.run(row.id, 1) == "failed:already_sent"

    assert (copy_results, adapter_calls) == (["sent"], [1, 1])
    sent = only_lapse(orders[-1])
    assert (sent.reminders_sent, sent.last_channel) == (1, "telegram")
    assert [kind for kind, *_rest in event_rows(episode=row)] == ["opened", "reminder_queued", "reminder_sent"]


def test_a_second_sent_record_for_the_same_step_records_nothing(store):
    """The record itself, on SQLite and on a migrated Postgres, where `_record_sent` takes a real
    `SELECT ... FOR UPDATE` on the episode row: once a step's `reminder_sent` has committed, a
    second record for that step returns `already_sent` and writes nothing."""
    ali, orders = customer_with_deliveries(store, WEEKLY, telegram=True)
    row = make_episode(store, ali, anchor=orders[-1], step=1)
    sent_at = at(11, 10, 5).astimezone(timezone.utc)

    assert messaging._record_sent(row, 1, "telegram", sent_at) == "sent"
    assert messaging._record_sent(row, 1, "telegram", sent_at) == "failed:already_sent"

    assert only_lapse(orders[-1]).reminders_sent == 1
    assert event_rows(episode=row) == [("reminder_sent", None, 1, "telegram")]


def test_a_failure_recorded_after_the_step_was_sent_records_nothing(db, lock, clock, mode, sends, telegram):
    """No phantom failure beside a real send: once step 1 has its `reminder_sent`, recording a
    failure for that step (a spent retry, here `telegram_502`) writes nothing and says so."""
    ali, orders, row = _reminded_on_day_11(db, clock, mode, sends)
    clock(at(11, 10, 5))
    assert SEND.run(row.id, 1) == "sent"

    assert messaging.record_send_failure(row.id, 1, "telegram_502") == "failed:already_sent"

    assert [kind for kind, *_rest in event_rows(episode=row)] == ["opened", "reminder_queued", "reminder_sent"]
    assert only_lapse(orders[-1]).reminders_sent == 1


def test_a_last_retry_that_fails_while_a_racing_copy_succeeds_records_only_the_send(
    db, lock, clock, mode, sends, monkeypatch
):
    """The same rule on the task's own path: the original's last retry is mid-send when a
    re-published copy gets through and commits `reminder_sent`; the original then gets a 502 with
    its retries spent, and its `record_send_failure` finds the send under the row lock."""
    ali, orders, row = _reminded_on_day_11(db, clock, mode, sends)
    clock(at(11, 10, 5))
    copy_results = []

    def send(episode, customer, evaluation, step, cfg, *, now):
        if copy_results:
            return ChannelResult(ok=True)  # the racing copy's own send
        copy_results.append(None)
        copy_results[0] = SEND.run(episode.id, step)
        return _telegram_502(episode, customer, evaluation, step, cfg, now=now)

    monkeypatch.setattr(messaging, "_send_telegram", send)
    retries = _record_retries(monkeypatch)

    SEND.push_request(retries=3)
    try:
        assert SEND.run(row.id, 1) == "failed:already_sent"
    finally:
        SEND.pop_request()

    assert (copy_results, retries) == (["sent"], [])
    assert [kind for kind, *_rest in event_rows(episode=row)] == ["opened", "reminder_queued", "reminder_sent"]
    assert only_lapse(orders[-1]).reminders_sent == 1


def test_a_facts_fault_at_send_time_records_nothing_and_the_next_run_re_publishes_the_send(
    db, lock, clock, mode, sends, telegram, monkeypatch
):
    """A fault loading the customer's facts at send time (a deleted outlet user here) is raised, not
    read as "no such user": the task fails, nothing is recorded, and `reminder_queued` stays the
    latest event, so the next run's re-publish (§12 step 8) sends it again."""
    ali, orders, row = _reminded_on_day_11(db, clock, mode, sends)
    real_outlet_facts = OutletService.outlet_facts_by_user
    faulty = {ali.id}

    def outlet_facts_or_fail(user_ids):
        if faulty & set(user_ids):
            raise RuntimeError("deleted outlet user")
        return real_outlet_facts(user_ids)

    monkeypatch.setattr(OutletService, "outlet_facts_by_user", staticmethod(outlet_facts_or_fail))
    clock(at(11, 10, 5))

    with pytest.raises(RuntimeError, match="deleted outlet user"):
        SEND.run(row.id, 1)

    assert telegram == []
    assert event_rows(episode=row)[-1] == ("reminder_queued", None, 1, "telegram")
    assert only_lapse(orders[-1]).reminders_sent == 0
    faulty.clear()
    sends.clear()
    clock(run_at(11, minute=31))
    assert RUN.run()["republished"] == 1
    assert sends == [(row.id, 1)]


def test_a_send_after_18_00_or_after_mode_off_is_refused_and_recorded(db, lock, clock, mode, sends, telegram):
    ali, orders, row = _reminded_on_day_11(db, clock, mode, sends)
    clock(at(11, 18, 0))
    assert SEND.run(row.id, 1) == "failed:outside_window"
    mode("off")
    clock(at(11, 10, 5))

    assert SEND.run(row.id, 1) == "failed:mode_off"

    assert telegram == []
    failures = [detail for kind, detail, _step, _channel in event_rows(episode=row) if kind == "reminder_failed"]
    assert failures == ["outside_window", "mode_off"]


def test_a_delivery_that_failed_since_the_run_refuses_the_send_as_stuck_order(db, lock, clock, mode, sends, telegram):
    ali, orders, row = _reminded_on_day_11(db, clock, mode, sends)
    live_order(db, ali, at(11, 10, 2), failed=True)
    clock(at(11, 10, 5))

    assert SEND.run(row.id, 1) == "failed:stuck_order"
    assert telegram == []


def test_a_bot_block_since_the_run_refuses_the_send_and_never_falls_back_to_email(
    db, lock, clock, mode, sends, telegram
):
    """D19: a blocked account gets no reminder on any channel, its real email included."""
    ali, orders, row = _reminded_on_day_11(db, clock, mode, sends)
    ali.email = "ali@example.com"
    db.session.commit()
    TelegramReachabilityService.mark_blocked(ali, source="my_chat_member", at=at(11, 10, 3))
    db.session.commit()
    clock(at(11, 10, 5))

    assert SEND.run(row.id, 1) == "failed:blocked"
    assert telegram == []


def test_a_reply_since_the_run_makes_the_send_not_active(db, lock, clock, mode, sends, telegram):
    ali, orders, row = _reminded_on_day_11(db, clock, mode, sends)
    ReorderReminderActions.on_customer_reply(ali.id, excerpt="Not now, thanks", now=at(11, 10, 3))
    clock(at(11, 10, 5))

    assert SEND.run(row.id, 1) == "failed:not_active"
    assert only_lapse(orders[-1]).escalation_flag == "replied"


def test_a_telegram_send_goes_through_the_wired_adapter_to_the_customer_bot(db, lock, clock, mode, sends, bot_webhook):
    """Task 15a wired `_send_telegram`: once the re-check passes, the reminder is POSTed to the
    customer bot's route under its stable request id and recorded once, on the run-time channel.
    Only the HTTP hop is replaced, by the `bot_webhook` recorder with `trigger_bot_webhook`'s
    exact signature."""
    ali, orders, row = _reminded_on_day_11(db, clock, mode, sends)
    clock(at(11, 10, 5))

    assert SEND.run(row.id, 1) == "sent"

    posted = [
        (call["endpoint"], call["payload"]["telegram_id"], call["payload"]["episode_id"], call["payload"]["step"],
         call["request_id"])
        for call in bot_webhook.calls
    ]
    assert posted == [("/internal/reorder-reminder", ali.telegram_id, row.id, 1, f"reorder-reminder:{row.id}:1")]
    assert event_rows(episode=row)[-1] == ("reminder_sent", None, 1, "telegram")


def test_a_blocked_bot_is_recorded_at_once_never_retried_and_marks_the_account(
    db, lock, clock, mode, sends, bot_webhook, monkeypatch
):
    """Spec §7.2 c, §12: the bot route's 410 is final. `bot_blocked` is recorded on the first
    attempt, the task does not retry, and this account (only) is marked blocked."""
    ali, orders, row = _reminded_on_day_11(db, clock, mode, sends)
    bot_webhook.answer = BOT_BLOCKED
    retries = _record_retries(monkeypatch)
    clock(at(11, 10, 5))

    assert SEND.run(row.id, 1) == "failed:bot_blocked"

    assert retries == []
    assert event_rows(episode=row)[-1] == ("reminder_failed", "bot_blocked", 1, None)
    assert [event for event in event_rows(user=ali) if event[0] == "blocked"] == [("blocked", "send_410", None, None)]
    db.session.refresh(ali)
    assert (ali.is_bot_active, ensure_utc(ali.telegram_blocked_at)) == (False, at(11, 10, 5))


def test_a_retry_the_bot_deduplicates_records_the_send_its_first_attempt_delivered(
    db, lock, clock, mode, sends, bot_webhook, monkeypatch
):
    """The first POST reached the bot and the reminder went out, but the answer never came back (a
    timeout): retryable, nothing recorded. The retry carries the same request id, so the bot
    answers "Already processed (deduplicated)" with success and sends nothing. No `reminder_sent`
    exists yet, so the step is recorded sent: the earlier attempt delivered it. One message, one
    record."""
    ali, orders, row = _reminded_on_day_11(db, clock, mode, sends)
    clock(at(11, 10, 5))
    retries = _record_retries(monkeypatch)
    bot_webhook.answer = {"success": False, "message": "Bot webhook timeout"}

    with pytest.raises(Retry):
        SEND.run(row.id, 1)
    bot_webhook.answer = BOT_DEDUPLICATED
    SEND.push_request(retries=1)
    try:
        assert SEND.run(row.id, 1) == "sent"
    finally:
        SEND.pop_request()

    assert [(call["exc"].detail, call["countdown"]) for call in retries] == [("telegram_error", 60)]
    assert [call["request_id"] for call in bot_webhook.calls] == [f"reorder-reminder:{row.id}:1"] * 2
    assert [kind for kind, *_rest in event_rows(episode=row)] == ["opened", "reminder_queued", "reminder_sent"]
    assert only_lapse(orders[-1]).reminders_sent == 1


def test_a_deduplicated_answer_after_a_racing_copy_recorded_the_send_records_nothing(
    db, lock, clock, mode, sends, bot_webhook
):
    """Two copies of one send (the original and a re-published copy) both pass the re-check. The
    copy's POST reaches the bot first: the bot sends it, and the copy records the step. The
    original's POST, under the same request id, is answered "Already processed (deduplicated)"
    with success. The original finds the copy's `reminder_sent` under the row lock and records
    nothing: one message, one record, no phantom failure."""
    ali, orders, row = _reminded_on_day_11(db, clock, mode, sends)
    clock(at(11, 10, 5))
    copy_results = []

    def answer(call):
        if len(bot_webhook.calls) == 1:  # the original's POST is in flight: the copy runs and commits now
            copy_results.append(SEND.run(row.id, 1))
            return BOT_DEDUPLICATED
        return BOT_DELIVERED

    bot_webhook.answer = answer

    assert SEND.run(row.id, 1) == "failed:already_sent"

    assert copy_results == ["sent"]
    original, copy = bot_webhook.calls
    assert (original["request_id"], copy["request_id"]) == (f"reorder-reminder:{row.id}:1",) * 2
    assert original["payload"] == copy["payload"]
    assert [kind for kind, *_rest in event_rows(episode=row)] == ["opened", "reminder_queued", "reminder_sent"]
    assert only_lapse(orders[-1]).reminders_sent == 1


@pytest.fixture
def brevo(monkeypatch, email_claims):
    """`NotificationService.send_notification` replaced by a recorder with its exact signature
    (`email_headers` included). It answers as an accepted Brevo send; a test sets `.answer` to the
    email result the real path returns for a failure (tests/integration/test_reorder_reminder_email.py
    drives that path through `requests.post`). The email claims go to the `email_claims` FakeRedis."""
    state = SimpleNamespace(calls=[], answer={"success": True, "message_id": "<brevo-1>", "status_code": 201})

    def send_notification(_self, user_id, notification_type, channels=None, template_data=None,
                          priority="normal", template_override=None, campaign_id=None, email_headers=None,
                          language=None):
        state.calls.append({
            "user_id": user_id,
            "notification_type": notification_type,
            "channels": channels,
            "template_data": dict(template_data or {}),
            "email_headers": email_headers,
            "language": language,
        })
        return {NotificationChannel.EMAIL.value: state.answer}

    monkeypatch.setattr(NotificationService, "send_notification", send_notification)
    return state


def test_an_email_send_goes_through_the_wired_adapter_with_its_unsubscribe_header(db, lock, clock, mode, sends, brevo):
    """Task 18a wired `_send_email`: once the re-check passes, ONE reorder-reminder email goes to
    the run-time recipient in their language, its List-Unsubscribe header naming the body's own
    link, and the send is recorded once, on the email channel."""
    seed_translations(db, reminder_copy_keys())
    ali, orders, row = _reminded_on_day_11(db, clock, mode, sends, email="ali@example.com")
    assert event_rows(episode=row)[-1] == ("reminder_queued", None, 1, "email")
    clock(at(11, 10, 5))

    assert SEND.run(row.id, 1) == "sent"

    (call,) = brevo.calls
    data = call["template_data"]
    url = data.pop("unsubscribe_url")
    assert data.pop("shop_url").endswith("/shop")
    assert (call["user_id"], call["notification_type"], call["channels"]) == (
        ali.id, NotificationType.REORDER_REMINDER, [NotificationChannel.EMAIL],
    )
    assert call["email_headers"] == unsubscribe_headers(url)
    assert read_unsubscribe_token(urlsplit(url).path.rsplit("/", 1)[1]) == ali.id
    assert urlsplit(url).query == "lang=uz"
    # The template and the subject are rendered in THE reminder language the copy is written in.
    assert call["language"] == "uz"
    # Ali's stored language is uz; her order has no lines, so the copy offers water.
    assert data == {
        "greeting": "Assalomu alaykum, Ali!",
        "statement": (
            "Assalomu alaykum, Ali! Oxirgi suv yetkazib berilganiga 11 kun boʻldi, "
            "odatda esa taxminan har 7 kunda buyurtma berasiz."
        ),
        "cta_line": "Saytimizda yoki Telegram-botimizda buyurtma bering — odatdagidek suv olib kelamiz.",
        "btn_website": "Saytda buyurtma berish",
        "btn_telegram": "Telegramda buyurtma berish",
        "unsubscribe_line": "Bunday eslatmalarni olishni xohlamaysizmi? Obunani bekor qilish",
    }
    assert event_rows(episode=row)[-1] == ("reminder_sent", None, 1, "email")
    assert only_lapse(orders[-1]).reminders_sent == 1


def test_a_brevo_failure_is_recorded_at_once_and_never_retried(db, lock, clock, mode, sends, brevo, monkeypatch):
    """Spec §12 "Email: one attempt": even a Brevo 503 is final. The task does not retry, and the
    failure names the status."""
    ali, orders, row = _reminded_on_day_11(db, clock, mode, sends, email="ali@example.com")
    brevo.answer = {"success": False, "error": "Email API error: Service Unavailable", "status_code": 503}
    retries = _record_retries(monkeypatch)
    clock(at(11, 10, 5))

    assert SEND.run(row.id, 1) == "failed:email_503"

    assert retries == []
    assert len(brevo.calls) == 1
    assert event_rows(episode=row)[-1] == ("reminder_failed", "email_503", 1, None)
    assert only_lapse(orders[-1]).reminders_sent == 0


def test_a_worker_that_died_after_brevo_accepted_leaves_a_claim_the_republished_send_honours(
    db, lock, clock, mode, sends, brevo, email_claims, monkeypatch
):
    """Brevo accepted the email, then the worker died before `reminder_sent` was committed: the event
    log shows the step still queued, so the run re-publishes it. The claim taken before the Brevo
    call stops the copy: no second email, and the step is recorded `already_claimed`."""
    ali, orders, row = _reminded_on_day_11(db, clock, mode, sends, email="ali@example.com")
    clock(at(11, 10, 5))
    real_record_sent = messaging._record_sent
    crashes = [RuntimeError("the worker died before the commit")]

    def record_sent(*args, **kwargs):
        if crashes:
            raise crashes.pop()
        return real_record_sent(*args, **kwargs)

    monkeypatch.setattr(messaging, "_record_sent", record_sent)
    with pytest.raises(RuntimeError):
        SEND.run(row.id, 1)
    assert len(brevo.calls) == 1
    assert event_rows(episode=row)[-1] == ("reminder_queued", None, 1, "email")

    clock(at(11, 10, 40))
    assert SEND.run(row.id, 1) == "failed:already_claimed"

    assert len(brevo.calls) == 1
    assert event_rows(episode=row)[-1] == ("reminder_failed", "already_claimed", 1, None)
    assert set(email_claims.store) == {RedisKeyspace.reorder_reminders_email_claim(row.id, 1)}


def test_the_second_reminder_email_is_not_blocked_by_the_first_ones_claim(
    db, lock, clock, mode, sends, brevo, email_claims
):
    """R1 on day 11, R2 on day 18 (spec §7.5): each step has its own claim, so R2 goes out, the short
    statement under the same greeting."""
    seed_translations(db, reminder_copy_keys())
    ali, orders, row = _reminded_on_day_11(db, clock, mode, sends, email="ali@example.com")
    clock(at(11, 10, 5))
    assert SEND.run(row.id, 1) == "sent"
    clock(run_at(18))
    RUN.run()
    assert sends == [(row.id, 1), (row.id, 2)]
    clock(at(18, 10, 5))

    assert SEND.run(row.id, 2) == "sent"

    assert [call["template_data"]["statement"] for call in brevo.calls] == [
        "Assalomu alaykum, Ali! Oxirgi suv yetkazib berilganiga 11 kun boʻldi, "
        "odatda esa taxminan har 7 kunda buyurtma berasiz.",
        "Assalomu alaykum, Ali! Kichik eslatma.",
    ]
    assert set(email_claims.store) == {RedisKeyspace.reorder_reminders_email_claim(row.id, step) for step in (1, 2)}
    assert event_rows(episode=row)[-1] == ("reminder_sent", None, 2, "email")
    assert only_lapse(orders[-1]).reminders_sent == 2


def test_a_transient_telegram_failure_retries_with_backoff_then_records_the_failure(
    db, lock, clock, mode, sends, monkeypatch
):
    ali, orders, row = _reminded_on_day_11(db, clock, mode, sends)
    clock(at(11, 10, 5))
    monkeypatch.setattr(messaging, "_send_telegram", _telegram_502)
    retries = _record_retries(monkeypatch)

    with pytest.raises(Retry):
        SEND.run(row.id, 1)
    assert [(type(call["exc"]), call["exc"].detail, call["countdown"]) for call in retries] == [
        (RetryableSendError, "telegram_502", 60)
    ]
    assert "reminder_failed" not in [kind for kind, *_rest in event_rows(episode=row)]

    SEND.push_request(retries=3)
    try:
        assert SEND.run(row.id, 1) == "failed:telegram_502"
    finally:
        SEND.pop_request()
    assert event_rows(episode=row)[-1] == ("reminder_failed", "telegram_502", 1, None)


@pytest.mark.parametrize("retries_so_far, countdown", [(1, 120), (2, 240)])
def test_each_telegram_retry_waits_twice_as_long_as_the_one_before(
    db, lock, clock, mode, sends, monkeypatch, retries_so_far, countdown
):
    """60 s, 120 s, 240 s: from a 15:59 run at the latest, the last retry still ends before 18:00."""
    ali, orders, row = _reminded_on_day_11(db, clock, mode, sends)
    clock(at(11, 10, 5))
    monkeypatch.setattr(messaging, "_send_telegram", _telegram_502)
    retries = _record_retries(monkeypatch)

    SEND.push_request(retries=retries_so_far)
    try:
        with pytest.raises(Retry):
            SEND.run(row.id, 1)
    finally:
        SEND.pop_request()

    assert [(call["exc"].detail, call["countdown"]) for call in retries] == [("telegram_502", countdown)]
    assert "reminder_failed" not in [kind for kind, *_rest in event_rows(episode=row)]


def test_the_after_commit_publisher_queues_the_send_task_expiring_at_18_local(db, monkeypatch):
    published = _task_spy(monkeypatch, SEND, "apply_async")
    monkeypatch.setattr("business_app.utils.timezone_utils.get_utc_now", lambda: at(11, 10).astimezone(timezone.utc))

    reorder_reminder_dispatch._publish_send(41, 2)

    assert published == [((41, 2), {}, {"expires": at(11, 18).astimezone(timezone.utc)})]
