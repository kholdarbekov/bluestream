"""StaffService.create_sales_agent / update_sales_agent and SalesAgentAccountService."""
from datetime import UTC, datetime

import pytest

from business_app.models.sales import Outlet, SalesAgentProfile
from business_app.models.sales_visits import Visit
from business_app.models.user import User
from business_app.services.sales.agent_account_service import SalesAgentAccountService
from business_app.services.staff_service import StaffService
from business_app.utils.exceptions import ConflictError, ValidationError
from shared.enums import UserRole, UserStatus, UserType
from tests.unit.test_sales_agent_role import make_sales_agent_user

PAYLOAD = {
    "full_name": "Sardor Alimov",
    "phone": "+998901234574",
    "districts": ["yunusabad", "chilanzar"],
    "weekly_new_outlet_target": 5,
    "employment_type": "employee",
    "notes": "Starts Monday",
}


def test_create_sales_agent_creates_user_profile_and_roles(db, admin_user):
    user = StaffService.create_sales_agent(dict(PAYLOAD), created_by=admin_user.id)

    fresh = User.query.get(user.id)
    assert fresh.role == UserRole.SALES_AGENT
    assert fresh.user_type == UserType.STAFF
    assert fresh.staff_roles == ["sales_agent"]
    assert fresh.first_name == "Sardor" and fresh.last_name == "Alimov"
    profile = SalesAgentProfile.query.filter_by(user_id=user.id).one()
    assert profile.is_active is True
    assert profile.districts == ["yunusabad", "chilanzar"]
    assert profile.weekly_new_outlet_target == 5
    assert profile.employment_type == "employee"
    assert profile.created_by_user_id == admin_user.id


def test_create_sales_agent_on_existing_driver_keeps_both_roles(db, second_delivery_driver):
    user = StaffService.create_sales_agent({"user_id": second_delivery_driver.id, "districts": []})
    assert set(user.staff_roles) == {"delivery_driver", "sales_agent"}
    assert user.role == UserRole.DELIVERY_DRIVER  # existing users keep users.role


def test_create_sales_agent_twice_conflicts(db):
    StaffService.create_sales_agent(dict(PAYLOAD))
    with pytest.raises(ConflictError) as excinfo:
        StaffService.create_sales_agent(dict(PAYLOAD))
    assert excinfo.value.error_code == "STAFF_SALES_AGENT_EXISTS"


def test_create_sales_agent_rejects_unknown_district(db):
    with pytest.raises(ValidationError) as excinfo:
        StaffService.create_sales_agent({**PAYLOAD, "districts": ["atlantis"]})
    assert excinfo.value.error_code == "SALES_DISTRICT_INVALID"


def test_update_staff_roles_force_keeps_sales_agent_while_profile_exists(db):
    user = StaffService.create_sales_agent(dict(PAYLOAD))
    StaffService.update_staff_roles(user.id, ["operator"])
    assert set(User.query.get(user.id).staff_roles) == {"operator", "sales_agent"}


def test_update_sales_agent_edits_profile_and_user(db):
    user = StaffService.create_sales_agent(dict(PAYLOAD))
    StaffService.update_sales_agent(user.id, {"full_name": "Sardor A.", "districts": ["yakkasaray"], "weekly_new_outlet_target": 3, "is_active": False})
    fresh = User.query.get(user.id)
    profile = SalesAgentProfile.query.filter_by(user_id=user.id).one()
    assert fresh.first_name == "Sardor" and fresh.last_name == "A."
    assert profile.districts == ["yakkasaray"]
    assert profile.weekly_new_outlet_target == 3
    assert profile.is_active is False


def test_update_sales_agent_requires_profile(db):
    stranger = make_sales_agent_user(db, phone="+998901234575", role=UserRole.CUSTOMER)
    with pytest.raises(ValidationError) as excinfo:
        StaffService.update_sales_agent(stranger.id, {"districts": []})
    assert excinfo.value.error_code == "SALES_AGENT_PROFILE_REQUIRED"


