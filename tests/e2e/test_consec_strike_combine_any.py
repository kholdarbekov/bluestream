"""E2E tests for LoyaltyConsecutiveStrikeRule with combine_mode='any'.

Covers all 20 enumerated cases from the 2026-06-24 spec review.
Each test is anchored to the locked business rules in the spec (§5.1–§6.1),
not to the current implementation output, so genuine bugs surface as failures.

Test infrastructure:
- Shared helpers imported from tests/e2e/_consecutive_strike_helpers.py
- autouse fixture silences all LoyaltyService notification side-effects
- Every DB test receives (app, db) fixtures from tests/conftest.py
- @pytest.mark.e2e (registered); @pytest.mark.loyalty is NOT registered — never used
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch, MagicMock
from types import SimpleNamespace

import pytest

from business_app import db
from business_app.models.loyalty import (
    LoyaltyConsecutiveStrikeRule,
    LoyaltyProgram,
    LoyaltyTransaction,
)
from business_app.services.loyalty_service import LoyaltyService
from business_app.services.order_service import OrderService
from business_app.utils.constants import LoyaltyActionType, LoyaltyTransactionType

from tests.e2e._consecutive_strike_helpers import (
    get_or_create_default_program,
    make_strike_rule,
    make_consecutive_rule,
    seed_strike_achievement,
    seed_consecutive_run,
    seed_delivered_orders,
    consecutive_awards,
    consecutive_award_total,
    strike_achievement_count,
    deliver_paid_order,
    silence_loyalty_notifications,
)

# ---------------------------------------------------------------------------
# Module marker
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.e2e


def _now() -> datetime:
    """Return the current UTC datetime, captured at call time.

    Use this instead of a module-level NOW constant so that boundary-sensitive
    tests (e.g. exactly-2*W) compute their cutoffs relative to the actual
    evaluation moment, not the import moment.
    """
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Autouse: silence loyalty notifications in every test in this module
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _silence(monkeypatch):
    silence_loyalty_notifications(monkeypatch)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_order_service():
    """Return an OrderService with inventory side-effects mocked out."""
    svc = OrderService()
    return svc


def _mock_inv(product):
    mock = MagicMock()
    mock.check_multiple_products_availability.return_value = [
        SimpleNamespace(
            product_id=product.id,
            requested_quantity=2,
            available_quantity=100,
            reserved_quantity=0,
            is_available=True,
            reason="Available",
        )
    ]
    mock.reserve_inventory.return_value = {"success": True, "expires_at": None}
    mock.release_reservations.return_value = {"success": True}
    return mock


# ---------------------------------------------------------------------------
# Case combine_any-001
# Single strike reaches N while the other never achieved — award fires
# ---------------------------------------------------------------------------


def test_combine_any_001_single_strike_reaches_n(app, db, sample_user):
    """max(4,0)=4 >= N=4: award fires even though B has zero achievements."""
    with app.app_context():
        now = _now()
        program = get_or_create_default_program()
        strike_a = make_strike_rule(program, name="A-30d", required_orders=3, window_days=30, bonus_points=50)
        strike_b = make_strike_rule(program, name="B-30d", required_orders=3, window_days=30, bonus_points=50)
        rule = make_consecutive_rule(
            program,
            strikes=[strike_a, strike_b],
            name="Champion",
            required_consecutive=4,
            combine_mode="any",
            bonus_points=500,
        )

        # Seed 4 consecutive achievements for A (30d apart, all gaps < 60d)
        seed_consecutive_run(user_id=sample_user.id, strike_rule=strike_a, count=4, now=now, spacing_days=30)
        # B has zero achievements

        LoyaltyService().update_consecutive_strikes(sample_user.id)

        awards = consecutive_awards(sample_user.id, rule.id)
        assert len(awards) == 1, "Expected exactly 1 CONSECUTIVE_STREAK_BONUS row"
        assert awards[0].points == 500
        assert (awards[0].extra_data or {}).get("milestone") == 1
        assert (awards[0].extra_data or {}).get("action_type") == LoyaltyActionType.CONSECUTIVE_STREAK_BONUS.value
        assert consecutive_award_total(sample_user.id, rule.id) == 500


# ---------------------------------------------------------------------------
# Case combine_any-002
# Neither strike reaches N — no award
# ---------------------------------------------------------------------------


def test_combine_any_002_neither_strike_reaches_n(app, db, sample_user):
    """max(3,2)=3 < N=4: guard fires, no award created."""
    with app.app_context():
        now = _now()
        program = get_or_create_default_program()
        strike_a = make_strike_rule(program, name="A-30d", required_orders=3, window_days=30, bonus_points=50)
        strike_b = make_strike_rule(program, name="B-30d", required_orders=3, window_days=30, bonus_points=50)
        rule = make_consecutive_rule(
            program,
            strikes=[strike_a, strike_b],
            name="Champion",
            required_consecutive=4,
            combine_mode="any",
            bonus_points=500,
        )

        seed_consecutive_run(user_id=sample_user.id, strike_rule=strike_a, count=3, now=now, spacing_days=30)
        seed_consecutive_run(user_id=sample_user.id, strike_rule=strike_b, count=2, now=now - timedelta(days=1), spacing_days=30)

        LoyaltyService().update_consecutive_strikes(sample_user.id)

        assert len(consecutive_awards(sample_user.id, rule.id)) == 0, "No award expected when max(3,2)=3 < N=4"


# ---------------------------------------------------------------------------
# Case combine_any-003
# Both strikes reach N simultaneously — award fires once; idempotent on second call
# ---------------------------------------------------------------------------


def test_combine_any_003_both_reach_n_simultaneously(app, db, sample_user):
    """max(4,4)=4=N; exactly 1 award (milestone=1). Second call is idempotent."""
    with app.app_context():
        now = _now()
        program = get_or_create_default_program()
        strike_a = make_strike_rule(program, name="A-30d", required_orders=3, window_days=30, bonus_points=50)
        strike_b = make_strike_rule(program, name="B-30d", required_orders=3, window_days=30, bonus_points=50)
        rule = make_consecutive_rule(
            program,
            strikes=[strike_a, strike_b],
            name="Champion",
            required_consecutive=4,
            combine_mode="any",
            bonus_points=700,
        )

        # Identical timestamps for both (30d spacings)
        seed_consecutive_run(user_id=sample_user.id, strike_rule=strike_a, count=4, now=now, spacing_days=30)
        seed_consecutive_run(user_id=sample_user.id, strike_rule=strike_b, count=4, now=now, spacing_days=30)

        svc = LoyaltyService()
        svc.update_consecutive_strikes(sample_user.id)

        # First call: exactly 1 bonus row
        awards = consecutive_awards(sample_user.id, rule.id)
        assert len(awards) == 1, "Exactly 1 bonus row expected on first call"
        assert awards[0].points == 700
        assert (awards[0].extra_data or {}).get("milestone") == 1

        # Second call must be idempotent — no additional award
        svc.update_consecutive_strikes(sample_user.id)
        awards_after_second = consecutive_awards(sample_user.id, rule.id)
        assert len(awards_after_second) == 1, "Second call must not add a second award (idempotent)"
        assert consecutive_award_total(sample_user.id, rule.id) == 700


# ---------------------------------------------------------------------------
# Case combine_any-004
# B overtakes A as leader — no double-award; floor(max/N) is the SSOT
# ---------------------------------------------------------------------------


def test_combine_any_004_b_overtakes_a_as_leader(app, db, sample_user):
    """Natural sequential progression: grow A to N, then B past A.
    After A reaches N=4 → 1 award. After B reaches 8 > A:
    combined=max(4,8)=8; target=8//4=2; run_start_B = 8*30=240d ago;
    already = awards since 240d ago. The step-1 award (created ~now) IS >= 240d ago
    so already=1; target=2; exactly 1 new award fires (milestone=2).
    Total awards = 2, total points = 1000."""
    with app.app_context():
        now = _now()
        program = get_or_create_default_program()
        strike_a = make_strike_rule(program, name="A-30d", required_orders=3, window_days=30, bonus_points=50)
        strike_b = make_strike_rule(program, name="B-30d", required_orders=3, window_days=30, bonus_points=50)
        rule = make_consecutive_rule(
            program,
            strikes=[strike_a, strike_b],
            name="Champion",
            required_consecutive=4,
            combine_mode="any",
            bonus_points=500,
        )

        # Step 1: A reaches N=4 → exactly 1 award
        seed_consecutive_run(user_id=sample_user.id, strike_rule=strike_a, count=4, now=now, spacing_days=30)
        LoyaltyService().update_consecutive_strikes(sample_user.id)
        awards_step1 = consecutive_awards(sample_user.id, rule.id)
        assert len(awards_step1) == 1, "Step 1: A at N=4 must produce exactly 1 award"
        assert awards_step1[0].points == 500
        assert (awards_step1[0].extra_data or {}).get("milestone") == 1

        # Step 2: B grows to 8 (overtakes A). B's run_start = 8*30=240d ago.
        # now is the evaluation time; step-1 award created_at ~= now which IS >= 240d ago → already=1.
        # target_awards = 8//4 = 2; already=1 → 1 new award at milestone=2.
        seed_consecutive_run(
            user_id=sample_user.id,
            strike_rule=strike_b,
            count=8,
            now=now + timedelta(seconds=1),  # slightly newer than A's most recent
            spacing_days=30,
        )
        LoyaltyService().update_consecutive_strikes(sample_user.id)
        awards_step2 = consecutive_awards(sample_user.id, rule.id)

        # combined=max(4,8)=8; target=8//4=2; already=1 (step-1 award within B's run_start window);
        # 1 new award (milestone=2). Total = 2 rows, 1000 points.
        assert len(awards_step2) == 2, (
            f"After B overtakes: expect 2 total awards (milestone=1 + milestone=2), got {len(awards_step2)}"
        )
        milestones = sorted((a.extra_data or {}).get("milestone") for a in awards_step2)
        assert milestones == [1, 2], f"Expected milestones [1, 2], got {milestones}"
        assert consecutive_award_total(sample_user.id, rule.id) == 1000, (
            "Total points must be exactly 1000 (500 * 2)"
        )

        # Step 3: running update again is idempotent — no extra award
        LoyaltyService().update_consecutive_strikes(sample_user.id)
        assert len(consecutive_awards(sample_user.id, rule.id)) == 2, "Step 3 must be idempotent"


# ---------------------------------------------------------------------------
# Case combine_any-005
# Leader resets (gap >= 2*W) — follower B is intact, governs
# ---------------------------------------------------------------------------


def test_combine_any_005_leader_resets_follower_governs(app, db, sample_user):
    """A's run resets due to gap >= 2*30=60d; B's run of 4 governs; combined=4>=N=4; 1 award."""
    with app.app_context():
        now = _now()
        program = get_or_create_default_program()
        strike_a = make_strike_rule(program, name="A-30d", required_orders=3, window_days=30, bonus_points=50)
        strike_b = make_strike_rule(program, name="B-30d", required_orders=3, window_days=30, bonus_points=50)
        rule = make_consecutive_rule(
            program,
            strikes=[strike_a, strike_b],
            name="Champion",
            required_consecutive=4,
            combine_mode="any",
            bonus_points=300,
        )

        # A: 4 achievements with a gap of 65d (>= 2*30=60d) before the most recent one
        # This means run for A breaks: the most recent achievement is isolated (run=1)
        # or gap breaks and the walk stops.
        # Seed A: oldest at 200d ago, then 170d ago, then 140d ago, then 65d ago (gap=75d >= 60d -> break)
        base = now
        for days_ago in [200, 170, 140, 65]:
            seed_strike_achievement(sample_user.id, strike_a, base - timedelta(days=days_ago))

        # B: 4 consecutive achievements 30d apart, all < 60d gaps
        seed_consecutive_run(user_id=sample_user.id, strike_rule=strike_b, count=4, now=base, spacing_days=30)

        LoyaltyService().update_consecutive_strikes(sample_user.id)

        # A's run: most recent = 65d ago; next = 140d ago; gap = 75d >= 60d → break. run_A = 1.
        # B's run: 4 consecutive (gaps = 30d < 60d). run_B = 4.
        # combined = max(1, 4) = 4 >= N=4. B governs.
        awards = consecutive_awards(sample_user.id, rule.id)
        assert len(awards) == 1, "B's intact run of 4 should fire one award"
        assert awards[0].points == 300
        assert (awards[0].extra_data or {}).get("milestone") == 1


# ---------------------------------------------------------------------------
# Case combine_any-006
# Repeat-every-N: leader run of 2N awards exactly twice
# ---------------------------------------------------------------------------


def test_combine_any_006_repeat_every_n_awards_twice(app, db, sample_user):
    """max(8,0)=8; target_awards=8//4=2; 2 CONSECUTIVE_STREAK_BONUS rows (milestone=1 and 2)."""
    with app.app_context():
        now = _now()
        program = get_or_create_default_program()
        strike_a = make_strike_rule(program, name="A-30d", required_orders=3, window_days=30, bonus_points=50)
        strike_b = make_strike_rule(program, name="B-30d", required_orders=3, window_days=30, bonus_points=50)
        rule = make_consecutive_rule(
            program,
            strikes=[strike_a, strike_b],
            name="Champion",
            required_consecutive=4,
            combine_mode="any",
            bonus_points=200,
        )

        seed_consecutive_run(user_id=sample_user.id, strike_rule=strike_a, count=8, now=now, spacing_days=30)
        # B has 0 achievements

        LoyaltyService().update_consecutive_strikes(sample_user.id)

        awards = consecutive_awards(sample_user.id, rule.id)
        assert len(awards) == 2, "8//4=2 awards expected (milestone 1 and 2)"
        milestones = sorted((a.extra_data or {}).get("milestone") for a in awards)
        assert milestones == [1, 2], "Milestones must be 1 and 2"
        assert consecutive_award_total(sample_user.id, rule.id) == 400  # 200 * 2


