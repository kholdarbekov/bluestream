"""Customer Later / Stop over HTTP (spec §6 "Customer actions", §7.3 "Callbacks", §14).

The two routes the reminder buttons call, with exactly the requests the customer bot sends
(telegram_bot/api_client.py, Task 16b): Later posts to its episode with NO body, Stop posts
{"episode_id": <id>|null}; both carry the customer's JWT. The rules themselves (D13 step-1 and max
rule, D20 off-over-off) belong to ReorderReminderActions (Task 12) and are pinned there. These tests
pin the HTTP contract the bot branches on (`snooze_days`, a TOP-LEVEL `error_code`), that each route
acts on the caller only, and what every answer leaves behind in the ladder.
"""

import pytest

from business_app.api.reorder_reminders import MAX_LATER_TAPS_PER_HOUR
from business_app.models.reorder_reminder import ReorderReminderEvent, ReorderReminderSetting
from business_app.services.reorder_reminders.settings import ReorderReminderSettingsService as Settings
from tests.integration.reorder_reminder_builders import (  # noqa: F401 - `*_fixture` are the `sends`, `clock` fixtures
    WEEKLY,
    at,
    clock_fixture,
    customer_headers,
    customer_with_deliveries,
    day,
    delivered_order,
    event_rows,
    link,
    make_customer,
    make_episode,
    only_lapse,
    run_engine,
    sends_fixture,
)

pytestmark = pytest.mark.integration

STOP = "/api/v1/reorder-reminders/stop"
NOT_ACTIVE = "REORDER_REMINDER_NOT_ACTIVE"
# What differs on every error response; everything else is the contract.
VOLATILE = ("timestamp", "request_id", "trace_id")


def _later(episode_id):
    return f"/api/v1/reorder-reminders/{episode_id}/later"


def _error_body(response):
    body = response.get_json()
    for key in VOLATILE:
        body.pop(key, None)
    return body


def _not_active(url):
    """The refusal the bot reads: the code is TOP-LEVEL (telegram_bot's `_response_error_code`)."""
    return {
        "error": "VALIDATION_ERROR",
        "message": "This reminder is no longer active",
        "status_code": 400,
        "path": url,
        "method": "POST",
        "error_code": NOT_ACTIVE,
        "details": {"validation_errors": []},
    }


def _after_reminder_one(db, first_name="Ali"):
    """A weekly customer whose reminder 1 the 10:00 run on day 11 just published."""
    user, orders = customer_with_deliveries(db, WEEKLY, telegram=True, first_name=first_name)
    run_engine(11)
    return user, orders, only_lapse(orders[-1])


def _stop_events(user):
    return ReorderReminderEvent.query.filter_by(user_id=user.id, kind="stop").all()


# --------------------------------------------------------------------------------------- Later


def test_later_answers_the_snooze_days_and_moves_reminder_two(app, client, db, sends, clock):
    """D13: the bot reads `snooze_days` for its confirmation; reminder 2 moves to today + 14."""
    ali, orders, row = _after_reminder_one(db)
    clock(at(11, 10, 30))

    response = client.post(_later(row.id), headers=customer_headers(app, ali))

    assert response.status_code == 200, response.get_json()
    assert response.get_json() == {"success": True, "data": {"snooze_days": 14}}
    row = only_lapse(orders[-1])
    assert row.next_action_on == day(25)
    assert event_rows(episode=row)[-1] == ("later", None, 1, None)
    later = ReorderReminderEvent.query.filter_by(episode_id=row.id, kind="later").one()
    assert (later.user_id, later.payload) == (ali.id, {"next_action_on": day(25).isoformat()})


def test_the_ladder_follows_the_new_date_after_a_later_tap(app, client, db, sends, clock):
    """Later over HTTP, then the daily runs: nothing on the old day, reminder 2 on the new one."""
    ali, orders, row = _after_reminder_one(db)
    clock(at(11, 10, 30))
    assert client.post(_later(row.id), headers=customer_headers(app, ali)).status_code == 200

    run_engine(18)
    assert only_lapse(orders[-1]).step == 1
    run_engine(25)

    assert only_lapse(orders[-1]).step == 2
    assert sends == [(row.id, 1), (row.id, 2)]