def test_account_service_lists_and_counts(db):
    user = StaffService.create_sales_agent(dict(PAYLOAD))
    items, total, summary = SalesAgentAccountService.list_agents(page=1, per_page=20)
    assert total == 1
    assert items[0]["user_id"] == user.id
    assert items[0]["is_active"] is True
    assert items[0]["outlets_assigned"] == 0
    assert items[0]["outlets_active"] == 0
    assert items[0]["visits_today"] == 0
    assert items[0]["orders_today"] == 0
    assert summary == {"total_agents": 1, "active_agents": 1, "visits_today": 0, "orders_today": 0}

    SalesAgentAccountService.set_active(user.id, False, actor_id=user.id)
    _, _, summary = SalesAgentAccountService.list_agents(page=1, per_page=20, status="inactive")
    assert summary == {"total_agents": 1, "active_agents": 0, "visits_today": 0, "orders_today": 0}


def test_the_list_row_counts_todays_work_and_drops_a_written_off_outlet(db):
    """R7: ONE definition of an agent's outlets, and the row's own numbers come from the
    metrics service. `stage == 'lost'` is nobody's territory — the old row counter counted
    it, so an agent who correctly closed a dead prospect kept carrying it forever.

    The wall clock is the right clock here: `list_agents` is the live admin page, and a row
    seeded at `now` is inside the current local day by construction.
    """
    agent = StaffService.create_sales_agent(dict(PAYLOAD))
    kept = Outlet(name="Bahor", outlet_type="grocery_store", stage="active", assigned_agent_user_id=agent.id)
    at_risk = Outlet(name="Chorsu", outlet_type="grocery_store", stage="at_risk", assigned_agent_user_id=agent.id)
    written_off = Outlet(name="Eski", outlet_type="grocery_store", stage="lost", assigned_agent_user_id=agent.id)
    db.session.add_all([kept, at_risk, written_off])
    db.session.commit()
    moment = datetime.now(UTC)
    db.session.add(
        Visit(
            outlet_id=kept.id,
            agent_user_id=agent.id,
            status="completed",
            planned=True,
            current_step="close",
            started_at=moment,
            checkin_at=moment,
            ended_at=moment,
            outcome="no_order",
        )
    )
    db.session.commit()

    items, _, _ = SalesAgentAccountService.list_agents(page=1, per_page=20)

    assert items[0]["outlets_assigned"] == 2
    assert items[0]["outlets_active"] == 1
    assert items[0]["visits_today"] == 1
    assert items[0]["orders_today"] == 0
    assert SalesAgentAccountService.get_agent(agent.id)["visits_today"] == 1


def test_a_profile_without_the_role_is_not_an_active_agent(db):
    """B1: `active_agents()` counts the ROLE as well as the profile.

    Nothing deletes a `SalesAgentProfile` when somebody stops being an agent (the profile is
    what `update_staff_roles` reads to keep the role pinned), so without the role predicate
    the *Active agents* card would go on counting a person who is no longer a sales agent at
    all — a number beside a table that no longer lists them.
    """
    user = StaffService.create_sales_agent(dict(PAYLOAD))
    user.role = UserRole.CUSTOMER
    user.staff_roles = []
    db.session.commit()

    assert SalesAgentProfile.query.filter_by(user_id=user.id).one().is_active is True
    _, _, summary = SalesAgentAccountService.list_agents(page=1, per_page=20)
    assert summary["active_agents"] == 0


def test_a_suspended_agent_is_listed_under_inactive_never_under_active(db):
    """B2/R21 + R36: the two filters are exact COMPLEMENTS of one roster.

    The *Active agents* card reads `active_agents()` — role, active profile AND an ACTIVE
    user row — and the table's *Active* filter splats those same clauses. "Inactive" is then
    everything else that has a profile. Both halves matter: before B2 a suspended agent was
    listed under "Active" beside a card refusing to count them, and after B2 alone they fell
    through BOTH filters — a row the page could hide, findable only with the filter cleared.
    """
    user = StaffService.create_sales_agent(dict(PAYLOAD))
    user.status = UserStatus.INACTIVE
    db.session.commit()

    active_items, active_total, summary = SalesAgentAccountService.list_agents(
        page=1, per_page=20, status="active"
    )
    inactive_items, inactive_total, _ = SalesAgentAccountService.list_agents(
        page=1, per_page=20, status="inactive"
    )

    assert (active_items, active_total) == ([], 0)
    assert summary["active_agents"] == 0
    assert [row["user_id"] for row in inactive_items] == [user.id]
    assert inactive_total == 1
