"""The customer's reorder-reminder switch over GET/PUT /api/v1/notifications/preferences (spec §8).

Two clients write it: the bot's Profile -> Notifications toggle and the storefront's profile
switch. The server tells them apart by the platform detection that already exists, never by the
body: the bot's own User-Agent (read here from telegram_bot/api_client.py, so a rename there
breaks this file) and the storefront's `X-Platform: web` (static/js/pages/base.js).

Both clients send `reorder_reminders_enabled` only when the switch changed (Tasks 19b/19c), so a
PUT that carries any other preference must leave the reminder setting, its events and its audit
trail alone.
"""

import re
from datetime import datetime
from pathlib import Path

import pytest

from business_app.models.reorder_reminder import CUSTOMER_SETTING_VIAS, ReorderReminderEvent, ReorderReminderSetting
from business_app.services.notification_service import NotificationService
from business_app.services.reorder_reminders import settings as settings_module
from business_app.services.reorder_reminders.settings import ReorderReminderSettingsService as Settings
from business_app.utils.timezone_utils import ensure_utc
from tests.integration.reorder_reminder_builders import customer_headers, event_rows, link, make_customer

pytestmark = pytest.mark.integration

PREFERENCES = "/api/v1/notifications/preferences"
_API_CLIENT = Path(__file__).resolve().parents[2] / "telegram_bot" / "api_client.py"
BOT_USER_AGENT = re.search(r"'User-Agent': '([^']+)'", _API_CLIENT.read_text(encoding="utf-8")).group(1)
BOT = {"User-Agent": BOT_USER_AGENT}
WEB = {"X-Platform": "web"}
ADMIN_OFF_REASON = "Asked not to be contacted"


@pytest.fixture
def audits(monkeypatch):
    """Every audit row the settings writer asks for, by its keyword arguments."""
    calls = []
    monkeypatch.setattr(settings_module.audit_logger, "log_event", lambda **kwargs: calls.append(kwargs))
    return calls


@pytest.fixture
def ali(db):
    return make_customer(db, telegram=True)


def _put(client, app, user, body, **headers):
    return client.put(PREFERENCES, headers={**customer_headers(app, user), **headers}, json=body)


def _published(response):
    assert response.status_code == 200, response.get_json()
    return response.get_json()["data"]["preferences"]["reorder_reminders"]


def _same_instant(published_at, stored_at):
    """`changed_at` is published as an ISO string; SQLite hands the stored one back naive (UTC)."""
    return datetime.fromisoformat(published_at) == ensure_utc(stored_at)


def _admin_off(db, user, admin_user):
    Settings.set(
        user.id, False, actor_kind="admin", via="admin_ui", reason=ADMIN_OFF_REASON, actor_user_id=admin_user.id
    )
    db.session.commit()


# -------------------------------------------------------------------------------------- publish


def test_the_preferences_publish_the_default_effective_setting(app, client, db, ali):
    response = client.get(PREFERENCES, headers=customer_headers(app, ali))

    assert _published(response) == {"enabled": True, "changed_by_kind": None, "changed_via": None, "changed_at": None}


def test_the_get_publishes_a_stored_change_without_the_admins_reason(app, client, db, ali, admin_user):
    _admin_off(db, ali, admin_user)
    stored_at = db.session.get(ReorderReminderSetting, ali.id).changed_at

    published = _published(client.get(PREFERENCES, headers=customer_headers(app, ali)))

    assert _same_instant(published.pop("changed_at"), stored_at)
    assert published == {"enabled": False, "changed_by_kind": "admin", "changed_via": "admin_ui"}


def test_the_setting_is_the_latest_change_across_linked_accounts(app, client, db, ali):
    ali_work = make_customer(db, first_name="Ali")
    link(db, ali, ali_work)
    Settings.set(ali_work.id, False, actor_kind="customer", via="telegram_button", actor_user_id=ali_work.id)

    published = _published(client.get(PREFERENCES, headers=customer_headers(app, ali)))

    assert (published["enabled"], published["changed_by_kind"], published["changed_via"]) == (
        False,
        "customer",
        "telegram_button",
    )


