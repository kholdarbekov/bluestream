"""E2E tests for LoyaltyConsecutiveStrikeRule — combine_mode='all' dimension.

All expected values are anchored to the spec:
  docs/superpowers/specs/2026-06-24-consecutive-strike-bonus-rule-design.md

Business rules (locked):
- combined = min(per-strike runs)  for combine_mode='all'
- gap < 2*W  → consecutive; gap >= 2*W → breaks the run (strict less-than)
- target_awards = combined // N
- run_start (for 'all') = max of per-strike run-starts; if any strike has no run
  (None), skip.
- _consecutive_awards_since counts CONSECUTIVE_STREAK_BONUS rows with
  created_at >= run_start (for idempotency / repeat-every-N)
- Rule evaluates only if is_active=True AND is_effective(now)
- Rule with zero attached strikes is always skipped
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest

from business_app import db as _db
from business_app.models.loyalty import (
    LoyaltyConsecutiveStrikeRule,
    LoyaltyProgram,
    LoyaltyStreakRule,
    LoyaltyTransaction,
)
from business_app.models.order import Order, OrderItem
from business_app.services.loyalty_service import LoyaltyService
from business_app.services.order_service import OrderService
from business_app.utils.constants import LoyaltyActionType, LoyaltyTransactionType
from shared.enums import OrderStatus, PaymentMethod

from tests.e2e._consecutive_strike_helpers import (
    build_entity_user,
    consecutive_award_total,
    consecutive_awards,
    deliver_paid_order,
    get_or_create_default_program,
    make_consecutive_rule,
    make_strike_rule,
    seed_consecutive_run,
    seed_delivered_orders,
    seed_strike_achievement,
    silence_loyalty_notifications,
    strike_achievement_count,
)

pytestmark = pytest.mark.e2e

# ---------------------------------------------------------------------------
# Autouse fixture — suppress all loyalty notification side-effects
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _silence(monkeypatch):
    silence_loyalty_notifications(monkeypatch)


# ---------------------------------------------------------------------------
# combine_all-01: Two strikes same window, both reach N — single bonus awarded
# ---------------------------------------------------------------------------


def test_combine_all_01_both_reach_n_single_bonus(app, db, sample_user):
    """Two strikes (30d each), both seeded with 6 consecutive achievements.
    combine_mode='all', N=6. Expect exactly 1 bonus row (1000 pts) after one call,
    and a second call (idempotency) produces no new rows."""
    with app.app_context():
        program = get_or_create_default_program()
        strike_a = make_strike_rule(program, name="A-3in30", required_orders=3, window_days=30, bonus_points=100)
        strike_b = make_strike_rule(program, name="B-3in30", required_orders=3, window_days=30, bonus_points=100)
        rule = make_consecutive_rule(
            program,
            strikes=[strike_a, strike_b],
            required_consecutive=6,
            combine_mode="all",
            bonus_points=1000,
        )

        # Seed 6 consecutive achievements for each strike (gap=30d < 60d threshold)
        seed_consecutive_run(sample_user.id, strike_a, count=6)
        seed_consecutive_run(sample_user.id, strike_b, count=6)

        svc = LoyaltyService()
        svc.update_consecutive_strikes(sample_user.id)

        awards = consecutive_awards(sample_user.id, rule.id)
        assert len(awards) == 1, "Exactly 1 CONSECUTIVE_STREAK_BONUS row expected"
        assert awards[0].extra_data.get("milestone") == 1
        assert consecutive_award_total(sample_user.id, rule.id) == 1000

        # Idempotency: second call produces no new rows
        svc.update_consecutive_strikes(sample_user.id)
        assert len(consecutive_awards(sample_user.id, rule.id)) == 1
        assert consecutive_award_total(sample_user.id, rule.id) == 1000


# ---------------------------------------------------------------------------
# combine_all-02: Spec worked example — strike A (30d) and strike B (40d)
# ---------------------------------------------------------------------------


def test_combine_all_02_spec_worked_example(app, db, sample_user):
    """Spec §1 worked example: A=3 orders/30d, B=5 orders/40d, N=6.
    Both seeded with 6 consecutive achievements on their own window clocks.
    min(6,6)=6 >= 6 → 1 bonus row, 1000 pts."""
    with app.app_context():
        program = get_or_create_default_program()
        strike_a = make_strike_rule(program, name="A-3in30", required_orders=3, window_days=30, bonus_points=100)
        strike_b = make_strike_rule(program, name="B-5in40", required_orders=5, window_days=40, bonus_points=100)
        rule = make_consecutive_rule(
            program,
            strikes=[strike_a, strike_b],
            required_consecutive=6,
            combine_mode="all",
            bonus_points=1000,
        )

        # A: 6 achievements at 30d spacing (gap=30d < 60d)
        seed_consecutive_run(sample_user.id, strike_a, count=6, spacing_days=30)
        # B: 6 achievements at 40d spacing (gap=40d < 80d)
        seed_consecutive_run(sample_user.id, strike_b, count=6, spacing_days=40)

        svc = LoyaltyService()
        svc.update_consecutive_strikes(sample_user.id)

        assert consecutive_award_total(sample_user.id, rule.id) == 1000
        awards = consecutive_awards(sample_user.id, rule.id)
        assert len(awards) == 1
        assert awards[0].extra_data.get("milestone") == 1


# ---------------------------------------------------------------------------
# combine_all-03: One strike at N, other at N-1 — min blocks the award
# ---------------------------------------------------------------------------


def test_combine_all_03_one_short_blocks_award(app, db, sample_user):
    """A=6 consecutive, B=5 consecutive. min(6,5)=5 < 6. No bonus expected."""
    with app.app_context():
        program = get_or_create_default_program()
        strike_a = make_strike_rule(program, name="A", window_days=30, bonus_points=100)
        strike_b = make_strike_rule(program, name="B", window_days=40, bonus_points=100)
        rule = make_consecutive_rule(
            program,
            strikes=[strike_a, strike_b],
            required_consecutive=6,
            combine_mode="all",
            bonus_points=1000,
        )

        seed_consecutive_run(sample_user.id, strike_a, count=6, spacing_days=30)
        seed_consecutive_run(sample_user.id, strike_b, count=5, spacing_days=40)

        svc = LoyaltyService()
        svc.update_consecutive_strikes(sample_user.id)

        assert len(consecutive_awards(sample_user.id, rule.id)) == 0
        assert consecutive_award_total(sample_user.id, rule.id) == 0


# ---------------------------------------------------------------------------
# combine_all-04: Boundary — exactly N for both strikes fires the award
# ---------------------------------------------------------------------------


def test_combine_all_04_boundary_exactly_n_fires(app, db, sample_user):
    """Off-by-one anchor: min(N, N) >= N must trigger. N=3 here.
    min(3,3)=3 >= 3 → 1 bonus (500 pts). If N-1 were used instead, no award."""
    with app.app_context():
        program = get_or_create_default_program()
        strike_a = make_strike_rule(program, name="A-3in30", required_orders=3, window_days=30, bonus_points=100)
        strike_b = make_strike_rule(program, name="B-3in30", required_orders=3, window_days=30, bonus_points=100)
        rule = make_consecutive_rule(
            program,
            strikes=[strike_a, strike_b],
            required_consecutive=3,
            combine_mode="all",
            bonus_points=500,
        )

        seed_consecutive_run(sample_user.id, strike_a, count=3, spacing_days=30)
        seed_consecutive_run(sample_user.id, strike_b, count=3, spacing_days=30)

        svc = LoyaltyService()
        svc.update_consecutive_strikes(sample_user.id)

        awards = consecutive_awards(sample_user.id, rule.id)
        assert len(awards) == 1
        assert consecutive_award_total(sample_user.id, rule.id) == 500


# ---------------------------------------------------------------------------
# combine_all-05: Strike A's run reset by a skipped period blocks the award
# ---------------------------------------------------------------------------


def test_combine_all_05_skipped_period_resets_run(app, db, sample_user):
    """Strike A (30d) has 3 old achievements, gap of 65d (>= 2*30d=60d), then 3 new.
    A's consecutive run = 3 (only new cluster after the break).
    B has 6 consecutive. min(3,6)=3 < 6 → no award."""
    with app.app_context():
        program = get_or_create_default_program()
        strike_a = make_strike_rule(program, name="A-3in30", required_orders=3, window_days=30, bonus_points=100)
        strike_b = make_strike_rule(program, name="B-5in40", required_orders=5, window_days=40, bonus_points=100)
        rule = make_consecutive_rule(
            program,
            strikes=[strike_a, strike_b],
            required_consecutive=6,
            combine_mode="all",
            bonus_points=1000,
        )

        now = datetime.now(timezone.utc)

        # Old cluster for A: 3 achievements far in the past
        for k in range(3):
            seed_strike_achievement(sample_user.id, strike_a, now - timedelta(days=300 - k * 30))

        # Gap of ~65 days separates old cluster from new cluster (>= 60d = 2*30d)
        # New cluster for A: 3 achievements (30d spacing, gap=30d < 60d → consecutive)
        for k in range(3):
            seed_strike_achievement(sample_user.id, strike_a, now - timedelta(days=95 - k * 30))
        # That gives: now-95, now-65, now-35 for new cluster.
        # Gap between now-35 (most recent new) and now-95 (oldest new): 60d? No, stepping back:
        # most recent new = now-35, next = now-65, oldest new = now-95
        # Gap: (now-35) - (now-65) = 30d < 60d ✓, (now-65) - (now-95) = 30d < 60d ✓
        # Gap between new cluster oldest (now-95) and old cluster newest (now-270 approx):
        # (now-95) - (now-270) = 175d >= 60d → break ✓
        # So A's run = 3 (only the new cluster)

        # B: 6 back-to-back (40d spacing, gap=40d < 80d)
        seed_consecutive_run(sample_user.id, strike_b, count=6, spacing_days=40)

        svc = LoyaltyService()
        svc.update_consecutive_strikes(sample_user.id)

        assert consecutive_award_total(sample_user.id, rule.id) == 0, (
            "A's run reset to 3 after skipped period; min(3,6)=3 < 6 should block award"
        )


# ---------------------------------------------------------------------------
# combine_all-06: Gap exactly at boundary (2*W) breaks the run (strict <)
# ---------------------------------------------------------------------------


def test_combine_all_06_gap_exactly_2w_breaks_run(app, db, sample_user):
    """Strike A (30d): 4 achievements where the gap between two adjacent ones is
    exactly 60d (= 2*30). Spec and implementation use strict < 2*W, so equality
    breaks the run. A's run = the count after the break."""
    with app.app_context():
        program = get_or_create_default_program()
        strike_a = make_strike_rule(program, name="A-30d", required_orders=3, window_days=30, bonus_points=100)
        strike_b = make_strike_rule(program, name="B-40d", required_orders=3, window_days=40, bonus_points=100)
        rule = make_consecutive_rule(
            program,
            strikes=[strike_a, strike_b],
            required_consecutive=6,
            combine_mode="all",
            bonus_points=1000,
        )

        now = datetime.now(timezone.utc)

        # Achievements for A (newest → oldest): now-10, now-40, now-100, now-130
        # Gaps (newest - next): 30d, 60d, 30d
        # The 60d gap == 2*30d is NOT < 60d → break.
        # Run from newest: now-10, now-40 → run=2 (stops at now-100 because gap=60d)
        seed_strike_achievement(sample_user.id, strike_a, now - timedelta(days=10))
        seed_strike_achievement(sample_user.id, strike_a, now - timedelta(days=40))
        seed_strike_achievement(sample_user.id, strike_a, now - timedelta(days=100))
        seed_strike_achievement(sample_user.id, strike_a, now - timedelta(days=130))

        # B: 6 back-to-back
        seed_consecutive_run(sample_user.id, strike_b, count=6, spacing_days=40)

        svc = LoyaltyService()
        run_a, _ = svc._strike_consecutive_run(sample_user.id, strike_a, now)
        # A's run should be 2 (only now-10 and now-40 before the boundary break)
        assert run_a == 2, f"Expected A run=2 (strict < 60d), got {run_a}"

        svc.update_consecutive_strikes(sample_user.id)

        # min(2, 6)=2 < 6 → no bonus
        assert consecutive_award_total(sample_user.id, rule.id) == 0


