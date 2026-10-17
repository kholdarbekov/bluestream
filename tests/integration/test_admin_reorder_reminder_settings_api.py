"""The admin per-customer settings, partial (spec §8, plan Task 21a).

Each payload the admin UI sends is driven through the real route with a role-claim token: the
Users page's delivery-only and reminder-only payloads, and the Lapsed customers page's *Turn off
reminders* action on a *Being reminded* row.
"""

from contextlib import contextmanager
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import event

from business_app.models.audit import AuditLog
from business_app.models.notification import NotificationPreference
from business_app.models.reorder_reminder import ReorderReminderEpisode, ReorderReminderEvent, ReorderReminderSetting
from business_app.services.notification_service import NotificationService
from business_app.services.reorder_reminders.settings import ReorderReminderSettingsService
from business_app.utils.translations import get_translation
from shared.enums import UserRole
from tests.integration.reorder_reminder_builders import (
    claim_headers,
    delivered_order,
    link,
    make_customer,
    staff_episode,
)

pytestmark = pytest.mark.integration
UTC = timezone.utc

DELIVERY_AUDIT_ACTION = "admin_update_delivery_telegram_notification_setting"
REMINDER_AUDIT_ACTION = "reorder_reminders_setting_changed"


def _url(user):
    return f"/api/v1/admin/users/{user.id}/notification-settings"


def _delivery_rows(user):
    return NotificationPreference.query.filter_by(
        user_id=user.id, notification_type=NotificationService.DELIVERY_TELEGRAM_STATUS_UPDATES_PREF_KEY
    ).all()


def _audits(user, action):
    return AuditLog.query.filter_by(action=action, resource_type="user", resource_id=str(user.id)).all()


def test_the_users_page_delivery_only_payload_keeps_working(client, db, manager_claim_headers):
    customer = make_customer(db, first_name="Dilnoza")

    response = client.put(
        _url(customer),
        json={"delivery_telegram_status_updates_enabled": False, "reason": "Customer asked by phone"},
        headers=manager_claim_headers,
    )

    assert response.status_code == 200, response.get_data(as_text=True)
    settings = response.get_json()["data"]["notification_settings"]
    assert (
        settings["delivery_telegram_status_updates_enabled"],
        settings["delivery_telegram_status_updates_source"],
    ) == (
        False,
        "explicit",
    )
    assert settings["reorder_reminders"] == {
        "enabled": True,
        "changed_by_kind": None,
        "changed_via": None,
        "changed_at": None,
        "reason": None,
        "state": "on",
    }
    assert [row.is_enabled for row in _delivery_rows(customer)] == [False]
    assert ReorderReminderSetting.query.filter_by(user_id=customer.id).count() == 0
    # The delivery field keeps its own audit trail, with the reason; no reminder audit exists.
    [delivery_audit] = _audits(customer, DELIVERY_AUDIT_ACTION)
    assert delivery_audit.additional_data["reason"] == "Customer asked by phone"
    assert _audits(customer, REMINDER_AUDIT_ACTION) == []


def test_the_users_page_reminder_only_payload_writes_no_delivery_row(client, db, manager_user, manager_claim_headers):
    customer = make_customer(db, first_name="Javlon")

    response = client.put(
        _url(customer),
        json={"reorder_reminders_enabled": False, "reason": "Moved to another city"},
        headers=manager_claim_headers,
    )

    assert response.status_code == 200, response.get_data(as_text=True)
    settings = response.get_json()["data"]["notification_settings"]
    reminders = settings["reorder_reminders"]
    assert (reminders["enabled"], reminders["changed_by_kind"], reminders["changed_via"], reminders["reason"]) == (
        False,
        "admin",
        "admin_ui",
        "Moved to another city",
    )
    assert reminders["changed_at"] is not None
    # The delivery field is untouched: still the default, no row, no delivery audit.
    assert (
        settings["delivery_telegram_status_updates_enabled"],
        settings["delivery_telegram_status_updates_source"],
    ) == (
        True,
        "default",
    )
    assert _delivery_rows(customer) == []
    assert _audits(customer, DELIVERY_AUDIT_ACTION) == []
    row = db.session.get(ReorderReminderSetting, customer.id)
    assert (row.enabled, row.changed_by_kind, row.changed_via, row.reason, row.changed_by_user_id) == (
        False,
        "admin",
        "admin_ui",
        "Moved to another city",
        manager_user.id,
    )
    assert ReorderReminderEvent.query.filter_by(user_id=customer.id, kind="setting_changed").count() == 1
    assert len(_audits(customer, REMINDER_AUDIT_ACTION)) == 1