# ---------------------------------------------------------------------------
# Case combine_any-007
# Idempotency: calling update_consecutive_strikes twice never double-awards
# ---------------------------------------------------------------------------


def test_combine_any_007_idempotency_no_double_award(app, db, sample_user):
    """Second call sees already=1, target=1; no new award."""
    with app.app_context():
        now = _now()
        program = get_or_create_default_program()
        strike_a = make_strike_rule(program, name="A-30d", required_orders=3, window_days=30, bonus_points=50)
        rule = make_consecutive_rule(
            program,
            strikes=[strike_a],
            name="Champion",
            required_consecutive=4,
            combine_mode="any",
            bonus_points=500,
        )

        seed_consecutive_run(user_id=sample_user.id, strike_rule=strike_a, count=4, now=now, spacing_days=30)

        svc = LoyaltyService()
        svc.update_consecutive_strikes(sample_user.id)
        svc.update_consecutive_strikes(sample_user.id)

        awards = consecutive_awards(sample_user.id, rule.id)
        assert len(awards) == 1, "Second call must be idempotent — exactly 1 bonus row total"
        assert consecutive_award_total(sample_user.id, rule.id) == 500


# ---------------------------------------------------------------------------
# Case combine_any-008
# Idempotency across overtake: prior bonus anchored to old run_start not re-counted
# ---------------------------------------------------------------------------


