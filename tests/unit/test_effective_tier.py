"""effective_tier resolves the tier a member's benefits follow.

Price follows the badge. The guarantee date is never consulted here — it is a
downgrade floor owned by _check_tier_upgrade, not an input to pricing.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from business_app.models.loyalty import LoyaltyPoints, LoyaltyProgram, LoyaltyTierConfig, LoyaltyTransaction
from business_app.services.loyalty_service import LoyaltyService, effective_tier
from business_app.utils.constants import LoyaltyTransactionType
from shared.enums import PaymentMethod


@pytest.fixture
def program(db):
    program = LoyaltyProgram(name="Test program", is_active=True, is_default=True, uzs_per_point=250)
    db.session.add(program)
    db.session.commit()
    return program


@pytest.fixture
def ladder(db, program):
    """Bronze 0 / Silver 4000 / Gold 15000, at test-owned rates."""
    tiers = {}
    for name, order, floor, rate in (
        ("Bronze", 0, 0, 0),
        ("Silver", 1, 4000, 1.5),
        ("Gold", 2, 15000, 2.0),
    ):
        tier = LoyaltyTierConfig(
            program_id=program.id,
            name=name,
            display_order=order,
            min_points=floor,
            discount_percentage=rate,
            points_multiplier=1.0,
            is_active=True,
        )
        db.session.add(tier)
        tiers[name] = tier
    db.session.commit()
    return tiers


def _account(db, user, program, *, badge, qualifying, guarantee_days=365):
    account = LoyaltyPoints(
        user_id=user.id,
        program_id=program.id,
        current_tier=badge,
        total_earned=qualifying,
        current_balance=qualifying,
    )
    if guarantee_days is not None:
        account.tier_valid_until = datetime.now(timezone.utc) + timedelta(days=guarantee_days)
    db.session.add(account)
    db.session.flush()
    if qualifying:
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


def test_badge_wins_when_live_points_fall_short(db, sample_user, program, ladder):
    """The production defect: badge Silver, points below the raised floor."""
    account = _account(db, sample_user, program, badge="Silver", qualifying=3488)

    assert effective_tier(account).name == "Silver"


def test_live_wins_when_badge_has_not_caught_up(db, sample_user, program, ladder):
    """An admin cut the floor; the benefit must not wait for the monthly job."""
    account = _account(db, sample_user, program, badge="Bronze", qualifying=20000)

    assert effective_tier(account).name == "Gold"


def test_guarantee_date_is_not_consulted(db, sample_user, program, ladder):
    """A lapsed guarantee must not reprice a member whose badge still says Silver."""
    account = _account(db, sample_user, program, badge="Silver", qualifying=3488, guarantee_days=-30)

    assert effective_tier(account).name == "Silver"


def test_badge_naming_a_missing_config_falls_back_to_live(db, sample_user, program, ladder):
    account = _account(db, sample_user, program, badge="Diamond", qualifying=3488)

    assert effective_tier(account).name == "Bronze"


def test_inactive_badge_tier_is_ignored(db, sample_user, program, ladder):
    ladder["Silver"].is_active = False
    db.session.commit()
    account = _account(db, sample_user, program, badge="Silver", qualifying=3488)

    assert effective_tier(account).name == "Bronze"


def test_returns_none_when_no_tiers_configured(db, sample_user, program):
    account = _account(db, sample_user, program, badge="Silver", qualifying=3488)

    assert effective_tier(account) is None


def test_effective_tier_of_none_is_none(db):
    assert effective_tier(None) is None


def test_quote_falls_back_to_default_program_when_no_account_exists(db, sample_user, program):
    """No ``LoyaltyPoints`` row: ``quote_tier_discount`` must still resolve
    through the default program's lowest tier, exactly as before
    ``effective_tier`` existed. This is the case FIX 1 restores — it must
    fail if the ``account is None`` early return is reintroduced.
    """
    tier = LoyaltyTierConfig(
        program_id=program.id,
        name="Bronze",
        display_order=0,
        min_points=0,
        discount_percentage=3.0,
        points_multiplier=1.0,
        is_active=True,
    )
    db.session.add(tier)
    db.session.commit()

    assert LoyaltyPoints.query.filter_by(user_id=sample_user.id).first() is None

    quote = LoyaltyService().quote_tier_discount(sample_user, Decimal("10000"), PaymentMethod.CASH)

    assert quote.tier_name == "Bronze"
    assert quote.percentage == Decimal("3.0")
    assert quote.amount == (Decimal("10000") * Decimal("3.0") / Decimal("100")).quantize(Decimal("0.01"))


def test_earning_multiplier_follows_the_same_tier_as_the_discount(db, sample_user, program, ladder):
    """Earning and the discount must never resolve different tiers."""
    from business_app.services.loyalty_service import LoyaltyService

    ladder["Silver"].points_multiplier = 1.05
    db.session.commit()
    _account(db, sample_user, program, badge="Silver", qualifying=3488)

    # 250 UZS per point, so 250000 UZS is 1000 base points before the multiplier.
    points = LoyaltyService().calculate_points_for_purchase(sample_user.id, 250000)

    assert points == 1050


def test_earning_multiplier_uses_live_tier_when_badge_lags(db, sample_user, program, ladder):
    """An admin cut Gold's floor; earning must not wait for the monthly job."""
    from business_app.services.loyalty_service import LoyaltyService

    ladder["Gold"].points_multiplier = 1.10
    db.session.commit()
    _account(db, sample_user, program, badge="Bronze", qualifying=20000)

    points = LoyaltyService().calculate_points_for_purchase(sample_user.id, 250000)

    assert points == 1100
