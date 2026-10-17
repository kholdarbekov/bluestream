"""The reorder-reminder setting (spec §8; D5, D7, D20): one store, one writer, last write wins."""

from datetime import timezone

import pytest

from business_app.models.audit import AuditEventType, AuditSeverity
from business_app.models.reorder_reminder import ReorderReminderEpisode, ReorderReminderEvent, ReorderReminderSetting
from business_app.services.reorder_reminders import settings as settings_module
from business_app.services.reorder_reminders.settings import DEFAULT_SETTING, EffectiveSetting
from business_app.services.reorder_reminders.settings import ReorderReminderSettingsService as Settings
from business_app.utils.exceptions import NotFoundError, ValidationError
from business_app.utils.timezone_utils import ensure_utc
from tests.integration.reorder_reminder_builders import (
    at,
    day,
    delivered_order,
    event_rows,
    link,
    make_customer,
    make_episode,
)


@pytest.fixture
def audits(monkeypatch):
    """Every audit row the writer asks for, by its exact keyword arguments."""
    calls = []

    def log_event(**kwargs):
        calls.append(kwargs)
        return "audit-event-id"

    monkeypatch.setattr(settings_module.audit_logger, "log_event", log_event)
    return calls


def test_no_row_anywhere_means_enabled(db):
    ali = make_customer(db)

    assert Settings.effective_for([ali.id]) == DEFAULT_SETTING
    assert (DEFAULT_SETTING.enabled, DEFAULT_SETTING.changed_by_kind) == (True, None)


def test_the_latest_change_across_linked_accounts_wins(db, admin_user, audits):
    ali, ali_work = make_customer(db), make_customer(db, first_name="Ali")
    link(db, ali, ali_work)

    Settings.set(ali.id, False, actor_kind="customer", via="telegram_button", actor_user_id=ali.id, now=at(1))
    assert Settings.effective_for([ali.id, ali_work.id]) == EffectiveSetting(
        enabled=False,
        changed_by_kind="customer",
        changed_via="telegram_button",
        changed_at=at(1),
        reason=None,
        source_user_id=ali.id,
    )

    Settings.set(
        ali_work.id, True, actor_kind="admin", via="admin_ui", reason="Asked to keep reminders",
        actor_user_id=admin_user.id, now=at(2),
    )
    effective = Settings.effective_for([ali.id, ali_work.id])
    assert (effective.enabled, effective.changed_by_kind, effective.source_user_id) == (True, "admin", ali_work.id)
    assert (effective.customer_off, effective.admin_off) == (False, False)


def test_a_customer_stop_over_an_admin_off_stays_admin_off_and_records_already_off(db, admin_user, audits):
    """D20 (amendment 16): a customer's unsubscribe can never turn an admin-off into a customer-off."""
    ali, ali_work = make_customer(db), make_customer(db, first_name="Ali")
    link(db, ali, ali_work)
    Settings.set(
        ali.id, False, actor_kind="admin", via="admin_ui", reason="Asked not to be contacted",
        actor_user_id=admin_user.id, now=at(1),
    )

    result = Settings.set(ali_work.id, False, actor_kind="customer", via="email_link", actor_user_id=ali_work.id, now=at(2))

    assert (result.admin_off, result.source_user_id) == (True, ali.id)
    assert db.session.get(ReorderReminderSetting, ali_work.id) is None
    assert event_rows(user=ali_work) == [("setting_changed", "already_off", None, None)]
    assert [call["new_values"]["enabled"] for call in audits] == [False]


def test_a_repeated_customer_stop_records_already_off_and_changes_nothing(db, audits):
    """D20 (amendment 16): the second Stop finds reminders already off; no row moves, no audit row."""
    ali = make_customer(db)
    first = Settings.set(ali.id, False, actor_kind="customer", via="telegram_button", actor_user_id=ali.id, now=at(1))

    again = Settings.set(ali.id, False, actor_kind="customer", via="email_link", actor_user_id=ali.id, now=at(2))

    assert again == first
    row = db.session.get(ReorderReminderSetting, ali.id)
    assert (row.changed_via, ensure_utc(row.changed_at)) == ("telegram_button", at(1))
    assert event_rows(user=ali) == [("setting_changed", "off", None, None), ("setting_changed", "already_off", None, None)]
    assert len(audits) == 1


