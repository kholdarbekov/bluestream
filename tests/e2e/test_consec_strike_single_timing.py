"""E2E tests for single-strike consecutive-bonus timing dimension.

Covers all 20 enumerated cases (single_timing-001 through single_timing-020) from
the consecutive-strike bonus rule spec.  Each test is independent: it uses fresh
DB state (function-scoped ``db`` fixture), seeds ledger rows directly via the
shared helpers, and asserts on real DB state / real service return values — no
mock return values are asserted.

All tests use the ``@pytest.mark.e2e`` marker (``@pytest.mark.loyalty`` is NOT
registered and would fail under ``--strict-markers``).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import pytest

from business_app import db
from business_app.services.loyalty_service import LoyaltyService
from business_app.services.order_service import OrderService

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

pytestmark = pytest.mark.e2e

# ---------------------------------------------------------------------------
# Module-level silence fixture (autouse so every test in this file is clean)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _silence(monkeypatch):
    """No-op every LoyaltyService notification side-effect."""
    silence_loyalty_notifications(monkeypatch)


# ---------------------------------------------------------------------------
# Convenience shorthand
# ---------------------------------------------------------------------------

NOW = None  # evaluated inside each test via datetime.now(timezone.utc)


def _now():
    return datetime.now(timezone.utc)


# ===========================================================================
# single_timing-001 — Zero achievements: no award, run=0, progress active=False
# ===========================================================================


def test_001_zero_achievements_no_award(app, db, sample_user):
    """Spec §5.1: returns 0 if there are no achievements.
    §5.3: combined < N so skip.
    """
    with app.app_context():
        program = get_or_create_default_program()
        strike = make_strike_rule(program, window_days=30, bonus_points=50)
        consec = make_consecutive_rule(
            program, [strike], required_consecutive=3, bonus_points=500
        )

        svc = LoyaltyService()
        svc.update_consecutive_strikes(sample_user.id)

        # No CONSECUTIVE_STREAK_BONUS rows
        assert consecutive_awards(sample_user.id, consec.id) == []
        assert consecutive_award_total(sample_user.id, consec.id) == 0

        # Progress
        prog = svc.get_consecutive_strike_progress(sample_user.id)
        assert len(prog) == 1
        assert prog[0]["combined_current"] == 0
        assert prog[0]["per_strike"][0]["current"] == 0
        assert prog[0]["per_strike"][0]["active"] is False


# ===========================================================================
# single_timing-002 — Single achievement: run=1, below N=3, no award
# ===========================================================================


def test_002_single_achievement_below_threshold(app, db, sample_user):
    """Spec §5.1: run of 1. §5.3: combined (1) < N (3) → skip."""
    with app.app_context():
        now = _now()
        program = get_or_create_default_program()
        strike = make_strike_rule(program, window_days=30, bonus_points=50)
        consec = make_consecutive_rule(
            program, [strike], required_consecutive=3, bonus_points=500
        )

        # Seed exactly 1 achievement
        seed_strike_achievement(sample_user.id, strike, now - timedelta(days=30))

        svc = LoyaltyService()
        svc.update_consecutive_strikes(sample_user.id)

        # No award
        assert consecutive_award_total(sample_user.id, consec.id) == 0

        # _strike_consecutive_run returns (1, timestamp)
        run_len, run_start = svc._strike_consecutive_run(sample_user.id, strike, now)
        assert run_len == 1
        assert run_start is not None


# ===========================================================================
# single_timing-003 — N-1 achievements (below threshold): no award
# ===========================================================================


def test_003_n_minus_1_achievements_no_award(app, db, sample_user):
    """Off-by-one boundary: exactly one below threshold must not award.
    Spec §5.3: combined (3) < N (4) → skip.
    """
    with app.app_context():
        now = _now()
        program = get_or_create_default_program()
        strike = make_strike_rule(program, window_days=30, bonus_points=50)
        consec = make_consecutive_rule(
            program, [strike], required_consecutive=4, bonus_points=500
        )

        # Seed 3 consecutive achievements spaced 30d apart (gaps 30d < 60d = 2*W)
        seed_consecutive_run(sample_user.id, strike, count=3, now=now, spacing_days=30)

        svc = LoyaltyService()
        svc.update_consecutive_strikes(sample_user.id)

        assert consecutive_award_total(sample_user.id, consec.id) == 0


# ===========================================================================
# single_timing-004 — Exactly N achievements: first award fires once
# ===========================================================================


def test_004_exactly_n_achievements_one_award(app, db, sample_user):
    """Spec §5.3: combined (3) == N (3) → target_awards=3//3=1, already=0,
    award milestone 1.
    """
    with app.app_context():
        now = _now()
        program = get_or_create_default_program()
        strike = make_strike_rule(program, window_days=30, bonus_points=50)
        consec = make_consecutive_rule(
            program, [strike], required_consecutive=3, bonus_points=500
        )

        # 3 consecutive achievements at now-90d, now-60d, now-30d; all gaps 30d < 60d
        seed_strike_achievement(sample_user.id, strike, now - timedelta(days=90))
        seed_strike_achievement(sample_user.id, strike, now - timedelta(days=60))
        seed_strike_achievement(sample_user.id, strike, now - timedelta(days=30))

        svc = LoyaltyService()
        svc.update_consecutive_strikes(sample_user.id)

        awards = consecutive_awards(sample_user.id, consec.id)
        assert len(awards) == 1, f"Expected 1 award, got {len(awards)}"
        assert awards[0].points == 500
        assert (awards[0].extra_data or {}).get("milestone") == 1
        assert consecutive_award_total(sample_user.id, consec.id) == 500


# ===========================================================================
# single_timing-005 — N=1 edge: single achievement immediately triggers award
# ===========================================================================


def test_005_n1_single_achievement_awards_immediately(app, db, sample_user):
    """Spec §5.3: combined (1) // 1 = 1 → award. N=1 is the minimum valid value."""
    with app.app_context():
        now = _now()
        program = get_or_create_default_program()
        strike = make_strike_rule(program, window_days=30, bonus_points=50)
        consec = make_consecutive_rule(
            program, [strike], required_consecutive=1, bonus_points=200
        )

        seed_strike_achievement(sample_user.id, strike, now - timedelta(days=5))

        svc = LoyaltyService()
        svc.update_consecutive_strikes(sample_user.id)

        awards = consecutive_awards(sample_user.id, consec.id)
        assert len(awards) == 1
        assert awards[0].points == 200
        assert (awards[0].extra_data or {}).get("milestone") == 1
        assert consecutive_award_total(sample_user.id, consec.id) == 200