def test_a_second_later_tap_answers_the_same_and_moves_nothing(app, client, db, sends, clock):
    """Review Focus #5: tapping Later twice is not an error and never moves the ladder."""
    ali, orders, row = _after_reminder_one(db)
    headers = customer_headers(app, ali)
    clock(at(11, 10, 30))
    first = client.post(_later(row.id), headers=headers)
    clock(at(11, 16))

    second = client.post(_later(row.id), headers=headers)

    assert (first.status_code, second.status_code) == (200, 200)
    assert first.get_json() == second.get_json() == {"success": True, "data": {"snooze_days": 14}}
    assert only_lapse(orders[-1]).next_action_on == day(25)
    assert [kind for kind, *_rest in event_rows(episode=row)].count("later") == 2


def test_later_after_the_second_reminder_is_refused_with_a_top_level_code(app, client, db, sends, clock):
    """Review Focus #5: once step != 1 the tap is refused, which makes the bot retire the message."""
    ali, orders, row = _after_reminder_one(db)
    run_engine(18)
    assert only_lapse(orders[-1]).step == 2
    clock(at(18, 11))

    response = client.post(_later(row.id), headers=customer_headers(app, ali))

    assert response.status_code == 400
    assert _error_body(response) == _not_active(_later(row.id))
    assert only_lapse(orders[-1]).next_action_on == day(25)
    assert "later" not in [kind for kind, *_rest in event_rows(episode=row)]


@pytest.mark.parametrize("status", ["escalated", "suspended", "resolved", "closed"])
def test_later_on_a_lapse_that_is_no_longer_open_is_refused_and_changes_nothing(app, client, db, status):
    ali = make_customer(db, telegram=True)
    order = delivered_order(db, ali, at(0))
    row = make_episode(db, ali, anchor=order, status=status, step=1)
    before = (row.status, row.next_action_on)

    response = client.post(_later(row.id), headers=customer_headers(app, ali))

    assert response.status_code == 400
    assert _error_body(response) == _not_active(_later(row.id))
    db.session.refresh(row)
    assert (row.status, row.next_action_on) == before
    assert event_rows(episode=row) == []


def test_another_customers_reminder_reads_like_a_missing_one_and_moves_nothing(app, client, db, sends):
    """Customer actions act on the caller's own lapse only: an id alone reaches nothing, and the
    refusal is the one a missing id gets, so an id leaks nothing."""
    ali, orders, row = _after_reminder_one(db)
    vali = make_customer(db, first_name="Vali", telegram=True)
    headers = customer_headers(app, vali)
    before = row.next_action_on
    events_before = event_rows(episode=row)

    someone_elses = client.post(_later(row.id), headers=headers)
    missing = client.post(_later(row.id + 1000), headers=headers)

    assert (someone_elses.status_code, missing.status_code) == (400, 400)
    assert _error_body(someone_elses) == _not_active(_later(row.id))
    assert _error_body(missing) == _not_active(_later(row.id + 1000))
    db.session.refresh(row)
    assert row.next_action_on == before
    assert event_rows(episode=row) == events_before
    assert event_rows(user=vali) == []


def test_later_on_a_dry_run_reminder_is_refused(app, client, db, sends):
    """§13: a simulated lapse is no customer's reminder, whoever holds its id."""
    ali, orders = customer_with_deliveries(db, WEEKLY, telegram=True)
    run_engine(11, mode="dry_run")
    simulated = only_lapse(orders[-1], is_dry_run=True)
    before = simulated.next_action_on

    response = client.post(_later(simulated.id), headers=customer_headers(app, ali))

    assert response.status_code == 400
    assert _error_body(response) == _not_active(_later(simulated.id))
    db.session.refresh(simulated)
    assert simulated.next_action_on == before
    assert "later" not in [kind for kind, *_rest in event_rows(episode=simulated)]


