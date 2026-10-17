"""Recording the mandatory outcome, and the permission that gates the page (spec §6, §9.1, §14;
plan Task 20c). Driven through the route the outcome modal posts to, with the exact body.

The modal's choices come from the summary's `outcomes`, and the Resolve button from each row's
`can_resolve`: both are read back over HTTP here, so the write path is held to what the page offers.
The page itself is shown from `permissions.can_manage_lapsed_customers`, which the login response
carries; the tie test checks it against the guard of the routes for every `UserRole`.
"""

from datetime import date, datetime, timezone

import pytest

from business_app.models.reorder_reminder import ReorderReminderEpisode, ReorderReminderEvent, ReorderReminderSetting
from business_app.services.auth_service import AuthService
from business_app.services.reorder_reminders.settings import ReorderReminderSettingsService
from business_app.utils.password_security import hash_password
from shared.enums import UserRole
from tests.integration.reorder_reminder_builders import (  # noqa: F401 - the `frozen` fixture
    claim_headers,
    delivered_order,
    event_rows,
    frozen_fixture,
    make_customer,
    staff_episode,
)

pytestmark = pytest.mark.integration

LOGIN = "/api/v1/auth/login"
EPISODES = "/api/v1/admin/reorder-reminders/episodes"
SUMMARY = "/api/v1/admin/reorder-reminders/summary"
NOT_RESOLVABLE = "REORDER_REMINDER_NOT_RESOLVABLE"
UTC = timezone.utc


def _resolve_url(episode_id):
    return f"/api/v1/admin/reorder-reminders/episodes/{episode_id}/resolve"


def _episode(db, name, **fields):
    customer = make_customer(db, first_name=name)
    anchor = delivered_order(db, customer, datetime(2026, 8, 1, 7, 0, tzinfo=UTC))
    return customer, staff_episode(db, customer, anchor=anchor, due_on=date(2026, 9, 1), **fields)


def _tab(client, headers, tab):
    response = client.get(EPISODES, query_string={"tab": tab}, headers=headers)
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()["data"]["items"]


def test_resolving_an_escalated_row_records_the_outcome(client, db, manager_user, manager_claim_headers):
    _, episode = _episode(db, "Oybek", status="escalated", escalation_flag="no_response", step=2, reminders_sent=2)

    response = client.post(
        _resolve_url(episode.id),
        json={"outcome": "away", "note": "Dachada, oktyabr oxirida qaytadi"},
        headers=manager_claim_headers,
    )

    assert response.status_code == 200, response.get_data(as_text=True)
    published = response.get_json()["data"]["episode"]
    assert {
        key: published[key] for key in ("episode_id", "status", "outcome", "outcome_note", "resolved_by_user_id")
    } == {
        "episode_id": episode.id,
        "status": "resolved",
        "outcome": "away",
        "outcome_note": "Dachada, oktyabr oxirida qaytadi",
        "resolved_by_user_id": manager_user.id,
    }
    assert published["resolved_at"].endswith("+00:00")
    db.session.expire_all()
    stored = db.session.get(ReorderReminderEpisode, episode.id)
    assert (stored.status, stored.outcome, stored.resolved_by_user_id) == ("resolved", "away", manager_user.id)
    # The one event the action writes: the audit trail names who closed it, and how.
    resolved_events = ReorderReminderEvent.query.filter_by(episode_id=episode.id, kind="resolved").all()
    assert [(e.detail, e.actor_user_id, e.payload) for e in resolved_events] == [
        ("away", manager_user.id, {"note": "Dachada, oktyabr oxirida qaytadi"})
    ]


def test_a_suspended_row_that_had_escalated_can_be_resolved(client, db, admin_claim_headers):
    _, episode = _episode(
        db,
        "Parvina",
        status="suspended",
        suspended_reason="subscription",
        escalation_flag="no_response",
        escalated_at=datetime(2026, 10, 1, 5, 0, tzinfo=UTC),
        step=2,
        reminders_sent=2,
    )

    response = client.post(_resolve_url(episode.id), json={"outcome": "other_supplier"}, headers=admin_claim_headers)

    assert response.status_code == 200, response.get_data(as_text=True)
    assert response.get_json()["data"]["episode"]["status"] == "resolved"