def test_a_manager_can_switch_reminders_back_on_with_a_reason(client, db, manager_user, manager_claim_headers):
    customer = make_customer(db, first_name="Nigora")
    ReorderReminderSettingsService.set(
        customer.id, False, actor_kind="admin", via="admin_ui", reason="Asked to stop", actor_user_id=manager_user.id
    )

    response = client.put(
        _url(customer),
        json={"reorder_reminders_enabled": True, "reason": "Customer asked to resume"},
        headers=manager_claim_headers,
    )

    assert response.status_code == 200, response.get_data(as_text=True)
    reminders = response.get_json()["data"]["notification_settings"]["reorder_reminders"]
    assert (reminders["enabled"], reminders["changed_by_kind"], reminders["reason"]) == (
        True,
        "admin",
        "Customer asked to resume",
    )


def test_turning_reminders_off_from_a_being_reminded_row_suspends_its_episode(
    client, db, admin_user, admin_claim_headers
):
    customer = make_customer(db, first_name="Kamola")
    episode = staff_episode(
        db,
        customer,
        anchor=delivered_order(db, customer, datetime(2026, 9, 1, 7, 0, tzinfo=UTC)),
        status="open",
        step=1,
        reminders_sent=1,
        next_action_on=date(2026, 10, 9),
        due_on=date(2026, 10, 2),
    )

    # The Lapsed customers page's row action sends exactly the Users page's reminder-only payload.
    response = client.put(
        _url(customer),
        json={"reorder_reminders_enabled": False, "reason": "Asked us to stop by phone"},
        headers=admin_claim_headers,
    )

    assert response.status_code == 200, response.get_data(as_text=True)
    db.session.expire_all()
    stored = db.session.get(ReorderReminderEpisode, episode.id)
    assert (stored.status, stored.suspended_reason) == ("suspended", "admin_off")
    # set() appends the episode's `suspended` event in the same transaction, naming the admin.
    [suspended] = ReorderReminderEvent.query.filter_by(user_id=customer.id, kind="suspended").all()
    assert (suspended.episode_id, suspended.detail, suspended.actor_user_id) == (
        episode.id,
        "admin_off",
        admin_user.id,
    )
    assert _delivery_rows(customer) == []


@contextmanager
def _closed_by_a_concurrent_writer(db, episode_id, at):
    """The daily run closes `episode_id` ('ordered') and commits just before the admin's suspend
    UPDATE reaches the row: the instant before that statement executes, the row is closed on the
    same connection, which is what READ COMMITTED shows the UPDATE once the run's commit lands."""
    fired = []

    def close_first(_conn, cursor, statement, parameters, _context, _executemany):
        if fired or not statement.lstrip().upper().startswith("UPDATE REORDER_REMINDER_EPISODES"):
            return
        if "admin_off" not in repr(parameters):
            return
        fired.append(statement)
        raw = cursor.connection.cursor()
        raw.execute(
            "UPDATE reorder_reminder_episodes SET status = 'closed', close_reason = 'ordered', "
            f"closed_at = '{at.isoformat()}', next_action_on = NULL WHERE id = {int(episode_id)}"
        )
        raw.close()

    engine = db.engine
    event.listen(engine, "before_cursor_execute", close_first)
    try:
        yield fired
    finally:
        event.remove(engine, "before_cursor_execute", close_first)