def test_later_works_from_any_account_of_the_same_customer(app, client, db, sends, clock):
    """One person, two accounts: the web account's login answers for the Telegram account's lapse."""
    ali, orders, row = _after_reminder_one(db)
    ali_web = make_customer(db, first_name="Ali", registration_source="web")
    link(db, ali, ali_web)
    clock(at(11, 10, 30))

    response = client.post(_later(row.id), headers=customer_headers(app, ali_web))

    assert response.status_code == 200, response.get_json()
    assert response.get_json() == {"success": True, "data": {"snooze_days": 14}}
    assert only_lapse(orders[-1]).next_action_on == day(25)


# ---------------------------------------------------------------------------------------- Stop


def test_stop_switches_the_customer_off_and_links_the_reminder(app, client, db, sends, clock):
    """D5: the answer is the one published setting (customer-off, via the button), without a reason."""
    ali, orders, row = _after_reminder_one(db)
    frozen = clock(at(11, 10, 30))

    response = client.post(STOP, headers=customer_headers(app, ali), json={"episode_id": row.id})

    assert response.status_code == 200, response.get_json()
    assert response.get_json() == {
        "success": True,
        "data": {
            "reorder_reminders": {
                "enabled": False,
                "changed_by_kind": "customer",
                "changed_via": "telegram_button",
                "changed_at": frozen.isoformat(),
            }
        },
    }
    setting = db.session.get(ReorderReminderSetting, ali.id)
    assert (setting.enabled, setting.changed_by_kind, setting.changed_via) == (False, "customer", "telegram_button")
    assert [(kind, detail, step) for kind, detail, step, _channel in event_rows(user=ali)][-2:] == [
        ("stop", "telegram_button", 1),
        ("setting_changed", "off", None),
    ]
    (stop,) = _stop_events(ali)
    assert (stop.episode_id, stop.actor_user_id) == (row.id, ali.id)


def test_stop_silences_the_rest_of_the_ladder_but_the_follow_up_still_happens(app, client, db, sends, clock):
    """D5, through the daily runs: Stop ends the messages, not the follow-up."""
    ali, orders, row = _after_reminder_one(db)
    clock(at(11, 10, 30))
    assert client.post(STOP, headers=customer_headers(app, ali), json={"episode_id": row.id}).status_code == 200

    run_engine(18)
    run_engine(25)

    row = only_lapse(orders[-1])
    assert (row.status, row.escalation_flag) == ("escalated", "stopped")
    assert sends == [(row.id, 1)]


@pytest.mark.parametrize(
    "request_kwargs",
    [{"json": {"episode_id": None}}, {"json": {}}, {}],
    ids=["null episode", "empty object", "no body"],
)
def test_stop_works_with_no_reminder_at_all(app, client, db, clock, request_kwargs):
    vali = make_customer(db, first_name="Vali", telegram=True)
    frozen = clock(at(3, 10))

    response = client.post(STOP, headers=customer_headers(app, vali), **request_kwargs)

    assert response.status_code == 200, response.get_json()
    assert response.get_json()["data"]["reorder_reminders"] == {
        "enabled": False,
        "changed_by_kind": "customer",
        "changed_via": "telegram_button",
        "changed_at": frozen.isoformat(),
    }
    (stop,) = _stop_events(vali)
    assert (stop.episode_id, stop.step, stop.detail) == (None, None, "telegram_button")


def test_a_second_stop_answers_the_first_change_and_writes_nothing_new(app, client, db, clock):
    """D20 for a customer over their own off: the attempt is recorded, the setting keeps its first stamp."""
    vali = make_customer(db, first_name="Vali", telegram=True)
    headers = customer_headers(app, vali)
    clock(at(3, 10))
    first = client.post(STOP, headers=headers, json={"episode_id": None})
    clock(at(4, 10))

    second = client.post(STOP, headers=headers, json={"episode_id": None})

    assert (first.status_code, second.status_code) == (200, 200)
    assert second.get_json() == first.get_json()
    assert [detail for kind, detail, *_rest in event_rows(user=vali) if kind == "setting_changed"] == [
        "off",
        "already_off",
    ]


