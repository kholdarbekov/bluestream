"""An entity user without a loyalty-eligible contract gets NO loyalty, in any form.

Owner rule: is_loyalty_points_eligible=false means no loyalty at all — no
discount, no earning, no advertised points, and no loyalty account row brought
into existence on their behalf.
"""

from decimal import Decimal

import pytest

from business_app.models.loyalty import LoyaltyPoints, LoyaltyProgram, LoyaltyTransaction
from business_app.models.user import User
from business_app.utils.password_security import hash_password
from business_app.services.loyalty_service import LoyaltyService
from tests.integration.tier_discount_factory import seed_account
from business_app.utils.constants import LoyaltyActionType
from shared.enums import PaymentMethod, UserRole, UserType


@pytest.fixture
def program(db):
    program = LoyaltyProgram(name="P", is_active=True, is_default=True, uzs_per_point=250)
    db.session.add(program)
    db.session.commit()
    return program


@pytest.fixture
def ineligible_entity(db, program):
    """Entity user with no loyalty-eligible corporate contract."""
    user = User(
        email="ineligible-entity@example.com",
        first_name="Ineligible",
        last_name="Entity",
        role=UserRole.CUSTOMER,
        user_type=UserType.ENTITY,
        password_hash=hash_password("TestPassword123!"),
    )
    db.session.add(user)
    db.session.commit()
    assert LoyaltyService.is_user_loyalty_eligible(user) is False
    return user


def test_no_tier_discount(db, ineligible_entity):
    quote = LoyaltyService().quote_tier_discount(ineligible_entity, Decimal("54000"), PaymentMethod.CASH)
    assert quote.amount == Decimal("0.00")


def test_advertises_no_points(db, ineligible_entity):
    """The cart estimate must not promise AquaCoins the award path will refuse."""
    assert LoyaltyService().calculate_points_for_purchase(ineligible_entity.id, 54000) == 0


def test_no_loyalty_account_is_created(db, ineligible_entity):
    """Guards calculate_points_for_purchase's own gate specifically: it must
    return before get_or_create_loyalty_account. The transient-account behaviour
    is covered separately by test_read_surfaces_create_no_account_row."""
    LoyaltyService().calculate_points_for_purchase(ineligible_entity.id, 54000)

    assert LoyaltyPoints.query.filter_by(user_id=ineligible_entity.id).count() == 0


def test_award_points_refuses(db, ineligible_entity):
    """The choke point: no positive-point write may land for an ineligible entity.

    Every bonus path (welcome, referral, birthday, gift, order-edit re-award)
    funnels through award_points, so gating here closes all of them at once and
    any path added later inherits the gate.
    """
    result = LoyaltyService().award_points(
        ineligible_entity.id, 500, "should not land", action_type=LoyaltyActionType.WELCOME_BONUS
    )

    assert result is None
    assert LoyaltyTransaction.query.filter_by(user_id=ineligible_entity.id).count() == 0
    assert LoyaltyPoints.query.filter_by(user_id=ineligible_entity.id).count() == 0


def test_welcome_bonus_grants_nothing(db, ineligible_entity, program):
    """Seeded with an EXISTING loyalty row, as 47 production entity users have —
    otherwise the transient account resolves signup_bonus to 0 and the test would
    pass even with grant_welcome_bonus's own gate removed."""
    program.signup_bonus = 300
    db.session.add(LoyaltyPoints(user_id=ineligible_entity.id, program_id=program.id, current_balance=0))
    db.session.commit()

    assert LoyaltyService().grant_welcome_bonus(ineligible_entity.id) == 0
    assert LoyaltyTransaction.query.filter_by(user_id=ineligible_entity.id).count() == 0


def test_birthday_bonus_skips_ineligible_entity(db, ineligible_entity, program):
    """The birthday sweep iterates every user with a date_of_birth."""
    from datetime import UTC, datetime

    program.birthday_bonus = 200
    today = datetime.now(UTC)
    ineligible_entity.date_of_birth = today.replace(year=1990)
    db.session.commit()

    LoyaltyService().grant_birthday_bonuses()

    assert LoyaltyTransaction.query.filter_by(user_id=ineligible_entity.id).count() == 0