# ===========================================================================
# single_timing-006 — Gap just under 2*W: run continues, award fires at N
# ===========================================================================


def test_006_gap_just_under_2w_consecutive_boundary(app, db, sample_user):
    """Spec §5.1 step 2: gap < 2*W is consecutive; one day inside the boundary
    must still count.  window_days=30 → 2*W=60d.
    """
    with app.app_context():
        now = _now()
        program = get_or_create_default_program()
        # window_days=30, 2*W=60d
        strike = make_strike_rule(program, window_days=30, bonus_points=50)
        consec = make_consecutive_rule(
            program, [strike], required_consecutive=3, bonus_points=500
        )

        # Gaps: (now-1d)-(now-60d)=59d < 60d, (now-60d)-(now-119d)=59d < 60d → run=3
        seed_strike_achievement(sample_user.id, strike, now - timedelta(days=119))
        seed_strike_achievement(sample_user.id, strike, now - timedelta(days=60))
        seed_strike_achievement(sample_user.id, strike, now - timedelta(days=1))

        svc = LoyaltyService()
        run_len, _ = svc._strike_consecutive_run(sample_user.id, strike, now)
        assert run_len == 3, f"Expected run=3, got {run_len}"

        svc.update_consecutive_strikes(sample_user.id)
        assert len(consecutive_awards(sample_user.id, consec.id)) == 1

        prog = svc.get_consecutive_strike_progress(sample_user.id)
        assert len(prog) == 1
        # combined_current capped at N=3
        assert prog[0]["combined_current"] == 3
        # active: (now - (now-1d)) = 1d < 60d → True
        assert prog[0]["per_strike"][0]["active"] is True


# ===========================================================================
# single_timing-007 — Gap exactly 2*W: run breaks, no award
# ===========================================================================