def test_do_not_contact_on_a_live_row_writes_admin_off(client, db, frozen, manager_claim_headers):
    customer, episode = _episode(db, "Qodir", status="escalated", escalation_flag="stopped", step=2, reminders_sent=0)
    # The customer pressed Stop on a reminder first: the `stopped` escalation's real cause.
    ReorderReminderSettingsService.set(
        customer.id, False, actor_kind="customer", via="telegram_button", now=datetime(2026, 10, 1, 6, 0, tzinfo=UTC)
    )
    assert ReorderReminderSettingsService.effective_for([customer.id]).customer_off

    response = client.post(
        _resolve_url(episode.id),
        json={"outcome": "do_not_contact", "note": "Asked not to be called"},
        headers=manager_claim_headers,
    )

    assert response.status_code == 200, response.get_data(as_text=True)
    effective = ReorderReminderSettingsService.effective_for([customer.id])
    # D20 (amendment 16): an admin off always applies, so it upgrades the customer's off.
    assert effective.admin_off
    assert (effective.enabled, effective.changed_by_kind, effective.changed_via, effective.reason) == (
        False,
        "admin",
        "worklist",
        "Worklist outcome: do_not_contact — Asked not to be called",
    )
    changes = (
        ReorderReminderEvent.query.filter_by(user_id=customer.id, kind="setting_changed")
        .order_by(ReorderReminderEvent.id)
        .all()
    )
    # Two real writes; the admin's is never recorded as an `already_off` attempt.
    assert [(event.detail, event.payload["changed_by_kind"], event.payload["changed_via"]) for event in changes] == [
        ("off", "customer", "telegram_button"),
        ("off", "admin", "worklist"),
    ]
    # The page refetches after the modal: the closed row shows the admin's off, not the customer's.
    (row,) = _tab(client, manager_claim_headers, "resolved")
    assert (row["episode_id"], row["outcome"], row["setting"]["changed_by_kind"]) == (
        episode.id,
        "do_not_contact",
        "admin",
    )


def test_do_not_contact_with_no_note_writes_admin_off_with_the_bare_reason(client, db, manager_claim_headers):
    customer, episode = _episode(db, "Nodir", status="escalated", escalation_flag="no_response")

    response = client.post(_resolve_url(episode.id), json={"outcome": "do_not_contact"}, headers=manager_claim_headers)

    assert response.status_code == 200, response.get_data(as_text=True)
    assert response.get_json()["data"]["episode"]["outcome_note"] is None
    effective = ReorderReminderSettingsService.effective_for([customer.id])
    assert effective.admin_off
    assert (effective.enabled, effective.changed_by_kind, effective.changed_via, effective.reason) == (
        False,
        "admin",
        "worklist",
        "Worklist outcome: do_not_contact",
    )


def test_do_not_contact_on_a_dry_run_row_writes_no_setting(client, db, manager_claim_headers):
    customer, episode = _episode(db, "Rustam", status="escalated", escalation_flag="no_response", is_dry_run=True)

    response = client.post(_resolve_url(episode.id), json={"outcome": "do_not_contact"}, headers=manager_claim_headers)

    assert response.status_code == 200, response.get_data(as_text=True)
    assert ReorderReminderSetting.query.filter_by(user_id=customer.id).count() == 0
    assert ReorderReminderEvent.query.filter_by(user_id=customer.id, kind="setting_changed").count() == 0
    db.session.expire_all()
    assert db.session.get(ReorderReminderEpisode, episode.id).status == "resolved"


def test_every_outcome_the_modal_offers_resolves(client, db, manager_claim_headers):
    """The modal's options are the summary's `outcomes`; each must be accepted by the route."""
    summary = client.get(SUMMARY, headers=manager_claim_headers)
    assert summary.status_code == 200, summary.get_data(as_text=True)
    offered = summary.get_json()["data"]["outcomes"]
    assert offered

    resolved = {}
    for index, outcome in enumerate(offered):
        _, episode = _episode(db, f"Mijoz{index}", status="escalated", escalation_flag="no_response")
        response = client.post(_resolve_url(episode.id), json={"outcome": outcome}, headers=manager_claim_headers)
        assert response.status_code == 200, (outcome, response.get_data(as_text=True))
        resolved[outcome] = response.get_json()["data"]["episode"]["outcome"]

    assert resolved == {outcome: outcome for outcome in offered}


