"""What a customer or staff member does to a lapse (spec §6, §8; D5, D13, D14, D20; master Review Focus #5)."""

import pytest

from business_app.models.reorder_reminder import (
    ReorderReminderEpisode,
    ReorderReminderEvent,
    ReorderReminderSetting,
    is_resolvable,
    resolvable_clause,
)
from business_app.models.support import SupportMessage
from business_app.services.reorder_reminders import actions as actions_module
from business_app.services.reorder_reminders.actions import ReorderReminderActions as Actions
from business_app.services.reorder_reminders.config import ReminderConfig
from business_app.services.reorder_reminders.settings import ReorderReminderSettingsService as Settings
from business_app.utils.exceptions import NotFoundError, ValidationError
from business_app.utils.timezone_utils import ensure_utc
from tests.integration.reorder_reminder_builders import (  # noqa: F401 - `*_fixture` are the `sends`, `mode` fixtures
    WEEKLY,
    at,
    customer_headers,
    customer_with_deliveries,
    day,
    event_rows,
    make_customer,
    make_episode,
    mode_fixture,
    only_lapse,
    run_engine,
    sends_fixture,
)

SUPPORT_MESSAGES = "/api/v1/support/messages"


def _weekly_after_r1(db, first_name="Ali"):
    user, orders = customer_with_deliveries(db, WEEKLY, telegram=True, first_name=first_name)
    run_engine(11)
    return user, orders, only_lapse(orders[-1])


def _escalated(db, *, first_name, mode="on"):
    user, orders = customer_with_deliveries(db, WEEKLY, telegram=True, first_name=first_name)
    for n in (11, 18, 25):
        run_engine(n, mode=mode)
    # The row set `mode` writes, read from the one rule (contract amendment 7), never re-derived here.
    row_set_is_dry_run = ReminderConfig.from_mapping({"REORDER_REMINDERS_MODE": mode}).row_set_is_dry_run
    return user, orders, only_lapse(orders[-1], is_dry_run=row_set_is_dry_run)


LATER = [("weekly", WEEKLY, 11, 25, 32), ("preset", (0,), 31, 45, 52)]


@pytest.mark.parametrize("label, deliveries, r1, r2, escalation", LATER, ids=[row[0] for row in LATER])
def test_later_on_reminder_one_moves_reminder_two_by_snooze_days_and_flags_snoozed(
    db, sends, label, deliveries, r1, r2, escalation
):
    """Spec §4 ladder table, last column; D13."""
    ali, orders = customer_with_deliveries(db, deliveries, telegram=True)
    run_engine(r1)
    row = only_lapse(orders[-1])

    assert Actions.snooze(ali.id, row.id, now=at(r1, 10, 30)) == 14

    assert only_lapse(orders[-1]).next_action_on == day(r2)
    run_engine(r1 + 7)
    assert only_lapse(orders[-1]).step == 1
    run_engine(r2)
    assert only_lapse(orders[-1]).step == 2
    run_engine(escalation)
    row = only_lapse(orders[-1])
    assert (row.status, row.escalation_flag) == ("escalated", "snoozed")
    assert ("later", None, 1, None) in event_rows(episode=row)


def test_a_second_later_never_moves_the_ladder_backwards(db, sends):
    """Review Focus #5, the max rule: a second tap the same day changes nothing, and a tap a day
    later can only push reminder 2 later, never earlier."""
    ali, orders, row = _weekly_after_r1(db)

    Actions.snooze(ali.id, row.id, now=at(11, 10, 30))
    assert Actions.snooze(ali.id, row.id, now=at(11, 16)) == 14
    assert only_lapse(orders[-1]).next_action_on == day(25)
    Actions.snooze(ali.id, row.id, now=at(12, 9))
    assert only_lapse(orders[-1]).next_action_on == day(26)

    assert [kind for kind, *_rest in event_rows(episode=row)].count("later") == 3


def test_later_after_the_second_reminder_is_refused_and_changes_nothing(db, sends):
    """Review Focus #5: once step != 1 the tap is refused with REORDER_REMINDER_NOT_ACTIVE, which
    makes the bot retire the old message."""
    ali, orders, row = _weekly_after_r1(db)
    run_engine(18)

    with pytest.raises(ValidationError) as refused:
        Actions.snooze(ali.id, row.id, now=at(18, 11))

    assert refused.value.error_code == "REORDER_REMINDER_NOT_ACTIVE"
    assert only_lapse(orders[-1]).next_action_on == day(25)
    assert "later" not in [kind for kind, *_rest in event_rows(episode=row)]