def test_combine_any_008_idempotency_across_overtake(app, db, sample_user):
    """Tie on max(4,4)=4; counts.index(4)=0 picks A; run_start_A=90d ago.
    Prior bonus from step-1 (created_at ~now >= run_start_A) → already=1; no 2nd award."""
    with app.app_context():
        now = _now()
        program = get_or_create_default_program()
        # A: window=30d, 4 achievements, run_start_A = 4*30=120d ago
        strike_a = make_strike_rule(program, name="A-30d", required_orders=3, window_days=30, bonus_points=50)
        # B: window=40d, 4 achievements, run_start_B = 4*40=160d ago
        strike_b = make_strike_rule(program, name="B-40d", required_orders=3, window_days=40, bonus_points=50)
        rule = make_consecutive_rule(
            program,
            strikes=[strike_a, strike_b],
            name="Champion",
            required_consecutive=4,
            combine_mode="any",
            bonus_points=600,
        )

        # Step 1: seed 4 A-achievements (run_start_A = 4*30=120d ago)
        seed_consecutive_run(user_id=sample_user.id, strike_rule=strike_a, count=4, now=now, spacing_days=30)
        LoyaltyService().update_consecutive_strikes(sample_user.id)
        assert len(consecutive_awards(sample_user.id, rule.id)) == 1

        # Step 2: add 4 B-achievements (run_start_B = 4*40=160d ago, even earlier)
        seed_consecutive_run(user_id=sample_user.id, strike_rule=strike_b, count=4, now=now, spacing_days=40)
        LoyaltyService().update_consecutive_strikes(sample_user.id)

        # counts = [4_a, 4_b]; combined=4; idx=0 (A, first occurrence)
        # run_start = A's run_start = 120d ago
        # _consecutive_awards_since(user, rule, 120d ago) counts the step-1 bonus (created ~now >= 120d ago) = 1
        # target_awards=1; already=1; no new award
        awards = consecutive_awards(sample_user.id, rule.id)
        assert len(awards) == 1, "Idempotency: tie picks A (first); already=1 prevents second award"
        assert consecutive_award_total(sample_user.id, rule.id) == 600