def test_the_setting_is_published_beside_every_existing_preference(app, client, db, ali):
    preferences = client.get(PREFERENCES, headers=customer_headers(app, ali)).get_json()["data"]["preferences"]

    assert set(preferences) == {
        "user_id", "email_enabled", "sms_enabled", "push_enabled", "in_app_enabled", "telegram_enabled",
        "delivery_telegram_status_updates_enabled", "reorder_reminders",
        "order_notifications", "delivery_notifications", "payment_notifications", "promotion_notifications",
        "system_notifications", "loyalty_notifications", "security_notifications", "reminder_notifications",
        "quiet_hours_enabled", "quiet_hours_start", "quiet_hours_end", "digest_enabled", "digest_frequency",
        "updated_at",
    }  # fmt: skip
    assert preferences["delivery_telegram_status_updates_enabled"] is True


# ---------------------------------------------------------------------------------- the route


def test_the_bots_toggle_is_recorded_as_telegram_settings(app, client, db, ali, audits):
    response = _put(client, app, ali, {"reorder_reminders_enabled": False}, **BOT)

    published = _published(response)
    assert datetime.fromisoformat(published["changed_at"])
    assert {key: published[key] for key in ("enabled", "changed_by_kind", "changed_via")} == {
        "enabled": False,
        "changed_by_kind": "customer",
        "changed_via": "telegram_settings",
    }
    assert set(published) == {"enabled", "changed_by_kind", "changed_via", "changed_at"}
    row = db.session.get(ReorderReminderSetting, ali.id)
    assert (row.enabled, row.changed_by_kind, row.changed_via, row.changed_by_user_id, row.reason) == (
        False,
        "customer",
        "telegram_settings",
        ali.id,
        None,
    )
    assert event_rows(user=ali) == [("setting_changed", "off", None, None)]
    assert [(call["action"], call["resource_id"]) for call in audits] == [
        ("reorder_reminders_setting_changed", str(ali.id))
    ]


def test_the_web_switch_is_recorded_as_web_settings(app, client, db, ali):
    published = _published(_put(client, app, ali, {"reorder_reminders_enabled": False}, **WEB))

    assert (published["enabled"], published["changed_by_kind"], published["changed_via"]) == (
        False,
        "customer",
        "web_settings",
    )
    assert db.session.get(ReorderReminderSetting, ali.id).changed_via == "web_settings"


def test_a_client_that_names_no_platform_is_the_web_switch(app, client, db, ali):
    """No X-Platform and a browser-ish User-Agent: detect_platform reads web, never the bot."""
    published = _published(_put(client, app, ali, {"reorder_reminders_enabled": False}))

    assert published["changed_via"] == "web_settings"


@pytest.mark.parametrize(
    ("x_platform", "expected_via"),
    [
        ("mobile", "web_settings"),
        ("Telegram", "telegram_settings"),
        ("garbage", "web_settings"),
        ("telegram", "telegram_settings"),
    ],
)
def test_whatever_x_platform_says_the_write_lands_on_an_allowed_route(app, client, db, ali, x_platform, expected_via):
    """`X-Platform` is the client's own word, so any value must still land on one of the two
    customer routes the CHECK allows: only "telegram" (any case) is the bot's, the rest are the web's.
    The customer starts on, so the off is a real write and never a D20 no-op."""
    response = _put(
        client,
        app,
        ali,
        {"reorder_reminders_enabled": False},
        **{"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)", "X-Platform": x_platform},
    )

    published = _published(response)
    assert published["enabled"] is False
    assert published["changed_via"] in CUSTOMER_SETTING_VIAS
    assert published["changed_via"] == expected_via
    row = db.session.get(ReorderReminderSetting, ali.id)
    assert (row.enabled, row.changed_via) == (False, expected_via)


@pytest.mark.parametrize("headers", [BOT, WEB], ids=["bot", "web"])
def test_a_body_cannot_choose_its_own_route_or_actor(app, client, db, ali, admin_user, headers):
    forged = {
        "reorder_reminders_enabled": False,
        "via": "admin_ui",
        "changed_via": "worklist",
        "actor_kind": "admin",
        "changed_by_kind": "admin",
        "actor_user_id": admin_user.id,
        "changed_by_user_id": admin_user.id,
        "reason": "forged",
    }

    published = _published(_put(client, app, ali, forged, **headers))

    expected_via = "telegram_settings" if headers == BOT else "web_settings"
    assert (published["changed_by_kind"], published["changed_via"]) == ("customer", expected_via)
    row = db.session.get(ReorderReminderSetting, ali.id)
    assert (row.changed_by_kind, row.changed_via, row.changed_by_user_id, row.reason) == (
        "customer",
        expected_via,
        ali.id,
        None,
    )
    assert db.session.get(ReorderReminderSetting, admin_user.id) is None


