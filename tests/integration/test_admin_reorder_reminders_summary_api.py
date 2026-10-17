"""The Lapsed-customers summary over HTTP (spec §9.1, plan Task 20b): mode, settings, the badge
counts, the 30-day metrics and the vocabularies, each from the row set the page shows.

Driven through `GET /api/v1/admin/reorder-reminders/summary`, the request the admin UI's page
header, nav badge and Analytics cards send, with a role-claim token. Every event is written by the
real writer (the settings service, the reachability service, `record_event_at`), so a drift in a
writer's detail or payload fails here. The business clock is frozen at `WORKLIST_NOW`, so the
30-day window is the same on every run; the writers stamp events with the wall clock, which the
tests override only where the metric is about an instant (`record_event_at`, `now=`, `_block`).
"""

import logging
from datetime import date, datetime, timedelta, timezone

import pytest

from business_app.models.reorder_reminder import (
    CHANNELS,
    ESCALATION_FLAGS,
    OUTCOMES,
    ReorderReminderEpisode,
    ReorderReminderEvent,
)
from business_app.models.user import User
from business_app.services.reorder_reminders import facts as facts_module
from business_app.services.reorder_reminders.events import record_event, record_event_at
from business_app.services.reorder_reminders.settings import ACTOR_CUSTOMER, DETAIL_OFF, ReorderReminderSettingsService
from business_app.services.reorder_reminders.worklist import logger as worklist_logger
from business_app.services.telegram_reachability_service import TelegramReachabilityService
from shared.enums import UserRole, UserStatus, UserType
from tests.integration.reorder_reminder_builders import (  # noqa: F401 - the `frozen`, `mode` and `sends` fixtures
    WEEKLY,
    WORKLIST_NOW,
    at,
    captured_logs,
    claim_headers,
    customer_with_deliveries,
    deliver,
    delivered_order,
    frozen_fixture,
    link,
    live_order,
    make_customer,
    mode_fixture,
    run_engine,
    sends_fixture,
    staff_episode,
)

pytestmark = pytest.mark.integration

SUMMARY = "/api/v1/admin/reorder-reminders/summary"
EPISODES = "/api/v1/admin/reorder-reminders/episodes"
UTC = timezone.utc
# The frozen business clock in the UTC the columns store (the builders write UTC, as the writers do).
NOW = WORKLIST_NOW.astimezone(UTC)
NO_FLAGS = {flag: 0 for flag in ESCALATION_FLAGS}
HOURS = timedelta(hours=1)
DAYS = timedelta(days=1)


def _summary(client, headers):
    response = client.get(SUMMARY, headers=headers)
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()["data"]


def _metrics(client, headers):
    return _summary(client, headers)["metrics_30d"]


def _tab_total(client, headers, tab):
    response = client.get(EPISODES, query_string={"tab": tab}, headers=headers)
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()["data"]["meta"]["total"]


def _episode(db, name, **fields):
    """A lapse of its own customer (connected to the bot), 60 days after its anchor delivery."""
    customer = make_customer(db, first_name=name, telegram=True)
    anchor_at = NOW - 60 * DAYS
    anchor = delivered_order(db, customer, anchor_at)
    return staff_episode(db, customer, anchor=anchor, due_on=(anchor_at + 31 * DAYS).date(), **fields)


def _event(episode, kind, at, *, channel=None, step=None, **fields):
    return record_event_at(
        at, user_id=episode.user_id, kind=kind, episode=episode, step=step, channel=channel, **fields
    )


def _escalation(episode, flag, when, **fields):
    """The `escalated` event the engine records with an escalation: its flag is the event's detail."""
    return _event(episode, "escalated", when, detail=flag, **fields)


def _block(db, user, *, at, reported_at, source="my_chat_member"):
    """The customer blocked the bot at `at`; Telegram's report reached us at `reported_at`.

    `mark_blocked` writes the event; it stamps `created_at` with the wall clock (the report), so the
    test sets the report instant, which a real late report would carry."""
    assert TelegramReachabilityService.mark_blocked(user, source=source, at=at)
    event = ReorderReminderEvent.query.filter_by(user_id=user.id, kind="blocked").one()
    event.created_at = reported_at
    db.session.commit()


