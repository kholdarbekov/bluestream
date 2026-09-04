"""The tier editor cannot silently reprice members.

A threshold edit changes what every COD order costs. It must refuse a ladder
with a hole in it, and it must make stranding existing members deliberate.
"""

from decimal import Decimal

import pytest
from flask_jwt_extended import create_access_token

from business_app.models.audit import AuditLog
from business_app.models.loyalty import LoyaltyPoints, LoyaltyTierConfig
from business_app.models.user import User
from business_app.utils.password_security import hash_password
from shared.enums import UserRole, UserType
from tests.integration.tier_discount_factory import seed_account, seed_program, seed_tier


@pytest.fixture
def admin_headers(app, db):
    """validate_admin_action reads the DB row, so the user must be STAFF+ADMIN;
    a role claim on the JWT alone is not enough."""
    admin = User(
        email="tier-admin@example.com",
        password_hash=hash_password("TestPassword123!"),
        first_name="Tier",
        last_name="Admin",
        role=UserRole.ADMIN,
        user_type=UserType.STAFF,
        is_verified=True,
    )
    db.session.add(admin)
    db.session.commit()
    with app.app_context():
        token = create_access_token(
            identity=str(admin.id),
            additional_claims={"role": admin.role.value},
        )
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


@pytest.fixture
def ladder(db):
    program = seed_program(db)
    bronze = seed_tier(db, program, name="Bronze", rate=Decimal("0"), min_points=0, display_order=0)
    bronze.max_points = 3000
    silver = seed_tier(db, program, name="Silver", rate=Decimal("1.5"), min_points=3000, display_order=1)
    silver.max_points = None
    db.session.commit()
    return {"program": program, "Bronze": bronze, "Silver": silver}


@pytest.fixture
def three_tier_ladder(db):
    """Bronze[0,3000) / Silver[3000,6000) / Gold[6000,None) — a valid ladder
    with a real middle tier, so deactivating it can be tested for a gap."""
    program = seed_program(db)
    bronze = seed_tier(db, program, name="Bronze", rate=Decimal("0"), min_points=0, display_order=0)
    bronze.max_points = 3000
    silver = seed_tier(db, program, name="Silver", rate=Decimal("1.5"), min_points=3000, display_order=1)
    silver.max_points = 6000
    gold = seed_tier(db, program, name="Gold", rate=Decimal("3"), min_points=6000, display_order=2)
    gold.max_points = None
    db.session.commit()
    return {"program": program, "Bronze": bronze, "Silver": silver, "Gold": gold}


def test_gapped_ladder_is_rejected(app, db, admin_headers, ladder):
    """Bronze ends at 3000; Silver may not start at 4000."""
    response = app.test_client().put(
        f"/api/v1/admin/loyalty/tiers/{ladder['Silver'].id}",
        json={"min_points": 4000},
        headers=admin_headers,
    )

    assert response.status_code == 422
    assert response.get_json()["data"]["error_code"] == "threshold_gap"
    db.session.refresh(ladder["Silver"])
    assert ladder["Silver"].min_points == 3000


def test_stranding_edit_needs_confirmation(app, db, admin_headers, ladder):
    """Raising the floor above a member's points must be deliberate."""
    member = User(
        email="m@example.com", password_hash=hash_password("TestPassword123!"),
        first_name="M", last_name="M",
        role=UserRole.CUSTOMER, user_type=UserType.INDIVIDUAL,
    )
    db.session.add(member)
    db.session.commit()
    seed_account(db, member, ladder["program"], qualifying_points=3488)
    account = LoyaltyPoints.query.filter_by(user_id=member.id).first()
    account.current_tier = "Silver"
    ladder["Bronze"].max_points = 4000
    db.session.commit()

    first = app.test_client().put(
        f"/api/v1/admin/loyalty/tiers/{ladder['Silver'].id}",
        json={"min_points": 4000},
        headers=admin_headers,
    )

    assert first.status_code == 409
    assert first.get_json()["data"]["error_code"] == "impact_confirmation_required"
    assert first.get_json()["data"]["stranded_members"] == 1

    second = app.test_client().put(
        f"/api/v1/admin/loyalty/tiers/{ladder['Silver'].id}",
        json={"min_points": 4000, "confirm_impact": True},
        headers=admin_headers,
    )

    assert second.status_code == 200
    db.session.refresh(ladder["Silver"])
    assert ladder["Silver"].min_points == 4000


def test_accepted_edit_writes_an_audit_row(app, db, admin_headers, ladder):
    response = app.test_client().put(
        f"/api/v1/admin/loyalty/tiers/{ladder['Silver'].id}",
        json={"discount_percentage": 2.5},
        headers=admin_headers,
    )

    assert response.status_code == 200
    row = AuditLog.query.filter_by(resource_type="loyalty_tier_config").order_by(AuditLog.id.desc()).first()
    assert row is not None
    assert row.new_values["discount_percentage"] == 2.5
    assert row.old_values["discount_percentage"] == 1.5