# ---------------------------------------------------------------------------
# combine_all-07: Staggered advancement — slower strike governs
# ---------------------------------------------------------------------------


def test_combine_all_07_staggered_slower_governs(app, db, sample_user):
    """First call: A=6, B=3. min(6,3)=3 < 6 → no award.
    After adding 3 more B achievements: A=6, B=6. min(6,6)=6 >= 6 → 1 award."""
    with app.app_context():
        program = get_or_create_default_program()
        strike_a = make_strike_rule(program, name="A-30d", required_orders=3, window_days=30, bonus_points=100)
        strike_b = make_strike_rule(program, name="B-40d", required_orders=5, window_days=40, bonus_points=100)
        rule = make_consecutive_rule(
            program,
            strikes=[strike_a, strike_b],
            required_consecutive=6,
            combine_mode="all",
            bonus_points=1000,
        )

        # Phase 1: A=6, B=3
        seed_consecutive_run(sample_user.id, strike_a, count=6, spacing_days=30)
        seed_consecutive_run(sample_user.id, strike_b, count=3, spacing_days=40)

        svc = LoyaltyService()
        svc.update_consecutive_strikes(sample_user.id)
        assert consecutive_award_total(sample_user.id, rule.id) == 0, "Phase 1: min(6,3)<6 → no award"

        # Phase 2: add 3 more B achievements (consecutive with existing B run)
        # The most recent B achievement is at spacing=40*3 days ago.
        # We need to add 3 more, each 40d after the last B.
        # The B run was seeded with newest at spacing_days=40 days ago.
        # seed_consecutive_run places newest at now - spacing_days*1.
        # So B run: now-120, now-80, now-40 (spacing 40d, count=3).
        # We extend B: now-40 is the newest, add now (spacing 40d from now-40):
        now = datetime.now(timezone.utc)
        # Add 3 more B achievements so total B run extends to 6
        # Place them after the last B (most recent was ~40d ago; add 3 more at 1d, 2d, 3d ahead of now)
        # Actually the consecutive run direction matters: achievements are newest-first.
        # The existing B run ends (oldest) at ~now-120d. New achievements must be
        # newer and gap < 2*40=80d from the newest existing (now-40d).
        # Add at now-30d, now-20d, now-10d (each 10d gap < 80d → consecutive with now-40d).
        # This extends B run to 6.
        seed_strike_achievement(sample_user.id, strike_b, now - timedelta(days=10))
        seed_strike_achievement(sample_user.id, strike_b, now - timedelta(days=20))
        seed_strike_achievement(sample_user.id, strike_b, now - timedelta(days=30))

        svc.update_consecutive_strikes(sample_user.id)
        assert consecutive_award_total(sample_user.id, rule.id) == 1000, "Phase 2: min(6,6)=6 → 1 award"