def test_007_gap_exactly_2w_resets_run(app, db, sample_user):
    """Spec §5.1 step 2: 'gap < 2*W' — equal is NOT consecutive.
    Exactly-at-boundary (60d gap) resets the run.  Critical boundary semantics.
    """
    with app.app_context():
        now = _now()
        program = get_or_create_default_program()
        # window_days=30, 2*W=60d; exactly 60d gap is NOT < 60d
        strike = make_strike_rule(program, window_days=30, bonus_points=50)
        consec = make_consecutive_rule(
            program, [strike], required_consecutive=3, bonus_points=500
        )

        # now-61d and now-1d: gap = 60d which is NOT < 60d → reset
        seed_strike_achievement(sample_user.id, strike, now - timedelta(days=121))
        seed_strike_achievement(sample_user.id, strike, now - timedelta(days=61))
        seed_strike_achievement(sample_user.id, strike, now - timedelta(days=1))

        svc = LoyaltyService()
        run_len, _ = svc._strike_consecutive_run(sample_user.id, strike, now)
        # Walk newest-first: (now-1d)-(now-61d)=60d, NOT < 60d → run stops at 1
        assert run_len == 1, f"Expected run=1 (boundary reset), got {run_len}"

        svc.update_consecutive_strikes(sample_user.id)
        # combined=1 < N=3 → no award
        assert consecutive_award_total(sample_user.id, consec.id) == 0


# ===========================================================================
# single_timing-008 — Gap just over 2*W: run starts fresh at 1
# ===========================================================================


def test_008_gap_just_over_2w_fresh_start(app, db, sample_user):
    """Spec §5.1 step 2: first gap >= 2*W stops the walk; older achievements
    beyond the gap are ignored.
    """
    with app.app_context():
        now = _now()
        program = get_or_create_default_program()
        # window_days=30, 2*W=60d; 65d gap > 60d → reset
        strike = make_strike_rule(program, window_days=30, bonus_points=50)
        consec = make_consecutive_rule(
            program, [strike], required_consecutive=3, bonus_points=500
        )

        # Old run of 2 at now-130d and now-70d (gap 60d, exactly at limit - these
        # are themselves at the boundary but the FRESH achievement gap is 65d from
        # now-70d to now-5d which breaks)
        seed_strike_achievement(sample_user.id, strike, now - timedelta(days=130))
        seed_strike_achievement(sample_user.id, strike, now - timedelta(days=70))
        # Gap: (now-5d) - (now-70d) = 65d > 60d → reset
        seed_strike_achievement(sample_user.id, strike, now - timedelta(days=5))

        svc = LoyaltyService()
        run_len, run_start = svc._strike_consecutive_run(sample_user.id, strike, now)
        assert run_len == 1, f"Expected run=1 after reset, got {run_len}"
        # run_start should be the single fresh achievement at ~now-5d
        assert run_start is not None

        svc.update_consecutive_strikes(sample_user.id)
        assert consecutive_award_total(sample_user.id, consec.id) == 0

        prog = svc.get_consecutive_strike_progress(sample_user.id)
        assert prog[0]["combined_current"] == 1


# ===========================================================================
# single_timing-009 — Reset then recover: new run awards at N
# ===========================================================================


def test_009_reset_then_recover_new_run_awards(app, db, sample_user):
    """Spec §5.3: run_start is the earliest timestamp of the CURRENT run.
    _consecutive_awards_since only counts awards >= run_start, so a pre-reset
    award does not block the new run's first milestone.
    """
    with app.app_context():
        now = _now()
        program = get_or_create_default_program()
        strike = make_strike_rule(program, window_days=30, bonus_points=50)
        consec = make_consecutive_rule(
            program, [strike], required_consecutive=2, bonus_points=300
        )

        # Old run of 2 at now-200d, now-170d (gap 30d < 60d → old run=2)
        seed_strike_achievement(sample_user.id, strike, now - timedelta(days=200))
        seed_strike_achievement(sample_user.id, strike, now - timedelta(days=170))
        # Gap 110d (from now-170d to now-60d) > 60d → reset
        # New run at now-30d, now-1d (gap 29d < 60d → new run=2)
        seed_strike_achievement(sample_user.id, strike, now - timedelta(days=30))
        seed_strike_achievement(sample_user.id, strike, now - timedelta(days=1))

        svc = LoyaltyService()
        run_len, run_start = svc._strike_consecutive_run(sample_user.id, strike, now)
        assert run_len == 2, f"Expected new run=2, got {run_len}"

        svc.update_consecutive_strikes(sample_user.id)

        awards = consecutive_awards(sample_user.id, consec.id)
        assert len(awards) == 1, f"Expected 1 award from new run, got {len(awards)}"
        assert consecutive_award_total(sample_user.id, consec.id) == 300


