"""Entity-eligibility gating of the loyalty EARNING path (TDD).

Decision (product owner, 2026-06-24): an ineligible ENTITY user — an entity with
NO active ``is_loyalty_points_eligible`` corporate contract — must earn NOTHING
on a delivered+paid order: no purchase AquaCoins, no streak bonus, and no
consecutive-strike bonus. Individual customers and eligible entities are
unaffected, and delivery is never blocked.

The SSOT guard is ``LoyaltyService.is_user_loyalty_eligible(user)`` (takes a USER
OBJECT). These tests drive the REAL award trigger path via
``deliver_paid_order(product=None)`` →
``OrderService._handle_status_change_actions(DELIVERED)`` →
``maybe_award_purchase_points`` + ``update_streak`` → ``update_consecutive_strikes``.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from business_app.models.loyalty import LoyaltyTransaction
from business_app.models.order import Order
from business_app.services.loyalty_service import LoyaltyService
from business_app.services.order_service import OrderService
from business_app.utils.constants import LoyaltyActionType, LoyaltyTransactionType
from shared.enums import OrderStatus

from tests.e2e._consecutive_strike_helpers import (
    build_entity_user,
    consecutive_award_total,
    deliver_paid_order,
    get_or_create_default_program,
    make_consecutive_rule,
    make_strike_rule,
    seed_consecutive_run,
    seed_delivered_orders,
    silence_loyalty_notifications,
    strike_achievement_count,
)

pytestmark = pytest.mark.e2e


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _silence(monkeypatch):
    silence_loyalty_notifications(monkeypatch)


def _purchase_earned_total(user_id: int) -> int:
    """Sum of points from PURCHASE EARNED ledger rows for this user."""
    total = 0
    for t in LoyaltyTransaction.query.filter_by(user_id=user_id).all():
        ed = t.extra_data or {}
        if (
            t.transaction_type == LoyaltyTransactionType.EARNED
            and ed.get("action_type") == LoyaltyActionType.PURCHASE.value
        ):
            total += t.points
    return total


def _streak_bonus_count(user_id: int) -> int:
    """Count of STREAK_BONUS ledger rows for this user (any rule)."""
    count = 0
    for t in LoyaltyTransaction.query.filter_by(user_id=user_id).all():
        ed = t.extra_data or {}
        if ed.get("action_type") == LoyaltyActionType.STREAK_BONUS.value:
            count += 1
    return count


def _consecutive_bonus_count(user_id: int) -> int:
    """Count of CONSECUTIVE_STREAK_BONUS ledger rows for this user (any rule)."""
    count = 0
    for t in LoyaltyTransaction.query.filter_by(user_id=user_id).all():
        ed = t.extra_data or {}
        if ed.get("action_type") == LoyaltyActionType.CONSECUTIVE_STREAK_BONUS.value:
            count += 1
    return count


def _build_program_with_rules():
    """Default program + a 3-in-30 strike rule + a 3-consecutive 'all' rule.

    required_orders=3, window_days=30 so a real delivery plus 2 seeded delivered
    orders crosses the threshold and earns one strike. required_consecutive=3 so
    seeding a 2-run plus the freshly earned strike triggers the consecutive bonus.
    """
    program = get_or_create_default_program()
    strike = make_strike_rule(
        program,
        name="3 in 30",
        required_orders=3,
        window_days=30,
        bonus_points=100,
    )
    consec = make_consecutive_rule(
        program,
        [strike],
        name="Champion",
        required_consecutive=3,
        combine_mode="all",
        bonus_points=1000,
    )
    return program, strike, consec


# ===========================================================================
# Sanity: the SSOT eligibility predicate behaves as expected on user objects
# ===========================================================================


def test_eligibility_predicate_sanity(app, db, sample_user):
    """is_user_loyalty_eligible: individual True, ineligible entity False."""
    with app.app_context():
        ineligible = build_entity_user(loyalty_eligible=False)
        assert LoyaltyService.is_user_loyalty_eligible(sample_user) is True
        assert LoyaltyService.is_user_loyalty_eligible(ineligible) is False


# ===========================================================================
# INDIVIDUAL: still earns streak + consecutive + purchase (no over-blocking)
# ===========================================================================


def test_individual_still_earns_all_bonuses(app, db, sample_user):
    """An individual customer with seeds + a real delivery earns the exact
    streak, consecutive, and purchase awards (guard must not over-block)."""
    with app.app_context():
        _program, strike, consec = _build_program_with_rules()
        uid = sample_user.id

        # 2 prior earned strikes → one more (from the real delivery) completes the
        # 3-consecutive run.
        seed_consecutive_run(uid, strike, count=2)
        # 2 qualifying delivered orders → the real delivery is the 3rd, earning the strike.
        seed_delivered_orders(uid, count=2, total=Decimal("50000"))

        order_service = OrderService()
        order = deliver_paid_order(order_service, uid, total=Decimal("50000"))

        # Exactly one new strike awarded by the real delivery.
        assert strike_achievement_count(uid, strike.id) == 3
        # The 3rd strike completes the consecutive run → exactly one bonus.
        assert consecutive_award_total(uid, consec.id) == consec.bonus_points
        assert _consecutive_bonus_count(uid) == 1
        # Purchase AquaCoins earned for the delivered+paid order.
        assert _purchase_earned_total(uid) > 0
        assert order.status == OrderStatus.DELIVERED


# ===========================================================================
# ELIGIBLE ENTITY: still earns (awards > 0)
# ===========================================================================


def test_eligible_entity_still_earns(app, db):
    """An entity with an active loyalty-eligible contract still earns bonuses."""
    with app.app_context():
        _program, strike, consec = _build_program_with_rules()
        user = build_entity_user(loyalty_eligible=True)
        uid = user.id

        seed_consecutive_run(uid, strike, count=2)
        seed_delivered_orders(uid, count=2, total=Decimal("50000"))

        order_service = OrderService()
        order = deliver_paid_order(order_service, uid, total=Decimal("50000"))

        assert strike_achievement_count(uid, strike.id) == 3
        assert consecutive_award_total(uid, consec.id) > 0
        assert _purchase_earned_total(uid) > 0
        assert order.status == OrderStatus.DELIVERED


# ===========================================================================
# INELIGIBLE ENTITY: earns NOTHING, delivery still completes
# ===========================================================================


def test_ineligible_entity_earns_nothing_via_real_delivery(app, db):
    """An entity with no loyalty-eligible contract earns ZERO purchase, ZERO
    streak, ZERO consecutive — yet the order still reaches DELIVERED."""
    with app.app_context():
        _program, strike, consec = _build_program_with_rules()
        user = build_entity_user(loyalty_eligible=False)
        uid = user.id

        # Seed a FULL consecutive run + qualifying delivered orders so that, absent
        # the guard, a real delivery WOULD award strike + consecutive + purchase.
        seed_consecutive_run(uid, strike, count=2)
        seed_delivered_orders(uid, count=2, total=Decimal("50000"))

        order_service = OrderService()
        order = deliver_paid_order(order_service, uid, total=Decimal("50000"))

        # The real delivery must add NO new strike bonus (only the 2 seeded remain).
        assert strike_achievement_count(uid, strike.id) == 2
        # No consecutive bonus at all.
        assert _consecutive_bonus_count(uid) == 0
        assert consecutive_award_total(uid, consec.id) == 0
        # No purchase AquaCoins earned.
        assert _purchase_earned_total(uid) == 0
        # Delivery is never blocked.
        assert order.status == OrderStatus.DELIVERED


# ===========================================================================
# Direct-call guard: the two LoyaltyService entry points return/award nothing
# ===========================================================================


def test_direct_calls_award_nothing_for_ineligible_entity(app, db):
    """update_consecutive_strikes returns False and update_streak awards nothing
    for an ineligible entity, even with qualifying delivered orders present."""
    with app.app_context():
        _program, strike, consec = _build_program_with_rules()
        user = build_entity_user(loyalty_eligible=False)
        uid = user.id

        # Qualifying delivered orders + a full prior consecutive run.
        seed_delivered_orders(uid, count=3, total=Decimal("50000"))
        seed_consecutive_run(uid, strike, count=2)

        svc = LoyaltyService()

        # Direct consecutive call: returns False, awards nothing new.
        assert svc.update_consecutive_strikes(uid) is False
        assert _consecutive_bonus_count(uid) == 0

        # Direct streak call: awards no new strike despite 3 qualifying orders.
        before = strike_achievement_count(uid, strike.id)
        svc.update_streak(uid)
        after = strike_achievement_count(uid, strike.id)
        assert after == before == 2
        assert _consecutive_bonus_count(uid) == 0
