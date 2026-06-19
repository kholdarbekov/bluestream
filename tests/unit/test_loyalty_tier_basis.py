"""Unit D — tier qualification basis (owner decisions, 2026-06-14).

Tier is determined by a 1-YEAR (365-day) ROLLING window of qualifying points,
and bonus credits DO count. Every tier-display method must read this single
basis (not lifetime total_earned, not current_balance), and the tier lock
(tier_valid_until) runs 365 days and refreshes whenever the user requalifies.
"""

from datetime import datetime, timedelta, timezone

import pytest

from business_app.models.loyalty import (
    LoyaltyPoints,
    LoyaltyProgram,
    LoyaltyTierConfig,
    LoyaltyTransaction,
)
from business_app.services.loyalty_service import LoyaltyService
from business_app.utils.constants import LoyaltyTransactionType


@pytest.fixture
def service(app):
    with app.app_context():
        return LoyaltyService()


@pytest.fixture
def program(db):
    prog = LoyaltyProgram(name="Default", description="d", is_active=True, is_default=True, uzs_per_point=250)
    db.session.add(prog)
    db.session.commit()
    return prog


@pytest.fixture
def tiers(db, program):
    bronze = LoyaltyTierConfig(program_id=program.id, name="Bronze", display_order=0, min_points=0, is_active=True)
    silver = LoyaltyTierConfig(program_id=program.id, name="Silver", display_order=1, min_points=500, is_active=True)
    db.session.add_all([bronze, silver])
    db.session.commit()
    return {"bronze": bronze, "silver": silver}


@pytest.fixture
def account(db, sample_user, program):
    acc = LoyaltyPoints(
        user_id=sample_user.id,
        program_id=program.id,
        total_earned=0,
        current_balance=0,
        current_tier="Bronze",
        points_to_next_tier=0,
    )
    db.session.add(acc)
    db.session.commit()
    return acc


def _txn(db, user_id, type_, points, *, age_days, ttl_days=365):
    now = datetime.now(timezone.utc)
    t = LoyaltyTransaction(
        user_id=user_id,
        transaction_type=type_,
        points=points,
        description="t",
        remaining_points=points if points > 0 else None,
        expires_at=now + timedelta(days=ttl_days),
        is_expired=False,
    )
    db.session.add(t)
    db.session.flush()
    t.created_at = now - timedelta(days=age_days)
    db.session.commit()
    return t


def _days_from_now(dt):
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return (dt - datetime.now(timezone.utc).replace(tzinfo=None)).days


@pytest.mark.unit
class TestTierBasis:
    def test_qualifying_points_365_day_window_includes_bonus(self, service, account, db):
        _txn(db, account.user_id, LoyaltyTransactionType.EARNED, 200, age_days=300)  # within 365
        _txn(db, account.user_id, LoyaltyTransactionType.BONUS, 100, age_days=10)  # BONUS counts now
        _txn(db, account.user_id, LoyaltyTransactionType.EARNED, 500, age_days=400)  # outside 365 -> excluded
        _txn(db, account.user_id, LoyaltyTransactionType.ADJUSTMENT, 50, age_days=5)  # excluded

        assert service.calculate_qualifying_points(account.user_id) == 300

    def test_tier_progress_uses_qualifying_points_not_lifetime(self, service, account, tiers, db):
        account.total_earned = 100000  # large lifetime must NOT drive tier
        db.session.commit()
        _txn(db, account.user_id, LoyaltyTransactionType.EARNED, 300, age_days=10)

        progress = service.calculate_tier_progress(account.user_id)
        assert progress["current_points"] == 300
        assert progress["points_to_next_tier"] == 200  # Silver(500) - 300

        req = service.get_tier_upgrade_requirements(account.user_id)
        assert req["current_points"] == 300
        assert req["points_needed"] == 200

    def test_dashboard_tier_progress_uses_qualifying_points(self, service, account, tiers, db):
        account.total_earned = 100000
        account.current_balance = 9999
        db.session.commit()
        _txn(db, account.user_id, LoyaltyTransactionType.EARNED, 300, age_days=10)

        dash = service.get_account_dashboard_for_user(account.user_id)
        assert dash["tier_progress"]["points_needed"] == 200  # Silver(500) - qualifying(300)

    def test_tier_upgrade_locks_for_365_days(self, service, account, tiers, db):
        _txn(db, account.user_id, LoyaltyTransactionType.EARNED, 600, age_days=10)  # qualifies for Silver
        account.current_tier = "Bronze"
        db.session.commit()

        service._check_tier_upgrade(account)
        db.session.commit()
        db.session.refresh(account)

        assert account.current_tier == "Silver"
        assert 360 <= _days_from_now(account.tier_valid_until) <= 365

    def test_tier_valid_until_refreshed_on_same_tier_requalification(self, service, account, tiers, db):
        account.current_tier = "Bronze"
        account.tier_valid_until = None
        db.session.commit()
        _txn(db, account.user_id, LoyaltyTransactionType.EARNED, 100, age_days=5)  # still Bronze

        service._check_tier_upgrade(account)
        db.session.commit()
        db.session.refresh(account)

        assert account.tier_valid_until is not None
        assert 360 <= _days_from_now(account.tier_valid_until) <= 365

    def test_model_calculate_tier_orphan_removed(self):
        assert not hasattr(LoyaltyPoints, "calculate_tier")