def test_later_on_someone_elses_or_a_dry_run_reminder_is_refused(db, sends):
    ali, orders, row = _weekly_after_r1(db)
    vali = make_customer(db, first_name="Vali")
    with pytest.raises(ValidationError) as not_yours:
        Actions.snooze(vali.id, row.id, now=at(11, 11))
    run_engine(11, mode="dry_run")
    simulated_row = only_lapse(orders[-1], is_dry_run=True)

    with pytest.raises(ValidationError) as simulated:
        Actions.snooze(ali.id, simulated_row.id, now=at(11, 11))

    assert {not_yours.value.error_code, simulated.value.error_code} == {"REORDER_REMINDER_NOT_ACTIVE"}
    assert only_lapse(orders[-1]).next_action_on == day(18)


def test_stop_mid_ladder_silences_the_rest_and_still_escalates_flagged_stopped(db, sends):
    """D5: Stop ends the messages, not the follow-up."""
    ali, orders, row = _weekly_after_r1(db)

    setting = Actions.stop(ali.id, via="telegram_button", episode_id=row.id, now=at(11, 11))

    assert (setting.customer_off, setting.changed_via) == (True, "telegram_button")
    run_engine(18)
    run_engine(25)
    row = only_lapse(orders[-1])
    assert (row.status, row.escalation_flag) == ("escalated", "stopped")
    assert [(kind, detail, step) for kind, detail, step, _channel in event_rows(episode=row)] == [
        ("opened", None, None),
        ("reminder_queued", None, 1),
        ("stop", "telegram_button", 1),
        ("reminder_skipped", "stopped", 2),
        ("escalated", "stopped", None),
    ]
    assert sends == [(row.id, 1)]


def test_stop_works_with_no_lapse_at_all(db):
    vali = make_customer(db, first_name="Vali")

    setting = Actions.stop(vali.id, via="email_link", now=at(3))

    assert setting.customer_off is True
    assert event_rows(user=vali) == [("stop", "email_link", None, None), ("setting_changed", "off", None, None)]
    stop = ReorderReminderEvent.query.filter_by(kind="stop").one()
    assert (stop.episode_id, stop.is_dry_run, stop.actor_user_id) == (None, False, vali.id)


@pytest.mark.parametrize("via", ["admin_ui", "worklist", "unknown_route"])
def test_stop_is_refused_on_a_staff_or_unknown_route_and_writes_nothing(db, via):
    """A Stop is the customer's own choice (§8 writer table): a staff route never carries one."""
    vali = make_customer(db, first_name="Vali")

    with pytest.raises(ValidationError) as refused:
        Actions.stop(vali.id, via=via, now=at(3))

    assert refused.value.error_code == "REORDER_REMINDER_SETTING_INVALID"
    assert (ReorderReminderEvent.query.count(), ReorderReminderSetting.query.count()) == (0, 0)


@pytest.mark.parametrize("via", ["telegram_button", "telegram_settings", "email_link", "web_settings"])
def test_stop_takes_every_customer_route(db, via):
    vali = make_customer(db, first_name="Vali")

    setting = Actions.stop(vali.id, via=via, now=at(3))

    assert (setting.customer_off, setting.changed_via) == (True, via)
    assert event_rows(user=vali) == [("stop", via, None, None), ("setting_changed", "off", None, None)]


def test_stop_naming_someone_elses_lapse_switches_off_only_the_caller_and_links_no_lapse(db, sends):
    """Customer actions act only on the caller's own customer: an episode id alone reaches nothing."""
    ali, orders, row = _weekly_after_r1(db)
    vali = make_customer(db, first_name="Vali")

    setting = Actions.stop(vali.id, via="telegram_button", episode_id=row.id, now=at(11, 11))

    assert (setting.customer_off, setting.source_user_id) == (True, vali.id)
    stop = ReorderReminderEvent.query.filter_by(kind="stop").one()
    assert (stop.user_id, stop.episode_id, stop.step) == (vali.id, None, None)
    assert Settings.effective_for([ali.id]).enabled is True
    assert "stop" not in [kind for kind, *_rest in event_rows(episode=row)]