def _reminded(db, name, sent_at):
    """A customer whose open lapse was sent its first reminder at `sent_at`: `(user, episode)`."""
    episode = _episode(db, name, status="open", step=1, reminders_sent=1, next_action_on=date(2026, 10, 20))
    _event(episode, "reminder_sent", sent_at, channel="telegram", step=1)
    db.session.commit()
    return db.session.get(User, episode.user_id), episode


def test_the_summary_publishes_mode_settings_counts_and_vocabularies(client, db, frozen, manager_claim_headers):
    _episode(db, "Anvar", status="escalated", escalation_flag="no_response", step=2, reminders_sent=2)
    _episode(db, "Barno", status="escalated", escalation_flag="long_lapsed")
    _episode(db, "Dono", status="open", step=1, reminders_sent=1, next_action_on=NOW.date())
    _episode(db, "Erkin", status="suspended", suspended_reason="subscription", step=1, reminders_sent=1)
    _episode(db, "Farrux", status="open", step=1, reminders_sent=1, next_action_on=NOW.date(), is_dry_run=True)

    summary = _summary(client, manager_claim_headers)

    assert set(summary) == {
        "mode",
        "row_set_is_dry_run",
        "settings",
        "counts",
        "metrics_30d",
        "outcomes",
        "flags",
        "tabs",
    }
    assert summary["mode"] == "off"
    assert summary["row_set_is_dry_run"] is False
    # Suspended rows are not counted (§9.1); dry-run rows are not the row set in mode `off`.
    assert summary["counts"] == {"follow_up": 2, "reminding": 1}
    assert summary["settings"] == [
        {"key": "fallback_days", "value": 30},
        {"key": "ratio", "value": 1.5},
        {"key": "min_deliveries", "value": 3},
        {"key": "max_per_episode", "value": 2},
        {"key": "gap_days", "value": 7},
        {"key": "snooze_days", "value": 14},
        {"key": "escalate_after_days", "value": 7},
        {"key": "fresh_days", "value": 14},
        {"key": "live_order_grace_days", "value": 7},
        {"key": "local_time", "value": "10:00"},
    ]
    # One count per channel the model knows: a new channel cannot be dropped silently.
    assert set(summary["metrics_30d"]["reminders_sent"]) == set(CHANNELS) == {"telegram", "email"}
    assert summary["outcomes"] == list(OUTCOMES)
    assert summary["flags"] == list(ESCALATION_FLAGS)
    assert summary["tabs"] == ["follow_up", "reminding", "paused", "resolved"]


@pytest.mark.parametrize("mode_name, follow_up, reminding", [("off", 2, 3), ("on", 2, 3), ("dry_run", 1, 2)])
def test_the_badge_counts_are_the_totals_of_the_tabs_they_open(
    client, db, frozen, mode, manager_claim_headers, mode_name, follow_up, reminding
):
    """The nav badge and the Analytics cards (D17) must never disagree with the tab they link to:
    both read the row set of the mode, and neither counts the paused or resolved rows."""
    for index in range(2):
        _episode(db, f"Live escalated {index}", status="escalated", escalation_flag="long_lapsed")
    for index in range(3):
        _episode(db, f"Live open {index}", status="open", step=1, reminders_sent=1, next_action_on=NOW.date())
    _episode(db, "Dry escalated", status="escalated", escalation_flag="long_lapsed", is_dry_run=True)
    for index in range(2):
        _episode(
            db, f"Dry open {index}", status="open", step=1, reminders_sent=1, next_action_on=NOW.date(), is_dry_run=True
        )
    _episode(db, "Paused", status="suspended", suspended_reason="subscription")
    _episode(db, "Resolved", status="resolved", outcome="away")
    mode(mode_name)

    counts = _summary(client, manager_claim_headers)["counts"]

    assert counts == {"follow_up": follow_up, "reminding": reminding}
    assert counts["follow_up"] == _tab_total(client, manager_claim_headers, "follow_up")
    assert counts["reminding"] == _tab_total(client, manager_claim_headers, "reminding")


