"""Parity test: LOYALTY_ELIGIBLE_SQL SQL predicate must agree with
LoyaltyService.is_user_loyalty_eligible across all eligibility scenarios.

Uses the project's standard SQLite in-memory test DB (via the `db` fixture).
CURRENT_TIMESTAMP in the SQL is portable across both SQLite and Postgres.
"""

from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import text

from business_app import db as _db
from business_app.models.corporate import CorporateContract, CorporateContractStatus
from business_app.models.user import User
from business_app.services.loyalty_service import LoyaltyService
from business_app.utils.password_security import hash_password
from shared.enums import UserRole, UserType
from shared.loyalty_eligibility import LOYALTY_ELIGIBLE_SQL


# ---------------------------------------------------------------------------
# Local helpers (match Task 1 pattern)
# ---------------------------------------------------------------------------

def _make_user(db, user_type: UserType) -> User:
    """Create a persisted user with a UUID-unique email/phone."""
    uid = uuid4().hex[:8]
    user = User(
        email=f"parity-{uid}@example.com",
        phone=f"+9981{uid[:8]}",
        password_hash=hash_password("TestPassword123!"),
        first_name="Parity",
        last_name="Test",
        user_type=user_type,
        role=UserRole.CUSTOMER,
        is_verified=True,
        created_at=datetime.utcnow(),
    )
    db.session.add(user)
    db.session.commit()
    return user


def _contract(user, **kw):
    defaults = dict(
        user_id=user.id,
        contract_number=f"PC-{user.id}-{kw.pop('tag', uuid4().hex[:6])}",
        name="c",
        status=CorporateContractStatus.ACTIVE,
        is_active=True,
        is_loyalty_points_eligible=True,
        start_date=datetime.utcnow() - timedelta(days=1),
        end_date=None,
    )
    defaults.update(kw)
    return CorporateContract(**defaults)


# ---------------------------------------------------------------------------
# Parity test
# ---------------------------------------------------------------------------

def test_sql_predicate_matches_python_rule(db):
    """SQL LOYALTY_ELIGIBLE_SQL must agree with Python is_user_loyalty_eligible
    across the full eligibility matrix."""

    # Case 1: individual — always eligible
    u_individual = _make_user(db, UserType.INDIVIDUAL)

    # Case 2: entity, no contract — not eligible
    u_no_contract = _make_user(db, UserType.ENTITY)

    # Case 3: entity, contract with is_loyalty_points_eligible=False — not eligible
    u_disabled_flag = _make_user(db, UserType.ENTITY)
    db.session.add(_contract(u_disabled_flag, is_loyalty_points_eligible=False))

    # Case 4: entity, suspended contract — not eligible
    u_suspended = _make_user(db, UserType.ENTITY)
    db.session.add(_contract(u_suspended, status=CorporateContractStatus.SUSPENDED))

    # Case 5: entity, expired contract (end_date in past) — not eligible
    u_expired = _make_user(db, UserType.ENTITY)
    db.session.add(_contract(u_expired, end_date=datetime.utcnow() - timedelta(days=1)))

    # Case 6: entity, one active+eligible contract — eligible
    u_active_ok = _make_user(db, UserType.ENTITY)
    db.session.add(_contract(u_active_ok, tag="good"))

    db.session.commit()

    cases = [u_individual, u_no_contract, u_disabled_flag, u_suspended, u_expired, u_active_ok]

    for user in cases:
        sql_val = _db.session.execute(
            text(f"SELECT {LOYALTY_ELIGIBLE_SQL} FROM users u WHERE u.id = :uid"),
            {"uid": user.id},
        ).scalar()
        python_val = LoyaltyService.is_user_loyalty_eligible(user)
        assert bool(sql_val) == python_val, (
            f"SQL/Python disagree for user {user.id} "
            f"(user_type={user.user_type}): SQL={bool(sql_val)}, Python={python_val}"
        )
