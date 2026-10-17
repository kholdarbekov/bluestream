"""The daily run, part 2: the reminder ladder over time (spec §4 ladder table, §6, §12, §13, §15.3)."""

import pytest

from business_app.models.reorder_reminder import ReorderReminderEvent
from business_app.services.reorder_reminders import engine as engine_module
from business_app.services.reorder_reminders.events import record_event_at
from business_app.services.reorder_reminders.settings import ReorderReminderSettingsService as Settings
from business_app.utils.timezone_utils import ensure_utc
from shared.enums import OrderStatus
from tests.integration.reorder_reminder_builders import (  # noqa: F401 - `*_fixture` are the `store`, `sends` fixtures
    WEEKLY,
    at,
    captured_logs,
    connect_telegram,
    customer_with_deliveries,
    day,
    delivered_order,
    event_rows,
    lapses,
    live_order,
    make_customer,
    make_episode,
    only_lapse,
    place,
    run_at,
    run_engine,
    sends_fixture,
    set_status,
    store_fixture,
)


LADDER = [
    ("weekly", WEEKLY, 11, 18, 25),
    ("every two weeks", (-42, -28, -14, 0), 22, 29, 36),
    ("monthly", (-90, -60, -30, 0), 46, 53, 60),
    ("preset", (0,), 31, 38, 45),
]


@pytest.mark.parametrize("label, deliveries, r1, r2, escalation", LADDER, ids=[row[0] for row in LADDER])
def test_the_default_ladder_reminds_twice_then_escalates(db, sends, label, deliveries, r1, r2, escalation):
    ali, orders = customer_with_deliveries(db, deliveries, telegram=True)

    run_engine(r1 - 1)
    assert lapses(orders[-1]) == []
    run_engine(r1)
    row = only_lapse(orders[-1])
    assert (row.status, row.step, row.next_action_on, sends) == ("open", 1, day(r2), [(row.id, 1)])
    run_engine(r2)
    row = only_lapse(orders[-1])
    assert (row.step, row.next_action_on, sends) == (2, day(escalation), [(row.id, 1), (row.id, 2)])
    run_engine(escalation)

    row = only_lapse(orders[-1])
    assert (row.status, row.escalation_flag, row.next_action_on, row.reminders_sent) == (
        "escalated", "no_response", None, 0,
    )
    assert event_rows(episode=row) == [
        ("opened", None, None, None),
        ("reminder_queued", None, 1, "telegram"),
        ("reminder_queued", None, 2, "telegram"),
        ("escalated", "no_response", None, None),
    ]
    stamps = [
        ensure_utc(event.created_at)
        for event in ReorderReminderEvent.query.filter_by(episode_id=row.id).order_by(ReorderReminderEvent.id)
    ]
    assert stamps == [run_at(r1), run_at(r1), run_at(r2), run_at(escalation)]  # each run's own clock (amendment 5)


def test_a_dry_run_ladder_simulates_both_reminders_and_moves_nothing_early(db, sends):
    ali, orders = customer_with_deliveries(db, WEEKLY, telegram=True)

    for n in (11, 17, 18, 24, 25):
        run_engine(n, mode="dry_run")

    row = only_lapse(orders[-1], is_dry_run=True)
    assert (row.status, row.escalation_flag, row.reminders_sent) == ("escalated", "no_response", 2)
    assert event_rows(episode=row) == [
        ("opened", None, None, None),
        ("reminder_simulated", None, 1, "telegram"),
        ("reminder_simulated", None, 2, "telegram"),
        ("escalated", "no_response", None, None),
    ]
    assert (sends, lapses(orders[-1])) == ([], [])


def test_dry_run_flags_only_a_truly_unreachable_customer_as_no_channel(db, sends):
    """§13: dry-run flags read `reminder_simulated` where live rows read `reminder_sent`."""
    unreachable, orders = customer_with_deliveries(db, WEEKLY, email="telegram_9@bot.internal")

    for n in (11, 18, 25):
        run_engine(n, mode="dry_run")

    row = only_lapse(orders[-1], is_dry_run=True)
    assert (row.escalation_flag, row.reminders_sent) == ("no_channel", 0)
    assert [(kind, detail) for kind, detail, _step, _channel in event_rows(episode=row)] == [
        ("opened", None),
        ("reminder_skipped", "no_channel"),
        ("reminder_skipped", "no_channel"),
        ("escalated", "no_channel"),
    ]