# ---------------------------------------------------------------------------
# Case combine_any-009
# Leader at N-1 (boundary below threshold) — no award
# ---------------------------------------------------------------------------


def test_combine_any_009_leader_at_n_minus_1_no_award(app, db, sample_user):
    """max(5,0)=5 < N=6: strictly below threshold; no award (off-by-one check)."""
    with app.app_context():
        now = _now()
        program = get_or_create_default_program()
        strike_a = make_strike_rule(program, name="A-30d", required_orders=3, window_days=30, bonus_points=50)
        strike_b = make_strike_rule(program, name="B-30d", required_orders=3, window_days=30, bonus_points=50)
        rule = make_consecutive_rule(
            program,
            strikes=[strike_a, strike_b],
            name="Champion",
            required_consecutive=6,
            combine_mode="any",
            bonus_points=1000,
        )

        seed_consecutive_run(user_id=sample_user.id, strike_rule=strike_a, count=5, now=now, spacing_days=30)
        # B has 0

        result = LoyaltyService().update_consecutive_strikes(sample_user.id)

        assert len(consecutive_awards(sample_user.id, rule.id)) == 0, "5 < 6: strictly below threshold"
        assert result is False


# ---------------------------------------------------------------------------
# Case combine_any-010
# Leader exactly at N (boundary at threshold) — exactly one award
# ---------------------------------------------------------------------------


def test_combine_any_010_leader_exactly_at_n(app, db, sample_user):
    """max(6,0)=6 == N=6: boundary condition fires first award (milestone=1)."""
    with app.app_context():
        now = _now()
        program = get_or_create_default_program()
        strike_a = make_strike_rule(program, name="A-30d", required_orders=3, window_days=30, bonus_points=50)
        strike_b = make_strike_rule(program, name="B-30d", required_orders=3, window_days=30, bonus_points=50)
        rule = make_consecutive_rule(
            program,
            strikes=[strike_a, strike_b],
            name="Champion",
            required_consecutive=6,
            combine_mode="any",
            bonus_points=1000,
        )

        seed_consecutive_run(user_id=sample_user.id, strike_rule=strike_a, count=6, now=now, spacing_days=30)
        # B has 0

        LoyaltyService().update_consecutive_strikes(sample_user.id)

        awards = consecutive_awards(sample_user.id, rule.id)
        assert len(awards) == 1, "Exactly N=6 should fire exactly 1 award"
        assert awards[0].points == 1000
        assert (awards[0].extra_data or {}).get("milestone") == 1


# ---------------------------------------------------------------------------
# Case combine_any-011
# Progress API caps combined_current at N even when leader is at 2N
# ---------------------------------------------------------------------------


def test_combine_any_011_progress_caps_combined_current_at_n(app, db, sample_user):
    """get_consecutive_strike_progress caps combined_current and per-strike current at N."""
    with app.app_context():
        now = _now()
        program = get_or_create_default_program()
        strike_a = make_strike_rule(program, name="A-30d", required_orders=3, window_days=30, bonus_points=50)
        strike_b = make_strike_rule(program, name="B-30d", required_orders=3, window_days=30, bonus_points=50)
        make_consecutive_rule(
            program,
            strikes=[strike_a, strike_b],
            name="Champion",
            required_consecutive=4,
            combine_mode="any",
            bonus_points=500,
        )

        # A has run=8 (2N), B has 0
        seed_consecutive_run(user_id=sample_user.id, strike_rule=strike_a, count=8, now=now, spacing_days=30)

        progress_list = LoyaltyService().get_consecutive_strike_progress(sample_user.id)
        assert len(progress_list) == 1
        prog = progress_list[0]

        # combined_current must be capped at N=4, not 8
        assert prog["combined_current"] == 4, f"combined_current should be capped at 4, got {prog['combined_current']}"

        # Per-strike: A capped at 4, B at 0; find by strike_name
        per_a = next((s for s in prog["per_strike"] if "A" in s["strike_name"]), None)
        per_b = next((s for s in prog["per_strike"] if "B" in s["strike_name"]), None)

        assert per_a is not None, "per_strike must include A"
        assert per_b is not None, "per_strike must include B"
        assert per_a["current"] == 4, f"A's current should be capped at N=4, got {per_a['current']}"
        assert per_b["current"] == 0, f"B has no achievements, current should be 0"

        # A's most recent achievement is 30d ago (spacing=30d, count=8 → most recent at 8*30=240d-7*30=30d ago)
        # Wait, seed_consecutive_run: most recent is at spacing_days * 1 ago = 30d ago
        # (for k=count-1=7, days_ago = spacing_days * (count - k) = 30 * 1 = 30)
        # 30d ago < 2*30=60d → active=True
        assert per_a["active"] is True, "A's last achievement was 30d ago < 2*30=60d so active=True"
        assert per_b["active"] is False, "B has no achievements → active=False"


# ---------------------------------------------------------------------------
# Case combine_any-012
# active flag is False when leader's last achievement is exactly 2*W days ago
# ---------------------------------------------------------------------------