def test_resolve_accepts_exactly_the_rows_the_page_offers_it_for(client, db, frozen, manager_claim_headers):
    """Each row publishes `can_resolve`; the write path honours it in both directions, and the closed
    rows then leave Needs follow-up for Resolved."""
    _episode(db, "Escalated", status="escalated", escalation_flag="no_response", step=2, reminders_sent=2)
    _episode(
        db,
        "Paused-after-escalation",
        status="suspended",
        suspended_reason="subscription",
        escalation_flag="no_response",
        escalated_at=datetime(2026, 10, 1, 5, 0, tzinfo=UTC),
        step=2,
    )
    _episode(db, "Paused", status="suspended", suspended_reason="subscription")
    _episode(db, "Reminding", status="open", step=1, reminders_sent=1, next_action_on=date(2026, 10, 20))
    rows = [row for tab in ("follow_up", "reminding", "paused") for row in _tab(client, manager_claim_headers, tab)]
    offered = {row["episode_id"]: row["can_resolve"] for row in rows}
    assert sorted(offered.values()) == [False, False, True, True]

    statuses = {
        episode_id: client.post(_resolve_url(episode_id), json={"outcome": "other"}, headers=manager_claim_headers)
        for episode_id in offered
    }

    assert {episode_id: response.status_code for episode_id, response in statuses.items()} == {
        episode_id: 200 if can_resolve else 400 for episode_id, can_resolve in offered.items()
    }
    refused = [r for episode_id, r in statuses.items() if not offered[episode_id]]
    assert [r.get_json()["error_code"] for r in refused] == [NOT_RESOLVABLE, NOT_RESOLVABLE]
    assert _tab(client, manager_claim_headers, "follow_up") == []
    assert sorted(row["episode_id"] for row in _tab(client, manager_claim_headers, "resolved")) == sorted(
        episode_id for episode_id, can_resolve in offered.items() if can_resolve
    )


def test_a_double_submit_resolves_once(client, db, manager_user, manager_claim_headers):
    """The modal's OK tapped twice: the conditional UPDATE lets exactly one through."""
    _, episode = _episode(db, "Ikki", status="escalated", escalation_flag="no_response")
    body = {"outcome": "reordered", "note": "Buyurtma berdi"}

    first = client.post(_resolve_url(episode.id), json=body, headers=manager_claim_headers)
    second = client.post(_resolve_url(episode.id), json=body, headers=manager_claim_headers)

    assert first.status_code == 200, first.get_data(as_text=True)
    assert second.status_code == 400
    assert second.get_json()["error_code"] == NOT_RESOLVABLE
    assert [row for row in event_rows(episode=episode) if row[0] == "resolved"] == [
        ("resolved", "reordered", None, None)
    ]
    db.session.expire_all()
    stored = db.session.get(ReorderReminderEpisode, episode.id)
    assert (stored.status, stored.outcome, stored.outcome_note) == ("resolved", "reordered", "Buyurtma berdi")


def test_an_open_row_cannot_be_resolved(client, db, manager_claim_headers):
    _, episode = _episode(db, "Sanjar", status="open", step=1, reminders_sent=1, next_action_on=date(2026, 10, 20))

    response = client.post(_resolve_url(episode.id), json={"outcome": "reordered"}, headers=manager_claim_headers)

    assert response.status_code == 400
    assert response.get_json()["error_code"] == NOT_RESOLVABLE
    db.session.expire_all()
    assert db.session.get(ReorderReminderEpisode, episode.id).status == "open"


def test_an_unknown_outcome_or_a_stray_key_is_refused(client, db, manager_claim_headers):
    _, episode = _episode(db, "Toxir", status="escalated", escalation_flag="no_response")

    unknown = client.post(_resolve_url(episode.id), json={"outcome": "angry"}, headers=manager_claim_headers)
    stray = client.post(_resolve_url(episode.id), json={"outcome": "away", "flag": "x"}, headers=manager_claim_headers)
    missing = client.post(_resolve_url(episode.id), json={"note": "no outcome"}, headers=manager_claim_headers)

    assert (unknown.status_code, stray.status_code, missing.status_code) == (400, 400, 400)
    db.session.expire_all()
    assert db.session.get(ReorderReminderEpisode, episode.id).status == "escalated"