def test_the_thirty_day_metrics_follow_the_spec_definitions(client, db, frozen, manager_user, manager_claim_headers):
    recent, old = NOW - 2 * DAYS, NOW - 40 * DAYS
    reordered = _episode(db, "R1", status="closed", close_reason="ordered", closed_at=recent, step=1, reminders_sent=1)
    no_reminder = _episode(db, "R2", status="closed", close_reason="ordered", closed_at=recent)
    superseded = _episode(
        db, "R3", status="closed", close_reason="superseded", closed_at=recent, step=1, reminders_sent=1
    )
    resupplied = _episode(
        db, "R4", status="closed", close_reason="place_resupplied", closed_at=recent, step=1, reminders_sent=1
    )
    before_contact = _episode(
        db,
        "R5",
        status="closed",
        close_reason="ordered",
        closed_at=recent,
        escalation_flag="no_response",
        escalated_at=recent - DAYS,
        step=2,
        reminders_sent=2,
    )
    stuck_delivered = _episode(
        db,
        "R6",
        status="closed",
        close_reason="ordered",
        closed_at=recent,
        escalation_flag="stuck_order",
        escalated_at=recent - DAYS,
    )
    long_ago = _episode(db, "R7", status="escalated", escalation_flag="long_lapsed", escalated_at=old)
    # Not counted: a reminded lapse closed 40 days ago (the close window is on `closed_at`), and a
    # lapse resupplied by another member with no reminder sent (D18: "after at least one reminder").
    closed_long_ago = _episode(
        db, "R8", status="closed", close_reason="ordered", closed_at=old, step=1, reminders_sent=1
    )
    _episode(db, "R9", status="closed", close_reason="place_resupplied", closed_at=recent)
    for episode, kind, channel, when in (
        (reordered, "opened", None, recent),
        (no_reminder, "opened", None, recent),
        (long_ago, "opened", None, old),
        (reordered, "reminder_sent", "telegram", recent),
        (superseded, "reminder_sent", "telegram", recent),
        (resupplied, "reminder_sent", "email", recent),
        (before_contact, "reminder_sent", "telegram", old),
        (closed_long_ago, "reminder_sent", "telegram", old),
    ):
        _event(episode, kind, when, channel=channel, step=1 if channel else None)
    _escalation(before_contact, "no_response", recent - DAYS)
    _escalation(stuck_delivered, "stuck_order", recent - DAYS)
    _escalation(long_ago, "long_lapsed", old)
    db.session.commit()
    switcher = make_customer(db, first_name="Switcher")
    # Counted: a customer's real off (detail "off", payload changed_by_kind "customer").
    ReorderReminderSettingsService.set(
        switcher.id, False, actor_kind="customer", via="web_settings", now=NOW - 3 * DAYS
    )
    ReorderReminderSettingsService.set(switcher.id, True, actor_kind="customer", via="web_settings", now=NOW - 2 * DAYS)
    # Not counted: an admin off.
    ReorderReminderSettingsService.set(
        long_ago.user_id,
        False,
        actor_kind="admin",
        via="admin_ui",
        reason="Asked by phone",
        actor_user_id=manager_user.id,
        now=NOW - 2 * DAYS,
    )
    # Not counted: the customer then taps Stop over that admin-off. D20 records the attempt as
    # `already_off` with a customer payload and changes nothing (amendment 16).
    ReorderReminderSettingsService.set(
        long_ago.user_id, False, actor_kind="customer", via="telegram_button", now=NOW - DAYS
    )
    attempt = ReorderReminderEvent.query.filter_by(
        user_id=long_ago.user_id, kind="setting_changed", detail="already_off"
    ).one()
    assert attempt.payload["changed_by_kind"] == "customer"
    # The reminded customer blocked the bot a day after the reminder; the never-reminded one did too.
    _block(db, db.session.get(User, reordered.user_id), at=NOW - DAYS, reported_at=NOW - DAYS + HOURS)
    _block(db, db.session.get(User, no_reminder.user_id), at=NOW - DAYS, reported_at=NOW - DAYS + HOURS)

    metrics = _metrics(client, manager_claim_headers)

    assert metrics == {
        "lapses_started": 2,
        "reminders_sent": {"telegram": 2, "email": 1},
        "reordered_after_reminder": 1,
        "place_resupplied": 1,
        "reordered_before_contact": 1,
        "stuck_order_delivered": 1,
        "escalated_by_flag": {**NO_FLAGS, "no_response": 1, "stuck_order": 1},
        "switched_off_by_customers": 1,
        "blocks_after_reminder": 1,
    }


