"""The cash discount follows the badge, not a live recomputation.

Reproduces the production shape that broke: a member promoted to Silver, then
an admin raising Silver's floor above their trailing-365-day points. The badge
still reads Silver, so the discount must still apply.
"""

from decimal import Decimal

from business_app.models.loyalty import LoyaltyPoints
from business_app.services.loyalty_service import LoyaltyService
from shared.enums import PaymentMethod
from tests.integration.tier_discount_factory import seed_account, seed_program, seed_tier

TIER_RATE = Decimal("1.5")
BASIS = Decimal("54000")


def _silver_badge_below_floor(db, user):
    program = seed_program(db)
    seed_tier(db, program, name="Bronze", rate=Decimal("0"), min_points=0, display_order=0)
    seed_tier(db, program, name="Silver", rate=TIER_RATE, min_points=4000, display_order=1)
    seed_account(db, user, program, qualifying_points=3488)

    account = LoyaltyPoints.query.filter_by(user_id=user.id).first()
    account.current_tier = "Silver"
    db.session.commit()
    return account


def test_cash_quote_uses_the_badge(db, sample_user):
    _silver_badge_below_floor(db, sample_user)

    quote = LoyaltyService().quote_tier_discount(sample_user, BASIS, PaymentMethod.CASH)

    assert quote.tier_name == "Silver"
    assert quote.percentage == TIER_RATE
    assert quote.amount == (BASIS * TIER_RATE / Decimal("100")).quantize(Decimal("0.01"))


def test_click_quote_stays_zero(db, sample_user):
    _silver_badge_below_floor(db, sample_user)

    quote = LoyaltyService().quote_tier_discount(sample_user, BASIS, PaymentMethod.CLICK)

    assert quote.amount == Decimal("0.00")
    assert quote.tier_name is None