# ---------------------------------------------------------------------------
# combine_all-08: Repeat-every-N when strikes at 2N — 2 bonuses total
# ---------------------------------------------------------------------------


def test_combine_all_08_repeat_every_n_two_payouts(app, db, sample_user):
    """Phase 1: A=12, B=6. min(12,6)=6 → target=1 award.
    Phase 2: B extended to 12. min(12,12)=12 → target=2, already=1 → 1 more.
    Total = 2000 pts."""
    with app.app_context():
        program = get_or_create_default_program()
        strike_a = make_strike_rule(program, name="A-30d", required_orders=3, window_days=30, bonus_points=100)
        strike_b = make_strike_rule(program, name="B-40d", required_orders=3, window_days=40, bonus_points=100)
        rule = make_consecutive_rule(
            program,
            strikes=[strike_a, strike_b],
            required_consecutive=6,
            combine_mode="all",
            bonus_points=1000,
        )

        now = datetime.now(timezone.utc)

        # A: 12 achievements at 30d spacing (newest at now-30d)
        seed_consecutive_run(sample_user.id, strike_a, count=12, spacing_days=30)
        # B: 6 achievements at 40d spacing (newest at now-40d)
        seed_consecutive_run(sample_user.id, strike_b, count=6, spacing_days=40)

        svc = LoyaltyService()
        svc.update_consecutive_strikes(sample_user.id)
        # Phase 1: min(12,6)=6, target=6//6=1, already=0 → 1 award
        assert consecutive_award_total(sample_user.id, rule.id) == 1000, "Phase 1: 1 award"

        # Phase 2: add 6 more B achievements so B run = 12
        # B newest was at now-40d; add 6 more consecutively NEWER
        # Gaps must be < 2*40=80d. Place at now-35, now-30, now-25, now-20, now-15, now-10.
        for offset in [35, 30, 25, 20, 15, 10]:
            seed_strike_achievement(sample_user.id, strike_b, now - timedelta(days=offset))

        svc.update_consecutive_strikes(sample_user.id)
        # Phase 2: min(12,12)=12, target=12//6=2
        # run_start for 'all' = max(A_run_start, B_run_start).
        # B's run_start is now the oldest of the 12 B achievements (much older than A's).
        # A's run_start is now-12*30=now-360.
        # _consecutive_awards_since since run_start counts existing award (1 row).
        # already=1 → award range(2,3) → 1 new award, milestone=2.
        total = consecutive_award_total(sample_user.id, rule.id)
        assert total == 2000, f"Phase 2: expected total 2000, got {total}"


