"""Regression tests for loyalty merge during cross-platform account linking.

Prod incident 2026-06-23: a customer linking their Telegram account to an
existing web account entered the CORRECT OTP, but the link failed with
``'LoyaltyPoints' object has no attribute 'current_points'``. The OTP was fine;
``CrossPlatformSyncService._merge_loyalty_membership`` still referenced loyalty
fields (``current_points``/``lifetime_points``/``total_orders``/``total_spent``)
that the loyalty SSOT refactor removed from the ``LoyaltyPoints`` model. The
crash only fires when BOTH accounts already own a ``LoyaltyPoints`` row — a path
no existing test exercised (the auth-link tests mock ``auto_link_accounts``).

Balance is now derived from the FIFO ``LoyaltyTransaction`` ledger, so the merge
must move the ledger lots and RECOMPUTE the cached balance (never manually sum a
derived balance, which would double-count once the ledger is also moved).
"""

from datetime import datetime, timedelta, timezone

import pytest

from business_app.models.loyalty import LoyaltyPoints, LoyaltyProgram, LoyaltyTransaction
from business_app.models.user import User
from business_app.services.cross_platform_sync_service import cross_platform_sync_service
from business_app.utils.constants import LoyaltyTransactionType
from business_app.utils.password_security import hash_password
from shared.enums import UserRole, UserStatus, UserType


@pytest.fixture
def program(db):
    prog = LoyaltyProgram(
        name="Default Program",
        description="Default loyalty program for tests",
        is_active=True,
        is_default=True,
    )
    db.session.add(prog)
    db.session.commit()
    return prog


@pytest.fixture
def web_user(db):
    user = User(
        email="web@example.com",
        phone="+998901110001",
        password_hash=hash_password("TestPassword123!"),
        first_name="Web",
        last_name="User",
        user_type=UserType.INDIVIDUAL,
        role=UserRole.CUSTOMER,
        status=UserStatus.ACTIVE,
        registration_source="web",
        is_verified=True,
        created_at=datetime.now(timezone.utc),
    )
    db.session.add(user)
    db.session.commit()
    return user


@pytest.fixture
def telegram_user(db):
    user = User(
        email="tg-placeholder@bluestream.local",
        phone="+998901110002",
        password_hash=hash_password("TestPassword123!"),
        first_name="Telegram",
        last_name="User",
        user_type=UserType.INDIVIDUAL,
        role=UserRole.CUSTOMER,
        status=UserStatus.ACTIVE,
        registration_source="telegram",
        telegram_id="8134062686",
        is_verified=True,
        created_at=datetime.now(timezone.utc) + timedelta(minutes=1),
    )
    db.session.add(user)
    db.session.commit()
    return user


def _account(db, user, program, *, total_earned, cached_balance):
    acc = LoyaltyPoints(
        user_id=user.id,
        program_id=program.id,
        total_earned=total_earned,
        total_redeemed=0,
        total_expired=0,
        # Deliberately STALE cached balance: the merge must recompute from the
        # ledger, not trust (or manually sum) this value.
        current_balance=cached_balance,
        current_tier="Bronze",
        points_to_next_tier=0,
    )
    db.session.add(acc)
    db.session.commit()
    return acc


def _earn_lot(db, user_id, points):
    now = datetime.now(timezone.utc)
    txn = LoyaltyTransaction(
        user_id=user_id,
        transaction_type=LoyaltyTransactionType.EARNED,
        points=points,
        remaining_points=points,
        description="earn",
        expires_at=now + timedelta(days=365),
        is_expired=False,
    )
    db.session.add(txn)
    db.session.commit()
    return txn


def test_merge_loyalty_membership_recomputes_balance_from_merged_ledger(db, program, web_user, telegram_user):
    """Merging two accounts that BOTH hold loyalty points must not crash and must
    derive the primary balance from the combined FIFO ledger."""
    # Primary (web): 300 + 200 = 500 live remaining; cached balance is stale.
    _account(db, web_user, program, total_earned=500, cached_balance=9999)
    _earn_lot(db, web_user.id, 300)
    _earn_lot(db, web_user.id, 200)

    # Secondary (telegram): 150 live remaining; cached balance is stale.
    _account(db, telegram_user, program, total_earned=150, cached_balance=8888)
    _earn_lot(db, telegram_user.id, 150)

    primary_id, secondary_id = web_user.id, telegram_user.id

    cross_platform_sync_service._merge_loyalty_membership(primary_id, secondary_id)
    db.session.commit()

    primary_points = LoyaltyPoints.query.filter_by(user_id=primary_id).first()
    secondary_points = LoyaltyPoints.query.filter_by(user_id=secondary_id).first()

    # Secondary record gone; its ledger reassigned to primary.
    assert secondary_points is None
    assert LoyaltyTransaction.query.filter_by(user_id=secondary_id).count() == 0
    assert LoyaltyTransaction.query.filter_by(user_id=primary_id).count() == 3

    # Balance recomputed from the merged ledger (300+200+150), NOT 9999+8888.
    assert primary_points.current_balance == 650
    # Lifetime aggregate is additive.
    assert primary_points.total_earned == 650


def test_merge_loyalty_membership_reassigns_when_primary_has_no_account(db, program, web_user, telegram_user):
    """When only the secondary account has a loyalty membership, the merge must
    move BOTH the membership record and its ledger to the primary (no orphaned
    ledger rows pointing at the about-to-be-deleted secondary user)."""
    _account(db, telegram_user, program, total_earned=150, cached_balance=7777)
    _earn_lot(db, telegram_user.id, 150)

    primary_id, secondary_id = web_user.id, telegram_user.id
    assert LoyaltyPoints.query.filter_by(user_id=primary_id).first() is None

    cross_platform_sync_service._merge_loyalty_membership(primary_id, secondary_id)
    db.session.commit()

    assert LoyaltyPoints.query.filter_by(user_id=secondary_id).first() is None
    assert LoyaltyTransaction.query.filter_by(user_id=secondary_id).count() == 0
    assert LoyaltyTransaction.query.filter_by(user_id=primary_id).count() == 1

    primary_points = LoyaltyPoints.query.filter_by(user_id=primary_id).first()
    assert primary_points is not None
    # Balance recomputed from the moved ledger, not the stale cached 7777.
    assert primary_points.current_balance == 150


def test_auto_link_accounts_succeeds_when_both_accounts_have_loyalty_points(db, program, web_user, telegram_user):
    """End-to-end reproduction of the prod incident: the merge step must succeed
    (return success=True) when both linked accounts already have loyalty points."""
    _account(db, web_user, program, total_earned=500, cached_balance=500)
    _earn_lot(db, web_user.id, 500)

    _account(db, telegram_user, program, total_earned=150, cached_balance=150)
    _earn_lot(db, telegram_user.id, 150)

    primary_id, secondary_id = web_user.id, telegram_user.id

    result = cross_platform_sync_service.auto_link_accounts(
        primary_user=web_user,
        secondary_user=telegram_user,
        link_type="merge",
    )

    assert result["success"] is True, result.get("error")
    # Secondary user fully merged away.
    assert User.query.get(secondary_id) is None
    primary_points = LoyaltyPoints.query.filter_by(user_id=primary_id).first()
    assert primary_points.current_balance == 650