FLAGS = [
    ("unreachable", dict(email="telegram_9@bot.internal"), False, "no_channel", "no_channel"),
    ("switched off", dict(telegram=True), True, "stopped", "stopped"),
    ("every account blocked the bot", dict(blocked_at=at(5)), False, "blocked", "blocked"),
]


@pytest.mark.parametrize("label, fields, stopped, skip_detail, flag", FLAGS, ids=[row[0] for row in FLAGS])
def test_a_customer_who_cannot_be_messaged_still_walks_the_ladder_and_escalates_flagged(
    db, sends, label, fields, stopped, skip_detail, flag
):
    """D5: no message, same timeline, escalated once at D+14 with the reason."""
    ali, orders = customer_with_deliveries(db, WEEKLY, **fields)
    if stopped:
        Settings.set(ali.id, False, actor_kind="customer", via="telegram_button", actor_user_id=ali.id, now=at(5))

    for n in (11, 18, 25):
        run_engine(n)

    row = only_lapse(orders[-1])
    assert (row.status, row.escalation_flag) == ("escalated", flag)
    assert [(kind, detail, step) for kind, detail, step, _channel in event_rows(episode=row)] == [
        ("opened", None, None),
        ("reminder_skipped", skip_detail, 1),
        ("reminder_skipped", skip_detail, 2),
        ("escalated", flag, None),
    ]
    assert sends == []


def test_a_second_run_the_same_day_takes_no_second_step_even_with_an_earlier_clock(db, sends):
    ali, orders = customer_with_deliveries(db, WEEKLY, telegram=True)

    run_engine(11, second=30)
    run_engine(11, second=30)
    run_engine(11, second=5)

    row = only_lapse(orders[-1])
    assert (row.step, row.next_action_on, sends) == (1, day(18), [(row.id, 1)])
    assert [kind for kind, *_rest in event_rows(episode=row)] == ["opened", "reminder_queued"]


def test_a_step_more_than_gap_days_late_is_skipped_as_stale_not_sent_weeks_late(db, sends):
    ali, orders = customer_with_deliveries(db, WEEKLY, telegram=True)
    make_episode(db, ali, anchor=orders[-1], next_action_on=day(1), created_at=run_at(1))

    run_engine(11)

    row = only_lapse(orders[-1])
    assert (row.step, row.next_action_on, sends) == (1, day(18), [])
    assert event_rows(episode=row) == [("reminder_skipped", "stale", 1, None)]


def test_an_order_on_its_way_pauses_the_ladder_and_a_cancelled_one_lets_it_resume(db, sends):
    ali, orders = customer_with_deliveries(db, WEEKLY, telegram=True)
    run_engine(11)
    pending = live_order(db, ali, at(12))

    run_engine(18)
    row = only_lapse(orders[-1])
    assert (row.step, row.next_action_on) == (1, day(18))
    set_status(db, pending, OrderStatus.CANCELLED)
    run_engine(20)

    row = only_lapse(orders[-1])
    assert (row.step, row.next_action_on) == (2, day(27))
    assert [(kind, step) for kind, _detail, step, _channel in event_rows(episode=row)] == [
        ("opened", None), ("reminder_queued", 1), ("reminder_queued", 2),
    ]


def test_a_failed_delivery_after_the_first_reminder_escalates_stuck_order_and_r2_never_goes(db, sends):
    ali, orders = customer_with_deliveries(db, WEEKLY, telegram=True)
    run_engine(11)
    live_order(db, ali, at(13), failed=True)

    run_engine(14)
    run_engine(18)

    row = only_lapse(orders[-1])
    assert (row.status, row.escalation_flag, row.step) == ("escalated", "stuck_order", 1)
    assert [send for send in sends if send[1] == 2] == []


def test_a_send_whose_publish_was_lost_is_published_again_by_the_next_run(db, sends):
    ali, orders = customer_with_deliveries(db, WEEKLY, telegram=True)
    run_engine(11)
    row = only_lapse(orders[-1])
    sends.clear()

    summary = run_engine(12)

    assert (sends, summary.republished) == ([(row.id, 1)], 1)
    record_event_at(at(12, 10, 5), user_id=ali.id, kind="reminder_sent", episode=row, step=1, channel="telegram")
    db.session.commit()
    sends.clear()
    assert (run_engine(13).republished, sends) == (0, [])