# ---------------------------------------------------------------------------
# combine_all-09: Idempotency — three calls never double-award
# ---------------------------------------------------------------------------


def test_combine_all_09_idempotency_three_calls(app, db, sample_user):
    """Three consecutive calls to update_consecutive_strikes with A=6 and B=6.
    Exactly 1 bonus row, 1000 pts total after all three calls."""
    with app.app_context():
        program = get_or_create_default_program()
        strike_a = make_strike_rule(program, name="A-30d", required_orders=3, window_days=30, bonus_points=100)
        strike_b = make_strike_rule(program, name="B-30d", required_orders=3, window_days=30, bonus_points=100)
        rule = make_consecutive_rule(
            program,
            strikes=[strike_a, strike_b],
            required_consecutive=6,
            combine_mode="all",
            bonus_points=1000,
        )

        seed_consecutive_run(sample_user.id, strike_a, count=6, spacing_days=30)
        seed_consecutive_run(sample_user.id, strike_b, count=6, spacing_days=30)

        svc = LoyaltyService()
        svc.update_consecutive_strikes(sample_user.id)
        svc.update_consecutive_strikes(sample_user.id)
        svc.update_consecutive_strikes(sample_user.id)

        awards = consecutive_awards(sample_user.id, rule.id)
        assert len(awards) == 1, f"Expected 1 bonus row (idempotency), got {len(awards)}"
        assert consecutive_award_total(sample_user.id, rule.id) == 1000


# ---------------------------------------------------------------------------
# combine_all-10: run_start anchoring — old bonus before run_start not counted
# ---------------------------------------------------------------------------


def test_combine_all_10_run_start_anchoring(app, db, sample_user):
    """A historic bonus row timestamped 200 days ago should NOT be counted by
    _consecutive_awards_since when computing 'already'. A new run of 6 should
    issue a fresh bonus (total rows=2: one old, one new)."""
    with app.app_context():
        program = get_or_create_default_program()
        strike_a = make_strike_rule(program, name="A-30d", required_orders=3, window_days=30, bonus_points=100)
        strike_b = make_strike_rule(program, name="B-40d", required_orders=3, window_days=40, bonus_points=100)
        rule = make_consecutive_rule(
            program,
            strikes=[strike_a, strike_b],
            required_consecutive=6,
            combine_mode="all",
            bonus_points=500,
        )

        now = datetime.now(timezone.utc)

        # Seed an OLD bonus row timestamped 200 days ago (before any new run_start)
        old_bonus = LoyaltyTransaction(
            user_id=sample_user.id,
            transaction_type=LoyaltyTransactionType.BONUS,
            points=500,
            description=rule.name,
            remaining_points=500,
            extra_data={
                "action_type": LoyaltyActionType.CONSECUTIVE_STREAK_BONUS.value,
                "consecutive_strike_rule_id": rule.id,
                "milestone": 1,
            },
        )
        _db.session.add(old_bonus)
        _db.session.flush()
        old_bonus.created_at = now - timedelta(days=200)
        _db.session.commit()

        # New run: 6 achievements each for A and B, all within the last 6*30=180 days
        # so run_start is at now - 6*30 = now-180d which is AFTER the old bonus (now-200d).
        seed_consecutive_run(sample_user.id, strike_a, count=6, spacing_days=30)
        seed_consecutive_run(sample_user.id, strike_b, count=6, spacing_days=40)

        svc = LoyaltyService()
        svc.update_consecutive_strikes(sample_user.id)

        all_bonus_rows = consecutive_awards(sample_user.id, rule.id)
        # Should have 2 rows: the old one + the new one
        assert len(all_bonus_rows) == 2, (
            f"Expected 2 bonus rows (old + new run), got {len(all_bonus_rows)}"
        )
        total = consecutive_award_total(sample_user.id, rule.id)
        assert total == 1000, f"Expected 1000 pts (500 old + 500 new), got {total}"


# ---------------------------------------------------------------------------
# combine_all-11: Three strikes all reach N — award fires (min of three)
# ---------------------------------------------------------------------------


def test_combine_all_11_three_strikes_all_reach_n(app, db, sample_user):
    """Three strikes A(30d), B(40d), C(20d). N=4. Each seeded with 4 consecutive.
    min(4,4,4)=4 >= 4 → 1 bonus (750 pts)."""
    with app.app_context():
        program = get_or_create_default_program()
        strike_a = make_strike_rule(program, name="A-30d", required_orders=3, window_days=30, bonus_points=100)
        strike_b = make_strike_rule(program, name="B-40d", required_orders=3, window_days=40, bonus_points=100)
        strike_c = make_strike_rule(program, name="C-20d", required_orders=3, window_days=20, bonus_points=100)
        rule = make_consecutive_rule(
            program,
            strikes=[strike_a, strike_b, strike_c],
            required_consecutive=4,
            combine_mode="all",
            bonus_points=750,
        )

        seed_consecutive_run(sample_user.id, strike_a, count=4, spacing_days=30)
        seed_consecutive_run(sample_user.id, strike_b, count=4, spacing_days=40)
        seed_consecutive_run(sample_user.id, strike_c, count=4, spacing_days=20)

        svc = LoyaltyService()
        svc.update_consecutive_strikes(sample_user.id)

        awards = consecutive_awards(sample_user.id, rule.id)
        assert len(awards) == 1
        assert consecutive_award_total(sample_user.id, rule.id) == 750