# ===========================================================================
# single_timing-010 — Long dormancy then resume: run=1 after a long gap
# ===========================================================================


def test_010_long_dormancy_then_resume(app, db, sample_user):
    """Long dormancy creates a full reset. Resume treated as first achievement of a
    new run. active flag should be True because last achievement was recent.
    """
    with app.app_context():
        now = _now()
        program = get_or_create_default_program()
        strike = make_strike_rule(program, window_days=30, bonus_points=50)
        consec = make_consecutive_rule(
            program, [strike], required_consecutive=4, bonus_points=600
        )

        # Old achievement 400d ago, well beyond 2*W=60d
        seed_strike_achievement(sample_user.id, strike, now - timedelta(days=400))
        # Fresh achievement 5d ago
        seed_strike_achievement(sample_user.id, strike, now - timedelta(days=5))

        svc = LoyaltyService()
        run_len, run_start = svc._strike_consecutive_run(sample_user.id, strike, now)
        assert run_len == 1, f"Expected run=1 after long dormancy, got {run_len}"

        svc.update_consecutive_strikes(sample_user.id)
        assert consecutive_award_total(sample_user.id, consec.id) == 0

        prog = svc.get_consecutive_strike_progress(sample_user.id)
        assert prog[0]["combined_current"] == 1
        # active: (now - (now-5d)) = 5d < 60d → True
        assert prog[0]["per_strike"][0]["active"] is True


# ===========================================================================
# single_timing-011 — 2*N consecutive achievements: exactly two awards
# ===========================================================================


def test_011_two_n_achievements_two_awards(app, db, sample_user):
    """Spec §5.3: target_awards = combined // N = 6//3 = 2.
    Award milestones 1 and 2 in one call.
    Spec §6.1: combined_current capped at N.
    """
    with app.app_context():
        now = _now()
        program = get_or_create_default_program()
        strike = make_strike_rule(program, window_days=30, bonus_points=50)
        consec = make_consecutive_rule(
            program, [strike], required_consecutive=3, bonus_points=500
        )

        # 6 consecutive achievements spaced 30d apart (all gaps 30d < 60d)
        seed_consecutive_run(sample_user.id, strike, count=6, now=now, spacing_days=30)

        svc = LoyaltyService()
        svc.update_consecutive_strikes(sample_user.id)

        awards = consecutive_awards(sample_user.id, consec.id)
        assert len(awards) == 2, f"Expected 2 awards for 2*N run, got {len(awards)}"

        milestones = sorted((a.extra_data or {}).get("milestone") for a in awards)
        assert milestones == [1, 2]
        assert consecutive_award_total(sample_user.id, consec.id) == 1000

        # combined_current capped at N=3 (not 6)
        prog = svc.get_consecutive_strike_progress(sample_user.id)
        assert prog[0]["combined_current"] == 3
        assert prog[0]["per_strike"][0]["current"] == 3


# ===========================================================================
# single_timing-012 — Idempotency: calling twice does not double-award
# ===========================================================================


def test_012_idempotency_no_double_award(app, db, sample_user):
    """Spec §5.3: already = _consecutive_awards_since >= run_start;
    target_awards (1) == already (1) → no new milestones.
    """
    with app.app_context():
        now = _now()
        program = get_or_create_default_program()
        strike = make_strike_rule(program, window_days=30, bonus_points=50)
        consec = make_consecutive_rule(
            program, [strike], required_consecutive=3, bonus_points=500
        )

        # 3 consecutive achievements → triggers milestone 1
        seed_strike_achievement(sample_user.id, strike, now - timedelta(days=90))
        seed_strike_achievement(sample_user.id, strike, now - timedelta(days=60))
        seed_strike_achievement(sample_user.id, strike, now - timedelta(days=30))

        svc = LoyaltyService()
        svc.update_consecutive_strikes(sample_user.id)
        # Second call — must be idempotent
        svc.update_consecutive_strikes(sample_user.id)

        awards = consecutive_awards(sample_user.id, consec.id)
        assert len(awards) == 1, f"Expected exactly 1 award after 2 calls, got {len(awards)}"
        assert consecutive_award_total(sample_user.id, consec.id) == 500