def test_an_episode_a_concurrent_writer_closes_first_stays_closed_and_the_admin_off_still_saves(
    client, db, admin_user, admin_claim_headers
):
    """Final review A2 (spec §12: every transition is a conditional update). The 10:00 run closes the
    live lapse while an admin saves 'reminders off'. The suspend must leave that row closed: no
    status-matrix CHECK violation, so no 500, and the setting is saved. It records `suspended` only
    for the row it really moved (the dry-run row nobody else touched), never for the closed one."""
    customer = make_customer(db, first_name="Kamola")
    anchor = delivered_order(db, customer, datetime(2026, 9, 1, 7, 0, tzinfo=UTC))
    live = staff_episode(
        db, customer, anchor=anchor, status="open", step=1, reminders_sent=1,
        next_action_on=date(2026, 10, 9), due_on=date(2026, 10, 2),
    )
    simulated = staff_episode(
        db, customer, anchor=anchor, status="escalated", escalation_flag="long_lapsed",
        due_on=date(2026, 10, 2), is_dry_run=True,
    )
    closed_at = datetime(2026, 10, 9, 5, 0, tzinfo=UTC)

    with _closed_by_a_concurrent_writer(db, live.id, closed_at) as fired:
        response = client.put(
            _url(customer),
            json={"reorder_reminders_enabled": False, "reason": "Asked us to stop by phone"},
            headers=admin_claim_headers,
        )

    assert fired, "the suspend UPDATE never ran: the simulation did not fire"
    assert response.status_code == 200, response.get_data(as_text=True)
    db.session.expire_all()
    stored_live = db.session.get(ReorderReminderEpisode, live.id)
    stored_simulated = db.session.get(ReorderReminderEpisode, simulated.id)
    assert (stored_live.status, stored_live.close_reason, stored_live.suspended_at) == ("closed", "ordered", None)
    assert (stored_simulated.status, stored_simulated.suspended_reason) == ("suspended", "admin_off")
    suspended = ReorderReminderEvent.query.filter_by(user_id=customer.id, kind="suspended").all()
    assert [(row.episode_id, row.detail, row.actor_user_id, row.is_dry_run) for row in suspended] == [
        (simulated.id, "admin_off", admin_user.id, True)
    ]
    setting = db.session.get(ReorderReminderSetting, customer.id)
    assert (setting.enabled, setting.changed_by_kind, setting.reason) == (False, "admin", "Asked us to stop by phone")


def test_the_admin_off_and_the_run_share_one_suspendable_status_constant():
    """Final review A2: one spelling of "which statuses a lapse can be suspended from"."""
    from business_app.models.reorder_reminder import ACTIVE_EPISODE_STATUSES, SUSPENDABLE_EPISODE_STATUSES
    from business_app.services.reorder_reminders import engine, settings

    assert SUSPENDABLE_EPISODE_STATUSES == ("open", "escalated")
    assert set(SUSPENDABLE_EPISODE_STATUSES) == set(ACTIVE_EPISODE_STATUSES) - {"suspended"}
    assert not hasattr(engine, "_SUSPENDABLE_STATUSES")
    assert not hasattr(settings, "_SUSPENDED_BY_ADMIN_OFF")
    assert engine.SUSPENDABLE_EPISODE_STATUSES is settings.SUSPENDABLE_EPISODE_STATUSES


def test_an_admin_off_upgrades_a_customers_own_off_over_http(client, db, manager_user, manager_claim_headers):
    """D20 (amendment 16): the Users switch is an admin off, and an admin off always applies."""
    primary = make_customer(db, first_name="Sevinch")
    sibling = make_customer(db, first_name="Sabina")
    link(db, primary, sibling)
    ReorderReminderSettingsService.set(
        sibling.id, False, actor_kind="customer", via="telegram_button", now=datetime(2026, 10, 3, 9, 0, tzinfo=UTC)
    )

    response = client.put(
        _url(primary),
        json={"reorder_reminders_enabled": False, "reason": "Customer called to confirm the stop"},
        headers=manager_claim_headers,
    )

    assert response.status_code == 200, response.get_data(as_text=True)
    reminders = response.get_json()["data"]["notification_settings"]["reorder_reminders"]
    assert (reminders["enabled"], reminders["changed_by_kind"], reminders["changed_via"], reminders["reason"]) == (
        False,
        "admin",
        "admin_ui",
        "Customer called to confirm the stop",
    )
    event = ReorderReminderEvent.query.filter_by(user_id=primary.id, kind="setting_changed").one()
    assert event.detail == "off"


def test_both_fields_in_one_put_write_both(client, db, manager_claim_headers):
    customer = make_customer(db, first_name="Lola")

    response = client.put(
        _url(customer),
        json={
            "delivery_telegram_status_updates_enabled": False,
            "reorder_reminders_enabled": False,
            "reason": "Customer wants no Telegram at all",
        },
        headers=manager_claim_headers,
    )

    assert response.status_code == 200, response.get_data(as_text=True)
    assert [row.is_enabled for row in _delivery_rows(customer)] == [False]
    assert db.session.get(ReorderReminderSetting, customer.id).enabled is False
    assert len(_audits(customer, DELIVERY_AUDIT_ACTION)) == 1
    assert len(_audits(customer, REMINDER_AUDIT_ACTION)) == 1