# ---------------------------------------------------------------------------
# combine_all-12: Three strikes — one reset blocks even if two at N
# ---------------------------------------------------------------------------


def test_combine_all_12_three_strikes_one_reset_blocks(app, db, sample_user):
    """A(30d)=4, B(40d)=4, C(20d) has a skipped period → run=2.
    min(4,4,2)=2 < 4 → no award."""
    with app.app_context():
        program = get_or_create_default_program()
        strike_a = make_strike_rule(program, name="A-30d", required_orders=3, window_days=30, bonus_points=100)
        strike_b = make_strike_rule(program, name="B-40d", required_orders=3, window_days=40, bonus_points=100)
        strike_c = make_strike_rule(program, name="C-20d", required_orders=3, window_days=20, bonus_points=100)
        rule = make_consecutive_rule(
            program,
            strikes=[strike_a, strike_b, strike_c],
            required_consecutive=4,
            combine_mode="all",
            bonus_points=750,
        )

        now = datetime.now(timezone.utc)

        seed_consecutive_run(sample_user.id, strike_a, count=4, spacing_days=30)
        seed_consecutive_run(sample_user.id, strike_b, count=4, spacing_days=40)

        # C: 2 old achievements, gap of 50d (>= 2*20=40d → break), then 2 new
        seed_strike_achievement(sample_user.id, strike_c, now - timedelta(days=200))
        seed_strike_achievement(sample_user.id, strike_c, now - timedelta(days=180))
        # gap: 180 - 200 = 20d < 40d → old pair is consecutive with each other
        # New pair, gap from now-180 to now-20: 160d >= 40d → break
        seed_strike_achievement(sample_user.id, strike_c, now - timedelta(days=20))
        seed_strike_achievement(sample_user.id, strike_c, now - timedelta(days=5))
        # C's run: now-5, now-20 (gap=15d<40d → consecutive), then break at now-180 (gap=160d>=40d)
        # C run = 2

        svc = LoyaltyService()
        run_c, _ = svc._strike_consecutive_run(sample_user.id, strike_c, now)
        assert run_c == 2, f"C run should be 2, got {run_c}"

        svc.update_consecutive_strikes(sample_user.id)
        assert consecutive_award_total(sample_user.id, rule.id) == 0, "min(4,4,2)<4 should block award"


# ---------------------------------------------------------------------------
# combine_all-13: min_order_amount — sub-threshold orders don't generate achievements
# ---------------------------------------------------------------------------


def test_combine_all_13_min_order_amount_filters(app, db, sample_user, sample_product):
    """Strike A requires min_order_amount=60000. Orders with total=50000 (below) must
    NOT generate streak_bonus rows for A. Only orders with total=70000 count.

    Strategy to make A's consecutive run deterministic at exactly 3:
    1. Drive 2 sub-threshold orders → assert A has 0 achievements (min_order_amount gate).
    2. Pre-seed 2 backdated A achievements (30d and 60d ago) so A already has run=2.
    3. Pre-seed 3 qualifying DELIVERED orders (total=70000) so _qualifying_order_count=3+
       before the final real delivery fires update_streak.
    4. Drive 1 real qualifying delivery → update_streak sees >=3 qualifying orders →
       A achieves (3rd achievement now) → A_run=3.
    5. B's seeded run=3 → combined=min(3,3)=3 >= N=3 → bonus fires (500 pts)."""
    with app.app_context():
        program = get_or_create_default_program()
        strike_a = make_strike_rule(
            program,
            name="A-60k-min",
            required_orders=3,
            window_days=30,
            bonus_points=100,
            min_order_amount=Decimal("60000"),
        )
        strike_b = make_strike_rule(
            program,
            name="B-no-min",
            required_orders=3,
            window_days=30,
            bonus_points=100,
        )
        rule = make_consecutive_rule(
            program,
            strikes=[strike_a, strike_b],
            required_consecutive=3,
            combine_mode="all",
            bonus_points=500,
        )

        # Seed 3 consecutive achievements for B (spacing=30d, all within 2*30=60d threshold)
        seed_consecutive_run(sample_user.id, strike_b, count=3, spacing_days=30)

        # Use product=None path in deliver_paid_order (direct-Order, no inventory machinery).
        order_service = OrderService(inventory_service=None)

        # Step 1: Drive 2 sub-threshold orders (total=50000 < 60000).
        # These must NOT generate A achievements (min_order_amount filter).
        for _ in range(2):
            deliver_paid_order(
                order_service,
                sample_user.id,
                total=Decimal("50000"),
                payment="prepaid",
            )

        # Assert: sub-threshold orders produced zero A achievements.
        a_count_before = strike_achievement_count(sample_user.id, strike_a.id)
        assert a_count_before == 0, (
            f"Sub-threshold orders should not generate A achievements; got {a_count_before}"
        )

        # Step 2: Pre-seed 2 backdated A achievements to give A a starting run of 2.
        # Spacing 30d (gap < 2*30=60d → consecutive).
        _now = datetime.now(timezone.utc)
        seed_strike_achievement(sample_user.id, strike_a, _now - timedelta(days=60))
        seed_strike_achievement(sample_user.id, strike_a, _now - timedelta(days=30))

        # Step 3: Pre-seed 3 qualifying DELIVERED orders (total=70000 >= 60000) so that
        # _qualifying_order_count for A reaches required_orders=3 before / alongside
        # the final real delivery.  These are bare Order rows — no update_streak call.
        seed_delivered_orders(
            sample_user.id,
            count=3,
            total=Decimal("70000"),
            newest_days_ago=1,
            spacing_days=2,
        )

        # Step 4: Drive 1 real qualifying delivery (total=70000).
        # update_streak sees >=3 qualifying orders → A achieves (3rd achievement) →
        # update_consecutive_strikes → A_run=3, B_run=3 → min(3,3)=3 >= N=3 → bonus.
        deliver_paid_order(
            order_service,
            sample_user.id,
            total=Decimal("70000"),
            payment="prepaid",
        )

        # Step 5: Verify the real delivery added A's achievement and run is exactly 3.
        a_count_after = strike_achievement_count(sample_user.id, strike_a.id)
        assert a_count_after >= 1, (
            f"Real qualifying delivery should have generated at least 1 A achievement; got {a_count_after}"
        )

        svc = LoyaltyService()
        _now2 = datetime.now(timezone.utc)
        run_a, _ = svc._strike_consecutive_run(sample_user.id, strike_a, _now2)
        assert run_a == 3, (
            f"A's consecutive run must be exactly 3 (2 backdated + 1 real); got {run_a}"
        )

        # Bonus must have fired inside update_streak on the real delivery.
        total_pts = consecutive_award_total(sample_user.id, rule.id)
        assert total_pts == 500, (
            f"min(A_run=3, B_run=3)=3 >= N=3 → expected 500 pts bonus, got {total_pts}"
        )


