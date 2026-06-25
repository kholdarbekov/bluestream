"""E2E tests: consecutive-strike bonus rule — repeat-every-N and idempotency.

Dimension: repeat_idempotency (cases 01–18)

All expected values are anchored to the spec at
docs/superpowers/specs/2026-06-24-consecutive-strike-bonus-rule-design.md,
NOT to whatever the code happens to produce.  A spec violation surfaces as a
real test failure so it can be investigated, not silently accepted.

Mechanism key:
- backdated_ledger: seed prior achievements directly as ledger rows, then call
  update_consecutive_strikes(user_id) to drive the award logic.
- real_flow: place and deliver a real order via deliver_paid_order to fire the
  full OrderService -> LoyaltyService -> update_consecutive_strikes path.
- mixed: combination of both.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from business_app import db as _db
from business_app.models.loyalty import LoyaltyTransaction
from business_app.services.loyalty_service import LoyaltyService
from business_app.utils.constants import LoyaltyActionType, LoyaltyTransactionType
from tests.e2e._consecutive_strike_helpers import (
    consecutive_award_total,
    consecutive_awards,
    deliver_paid_order,
    get_or_create_default_program,
    make_consecutive_rule,
    make_strike_rule,
    seed_consecutive_run,
    seed_strike_achievement,
    silence_loyalty_notifications,
    strike_achievement_count,
)

# ---------------------------------------------------------------------------
# Module marker
# ---------------------------------------------------------------------------
pytestmark = pytest.mark.e2e


# ---------------------------------------------------------------------------
# Autouse fixture: silence notification side-effects
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _silence(monkeypatch):
    silence_loyalty_notifications(monkeypatch)


# ---------------------------------------------------------------------------
# Helper: call update_consecutive_strikes inside a fresh service instance
# ---------------------------------------------------------------------------


def _update(user_id: int) -> bool:
    svc = LoyaltyService()
    return svc.update_consecutive_strikes(user_id)


# ---------------------------------------------------------------------------
# Case 01: 2N achievements yield exactly 2 awards (milestones 1 and 2)
# ---------------------------------------------------------------------------


def test_repeat_idempotency_01_two_n_achievements_two_awards(app, db, sample_user):
    """2N=12 achievements -> exactly 2 CONSECUTIVE_STREAK_BONUS rows, milestones 1 & 2.

    Spec §5.3: target_awards = 12 // 6 = 2; awards milestones 1 and 2 exactly once each.
    """
    with app.app_context():
        program = get_or_create_default_program()
        strike = make_strike_rule(program, name="3 in 30", window_days=30, bonus_points=100)
        rule = make_consecutive_rule(
            program, strikes=[strike], name="Champion",
            required_consecutive=6, combine_mode="all", bonus_points=500,
        )

        # Seed 12 consecutive achievements spaced 30d apart
        seed_consecutive_run(sample_user.id, strike, count=12, spacing_days=30)

        _update(sample_user.id)

        awards = consecutive_awards(sample_user.id, rule.id)
        assert len(awards) == 2, f"Expected exactly 2 awards, got {len(awards)}"

        milestones = sorted(a.extra_data["milestone"] for a in awards)
        assert milestones == [1, 2], f"Expected milestones [1, 2], got {milestones}"

        assert consecutive_award_total(sample_user.id, rule.id) == 1000  # 2 * 500


# ---------------------------------------------------------------------------
# Case 02: 3N achievements yield exactly 3 awards
# ---------------------------------------------------------------------------


def test_repeat_idempotency_02_three_n_achievements_three_awards(app, db, sample_user):
    """18 achievements (3*6) -> exactly 3 CONSECUTIVE_STREAK_BONUS rows.

    Spec §5.3: floor(18/6)=3 distinct milestones in a single evaluation pass.
    """
    with app.app_context():
        program = get_or_create_default_program()
        strike = make_strike_rule(program, name="3 in 30", window_days=30, bonus_points=100)
        rule = make_consecutive_rule(
            program, strikes=[strike], name="Triple Champ",
            required_consecutive=6, combine_mode="all", bonus_points=200,
        )

        seed_consecutive_run(sample_user.id, strike, count=18, spacing_days=30)

        _update(sample_user.id)

        awards = consecutive_awards(sample_user.id, rule.id)
        assert len(awards) == 3, f"Expected exactly 3 awards, got {len(awards)}"

        milestones = sorted(a.extra_data["milestone"] for a in awards)
        assert milestones == [1, 2, 3]

        for award in awards:
            assert award.transaction_type == LoyaltyTransactionType.BONUS
            assert award.extra_data["action_type"] == LoyaltyActionType.CONSECUTIVE_STREAK_BONUS.value
            assert award.extra_data["consecutive_strike_rule_id"] == rule.id

        assert consecutive_award_total(sample_user.id, rule.id) == 600  # 3 * 200


# ---------------------------------------------------------------------------
# Case 03: 2N+2=14 achievements yield exactly 2 awards (not 3)
# ---------------------------------------------------------------------------


def test_repeat_idempotency_03_non_multiple_yields_floor(app, db, sample_user):
    """14 achievements (2*6+2) -> exactly 2 awards, milestone 3 not reached.

    Spec §5.3: target_awards = 14 // 6 = 2 (integer division). No third award.
    """
    with app.app_context():
        program = get_or_create_default_program()
        strike = make_strike_rule(program, name="3 in 30", window_days=30, bonus_points=100)
        rule = make_consecutive_rule(
            program, strikes=[strike], name="Partial Champ",
            required_consecutive=6, combine_mode="all", bonus_points=300,
        )

        seed_consecutive_run(sample_user.id, strike, count=14, spacing_days=30)

        _update(sample_user.id)

        awards = consecutive_awards(sample_user.id, rule.id)
        assert len(awards) == 2, f"Expected exactly 2 awards, got {len(awards)}"
        milestones = sorted(a.extra_data["milestone"] for a in awards)
        assert milestones == [1, 2]

        assert consecutive_award_total(sample_user.id, rule.id) == 600  # 2 * 300


# ---------------------------------------------------------------------------
# Case 04: Re-running update_consecutive_strikes without new achievements does
#          not double-award
# ---------------------------------------------------------------------------


def test_repeat_idempotency_04_rerun_no_double_award(app, db, sample_user):
    """Second call with no new achievements produces no additional awards.

    Spec §5.3 steps 3-4: already=1, target_awards=1 -> range(2,2) is empty.
    """
    with app.app_context():
        program = get_or_create_default_program()
        strike = make_strike_rule(program, name="3 in 30", window_days=30, bonus_points=100)
        rule = make_consecutive_rule(
            program, strikes=[strike], name="Idempotent Champ",
            required_consecutive=6, combine_mode="all", bonus_points=1000,
        )

        seed_consecutive_run(sample_user.id, strike, count=6, spacing_days=30)

        # First call awards milestone 1
        _update(sample_user.id)
        awards_after_first = consecutive_awards(sample_user.id, rule.id)
        assert len(awards_after_first) == 1
        assert awards_after_first[0].extra_data["milestone"] == 1
        assert consecutive_award_total(sample_user.id, rule.id) == 1000

        # Second call: no new achievements added — must not produce more rows
        _update(sample_user.id)
        awards_after_second = consecutive_awards(sample_user.id, rule.id)
        assert len(awards_after_second) == 1, (
            f"Expected still 1 award after second call, got {len(awards_after_second)}"
        )
        assert consecutive_award_total(sample_user.id, rule.id) == 1000


# ---------------------------------------------------------------------------
# Case 05: N-1 achievements yield zero awards
# ---------------------------------------------------------------------------


def test_repeat_idempotency_05_below_threshold_no_award(app, db, sample_user):
    """5 achievements (N-1=5) -> zero awards, update_consecutive_strikes returns False.

    Spec §5.3 step 1: combined < N (5 < 6) -> skip entirely.
    """
    with app.app_context():
        program = get_or_create_default_program()
        strike = make_strike_rule(program, name="3 in 30", window_days=30, bonus_points=100)
        rule = make_consecutive_rule(
            program, strikes=[strike], name="Almost Champ",
            required_consecutive=6, combine_mode="all", bonus_points=500,
        )

        seed_consecutive_run(sample_user.id, strike, count=5, spacing_days=30)

        result = _update(sample_user.id)
        assert result is False

        awards = consecutive_awards(sample_user.id, rule.id)
        assert len(awards) == 0
        assert consecutive_award_total(sample_user.id, rule.id) == 0


# ---------------------------------------------------------------------------
# Case 06: Milestone numbers are 1-based (not 0-based)
# ---------------------------------------------------------------------------


def test_repeat_idempotency_06_milestones_are_one_based(app, db, sample_user):
    """12 achievements with N=4 -> milestones {1, 2, 3}, no milestone=0.

    Spec §5.3 step 4: range(already+1, target_awards+1) with already=0, target=3
    produces [1, 2, 3].
    """
    with app.app_context():
        program = get_or_create_default_program()
        strike = make_strike_rule(program, name="3 in 30", window_days=30, bonus_points=100)
        rule = make_consecutive_rule(
            program, strikes=[strike], name="Base Champ",
            required_consecutive=4, combine_mode="all", bonus_points=100,
        )

        seed_consecutive_run(sample_user.id, strike, count=12, spacing_days=30)

        _update(sample_user.id)

        awards = consecutive_awards(sample_user.id, rule.id)
        assert len(awards) == 3  # 12 // 4 = 3

        milestones = {a.extra_data["milestone"] for a in awards}
        assert milestones == {1, 2, 3}, f"Expected milestones {{1,2,3}}, got {milestones}"
        assert 0 not in milestones, "Milestone 0 should never appear (must be 1-based)"


# ---------------------------------------------------------------------------
# Case 07: Two distinct consecutive rules referencing the same strike award
#          independently
# ---------------------------------------------------------------------------


def test_repeat_idempotency_07_two_rules_same_strike_independent(app, db, sample_user):
    """R1 (N=3) and R2 (N=6), both on the same strike; 6 achievements.

    R1: 6//3=2 awards (milestones 1,2; total 400 pts)
    R2: 6//6=1 award  (milestone 1; total 500 pts)
    Grand total = 900 pts. No cross-contamination between rule_ids.
    Spec §5.3: each rule is evaluated independently.
    """
    with app.app_context():
        program = get_or_create_default_program()
        strike = make_strike_rule(program, name="3 in 30", window_days=30, bonus_points=100)
        r1 = make_consecutive_rule(
            program, strikes=[strike], name="R1",
            required_consecutive=3, combine_mode="all", bonus_points=200,
        )
        r2 = make_consecutive_rule(
            program, strikes=[strike], name="R2",
            required_consecutive=6, combine_mode="all", bonus_points=500,
        )

        seed_consecutive_run(sample_user.id, strike, count=6, spacing_days=30)

        _update(sample_user.id)

        r1_awards = consecutive_awards(sample_user.id, r1.id)
        r2_awards = consecutive_awards(sample_user.id, r2.id)

        assert len(r1_awards) == 2, f"R1: expected 2 awards, got {len(r1_awards)}"
        assert sorted(a.extra_data["milestone"] for a in r1_awards) == [1, 2]
        assert consecutive_award_total(sample_user.id, r1.id) == 400  # 2 * 200

        assert len(r2_awards) == 1, f"R2: expected 1 award, got {len(r2_awards)}"
        assert r2_awards[0].extra_data["milestone"] == 1
        assert consecutive_award_total(sample_user.id, r2.id) == 500

        # No cross-contamination: each award references the correct rule
        for a in r1_awards:
            assert a.extra_data["consecutive_strike_rule_id"] == r1.id
        for a in r2_awards:
            assert a.extra_data["consecutive_strike_rule_id"] == r2.id


# ---------------------------------------------------------------------------
# Case 08: Same real delivery fires both a streak EARNED row AND a consecutive
#          BONUS row (distinct ledger entries)
# ---------------------------------------------------------------------------


def test_repeat_idempotency_08_real_flow_distinct_ledger_rows(
    app, db, sample_user, sample_product
):
    """deliver_paid_order fires update_streak -> strike EARNED + consecutive BONUS.

    The two rewards have different transaction_type and action_type:
    - Strike achievement: EARNED / streak_bonus
    - Consecutive milestone: BONUS / consecutive_streak_bonus
    Spec §5.4: update_consecutive_strikes is called after the strike-award loop.
    """
    from business_app.models.loyalty import LoyaltyTierConfig
    from unittest.mock import MagicMock

    with app.app_context():
        program = get_or_create_default_program()

        # Pin a Bronze tier so purchase-points calc works
        if not LoyaltyTierConfig.query.filter_by(program_id=program.id).first():
            tier = LoyaltyTierConfig(
                program_id=program.id, name="Bronze", display_order=0,
                min_points=0, max_points=None, points_multiplier=1.0, is_active=True,
            )
            _db.session.add(tier)
            _db.session.commit()

        # Strike rule: requires 3 orders in 30 days
        strike = make_strike_rule(
            program, name="3 in 30",
            required_orders=3, window_days=30, bonus_points=150,
        )
        # Consecutive rule: N=2 -> awarded after 2 consecutive strike achievements
        rule = make_consecutive_rule(
            program, strikes=[strike], name="2-in-a-row",
            required_consecutive=2, combine_mode="all", bonus_points=400,
        )

        now = datetime.now(timezone.utc)

        # Seed 1 prior strike achievement (run of 1) so next delivery makes run=2
        seed_strike_achievement(sample_user.id, strike, when=now - timedelta(days=30))

        # Deliver orders so that qualifying_order_count >= 3 in the last 30 days.
        # We need 3 delivered orders in window for the strike to fire.
        # Seed 2 previous delivered orders (backdated within window) via raw rows:
        from business_app.models.order import Order, OrderItem
        from shared.enums import OrderStatus, PaymentMethod
        for offset in [25, 20]:
            o = Order(
                user_id=sample_user.id,
                order_number=f"CSH-PREV-{offset}",
                status=OrderStatus.DELIVERED,
                subtotal=Decimal("30000"),
                total_amount=Decimal("30000"),
                payment_method=PaymentMethod.CLICK,
                is_paid=True,
            )
            _db.session.add(o)
            _db.session.flush()
            _db.session.add(OrderItem(
                order_id=o.id, product_id=sample_product.id,
                quantity=1, unit_price=Decimal("30000"), total_price=Decimal("30000"),
            ))
            o.created_at = now - timedelta(days=offset)
            o.delivered_at = now - timedelta(days=offset)
            _db.session.commit()

        from business_app.services.order_service import OrderService
        from unittest.mock import MagicMock
        from types import SimpleNamespace

        mock_inv = MagicMock()
        mock_inv.check_multiple_products_availability.return_value = [
            SimpleNamespace(
                product_id=sample_product.id,
                requested_quantity=2,
                available_quantity=100,
                reserved_quantity=0,
                is_available=True,
                reason="Available",
            )
        ]
        mock_inv.reserve_inventory.return_value = {"success": True, "expires_at": None}
        mock_inv.release_reservations.return_value = {"success": True}

        order_service = OrderService(inventory_service=mock_inv)
        deliver_paid_order(
            order_service,
            user_id=sample_user.id,
            total=Decimal("30000"),
            payment="prepaid",
            product=sample_product,
        )

        # Check for distinct ledger rows
        all_txns = LoyaltyTransaction.query.filter_by(user_id=sample_user.id).all()

        streak_bonus_rows = [
            t for t in all_txns
            if (t.extra_data or {}).get("action_type") == LoyaltyActionType.STREAK_BONUS.value
            and (t.extra_data or {}).get("streak_rule_id") == strike.id
        ]
        consec_bonus_rows = [
            t for t in all_txns
            if (t.extra_data or {}).get("action_type") == LoyaltyActionType.CONSECUTIVE_STREAK_BONUS.value
            and (t.extra_data or {}).get("consecutive_strike_rule_id") == rule.id
        ]

        assert len(streak_bonus_rows) >= 1, "Expected at least 1 STREAK_BONUS EARNED row"
        for r in streak_bonus_rows:
            assert r.transaction_type == LoyaltyTransactionType.EARNED

        assert len(consec_bonus_rows) == 1, (
            f"Expected 1 CONSECUTIVE_STREAK_BONUS BONUS row, got {len(consec_bonus_rows)}"
        )
        assert consec_bonus_rows[0].transaction_type == LoyaltyTransactionType.BONUS
        assert consec_bonus_rows[0].extra_data["milestone"] == 1
        assert consec_bonus_rows[0].points == 400

        # No row mixes both action_types
        for t in all_txns:
            ed = t.extra_data or {}
            assert not (
                ed.get("action_type") == LoyaltyActionType.STREAK_BONUS.value
                and ed.get("consecutive_strike_rule_id") == rule.id
            ), "A row must not carry both streak_bonus and consecutive_strike_rule_id"


# ---------------------------------------------------------------------------
# Case 09: Real re-delivery is idempotent across all award paths
# ---------------------------------------------------------------------------


def test_repeat_idempotency_09_real_redelivery_idempotent(
    app, db, sample_user, sample_product
):
    """Second _handle_status_change_actions on same delivered order adds no rows.

    Guards: has_purchase_award (purchase), streak cooldown (strike),
    _consecutive_awards_since (consecutive).
    Spec §5.3 step 3: already=1, target_awards=1 -> range(2,2) empty.
    """
    from business_app.models.loyalty import LoyaltyTierConfig
    from business_app.models.order import Order, OrderItem
    from business_app.services.order_service import OrderService
    from unittest.mock import MagicMock
    from types import SimpleNamespace
    from shared.enums import OrderStatus, PaymentMethod

    with app.app_context():
        program = get_or_create_default_program()

        if not LoyaltyTierConfig.query.filter_by(program_id=program.id).first():
            tier = LoyaltyTierConfig(
                program_id=program.id, name="Bronze", display_order=0,
                min_points=0, max_points=None, points_multiplier=1.0, is_active=True,
            )
            _db.session.add(tier)
            _db.session.commit()

        # Strike: requires 3 orders in 30 days
        strike = make_strike_rule(
            program, name="3 in 30",
            required_orders=3, window_days=30, bonus_points=100,
        )
        # Consecutive rule: N=1 -> awarded on FIRST consecutive achievement
        rule = make_consecutive_rule(
            program, strikes=[strike], name="First-Timer",
            required_consecutive=1, combine_mode="all", bonus_points=300,
        )

        now = datetime.now(timezone.utc)

        # Seed 2 delivered orders in window so the new delivery makes 3
        for offset in [25, 20]:
            o = Order(
                user_id=sample_user.id,
                order_number=f"CSH-IDMP-{offset}",
                status=OrderStatus.DELIVERED,
                subtotal=Decimal("30000"),
                total_amount=Decimal("30000"),
                payment_method=PaymentMethod.CLICK,
                is_paid=True,
            )
            _db.session.add(o)
            _db.session.flush()
            _db.session.add(OrderItem(
                order_id=o.id, product_id=sample_product.id,
                quantity=1, unit_price=Decimal("30000"), total_price=Decimal("30000"),
            ))
            o.created_at = now - timedelta(days=offset)
            o.delivered_at = now - timedelta(days=offset)
            _db.session.commit()

        mock_inv = MagicMock()
        mock_inv.check_multiple_products_availability.return_value = [
            SimpleNamespace(
                product_id=sample_product.id,
                requested_quantity=2,
                available_quantity=100,
                reserved_quantity=0,
                is_available=True,
                reason="Available",
            )
        ]
        mock_inv.reserve_inventory.return_value = {"success": True, "expires_at": None}
        mock_inv.release_reservations.return_value = {"success": True}

        order_service = OrderService(inventory_service=mock_inv)
        order = deliver_paid_order(
            order_service,
            user_id=sample_user.id,
            total=Decimal("30000"),
            payment="prepaid",
            product=sample_product,
        )

        # Count rows after first delivery
        all_after_first = LoyaltyTransaction.query.filter_by(user_id=sample_user.id).all()
        count_after_first = len(all_after_first)
        assert count_after_first >= 1, "Expected at least 1 ledger row after first delivery"

        # Verify consecutive award happened
        consec_rows = [
            t for t in all_after_first
            if (t.extra_data or {}).get("action_type") == LoyaltyActionType.CONSECUTIVE_STREAK_BONUS.value
        ]
        assert len(consec_rows) == 1, (
            f"Expected 1 consecutive award after first delivery, got {len(consec_rows)}"
        )

        # Second call: simulate a double-trigger by calling _handle_status_change_actions again
        from shared.enums import OrderStatus as OS
        order_service._handle_status_change_actions(order, OS.DELIVERED, commit=True)

        all_after_second = LoyaltyTransaction.query.filter_by(user_id=sample_user.id).all()
        count_after_second = len(all_after_second)

        assert count_after_second == count_after_first, (
            f"Expected no new rows after re-delivery; had {count_after_first}, now {count_after_second}"
        )


# ---------------------------------------------------------------------------
# Case 10: combine_mode=any: 2N on one strike, 0 on other -> 2 awards
# ---------------------------------------------------------------------------


def test_repeat_idempotency_10_combine_any_one_strike_dominates(app, db, sample_user):
    """combine_mode=any: 6 achievements on A, 0 on B -> combined=max(6,0)=6.

    target_awards = 6 // 3 = 2. Two awards (milestones 1, 2). Total = 500 pts.
    Spec §5.2: combine_mode='any' uses max(per-strike counts).
    """
    with app.app_context():
        program = get_or_create_default_program()
        strike_a = make_strike_rule(program, name="A", window_days=30, bonus_points=100)
        strike_b = make_strike_rule(program, name="B", window_days=30, bonus_points=100)
        rule = make_consecutive_rule(
            program, strikes=[strike_a, strike_b], name="Any Mode",
            required_consecutive=3, combine_mode="any", bonus_points=250,
        )

        # Only seed for strike_a; strike_b has 0 achievements
        seed_consecutive_run(sample_user.id, strike_a, count=6, spacing_days=30)

        _update(sample_user.id)

        awards = consecutive_awards(sample_user.id, rule.id)
        assert len(awards) == 2, f"Expected 2 awards (combine_mode=any), got {len(awards)}"
        milestones = sorted(a.extra_data["milestone"] for a in awards)
        assert milestones == [1, 2]
        assert consecutive_award_total(sample_user.id, rule.id) == 500  # 2 * 250


# ---------------------------------------------------------------------------
# Case 11: combine_mode=all: N on both strikes -> 1 award; second call idempotent
# ---------------------------------------------------------------------------


def test_repeat_idempotency_11_combine_all_n_on_both_one_award_idempotent(
    app, db, sample_user
):
    """4 achievements on A (window=30) and 4 on B (window=40) -> combined=min(4,4)=4.

    First call: target_awards=4//4=1, 1 CONSECUTIVE_STREAK_BONUS row (600 pts).
    Second call: already=1, target_awards=1, range(2,2) -> no new row.
    Spec §5.3 steps 3-4.
    """
    with app.app_context():
        program = get_or_create_default_program()
        strike_a = make_strike_rule(program, name="A30", window_days=30, bonus_points=100)
        strike_b = make_strike_rule(program, name="B40", window_days=40, bonus_points=100)
        rule = make_consecutive_rule(
            program, strikes=[strike_a, strike_b], name="Both Need 4",
            required_consecutive=4, combine_mode="all", bonus_points=600,
        )

        seed_consecutive_run(sample_user.id, strike_a, count=4, spacing_days=30)
        seed_consecutive_run(sample_user.id, strike_b, count=4, spacing_days=40)

        # First call
        _update(sample_user.id)
        awards = consecutive_awards(sample_user.id, rule.id)
        assert len(awards) == 1
        assert awards[0].extra_data["milestone"] == 1
        assert consecutive_award_total(sample_user.id, rule.id) == 600

        # Second call: idempotent
        _update(sample_user.id)
        awards_after = consecutive_awards(sample_user.id, rule.id)
        assert len(awards_after) == 1, (
            f"Expected still 1 award after second call, got {len(awards_after)}"
        )
        assert consecutive_award_total(sample_user.id, rule.id) == 600


# ---------------------------------------------------------------------------
# Case 12: Broken run resets; only recent 2 achievements count
# ---------------------------------------------------------------------------


def test_repeat_idempotency_12_broken_run_resets_carry_over_zero(app, db, sample_user):
    """8 achievements with 90d gap (>60d=2*30) between block of 6 and recent 2.

    Only the 2 recent achievements form the current run. combined=2 < N=6 -> skipped.
    Zero CONSECUTIVE_STREAK_BONUS rows.
    Spec §5.1 step 2: gap >= 2*W breaks the run entirely.
    """
    with app.app_context():
        program = get_or_create_default_program()
        strike = make_strike_rule(program, name="3 in 30", window_days=30, bonus_points=100)
        rule = make_consecutive_rule(
            program, strikes=[strike], name="Break Test",
            required_consecutive=6, combine_mode="all", bonus_points=500,
        )

        now = datetime.now(timezone.utc)

        # Old block of 6 (about 270d..120d ago), all consecutive internally
        for k in range(6):
            when = now - timedelta(days=270 - k * 30)
            seed_strike_achievement(sample_user.id, strike, when=when)

        # Gap of 90 days (> 2*30=60d) -> run broken
        # Recent 2 achievements, 60d and 30d ago (gap=30d < 60d, so these 2 are consecutive)
        seed_strike_achievement(sample_user.id, strike, when=now - timedelta(days=60))
        seed_strike_achievement(sample_user.id, strike, when=now - timedelta(days=30))

        result = _update(sample_user.id)

        awards = consecutive_awards(sample_user.id, rule.id)
        assert len(awards) == 0, (
            f"Expected 0 awards (broken run), got {len(awards)}"
        )
        assert result is False


# ---------------------------------------------------------------------------
# Case 13: Old run awards don't count against new run's already-count
# ---------------------------------------------------------------------------


def test_repeat_idempotency_13_old_run_awards_scoped_to_old_run_start(
    app, db, sample_user
):
    """Completed old run awards do not suppress fresh milestones in a new run.

    Scenario:
    1. Seed 3 achievements ~240..180d ago (old consecutive run; W=30, gap<60 internally).
    2. _update -> milestone-1 award stamped at DB time (~now).
    3. Backdate that award's created_at to ~210d ago (inside the old run, before the gap).
    4. Gap of ~120d (> 2*W=60) resets the run.  Seed 6 new achievements 60..10d ago
       (spacing=10d < 60, all consecutive; new run_start ~ now-60d).
    5. _update again: new run_start = earliest new achievement = now-60d.
       _consecutive_awards_since(run_start=now-60d) returns 0 because the old award
       is backdated to ~210d ago, which is < run_start.  already=0.
       target_awards = 6 // 3 = 2.  Two fresh awards: milestones 1 and 2.
    6. Total awards = 1 (old) + 2 (new) = 3.  New awards are milestones 1 and 2
       of the fresh run, each worth 100 pts -> new total contribution = 200 pts.

    Spec §5.3 step 3: already is scoped to >= run_start of the CURRENT run;
    awards from a prior completed run (whose created_at precedes run_start) are
    invisible to the new-run idempotency guard.
    """
    with app.app_context():
        program = get_or_create_default_program()
        # W = 30 days; gap threshold = 2*W = 60 days
        strike = make_strike_rule(program, name="3 in 30", window_days=30, bonus_points=100)
        rule = make_consecutive_rule(
            program, strikes=[strike], name="Fresh Run",
            required_consecutive=3, combine_mode="all", bonus_points=100,
        )

        now = datetime.now(timezone.utc)

        # --- Old run: 3 consecutive achievements, ending ~180d ago ---
        # Spaced 30d apart: 240d, 210d, 180d ago; gaps = 30d < 60 -> consecutive.
        for k in range(3):
            seed_strike_achievement(
                sample_user.id, strike, when=now - timedelta(days=240 - k * 30)
            )

        # First evaluation: awards milestone 1 from old run.
        # The award's created_at is set by the DB to ~now (wall-clock time of this call).
        _update(sample_user.id)
        awards_first = consecutive_awards(sample_user.id, rule.id)
        assert len(awards_first) == 1, (
            f"Precondition: expected 1 old award, got {len(awards_first)}"
        )
        assert awards_first[0].extra_data["milestone"] == 1, (
            "Precondition: old award must be milestone 1"
        )

        # Backdate the old award's created_at to ~210d ago so it falls BEFORE the
        # new run_start (which will be ~60d ago).  This is the crux of the test:
        # it makes _consecutive_awards_since(run_start=now-60d) return 0 for the new run.
        from business_app import db as _db_local
        old_award = awards_first[0]
        old_award.created_at = now - timedelta(days=210)
        _db_local.session.commit()

        # --- Gap: last old achievement = 180d ago; first new = 60d ago.
        # Gap between them = 120d > 2*W=60d -> run resets to 0. ---

        # --- New run: 6 consecutive achievements, newest = 10d ago ---
        # Spaced 10d apart (10d < 60 -> consecutive). Earliest = now-60d (new run_start).
        new_run_start_approx = now - timedelta(days=60)
        for k in range(6):
            when = new_run_start_approx + timedelta(days=k * 10)
            seed_strike_achievement(sample_user.id, strike, when=when)

        # Second evaluation: new combined run = 6, target_awards = 6 // 3 = 2.
        # _consecutive_awards_since(run_start ~ now-60d) = 0 because old award is
        # backdated to now-210d (< run_start). Awards milestones 1 AND 2 fresh.
        _update(sample_user.id)
        awards_all = consecutive_awards(sample_user.id, rule.id)

        # 1 old + 2 new = 3 total
        assert len(awards_all) == 3, (
            f"Expected 3 total awards (1 old + 2 new fresh), got {len(awards_all)}"
        )

        milestones = sorted(a.extra_data["milestone"] for a in awards_all)
        # Two of the awards are the fresh run milestones 1 and 2; one is the old milestone 1.
        assert milestones == [1, 1, 2], (
            f"Expected milestones [1, 1, 2] (one old milestone-1 + new milestones 1 & 2), "
            f"got {milestones}"
        )

        # Exact points: 1 old (100) + 2 new (100 each) = 300 pts total
        assert consecutive_award_total(sample_user.id, rule.id) == 300, (
            f"Expected 300 pts total (3 x 100), got {consecutive_award_total(sample_user.id, rule.id)}"
        )

        # Verify the new-run awards carry milestone numbers from the fresh run.
        # LoyaltyTransaction.created_at from sqlite may be tz-naive; compare against
        # a naive cutoff to avoid TypeError (see test mechanics note in module docstring).
        cutoff_naive = new_run_start_approx.replace(tzinfo=None)
        new_run_awards = [
            a for a in awards_all
            if (
                a.created_at.replace(tzinfo=None)
                if a.created_at.tzinfo is not None
                else a.created_at
            ) >= cutoff_naive
        ]
        assert len(new_run_awards) == 2, (
            "Expected exactly 2 awards with created_at >= new run_start"
        )
        new_milestones = sorted(a.extra_data["milestone"] for a in new_run_awards)
        assert new_milestones == [1, 2], (
            f"Fresh run milestones must be exactly [1, 2], got {new_milestones}"
        )


# ---------------------------------------------------------------------------
# Case 14: Inactive rule is skipped entirely
# ---------------------------------------------------------------------------


def test_repeat_idempotency_14_inactive_rule_skipped(app, db, sample_user):
    """Rule with is_active=False -> zero awards even when N is satisfied.

    Spec §5.3 preamble: rule must be is_active AND is_effective(now).
    """
    with app.app_context():
        program = get_or_create_default_program()
        strike = make_strike_rule(program, name="3 in 30", window_days=30, bonus_points=100)
        rule = make_consecutive_rule(
            program, strikes=[strike], name="Inactive Rule",
            required_consecutive=3, combine_mode="all", bonus_points=500,
            is_active=False,
        )

        seed_consecutive_run(sample_user.id, strike, count=6, spacing_days=30)

        result = _update(sample_user.id)

        awards = consecutive_awards(sample_user.id, rule.id)
        assert len(awards) == 0, f"Expected 0 awards for inactive rule, got {len(awards)}"
        assert result is False


# ---------------------------------------------------------------------------
# Case 15: Expired rule (ends_at in past) is skipped
# ---------------------------------------------------------------------------


def test_repeat_idempotency_15_expired_rule_skipped(app, db, sample_user):
    """Rule with ends_at=now-1day -> zero awards even when N is satisfied.

    Spec §5.3 preamble: is_effective(now) returns False -> rule skipped.
    """
    with app.app_context():
        program = get_or_create_default_program()
        strike = make_strike_rule(program, name="3 in 30", window_days=30, bonus_points=100)
        now = datetime.now(timezone.utc)
        rule = make_consecutive_rule(
            program, strikes=[strike], name="Expired Rule",
            required_consecutive=3, combine_mode="all", bonus_points=500,
            is_active=True,
            ends_at=now - timedelta(days=1),
        )

        seed_consecutive_run(sample_user.id, strike, count=6, spacing_days=30)

        result = _update(sample_user.id)

        awards = consecutive_awards(sample_user.id, rule.id)
        assert len(awards) == 0, f"Expected 0 awards for expired rule, got {len(awards)}"
        assert result is False


# ---------------------------------------------------------------------------
# Case 16: Rule with zero attached strikes never fires
# ---------------------------------------------------------------------------


def test_repeat_idempotency_16_zero_attached_strikes_never_fires(app, db, sample_user):
    """Rule with rule.strikes=[] -> zero awards regardless of anything.

    Spec §5.2 edge case: 'a rule with zero attached strikes never fires'.
    Service guard: `if not rule.is_effective(now) or not rule.strikes: continue`.
    """
    with app.app_context():
        program = get_or_create_default_program()
        # Create the rule with no strikes at all (empty list)
        rule = make_consecutive_rule(
            program, strikes=[],  # intentionally empty
            name="No Strikes Rule",
            required_consecutive=3, combine_mode="all", bonus_points=500,
            is_active=True,
        )

        # No achievements seeded either
        result = _update(sample_user.id)

        awards = consecutive_awards(sample_user.id, rule.id)
        assert len(awards) == 0, f"Expected 0 awards for zero-strike rule, got {len(awards)}"
        assert result is False


# ---------------------------------------------------------------------------
# Case 17: Exactly N achievements (boundary) yields exactly 1 award
# ---------------------------------------------------------------------------


def test_repeat_idempotency_17_exactly_n_boundary_one_award(app, db, sample_user):
    """6 achievements with N=6 -> exactly 1 award (milestone 1, 750 pts).

    Spec §5.3 step 1: condition is combined < N (strict); combined=N=6 passes.
    target_awards = 6 // 6 = 1.
    """
    with app.app_context():
        program = get_or_create_default_program()
        strike = make_strike_rule(program, name="3 in 30", window_days=30, bonus_points=100)
        rule = make_consecutive_rule(
            program, strikes=[strike], name="Exact Boundary",
            required_consecutive=6, combine_mode="all", bonus_points=750,
        )

        seed_consecutive_run(sample_user.id, strike, count=6, spacing_days=30)

        _update(sample_user.id)

        awards = consecutive_awards(sample_user.id, rule.id)
        assert len(awards) == 1, f"Expected exactly 1 award at boundary N=6, got {len(awards)}"
        assert awards[0].extra_data["milestone"] == 1
        assert consecutive_award_total(sample_user.id, rule.id) == 750


# ---------------------------------------------------------------------------
# Case 18: combine_mode=all: weak strike (N-1) blocks all awards
# ---------------------------------------------------------------------------


def test_repeat_idempotency_18_combine_all_weak_link_blocks_award(app, db, sample_user):
    """12 achievements on A, only 5 (N-1) on B -> combined=min(12,5)=5 < N=6.

    Zero awards. Even though A has 2 milestones worth, B's shortfall blocks.
    Spec §5.2: combine_mode='all' uses min(per-strike counts).
    Spec §5.3 step 1: combined < N -> skip.
    """
    with app.app_context():
        program = get_or_create_default_program()
        strike_a = make_strike_rule(program, name="A", window_days=30, bonus_points=100)
        strike_b = make_strike_rule(program, name="B", window_days=30, bonus_points=100)
        rule = make_consecutive_rule(
            program, strikes=[strike_a, strike_b], name="Weakest Link",
            required_consecutive=6, combine_mode="all", bonus_points=800,
        )

        # A has 12 achievements; B has only 5
        seed_consecutive_run(sample_user.id, strike_a, count=12, spacing_days=30)
        seed_consecutive_run(sample_user.id, strike_b, count=5, spacing_days=30)

        result = _update(sample_user.id)

        awards = consecutive_awards(sample_user.id, rule.id)
        assert len(awards) == 0, (
            f"Expected 0 awards when weak link B has only 5 (< N=6), got {len(awards)}"
        )
        assert result is False