def test_a_customer_who_switches_off_on_and_off_again_is_one_customer(client, db, frozen, manager_claim_headers):
    """The metric counts customers, not clicks: off, back on and off again is one customer, and a
    second customer's single off makes two. An off older than the window is not in it."""
    flapper, steady, long_gone = (make_customer(db, first_name=name) for name in ("Flapper", "Steady", "Gone"))
    for user, enabled, days_ago in (
        (flapper, False, 6),
        (flapper, True, 5),
        (flapper, False, 4),
        (steady, False, 3),
        (long_gone, False, 40),
    ):
        ReorderReminderSettingsService.set(
            user.id, enabled, actor_kind="customer", via="web_settings", now=NOW - days_ago * DAYS
        )
    flapper_offs = ReorderReminderEvent.query.filter_by(
        user_id=flapper.id, kind="setting_changed", detail=DETAIL_OFF
    ).count()
    assert flapper_offs == 2  # two real offs are two rows; they are one customer

    assert _metrics(client, manager_claim_headers)["switched_off_by_customers"] == 2


def test_two_linked_accounts_of_one_person_switching_off_are_one_customer(client, db, frozen, manager_claim_headers):
    """The customer is the linked person, not the account. The person switched reminders off in the
    bot, back on, and later off again from their web account: two real `off` events on two accounts
    (the effective setting is shared, so the second off is a real write only because of the
    re-enable), one customer."""
    bot_account, web_account = make_customer(db, first_name="Bot"), make_customer(db, first_name="Web")
    link(db, bot_account, web_account)
    for user, enabled, days_ago in ((bot_account, False, 6), (bot_account, True, 5), (web_account, False, 4)):
        ReorderReminderSettingsService.set(
            user.id, enabled, actor_kind="customer", via="web_settings", now=NOW - days_ago * DAYS
        )
    off_events = ReorderReminderEvent.query.filter_by(kind="setting_changed", detail=DETAIL_OFF).all()
    assert sorted(event.user_id for event in off_events) == sorted([bot_account.id, web_account.id])

    assert _metrics(client, manager_claim_headers)["switched_off_by_customers"] == 1


def test_two_unlinked_people_switching_off_are_two_customers(client, db, frozen, manager_claim_headers):
    """Two unrelated, unlinked accounts are two customers."""
    first, second = make_customer(db, first_name="First"), make_customer(db, first_name="Second")
    for index, user in enumerate((first, second)):
        ReorderReminderSettingsService.set(
            user.id, False, actor_kind="customer", via="telegram_button", now=NOW - (index + 1) * DAYS
        )

    assert _metrics(client, manager_claim_headers)["switched_off_by_customers"] == 2