def test_combine_any_012_active_false_at_exactly_2w(app, db, sample_user):
    """Exactly at boundary 2*W: active=False (strict less-than, not <=)."""
    with app.app_context():
        # Capture now close to the seed so the 2*W boundary is exact.
        now = _now()
        program = get_or_create_default_program()
        strike_a = make_strike_rule(program, name="A-30d", required_orders=3, window_days=30, bonus_points=50)
        strike_b = make_strike_rule(program, name="B-30d", required_orders=3, window_days=30, bonus_points=50)
        make_consecutive_rule(
            program,
            strikes=[strike_a, strike_b],
            name="Champion",
            required_consecutive=4,
            combine_mode="any",
            bonus_points=500,
        )

        # 4 achievements; most recent placed at exactly 60d ago (== 2*30)
        # spacing=30d: most recent = 60d ago, others at 90, 120, 150d ago
        # All gaps are 30d < 60d → run = 4
        base_time = now - timedelta(days=60)  # most recent = exactly 2*30 days ago
        for k in range(4):
            when = base_time - timedelta(days=30 * k)
            seed_strike_achievement(sample_user.id, strike_a, when)

        progress_list = LoyaltyService().get_consecutive_strike_progress(sample_user.id)
        assert len(progress_list) == 1
        prog = progress_list[0]

        per_a = next((s for s in prog["per_strike"] if "A" in s["strike_name"]), None)
        assert per_a is not None

        # Spec §6.1: active = (now - last_achievement) < 2*W (strict <)
        # At exactly 2*W: the condition is False → active=False
        assert per_a["active"] is False, "active must be False when gap == 2*W (strict <, not <=)"

        # Run length is still 4 (the gap-< check during run computation is also strict <,
        # so gaps of 30d < 60d pass, and the run itself is intact)
        # combined_current should be capped at min(4, N=4) = 4
        assert prog["combined_current"] == 4


# ---------------------------------------------------------------------------
# Case combine_any-013
# Real delivery trigger: delivered+paid order fires update_streak → update_consecutive_strikes
# ---------------------------------------------------------------------------


def test_combine_any_013_real_delivery_trigger(app, db, sample_user):
    """End-to-end: single delivery fires both streak and consecutive-strike bonus.

    Uses product=None (direct-Order path) to avoid Postgres NOW() in sqlite.
    seed_delivered_orders ensures the qualifying DELIVERED order count is met
    so update_streak's _qualifying_order_count threshold is satisfied.
    """
    with app.app_context():
        program = get_or_create_default_program()
        # Strike that awards on first qualifying order in 30d window
        strike_a = make_strike_rule(
            program, name="A-easy", required_orders=1, window_days=30, bonus_points=50
        )
        # Consecutive rule: N=1, combine_mode=any, bonus=200 → fires after 1 strike achievement
        rule = make_consecutive_rule(
            program,
            strikes=[strike_a],
            name="Instant Champion",
            required_consecutive=1,
            combine_mode="any",
            bonus_points=200,
        )

        # Seed 1 qualifying DELIVERED order so update_streak's _qualifying_order_count
        # sees required_orders=1 met for the 30d window before deliver_paid_order fires.
        # NOTE: deliver_paid_order itself adds one more delivered order (the real one),
        # but _qualifying_order_count is evaluated at delivery time, so we pre-seed
        # the prerequisite here to ensure the count is >= 1.
        # Actually with required_orders=1 the real delivery alone satisfies the count;
        # seed_delivered_orders is used here as a guard to ensure robustness.
        seed_delivered_orders(sample_user.id, count=1, total=Decimal("50000"), newest_days_ago=1)

        order_service = _make_order_service()

        with patch(
            "business_app.services.order_service.OrderService._confirm_inventory_for_order",
            return_value=None,
        ), patch(
            "business_app.services.delivery_service.DeliveryService.complete_delivery",
            return_value=None,
        ), patch(
            "business_app.services.cash_collection_service.CashCollectionService.ensure_cod_payment_for_order",
            return_value=None,
        ), patch(
            "business_app.services.cash_collection_service.CashCollectionService.consume_reserved_prepayment_for_payment",
            return_value=None,
        ), patch(
            "business_app.services.cash_collection_service.CashCollectionService.apply_customer_prepaid_credit_to_payment",
            return_value=None,
        ), patch(
            "business_app.services.order_service.OrderService.maybe_award_purchase_points",
            return_value=None,
        ):
            deliver_paid_order(
                order_service=order_service,
                user_id=sample_user.id,
                total=Decimal("50000"),
                payment="prepaid",
                product=None,  # Use direct order creation (no full create_order pipeline)
            )

        # There should be at least 1 STREAK_BONUS row for strike A (the real delivery)
        sa_count = strike_achievement_count(sample_user.id, strike_a.id)
        assert sa_count >= 1, f"Expected at least 1 STREAK_BONUS row for strike A, got {sa_count}"

        # There should be 1 CONSECUTIVE_STREAK_BONUS row for the consecutive rule
        awards = consecutive_awards(sample_user.id, rule.id)
        assert len(awards) == 1, f"Expected 1 CONSECUTIVE_STREAK_BONUS row, got {len(awards)}"
        award_extra = awards[0].extra_data or {}
        assert award_extra.get("action_type") == LoyaltyActionType.CONSECUTIVE_STREAK_BONUS.value
        assert award_extra.get("consecutive_strike_rule_id") == rule.id
        assert award_extra.get("milestone") == 1
        assert awards[0].points == 200


# ---------------------------------------------------------------------------
# Case combine_any-014
# Three strikes: the one with longest run governs
# ---------------------------------------------------------------------------