@pytest.mark.parametrize(
    "body, error_code",
    [
        # The PUT's own checks (amendment 20): at least one field, and the delivery field's rules.
        ({"reason": "Nothing to change"}, None),
        (
            {
                "delivery_telegram_status_updates_enabled": None,
                "reorder_reminders_enabled": None,
                "reason": "Null is absent",
            },
            None,
        ),
        ({"delivery_telegram_status_updates_enabled": 0, "reason": "Customer asked"}, None),
        ({"delivery_telegram_status_updates_enabled": False, "reason": "   "}, None),
        ({"delivery_telegram_status_updates_enabled": False}, None),
        # The reminder field's refusals are set()'s own codes, passed straight through.
        ({"reorder_reminders_enabled": False, "reason": "   "}, "REASON_REQUIRED"),
        ({"reorder_reminders_enabled": False}, "REASON_REQUIRED"),
        # A reason that is not a string is a missing reason, never a 500 (whichever field is sent).
        ({"reorder_reminders_enabled": False, "reason": 123}, "REASON_REQUIRED"),
        ({"reorder_reminders_enabled": False, "reason": True}, "REASON_REQUIRED"),
        ({"delivery_telegram_status_updates_enabled": False, "reason": ["x"]}, None),
        ({"delivery_telegram_status_updates_enabled": False, "reason": {}}, None),
        ({"reorder_reminders_enabled": "off", "reason": "Customer asked"}, "REORDER_REMINDER_SETTING_INVALID"),
        ({"reorder_reminders_enabled": 0, "reason": "Customer asked"}, "REORDER_REMINDER_SETTING_INVALID"),
        # Both fields: a refusal on either side leaves the other unwritten, in either order.
        # An invalid delivery field with a valid reminder field: the reminder row is never written.
        (
            {
                "delivery_telegram_status_updates_enabled": 0,
                "reorder_reminders_enabled": False,
                "reason": "Customer asked",
            },
            None,
        ),
        (
            {
                "delivery_telegram_status_updates_enabled": "off",
                "reorder_reminders_enabled": True,
                "reason": "Customer asked",
            },
            None,
        ),
        # Both fields and no reason: the delivery field needs one first, so nothing reaches set().
        ({"delivery_telegram_status_updates_enabled": False, "reorder_reminders_enabled": False}, None),
        ({"delivery_telegram_status_updates_enabled": False, "reorder_reminders_enabled": False, "reason": "  "}, None),
        # A valid delivery field with an invalid reminder field: the staged delivery row is rolled back.
        (
            {
                "delivery_telegram_status_updates_enabled": False,
                "reorder_reminders_enabled": "off",
                "reason": "Customer asked",
            },
            "REORDER_REMINDER_SETTING_INVALID",
        ),
        (
            {
                "delivery_telegram_status_updates_enabled": True,
                "reorder_reminders_enabled": 1,
                "reason": "Customer asked",
            },
            "REORDER_REMINDER_SETTING_INVALID",
        ),
    ],
    ids=[
        "no-field",
        "null-fields-are-absent",
        "delivery-int-toggle",
        "delivery-blank-reason",
        "delivery-no-reason",
        "reminder-blank-reason",
        "reminder-no-reason",
        "reminder-int-reason",
        "reminder-bool-reason",
        "delivery-list-reason",
        "delivery-object-reason",
        "reminder-string-toggle",
        "reminder-int-toggle",
        "invalid-delivery-valid-reminder-off",
        "invalid-delivery-valid-reminder-on",
        "both-no-reason",
        "both-blank-reason",
        "valid-delivery-bad-reminder-string",
        "valid-delivery-bad-reminder-int",
    ],
)
def test_an_invalid_body_is_refused_and_writes_nothing(client, db, manager_claim_headers, body, error_code):
    customer = make_customer(db, first_name="Malika")

    response = client.put(_url(customer), json=body, headers=manager_claim_headers)

    assert response.status_code == 400
    assert (response.get_json().get("data") or {}).get("error_code") == error_code
    # Nothing may be left staged on the session either: a commit right after the refusal (any later
    # write on the same session) must not persist a half-applied PUT.
    db.session.commit()
    db.session.expire_all()
    assert _delivery_rows(customer) == []
    assert ReorderReminderSetting.query.filter_by(user_id=customer.id).count() == 0
    assert ReorderReminderEvent.query.filter_by(user_id=customer.id).count() == 0
    assert _audits(customer, DELIVERY_AUDIT_ACTION) == []
    assert _audits(customer, REMINDER_AUDIT_ACTION) == []