@pytest.mark.parametrize(
    "mode_name, sent_by_channel, flag",
    [
        ("off", {"telegram": 2, "email": 0}, "stopped"),
        ("on", {"telegram": 2, "email": 0}, "stopped"),
        ("dry_run", {"telegram": 0, "email": 2}, "no_channel"),
    ],
)
def test_every_metric_reads_the_row_set_of_the_mode(
    client, db, frozen, mode, manager_claim_headers, mode_name, sent_by_channel, flag
):
    """Live rows in `off` and `on`, dry-run rows in `dry_run` (§13): the episode metrics, the sends
    (`reminder_sent` live, `reminder_simulated` dry-run) and the blocks after them. A real customer
    action is never dry-run (§10), so the switch-off count is the same in every mode."""
    recent = NOW - 2 * DAYS
    for dry, kind, channel in ((False, "reminder_sent", "telegram"), (True, "reminder_simulated", "email")):
        open_row = _episode(
            db, f"Open {dry}", status="open", step=1, reminders_sent=1, next_action_on=NOW.date(), is_dry_run=dry
        )
        _event(open_row, "opened", NOW - 5 * DAYS)
        _event(open_row, kind, recent, channel=channel, step=1)
        closed_row = _episode(
            db, f"Closed {dry}", status="closed", close_reason="ordered", closed_at=recent, is_dry_run=dry
        )
        _event(closed_row, kind, recent, channel=channel, step=1)
        escalated_row = _episode(
            db,
            f"Escalated {dry}",
            status="escalated",
            escalation_flag="no_channel" if dry else "stopped",
            escalated_at=recent,
            is_dry_run=dry,
        )
        _escalation(escalated_row, "no_channel" if dry else "stopped", recent)
        _block(db, db.session.get(User, open_row.user_id), at=NOW - DAYS, reported_at=NOW - DAYS + HOURS)
        if dry:
            # Impossible through the writers (a setting change has no episode), which is the point of
            # the rule: even such an event is not a customer's real switch-off.
            _event(
                open_row, "setting_changed", NOW - DAYS, detail=DETAIL_OFF, payload={"changed_by_kind": ACTOR_CUSTOMER}
            )
    switcher = make_customer(db, first_name="Switcher")
    ReorderReminderSettingsService.set(switcher.id, False, actor_kind="customer", via="web_settings", now=NOW - DAYS)
    db.session.commit()
    mode(mode_name)

    summary = _summary(client, manager_claim_headers)

    assert summary["row_set_is_dry_run"] is (mode_name == "dry_run")
    assert summary["counts"] == {"follow_up": 1, "reminding": 1}
    assert summary["metrics_30d"] == {
        "lapses_started": 1,
        "reminders_sent": sent_by_channel,
        "reordered_after_reminder": 1,
        "place_resupplied": 0,
        "reordered_before_contact": 0,
        "stuck_order_delivered": 0,
        "escalated_by_flag": {**NO_FLAGS, flag: 1},
        "switched_off_by_customers": 1,
        "blocks_after_reminder": 1,
    }


def test_a_lapse_that_escalates_again_counts_once_under_each_flag(client, db, frozen, manager_claim_headers):
    """§9.1: *Escalated, by flag* counts `escalated` EVENTS. A lapse escalated `long_lapsed`, was
    suspended for its outlet and resumed as the dormant outlet's row (amendment 18, a second
    escalation of the same lapse); its column now says only `outlet_dormant`, which alone would
    have counted it once and under the wrong flag for the first escalation."""
    row = _episode(db, "Twice", status="escalated", escalation_flag="outlet_dormant", escalated_at=NOW - 2 * DAYS)
    _event(row, "opened", NOW - 12 * DAYS)
    _escalation(row, "long_lapsed", NOW - 10 * DAYS)
    _event(row, "suspended", NOW - 8 * DAYS, detail="outlet")
    _event(row, "resumed", NOW - 2 * DAYS, detail="escalated")
    _escalation(row, "outlet_dormant", NOW - 2 * DAYS, payload={"outlets": []})
    # An escalation from before the window is not in it, whatever the lapse did since.
    _escalation(row, "no_response", NOW - 40 * DAYS)
    db.session.commit()

    metrics = _metrics(client, manager_claim_headers)

    assert metrics["escalated_by_flag"] == {**NO_FLAGS, "long_lapsed": 1, "outlet_dormant": 1}


def test_the_escalations_the_engine_records_are_counted_by_their_flag(client, db, frozen, sends, manager_claim_headers):
    """The real daily run: a long-lapsed customer and one whose only live order is stuck are each
    escalated with their flag as the event's detail, which is what the metric groups by."""
    run_day = 220  # 2026-10-08, inside the 30 days before the frozen 15 October
    # The engine tests' shapes (their run is day 11), moved to the run day.
    late_days = [day + run_day - 11 for day in (-36, -29, -22, -15)]  # weekly, 15 days past due: long lapsed
    customer_with_deliveries(db, late_days, telegram=True, first_name="Late")
    stuck_days = [day + run_day - 11 for day in WEEKLY]  # weekly, last delivery 11 days ago
    stuck, _ = customer_with_deliveries(db, stuck_days, telegram=True, first_name="Stuck")
    live_order(db, stuck, at(run_day - 10))  # ten days old, no delivery date: stuck

    summary = run_engine(run_day)

    assert (summary.opened, summary.errors, summary.escalated) == (2, 0, {"stuck_order": 1, "long_lapsed": 1})
    metrics = _metrics(client, manager_claim_headers)
    assert metrics["lapses_started"] == 2
    assert metrics["escalated_by_flag"] == {**NO_FLAGS, "stuck_order": 1, "long_lapsed": 1}