def test_stop_never_turns_an_admin_off_into_a_customer_off_nor_publishes_the_reason(app, client, db, admin_user, clock):
    """D20: the setting the customer sees is the admin's, and the staff-only reason stays staff-only."""
    vali = make_customer(db, first_name="Vali", telegram=True)
    admin_at = Settings.set(
        vali.id,
        False,
        actor_kind="admin",
        via="admin_ui",
        reason="Asked not to be contacted",
        actor_user_id=admin_user.id,
        now=at(3, 9),
    ).changed_at
    clock(at(3, 10))

    response = client.post(STOP, headers=customer_headers(app, vali), json={"episode_id": None})

    assert response.status_code == 200, response.get_json()
    assert response.get_json() == {
        "success": True,
        "data": {
            "reorder_reminders": {
                "enabled": False,
                "changed_by_kind": "admin",
                "changed_via": "admin_ui",
                "changed_at": admin_at.isoformat(),
            }
        },
    }
    assert "Asked not to be contacted" not in response.get_data(as_text=True)
    setting = db.session.get(ReorderReminderSetting, vali.id)
    assert (setting.changed_by_kind, setting.changed_via) == ("admin", "admin_ui")


def test_stop_naming_someone_elses_lapse_switches_off_only_the_caller_and_links_no_lapse(app, client, db, sends):
    ali, orders, row = _after_reminder_one(db)
    vali = make_customer(db, first_name="Vali", telegram=True)

    response = client.post(STOP, headers=customer_headers(app, vali), json={"episode_id": row.id})

    assert response.status_code == 200, response.get_json()
    assert response.get_json()["data"]["reorder_reminders"]["enabled"] is False
    (stop,) = _stop_events(vali)
    assert (stop.episode_id, stop.step) == (None, None)
    assert db.session.get(ReorderReminderSetting, vali.id).enabled is False
    assert db.session.get(ReorderReminderSetting, ali.id) is None
    assert "stop" not in [kind for kind, *_rest in event_rows(episode=row)]


def test_stop_naming_a_dry_run_lapse_switches_the_caller_off_and_links_no_lapse(app, client, db, sends):
    ali, orders = customer_with_deliveries(db, WEEKLY, telegram=True)
    run_engine(11, mode="dry_run")
    simulated = only_lapse(orders[-1], is_dry_run=True)

    response = client.post(STOP, headers=customer_headers(app, ali), json={"episode_id": simulated.id})

    assert response.status_code == 200, response.get_json()
    assert db.session.get(ReorderReminderSetting, ali.id).enabled is False
    (stop,) = _stop_events(ali)
    assert (stop.episode_id, stop.is_dry_run) == (None, False)


@pytest.mark.parametrize("status", ["escalated", "suspended", "resolved", "closed"])
def test_stop_never_refuses_for_the_state_of_the_lapse(app, client, db, status):
    """§7.3: Later is refused once a reminder has moved on; Stop never is."""
    ali = make_customer(db, telegram=True)
    order = delivered_order(db, ali, at(0))
    row = make_episode(db, ali, anchor=order, status=status, step=1)

    response = client.post(STOP, headers=customer_headers(app, ali), json={"episode_id": row.id})

    assert response.status_code == 200, response.get_json()
    assert response.get_json()["data"]["reorder_reminders"]["enabled"] is False
    assert db.session.get(ReorderReminderSetting, ali.id).changed_via == "telegram_button"


def test_a_stop_body_can_never_claim_another_route(app, client, db):
    """The only client of this route is the Stop button, so the route names itself (§8 table)."""
    vali = make_customer(db, first_name="Vali", telegram=True)

    response = client.post(
        STOP, headers=customer_headers(app, vali), json={"episode_id": None, "via": "email_link", "actor": "admin"}
    )

    assert response.status_code == 200, response.get_json()
    published = response.get_json()["data"]["reorder_reminders"]
    assert (published["changed_by_kind"], published["changed_via"]) == ("customer", "telegram_button")