# ---------------------------------------------------------------------------
# combine_all-14: Inactive rule is skipped even when both strikes reach N
# ---------------------------------------------------------------------------


def test_combine_all_14_inactive_rule_skipped(app, db, sample_user):
    """is_active=False rule is excluded by the filter_by(is_active=True) query.
    Both A=6 and B=6 but no bonus row issued."""
    with app.app_context():
        program = get_or_create_default_program()
        strike_a = make_strike_rule(program, name="A-30d", required_orders=3, window_days=30, bonus_points=100)
        strike_b = make_strike_rule(program, name="B-30d", required_orders=3, window_days=30, bonus_points=100)
        rule = make_consecutive_rule(
            program,
            strikes=[strike_a, strike_b],
            required_consecutive=6,
            combine_mode="all",
            bonus_points=1000,
            is_active=False,  # explicitly inactive
        )

        seed_consecutive_run(sample_user.id, strike_a, count=6, spacing_days=30)
        seed_consecutive_run(sample_user.id, strike_b, count=6, spacing_days=30)

        svc = LoyaltyService()
        svc.update_consecutive_strikes(sample_user.id)

        assert len(consecutive_awards(sample_user.id, rule.id)) == 0
        assert consecutive_award_total(sample_user.id, rule.id) == 0


# ---------------------------------------------------------------------------
# combine_all-15: Rule with ends_at in the past is skipped
# ---------------------------------------------------------------------------


def test_combine_all_15_expired_ends_at_skipped(app, db, sample_user):
    """Rule with ends_at = now - 1 hour is not effective. No bonus despite both
    strikes reaching N=6."""
    with app.app_context():
        program = get_or_create_default_program()
        now = datetime.now(timezone.utc)
        strike_a = make_strike_rule(program, name="A-30d", required_orders=3, window_days=30, bonus_points=100)
        strike_b = make_strike_rule(program, name="B-30d", required_orders=3, window_days=30, bonus_points=100)
        rule = make_consecutive_rule(
            program,
            strikes=[strike_a, strike_b],
            required_consecutive=6,
            combine_mode="all",
            bonus_points=1000,
            is_active=True,
            ends_at=now - timedelta(hours=1),  # expired 1 hour ago
        )

        seed_consecutive_run(sample_user.id, strike_a, count=6, spacing_days=30)
        seed_consecutive_run(sample_user.id, strike_b, count=6, spacing_days=30)

        svc = LoyaltyService()
        svc.update_consecutive_strikes(sample_user.id)

        assert len(consecutive_awards(sample_user.id, rule.id)) == 0, (
            "Expired rule (ends_at in past) should not fire even when strikes reach N"
        )
        assert consecutive_award_total(sample_user.id, rule.id) == 0


# ---------------------------------------------------------------------------
# combine_all-16: Full real-flow trigger — delivered+paid order fires bonus
# ---------------------------------------------------------------------------


