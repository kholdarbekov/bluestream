"""Account merges keep reorder-reminder settings, lapses and their history (spec §10 Merges, §15.4).

Every merge direction goes through `cross_platform_sync_service.auto_link_accounts` (its callers:
auth_service.py:1771 and :1944, api/auth.py:3150), whose three branches each call
`_transfer_user_references` before deleting the secondary user. On Postgres the user FKs of all
three reminder tables are RESTRICT, so a reference the merge forgot would fail it loudly.
"""

import pytest
from sqlalchemy import text

from business_app.models.reorder_reminder import ReorderReminderEpisode, ReorderReminderEvent, ReorderReminderSetting
from business_app.models.user import User
from business_app.services.cross_platform_sync_service import cross_platform_sync_service
from business_app.services.reorder_reminders.actions import ReorderReminderActions as Actions
from business_app.services.reorder_reminders.events import record_event_at
from business_app.services.reorder_reminders.settings import ReorderReminderSettingsService as Settings
from business_app.services.telegram_reachability_service import TelegramReachabilityService
from shared.enums import UserRole, UserType
from tests.integration.reorder_reminder_builders import (  # noqa: F401 - `*_fixture` are the `store`, `sends` fixtures
    WEEKLY,
    at,
    customer_with_deliveries,
    delivered_order,
    make_customer,
    make_episode,
    only_lapse,
    run_engine,
    sends_fixture,
    store_fixture,
)

DIRECTIONS = [
    ("web keeps, Telegram merges in", "web", "telegram"),
    ("Telegram keeps, web merges in", "telegram", "web"),
    ("two web accounts, the primary keeps", "web", "web"),
]

Episode = ReorderReminderEpisode
Event = ReorderReminderEvent
Setting = ReorderReminderSetting

# Every reminder column that names a user: the three RESTRICT owners and the three SET NULL stamps.
USER_COLUMNS = (
    Episode.user_id,
    Episode.resolved_by_user_id,
    Event.user_id,
    Event.actor_user_id,
    Setting.user_id,
    Setting.changed_by_user_id,
)


def _account(store, source, name, created_day):
    return make_customer(
        store,
        first_name=name,
        registration_source=source,
        telegram=source == "telegram",
        email=f"{name.lower()}@example.com" if source == "web" else None,
        created_at=at(created_day),
    )


def _merge(store, primary, secondary):
    """The merge every caller runs (`auto_link_accounts`), on `store`.

    On Postgres the throwaway database first gets `users.phone` as the model declares it (nullable).
    A drift that predates this feature: the migration chain still creates the column NOT NULL
    (06f202673824; 2b9b22aff258 "make phone nullable" altered `registration_method` instead), while
    the model and the dev database have it nullable. The Telegram-primary and same-platform branches
    null the secondary's phone before the delete, so without this they fail there, before the
    reminder tables' RESTRICT foreign keys, which are what these tests are about, are ever reached."""
    if store.engine.dialect.name == "postgresql":
        store.session.execute(text("ALTER TABLE users ALTER COLUMN phone DROP NOT NULL"))
        store.session.commit()
    return cross_platform_sync_service.auto_link_accounts(
        primary_user=primary, secondary_user=secondary, link_type="merge"
    )


def _references_to(user_id):
    """`{column: rows naming user_id}` over every reminder column that names a user."""
    return {
        f"{column.class_.__tablename__}.{column.key}": column.class_.query.filter(column == user_id).count()
        for column in USER_COLUMNS
    }


@pytest.mark.parametrize("label, primary_source, secondary_source", DIRECTIONS, ids=[row[0] for row in DIRECTIONS])
def test_a_merge_keeps_customer_off_and_resolved_history_and_never_reopens_an_anchor(
    store, sends, label, primary_source, secondary_source
):
    primary = _account(store, primary_source, "Keep", -500)
    secondary = _account(store, secondary_source, "Gone", -300)
    admin = make_customer(store, first_name="Admin", role=UserRole.ADMIN, user_type=UserType.STAFF)
    keep_orders = [delivered_order(store, primary, at(n)) for n in (-60, -50)]
    gone_orders = [delivered_order(store, secondary, at(n)) for n in WEEKLY]
    history = make_episode(store, secondary, anchor=gone_orders[0], status="resolved", created_at=at(-10))
    record_event_at(at(-9), user_id=secondary.id, kind="resolved", episode=history, detail="other", actor_user_id=admin.id)
    store.session.commit()
    run_engine(11)
    primary_lapse = only_lapse(keep_orders[-1])  # preset, due day -19: long lapsed on day 11
    secondary_lapse = only_lapse(gone_orders[-1])  # weekly, reminded on day 11
    assert (primary_lapse.status, secondary_lapse.status) == ("escalated", "open")
    Settings.set(secondary.id, False, actor_kind="customer", via="telegram_button", actor_user_id=secondary.id,
                 now=at(11, 12))
    survivor, merged_away = primary.id, secondary.id  # read before the merge deletes the secondary row

    result = _merge(store, primary, secondary)

    assert result["success"] is True, result
    assert store.session.get(User, merged_away) is None
    effective = Settings.effective_for([survivor])
    assert (effective.customer_off, effective.source_user_id, effective.changed_via) == (True, survivor, "telegram_button")
    assert ReorderReminderSetting.query.count() == 1
    rows = {row.id: row for row in ReorderReminderEpisode.query.all()}
    assert {row.user_id for row in rows.values()} == {survivor}
    assert (rows[history.id].status, rows[history.id].outcome) == ("resolved", "other")
    assert (rows[primary_lapse.id].status, rows[primary_lapse.id].close_reason) == ("closed", "superseded")
    assert rows[secondary_lapse.id].status == "open"
    events = ReorderReminderEvent.query.all()
    assert {event.user_id for event in events} == {survivor}
    assert {event.actor_user_id for event in events} <= {None, survivor, admin.id}
    assert ReorderReminderEvent.query.filter_by(episode_id=history.id, kind="resolved").one().actor_user_id == admin.id

    run_engine(12)
    assert ReorderReminderEpisode.query.count() == 3