def test_deactivating_a_held_tier_needs_confirmation(app, db, admin_headers, ladder):
    """Turning a tier off strands every member wearing its badge — that must
    be confirmed exactly like a min_points raise, not slip through free."""
    member = User(
        email="held@example.com", password_hash=hash_password("TestPassword123!"),
        first_name="Held", last_name="M",
        role=UserRole.CUSTOMER, user_type=UserType.INDIVIDUAL,
    )
    db.session.add(member)
    db.session.commit()
    seed_account(db, member, ladder["program"], qualifying_points=500)
    account = LoyaltyPoints.query.filter_by(user_id=member.id).first()
    account.current_tier = "Bronze"
    db.session.commit()

    first = app.test_client().put(
        f"/api/v1/admin/loyalty/tiers/{ladder['Bronze'].id}",
        json={"is_active": False},
        headers=admin_headers,
    )

    assert first.status_code == 409
    assert first.get_json()["data"]["error_code"] == "impact_confirmation_required"
    assert first.get_json()["data"]["stranded_members"] == 1
    db.session.refresh(ladder["Bronze"])
    assert ladder["Bronze"].is_active is True

    second = app.test_client().put(
        f"/api/v1/admin/loyalty/tiers/{ladder['Bronze'].id}",
        json={"is_active": False, "confirm_impact": True},
        headers=admin_headers,
    )

    assert second.status_code == 200
    db.session.refresh(ladder["Bronze"])
    assert ladder["Bronze"].is_active is False


def test_deactivating_middle_tier_leaves_gap(app, db, admin_headers, three_tier_ladder):
    """Bronze ends at 3000, Gold starts at 6000; deactivating Silver would
    leave those points mapping to no tier."""
    response = app.test_client().put(
        f"/api/v1/admin/loyalty/tiers/{three_tier_ladder['Silver'].id}",
        json={"is_active": False},
        headers=admin_headers,
    )

    assert response.status_code == 422
    assert response.get_json()["data"]["error_code"] == "threshold_gap"
    db.session.refresh(three_tier_ladder["Silver"])
    assert three_tier_ladder["Silver"].is_active is True


def test_null_min_points_is_rejected_not_500(app, db, admin_headers, ladder):
    """An explicit JSON null must 422, not crash the comparison it feeds."""
    response = app.test_client().put(
        f"/api/v1/admin/loyalty/tiers/{ladder['Silver'].id}",
        json={"min_points": None},
        headers=admin_headers,
    )

    assert response.status_code == 422
    assert response.get_json()["data"]["error_code"] == "threshold_invalid"
    db.session.refresh(ladder["Silver"])
    assert ladder["Silver"].min_points == 3000


def test_threshold_gap_on_put_is_overridden_by_confirm_impact(app, db, admin_headers, ladder):
    """threshold_gap does not change pricing (get_tier_for_points reads
    min_points alone) and is the guaranteed transient state of a legitimate
    multi-step ladder edit, so confirm_impact must be able to waive it."""
    response = app.test_client().put(
        f"/api/v1/admin/loyalty/tiers/{ladder['Silver'].id}",
        json={"min_points": 4000, "confirm_impact": True},
        headers=admin_headers,
    )

    assert response.status_code == 200
    db.session.refresh(ladder["Silver"])
    assert ladder["Silver"].min_points == 4000


def test_threshold_overlap_on_put_is_not_overridden_by_confirm_impact(app, db, admin_headers, ladder):
    """Unlike a gap, an overlap changes which tier a given points total
    resolves to — confirm_impact must not be able to waive it."""
    response = app.test_client().put(
        f"/api/v1/admin/loyalty/tiers/{ladder['Silver'].id}",
        json={"min_points": 0, "confirm_impact": True},
        headers=admin_headers,
    )

    assert response.status_code == 422
    assert response.get_json()["data"]["error_code"] == "threshold_overlap"
    db.session.refresh(ladder["Silver"])
    assert ladder["Silver"].min_points == 3000


def test_delete_of_held_tier_needs_confirmation_then_succeeds(app, db, admin_headers, ladder):
    """DELETE is the same operation as a deactivating PUT (both flip
    is_active=False and both strand every badge holder), so it must clear the
    same 409 impact gate before it can proceed."""
    member = User(
        email="held-delete@example.com", password_hash=hash_password("TestPassword123!"),
        first_name="Held", last_name="Delete",
        role=UserRole.CUSTOMER, user_type=UserType.INDIVIDUAL,
    )
    db.session.add(member)
    db.session.commit()
    seed_account(db, member, ladder["program"], qualifying_points=500)
    account = LoyaltyPoints.query.filter_by(user_id=member.id).first()
    account.current_tier = "Bronze"
    db.session.commit()

    first = app.test_client().delete(
        f"/api/v1/admin/loyalty/tiers/{ladder['Bronze'].id}",
        headers=admin_headers,
    )

    assert first.status_code == 409
    assert first.get_json()["data"]["error_code"] == "impact_confirmation_required"
    assert first.get_json()["data"]["stranded_members"] == 1
    db.session.refresh(ladder["Bronze"])
    assert ladder["Bronze"].is_active is True

    second = app.test_client().delete(
        f"/api/v1/admin/loyalty/tiers/{ladder['Bronze'].id}",
        json={"confirm_impact": True},
        headers=admin_headers,
    )

    assert second.status_code == 200
    db.session.refresh(ladder["Bronze"])
    assert ladder["Bronze"].is_active is False