def test_order_edit_reaward_grants_nothing_and_creates_no_row(db, ineligible_entity):
    """Order-edit re-award is the path that actually fired in production."""
    LoyaltyService().reverse_earnings(
        user_id=ineligible_entity.id, order_id=1, old_points_earned=0, new_points_earned=576
    )

    assert LoyaltyTransaction.query.filter_by(user_id=ineligible_entity.id).count() == 0
    assert LoyaltyPoints.query.filter_by(user_id=ineligible_entity.id).count() == 0


def test_read_surfaces_create_no_account_row(db, ineligible_entity, program):
    """Web/admin read paths must render a zero state, not materialise a row."""
    account = LoyaltyService().get_or_create_loyalty_account(ineligible_entity.id)

    assert account.current_balance == 0
    assert LoyaltyPoints.query.filter_by(user_id=ineligible_entity.id).count() == 0
    db.session.commit()
    assert LoyaltyPoints.query.filter_by(user_id=ineligible_entity.id).count() == 0


def test_gift_to_ineligible_recipient_does_not_destroy_sender_points(db, ineligible_entity, program, sample_user):
    """Refuse before debiting: award_points drops the credit for an ineligible
    recipient, so debiting first would burn the sender's points."""
    from business_app.utils.exceptions import ValidationError

    from tests.integration.tier_discount_factory import seed_account

    seed_account(db, sample_user, program, qualifying_points=1000, balance=1000)
    before = LoyaltyService().get_available_points(sample_user.id)

    with pytest.raises(ValidationError):
        LoyaltyService().gift_points(sample_user.id, ineligible_entity.id, 100, "gift")

    assert LoyaltyService().get_available_points(sample_user.id) == before
    assert LoyaltyTransaction.query.filter_by(user_id=ineligible_entity.id).count() == 0


def test_tier_sweep_skips_ineligible_accounts(db, ineligible_entity, program):
    """47 production entity users hold historical loyalty rows. Recomputing their
    tier would push them an upgrade/downgrade message for a programme they are
    not in."""
    from tests.integration.tier_discount_factory import seed_tier

    seed_tier(db, program, name="Bronze", rate=Decimal("0"), min_points=0, display_order=0)
    seed_tier(db, program, name="Silver", rate=Decimal("1.5"), min_points=4000, display_order=1)
    # Real EARNED lots, so qualifying points would genuinely promote them to
    # Silver if the sweep did not skip ineligible accounts.
    seed_account(db, ineligible_entity, program, qualifying_points=9999, balance=9999)
    row = LoyaltyPoints.query.filter_by(user_id=ineligible_entity.id).first()
    row.current_tier = "Bronze"
    db.session.commit()

    result = LoyaltyService().update_all_tiers()

    moved = [e for e in result["upgrades"] + result["downgrades"] if e["user_id"] == ineligible_entity.id]
    assert moved == []
    db.session.refresh(row)
    assert row.current_tier == "Bronze"


def test_campaign_audience_excludes_ineligible_entity(db, ineligible_entity, program, sample_user):
    """A loyalty_points row is not membership — the audience must be filtered by
    the eligibility SSOT, not by the presence of a row."""
    from types import SimpleNamespace

    from business_app.services.notification_service import NotificationService
    from tests.integration.tier_discount_factory import seed_account

    db.session.add(LoyaltyPoints(user_id=ineligible_entity.id, program_id=program.id, current_balance=0))
    seed_account(db, sample_user, program, qualifying_points=100, balance=100)
    db.session.commit()

    campaign = SimpleNamespace(target_audience="loyalty_members", specific_user_ids=None, target_segment_id=None)
    ids = NotificationService()._resolve_notification_campaign_recipient_ids(campaign)

    assert sample_user.id in ids
    assert ineligible_entity.id not in ids