def test_the_switch_turns_back_on_from_the_same_client(app, client, db, ali):
    _put(client, app, ali, {"reorder_reminders_enabled": False}, **BOT)

    published = _published(_put(client, app, ali, {"reorder_reminders_enabled": True}, **BOT))

    assert (published["enabled"], published["changed_by_kind"], published["changed_via"]) == (
        True,
        "customer",
        "telegram_settings",
    )
    assert [detail for _, detail, _, _ in event_rows(user=ali)] == ["off", "on"]


def test_the_switch_and_another_preference_in_one_put_both_apply(app, client, db, ali):
    response = _put(
        client,
        app,
        ali,
        {"delivery_telegram_status_updates_enabled": False, "reorder_reminders_enabled": False},
        **BOT,
    )

    preferences = response.get_json()["data"]["preferences"]
    assert preferences["delivery_telegram_status_updates_enabled"] is False
    assert preferences["reorder_reminders"]["enabled"] is False
    assert db.session.get(ReorderReminderSetting, ali.id).enabled is False


@pytest.mark.parametrize("bad", ["off", "false", 0, 1, [], {}], ids=repr)
def test_a_non_boolean_is_refused_and_writes_nothing(app, client, db, ali, audits, bad):
    response = _put(client, app, ali, {"reorder_reminders_enabled": bad}, **WEB)

    assert response.status_code == 400
    assert response.get_json()["message"] == "reorder_reminders_enabled must be a boolean"
    assert db.session.get(ReorderReminderSetting, ali.id) is None
    assert event_rows(user=ali) == []
    assert audits == []


def test_a_refused_switch_leaves_the_other_preferences_in_the_same_put_unwritten(app, client, db, ali):
    response = _put(
        client,
        app,
        ali,
        {"delivery_telegram_status_updates_enabled": False, "reorder_reminders_enabled": "off"},
        **BOT,
    )

    assert response.status_code == 400
    setting = NotificationService().get_delivery_telegram_status_updates_setting(ali.id)
    assert setting["delivery_telegram_status_updates_enabled"] is True


def test_a_refused_delivery_flag_leaves_a_valid_switch_in_the_same_put_unwritten(app, client, db, ali, audits):
    """The reverse order: the switch is valid, the other preference is not. `set()` commits, so it
    must run only after every other validation has passed."""
    response = _put(
        client,
        app,
        ali,
        {"delivery_telegram_status_updates_enabled": "nope", "reorder_reminders_enabled": False},
        **BOT,
    )

    assert response.status_code == 400
    assert response.get_json()["message"] == "delivery_telegram_status_updates_enabled must be a boolean"
    assert db.session.get(ReorderReminderSetting, ali.id) is None
    assert event_rows(user=ali) == []
    assert audits == []


# -------------------------------------------------------------- unchanged: the key is omitted


@pytest.mark.parametrize("headers", [BOT, WEB], ids=["bot", "web"])
def test_a_put_carrying_only_another_preference_writes_no_reminder_setting(app, client, db, ali, audits, headers):
    """The clients send the key only when it changed, so saving another preference never writes an
    'on' row, an event or an audit row for the reminders."""
    response = _put(client, app, ali, {"delivery_telegram_status_updates_enabled": False}, **headers)

    assert _published(response) == {"enabled": True, "changed_by_kind": None, "changed_via": None, "changed_at": None}
    assert db.session.get(ReorderReminderSetting, ali.id) is None
    assert ReorderReminderEvent.query.count() == 0
    assert audits == []


def test_a_null_switch_is_the_same_as_an_omitted_one(app, client, db, ali, audits):
    response = _put(client, app, ali, {"reorder_reminders_enabled": None, "email_enabled": True}, **WEB)

    assert response.status_code == 200
    assert db.session.get(ReorderReminderSetting, ali.id) is None
    assert ReorderReminderEvent.query.count() == 0
    assert audits == []