def test_stop_on_an_old_reminder_applies_only_while_the_setting_is_effectively_on(db, sends, admin_user):
    """Review Focus #5 / D20: a Stop over an admin-off changes nothing; once the admin turned
    reminders back on, the same old button switches them off as the customer's own choice."""
    ali, orders, row = _weekly_after_r1(db)
    Settings.set(ali.id, False, actor_kind="admin", via="admin_ui", reason="Asked for a break",
                 actor_user_id=admin_user.id, now=at(11, 12))

    still_admin = Actions.stop(ali.id, via="telegram_button", episode_id=row.id, now=at(11, 13))

    assert still_admin.admin_off is True
    assert only_lapse(orders[-1]).status == "suspended"
    Settings.set(ali.id, True, actor_kind="admin", via="admin_ui", reason="Customer asked to resume",
                 actor_user_id=admin_user.id, now=at(12, 9))
    now_customer = Actions.stop(ali.id, via="telegram_button", episode_id=row.id, now=at(12, 10))
    assert (now_customer.customer_off, now_customer.changed_at) == (True, at(12, 10))


def test_a_text_reply_while_reminded_escalates_the_lapse_and_still_lands_in_the_support_inbox(
    app, client, db, sends, mode
):
    """D14, through the endpoint the bot calls for every free-text message."""
    mode("on")
    ali, orders, row = _weekly_after_r1(db)

    response = client.post(
        SUPPORT_MESSAGES,
        json={"content": "We switched to another supplier", "message_type": "text"},
        headers=customer_headers(app, ali),
    )

    assert response.status_code == 200, response.get_data(as_text=True)
    row = only_lapse(orders[-1])
    assert (row.status, row.escalation_flag, row.next_action_on) == ("escalated", "replied", None)
    replied = ReorderReminderEvent.query.filter_by(episode_id=row.id, kind="replied").one()
    assert (replied.payload, replied.user_id) == ({"excerpt": "We switched to another supplier"}, ali.id)
    assert event_rows(episode=row)[-1] == ("escalated", "replied", None, None)
    assert SupportMessage.query.filter_by(content="We switched to another supplier").count() == 1


def test_a_photo_reply_counts_with_its_type_label(app, client, db, sends, mode):
    mode("on")
    ali, orders, row = _weekly_after_r1(db)

    response = client.post(
        SUPPORT_MESSAGES,
        json={"message_type": "photo", "telegram_file_id": "AgACAgIAAxkBAAIBQ2ZmZmZm"},
        headers=customer_headers(app, ali),
    )

    assert response.status_code == 200, response.get_data(as_text=True)
    replied = ReorderReminderEvent.query.filter_by(episode_id=row.id, kind="replied").one()
    assert replied.payload == {"excerpt": "📷 Photo"}
    assert only_lapse(orders[-1]).escalation_flag == "replied"


def test_in_mode_off_a_reply_touches_no_lapse(app, client, db, sends, mode):
    ali, orders, row = _weekly_after_r1(db)
    mode("off")

    response = client.post(SUPPORT_MESSAGES, json={"content": "Thanks!", "message_type": "text"},
                           headers=customer_headers(app, ali))

    assert response.status_code == 200
    assert only_lapse(orders[-1]).status == "open"
    assert "replied" not in [kind for kind, *_rest in event_rows(episode=row)]


def test_in_dry_run_a_reply_escalates_the_simulated_lapse_and_leaves_the_live_one(app, client, db, sends, mode):
    """§13: the reply hook works on dry-run rows; the row set is the one `row_set_is_dry_run` names
    (contract amendment 7), so a live row from before is not touched."""
    ali, orders, live_row = _weekly_after_r1(db)
    run_engine(11, mode="dry_run", minute=1)
    mode("dry_run")

    response = client.post(SUPPORT_MESSAGES, json={"content": "Not now, thanks", "message_type": "text"},
                           headers=customer_headers(app, ali))

    assert response.status_code == 200, response.get_data(as_text=True)
    simulated = only_lapse(orders[-1], is_dry_run=True)
    assert (simulated.status, simulated.escalation_flag) == ("escalated", "replied")
    replied = ReorderReminderEvent.query.filter_by(kind="replied").one()
    assert (replied.episode_id, replied.is_dry_run, replied.payload) == (
        simulated.id, True, {"excerpt": "Not now, thanks"},
    )
    assert only_lapse(orders[-1]).status == "open"
    assert "replied" not in [kind for kind, *_rest in event_rows(episode=live_row)]