def test_delete_of_middle_tier_leaves_gap(app, db, admin_headers, three_tier_ladder):
    """Deleting Silver would leave Bronze[0,3000) and Gold[6000,None) with no
    tier covering the points in between — same guard as the PUT path."""
    response = app.test_client().delete(
        f"/api/v1/admin/loyalty/tiers/{three_tier_ladder['Silver'].id}",
        headers=admin_headers,
    )

    assert response.status_code == 422
    assert response.get_json()["data"]["error_code"] == "threshold_gap"
    db.session.refresh(three_tier_ladder["Silver"])
    assert three_tier_ladder["Silver"].is_active is True


def test_delete_with_no_body_does_not_500(app, db, admin_headers):
    """A DELETE legitimately carries no JSON body; reading it must not raise."""
    program = seed_program(db)
    tier = seed_tier(db, program, name="Solo", rate=Decimal("0"), min_points=0, display_order=0)
    tier.max_points = None
    db.session.commit()
    tier_id = tier.id

    response = app.test_client().delete(
        f"/api/v1/admin/loyalty/tiers/{tier_id}",
        headers=admin_headers,
    )

    assert response.status_code == 200
    assert LoyaltyTierConfig.query.get(tier_id) is None


def test_delete_writes_an_audit_row(app, db, admin_headers, ladder):
    """Bronze has no badge holders here, so this is a HARD delete — the row
    no longer exists, so new_values must say so rather than claiming
    {"is_active": False}, which would read as a still-existing, deactivated
    row to an auditor."""
    response = app.test_client().delete(
        f"/api/v1/admin/loyalty/tiers/{ladder['Bronze'].id}",
        headers=admin_headers,
    )

    assert response.status_code == 200
    row = (
        AuditLog.query.filter_by(resource_type="loyalty_tier_config", action="loyalty_tier_deleted")
        .order_by(AuditLog.id.desc())
        .first()
    )
    assert row is not None
    assert row.old_values["is_active"] is True
    assert row.new_values == {"deleted": True}


def test_soft_delete_writes_a_deactivation_audit_row(app, db, admin_headers, ladder):
    """A held tier's DELETE actually deactivates (soft delete) — the audit
    row must say that, distinctly from a hard delete's {"deleted": True}."""
    member = User(
        email="held-audit@example.com", password_hash=hash_password("TestPassword123!"),
        first_name="Held", last_name="Audit",
        role=UserRole.CUSTOMER, user_type=UserType.INDIVIDUAL,
    )
    db.session.add(member)
    db.session.commit()
    seed_account(db, member, ladder["program"], qualifying_points=500)
    account = LoyaltyPoints.query.filter_by(user_id=member.id).first()
    account.current_tier = "Bronze"
    db.session.commit()

    response = app.test_client().delete(
        f"/api/v1/admin/loyalty/tiers/{ladder['Bronze'].id}",
        json={"confirm_impact": True},
        headers=admin_headers,
    )

    assert response.status_code == 200
    row = (
        AuditLog.query.filter_by(resource_type="loyalty_tier_config", action="loyalty_tier_deleted")
        .order_by(AuditLog.id.desc())
        .first()
    )
    assert row is not None
    assert row.old_values["is_active"] is True
    assert row.new_values == {"is_active": False}
    assert LoyaltyTierConfig.query.get(ladder["Bronze"].id) is not None


def test_delete_of_top_tier_is_rejected_as_gap(app, db, admin_headers, three_tier_ladder):
    """Deleting Gold (max=None) leaves Silver[3000,6000) as the new top tier
    with a non-NULL max — the ladder validator requires the top tier's max
    to be NULL, so this must 422 rather than silently delete."""
    response = app.test_client().delete(
        f"/api/v1/admin/loyalty/tiers/{three_tier_ladder['Gold'].id}",
        headers=admin_headers,
    )

    assert response.status_code == 422
    assert response.get_json()["data"]["error_code"] == "threshold_gap"
    assert LoyaltyTierConfig.query.get(three_tier_ladder["Gold"].id) is not None


def test_confirm_impact_waives_threshold_gap_on_delete(app, db, admin_headers, three_tier_ladder):
    """Deleting the middle tier (Silver) opens a gap between Bronze and Gold.
    That gap changes no pricing, so confirm_impact must waive it here exactly
    as it does on PUT, and the delete must actually proceed."""
    silver_id = three_tier_ladder["Silver"].id

    blocked = app.test_client().delete(
        f"/api/v1/admin/loyalty/tiers/{silver_id}",
        headers=admin_headers,
    )
    assert blocked.status_code == 422
    assert blocked.get_json()["data"]["error_code"] == "threshold_gap"

    response = app.test_client().delete(
        f"/api/v1/admin/loyalty/tiers/{silver_id}",
        json={"confirm_impact": True},
        headers=admin_headers,
    )

    assert response.status_code == 200
    assert LoyaltyTierConfig.query.get(silver_id) is None