def test_another_preference_never_disturbs_an_existing_setting(app, client, db, ali):
    first = _published(_put(client, app, ali, {"reorder_reminders_enabled": False}, **BOT))

    after = _published(_put(client, app, ali, {"delivery_telegram_status_updates_enabled": False}, **WEB))

    assert after == first
    assert [detail for _, detail, _, _ in event_rows(user=ali)] == ["off"]


# ------------------------------------------------------------------------------------ D7, D20


def test_a_customer_turning_reminders_back_on_wins_over_an_admin_off(app, client, db, ali, admin_user):
    """Spec §8: an enabled=true write always applies, even over an admin's off."""
    _admin_off(db, ali, admin_user)

    published = _published(_put(client, app, ali, {"reorder_reminders_enabled": True}, **WEB))

    assert (published["enabled"], published["changed_by_kind"], published["changed_via"]) == (
        True,
        "customer",
        "web_settings",
    )
    row = db.session.get(ReorderReminderSetting, ali.id)
    assert (row.enabled, row.changed_by_kind, row.changed_via, row.reason) == (True, "customer", "web_settings", None)
    assert [detail for _, detail, _, _ in event_rows(user=ali)] == ["off", "on"]


def test_a_customer_off_over_an_admin_off_changes_nothing(app, client, db, ali, admin_user, audits):
    """D20: the customer's off over an off is a no-op, so the publish still shows the admin's off,
    and never its reason."""
    _admin_off(db, ali, admin_user)
    audits.clear()
    stamp = db.session.get(ReorderReminderSetting, ali.id).changed_at

    published = _published(_put(client, app, ali, {"reorder_reminders_enabled": False}, **BOT))

    assert _same_instant(published.pop("changed_at"), stamp)
    assert published == {"enabled": False, "changed_by_kind": "admin", "changed_via": "admin_ui"}
    db.session.expire_all()
    row = db.session.get(ReorderReminderSetting, ali.id)
    assert (row.enabled, row.changed_by_kind, row.changed_via, row.reason, row.changed_at) == (
        False,
        "admin",
        "admin_ui",
        ADMIN_OFF_REASON,
        stamp,
    )
    assert [detail for _, detail, _, _ in event_rows(user=ali)] == ["off", "already_off"]
    assert audits == []


def test_a_customer_off_over_a_linked_accounts_customer_off_changes_nothing(app, client, db, ali):
    ali_work = make_customer(db, first_name="Ali")
    link(db, ali, ali_work)
    Settings.set(ali_work.id, False, actor_kind="customer", via="telegram_button", actor_user_id=ali_work.id)

    published = _published(_put(client, app, ali, {"reorder_reminders_enabled": False}, **WEB))

    assert (published["changed_by_kind"], published["changed_via"]) == ("customer", "telegram_button")
    assert db.session.get(ReorderReminderSetting, ali.id) is None


# ------------------------------------------------------------------- the one publisher


def test_the_one_publisher_adds_the_admins_reason_only_when_asked(db, ali, admin_user):
    """Master amendment 15: `reorder_reminders_view` is the one publisher of the setting, through
    `EffectiveSetting.to_dict`. The customer preferences call it without the reason (and without the
    staff `state`, deferred 507); Task 21a's admin GET passes include_reason=True."""
    _admin_off(db, ali, admin_user)

    customer_view = NotificationService.reorder_reminders_view(ali.id)
    admin_view = NotificationService.reorder_reminders_view(ali.id, include_reason=True)

    assert (customer_view["enabled"], customer_view["changed_by_kind"], customer_view["changed_via"]) == (
        False,
        "admin",
        "admin_ui",
    )
    assert "reason" not in customer_view and "state" not in customer_view
    assert admin_view == {**customer_view, "reason": ADMIN_OFF_REASON, "state": "admin_off"}


def test_the_publisher_takes_the_jwt_identity_string(db, ali):
    """The route hands over `get_jwt_identity()`, a string."""
    assert NotificationService.reorder_reminders_view(str(ali.id)) == NotificationService.reorder_reminders_view(ali.id)