@pytest.mark.parametrize("reason", [123, True, ["x"], {}], ids=["int", "bool", "list", "object"])
def test_a_non_string_reason_on_the_delivery_field_gets_the_reason_required_message(
    app, client, db, manager_claim_headers, reason
):
    customer = make_customer(db, first_name="Rayhon")

    response = client.put(
        _url(customer),
        json={"delivery_telegram_status_updates_enabled": False, "reason": reason},
        headers=manager_claim_headers,
    )

    with app.app_context():
        expected = get_translation("api.notifications.validation.reason_required")
    assert response.status_code == 400
    assert response.get_json()["errors"] == [expected]
    assert _delivery_rows(customer) == []


def test_a_refused_reminder_field_leaves_an_existing_delivery_choice_untouched(client, db, manager_claim_headers):
    """The refusal must not commit the delivery change, even over an existing explicit row."""
    customer = make_customer(db, first_name="Zarina")
    first = client.put(
        _url(customer),
        json={"delivery_telegram_status_updates_enabled": False, "reason": "Customer asked"},
        headers=manager_claim_headers,
    )
    assert first.status_code == 200, first.get_data(as_text=True)

    refused = client.put(
        _url(customer),
        json={
            "delivery_telegram_status_updates_enabled": True,
            "reorder_reminders_enabled": "off",
            "reason": "Back on",
        },
        headers=manager_claim_headers,
    )

    assert refused.status_code == 400
    assert refused.get_json()["data"]["error_code"] == "REORDER_REMINDER_SETTING_INVALID"
    # A commit right after the refusal (any later write on the same session) must not persist the
    # staged True over the existing False.
    db.session.commit()
    db.session.expire_all()
    assert [row.is_enabled for row in _delivery_rows(customer)] == [False]
    assert len(_audits(customer, DELIVERY_AUDIT_ACTION)) == 1


@pytest.mark.parametrize(
    "body",
    [["delivery_telegram_status_updates_enabled"], "delivery_telegram_status_updates_enabled", 7, True],
    ids=["list", "string", "number", "true"],
)
def test_a_body_that_is_not_an_object_is_a_400_not_a_500(client, db, manager_claim_headers, body):
    customer = make_customer(db, first_name="Odina")

    response = client.put(_url(customer), json=body, headers=manager_claim_headers)

    assert response.status_code == 400
    assert _delivery_rows(customer) == []


def test_a_missing_customer_is_a_404_and_writes_nothing(client, db, manager_claim_headers):
    response = client.put(
        "/api/v1/admin/users/99999999/notification-settings",
        json={"reorder_reminders_enabled": False, "reason": "No such customer"},
        headers=manager_claim_headers,
    )

    assert response.status_code == 404
    assert ReorderReminderSetting.query.filter_by(user_id=99999999).count() == 0
    assert ReorderReminderEvent.query.filter_by(user_id=99999999).count() == 0


def test_the_get_publishes_the_clusters_effective_reminder_setting_with_its_source(client, db, admin_claim_headers):
    primary = make_customer(db, first_name="Sevara")
    sibling = make_customer(db, first_name="Sitora")
    link(db, primary, sibling)
    ReorderReminderSettingsService.set(
        sibling.id, False, actor_kind="customer", via="telegram_button", now=datetime(2026, 10, 3, 9, 0, tzinfo=UTC)
    )
    db.session.commit()

    response = client.get(_url(primary), headers=admin_claim_headers)

    assert response.status_code == 200, response.get_data(as_text=True)
    assert response.get_json()["data"]["notification_settings"]["reorder_reminders"] == {
        "enabled": False,
        "changed_by_kind": "customer",
        "changed_via": "telegram_button",
        "changed_at": "2026-10-03T09:00:00+00:00",
        "reason": None,
        "state": "customer_off",
    }