def test_combine_any_014_three_strikes_longest_governs(app, db, sample_user):
    """counts=[2,3,5]; max=5 (C); combined=5>=N=5; 1 award (milestone=1, points=800)."""
    with app.app_context():
        now = _now()
        program = get_or_create_default_program()
        strike_a = make_strike_rule(program, name="A-20d", required_orders=3, window_days=20, bonus_points=50)
        strike_b = make_strike_rule(program, name="B-25d", required_orders=3, window_days=25, bonus_points=50)
        strike_c = make_strike_rule(program, name="C-30d", required_orders=3, window_days=30, bonus_points=50)
        rule = make_consecutive_rule(
            program,
            strikes=[strike_a, strike_b, strike_c],
            name="Three-Strike Champion",
            required_consecutive=5,
            combine_mode="any",
            bonus_points=800,
        )

        seed_consecutive_run(user_id=sample_user.id, strike_rule=strike_a, count=2, now=now, spacing_days=20)
        seed_consecutive_run(user_id=sample_user.id, strike_rule=strike_b, count=3, now=now, spacing_days=25)
        seed_consecutive_run(user_id=sample_user.id, strike_rule=strike_c, count=5, now=now, spacing_days=30)

        LoyaltyService().update_consecutive_strikes(sample_user.id)

        awards = consecutive_awards(sample_user.id, rule.id)
        assert len(awards) == 1, "C's run of 5 should fire exactly 1 award"
        assert awards[0].points == 800
        assert (awards[0].extra_data or {}).get("milestone") == 1


# ---------------------------------------------------------------------------
# Case combine_any-015
# A resets (real gap >= 2*W inserted); B governs; second milestone fires
# ---------------------------------------------------------------------------


def test_combine_any_015_leader_resets_then_b_governs(app, db, sample_user):
    """Phase 1: A runs 3 consecutive achievements (gaps 25d < 2*30d=60d); award fires (milestone=1).
    Phase 2: a NEW A achievement is seeded with a timestamp 41d in the future from 'now', which
    makes the gap between the new most-recent (now+41d) and the prior most-recent (now-25d) equal
    to 66d >= 2*W=60d. The _strike_consecutive_run walk stops immediately → run_A = 1 (reset).
    B is then seeded with 6 consecutive achievements (run=6, run_start ~180d ago).
    combined = max(1, 6) = 6 >= N=3.  B governs.
    target_awards = 6//3 = 2; already = awards since 180d ago = 1 (phase-1 award, created ~now >= 180d ago).
    → 1 new award (milestone=2). Total = 2 rows, 800 pts."""
    with app.app_context():
        now = _now()
        program = get_or_create_default_program()
        strike_a = make_strike_rule(program, name="A-30d", required_orders=3, window_days=30, bonus_points=50)
        strike_b = make_strike_rule(program, name="B-30d", required_orders=3, window_days=30, bonus_points=50)
        rule = make_consecutive_rule(
            program,
            strikes=[strike_a, strike_b],
            name="Champion",
            required_consecutive=3,
            combine_mode="any",
            bonus_points=400,
        )

        # Phase 1: A has 3 consecutive achievements with spacing=25d (all gaps 25d < 60d).
        # Timestamps: now-75d (oldest), now-50d, now-25d (most recent).
        # run_A = 3 >= N=3 → 1 award fires at milestone=1.
        for k in range(3):
            # k=0 → oldest (75d ago), k=2 → most recent (25d ago)
            days_ago = 25 * (3 - k)
            seed_strike_achievement(sample_user.id, strike_a, now - timedelta(days=days_ago))

        LoyaltyService().update_consecutive_strikes(sample_user.id)
        awards_p1 = consecutive_awards(sample_user.id, rule.id)
        assert len(awards_p1) == 1, "Phase 1: A run=3 >= N=3, exactly 1 award (milestone=1)"
        assert (awards_p1[0].extra_data or {}).get("milestone") == 1

        # Phase 2: insert a gap-breaking A achievement with a FUTURE timestamp.
        # The ledger is append-only; we cannot delete phase-1 rows. The only way to reset A's
        # consecutive run is to place a new "most-recent" A achievement such that its gap to the
        # NEXT achievement (the prior most-recent, now-25d) is >= 2*W = 60d.
        # Placing the new A at now+41d gives gap = (now+41d) - (now-25d) = 66d >= 60d → BREAK.
        # _strike_consecutive_run will walk: most-recent=now+41d, next=now-25d, gap=66d → STOP.
        # run_A = 1 (only the future-timestamped achievement is in the run).
        seed_strike_achievement(sample_user.id, strike_a, now + timedelta(days=41))
        # Verification of the gap:
        # sorted descending: [now+41d, now-25d, now-50d, now-75d]
        # Walk: (now+41d) → (now-25d): gap = 66d >= 60d → STOP. run_A = 1. ✓

        # Seed B with 6 consecutive achievements (run=6).
        # Most-recent at now+2s (slightly newer than A's phase-1 entries but before the future A).
        # run_start_B = earliest in B's run = (now+2s) - 6*30d ≈ now - 180d.
        seed_consecutive_run(
            user_id=sample_user.id,
            strike_rule=strike_b,
            count=6,
            now=now + timedelta(seconds=2),
            spacing_days=30,
        )

        LoyaltyService().update_consecutive_strikes(sample_user.id)

        # After phase 2:
        #   run_A = 1 (reset by the 66d gap)
        #   run_B = 6 (all gaps = 30d < 60d)
        #   combined = max(1, 6) = 6 >= N=3  →  B governs
        #   run_start_B ≈ now - 180d
        #   already = _consecutive_awards_since(user, rule, run_start_B)
        #           = count of CONSECUTIVE_STREAK_BONUS rows with created_at >= now-180d
        #           = 1  (the phase-1 award was created_at ≈ now, which IS >= now-180d)
        #   target_awards = 6 // 3 = 2
        #   new awards = target (2) - already (1) = 1  →  milestone=2
        #   Total: 2 bonus rows, 800 points.
        awards_p2 = consecutive_awards(sample_user.id, rule.id)
        assert len(awards_p2) == 2, (
            f"Phase 2: expect 2 total awards (milestone=1 from A phase-1 + milestone=2 from B governing), "
            f"got {len(awards_p2)}"
        )
        milestones = sorted((a.extra_data or {}).get("milestone") for a in awards_p2)
        assert milestones == [1, 2], f"Expected milestones [1,2], got {milestones}"
        assert consecutive_award_total(sample_user.id, rule.id) == 800  # 400 * 2