# ===========================================================================
# single_timing-013 — Progress active flag transitions
# ===========================================================================


def test_013_progress_active_flag_transitions(app, db, sample_user):
    """Spec §6.1: active = (now - last_achievement) < 2*W.
    Scenario A: last achievement 35d ago → active=True (35d < 60d).
    Scenario B: last achievement 65d ago → active=False (65d >= 60d), but run
    count is still 2 because internal gap (40d) < 60d.
    """
    with app.app_context():
        now = _now()
        program = get_or_create_default_program()

        # Scenario A: most recent achievement is 35d ago (active)
        strike_a = make_strike_rule(program, name="Strike A", window_days=30, bonus_points=50)
        consec_a = make_consecutive_rule(
            program, [strike_a], name="Consec A", required_consecutive=3, bonus_points=500
        )
        # now-70d and now-35d → gap 35d < 60d → run=2
        seed_strike_achievement(sample_user.id, strike_a, now - timedelta(days=70))
        seed_strike_achievement(sample_user.id, strike_a, now - timedelta(days=35))

        svc = LoyaltyService()
        prog_a = svc.get_consecutive_strike_progress(sample_user.id)

        # Find the entry for consec_a
        entry_a = next(p for p in prog_a if p["name"] == consec_a.name)
        # now - (now-35d) = 35d < 60d → active=True
        assert entry_a["per_strike"][0]["active"] is True
        assert entry_a["combined_current"] == 2

        # Scenario B: shift the most recent to 65d ago (inactive)
        # Use a SEPARATE user via a second fresh entry for clarity; manipulate
        # the achievement timestamp directly to keep this in the same app context.
        #
        # Instead, re-seed: adjust the last achievement to now-65d for strike_a.
        # We can't modify the existing txn cleanly, so create a separate strike rule.
        strike_b = make_strike_rule(program, name="Strike B", window_days=30, bonus_points=50)
        consec_b = make_consecutive_rule(
            program, [strike_b], name="Consec B", required_consecutive=3, bonus_points=500
        )
        # now-105d and now-65d → internal gap 40d < 60d → run=2;
        # but now - (now-65d) = 65d >= 60d → active=False
        seed_strike_achievement(sample_user.id, strike_b, now - timedelta(days=105))
        seed_strike_achievement(sample_user.id, strike_b, now - timedelta(days=65))

        prog_b = svc.get_consecutive_strike_progress(sample_user.id)
        entry_b = next(p for p in prog_b if p["name"] == consec_b.name)
        # now - (now-65d) = 65d NOT < 60d → active=False
        assert entry_b["per_strike"][0]["active"] is False
        # run count is still 2 (internal gap 40d < 60d)
        assert entry_b["combined_current"] == 2


# ===========================================================================
# single_timing-014 — Real delivery trigger: Nth delivery causes consecutive award
# ===========================================================================