def test_the_get_publishes_an_admins_reason_to_staff(client, db, manager_user, admin_claim_headers):
    customer = make_customer(db, first_name="Gulnora")
    ReorderReminderSettingsService.set(
        customer.id, False, actor_kind="admin", via="admin_ui", reason="Asked to stop", actor_user_id=manager_user.id
    )

    response = client.get(_url(customer), headers=admin_claim_headers)

    assert response.status_code == 200, response.get_data(as_text=True)
    settings = response.get_json()["data"]["notification_settings"]
    assert settings["reorder_reminders"]["reason"] == "Asked to stop"
    assert settings["delivery_telegram_status_updates_enabled"] is True


def test_the_get_of_a_customer_with_no_setting_row_says_enabled(client, db, admin_claim_headers):
    customer = make_customer(db, first_name="Hilola")

    response = client.get(_url(customer), headers=admin_claim_headers)

    assert response.status_code == 200, response.get_data(as_text=True)
    assert response.get_json()["data"]["notification_settings"]["reorder_reminders"] == {
        "enabled": True,
        "changed_by_kind": None,
        "changed_via": None,
        "changed_at": None,
        "reason": None,
        "state": "on",
    }


@pytest.mark.parametrize(
    "writes, state",
    [
        pytest.param([], "on", id="never-changed"),
        pytest.param([("customer", False)], "customer_off", id="customer-off"),
        pytest.param([("admin", False)], "admin_off", id="admin-off"),
        pytest.param([("customer", False), ("admin", False)], "admin_off", id="admin-off-over-customer-off"),
        pytest.param([("admin", False), ("customer", False)], "admin_off", id="customer-stop-over-admin-off"),
        pytest.param([("customer", False), ("customer", True)], "on", id="customer-back-on"),
        pytest.param([("admin", False), ("admin", True)], "on", id="staff-back-on"),
    ],
)
def test_the_users_page_reads_the_published_state_of_the_effective_setting(
    client, db, manager_user, admin_claim_headers, writes, state
):
    """Final review deferred 507 (ruling): the staff shape publishes `state` in {on, customer_off,
    admin_off}, the backend's own `EffectiveSetting.customer_off` / `admin_off`, so the Users page
    offers *Exclude (staff off)* on `state === 'customer_off'` and re-derives nothing. Driven through
    the GET the page calls, after the writes each route really makes."""
    customer = make_customer(db, first_name="Malika")
    for hour, (actor, enabled) in enumerate(writes):
        admin = actor == "admin"
        ReorderReminderSettingsService.set(
            customer.id,
            enabled,
            actor_kind=actor,
            via="admin_ui" if admin else "web_settings",
            reason="Staff change" if admin else None,
            actor_user_id=manager_user.id if admin else customer.id,
            now=datetime(2026, 10, 3, 9 + hour, 0, tzinfo=UTC),
        )

    response = client.get(_url(customer), headers=admin_claim_headers)

    assert response.status_code == 200, response.get_data(as_text=True)
    published = response.get_json()["data"]["notification_settings"]["reorder_reminders"]
    assert published["state"] == state
    assert set(published) == {"enabled", "changed_by_kind", "changed_via", "changed_at", "reason", "state"}


def test_a_customer_token_is_forbidden_on_both_verbs(app, client, db):
    customer = make_customer(db, first_name="Intruder")
    target = make_customer(db, first_name="Target")
    headers = claim_headers(app, customer, "customer")

    got = client.get(_url(target), headers=headers)
    put = client.put(_url(target), json={"reorder_reminders_enabled": False, "reason": "Not allowed"}, headers=headers)

    assert (got.status_code, put.status_code) == (403, 403)
    assert ReorderReminderSetting.query.filter_by(user_id=target.id).count() == 0
    assert ReorderReminderEvent.query.filter_by(user_id=target.id).count() == 0


def test_an_operator_cannot_change_the_setting(app, client, db):
    """PUT stays manager-or-higher (the route's existing guard); an operator's write is refused."""
    operator = make_customer(db, first_name="Operator", role=UserRole.OPERATOR)
    target = make_customer(db, first_name="Target2")

    response = client.put(
        _url(target),
        json={"reorder_reminders_enabled": False, "reason": "Operators may not"},
        headers=claim_headers(app, operator, "operator"),
    )

    assert response.status_code == 403
    assert ReorderReminderSetting.query.filter_by(user_id=target.id).count() == 0