def test_a_block_reported_late_is_timed_by_when_it_happened(client, db, frozen, manager_claim_headers):
    """F14: the 72 h window runs from the reminder to the block INSTANT (`payload["at"]`), not to the
    moment Telegram's report reached us. This block happened 71 h after the reminder and was reported
    5 days later (a restart dropped the update, the recovery middleware told us)."""
    sent_at = NOW - 10 * DAYS
    user, _ = _reminded(db, "Late", sent_at)
    _block(db, user, at=sent_at + 71 * HOURS, reported_at=sent_at + 5 * DAYS)

    assert _metrics(client, manager_claim_headers)["blocks_after_reminder"] == 1


def test_a_block_that_happened_before_the_reminder_is_not_after_it_however_late_it_was_reported(
    client, db, frozen, manager_claim_headers
):
    """The other way round: the customer blocked the bot an hour BEFORE the reminder and Telegram's
    report arrived an hour AFTER it, inside 72 h. It is not a block after the reminder."""
    sent_at = NOW - 10 * DAYS
    user, _ = _reminded(db, "Early", sent_at)
    _block(db, user, at=sent_at - HOURS, reported_at=sent_at + HOURS)

    assert _metrics(client, manager_claim_headers)["blocks_after_reminder"] == 0


def test_the_seventy_two_hour_window_is_inclusive_and_a_block_counts_once(client, db, frozen, manager_claim_headers):
    sent_at = NOW - 10 * DAYS
    edge, _ = _reminded(db, "Edge", sent_at)
    _block(db, edge, at=sent_at + 72 * HOURS, reported_at=sent_at + 72 * HOURS)
    just_after, _ = _reminded(db, "Just after", sent_at)
    _block(db, just_after, at=sent_at + 72 * HOURS + timedelta(minutes=1), reported_at=sent_at + 73 * HOURS)
    # Two reminders inside the 72 h before one block: still one block.
    twice, episode = _reminded(db, "Twice", sent_at)
    _event(episode, "reminder_sent", sent_at + 24 * HOURS, channel="email", step=2)
    db.session.commit()
    _block(db, twice, at=sent_at + 48 * HOURS, reported_at=sent_at + 48 * HOURS)

    assert _metrics(client, manager_claim_headers)["blocks_after_reminder"] == 2


def test_a_block_is_in_the_thirty_day_window_by_when_it_happened(client, db, frozen, manager_claim_headers):
    """A block from 31 days ago that was only reported yesterday is not one of the last 30 days'."""
    sent_at = NOW - 31 * DAYS - HOURS
    user, _ = _reminded(db, "Ancient", sent_at)
    _block(db, user, at=NOW - 31 * DAYS, reported_at=NOW - DAYS)

    assert _metrics(client, manager_claim_headers)["blocks_after_reminder"] == 0


def test_a_block_whose_instant_is_unreadable_costs_only_that_block(client, db, frozen, caplog, manager_claim_headers):
    """The reachability writer's payload is pinned end to end above; an event whose payload drifted
    anyway is skipped and logged with its id, and the summary still answers."""
    sent_at = NOW - 10 * DAYS
    good, _ = _reminded(db, "Good", sent_at)
    _block(db, good, at=sent_at + HOURS, reported_at=sent_at + HOURS)
    broken, _ = _reminded(db, "Broken", sent_at)
    drifted = record_event_at(
        sent_at + HOURS, user_id=broken.id, kind="blocked", detail="my_chat_member", payload={"when": 1}
    )
    db.session.commit()

    with captured_logs(caplog, worklist_logger, level=logging.WARNING):
        metrics = _metrics(client, manager_claim_headers)

    assert metrics["blocks_after_reminder"] == 1
    # A warning, not an error: the page polls this endpoint, so a drifted event must not page anyone
    # on every poll.
    logged = [r for r in caplog.records if f"`blocked` event {drifted.id} has an unreadable instant" in r.getMessage()]
    assert [record.levelno for record in logged] == [logging.WARNING]