def test_an_admin_off_over_a_customer_off_upgrades_it_and_suspends_the_active_lapses(db, admin_user, audits):
    """D20 as the owner meant it (amendment 16): an admin's off always applies, so the Users switch
    and the worklist's do_not_contact take a customer who pressed Stop out of scope (E6)."""
    ali, ali_work = make_customer(db), make_customer(db, first_name="Ali")
    link(db, ali, ali_work)
    lapse = make_episode(
        db, ali, anchor=delivered_order(db, ali, at(-30)), status="escalated", escalation_flag="stopped"
    )
    Settings.set(ali.id, False, actor_kind="customer", via="telegram_button", actor_user_id=ali.id, now=at(1))

    result = Settings.set(
        ali_work.id, False, actor_kind="admin", via="admin_ui", reason="Asked not to be contacted",
        actor_user_id=admin_user.id, now=at(2),
    )

    assert (result.admin_off, result.customer_off, result.source_user_id, result.reason) == (
        True, False, ali_work.id, "Asked not to be contacted",
    )
    assert event_rows(user=ali_work) == [("setting_changed", "off", None, None)]
    upgrade = ReorderReminderEvent.query.filter_by(user_id=ali_work.id, kind="setting_changed").one()
    assert (upgrade.payload["changed_by_kind"], upgrade.payload["previous"]["changed_by_kind"]) == ("admin", "customer")
    reloaded = db.session.get(ReorderReminderEpisode, lapse.id)
    assert (reloaded.status, reloaded.suspended_reason, reloaded.escalation_flag) == ("suspended", "admin_off", "stopped")
    assert ensure_utc(reloaded.suspended_at) == at(2)
    assert [call["new_values"]["changed_by_kind"] for call in audits] == ["customer", "admin"]


def test_a_customer_turning_reminders_back_on_wins_over_an_admin_off(db, admin_user, audits):
    """D7: last write wins, so a customer may re-enable their own reminders."""
    ali = make_customer(db)
    Settings.set(ali.id, False, actor_kind="admin", via="admin_ui", reason="Paused", actor_user_id=admin_user.id, now=at(1))

    result = Settings.set(ali.id, True, actor_kind="customer", via="telegram_settings", actor_user_id=ali.id, now=at(2))

    assert (result.enabled, result.changed_by_kind, result.changed_via) == (True, "customer", "telegram_settings")
    row = db.session.get(ReorderReminderSetting, ali.id)
    assert (row.reason, row.changed_by_user_id) == (None, ali.id)


@pytest.mark.parametrize("reason", [None, "", "   "])
def test_an_admin_change_needs_a_reason(db, admin_user, audits, reason):
    ali = make_customer(db)

    with pytest.raises(ValidationError) as refused:
        Settings.set(ali.id, False, actor_kind="admin", via="admin_ui", reason=reason, actor_user_id=admin_user.id)

    assert refused.value.error_code == "REASON_REQUIRED"
    assert (ReorderReminderSetting.query.count(), event_rows(user=ali), audits) == (0, [], [])


@pytest.mark.parametrize("kind, via", [("robot", "admin_ui"), ("customer", "sms")])
def test_an_unknown_actor_or_route_is_refused(db, audits, kind, via):
    ali = make_customer(db)

    with pytest.raises(ValidationError) as refused:
        Settings.set(ali.id, False, actor_kind=kind, via=via, actor_user_id=ali.id)

    assert refused.value.error_code == "REORDER_REMINDER_SETTING_INVALID"


def test_an_unknown_user_is_not_found(db, audits):
    with pytest.raises(NotFoundError):
        Settings.set(987654, False, actor_kind="customer", via="email_link")


def test_admin_off_suspends_the_customers_open_and_escalated_lapses_in_the_same_commit(db, admin_user, audits):
    ali, ali_work = make_customer(db), make_customer(db, first_name="Ali")
    link(db, ali, ali_work)
    vali = make_customer(db, first_name="Vali")
    open_row = make_episode(db, ali, anchor=delivered_order(db, ali, at(-30)), step=1, next_action_on=day(18))
    escalated_row = make_episode(db, ali_work, anchor=delivered_order(db, ali_work, at(-60)), status="escalated")
    resolved_row = make_episode(db, ali, anchor=delivered_order(db, ali, at(-90)), status="resolved")
    others_row = make_episode(db, vali, anchor=delivered_order(db, vali, at(-30)))

    Settings.set(ali.id, False, actor_kind="admin", via="admin_ui", reason="Moved abroad", actor_user_id=admin_user.id, now=at(12))

    states = {row.id: (row.status, row.suspended_reason) for row in ReorderReminderEpisode.query.all()}
    assert states == {
        open_row.id: ("suspended", "admin_off"),
        escalated_row.id: ("suspended", "admin_off"),
        resolved_row.id: ("resolved", None),
        others_row.id: ("open", None),
    }
    assert event_rows(episode=open_row) == [("suspended", "admin_off", None, None)]
    reloaded = db.session.get(ReorderReminderEpisode, escalated_row.id)
    assert reloaded.escalated_at is not None and ensure_utc(reloaded.suspended_at) == at(12)