def test_014_real_delivery_trigger_nth_delivery(app, db, sample_user):
    """Spec §5.4: hook is inside update_streak, same transaction.
    Seed required_orders-1 DELIVERED orders so the next real delivery is the Nth
    qualifying order; the strike fires, then the consecutive rule fires.

    Uses seed_delivered_orders + deliver_paid_order(product=None) — the only path
    that works under sqlite (create_order runs Postgres NOW(), which fails on
    sqlite; see deliver_paid_order docstring).
    """
    with app.app_context():
        program = get_or_create_default_program()

        # Strike rule: 3 orders in 30 days.
        required_orders = 3
        strike = make_strike_rule(
            program,
            name="3 in 30",
            required_orders=required_orders,
            window_days=30,
            bonus_points=50,
        )
        consec = make_consecutive_rule(
            program, [strike], required_consecutive=3, bonus_points=750
        )

        # Seed 2 backdated strike achievements (N-1=2) so this user already has
        # a consecutive run of 2 entering the final delivery.
        now = _now()
        seed_strike_achievement(sample_user.id, strike, now - timedelta(days=60))
        seed_strike_achievement(sample_user.id, strike, now - timedelta(days=30))

        # Seed required_orders-1 DELIVERED+paid orders inside the 30-day window so
        # update_streak's _qualifying_order_count sees them when the Nth order arrives.
        # newest_days_ago=1 keeps them inside the window; spacing_days=1 keeps them
        # distinct without leaving the window (required_orders-1=2 orders → 2 days span).
        seed_delivered_orders(
            sample_user.id,
            count=required_orders - 1,
            total=Decimal("50000"),
            newest_days_ago=1,
            spacing_days=1,
        )

        # The Nth real DELIVERED order: drives the real award-trigger path
        #   _handle_status_change_actions → update_streak → update_consecutive_strikes.
        order_service = OrderService()
        deliver_paid_order(
            order_service,
            sample_user.id,
            total=Decimal("50000"),
            payment="prepaid",
            product=None,  # direct-Order path — safe under sqlite
        )

        # EXACT: 2 seeded ledger achievements + 1 from the real DELIVERED edge = 3.
        assert strike_achievement_count(sample_user.id, strike.id) == 3, (
            "Expected exactly 3 strike achievements (2 seeded + 1 from real delivery)"
        )

        # The consecutive rule (N=3 consecutive strikes, run now = 3) must fire once.
        awards = consecutive_awards(sample_user.id, consec.id)
        assert len(awards) == 1, f"Expected 1 consecutive award, got {len(awards)}"
        assert awards[0].points == 750
        assert (awards[0].extra_data or {}).get("milestone") == 1
        assert consecutive_award_total(sample_user.id, consec.id) == 750


# ===========================================================================
# single_timing-015 — Ordering invariance: out-of-order inserts produce correct run
# ===========================================================================


def test_015_ordering_invariance_out_of_order_inserts(app, db, sample_user):
    """Spec §5.1 step 1: '_strike_achievement_times sorts descending by created_at'.
    Insertion order must not affect correctness.
    """
    with app.app_context():
        now = _now()
        program = get_or_create_default_program()
        strike = make_strike_rule(program, window_days=30, bonus_points=50)
        consec = make_consecutive_rule(
            program, [strike], required_consecutive=3, bonus_points=500
        )

        # Insert in REVERSE chronological order (newest first, oldest last)
        seed_strike_achievement(sample_user.id, strike, now - timedelta(days=5))    # newest
        seed_strike_achievement(sample_user.id, strike, now - timedelta(days=35))   # middle
        seed_strike_achievement(sample_user.id, strike, now - timedelta(days=65))   # oldest

        svc = LoyaltyService()
        # All gaps 30d < 60d → run=3 regardless of insertion order
        run_len, _ = svc._strike_consecutive_run(sample_user.id, strike, now)
        assert run_len == 3, f"Expected run=3 (insertion order must not matter), got {run_len}"

        svc.update_consecutive_strikes(sample_user.id)
        assert len(consecutive_awards(sample_user.id, consec.id)) == 1


# ===========================================================================
# single_timing-016 — Progress combined_current caps at N for long runs
# ===========================================================================


def test_016_progress_caps_at_n_for_long_run(app, db, sample_user):
    """Spec §6.1: combined_current capped at N; per_strike[i].current capped at N."""
    with app.app_context():
        now = _now()
        program = get_or_create_default_program()
        strike = make_strike_rule(program, window_days=30, bonus_points=50)
        consec = make_consecutive_rule(
            program, [strike], required_consecutive=4, bonus_points=600
        )

        # 9 consecutive achievements spaced 30d apart (run=9)
        seed_consecutive_run(sample_user.id, strike, count=9, now=now, spacing_days=30)

        svc = LoyaltyService()
        prog = svc.get_consecutive_strike_progress(sample_user.id)

        assert len(prog) == 1
        assert prog[0]["required_consecutive"] == 4
        # combined_current must be capped at N=4, not 9
        assert prog[0]["combined_current"] == 4
        assert prog[0]["per_strike"][0]["current"] == 4


# ===========================================================================
# single_timing-017 — Mixed run: two consecutive then large gap then two consecutive
# ===========================================================================