def test_the_place_group_example_alice_is_reminded_on_her_rhythm_and_bob_on_his(db, sends):
    """Spec §4 "Place group example (D18)", when nobody orders."""
    alice = make_customer(db, first_name="Alice", telegram=True)
    bob = make_customer(db, first_name="Bob", telegram=True)
    office = place(db, alice, bob)
    alice_orders = [delivered_order(db, alice, at(n), address=office[alice.id]) for n in WEEKLY]
    bob_orders = [delivered_order(db, bob, at(n), address=office[bob.id]) for n in (-150, -120, -90, -60)]

    for n in (11, 18, 25, 45, 46):
        run_engine(n)

    alice_row, bob_row = only_lapse(alice_orders[-1]), only_lapse(bob_orders[-1])
    assert (alice_row.status, alice_row.escalation_flag) == ("escalated", "no_response")
    assert (bob_row.status, bob_row.step, bob_row.due_on, bob_row.supply_order_id) == (
        "open", 1, day(46), alice_orders[-1].id,
    )


def test_when_bob_orders_for_the_office_alices_open_lapse_closes_with_no_second_reminder(db, sends):
    alice = make_customer(db, first_name="Alice", telegram=True)
    bob = make_customer(db, first_name="Bob", telegram=True)
    office = place(db, alice, bob)
    alice_orders = [delivered_order(db, alice, at(n), address=office[alice.id]) for n in WEEKLY]
    run_engine(11)
    delivered_order(db, bob, at(12), address=office[bob.id])

    run_engine(13)
    run_engine(18)

    row = only_lapse(alice_orders[-1])
    assert (row.status, row.close_reason, row.step) == ("closed", "place_resupplied", 1)
    assert [send for send in sends if send[0] == row.id] == [(row.id, 1)]


# --------------------------------------------------------------------------- fitted to Task 11a's engine


def test_an_open_lapse_whose_customer_left_scope_is_never_messaged_even_when_its_close_failed(db, sends, monkeypatch):
    """The channel is trusted only in scope: a scope fact is not a channel fact (§5), so an
    out-of-scope customer's channel can still read `telegram`. Here every order came back and this
    run's close of the lapse failed (a counted error): the customer is out of scope
    (`no_deliveries`) while its lapse is still open and due. It takes no step and nothing is sent;
    the next run closes it."""
    ali, orders = customer_with_deliveries(db, WEEKLY, telegram=True)
    run_engine(11)
    row = only_lapse(orders[-1])
    record_event_at(at(11, 10, 5), user_id=ali.id, kind="reminder_sent", episode=row, step=1, channel="telegram")
    db.session.commit()
    for order in orders:
        set_status(db, order, OrderStatus.RETURNED)
    sends.clear()
    lapse_id, real_close_decision = row.id, engine_module.close_decision

    def close_or_fail(episode, anchor, supply):
        if episode.id == lapse_id:
            raise RuntimeError("corrupt lapse row")
        return real_close_decision(episode, anchor, supply)

    monkeypatch.setattr(engine_module, "close_decision", close_or_fail)

    summary = run_engine(18)

    row = only_lapse(orders[-1])
    assert (row.status, row.step, row.next_action_on) == ("open", 1, day(18))
    assert (summary.errors, summary.queued, summary.skipped, sends) == (1, 0, {"out_of_scope": 1}, [])
    assert [kind for kind, *_rest in event_rows(episode=row)] == ["opened", "reminder_queued", "reminder_sent"]


def test_a_step_that_fails_at_commit_counts_one_error_moves_nothing_and_publishes_nothing(
    db, sends, monkeypatch, caplog
):
    """A ladder unit counts into its own summary, merged only once it commits, and its send is parked
    until then (master Review Focus #4). Here the `reminder_queued` event is refused at commit
    (`ck_reorder_reminder_events_kind`) after the unit had counted and parked its send. The error
    is logged with the customer key and the episode."""
    ali, orders = customer_with_deliveries(db, WEEKLY, telegram=True)
    real_record = engine_module.record_event_at

    def record_then_corrupt(at_, **fields):
        event = real_record(at_, **fields)
        if fields["kind"] == "reminder_queued":
            event.kind = "not_an_event_kind"
        return event

    monkeypatch.setattr(engine_module, "record_event_at", record_then_corrupt)

    with captured_logs(caplog, engine_module.logger):
        summary = run_engine(11)

    row = only_lapse(orders[-1])
    assert (summary.opened, summary.queued, summary.errors) == (1, 0, 1)
    assert (row.status, row.step, row.next_action_on, sends) == ("open", 0, day(11), [])
    assert event_rows(episode=row) == [("opened", None, None, None)]
    assert f"u:{ali.id} episode {row.id}" in caplog.text