def test_the_note_is_optional_trimmed_and_capped(client, db, manager_claim_headers):
    notes = {}
    for label, body in (
        ("omitted", {"outcome": "other"}),
        ("null", {"outcome": "other", "note": None}),
        ("blank", {"outcome": "other", "note": "   "}),
        ("padded", {"outcome": "other", "note": "  Telefon o'chiq  "}),
        ("longest", {"outcome": "other", "note": "x" * 2000}),
        # The cap measures the trimmed note: padding never turns exactly 2000 characters into a 400.
        ("longest-padded", {"outcome": "other", "note": " \n" + "x" * 2000 + " \t "}),
    ):
        _, episode = _episode(db, label, status="escalated", escalation_flag="no_response")
        response = client.post(_resolve_url(episode.id), json=body, headers=manager_claim_headers)
        assert response.status_code == 200, (label, response.get_data(as_text=True))
        notes[label] = response.get_json()["data"]["episode"]["outcome_note"]
    _, too_long = _episode(db, "too-long", status="escalated", escalation_flag="no_response")
    refused = client.post(
        _resolve_url(too_long.id), json={"outcome": "other", "note": "x" * 2001}, headers=manager_claim_headers
    )
    _, too_long_padded = _episode(db, "too-long-padded", status="escalated", escalation_flag="no_response")
    refused_padded = client.post(
        _resolve_url(too_long_padded.id),
        json={"outcome": "other", "note": "  " + "x" * 2001 + "  "},
        headers=manager_claim_headers,
    )

    assert notes == {
        "omitted": None,
        "null": None,
        "blank": None,
        "padded": "Telefon o'chiq",
        "longest": "x" * 2000,
        "longest-padded": "x" * 2000,
    }
    assert (refused.status_code, refused_padded.status_code) == (400, 400)
    db.session.expire_all()
    assert [db.session.get(ReorderReminderEpisode, e.id).status for e in (too_long, too_long_padded)] == [
        "escalated",
        "escalated",
    ]


def test_a_missing_episode_is_404(client, db, manager_claim_headers):
    response = client.post(_resolve_url(987654), json={"outcome": "away"}, headers=manager_claim_headers)

    # The service's own refusal, not the router's "no such route" 404.
    assert (response.status_code, response.get_json()["error"], response.get_json()["message"]) == (
        404,
        "NOT_FOUND",
        "Lapse not found",
    )


def test_a_request_without_a_token_is_refused(client, db):
    _, episode = _episode(db, "Tokensiz", status="escalated", escalation_flag="no_response")

    response = client.post(_resolve_url(episode.id), json={"outcome": "away"})

    assert response.status_code == 401
    db.session.expire_all()
    assert db.session.get(ReorderReminderEpisode, episode.id).status == "escalated"


@pytest.mark.parametrize(
    "user_fixture,identifier_field,password,expected",
    [
        ("admin_user", "email", "AdminPassword123!", True),
        ("manager_user", "email", "ManagerPassword123!", True),
        ("operator_user", "email", "OperatorPassword123!", False),
        ("sales_agent_user", "phone", "AgentPassword123!", False),
    ],
    ids=["admin", "manager", "operator", "sales-agent"],
)
def test_login_publishes_the_lapsed_customers_flag_to_admins_and_managers(
    request, client, db, user_fixture, identifier_field, password, expected
):
    user = request.getfixturevalue(user_fixture)

    response = client.post(LOGIN, json={"identifier": getattr(user, identifier_field), "password": password})

    assert response.status_code == 200, response.get_data(as_text=True)
    assert response.get_json()["data"]["permissions"]["can_manage_lapsed_customers"] is expected


def test_a_driver_has_no_lapsed_customers_flag(db, delivery_driver):
    assert AuthService().get_user_permissions(delivery_driver.id)["can_manage_lapsed_customers"] is False


@pytest.mark.parametrize("role", list(UserRole), ids=lambda role: role.value)
def test_the_flag_and_the_worklist_routes_state_one_rule(app, client, db, role):
    """The flag the admin UI reads at login and the guard on the routes, for every role: the flag is
    true exactly when the summary answers 200 and the resolve route lets the request through, and
    false exactly when both refuse with 403 and the lapse is left as it was."""
    password = "RolePassword123!"
    user = make_customer(db, first_name=role.value, role=role)
    user.password_hash = hash_password(password)
    db.session.commit()
    _, episode = _episode(db, f"Lapse of {role.value}", status="escalated", escalation_flag="no_response")
    headers = claim_headers(app, user, role.value)

    login = client.post(LOGIN, json={"identifier": user.phone, "password": password})
    assert login.status_code == 200, login.get_data(as_text=True)
    flag = login.get_json()["data"]["permissions"]["can_manage_lapsed_customers"]
    summary = client.get(SUMMARY, headers=headers).status_code
    resolve = client.post(_resolve_url(episode.id), json={"outcome": "away"}, headers=headers).status_code

    # The role set itself is pinned (spec §14), so widening the flag and the guard together fails here.
    assert flag is (role in (UserRole.ADMIN, UserRole.MANAGER))
    assert (summary, resolve) == (200 if flag else 403, 200 if flag else 403)
    db.session.expire_all()
    assert db.session.get(ReorderReminderEpisode, episode.id).status == ("resolved" if flag else "escalated")