def test_blocks_are_only_matched_to_reminders_when_someone_blocked(
    client, db, frozen, count_queries, manager_claim_headers
):
    """The summary is polled by every open page: with no block in the window it reads no reminder
    sends at all, and with one it reads them once, for the users who blocked."""
    sent_at = NOW - 10 * DAYS
    user, _ = _reminded(db, "Soon blocked", sent_at)
    _reminded(db, "Never blocked", sent_at)

    with count_queries() as without_blocks:
        assert _metrics(client, manager_claim_headers)["blocks_after_reminder"] == 0
    _block(db, user, at=sent_at + HOURS, reported_at=sent_at + HOURS)
    with count_queries() as with_block:
        assert _metrics(client, manager_claim_headers)["blocks_after_reminder"] == 1

    assert with_block.count == without_blocks.count + 1


def test_a_stuck_order_lapse_the_engine_closes_after_its_delivery_is_counted_as_delivered(
    client, db, frozen, sends, manager_claim_headers
):
    """The convention the metric reads: a close keeps the lapse's `escalated_at` and
    `escalation_flag`. The real run escalates the customer's only live order as stuck, the order is
    delivered the next morning, and the next real run closes the lapse `ordered`."""
    run_day = 220  # 2026-10-08, inside the 30 days before the frozen 15 October
    stuck_days = [day + run_day - 11 for day in WEEKLY]  # weekly, last delivery 11 days ago
    stuck, _ = customer_with_deliveries(db, stuck_days, telegram=True, first_name="Stuck")
    stuck_order = live_order(db, stuck, at(run_day - 10))  # ten days old, no delivery date: stuck
    run_engine(run_day)
    lapse = ReorderReminderEpisode.query.filter_by(user_id=stuck.id).one()
    assert (lapse.status, lapse.escalation_flag) == ("escalated", "stuck_order")

    deliver(db, stuck_order, at(run_day + 1, 9))
    run_engine(run_day + 1)

    db.session.refresh(lapse)
    assert (lapse.status, lapse.close_reason, lapse.escalation_flag) == ("closed", "ordered", "stuck_order")
    assert lapse.escalated_at is not None
    metrics = _metrics(client, manager_claim_headers)
    assert metrics["stuck_order_delivered"] == 1
    assert (metrics["reordered_before_contact"], metrics["reordered_after_reminder"]) == (0, 0)
    assert metrics["escalated_by_flag"] == {**NO_FLAGS, "stuck_order": 1}


def test_switch_offs_are_counted_per_customer_across_batches_of_accounts(
    client, db, frozen, monkeypatch, manager_claim_headers
):
    """The accounts' identities are read in batches of ids. With a batch of two, a linked pair is
    split across two batches and still makes one customer: three single accounts and the pair are
    four customers."""
    monkeypatch.setattr(facts_module, "_CHUNK", 2)
    first = make_customer(db, first_name="First")
    bot_account, web_account = make_customer(db, first_name="Bot"), make_customer(db, first_name="Web")
    link(db, bot_account, web_account)
    second, third = make_customer(db, first_name="Second"), make_customer(db, first_name="Third")
    for index, (user, enabled) in enumerate(
        [
            (first, False),
            (bot_account, False),
            (bot_account, True),
            (web_account, False),
            (second, False),
            (third, False),
        ]
    ):
        ReorderReminderSettingsService.set(
            user.id, enabled, actor_kind="customer", via="web_settings", now=NOW - (10 - index) * HOURS
        )
    batches = list(facts_module.id_chunks([first.id, bot_account.id, web_account.id, second.id, third.id]))
    assert [bot_account.id in batch for batch in batches] == [True, False, False]
    assert web_account.id in batches[1]

    assert _metrics(client, manager_claim_headers)["switched_off_by_customers"] == 4


@pytest.mark.parametrize("headers_fixture", ["admin_claim_headers", "manager_claim_headers"])
def test_admins_and_managers_read_the_summary(client, db, frozen, request, headers_fixture):
    _episode(db, "Adham", status="escalated", escalation_flag="long_lapsed")

    summary = _summary(client, request.getfixturevalue(headers_fixture))

    assert summary["counts"] == {"follow_up": 1, "reminding": 0}