def test_a_reply_on_an_escalated_lapse_is_recorded_and_keeps_its_flag(app, client, db, sends, mode):
    mode("on")
    ali, orders, row = _escalated(db, first_name="Ali")

    response = client.post(SUPPORT_MESSAGES, json={"content": "Please bring two bottles", "message_type": "text"},
                           headers=customer_headers(app, ali))

    assert response.status_code == 200
    row = only_lapse(orders[-1])
    assert (row.status, row.escalation_flag) == ("escalated", "no_response")
    assert event_rows(episode=row)[-1] == ("replied", None, None, None)


def test_a_failing_reply_hook_never_breaks_support_capture(app, client, db, sends, mode, monkeypatch):
    mode("on")
    ali, orders, row = _weekly_after_r1(db)

    def on_customer_reply(user_id, *, excerpt, now=None):
        raise RuntimeError("reminder tables locked")

    monkeypatch.setattr(Actions, "on_customer_reply", staticmethod(on_customer_reply))

    response = client.post(SUPPORT_MESSAGES, json={"content": "Hello?", "message_type": "text"},
                           headers=customer_headers(app, ali))

    assert response.status_code == 200, response.get_data(as_text=True)
    assert SupportMessage.query.filter_by(content="Hello?").count() == 1
    assert only_lapse(orders[-1]).status == "open"


def test_a_reply_hook_failing_mid_write_rolls_back_only_its_own_rows_never_the_message(
    app, client, db, sends, mode, monkeypatch
):
    """The hook runs AFTER the support commit. Here it fails after it has already written its
    `replied` event (the escalation's event raises), so its rollback discards its own rows; had the
    hook run before the commit, the same rollback would silently discard the customer's message."""
    mode("on")
    ali, orders, row = _weekly_after_r1(db)
    real_record_event_at = actions_module.record_event_at

    def record_event_at(at, **fields):
        if fields["kind"] == "escalated":
            raise RuntimeError("reminder tables locked")
        return real_record_event_at(at, **fields)

    monkeypatch.setattr(actions_module, "record_event_at", record_event_at)

    response = client.post(SUPPORT_MESSAGES, json={"content": "Are you open on Sunday?", "message_type": "text"},
                           headers=customer_headers(app, ali))

    assert response.status_code == 200, response.get_data(as_text=True)
    assert SupportMessage.query.count() == 1
    assert ReorderReminderEvent.query.filter_by(kind="replied").count() == 0
    assert only_lapse(orders[-1]).status == "open"


def test_staff_resolve_an_escalated_or_a_suspended_escalated_lapse_with_an_outcome(db, sends, admin_user):
    ali, _ali_orders, ali_row = _escalated(db, first_name="Ali")
    vali, vali_orders, vali_row = _escalated(db, first_name="Vali")
    Settings.set(vali.id, False, actor_kind="admin", via="admin_ui", reason="Moved abroad",
                 actor_user_id=admin_user.id, now=at(25, 12))
    assert only_lapse(vali_orders[-1]).status == "suspended"

    resolved = Actions.resolve(ali_row.id, outcome="away", note="  Out of town until May ",
                               actor_user_id=admin_user.id, now=at(26))
    from_suspended = Actions.resolve(vali_row.id, outcome="moved", note=None, actor_user_id=admin_user.id, now=at(26))

    assert (resolved.status, resolved.outcome, resolved.outcome_note, resolved.resolved_by_user_id) == (
        "resolved", "away", "Out of town until May", admin_user.id,
    )
    assert ensure_utc(resolved.resolved_at) == at(26)
    assert (from_suspended.status, from_suspended.suspended_at, from_suspended.suspended_reason) == ("resolved", None, None)
    assert event_rows(episode=resolved)[-1] == ("resolved", "away", None, None)


def test_only_a_lapse_waiting_for_follow_up_can_be_resolved_and_only_with_a_known_outcome(db, sends, admin_user):
    ali, orders, row = _weekly_after_r1(db)

    with pytest.raises(ValidationError) as still_open:
        Actions.resolve(row.id, outcome="away", note=None, actor_user_id=admin_user.id)
    with pytest.raises(ValidationError) as unknown:
        Actions.resolve(row.id, outcome="lost_interest", note=None, actor_user_id=admin_user.id)
    with pytest.raises(NotFoundError):
        Actions.resolve(987654, outcome="away", note=None, actor_user_id=admin_user.id)

    assert (still_open.value.error_code, unknown.value.error_code) == (
        "REORDER_REMINDER_NOT_RESOLVABLE", "REORDER_REMINDER_OUTCOME_INVALID",
    )
    assert only_lapse(orders[-1]).status == "open"


