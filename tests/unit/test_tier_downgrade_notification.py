"""Badge maintenance: what _check_tier_upgrade does when a downgrade is blocked."""

from datetime import datetime, timedelta, timezone

import pytest

from business_app.models.loyalty import LoyaltyPoints, LoyaltyProgram, LoyaltyTierConfig, LoyaltyTransaction
from business_app.services.loyalty_service import LoyaltyService
from business_app.utils.constants import LoyaltyTransactionType


@pytest.fixture
def program(db):
    program = LoyaltyProgram(name="Test program", is_active=True, is_default=True, uzs_per_point=250)
    db.session.add(program)
    db.session.commit()
    return program


@pytest.fixture
def ladder(db, program):
    for name, order, floor in (("Bronze", 0, 0), ("Silver", 1, 4000), ("Gold", 2, 15000)):
        db.session.add(
            LoyaltyTierConfig(
                program_id=program.id,
                name=name,
                display_order=order,
                min_points=floor,
                discount_percentage=0,
                points_multiplier=1.0,
                is_active=True,
            )
        )
    db.session.commit()


def _account(db, user, program, *, badge, qualifying, guarantee_days, stale_next=99999):
    account = LoyaltyPoints(
        user_id=user.id,
        program_id=program.id,
        current_tier=badge,
        total_earned=qualifying,
        current_balance=qualifying,
        points_to_next_tier=stale_next,
        tier_valid_until=datetime.now(timezone.utc) + timedelta(days=guarantee_days),
    )
    db.session.add(account)
    db.session.flush()
    db.session.add(
        LoyaltyTransaction(
            user_id=user.id,
            transaction_type=LoyaltyTransactionType.EARNED,
            points=qualifying,
            remaining_points=qualifying,
            description="seed",
        )
    )
    db.session.commit()
    return account


def test_blocked_downgrade_refreshes_points_to_next_tier(db, sample_user, program, ladder):
    """The guarantee holds the badge, but the next-tier target must stay true."""
    account = _account(db, sample_user, program, badge="Silver", qualifying=3488, guarantee_days=365)

    LoyaltyService()._check_tier_upgrade(account)
    db.session.commit()

    assert account.current_tier == "Silver"
    assert account.points_to_next_tier == 15000 - 3488


def test_dashboard_progress_never_goes_negative(db, sample_user, program, ladder):
    """A member below their own badge's floor must not render a negative bar."""
    _account(db, sample_user, program, badge="Silver", qualifying=3488, guarantee_days=365)

    dashboard = LoyaltyService().get_account_dashboard_for_user(sample_user.id)

    assert dashboard["tier_progress"]["current"] >= 0
    assert dashboard["tier_progress"]["points_needed"] >= 0


def test_real_downgrade_parks_exactly_one_downgrade_event(db, sample_user, program, ladder):
    from business_app import db as _db
    from business_app.utils.loyalty_award_dispatch import KIND_TIER_DOWNGRADE, PENDING_KEY

    account = _account(db, sample_user, program, badge="Silver", qualifying=3488, guarantee_days=-1)

    LoyaltyService()._check_tier_upgrade(account)

    parked = [e for e in _db.session.info.get(PENDING_KEY, []) if e["kind"] == KIND_TIER_DOWNGRADE]
    assert len(parked) == 1
    assert parked[0]["user_id"] == sample_user.id
    assert parked[0]["tier"] == "Bronze"
    assert parked[0]["qualifying_points"] == 3488
    assert parked[0]["required_points"] == 4000
    assert account.current_tier == "Bronze"


def test_blocked_downgrade_parks_nothing(db, sample_user, program, ladder):
    from business_app import db as _db
    from business_app.utils.loyalty_award_dispatch import PENDING_KEY

    account = _account(db, sample_user, program, badge="Silver", qualifying=3488, guarantee_days=365)

    LoyaltyService()._check_tier_upgrade(account)

    assert _db.session.info.get(PENDING_KEY, []) == []


def test_requalifying_at_the_same_tier_parks_nothing(db, sample_user, program, ladder):
    from business_app import db as _db
    from business_app.utils.loyalty_award_dispatch import PENDING_KEY

    account = _account(db, sample_user, program, badge="Silver", qualifying=5000, guarantee_days=10)

    LoyaltyService()._check_tier_upgrade(account)

    assert _db.session.info.get(PENDING_KEY, []) == []


def test_downgrade_event_is_mapped_to_its_own_notification_type(db):
    from business_app.services.notification_service import LOYALTY_EVENT_NOTIFICATION_TYPES
    from business_app.utils.constants import NotificationType

    assert LOYALTY_EVENT_NOTIFICATION_TYPES["tier_downgrade"] is NotificationType.LOYALTY_TIER_DOWNGRADE