MALFORMED_STOP_BODIES = [
    pytest.param({"json": {"episode_id": "77"}}, id="text id"),
    pytest.param({"json": {"episode_id": 7.5}}, id="fractional id"),
    pytest.param({"json": {"episode_id": True}}, id="boolean id"),
    pytest.param({"json": [77]}, id="json list"),
    pytest.param({"json": "77"}, id="json text"),
    pytest.param({"data": "{not json", "content_type": "application/json"}, id="broken json"),
    pytest.param({"data": "episode_id=77", "content_type": "application/x-www-form-urlencoded"}, id="form post"),
]


@pytest.mark.parametrize("request_kwargs", MALFORMED_STOP_BODIES)
def test_stop_refuses_a_malformed_body_and_writes_nothing(app, client, db, request_kwargs):
    """The contract is a 400 `Validation failed` with reasons; the reasons' wording is Pydantic's, not pinned."""
    vali = make_customer(db, first_name="Vali", telegram=True)

    response = client.post(STOP, headers=customer_headers(app, vali), **request_kwargs)

    assert response.status_code == 400
    body = response.get_json()
    assert body["success"] is False
    assert body["message"] == "Validation failed"
    assert isinstance(body["errors"], list) and body["errors"]
    assert ReorderReminderSetting.query.count() == 0
    assert ReorderReminderEvent.query.count() == 0


# -------------------------------------------------------------------------- login and budget


@pytest.mark.parametrize("url", [_later(1), STOP], ids=["later", "stop"])
@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer not-a-token"}, {"Authorization": "Token abc"}],
    ids=["no login", "garbage token", "wrong scheme"],
)
def test_both_routes_need_a_customer_login(app, db, url, headers):
    # A fresh client: the shared one keeps the JWT cookie an earlier web login left on it.
    response = app.test_client().post(url, headers=headers, json={"episode_id": None})

    assert response.status_code == 401
    assert ReorderReminderSetting.query.count() == 0
    assert ReorderReminderEvent.query.count() == 0


def test_later_has_a_tap_budget_per_customer_and_the_rejected_tap_applies_nothing(app, client, db, sends, clock):
    """The bot calls from one address for every customer, so the budget is per login, not per IP."""
    ali, orders, row = _after_reminder_one(db)
    vali = make_customer(db, first_name="Vali", telegram=True)
    clock(at(11, 10, 30))
    for _ in range(MAX_LATER_TAPS_PER_HOUR):
        assert client.post(_later(row.id), headers=customer_headers(app, ali)).status_code == 200
    next_action_on = only_lapse(orders[-1]).next_action_on
    later_events = ReorderReminderEvent.query.filter_by(episode_id=row.id, kind="later").count()
    assert later_events == MAX_LATER_TAPS_PER_HOUR

    over = client.post(_later(row.id), headers=customer_headers(app, ali))
    other = client.post(_later(row.id), headers=customer_headers(app, vali))

    assert over.status_code == 429
    assert over.get_json()["error"] == "RATE_LIMIT_EXCEEDED"
    assert only_lapse(orders[-1]).next_action_on == next_action_on
    assert ReorderReminderEvent.query.filter_by(episode_id=row.id, kind="later").count() == later_events
    assert other.status_code == 400  # Vali's own answer (not his lapse), not Ali's 429
    assert _error_body(other) == _not_active(_later(row.id))


def test_stop_is_never_rate_limited(app, client, db):
    """An opt-out must always work (§7.3: Stop never refuses; §14 sets no limit)."""
    vali = make_customer(db, first_name="Vali", telegram=True)

    answers = [
        client.post(STOP, headers=customer_headers(app, vali), json={"episode_id": None}).status_code
        for _ in range(MAX_LATER_TAPS_PER_HOUR + 5)
    ]

    assert answers == [200] * (MAX_LATER_TAPS_PER_HOUR + 5)