RESOLVABLE = [
    ("escalated", "escalated", None, True),
    ("suspended after it was escalated", "suspended", "no_response", True),
    ("suspended before any escalation", "suspended", None, False),
    ("open", "open", None, False),
    ("resolved", "resolved", None, False),
    ("closed", "closed", None, False),
]


@pytest.mark.parametrize("label, status, flag, expected", RESOLVABLE, ids=[row[0] for row in RESOLVABLE])
def test_is_resolvable_and_its_sql_twin_agree_on_every_state(db, label, status, flag, expected):
    """Contract amendment 4: one rule in two forms. The worklist's `can_resolve` reads the
    predicate and `resolve`'s conditional UPDATE the clause, so a row the page offers to resolve is
    exactly a row the write accepts."""
    ali, orders = customer_with_deliveries(db, (-7,), first_name="Ali")
    row = make_episode(db, ali, anchor=orders[-1], status=status, escalation_flag=flag)

    matched = ReorderReminderEpisode.query.filter(ReorderReminderEpisode.id == row.id, resolvable_clause()).count()

    assert (is_resolvable(row), matched) == (expected, 1 if expected else 0)


def test_do_not_contact_on_a_live_row_switches_reminders_off_as_admin_with_a_synthesized_reason(db, sends, admin_user):
    ali, orders, row = _escalated(db, first_name="Ali")

    Actions.resolve(row.id, outcome="do_not_contact", note="Asked us not to call", actor_user_id=admin_user.id, now=at(26))

    effective = Settings.effective_for([ali.id])
    assert (effective.admin_off, effective.changed_via, effective.reason) == (
        True, "worklist", "Worklist outcome: do_not_contact — Asked us not to call",
    )
    assert only_lapse(orders[-1]).status == "resolved"
    setting_event = ReorderReminderEvent.query.filter_by(kind="setting_changed").one()
    assert (setting_event.payload["episode_id"], setting_event.actor_user_id) == (row.id, admin_user.id)


def test_do_not_contact_upgrades_a_customers_own_stop_to_admin_off(db, sends, admin_user):
    """Contract amendment 16: the customer pressed Stop (a real customer-off row written by the one
    writer), staff then record do_not_contact. The admin's off applies over the customer's, so the
    customer is out of scope (E6), not merely silenced."""
    ali, orders = customer_with_deliveries(db, WEEKLY, telegram=True, first_name="Ali")
    Settings.set(ali.id, False, actor_kind="customer", via="telegram_button", actor_user_id=ali.id, now=at(5))
    for n in (11, 18, 25):
        run_engine(n)
    row = only_lapse(orders[-1])
    assert (row.status, row.escalation_flag) == ("escalated", "stopped")
    assert db.session.get(ReorderReminderSetting, ali.id).changed_by_kind == "customer"

    Actions.resolve(row.id, outcome="do_not_contact", note=None, actor_user_id=admin_user.id, now=at(26))

    effective = Settings.effective_for([ali.id])
    assert (effective.admin_off, effective.changed_via, effective.reason, effective.changed_at) == (
        True, "worklist", "Worklist outcome: do_not_contact", at(26),
    )
    changes = ReorderReminderEvent.query.filter_by(kind="setting_changed").order_by(ReorderReminderEvent.id).all()
    assert [(event.detail, event.payload["changed_by_kind"]) for event in changes] == [("off", "customer"), ("off", "admin")]
    assert changes[-1].payload["previous"]["changed_by_kind"] == "customer"
    assert only_lapse(orders[-1]).status == "resolved"


def test_do_not_contact_on_a_dry_run_row_records_the_outcome_only(db, sends, admin_user):
    """§13: dry-run rows never write settings."""
    ali, orders, row = _escalated(db, first_name="Ali", mode="dry_run")

    Actions.resolve(row.id, outcome="do_not_contact", note=None, actor_user_id=admin_user.id, now=at(26))

    assert ReorderReminderSetting.query.count() == 0
    assert only_lapse(orders[-1], is_dry_run=True).outcome == "do_not_contact"