def test_a_lapse_whose_customer_reordered_takes_no_step_while_its_close_fails_and_closes_the_next_day(
    store, sends, monkeypatch
):
    """A close unit that fails in a run must never let R2 reach a customer who has just reordered (the
    owner's "never annoying" rule; the stale guard covers lateness only). The step reads the close
    rule's own answer and waits: no event, no advance. The next run's close step closes the lapse."""
    ali, orders = customer_with_deliveries(store, WEEKLY, telegram=True)
    run_engine(11)
    row = only_lapse(orders[-1])
    record_event_at(at(11, 10, 5), user_id=ali.id, kind="reminder_sent", episode=row, step=1, channel="telegram")
    store.session.commit()
    delivered_order(store, ali, at(17))  # the reorder: the lapse's pair is superseded
    history = event_rows(episode=row)
    sends.clear()
    lapse_id = row.id
    real_close = engine_module.ReorderReminderEngine._close_if_superseded

    def close_or_fail(self, episode_id, customer):
        if episode_id == lapse_id:
            raise RuntimeError("corrupt lapse row")
        return real_close(self, episode_id, customer)

    monkeypatch.setattr(engine_module.ReorderReminderEngine, "_close_if_superseded", close_or_fail)

    summary = run_engine(18)  # R2's day

    held = only_lapse(orders[-1])
    assert (held.status, held.step, held.next_action_on, held.reminders_sent) == ("open", 1, day(18), 0)
    assert (summary.errors, summary.skipped, summary.queued, sends) == (1, {"pending_close": 1}, 0, [])
    assert event_rows(episode=held) == history

    monkeypatch.setattr(engine_module.ReorderReminderEngine, "_close_if_superseded", real_close)
    summary = run_engine(19)

    closed = only_lapse(orders[-1])
    assert (closed.status, closed.close_reason, closed.step, summary.closed, summary.errors) == (
        "closed", "ordered", 1, {"ordered": 1}, 0,
    )
    assert event_rows(episode=closed) == history + [("closed", "ordered", None, None)]
    assert sends == []


# --------------------------------------------------------------------------- fix round 1


def test_a_dry_run_lapse_that_reached_the_customer_once_escalates_no_response_not_no_channel(db, sends):
    """§13 and §15.3: a dry-run flag reads `reminder_simulated` where a live one reads `reminder_sent`.
    R1 found no channel; the customer then connected the bot and R2 was simulated. They were reached,
    so the end-of-ladder flag is `no_response`, never a false `no_channel`."""
    ali, orders = customer_with_deliveries(db, WEEKLY)  # no bot and no email: no channel
    run_engine(11, mode="dry_run")
    connect_telegram(db, ali)

    for n in (18, 25):
        run_engine(n, mode="dry_run")

    row = only_lapse(orders[-1], is_dry_run=True)
    assert (row.status, row.escalation_flag, row.reminders_sent) == ("escalated", "no_response", 1)
    assert event_rows(episode=row) == [
        ("opened", None, None, None),
        ("reminder_skipped", "no_channel", 1, None),
        ("reminder_simulated", None, 2, "telegram"),
        ("escalated", "no_response", None, None),
    ]
    assert sends == []


def test_a_live_lapse_whose_second_reminder_was_sent_escalates_no_response_not_no_channel(db, sends):
    """The live twin: R1 found no channel, R2 went out after the customer connected the bot. The
    send task (Task 13) records `reminder_sent`; here it is written at the instant the task would."""
    ali, orders = customer_with_deliveries(db, WEEKLY)
    run_engine(11)
    connect_telegram(db, ali)
    run_engine(18)
    row = only_lapse(orders[-1])
    record_event_at(at(18, 10, 5), user_id=ali.id, kind="reminder_sent", episode=row, step=2, channel="telegram")
    db.session.commit()

    run_engine(25)

    row = only_lapse(orders[-1])
    assert (row.status, row.escalation_flag) == ("escalated", "no_response")
    assert event_rows(episode=row) == [
        ("opened", None, None, None),
        ("reminder_skipped", "no_channel", 1, None),
        ("reminder_queued", None, 2, "telegram"),
        ("reminder_sent", None, 2, "telegram"),
        ("escalated", "no_response", None, None),
    ]
    assert sends == [(row.id, 2)]