def test_017_mixed_run_old_and_new_run(app, db, sample_user):
    """Spec §5.3: run_start = earliest timestamp of the CURRENT run.
    _consecutive_awards_since anchors at run_start.  Old run awards stamped
    before run_start are NOT counted in 'already' for the new run.
    """
    with app.app_context():
        now = _now()
        program = get_or_create_default_program()
        strike = make_strike_rule(program, window_days=30, bonus_points=50)
        consec = make_consecutive_rule(
            program, [strike], required_consecutive=2, bonus_points=300
        )

        # Old run of 2 at now-240d, now-210d (gap 30d < 60d)
        seed_strike_achievement(sample_user.id, strike, now - timedelta(days=240))
        seed_strike_achievement(sample_user.id, strike, now - timedelta(days=210))
        # 150d gap from now-210d to now-60d > 60d → reset
        # New run at now-30d, now-1d (gap 29d < 60d → new run=2)
        seed_strike_achievement(sample_user.id, strike, now - timedelta(days=60))
        seed_strike_achievement(sample_user.id, strike, now - timedelta(days=30))

        svc = LoyaltyService()
        run_len, run_start = svc._strike_consecutive_run(sample_user.id, strike, now)
        assert run_len == 2, f"Expected current run=2, got {run_len}"

        svc.update_consecutive_strikes(sample_user.id)

        awards = consecutive_awards(sample_user.id, consec.id)
        # milestone=1 should be awarded for the new run
        assert len(awards) == 1
        assert (awards[0].extra_data or {}).get("milestone") == 1
        assert consecutive_award_total(sample_user.id, consec.id) == 300


# ===========================================================================
# single_timing-018 — Inactive consecutive rule: skipped even when run >= N
# ===========================================================================


def test_018_inactive_consecutive_rule_skipped(app, db, sample_user):
    """Spec §5.3: 'For each active, currently-effective LoyaltyConsecutiveStrikeRule'.
    is_active=False → skip.
    """
    with app.app_context():
        now = _now()
        program = get_or_create_default_program()
        strike = make_strike_rule(program, window_days=30, bonus_points=50)
        consec = make_consecutive_rule(
            program, [strike], required_consecutive=2, is_active=False, bonus_points=400
        )

        # Seed 3 consecutive achievements (run=3 >= N=2)
        seed_consecutive_run(sample_user.id, strike, count=3, now=now, spacing_days=30)

        svc = LoyaltyService()
        svc.update_consecutive_strikes(sample_user.id)

        # is_active=False → no award
        assert consecutive_award_total(sample_user.id, consec.id) == 0


# ===========================================================================
# single_timing-019 — Effective-date gating: rule not yet started is skipped
# ===========================================================================


def test_019_effective_date_not_yet_started_skipped(app, db, sample_user):
    """Spec §5.3: rule evaluates only if is_active AND is_effective(now).
    starts_at > now → is_effective() returns False → skip.
    """
    with app.app_context():
        now = _now()
        program = get_or_create_default_program()
        strike = make_strike_rule(program, window_days=30, bonus_points=50)
        consec = make_consecutive_rule(
            program,
            [strike],
            required_consecutive=2,
            bonus_points=400,
            # starts 7 days in the future
            starts_at=now + timedelta(days=7),
        )

        # Seed 3 consecutive achievements (run=3 >= N=2)
        seed_consecutive_run(sample_user.id, strike, count=3, now=now, spacing_days=30)

        svc = LoyaltyService()
        svc.update_consecutive_strikes(sample_user.id)

        # Not yet started → no award
        assert consecutive_award_total(sample_user.id, consec.id) == 0


# ===========================================================================
# single_timing-020 — Effective-date gating: expired rule is skipped
# ===========================================================================


def test_020_effective_date_expired_skipped(app, db, sample_user):
    """Spec §5.3: ends_at in the past makes is_effective() False.
    Past achievements must not award if the rule window has closed.
    """
    with app.app_context():
        now = _now()
        program = get_or_create_default_program()
        strike = make_strike_rule(program, window_days=30, bonus_points=50)
        consec = make_consecutive_rule(
            program,
            [strike],
            required_consecutive=2,
            bonus_points=400,
            # ended 1 day ago
            ends_at=now - timedelta(days=1),
        )

        # Seed 3 consecutive achievements (run=3 >= N=2)
        seed_consecutive_run(sample_user.id, strike, count=3, now=now, spacing_days=30)

        svc = LoyaltyService()
        svc.update_consecutive_strikes(sample_user.id)

        # Expired → no award
        assert consecutive_award_total(sample_user.id, consec.id) == 0


# ===========================================================================
# single_timing-021 — Gap exactly 2*W: reset boundary (not consecutive)
# ===========================================================================


