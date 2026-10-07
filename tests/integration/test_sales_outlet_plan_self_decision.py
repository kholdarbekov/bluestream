"""A manager who holds an agent profile cannot change his own due plan through the outlet routes.

Final-review I1 (owner decision 2026-10-05). An outlet's class and cadence override set its
cadence, the cadence sets `next_visit_due_at`, and that decides the 01:20 frozen due set that
compliance and the pay gate measure. Reassigning an outlet or marking it lost moves it off the
plan just the same. So the four outlet-plan writes the admin Outlets page makes (PUT class /
cadence, assign, bulk-assign, mark-lost) run `pay_rules.check_self_decision` against the
outlet's DUE OWNER (`OutletService.due_owner_id`, the Python twin of `agent_due_filter`) before
anything is written:
- Mansur, a MANAGER with an agent profile, is refused 403 `SALES_PAY_SELF_DECISION` about an
  outlet on his own plan, and the row is unchanged;
- Malika, a manager with no profile, makes the same five calls about it: 200;
- Anvar, an ADMIN who is the outlet's due owner, is allowed (tagging an admin's own plan change
  is deferred: there is no actor record for these writes yet).
"""

import pytest

from business_app.models.sales import Outlet
from business_app.services.sales.outlet_service import OutletService
from shared.enums import UserRole
from tests.integration.sales_pay_builders import claim_headers, make_outlet, pay_call, refusal, staff_agent, utc_at

pytestmark = pytest.mark.integration

OUTLETS = "/api/v1/admin/sales/outlets"
ONBOARDED_AT = utc_at(2026, 9, 1, 9)


def _plan_columns(db, outlet_id):
    db.session.expire_all()
    outlet = db.session.get(Outlet, outlet_id)
    return (
        outlet.outlet_class,
        outlet.cadence_days_override,
        outlet.assigned_agent_user_id,
        outlet.stage,
        outlet.lost_reason,
    )


def _owned_by(db, owner, *, how):
    """An active class-B outlet in Chilanzar on `owner`'s plan: assigned to them, or onboarded by
    them while nobody owns it (the two halves of `agent_due_filter`)."""
    if how == "assigned":
        return make_outlet(db, onboarded_by=None, created_at=ONBOARDED_AT, assigned_agent=owner)
    return make_outlet(db, onboarded_by=owner, created_at=ONBOARDED_AT)


def _plan_changes(outlet_id, colleague_id):
    """The five outlet-plan writes the Outlets page makes, with the bodies it sends."""
    return [
        ("PUT", f"{OUTLETS}/{outlet_id}", {"class": "A"}),
        ("PUT", f"{OUTLETS}/{outlet_id}", {"cadence_days_override": 365}),
        ("POST", f"{OUTLETS}/{outlet_id}/assign", {"agent_user_id": colleague_id}),
        ("POST", f"{OUTLETS}/bulk-assign", {"district": "chilanzar", "agent_user_id": colleague_id}),
        ("POST", f"{OUTLETS}/{outlet_id}/mark-lost", {"reason": "closed", "note": "Shutters down"}),
    ]


@pytest.mark.parametrize("how", ["assigned", "onboarded"])
def test_a_manager_agent_is_refused_every_change_to_his_own_plan_and_nothing_is_written(app, client, db, how):
    mansur = staff_agent(db, phone="+998901238411", first_name="Mansur", role=UserRole.MANAGER)
    aziz = staff_agent(db, phone="+998901238412", first_name="Aziz")
    outlet = _owned_by(db, mansur, how=how)
    headers = claim_headers(app, mansur, "manager")
    before = _plan_columns(db, outlet.id)

    for method, path, body in _plan_changes(outlet.id, aziz.id):
        details = refusal(client, method, path, headers, body, status=403, code="SALES_PAY_SELF_DECISION")
        assert details == {"agent_user_id": mansur.id}, (method, path)
        assert _plan_columns(db, outlet.id) == before, (method, path)


