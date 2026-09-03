"""LoyaltyService.quote_tier_discount — the ONE place that decides whether a
loyalty tier discount applies to an order, and how much it is worth.

FIXTURE DISCIPLINE. Production's tier thresholds and percentages differ from
dev's, and an older migration seeded a third set. Every rate below is seeded
by the test itself and asserted against the seeded value; nothing here may
reference the dev DB's numbers.
"""

from decimal import Decimal

import pytest

from business_app.models.corporate import (
    CorporateContract,
    CorporateContractStatus,
)
from business_app.models.loyalty import LoyaltyProgram, LoyaltyTierConfig
from business_app.services.loyalty_service import (
    LoyaltyService,
    TierDiscountQuote,
    clamp_tier_discount,
)
from shared.enums import EntitySubtype, PaymentMethod, UserType

from datetime import UTC, datetime, timedelta

BASIS = Decimal("30000.00")
LOW_RATE = Decimal("7")
HIGH_RATE = Decimal("13")


@pytest.fixture
def program(db):
    prog = LoyaltyProgram(name="Quote program", is_active=True, is_default=True)
    db.session.add(prog)
    db.session.commit()
    return prog


def _tier(db, program, *, name, rate, min_points=0, display_order=0):
    tier = LoyaltyTierConfig(
        program_id=program.id,
        name=name,
        display_order=display_order,
        min_points=min_points,
        discount_percentage=float(rate),
        is_active=True,
    )
    db.session.add(tier)
    db.session.commit()
    return tier


def test_cash_rail_quotes_the_resolved_tier_s_live_rate(db, sample_user, program):
    _tier(db, program, name="Base", rate=LOW_RATE)

    quote = LoyaltyService().quote_tier_discount(sample_user, BASIS, PaymentMethod.CASH)

    assert isinstance(quote, TierDiscountQuote)
    assert quote.percentage == LOW_RATE
    assert quote.tier_name == "Base"
    assert quote.amount == (BASIS * LOW_RATE / Decimal("100")).quantize(Decimal("0.01"))


def test_rate_comes_from_the_tier_the_member_actually_resolves_to(db, sample_user, program):
    """Two tiers, one member. The quote must read the RESOLVED tier's rate."""
    _tier(db, program, name="Base", rate=LOW_RATE, min_points=0, display_order=0)
    _tier(db, program, name="Upper", rate=HIGH_RATE, min_points=500, display_order=1)

    # 0 qualifying points -> Base, not Upper.
    assert LoyaltyService().quote_tier_discount(sample_user, BASIS, PaymentMethod.CASH).percentage == LOW_RATE


@pytest.mark.parametrize(
    "method",
    [
        PaymentMethod.CLICK,
        PaymentMethod.PAYME,
        PaymentMethod.CARD,
        PaymentMethod.BUSINESS_ACCOUNT,
        "click",
        None,
    ],
)
def test_non_cod_rails_quote_nothing(db, sample_user, program, method):
    _tier(db, program, name="Base", rate=LOW_RATE)

    quote = LoyaltyService().quote_tier_discount(sample_user, BASIS, method)

    assert quote.amount == Decimal("0.00")
    assert quote.percentage == Decimal("0")
    assert quote.tier_name is None


def test_zero_percent_tier_quotes_nothing(db, sample_user, program):
    _tier(db, program, name="Base", rate=Decimal("0"))

    assert LoyaltyService().quote_tier_discount(sample_user, BASIS, PaymentMethod.CASH).amount == Decimal("0.00")


def test_no_tier_configured_quotes_nothing(db, sample_user, program):
    assert LoyaltyService().quote_tier_discount(sample_user, BASIS, PaymentMethod.CASH).amount == Decimal("0.00")


def test_loyalty_ineligible_entity_quotes_nothing(db, sample_user, program):
    """An entity user with no loyalty-eligible active contract is excluded by
    the pre-existing SSOT gate, on every rail including CASH."""
    _tier(db, program, name="Base", rate=LOW_RATE)
    sample_user.user_type = UserType.ENTITY
    sample_user.entity_subtype = EntitySubtype.WORKPLACE
    db.session.add(
        CorporateContract(
            user_id=sample_user.id,
            contract_number="C-QUOTE-INELIGIBLE",
            name="No loyalty",
            status=CorporateContractStatus.ACTIVE,
            start_date=datetime.now(UTC) - timedelta(days=1),
            currency="UZS",
            is_active=True,
            is_loyalty_points_eligible=False,
        )
    )
    db.session.commit()

    assert LoyaltyService().is_user_loyalty_eligible(sample_user) is False
    assert LoyaltyService().quote_tier_discount(sample_user, BASIS, PaymentMethod.CASH).amount == Decimal("0.00")


def test_negative_basis_never_produces_a_negative_quote(db, sample_user, program):
    _tier(db, program, name="Base", rate=LOW_RATE)

    assert LoyaltyService().quote_tier_discount(sample_user, Decimal("-500.00"), PaymentMethod.CASH).amount == Decimal(
        "0.00"
    )


class TestClampTierDiscount:
    def test_untouched_when_there_is_headroom(self):
        assert clamp_tier_discount(
            Decimal("2100.00"),
            subtotal=Decimal("30000.00"),
            discount_amount=Decimal("0.00"),
            loyalty_discount=Decimal("0.00"),
        ) == Decimal("2100.00")

    def test_held_to_whatever_the_other_discounts_left(self):
        assert clamp_tier_discount(
            Decimal("27000.00"),
            subtotal=Decimal("30000.00"),
            discount_amount=Decimal("15000.00"),
            loyalty_discount=Decimal("0.00"),
        ) == Decimal("15000.00")

    def test_never_negative_when_the_others_already_exceed_the_subtotal(self):
        assert clamp_tier_discount(
            Decimal("2100.00"),
            subtotal=Decimal("30000.00"),
            discount_amount=Decimal("20000.00"),
            loyalty_discount=Decimal("20000.00"),
        ) == Decimal("0.00")
