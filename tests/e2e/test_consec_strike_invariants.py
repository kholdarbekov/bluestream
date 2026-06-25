"""E2E tests: consecutive-strike bonus rule — high-value invariants.

Dimension: invariants (multi-user isolation, cross-strike isolation, natural
progression idempotency, points expiry, tier downgrade, bonus_points=0 boundary,
two-real-delivery repeat-every-N).

All expected values are anchored to the spec at
docs/superpowers/specs/2026-06-24-consecutive-strike-bonus-rule-design.md, NOT
to whatever the code happens to produce.  A spec violation surfaces as a real
test failure so it can be investigated, not silently accepted.

Test mechanics that drive these tests (see the helpers module docstring):
- update_streak writes a STREAK_BONUS achievement only when _qualifying_order_count
  (real DELIVERED Order rows in the window) is met — it does NOT count seeded
  ledger rows.  To fire the Nth strike on a REAL delivery we seed the prerequisite
  DELIVERED orders (seed_delivered_orders) and drive the final delivery via
  deliver_paid_order(product=None) (the direct-Order path; product!=None routes
  through Postgres NOW() and fails under sqlite).
- For pure consecutive-run math we seed backdated STREAK_BONUS ledger rows
  (seed_strike_achievement / seed_consecutive_run) and call update_consecutive_strikes.
- LoyaltyTransaction.created_at read back from sqlite is tz-NAIVE; normalize with
  ensure_utc before comparing against a tz-aware datetime.
- is_user_loyalty_eligible is a @staticmethod taking a USER OBJECT, not an id.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from business_app import db as _db
from business_app.models.loyalty import (
    LoyaltyPoints,
    LoyaltyTierConfig,
    LoyaltyTransaction,
)
from business_app.models.user import User
from business_app.services.loyalty_service import LoyaltyService
from business_app.services.order_service import OrderService
from business_app.utils.constants import LoyaltyActionType, LoyaltyTransactionType
from business_app.utils.password_security import hash_password
from business_app.utils.timezone_utils import ensure_utc
from shared.enums import UserRole, UserType
from tests.e2e._consecutive_strike_helpers import (
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
# Local helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    """Capture wall-clock now CLOSE to the call (never a module constant) so
    boundary (gap == 2*W) cases stay exact relative to evaluation-time now."""
    return datetime.now(timezone.utc)


def _make_individual_user(label: str) -> User:
    """A second/third INDIVIDUAL customer (sample_user covers only one)."""
    user = User(
        email=f"{label}@example.com",
        phone=f"+99890{abs(hash(label)) % 10000000:07d}",
        password_hash=hash_password("TestPassword123!"),
        first_name=label.capitalize(),
        last_name="User",
        user_type=UserType.INDIVIDUAL,
        role=UserRole.CUSTOMER,
        is_verified=True,
    )
    _db.session.add(user)
    _db.session.commit()
    return user


def _ensure_bronze_tier(program):
    """Pin a Bronze tier so award_points' tier-upgrade path and the purchase-points
    calculator have a baseline tier to read."""
    if not LoyaltyTierConfig.query.filter_by(program_id=program.id).first():
        _db.session.add(
            LoyaltyTierConfig(
                program_id=program.id,
                name="Bronze",
                display_order=0,
                min_points=0,
                max_points=None,
                points_multiplier=1.0,
                is_active=True,
            )
        )
        _db.session.commit()


def _balance(user_id: int) -> int:
    """Authoritative ledger-derived AquaCoins balance (excludes expired/lapsed lots)."""
    account = LoyaltyPoints.query.filter_by(user_id=user_id).first()
    assert account is not None, "Expected a loyalty account to exist"
    return account.calculate_current_balance()


def _svc() -> LoyaltyService:
    return LoyaltyService()


# ===========================================================================
# 1. MULTI-USER ISOLATION
# ===========================================================================


def test_invariant_multi_user_isolation(app, db, sample_user):
    """User A reaches a full run + an awarded milestone; a brand-new user B earns
    NOTHING, and A is left untouched.

    Guards against a dropped ``user_id`` filter anywhere in the consecutive-run
    read path.  Spec §5.1 (per-strike count is per-user) and §5.3 (awards are
    per-user).
    """
    with app.app_context():
        program = get_or_create_default_program()
        strike = make_strike_rule(program, name="3 in 30", window_days=30, bonus_points=100)
        rule = make_consecutive_rule(
            program, strikes=[strike], name="Champion",
            required_consecutive=6, combine_mode="all", bonus_points=500,
        )

        user_a = sample_user
        user_b = _make_individual_user("user_b_isolated")

        # A: a full run of exactly N=6 -> one milestone-1 award.
        seed_consecutive_run(user_a.id, strike, count=6, spacing_days=30)
        assert _svc().update_consecutive_strikes(user_a.id) is True

        a_awards = consecutive_awards(user_a.id, rule.id)
        assert len(a_awards) == 1, f"Precondition: A must have exactly 1 award, got {len(a_awards)}"
        assert a_awards[0].extra_data["milestone"] == 1
        assert consecutive_award_total(user_a.id, rule.id) == 500

        # B: brand new, zero achievements -> every read returns the empty/zero state.
        svc = _svc()
        assert svc.update_consecutive_strikes(user_b.id) is False
        assert svc._strike_consecutive_run(user_b.id, strike, _now()) == (0, None)
        assert consecutive_award_total(user_b.id, rule.id) == 0

        b_progress = svc.get_consecutive_strike_progress(user_b.id)
        assert len(b_progress) == 1, "Exactly one active consecutive rule should surface"
        assert b_progress[0]["combined_current"] == 0
        assert b_progress[0]["per_strike"][0]["current"] == 0
        assert b_progress[0]["per_strike"][0]["active"] is False

        # A is UNCHANGED by B's evaluation.
        a_awards_after = consecutive_awards(user_a.id, rule.id)
        assert len(a_awards_after) == 1, "A's awards must not change when B is evaluated"
        assert consecutive_award_total(user_a.id, rule.id) == 500
        a_run = svc._strike_consecutive_run(user_a.id, strike, _now())
        assert a_run[0] == 6, f"A's run must remain 6, got {a_run[0]}"


# ===========================================================================
# 2. CROSS-STRIKE ISOLATION (filter by streak_rule_id)
# ===========================================================================


def test_invariant_cross_strike_isolation(app, db, sample_user):
    """One user with two strikes X (many achievements) and Y (few): Y's run counts
    ONLY Y, never inflated by X; an ``all`` rule over [X, Y] uses combined=min(X,Y)
    and withholds while Y < N.

    Spec §5.1 step 1 (filter by ``streak_rule_id``) and §5.2 (``all`` = min).
    """
    with app.app_context():
        program = get_or_create_default_program()
        strike_x = make_strike_rule(program, name="X", window_days=30, bonus_points=100)
        strike_y = make_strike_rule(program, name="Y", window_days=30, bonus_points=100)
        rule = make_consecutive_rule(
            program, strikes=[strike_x, strike_y], name="X-and-Y",
            required_consecutive=6, combine_mode="all", bonus_points=500,
        )

        # X: 10 consecutive achievements. Y: only 2.
        seed_consecutive_run(sample_user.id, strike_x, count=10, spacing_days=30)
        seed_consecutive_run(sample_user.id, strike_y, count=2, spacing_days=30)

        svc = _svc()
        now = _now()

        x_run, _ = svc._strike_consecutive_run(sample_user.id, strike_x, now)
        y_run, _ = svc._strike_consecutive_run(sample_user.id, strike_y, now)
        assert x_run == 10, f"X run must be exactly 10 (its own achievements), got {x_run}"
        assert y_run == 2, f"Y run must be exactly 2 — NOT inflated by X's 10, got {y_run}"

        # 'all' rule: combined = min(10, 2) = 2 < N=6 -> withheld.
        result = svc.update_consecutive_strikes(sample_user.id)
        assert result is False, "combine_mode=all with weak link Y(2) < N(6) must award nothing"
        assert consecutive_award_total(sample_user.id, rule.id) == 0

        progress = svc.get_consecutive_strike_progress(sample_user.id)
        assert len(progress) == 1
        # combined_current is capped at N; min(10,2)=2 < N so it shows 2.
        assert progress[0]["combined_current"] == 2, (
            f"combined_current must be min(X,Y) capped at N = 2, got {progress[0]['combined_current']}"
        )
        per = {p["strike_name"]: p["current"] for p in progress[0]["per_strike"]}
        # per-strike currents are each capped at N=6: X=min(10,6)=6, Y=min(2,6)=2.
        assert per["X"] == 6, f"X per-strike current capped at N=6, got {per['X']}"
        assert per["Y"] == 2, f"Y per-strike current, got {per['Y']}"


# ===========================================================================
# 3. NATURAL-PROGRESSION IDEMPOTENCY (the core money-correctness invariant)
# ===========================================================================


def _grow_single_strike_progression(user_id, strike, rule, periods, N, bonus, spacing):
    """Grow ONE strike one window-period at a time, re-evaluating after EACH new
    achievement (exactly as the real system does on each delivery).  Assert the
    cumulative award total == floor(run/N)*bonus at every step, milestones strictly
    [1,2,3,...], and no double-pay.
    """
    svc = LoyaltyService()
    now = _now()
    for run_len in range(1, periods + 1):
        # Reseed the whole run so the newest achievement is freshly placed; older
        # ones keep their spacing (gap < 2*W -> consecutive).  This mirrors a run
        # that has grown to ``run_len`` achievements.
        for txn in list(LoyaltyTransaction.query.filter_by(user_id=user_id).all()):
            ed = txn.extra_data or {}
            if ed.get("action_type") == LoyaltyActionType.STREAK_BONUS.value and ed.get("streak_rule_id") == strike.id:
                _db.session.delete(txn)
        _db.session.commit()
        seed_consecutive_run(user_id, strike, count=run_len, now=now, spacing_days=spacing)

        svc.update_consecutive_strikes(user_id)

        expected_milestones = run_len // N
        awards = consecutive_awards(user_id, rule.id)
        assert len(awards) == expected_milestones, (
            f"run_len={run_len}: expected floor({run_len}/{N})={expected_milestones} awards, got {len(awards)}"
        )
        milestones = sorted(a.extra_data["milestone"] for a in awards)
        assert milestones == list(range(1, expected_milestones + 1)), (
            f"run_len={run_len}: milestones must be strictly {list(range(1, expected_milestones + 1))}, got {milestones}"
        )
        assert consecutive_award_total(user_id, rule.id) == expected_milestones * bonus, (
            f"run_len={run_len}: cumulative total must be {expected_milestones * bonus}"
        )


def test_invariant_natural_progression_single_strike(app, db, sample_user):
    """Single strike, N=3, grown across 2N+1=7 periods, re-evaluating each period.
    At every step cumulative total == floor(run/3)*bonus, milestones strictly
    [1,2,...], no double-pay.  Spec §5.3.
    """
    with app.app_context():
        program = get_or_create_default_program()
        strike = make_strike_rule(program, name="3 in 30", window_days=30, bonus_points=100)
        rule = make_consecutive_rule(
            program, strikes=[strike], name="Natural",
            required_consecutive=3, combine_mode="all", bonus_points=200,
        )
        _grow_single_strike_progression(
            sample_user.id, strike, rule, periods=7, N=3, bonus=200, spacing=30
        )
        # Final: run=7 -> floor(7/3)=2 awards, 400 pts.
        assert consecutive_award_total(sample_user.id, rule.id) == 400


def test_invariant_natural_progression_all_min_governs(app, db, sample_user):
    """combine_mode='all', two strikes advancing at DIFFERENT rates; the slower
    strike (min) governs the award count at every step.  Spec §5.2 (all=min).

    A advances every period (up to 7); B advances every OTHER period (up to ~4).
    After each pair of (A,B) achievement updates, combined=min(runA,runB) and
    floor(min/N) awards must hold.
    """
    with app.app_context():
        program = get_or_create_default_program()
        strike_a = make_strike_rule(program, name="A", window_days=30, bonus_points=100)
        strike_b = make_strike_rule(program, name="B", window_days=30, bonus_points=100)
        N = 2
        bonus = 300
        rule = make_consecutive_rule(
            program, strikes=[strike_a, strike_b], name="MinGoverns",
            required_consecutive=N, combine_mode="all", bonus_points=bonus,
        )

        svc = LoyaltyService()
        now = _now()
        a_run = 0
        b_run = 0
        # 8 steps: A advances every step; B advances on even steps only.
        for step in range(1, 9):
            a_run = step
            if step % 2 == 0:
                b_run = step // 2

            # Reseed both strikes to their current run lengths.
            for txn in list(LoyaltyTransaction.query.filter_by(user_id=sample_user.id).all()):
                ed = txn.extra_data or {}
                if ed.get("action_type") == LoyaltyActionType.STREAK_BONUS.value:
                    _db.session.delete(txn)
            _db.session.commit()
            seed_consecutive_run(sample_user.id, strike_a, count=a_run, now=now, spacing_days=30)
            if b_run > 0:
                seed_consecutive_run(sample_user.id, strike_b, count=b_run, now=now, spacing_days=30)

            svc.update_consecutive_strikes(sample_user.id)

            combined = min(a_run, b_run) if b_run > 0 else 0
            expected_awards = combined // N
            awards = consecutive_awards(sample_user.id, rule.id)
            assert len(awards) == expected_awards, (
                f"step={step} (A={a_run},B={b_run}): combined=min={combined}, "
                f"expected floor({combined}/{N})={expected_awards} awards, got {len(awards)}"
            )
            assert sorted(a.extra_data["milestone"] for a in awards) == list(range(1, expected_awards + 1))
            assert consecutive_award_total(sample_user.id, rule.id) == expected_awards * bonus


def test_invariant_natural_progression_any_max_governs(app, db, sample_user):
    """combine_mode='any', two strikes advancing at different rates; the faster
    strike (max) governs the award count at every step.  Spec §5.2 (any=max).
    """
    with app.app_context():
        program = get_or_create_default_program()
        strike_a = make_strike_rule(program, name="A", window_days=30, bonus_points=100)
        strike_b = make_strike_rule(program, name="B", window_days=30, bonus_points=100)
        N = 2
        bonus = 150
        rule = make_consecutive_rule(
            program, strikes=[strike_a, strike_b], name="MaxGoverns",
            required_consecutive=N, combine_mode="any", bonus_points=bonus,
        )

        svc = LoyaltyService()
        now = _now()
        a_run = 0
        b_run = 0
        for step in range(1, 9):
            a_run = step  # fast
            if step % 2 == 0:
                b_run = step // 2  # slow

            for txn in list(LoyaltyTransaction.query.filter_by(user_id=sample_user.id).all()):
                ed = txn.extra_data or {}
                if ed.get("action_type") == LoyaltyActionType.STREAK_BONUS.value:
                    _db.session.delete(txn)
            _db.session.commit()
            seed_consecutive_run(sample_user.id, strike_a, count=a_run, now=now, spacing_days=30)
            if b_run > 0:
                seed_consecutive_run(sample_user.id, strike_b, count=b_run, now=now, spacing_days=30)

            svc.update_consecutive_strikes(sample_user.id)

            combined = max(a_run, b_run)
            expected_awards = combined // N
            awards = consecutive_awards(sample_user.id, rule.id)
            assert len(awards) == expected_awards, (
                f"step={step} (A={a_run},B={b_run}): combined=max={combined}, "
                f"expected floor({combined}/{N})={expected_awards} awards, got {len(awards)}"
            )
            assert sorted(a.extra_data["milestone"] for a in awards) == list(range(1, expected_awards + 1))
            assert consecutive_award_total(sample_user.id, rule.id) == expected_awards * bonus


# ===========================================================================
# 4. POINTS EXPIRY of a consecutive bonus
# ===========================================================================


def test_invariant_consecutive_bonus_expiry_drawdown_and_no_reaward(app, db, sample_user):
    """A consecutive bonus lot expires; balance draws down correctly AND re-running
    update_consecutive_strikes does NOT re-award (idempotency is row-presence based,
    unaffected by expiry).

    Spec §5.3 (idempotency via ``_consecutive_awards_since``, which counts the
    BONUS ledger row regardless of its ``is_expired`` flag).
    """
    with app.app_context():
        program = get_or_create_default_program()
        _ensure_bronze_tier(program)
        # Short expiry window for this program.
        program.points_expiry_days = 10
        _db.session.commit()

        # Strike bonus_points=0 so the seeded strike achievements are NOT lots that
        # confound the balance — only the consecutive bonus contributes to balance.
        strike = make_strike_rule(program, name="3 in 30", window_days=30, bonus_points=0)
        rule = make_consecutive_rule(
            program, strikes=[strike], name="Expiring",
            required_consecutive=3, combine_mode="all", bonus_points=700,
        )

        seed_consecutive_run(sample_user.id, strike, count=3, spacing_days=30)
        # The seeded STREAK_BONUS rows carry 0 points (and 0 remaining_points), so
        # they contribute nothing to the balance — the only positive lot is the
        # consecutive bonus awarded next.
        seeded_total = sum(t.points for t in LoyaltyTransaction.query.filter_by(user_id=sample_user.id).all())
        assert seeded_total == 0, f"Precondition: seeded strike rows must be 0 pts, got {seeded_total}"

        svc = _svc()
        assert svc.update_consecutive_strikes(sample_user.id) is True
        awards = consecutive_awards(sample_user.id, rule.id)
        assert len(awards) == 1, f"Precondition: exactly 1 award, got {len(awards)}"
        bonus_lot = awards[0]
        assert bonus_lot.points == 700
        assert bonus_lot.remaining_points == 700

        # Balance reflects ONLY the live bonus (seeded strike rows are 0-point).
        bal_before = _balance(sample_user.id)
        assert bal_before == 700, f"Balance must include the 700 bonus, got {bal_before}"

        # Force the lot past expiry, then run the daily expiry sweep.
        bonus_lot.expires_at = _now() - timedelta(days=1)
        _db.session.commit()

        expiry_result = svc.expire_points()
        assert expiry_result["total_expired_points"] == 700, (
            f"Expected 700 expired, got {expiry_result['total_expired_points']}"
        )

        # Balance drew down to 0; the lot is flagged expired with zero remainder.
        _db.session.refresh(bonus_lot)
        assert bonus_lot.is_expired is True
        assert bonus_lot.remaining_points == 0
        assert _balance(sample_user.id) == 0, "Balance must draw down to 0 after expiry"

        # Re-evaluate: the BONUS row still exists (expired) so idempotency holds —
        # NO new award even though the balance is now 0.
        result = svc.update_consecutive_strikes(sample_user.id)
        assert result is False, "Expired bonus must not be re-awarded"
        awards_after = consecutive_awards(sample_user.id, rule.id)
        assert len(awards_after) == 1, (
            f"Idempotency is row-presence based: still exactly 1 award row, got {len(awards_after)}"
        )
        assert _balance(sample_user.id) == 0, "Still 0 — no re-award credited"


# ===========================================================================
# 5. TIER DOWNGRADE around a consecutive bonus
# ===========================================================================


def test_invariant_tier_upgrade_then_downgrade_after_lock_expires(app, db, sample_user):
    """A consecutive bonus pushes qualifying points over a Silver threshold
    (immediate upgrade + 365-day lock); then the lot expires so qualifying points
    fall below.  Downgrade is gated by the tier lock — assert exactly what is
    reachable (upgrade now; downgrade only once tier_valid_until < now).

    Spec/impl: _check_tier_upgrade — upgrade immediate + lock; downgrade only when
    lock expired AND qualifying points below current threshold.
    """
    with app.app_context():
        program = get_or_create_default_program()
        # Two tiers: Bronze (0+), Silver (>=500).
        if not LoyaltyTierConfig.query.filter_by(program_id=program.id, name="Bronze").first():
            _db.session.add(
                LoyaltyTierConfig(
                    program_id=program.id, name="Bronze", display_order=0,
                    min_points=0, max_points=499, points_multiplier=1.0, is_active=True,
                )
            )
        if not LoyaltyTierConfig.query.filter_by(program_id=program.id, name="Silver").first():
            _db.session.add(
                LoyaltyTierConfig(
                    program_id=program.id, name="Silver", display_order=1,
                    min_points=500, max_points=None, points_multiplier=1.0, is_active=True,
                )
            )
        program.points_expiry_days = 10
        _db.session.commit()

        # Strike bonus_points=0 so the ONLY qualifying points come from the
        # consecutive bonus (600) — keeps the tier math unambiguous.
        strike = make_strike_rule(program, name="3 in 30", window_days=30, bonus_points=0)
        rule = make_consecutive_rule(
            program, strikes=[strike], name="TierMaker",
            required_consecutive=3, combine_mode="all", bonus_points=600,
        )

        seed_consecutive_run(sample_user.id, strike, count=3, spacing_days=30)

        svc = _svc()
        assert svc.update_consecutive_strikes(sample_user.id) is True

        account = LoyaltyPoints.query.filter_by(user_id=sample_user.id).first()
        _db.session.refresh(account)
        # 600 qualifying points (the bonus is BONUS within 365d) >= 500 -> Silver.
        assert svc.calculate_qualifying_points(sample_user.id) == 600
        assert account.current_tier == "Silver", (
            f"Bonus of 600 must upgrade to Silver, got {account.current_tier}"
        )
        assert account.tier_valid_until is not None, "Upgrade must lock the tier for 365 days"
        lock_until = ensure_utc(account.tier_valid_until)
        assert lock_until > _now(), "Lock must be in the future"

        # Expire the bonus lot -> qualifying points fall to 0 (< 500).
        bonus_lot = consecutive_awards(sample_user.id, rule.id)[0]
        bonus_lot.expires_at = _now() - timedelta(days=1)
        _db.session.commit()
        svc.expire_points()
        # Note: expiry flags the lot but qualifying-points = SUM(points) over the
        # 365d window regardless of is_expired, so still 600 right now. The
        # downgrade gate is the LOCK, not the balance — assert downgrade is blocked
        # while the lock holds.
        svc.check_tier_expiration(sample_user.id)
        _db.session.refresh(account)
        assert account.current_tier == "Silver", (
            "Downgrade must be BLOCKED while the 365-day tier lock holds"
        )

        # Now expire the lock: tier_valid_until in the past AND make qualifying
        # points fall below 500 by backdating the bonus row out of the 365d window.
        account.tier_valid_until = _now() - timedelta(days=1)
        bonus_lot.created_at = _now() - timedelta(days=400)  # outside 365d tier window
        _db.session.commit()
        assert svc.calculate_qualifying_points(sample_user.id) == 0, (
            "Backdated bonus must drop out of the 365-day qualifying window"
        )

        svc.check_tier_expiration(sample_user.id)
        _db.session.refresh(account)
        assert account.current_tier == "Bronze", (
            f"With lock expired AND qualifying points (0) < Silver threshold, must "
            f"downgrade to Bronze, got {account.current_tier}"
        )


# ===========================================================================
# 6. bonus_points = 0 BOUNDARY
# ===========================================================================


def test_invariant_bonus_points_zero_no_spurious_row(app, db, sample_user):
    """A consecutive rule with bonus_points=0 (constructible at model level; the API
    now rejects it, but make_consecutive_rule bypasses the API).  Reaching N:
    update_consecutive_strikes must silently skip the rule (no award, no crash)
    rather than calling award_points(0) which would raise ValidationError.

    Fix 2(b): a guard ``if rule.bonus_points <= 0: continue`` in
    update_consecutive_strikes makes this a safe no-op.
    """
    with app.app_context():
        program = get_or_create_default_program()
        _ensure_bronze_tier(program)
        strike = make_strike_rule(program, name="3 in 30", window_days=30, bonus_points=100)
        rule = make_consecutive_rule(
            program, strikes=[strike], name="Zero Bonus",
            required_consecutive=3, combine_mode="all", bonus_points=0,
        )

        seed_consecutive_run(sample_user.id, strike, count=3, spacing_days=30)

        svc = _svc()
        # The rule is silently skipped — no exception raised, returns False.
        result = svc.update_consecutive_strikes(sample_user.id)
        assert result is False, "update_consecutive_strikes must return False when no award is made"

        # No spurious 0-point CONSECUTIVE_STREAK_BONUS row was persisted.
        awards = consecutive_awards(sample_user.id, rule.id)
        assert len(awards) == 0, (
            f"bonus_points=0 must not create a ledger row, got {len(awards)}"
        )
        assert consecutive_award_total(sample_user.id, rule.id) == 0


# ===========================================================================
# 7. SECOND REAL DELIVERY repeat-every-N
# ===========================================================================


def test_invariant_two_real_deliveries_repeat_every_n(app, db, sample_user):
    """Milestone 1 fires on a real delivery completing strike #N; after another full
    window, milestone 2 fires on a second real delivery completing strike #2N — all
    through the real update_streak -> update_consecutive_strikes path across two real
    deliveries (strike cooldown respected, run anchored).

    Spec §5.3 (repeat every N) + §5.4 (the only trigger is a new strike achievement,
    evaluated inside update_streak).

    Mechanics deterministically managed here:
    - Each real delivery (3 DELIVERED orders in the 30-day window) fires EXACTLY one
      new STREAK_BONUS achievement via update_streak, then evaluates the consecutive
      rule in the same transaction.
    - The strike's own cooldown (no second STREAK_BONUS within ``window_days``) means
      a real delivery only fires a NEW achievement if no STREAK_BONUS sits inside the
      trailing 30 days. Before delivery #2 we therefore backdate ALL existing
      STREAK_BONUS rows to >30 days ago (still spaced <2*W=60d apart so the run stays
      consecutive), clearing the cooldown so delivery #2 fires the next achievement.

    The strike carries positive bonus_points (a real delivery's update_streak path
    calls award_points, which rejects a 0-point award) — but this test asserts only
    CONSECUTIVE bonus counts/totals, which the strike's own points never touch.

    Progression (N=2):
      - pre-seed 2 backdated achievements (run=2 — but no consecutive rule evaluation
        has run yet, so no award),
      - delivery #1 fires achievement #3 -> run=3 -> floor(3/2)=1 -> milestone 1,
      - (advance a window; clear cooldown by backdating)
      - delivery #2 fires achievement #4 -> run=4 -> floor(4/2)=2 -> milestone 2.
    """
    with app.app_context():
        program = get_or_create_default_program()
        _ensure_bronze_tier(program)

        # Strike: 3 DELIVERED orders within 30 days fires one achievement. Positive
        # bonus_points is REQUIRED here — the real-delivery update_streak path calls
        # award_points which rejects a 0-point award.
        strike = make_strike_rule(
            program, name="3 in 30",
            required_orders=3, window_days=30, bonus_points=150,
        )
        # N=2 -> a milestone fires every 2 consecutive strike achievements.
        rule = make_consecutive_rule(
            program, strikes=[strike], name="Two-Real",
            required_consecutive=2, combine_mode="all", bonus_points=400,
        )

        now = _now()
        # Pre-seed TWO backdated strike achievements (run=2), spaced 30d (<2*W=60 ->
        # consecutive). They sit at ~75d and ~45d ago so neither is inside the 30-day
        # cooldown window of delivery #1.
        seed_strike_achievement(sample_user.id, strike, when=now - timedelta(days=75))
        seed_strike_achievement(sample_user.id, strike, when=now - timedelta(days=45))

        order_service = OrderService(inventory_service=None)

        # --- Real delivery #1: fires achievement #3 -> run=3 -> milestone 1 ---
        # Seed 2 prerequisite DELIVERED orders inside the trailing 30d window so the
        # final real delivery makes 3 -> the strike fires.
        seed_delivered_orders(sample_user.id, count=2, newest_days_ago=2, spacing_days=5)
        deliver_paid_order(
            order_service, user_id=sample_user.id, total=Decimal("50000"),
            when=now, payment="prepaid", product=None,
        )

        strike_rows_1 = [
            t for t in LoyaltyTransaction.query.filter_by(user_id=sample_user.id).all()
            if (t.extra_data or {}).get("action_type") == LoyaltyActionType.STREAK_BONUS.value
            and (t.extra_data or {}).get("streak_rule_id") == strike.id
        ]
        assert len(strike_rows_1) == 3, (
            f"After delivery #1 there must be 3 strike achievements (2 seeded + 1 real), "
            f"got {len(strike_rows_1)}"
        )
        awards_1 = consecutive_awards(sample_user.id, rule.id)
        assert len(awards_1) == 1, (
            f"Delivery #1 (run=3, N=2 -> floor(3/2)=1) must fire milestone 1, got {len(awards_1)}"
        )
        assert awards_1[0].extra_data["milestone"] == 1
        assert consecutive_award_total(sample_user.id, rule.id) == 400

        # --- Advance a window: clear the strike cooldown so delivery #2 can fire a
        # NEW achievement. Backdate every existing STREAK_BONUS row to >30d ago,
        # keeping them spaced 20d apart (<2*W=60 -> still a single consecutive run). ---
        rows_sorted = sorted(strike_rows_1, key=lambda r: ensure_utc(r.created_at))
        for i, t in enumerate(rows_sorted):
            # oldest -> ~95d ago, ... newest -> ~35d ago (>30d cooldown, gaps 20d <60)
            t.created_at = now - timedelta(days=95 - i * 20)
        _db.session.commit()

        # --- Real delivery #2: fires achievement #4 -> run=4 -> milestone 2 ---
        seed_delivered_orders(sample_user.id, count=2, newest_days_ago=1, spacing_days=3)
        deliver_paid_order(
            order_service, user_id=sample_user.id, total=Decimal("50000"),
            when=_now(), payment="prepaid", product=None,
        )

        strike_rows_2 = [
            t for t in LoyaltyTransaction.query.filter_by(user_id=sample_user.id).all()
            if (t.extra_data or {}).get("action_type") == LoyaltyActionType.STREAK_BONUS.value
            and (t.extra_data or {}).get("streak_rule_id") == strike.id
        ]
        # 3 (after delivery #1) + 1 (delivery #2 real, cooldown cleared) = 4
        assert len(strike_rows_2) == 4, (
            f"After delivery #2 there must be 4 strike achievements, got {len(strike_rows_2)}"
        )

        # run = 4, N = 2 -> floor(4/2)=2 -> milestone 2 fires on the second delivery.
        awards_2 = consecutive_awards(sample_user.id, rule.id)
        assert len(awards_2) == 2, (
            f"Delivery #2 (run=4, N=2) must fire milestone 2 (total 2 awards), got {len(awards_2)}"
        )
        assert sorted(a.extra_data["milestone"] for a in awards_2) == [1, 2]
        assert consecutive_award_total(sample_user.id, rule.id) == 800  # 2 * 400