def test_assigning_to_himself_or_bulk_assigning_a_district_to_himself_is_refused_too(app, client, db):
    """The target agent is a due owner after the write, so naming himself is the same decision."""
    mansur = staff_agent(db, phone="+998901238411", first_name="Mansur", role=UserRole.MANAGER)
    aziz = staff_agent(db, phone="+998901238412", first_name="Aziz")
    outlet = _owned_by(db, aziz, how="assigned")
    headers = claim_headers(app, mansur, "manager")
    before = _plan_columns(db, outlet.id)

    refusal(
        client, "POST", f"{OUTLETS}/{outlet.id}/assign", headers, {"agent_user_id": mansur.id},
        status=403, code="SALES_PAY_SELF_DECISION",
    )
    refusal(
        client, "POST", f"{OUTLETS}/bulk-assign", headers, {"district": "chilanzar", "agent_user_id": mansur.id},
        status=403, code="SALES_PAY_SELF_DECISION",
    )
    assert _plan_columns(db, outlet.id) == before


def test_a_manager_agent_still_edits_the_rest_of_his_outlet_and_may_resend_its_class(app, client, db):
    """Only a CHANGE to class or cadence is a plan decision: notes, or the class the row already
    has (the edit form sends what the admin touched), go through."""
    mansur = staff_agent(db, phone="+998901238411", first_name="Mansur", role=UserRole.MANAGER)
    outlet = _owned_by(db, mansur, how="assigned")
    headers = claim_headers(app, mansur, "manager")

    pay_call(client, "PUT", f"{OUTLETS}/{outlet.id}", headers, {"notes": "Back door after 14:00"})
    pay_call(client, "PUT", f"{OUTLETS}/{outlet.id}", headers, {"class": "B"})

    db.session.expire_all()
    stored = db.session.get(Outlet, outlet.id)
    assert (stored.notes, stored.outlet_class) == ("Back door after 14:00", "B")


def test_a_manager_without_a_profile_makes_the_same_changes(app, client, db, manager_user, manager_claim_headers):
    mansur = staff_agent(db, phone="+998901238411", first_name="Mansur", role=UserRole.MANAGER)
    aziz = staff_agent(db, phone="+998901238412", first_name="Aziz")
    outlet = _owned_by(db, mansur, how="assigned")

    for method, path, body in _plan_changes(outlet.id, aziz.id):
        pay_call(client, method, path, manager_claim_headers, body)

    assert _plan_columns(db, outlet.id) == ("A", 365, aziz.id, "lost", "closed")


def test_an_admin_who_is_the_due_owner_is_allowed(app, client, db):
    anvar = staff_agent(db, phone="+998901238413", first_name="Anvar", role=UserRole.ADMIN)
    aziz = staff_agent(db, phone="+998901238412", first_name="Aziz")
    outlet = _owned_by(db, anvar, how="onboarded")
    headers = claim_headers(app, anvar, "admin")

    for method, path, body in _plan_changes(outlet.id, aziz.id):
        pay_call(client, method, path, headers, body)

    assert _plan_columns(db, outlet.id) == ("A", 365, aziz.id, "lost", "closed")


@pytest.mark.parametrize(
    "assigned, onboarded, expected",
    [("a", "b", "a"), (None, "b", "b"), (None, None, None), ("a", None, "a")],
)
def test_due_owner_id_is_the_python_twin_of_agent_due_filter(db, assigned, onboarded, expected):
    """For each ownership shape, an outlet matches `agent_due_filter(X)` exactly when
    `due_owner_id(outlet) == X`, for X in {A, B}."""
    agents = {
        "a": staff_agent(db, phone="+998901238421", first_name="Alisher"),
        "b": staff_agent(db, phone="+998901238422", first_name="Bekzod"),
    }
    outlet = make_outlet(
        db,
        onboarded_by=agents[onboarded] if onboarded else None,
        created_at=ONBOARDED_AT,
        assigned_agent=agents[assigned] if assigned else None,
    )

    assert OutletService.due_owner_id(outlet) == (agents[expected].id if expected else None)
    for agent in agents.values():
        matches = Outlet.query.filter(Outlet.id == outlet.id, OutletService.agent_due_filter(agent.id)).count() == 1
        assert matches is (OutletService.due_owner_id(outlet) == agent.id), agent.first_name