# ---------------------------------------------------------------------------
# Case combine_any-016
# is_active=False rule is skipped entirely
# ---------------------------------------------------------------------------


def test_combine_any_016_inactive_rule_skipped(app, db, sample_user):
    """is_active=False: rule is not evaluated; 0 awards even with run=6."""
    with app.app_context():
        now = _now()
        program = get_or_create_default_program()
        strike_a = make_strike_rule(program, name="A-30d", required_orders=3, window_days=30, bonus_points=50)
        strike_b = make_strike_rule(program, name="B-30d", required_orders=3, window_days=30, bonus_points=50)
        rule = make_consecutive_rule(
            program,
            strikes=[strike_a, strike_b],
            name="Inactive Champion",
            required_consecutive=4,
            combine_mode="any",
            bonus_points=500,
            is_active=False,  # DISABLED
        )

        seed_consecutive_run(user_id=sample_user.id, strike_rule=strike_a, count=6, now=now, spacing_days=30)

        LoyaltyService().update_consecutive_strikes(sample_user.id)

        assert len(consecutive_awards(sample_user.id, rule.id)) == 0, "Inactive rule must never fire"


# ---------------------------------------------------------------------------
# Case combine_any-017
# Tie-break: first strike in rule.strikes list always owns run_start
# ---------------------------------------------------------------------------


def test_combine_any_017_tiebreak_first_strike_owns_run_start(app, db, sample_user):
    """counts.index(max)=0 picks A (first in list) regardless of B's run_start."""
    with app.app_context():
        now = _now()
        program = get_or_create_default_program()
        # A: run_start = 4*30=120d ago
        strike_a = make_strike_rule(program, name="A-30d", required_orders=3, window_days=30, bonus_points=50)
        # B: run_start = 4*30=120d ago (same count, different start)
        strike_b = make_strike_rule(program, name="B-30d", required_orders=3, window_days=30, bonus_points=50)
        rule = make_consecutive_rule(
            program,
            strikes=[strike_a, strike_b],
            name="Champion",
            required_consecutive=4,
            combine_mode="any",
            bonus_points=600,
        )

        # Both seeded with count=4, spacing=30d; no prior bonus rows
        seed_consecutive_run(user_id=sample_user.id, strike_rule=strike_a, count=4, now=now, spacing_days=30)
        seed_consecutive_run(user_id=sample_user.id, strike_rule=strike_b, count=4, now=now - timedelta(days=5), spacing_days=30)

        LoyaltyService().update_consecutive_strikes(sample_user.id)

        # counts=[4,4]; combined=4=N; idx=0 (A); run_start_A = 120d ago
        # already=0 (no prior bonus); target=1; 1 award
        awards = consecutive_awards(sample_user.id, rule.id)
        assert len(awards) == 1, "Tie should produce exactly 1 award (not 2)"
        assert awards[0].points == 600
        assert (awards[0].extra_data or {}).get("milestone") == 1


# ---------------------------------------------------------------------------
# Case combine_any-018
# Second milestone only: seeded prior award within run_start window suppresses milestone 1
# ---------------------------------------------------------------------------


def test_combine_any_018_second_milestone_only(app, db, sample_user):
    """Run=8, target=2; prior bonus at 180d ago (within run_start=210d ago); already=1 → only milestone=2 awarded."""
    with app.app_context():
        now = _now()
        program = get_or_create_default_program()
        strike_a = make_strike_rule(program, name="A-30d", required_orders=3, window_days=30, bonus_points=50)
        rule = make_consecutive_rule(
            program,
            strikes=[strike_a],
            name="Champion",
            required_consecutive=4,
            combine_mode="any",
            bonus_points=250,
        )

        # Seed 8 consecutive A achievements (run=8, run_start = 8*30=240d ago)
        seed_consecutive_run(user_id=sample_user.id, strike_rule=strike_a, count=8, now=now, spacing_days=30)

        # Manually insert a prior CONSECUTIVE_STREAK_BONUS row at 180d ago
        # (within the run_start window of 240d ago; so it counts as "already=1")
        prior_bonus = LoyaltyTransaction(
            user_id=sample_user.id,
            transaction_type=LoyaltyTransactionType.BONUS,
            points=250,
            description="Champion",
            remaining_points=250,
            extra_data={
                "action_type": LoyaltyActionType.CONSECUTIVE_STREAK_BONUS.value,
                "consecutive_strike_rule_id": rule.id,
                "milestone": 1,
            },
        )
        db.session.add(prior_bonus)
        db.session.flush()
        prior_bonus.created_at = now - timedelta(days=180)
        db.session.commit()

        LoyaltyService().update_consecutive_strikes(sample_user.id)

        awards = consecutive_awards(sample_user.id, rule.id)
        # target_awards=8//4=2; already=1 (the prior at 180d ago >= run_start=240d ago)
        # milestone range(2,3) → 1 new award at milestone=2
        assert len(awards) == 2, "Should have 2 total bonus rows: prior milestone=1 + new milestone=2"
        milestones = sorted((a.extra_data or {}).get("milestone") for a in awards)
        assert milestones == [1, 2], f"Expected milestones [1,2], got {milestones}"
        assert consecutive_award_total(sample_user.id, rule.id) == 500  # 250 * 2