@pytest.mark.parametrize("label, primary_source, secondary_source", DIRECTIONS, ids=[row[0] for row in DIRECTIONS])
def test_a_merge_moves_every_row_and_stamp_that_names_the_merged_away_account(
    store, sends, label, primary_source, secondary_source
):
    """An account that owns ONLY reachability events still merges: a web account that linked the
    bot owns an `unblocked` event (Task 4), with no lapse and no setting. And every stamp naming the
    account (an event's actor, a lapse it resolved, a setting it changed) moves to the survivor,
    so no reminder row is left naming a deleted user."""
    primary = _account(store, primary_source, "Keep", -500)
    secondary = _account(store, secondary_source, "Gone", -300)
    TelegramReachabilityService.mark_blocked(secondary, source="send_410", at=at(-5))
    TelegramReachabilityService.mark_reachable(secondary, source="link", at=at(-4))
    store.session.commit()
    vali, vali_orders = customer_with_deliveries(store, WEEKLY, telegram=True, first_name="Vali")
    for n in (11, 18, 25):
        run_engine(n)
    vali_lapse = only_lapse(vali_orders[-1])
    Actions.resolve(vali_lapse.id, outcome="do_not_contact", note=None, actor_user_id=secondary.id, now=at(26))
    survivor, merged_away, vali_id = primary.id, secondary.id, vali.id
    assert _references_to(merged_away) == {
        "reorder_reminder_episodes.user_id": 0,
        "reorder_reminder_episodes.resolved_by_user_id": 1,
        "reorder_reminder_events.user_id": 2,
        "reorder_reminder_events.actor_user_id": 2,
        "reorder_reminder_settings.user_id": 0,
        "reorder_reminder_settings.changed_by_user_id": 1,
    }
    events_before = ReorderReminderEvent.query.count()

    result = _merge(store, primary, secondary)

    assert result["success"] is True, result
    assert set(_references_to(merged_away).values()) == {0}
    assert ReorderReminderEvent.query.count() == events_before
    reachability = (
        ReorderReminderEvent.query.filter(ReorderReminderEvent.kind.in_(("blocked", "unblocked")))
        .order_by(ReorderReminderEvent.id)
        .all()
    )
    assert [(event.user_id, event.kind, event.detail, event.episode_id) for event in reachability] == [
        (survivor, "blocked", "send_410", None),
        (survivor, "unblocked", "link", None),
    ]
    assert only_lapse(vali_orders[-1]).resolved_by_user_id == survivor
    resolved = ReorderReminderEvent.query.filter_by(episode_id=vali_lapse.id, kind="resolved").one()
    setting_changed = ReorderReminderEvent.query.filter_by(kind="setting_changed").one()
    assert (resolved.actor_user_id, setting_changed.actor_user_id, setting_changed.user_id) == (
        survivor, survivor, vali_id,
    )
    assert store.session.get(ReorderReminderSetting, vali_id).changed_by_user_id == survivor


def test_a_merge_closes_the_superseded_lapse_in_each_row_set_before_re_pointing(store, sends):
    """§10 Merges, per row set: both accounts hold an active lapse in the live AND the dry-run row
    set, and the one-active index allows one per user per set. In each set the lapse on the older
    anchor closes `superseded`, naming the newer anchor, and the other moves to the survivor."""
    primary = _account(store, "web", "Keep", -500)
    secondary = _account(store, "telegram", "Gone", -300)
    keep_orders = [delivered_order(store, primary, at(n)) for n in (-60, -50)]
    gone_orders = [delivered_order(store, secondary, at(n)) for n in WEEKLY]
    run_engine(11)
    run_engine(11, mode="dry_run", minute=1)
    survivor, merged_away = primary.id, secondary.id

    result = _merge(store, primary, secondary)

    assert result["success"] is True, result
    for is_dry_run in (False, True):
        superseded = only_lapse(keep_orders[-1], is_dry_run=is_dry_run)
        current = only_lapse(gone_orders[-1], is_dry_run=is_dry_run)
        assert (superseded.status, superseded.close_reason, superseded.user_id) == ("closed", "superseded", survivor)
        assert (current.status, current.user_id) == ("open", survivor)
        closed = ReorderReminderEvent.query.filter_by(episode_id=superseded.id, kind="closed").one()
        assert (closed.detail, closed.is_dry_run, closed.payload["order_id"], closed.payload["merged_user_id"]) == (
            "superseded", is_dry_run, gone_orders[-1].id, merged_away,
        )