def test_021_gap_exactly_2w_boundary_resets(app, db, sample_user):
    """Spec §5.1 step 2: the condition is gap < 2·W (strict less-than).
    A gap EXACTLY equal to 2·W is NOT consecutive → run resets to 1.

    Uses a call-time 'now' so the boundary arithmetic is exact to the millisecond
    (no module-level NOW constant whose gap to real now drifts).
    window_days=30 → 2·W = 60 days.
    """
    with app.app_context():
        program = get_or_create_default_program()
        strike = make_strike_rule(program, window_days=30, bonus_points=50)
        consec = make_consecutive_rule(
            program, [strike], required_consecutive=3, bonus_points=500
        )

        # Capture now close to the seeding calls so the gap is exactly 60 days.
        now = _now()
        two_w = timedelta(days=2 * strike.window_days)  # 60 days

        # Three achievements with the innermost gap exactly 60 days:
        #   oldest=now-120d, middle=now-60d, newest=now-1d
        # Gap newest→middle = 59d < 60d → consecutive so far.
        # Gap middle→oldest = 60d which is NOT < 60d → reset; run stops at 2
        # (newest + middle form the current run of length 2).
        seed_strike_achievement(sample_user.id, strike, now - 2 * two_w)        # now-120d
        seed_strike_achievement(sample_user.id, strike, now - two_w)             # now-60d (gap=60d → reset)
        seed_strike_achievement(sample_user.id, strike, now - timedelta(days=1)) # now-1d  (gap=59d → ok)

        svc = LoyaltyService()
        run_len, run_start = svc._strike_consecutive_run(sample_user.id, strike, now)

        # The 60-day gap between now-120d and now-60d breaks the run;
        # only the two newest achievements (now-60d … now-1d, gap 59d) form the run.
        assert run_len == 2, (
            f"Expected run=2 (gap==2*W is not consecutive), got {run_len}"
        )
        assert run_start is not None

        svc.update_consecutive_strikes(sample_user.id)
        # run=2 < N=3 → no award
        assert consecutive_award_total(sample_user.id, consec.id) == 0
        # Progress: combined_current == 2, not 3
        prog = svc.get_consecutive_strike_progress(sample_user.id)
        assert len(prog) == 1
        assert prog[0]["combined_current"] == 2


# ===========================================================================
# single_timing-022 — Gap just under 2*W: consecutive run continues, award fires
# ===========================================================================


def test_022_gap_just_under_2w_continues_run(app, db, sample_user):
    """Spec §5.1 step 2: gap < 2·W is consecutive.
    A gap of (2·W - 1 day) must keep the run going and produce an award at N.

    Uses a call-time 'now' so the gap arithmetic is exact.
    window_days=30 → 2·W = 60 days; just-under = 59 days.
    """
    with app.app_context():
        program = get_or_create_default_program()
        strike = make_strike_rule(program, window_days=30, bonus_points=50)
        consec = make_consecutive_rule(
            program, [strike], required_consecutive=3, bonus_points=500
        )

        now = _now()
        just_under = timedelta(days=2 * strike.window_days - 1)  # 59 days

        # Three achievements spaced exactly 59 days apart — all gaps are 59d < 60d
        # so the run is unbroken and length = 3.
        seed_strike_achievement(sample_user.id, strike, now - 2 * just_under)   # now-118d
        seed_strike_achievement(sample_user.id, strike, now - just_under)        # now-59d  (gap=59d)
        seed_strike_achievement(sample_user.id, strike, now - timedelta(days=1)) # now-1d   (gap=58d)

        svc = LoyaltyService()
        run_len, run_start = svc._strike_consecutive_run(sample_user.id, strike, now)

        # All gaps < 60d → unbroken run of 3
        assert run_len == 3, (
            f"Expected run=3 (gap just under 2*W is consecutive), got {run_len}"
        )
        assert run_start is not None

        svc.update_consecutive_strikes(sample_user.id)

        # run=3 == N=3 → exactly 1 award
        awards = consecutive_awards(sample_user.id, consec.id)
        assert len(awards) == 1, f"Expected 1 award for gap just-under-2*W run, got {len(awards)}"
        assert awards[0].points == 500
        assert (awards[0].extra_data or {}).get("milestone") == 1
        assert consecutive_award_total(sample_user.id, consec.id) == 500