def test_a_pause_that_outlasts_the_gap_skips_the_late_reminder_as_stale_and_the_ladder_moves_on(db, sends):
    """§6 stale guard: an order scheduled for day 30 pauses the ladder over R2's day (18). Cancelled on
    day 27, nine days past R2's day and more than GAP_DAYS (7), R2 is skipped `stale`, not sent late,
    and the ladder still reaches its end on time."""
    ali, orders = customer_with_deliveries(db, WEEKLY, telegram=True)
    run_engine(11)
    row = only_lapse(orders[-1])
    record_event_at(at(11, 10, 5), user_id=ali.id, kind="reminder_sent", episode=row, step=1, channel="telegram")
    db.session.commit()
    scheduled = live_order(db, ali, at(12), delivery_date=day(30))
    sends.clear()

    paused = [run_engine(n).skipped for n in (18, 25)]
    set_status(db, scheduled, OrderStatus.CANCELLED)
    run_engine(27)

    row = only_lapse(orders[-1])
    assert paused == [{"paused": 1}, {"paused": 1}]
    assert (row.status, row.step, row.next_action_on) == ("open", 2, day(34))
    assert event_rows(episode=row)[-1] == ("reminder_skipped", "stale", 2, None)
    run_engine(34)

    row = only_lapse(orders[-1])
    assert (row.status, row.escalation_flag) == ("escalated", "no_response")
    assert event_rows(episode=row) == [
        ("opened", None, None, None),
        ("reminder_queued", None, 1, "telegram"),
        ("reminder_sent", None, 1, "telegram"),
        ("reminder_skipped", "stale", 2, None),
        ("escalated", "no_response", None, None),
    ]
    assert sends == []


def test_a_queued_send_is_published_again_thirty_minutes_on_and_not_a_minute_sooner(db, sends):
    """§12 step 8: the cutoff is inclusive, `REPUBLISH_AFTER` or more after the queued stamp."""
    ali, orders = customer_with_deliveries(db, WEEKLY, telegram=True)
    run_engine(11)
    row = only_lapse(orders[-1])

    early = run_engine(11, minute=29)
    on_time = run_engine(11, minute=30)

    assert (early.republished, on_time.republished) == (0, 1)
    assert sends == [(row.id, 1), (row.id, 1)]


SHORT_ESCALATION = [
    ("one reminder", 1, [(11, 1, 14)], 14),
    ("two reminders", 2, [(11, 1, 18), (18, 2, 21)], 21),
]


@pytest.mark.parametrize("label, cap, steps, escalation", SHORT_ESCALATION, ids=[row[0] for row in SHORT_ESCALATION])
def test_the_last_reminder_waits_escalate_after_days_and_every_earlier_one_gap_days(
    db, sends, label, cap, steps, escalation
):
    """§6 Reminder step k, item 3: `next_action_on = today + GAP_DAYS` while k < MAX, else
    `today + ESCALATE_AFTER_DAYS`. ESCALATE_AFTER_DAYS is 3 here, so it differs from GAP_DAYS (7)."""
    tunables = dict(REORDER_REMINDER_MAX_PER_EPISODE=cap, REORDER_REMINDER_ESCALATE_AFTER_DAYS=3)
    ali, orders = customer_with_deliveries(db, WEEKLY, telegram=True)

    for n, step, next_on in steps:
        run_engine(n, **tunables)
        row = only_lapse(orders[-1])
        assert (row.status, row.step, row.next_action_on) == ("open", step, day(next_on))
        # The send task's outcome, so the day-before check run has nothing to re-publish.
        record_event_at(at(n, 10, 5), user_id=ali.id, kind="reminder_sent", episode=row, step=step, channel="telegram")
        db.session.commit()
    run_engine(escalation - 1, **tunables)
    assert only_lapse(orders[-1]).status == "open"
    run_engine(escalation, **tunables)

    row = only_lapse(orders[-1])
    assert (row.status, row.escalation_flag, ensure_utc(row.escalated_at)) == (
        "escalated", "no_response", run_at(escalation),
    )
    assert sends == [(row.id, step) for _n, step, _next_on in steps]