def test_combine_all_16_real_flow_end_to_end(app, db, sample_user, sample_product):
    """End-to-end: N=1 required. Both strikes have required_orders=1 so a single
    qualifying delivery satisfies each. Pre-seed one qualifying delivered order per
    strike window so _qualifying_order_count=1 is guaranteed before the real delivery.
    Then drive one real delivery through _handle_status_change_actions:

      update_streak → both A and B awarded (no prior cooldown, count >= required_orders=1)
      → update_consecutive_strikes → min(1,1)=1 >= N=1 → 1 BONUS row (300 pts)

    Both assertions (a_count==1, b_count==1, total==300) are unconditional."""
    with app.app_context():
        program = get_or_create_default_program()

        # Strikes: required_orders=1 — any single qualifying delivery completes the strike.
        strike_a = make_strike_rule(
            program, name="A-1in30", required_orders=1, window_days=30, bonus_points=50
        )
        strike_b = make_strike_rule(
            program, name="B-1in30", required_orders=1, window_days=30, bonus_points=50
        )
        rule = make_consecutive_rule(
            program,
            strikes=[strike_a, strike_b],
            required_consecutive=1,  # N=1: one achievement suffices
            combine_mode="all",
            bonus_points=300,
        )

        # Pre-seed one qualifying DELIVERED order per strike window (bare Order rows,
        # no update_streak called) so _qualifying_order_count >= required_orders=1
        # even before the real delivery, making the trigger deterministic.
        seed_delivered_orders(
            sample_user.id,
            count=1,
            total=Decimal("25000"),
            newest_days_ago=5,
        )

        # Use product=None path in deliver_paid_order (direct-Order, no inventory needed).
        order_service = OrderService(inventory_service=None)

        # Drive ONE real delivered+paid order.
        # _handle_status_change_actions → update_streak → both A and B fire
        # (no prior achievement → no cooldown; _qualifying_order_count >= 1) →
        # update_consecutive_strikes → min(1,1)=1 >= N=1 → 1 BONUS row.
        deliver_paid_order(
            order_service,
            sample_user.id,
            total=Decimal("25000"),
            payment="prepaid",
        )

        # Both strikes must have exactly 1 achievement (from the real delivery).
        a_count = strike_achievement_count(sample_user.id, strike_a.id)
        b_count = strike_achievement_count(sample_user.id, strike_b.id)
        assert a_count == 1, f"Expected A achievement count=1, got {a_count}"
        assert b_count == 1, f"Expected B achievement count=1, got {b_count}"

        # Consecutive bonus must have fired.
        awards = consecutive_awards(sample_user.id, rule.id)
        total_pts = consecutive_award_total(sample_user.id, rule.id)
        assert len(awards) == 1, (
            f"Expected 1 CONSECUTIVE_STREAK_BONUS row after E2E trigger; got {len(awards)}"
        )
        assert total_pts == 300, f"Expected 300 pts; got {total_pts}"


# ---------------------------------------------------------------------------
# combine_all-17: No program present — update_consecutive_strikes returns False
# ---------------------------------------------------------------------------


def test_combine_all_17_no_program_returns_false(app, db, sample_user):
    """No LoyaltyProgram in DB. update_consecutive_strikes should return False
    without raising exceptions or creating any ledger rows."""
    with app.app_context():
        # Do NOT call get_or_create_default_program — we want NO program.
        svc = LoyaltyService()
        result = svc.update_consecutive_strikes(sample_user.id)
        assert result is False, "Should return False when no program exists"

        all_txns = LoyaltyTransaction.query.filter_by(user_id=sample_user.id).all()
        assert len(all_txns) == 0, "No ledger rows should be created when no program exists"


# ---------------------------------------------------------------------------
# combine_all-18: Rule with zero attached strikes is skipped
# ---------------------------------------------------------------------------


def test_combine_all_18_zero_strikes_skipped(app, db, sample_user):
    """A LoyaltyConsecutiveStrikeRule with no strikes (rule.strikes=[]) is skipped
    by the 'not rule.strikes' guard. No bonus row."""
    with app.app_context():
        program = get_or_create_default_program()
        # Create rule with no strikes attached
        rule = LoyaltyConsecutiveStrikeRule(
            program_id=program.id,
            name="No-strike rule",
            required_consecutive=6,
            combine_mode="all",
            bonus_points=1000,
            is_active=True,
        )
        rule.strikes = []  # explicitly empty
        _db.session.add(rule)
        _db.session.commit()

        svc = LoyaltyService()
        svc.update_consecutive_strikes(sample_user.id)

        assert len(consecutive_awards(sample_user.id, rule.id)) == 0
        assert consecutive_award_total(sample_user.id, rule.id) == 0


# ---------------------------------------------------------------------------
# combine_all-19: Repeat-every-N: 2N combined issues exactly 2 bonuses in one call
# ---------------------------------------------------------------------------


def test_combine_all_19_two_milestones_in_one_call(app, db, sample_user):
    """A and B each seeded with 12 consecutive achievements (2N). No prior bonus rows.
    One call to update_consecutive_strikes: min(12,12)=12, target=12//6=2, already=0.
    Loop issues milestone=1 and milestone=2. Expect 2 rows, 2000 pts."""
    with app.app_context():
        program = get_or_create_default_program()
        strike_a = make_strike_rule(program, name="A-30d", required_orders=3, window_days=30, bonus_points=100)
        strike_b = make_strike_rule(program, name="B-40d", required_orders=3, window_days=40, bonus_points=100)
        rule = make_consecutive_rule(
            program,
            strikes=[strike_a, strike_b],
            required_consecutive=6,
            combine_mode="all",
            bonus_points=1000,
        )

        # 12 consecutive achievements for each (no prior bonus rows)
        seed_consecutive_run(sample_user.id, strike_a, count=12, spacing_days=30)
        seed_consecutive_run(sample_user.id, strike_b, count=12, spacing_days=40)

        svc = LoyaltyService()
        svc.update_consecutive_strikes(sample_user.id)

        awards = consecutive_awards(sample_user.id, rule.id)
        assert len(awards) == 2, (
            f"Expected 2 bonus rows (milestones 1 and 2 in one call), got {len(awards)}"
        )
        milestones = sorted(a.extra_data.get("milestone") for a in awards)
        assert milestones == [1, 2], f"Expected milestones [1,2], got {milestones}"
        assert consecutive_award_total(sample_user.id, rule.id) == 2000


# ---------------------------------------------------------------------------
# combine_all-20: Progress API — combined_current capped at N even when run > N
# ---------------------------------------------------------------------------