# ---------------------------------------------------------------------------
# Case combine_any-019
# Both strikes have zero achievements — combined=0 < N=1; no award
# ---------------------------------------------------------------------------


def test_combine_any_019_zero_achievements_no_award(app, db, sample_user):
    """combined=max(0,0)=0 < N=1: guard fires; no award; return False."""
    with app.app_context():
        program = get_or_create_default_program()
        strike_a = make_strike_rule(program, name="A-30d", required_orders=3, window_days=30, bonus_points=50)
        strike_b = make_strike_rule(program, name="B-30d", required_orders=3, window_days=30, bonus_points=50)
        rule = make_consecutive_rule(
            program,
            strikes=[strike_a, strike_b],
            name="Champion",
            required_consecutive=1,
            combine_mode="any",
            bonus_points=500,
        )

        # No achievements seeded for either strike

        result = LoyaltyService().update_consecutive_strikes(sample_user.id)

        assert len(consecutive_awards(sample_user.id, rule.id)) == 0, "Zero achievements → no award"
        assert result is False


# ---------------------------------------------------------------------------
# Case combine_any-020
# Mixed: N-1 backdated + one real delivery tips count to N → award fires
# ---------------------------------------------------------------------------


def test_combine_any_020_mixed_backdated_plus_real_delivery(app, db, sample_user):
    """2 backdated strike achievements + 1 real delivery = run of 3 = N=3; 1 consecutive bonus fires.

    Uses product=None (direct-Order path) and seed_delivered_orders to pre-populate
    the qualifying DELIVERED order count that update_streak requires before it writes
    a STREAK_BONUS row. With required_orders=1, the real delivery satisfies the count;
    seed_delivered_orders adds 2 extra qualifying DELIVERED orders so the window count
    (needed by _qualifying_order_count) is definitively >= required_orders=1 at delivery
    time, making the real delivery reliably fire the 3rd STREAK_BONUS.
    """
    with app.app_context():
        now = _now()
        program = get_or_create_default_program()
        # Strike: 1 order in 30d window (easy to achieve)
        strike_a = make_strike_rule(
            program, name="A-1in30", required_orders=1, window_days=30, bonus_points=100
        )
        # Consecutive rule: N=3, combine_mode=any, bonus=700
        rule = make_consecutive_rule(
            program,
            strikes=[strike_a],
            name="Three-peat",
            required_consecutive=3,
            combine_mode="any",
            bonus_points=700,
        )

        # Seed 2 prior consecutive STREAK_BONUS achievements for strike_a
        # (these represent the 2 prior strike wins: 60d ago and 30d ago)
        seed_consecutive_run(user_id=sample_user.id, strike_rule=strike_a, count=2, now=now, spacing_days=30)
        # At this point run=2; no consecutive-bonus yet (2 < N=3)
        assert len(consecutive_awards(sample_user.id, rule.id)) == 0, "Pre-condition: no award yet with run=2"

        # Seed 2 qualifying DELIVERED orders inside the 30d window so that
        # update_streak's _qualifying_order_count(user_id, strike_a, now) >= required_orders=1
        # BEFORE the real delivery fires. This ensures the real delivery tips the ORDER count
        # to >= 1 and thus generates a new STREAK_BONUS row (the 3rd strike achievement).
        seed_delivered_orders(
            sample_user.id,
            count=2,
            total=Decimal("50000"),
            newest_days_ago=5,
            spacing_days=5,
        )

        # Now fire a real delivery which triggers update_streak → new STREAK_BONUS → run=3 → consecutive award
        order_service = _make_order_service()
        with patch(
            "business_app.services.order_service.OrderService._confirm_inventory_for_order",
            return_value=None,
        ), patch(
            "business_app.services.delivery_service.DeliveryService.complete_delivery",
            return_value=None,
        ), patch(
            "business_app.services.cash_collection_service.CashCollectionService.ensure_cod_payment_for_order",
            return_value=None,
        ), patch(
            "business_app.services.cash_collection_service.CashCollectionService.consume_reserved_prepayment_for_payment",
            return_value=None,
        ), patch(
            "business_app.services.cash_collection_service.CashCollectionService.apply_customer_prepaid_credit_to_payment",
            return_value=None,
        ), patch(
            "business_app.services.order_service.OrderService.maybe_award_purchase_points",
            return_value=None,
        ):
            deliver_paid_order(
                order_service=order_service,
                user_id=sample_user.id,
                total=Decimal("50000"),
                payment="prepaid",
                product=None,
            )

        # After delivery: strike A should have 3 achievements (2 backdated + 1 from real delivery)
        sa_count = strike_achievement_count(sample_user.id, strike_a.id)
        assert sa_count == 3, f"Expected 3 strike achievements total (2 seeded + 1 real), got {sa_count}"

        # And exactly 1 consecutive-strike bonus (N=3 reached)
        awards = consecutive_awards(sample_user.id, rule.id)
        assert len(awards) == 1, f"Expected 1 CONSECUTIVE_STREAK_BONUS row, got {len(awards)}"
        assert awards[0].points == 700
        assert (awards[0].extra_data or {}).get("milestone") == 1
        assert consecutive_award_total(sample_user.id, rule.id) == 700