def test_every_applied_write_is_one_event_and_one_audit_row(db, admin_user, audits):
    ali = make_customer(db)

    Settings.set(
        ali.id, False, actor_kind="admin", via="worklist", reason="Worklist outcome: do_not_contact — asked",
        actor_user_id=admin_user.id, now=at(3),
    )

    event = ReorderReminderEvent.query.one()
    assert (event.kind, event.detail, event.episode_id, event.is_dry_run, event.actor_user_id) == (
        "setting_changed", "off", None, False, admin_user.id,
    )
    assert event.payload == {
        "enabled": False,
        "changed_by_kind": "admin",
        "changed_via": "worklist",
        "episode_id": None,
        "previous": {"enabled": True, "changed_by_kind": None, "changed_via": None, "changed_at": None},
    }
    assert audits == [
        {
            "event_type": AuditEventType.SETTINGS_CHANGED,
            "action": "reorder_reminders_setting_changed",
            "severity": AuditSeverity.MEDIUM,
            "resource_type": "user",
            "resource_id": str(ali.id),
            "description": "Reorder reminders disabled by admin via worklist",
            "old_values": {
                "enabled": True, "changed_by_kind": None, "changed_via": None, "changed_at": None, "reason": None,
                "state": "on",
            },
            "new_values": {
                "enabled": False,
                "changed_by_kind": "admin",
                "changed_via": "worklist",
                "changed_at": at(3).astimezone(timezone.utc).isoformat(),
                "reason": "Worklist outcome: do_not_contact — asked",
                "state": "admin_off",
            },
            "additional_data": {"actor_user_id": admin_user.id, "episode_id": None},
        }
    ]


def test_the_staff_shape_adds_the_state_and_the_customer_shape_is_unchanged(db, admin_user, audits):
    """Final review deferred 507 (ruling): `to_dict(include_reason=True)` publishes `state`, the
    properties' own answer (`customer_off` / `admin_off`, else on); the customer shape is exactly
    what it was, with no reason and no state."""
    never, stopped, excluded = make_customer(db), make_customer(db, first_name="B"), make_customer(db, first_name="C")
    Settings.set(stopped.id, False, actor_kind="customer", via="email_link", actor_user_id=stopped.id, now=at(1))
    Settings.set(excluded.id, False, actor_kind="admin", via="admin_ui", reason="Moved", actor_user_id=admin_user.id,
                 now=at(1))

    staff = {user: Settings.effective_for([user.id]).to_dict(include_reason=True) for user in (never, stopped, excluded)}
    customer = {user: Settings.effective_for([user.id]).to_dict() for user in (never, stopped, excluded)}

    assert [staff[user]["state"] for user in (never, stopped, excluded)] == ["on", "customer_off", "admin_off"]
    assert all(set(shape) == {"enabled", "changed_by_kind", "changed_via", "changed_at", "reason", "state"}
               for shape in staff.values())
    assert customer[stopped] == {
        "enabled": False,
        "changed_by_kind": "customer",
        "changed_via": "email_link",
        "changed_at": at(1).astimezone(timezone.utc).isoformat(),
    }
    assert all(set(shape) == {"enabled", "changed_by_kind", "changed_via", "changed_at"} for shape in customer.values())


def test_effective_by_key_reads_every_customer_in_one_query(db, count_queries, audits):
    a, b, c = make_customer(db), make_customer(db, first_name="B"), make_customer(db, first_name="C")
    Settings.set(b.id, False, actor_kind="customer", via="web_settings", actor_user_id=b.id, now=at(1))

    members = {"u:a": [a.id], "u:b": [b.id], "u:c": [c.id]}  # read before counting: set() committed, so ids reload

    with count_queries() as counter:
        result = Settings.effective_by_key(members)

    assert counter.count == 1
    assert (result["u:a"], result["u:b"].customer_off, result["u:c"]) == (DEFAULT_SETTING, True, DEFAULT_SETTING)


def test_merging_accounts_keeps_the_newer_setting_and_drops_the_secondarys_row(db, admin_user, audits):
    primary, secondary = make_customer(db, first_name="Web"), make_customer(db, first_name="Bot")
    Settings.set(primary.id, False, actor_kind="admin", via="admin_ui", reason="Paused", actor_user_id=admin_user.id, now=at(1))
    Settings.set(secondary.id, False, actor_kind="customer", via="telegram_button", actor_user_id=secondary.id, now=at(2))

    Settings.merge_rows(primary.id, secondary.id)
    db.session.commit()

    row = db.session.get(ReorderReminderSetting, primary.id)
    assert (row.enabled, row.changed_by_kind, row.changed_via, row.reason, row.changed_by_user_id) == (
        False, "customer", "telegram_button", None, primary.id,
    )
    assert ensure_utc(row.changed_at) == at(2)
    assert db.session.get(ReorderReminderSetting, secondary.id) is None