def test_combine_all_20_progress_capped_at_n(app, db, sample_user):
    """A(30d) run=8, B(40d) run=8. N=6. get_consecutive_strike_progress should
    return combined_current=6 (capped), per_strike each showing current=6, target=6,
    active=True (latest achievement < 2*W ago)."""
    with app.app_context():
        program = get_or_create_default_program()
        strike_a = make_strike_rule(program, name="A-30d", required_orders=3, window_days=30, bonus_points=100)
        strike_b = make_strike_rule(program, name="B-40d", required_orders=3, window_days=40, bonus_points=100)
        rule = make_consecutive_rule(
            program,
            strikes=[strike_a, strike_b],
            required_consecutive=6,
            combine_mode="all",
            bonus_points=1000,
        )

        # A: 8 achievements (spacing=30d, newest at 30d ago < 2*30=60d → active=True)
        seed_consecutive_run(sample_user.id, strike_a, count=8, spacing_days=30)
        # B: 8 achievements (spacing=40d, newest at 40d ago < 2*40=80d → active=True)
        seed_consecutive_run(sample_user.id, strike_b, count=8, spacing_days=40)

        svc = LoyaltyService()
        progress = svc.get_consecutive_strike_progress(sample_user.id)

        assert len(progress) == 1, f"Expected 1 rule in progress, got {len(progress)}"
        p = progress[0]

        # combined_current must be capped at N=6 (spec §6.1)
        assert p["combined_current"] == 6, (
            f"combined_current should be capped at N=6, got {p['combined_current']}"
        )
        assert p["required_consecutive"] == 6

        # per_strike: each should show current=6 (capped), target=6, active=True
        per_strike = {ps["strike_name"]: ps for ps in p["per_strike"]}
        assert "A-30d" in per_strike, f"Expected A-30d in per_strike, got {list(per_strike.keys())}"
        assert "B-40d" in per_strike, f"Expected B-40d in per_strike, got {list(per_strike.keys())}"

        a_prog = per_strike["A-30d"]
        assert a_prog["current"] == 6, f"A current should be capped at 6, got {a_prog['current']}"
        assert a_prog["target"] == 6
        assert a_prog["active"] is True, "A should be active (latest < 2*30=60d ago)"

        b_prog = per_strike["B-40d"]
        assert b_prog["current"] == 6, f"B current should be capped at 6, got {b_prog['current']}"
        assert b_prog["target"] == 6
        assert b_prog["active"] is True, "B should be active (latest < 2*40=80d ago)"


# ---------------------------------------------------------------------------
# combine_all-21: all-mode — min_order_amount strike lapses while other runs on
# ---------------------------------------------------------------------------


def test_combine_all_21_min_amount_strike_lapsed_blocks_award(app, db, sample_user):
    """all-mode: strike A has min_order_amount=80000 ('big-order streak');
    strike B has no min amount ('regular streak'). B keeps running (5 consecutive
    at 30d spacing). A had 2 consecutive achievements, then a gap of 145d >=
    2*30=60d which breaks A's run, followed by 1 recent achievement.

    After the lapse:  A_run=1, B_run=5
    combined = min(A_run=1, B_run=5) = 1 < N=3 → NO award.

    This is the realistic 'big-order streak lapsed' scenario: the customer earned
    regular B points every month but missed the big-order threshold for two+ months,
    resetting the premium A streak and blocking the consecutive bonus."""
    with app.app_context():
        program = get_or_create_default_program()
        strike_a = make_strike_rule(
            program,
            name="A-bigorder",
            required_orders=3,
            window_days=30,
            bonus_points=200,
            min_order_amount=Decimal("80000"),
        )
        strike_b = make_strike_rule(
            program,
            name="B-regular",
            required_orders=3,
            window_days=30,
            bonus_points=100,
        )
        rule = make_consecutive_rule(
            program,
            strikes=[strike_a, strike_b],
            required_consecutive=3,
            combine_mode="all",
            bonus_points=800,
        )

        _now = datetime.now(timezone.utc)

        # Strike B: 5 consecutive achievements (spacing=30d, newest at now-30d).
        # All gaps 30d < 2*30=60d → B_run = 5.
        seed_consecutive_run(sample_user.id, strike_b, count=5, spacing_days=30)

        # Strike A history:
        #   now-200d: old achievement #1
        #   now-170d: old achievement #2  (gap=30d < 60d → consecutive with #1)
        #   --- 145-day lapse ---  (gap from now-170d to now-25d = 145d >= 60d → BREAK)
        #   now-25d:  new achievement #1 (post-lapse; isolated → A_run = 1)
        seed_strike_achievement(sample_user.id, strike_a, _now - timedelta(days=200))
        seed_strike_achievement(sample_user.id, strike_a, _now - timedelta(days=170))
        seed_strike_achievement(sample_user.id, strike_a, _now - timedelta(days=25))

        svc = LoyaltyService()
        _now2 = datetime.now(timezone.utc)
        run_a, _ = svc._strike_consecutive_run(sample_user.id, strike_a, _now2)
        run_b, _ = svc._strike_consecutive_run(sample_user.id, strike_b, _now2)

        # Hard-assert the run lengths before evaluating the rule.
        assert run_a == 1, (
            f"A's run should be 1 after the 145d lapse (>= 2*30=60d reset threshold), got {run_a}"
        )
        assert run_b == 5, f"B's run should be 5, got {run_b}"

        svc.update_consecutive_strikes(sample_user.id)

        # combined = min(1, 5) = 1 < N=3 → no award.
        assert consecutive_award_total(sample_user.id, rule.id) == 0, (
            "combined=min(A_run=1, B_run=5)=1 < N=3: lapsed big-order streak must block the award"
        )
        assert len(consecutive_awards(sample_user.id, rule.id)) == 0, (
            "No CONSECUTIVE_STREAK_BONUS rows expected when combined count < N"
        )