def test_a_request_without_a_token_is_refused(client, db):
    assert client.get(SUMMARY).status_code == 401


@pytest.mark.parametrize(
    "fixture_name, role",
    [("sample_user", "customer"), ("operator_user", "operator"), ("delivery_driver", "delivery_driver")],
)
def test_customers_operators_and_drivers_are_refused(app, client, db, request, fixture_name, role):
    user = request.getfixturevalue(fixture_name)

    response = client.get(SUMMARY, headers=claim_headers(app, user, role))

    assert response.status_code == 403


def test_the_worklist_and_the_summary_run_on_postgres(pg_app, pg_db):
    """Every summary query on the real database: the row-set counts, the grouped and subquery
    metrics, the customer keys of linked accounts, and the JSON payloads of the setting and block
    events read back from Postgres."""
    admin = User(
        email="pg-worklist-admin@example.com",
        phone="+998901239555",
        password_hash="!pg-worklist",
        first_name="Pg",
        last_name="Admin",
        user_type=UserType.STAFF,
        role=UserRole.ADMIN,
        status=UserStatus.ACTIVE,
        is_verified=True,
    )
    pg_db.session.add(admin)
    pg_db.session.commit()
    now = datetime.now(UTC)
    customer = make_customer(pg_db, first_name="Postgres", telegram=True)
    anchor = delivered_order(pg_db, customer, datetime(2026, 8, 1, 7, 0, tzinfo=UTC))
    episode = staff_episode(
        pg_db,
        customer,
        anchor=anchor,
        status="escalated",
        escalation_flag="long_lapsed",
        escalated_at=now - 3 * DAYS,
        due_on=date(2026, 9, 1),
    )
    record_event(user_id=customer.id, kind="opened", episode=episode)
    _escalation(episode, "long_lapsed", now - 3 * DAYS)
    reminded = make_customer(pg_db, first_name="Reminded", telegram=True)
    reminded_anchor = delivered_order(pg_db, reminded, datetime(2026, 8, 2, 7, 0, tzinfo=UTC))
    closed = staff_episode(
        pg_db,
        reminded,
        anchor=reminded_anchor,
        status="closed",
        close_reason="ordered",
        closed_at=now - DAYS,
        due_on=date(2026, 9, 2),
        reminders_sent=1,
    )
    record_event_at(
        now - 2 * DAYS, user_id=reminded.id, kind="reminder_sent", episode=closed, step=1, channel="telegram"
    )
    pg_db.session.commit()
    TelegramReachabilityService.mark_blocked(reminded, source="my_chat_member", at=now - DAYS)
    pg_db.session.commit()
    # One person with two accounts switches off from both (with a re-enable between): one customer.
    sibling = make_customer(pg_db, first_name="Sibling")
    link(pg_db, reminded, sibling)
    for user, enabled, days_ago in ((reminded, False, 3), (reminded, True, 2), (sibling, False, 1)):
        ReorderReminderSettingsService.set(
            user.id, enabled, actor_kind="customer", via="web_settings", now=now - days_ago * DAYS
        )
    headers = claim_headers(pg_app, admin, "admin")
    client = pg_app.test_client()

    rows = client.get(EPISODES, query_string={"tab": "follow_up"}, headers=headers)
    summary = client.get(SUMMARY, headers=headers)

    assert rows.status_code == 200, rows.get_data(as_text=True)
    assert [row["episode_id"] for row in rows.get_json()["data"]["items"]] == [episode.id]
    assert summary.status_code == 200, summary.get_data(as_text=True)
    data = summary.get_json()["data"]
    assert data["counts"] == {"follow_up": 1, "reminding": 0}
    assert data["metrics_30d"] == {
        "lapses_started": 1,
        "reminders_sent": {"telegram": 1, "email": 0},
        "reordered_after_reminder": 1,
        "place_resupplied": 0,
        "reordered_before_contact": 0,
        "stuck_order_delivered": 0,
        "escalated_by_flag": {**NO_FLAGS, "long_lapsed": 1},
        "switched_off_by_customers": 1,
        "blocks_after_reminder": 1,
    }