def test_merging_keeps_the_survivors_row_when_it_is_the_newer_one(db, audits):
    primary, secondary = make_customer(db, first_name="Web"), make_customer(db, first_name="Bot")
    Settings.set(secondary.id, False, actor_kind="customer", via="telegram_button", actor_user_id=secondary.id, now=at(1))
    Settings.set(primary.id, True, actor_kind="customer", via="web_settings", actor_user_id=primary.id, now=at(2))

    Settings.merge_rows(primary.id, secondary.id)
    db.session.commit()

    row = db.session.get(ReorderReminderSetting, primary.id)
    assert (row.enabled, row.changed_via) == (True, "web_settings")
    assert db.session.get(ReorderReminderSetting, secondary.id) is None


# Two rows written at the SAME instant: `(enabled, actor_kind, via)` on the first and on the second
# account (the second has the higher id), and the index of the row that must win. One rule ranks
# them everywhere (`_recency`): the later change; at equal instants an off row, then an admin row,
# then the higher account id, so a tie can never quietly re-enable reminders.
TIES = [
    ("an off row beats an admin's on row", (False, "customer", "telegram_button"), (True, "admin", "admin_ui"), 0),
    ("an off row wins on either account", (True, "admin", "admin_ui"), (False, "customer", "email_link"), 1),
    ("between two offs the admin's wins", (False, "customer", "telegram_button"), (False, "admin", "admin_ui"), 1),
    # The admin row on the LOWER id: the admin rank decides before the id does. Both rows are on: a
    # customer's off after an admin's off on a linked account is a D20 no-op and writes no row.
    ("an admin row beats a higher account id", (True, "admin", "admin_ui"), (True, "customer", "web_settings"), 0),
    (
        "between equal rows the higher account id wins",
        (True, "customer", "web_settings"),
        (True, "customer", "telegram_settings"),
        1,
    ),
]


def _write_at_one_instant(accounts, rows, admin_user):
    for account, (enabled, kind, via) in zip(accounts, rows):
        admin = kind == "admin"
        Settings.set(
            account.id, enabled, actor_kind=kind, via=via, reason="Owner request" if admin else None,
            actor_user_id=admin_user.id if admin else account.id, now=at(1),
        )


@pytest.mark.parametrize("label, first, second, winner", TIES, ids=[row[0] for row in TIES])
def test_a_tie_across_linked_accounts_goes_to_the_off_row_then_the_admin_row_then_the_higher_id(
    db, admin_user, audits, label, first, second, winner
):
    ali, ali_work = make_customer(db), make_customer(db, first_name="Ali")
    link(db, ali, ali_work)
    accounts = (ali, ali_work)

    _write_at_one_instant(accounts, (first, second), admin_user)

    assert ReorderReminderSetting.query.count() == 2
    effective = Settings.effective_for([ali.id, ali_work.id])
    assert (effective.enabled, effective.changed_by_kind, effective.changed_via, effective.source_user_id) == (
        *(first, second)[winner], accounts[winner].id,
    )


@pytest.mark.parametrize("survivor", [0, 1], ids=["first account keeps", "second account keeps"])
@pytest.mark.parametrize("label, first, second, winner", TIES, ids=[row[0] for row in TIES])
def test_a_merge_keeps_the_row_the_effective_setting_reads_even_on_a_tie(
    db, admin_user, audits, label, first, second, winner, survivor
):
    """`merge_rows` decides "newer" by `_effective`'s own key, so the survivor keeps exactly the
    setting that was in effect for the two accounts together, whichever account survives."""
    accounts = (make_customer(db, first_name="Web"), make_customer(db, first_name="Bot"))
    _write_at_one_instant(accounts, (first, second), admin_user)
    in_effect = Settings.effective_for([account.id for account in accounts])
    primary, secondary = accounts[survivor], accounts[1 - survivor]

    Settings.merge_rows(primary.id, secondary.id)
    db.session.commit()

    kept = Settings.effective_for([primary.id])
    assert (kept.enabled, kept.changed_by_kind, kept.changed_via, kept.changed_at, kept.reason) == (
        in_effect.enabled, in_effect.changed_by_kind, in_effect.changed_via, in_effect.changed_at, in_effect.reason,
    )
    assert (kept.enabled, kept.changed_by_kind, kept.changed_via) == (first, second)[winner]
    assert ReorderReminderSetting.query.count() == 1
