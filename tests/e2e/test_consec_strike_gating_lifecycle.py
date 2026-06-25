"""E2E tests: consecutive-strike bonus rule gating & lifecycle.

Covers dimension: gating_lifecycle (cases 01-20).
All expected values are anchored to the spec at:
  docs/superpowers/specs/2026-06-24-consecutive-strike-bonus-rule-design.md

Key invariants:
- Reward = BONUS transaction with action_type=consecutive_streak_bonus.
- Achievement = EARNED transaction with action_type=streak_bonus + streak_rule_id.
- Consecutive run: gap < 2*W days keeps the run alive; gap >= 2*W resets to 0.
- combine_mode='all' → min(per-strike runs); 'any' → max.
- repeat-every-N: target_awards = combined // N; idempotent via _consecutive_awards_since.
- Gating: is_active + is_effective(now) + >=1 attached strike + program exists.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from business_app import db as _db
from business_app.models.loyalty import (
    LoyaltyConsecutiveStrikeRule,
    LoyaltyProgram,
    LoyaltyTransaction,
)
from business_app.services.loyalty_service import LoyaltyService
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


# ---------------------------------------------------------------------------
# Autouse: silence loyalty notification side-effects for every test
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _silence(monkeypatch):
    silence_loyalty_notifications(monkeypatch)


# ---------------------------------------------------------------------------
# Helper: get the LoyaltyService instance
# ---------------------------------------------------------------------------


def _svc() -> LoyaltyService:
    return LoyaltyService()


# ===========================================================================
# Case gating_lifecycle-01
# ===========================================================================


@pytest.mark.e2e
def test_gating_lifecycle_01_inactive_rule_is_silently_skipped(app, db, sample_user):
    """is_active=False rule is silently skipped — no award and no crash."""
    with app.app_context():
        program = get_or_create_default_program()
        strike_a = make_strike_rule(program, name="A", window_days=30, bonus_points=50)
        consec_rule = make_consecutive_rule(
            program,
            strikes=[strike_a],
            name="Inactive Rule",
            required_consecutive=3,
            bonus_points=500,
            is_active=False,  # <-- inactive
        )

        # Seed 3 consecutive achievements for A
        seed_consecutive_run(sample_user.id, strike_a, count=3)

        result = _svc().update_consecutive_strikes(sample_user.id, commit=True)

        assert result is False, "update_consecutive_strikes must return False for inactive rule"
        awards = consecutive_awards(sample_user.id, consec_rule.id)
        assert len(awards) == 0, "No CONSECUTIVE_STREAK_BONUS awards expected for inactive rule"


# ===========================================================================
# Case gating_lifecycle-02
# ===========================================================================


@pytest.mark.e2e
def test_gating_lifecycle_02_starts_at_future_not_effective(app, db, sample_user):
    """starts_at in the future — rule not yet effective, no award."""
    with app.app_context():
        program = get_or_create_default_program()
        strike_a = make_strike_rule(program, name="A", window_days=30, bonus_points=50)
        now = datetime.now(timezone.utc)
        consec_rule = make_consecutive_rule(
            program,
            strikes=[strike_a],
            name="Future Rule",
            required_consecutive=3,
            bonus_points=500,
            is_active=True,
            starts_at=now + timedelta(days=1),  # future start
            ends_at=None,
        )

        seed_consecutive_run(sample_user.id, strike_a, count=3)

        result = _svc().update_consecutive_strikes(sample_user.id, commit=True)

        assert result is False, "starts_at in the future → is_effective=False → no award"
        awards = consecutive_awards(sample_user.id, consec_rule.id)
        assert len(awards) == 0


# ===========================================================================
# Case gating_lifecycle-03
# ===========================================================================


@pytest.mark.e2e
def test_gating_lifecycle_03_ends_at_past_expired_no_award(app, db, sample_user):
    """ends_at in the past — rule expired, no award."""
    with app.app_context():
        program = get_or_create_default_program()
        strike_a = make_strike_rule(program, name="A", window_days=30, bonus_points=50)
        now = datetime.now(timezone.utc)
        consec_rule = make_consecutive_rule(
            program,
            strikes=[strike_a],
            name="Expired Rule",
            required_consecutive=3,
            bonus_points=500,
            is_active=True,
            starts_at=None,
            ends_at=now - timedelta(seconds=1),  # already past
        )

        seed_consecutive_run(sample_user.id, strike_a, count=3)

        result = _svc().update_consecutive_strikes(sample_user.id, commit=True)

        assert result is False, "ends_at in the past → is_effective=False → no award"
        awards = consecutive_awards(sample_user.id, consec_rule.id)
        assert len(awards) == 0


# ===========================================================================
# Case gating_lifecycle-04
# ===========================================================================


@pytest.mark.e2e
def test_gating_lifecycle_04_starts_at_boundary_in_past_awards(app, db, sample_user):
    """starts_at boundary: rule effective exactly at starts_at — awards."""
    with app.app_context():
        program = get_or_create_default_program()
        strike_a = make_strike_rule(program, name="A", window_days=30, bonus_points=50)
        # starts_at is 1 second in the past (just became effective)
        t0 = datetime.now(timezone.utc) - timedelta(seconds=1)
        consec_rule = make_consecutive_rule(
            program,
            strikes=[strike_a],
            name="Boundary Rule",
            required_consecutive=3,
            bonus_points=500,
            is_active=True,
            starts_at=t0,
            ends_at=None,
        )

        seed_consecutive_run(sample_user.id, strike_a, count=3)

        result = _svc().update_consecutive_strikes(sample_user.id, commit=True)

        assert result is True, "starts_at in the past → is_effective=True → should award"
        awards = consecutive_awards(sample_user.id, consec_rule.id)
        assert len(awards) == 1, "Exactly 1 CONSECUTIVE_STREAK_BONUS award expected"
        assert awards[0].points == 500, "Award must be 500 bonus_points"
        assert (awards[0].extra_data or {}).get("milestone") == 1, "milestone must be 1"
        assert (awards[0].extra_data or {}).get("consecutive_strike_rule_id") == consec_rule.id


# ===========================================================================
# Case gating_lifecycle-05
# ===========================================================================


@pytest.mark.e2e
def test_gating_lifecycle_05_zero_attached_strikes_no_award(app, db, sample_user):
    """Zero attached strikes — rule skipped, no crash."""
    with app.app_context():
        program = get_or_create_default_program()
        # Construct rule directly with empty strikes list
        rule = LoyaltyConsecutiveStrikeRule(
            program_id=program.id,
            name="Empty Strikes Rule",
            required_consecutive=3,
            combine_mode="all",
            bonus_points=500,
            is_active=True,
        )
        rule.strikes = []
        _db.session.add(rule)
        _db.session.commit()

        result = _svc().update_consecutive_strikes(sample_user.id, commit=True)

        assert result is False, "Rule with zero attached strikes must be skipped"
        awards = consecutive_awards(sample_user.id, rule.id)
        assert len(awards) == 0


# ===========================================================================
# Case gating_lifecycle-06
# ===========================================================================


@pytest.mark.e2e
def test_gating_lifecycle_06_no_active_program_returns_false(app, db, sample_user):
    """No default+active program — update_consecutive_strikes returns False immediately."""
    with app.app_context():
        # Ensure NO active programs exist (fresh db, so none yet)
        assert LoyaltyProgram.query.filter_by(is_active=True).first() is None

        result = _svc().update_consecutive_strikes(sample_user.id, commit=True)

        assert result is False, "No active program → immediate False return"
        awards_count = LoyaltyTransaction.query.filter_by(user_id=sample_user.id).count()
        # No transactions should have been created
        from business_app.utils.constants import LoyaltyActionType
        bonus_awards = [
            t for t in LoyaltyTransaction.query.filter_by(user_id=sample_user.id).all()
            if (t.extra_data or {}).get("action_type")
            == LoyaltyActionType.CONSECUTIVE_STREAK_BONUS.value
        ]
        assert len(bonus_awards) == 0


# ===========================================================================
# Case gating_lifecycle-07
# ===========================================================================


@pytest.mark.e2e
def test_gating_lifecycle_07_admin_deactivates_rule_stops_awards(
    app, db, sample_user, client, admin_auth_headers
):
    """Admin deactivates rule via PUT — next evaluation awards nothing."""
    with app.app_context():
        program = get_or_create_default_program()
        strike_a = make_strike_rule(program, name="A", window_days=30, bonus_points=50)
        consec_rule = make_consecutive_rule(
            program,
            strikes=[strike_a],
            name="Active→Inactive",
            required_consecutive=3,
            bonus_points=500,
            is_active=True,
        )

        seed_consecutive_run(sample_user.id, strike_a, count=3)

        # Deactivate via admin API
        resp = client.put(
            f"/api/v1/admin/loyalty/consecutive-strike-rules/{consec_rule.id}",
            data=json.dumps({"is_active": False}),
            headers=admin_auth_headers,
        )
        assert resp.status_code == 200, f"PUT should return 200, got {resp.status_code}: {resp.data}"
        resp_data = json.loads(resp.data)
        assert resp_data["data"]["consecutive_strike_rule"]["is_active"] is False

        # Now update_consecutive_strikes must skip the deactivated rule
        result = _svc().update_consecutive_strikes(sample_user.id, commit=True)

        assert result is False, "Deactivated rule must not award"
        awards = consecutive_awards(sample_user.id, consec_rule.id)
        assert len(awards) == 0


# ===========================================================================
# Case gating_lifecycle-08
# ===========================================================================


@pytest.mark.e2e
def test_gating_lifecycle_08_admin_increases_required_consec_no_award(
    app, db, sample_user, client, admin_auth_headers
):
    """Admin increases required_consecutive from 3 to 6 — run=3 no longer qualifies."""
    with app.app_context():
        program = get_or_create_default_program()
        strike_a = make_strike_rule(program, name="A", window_days=30, bonus_points=50)
        consec_rule = make_consecutive_rule(
            program,
            strikes=[strike_a],
            name="Threshold Up",
            required_consecutive=3,
            bonus_points=500,
        )

        # Seed exactly 3 consecutive achievements
        seed_consecutive_run(sample_user.id, strike_a, count=3)

        # Raise threshold to 6 via PUT
        resp = client.put(
            f"/api/v1/admin/loyalty/consecutive-strike-rules/{consec_rule.id}",
            data=json.dumps({"required_consecutive": 6}),
            headers=admin_auth_headers,
        )
        assert resp.status_code == 200
        resp_data = json.loads(resp.data)
        assert resp_data["data"]["consecutive_strike_rule"]["required_consecutive"] == 6

        result = _svc().update_consecutive_strikes(sample_user.id, commit=True)

        assert result is False, "combined=3 < n=6 → no award"
        awards = consecutive_awards(sample_user.id, consec_rule.id)
        assert len(awards) == 0


# ===========================================================================
# Case gating_lifecycle-09
# ===========================================================================


@pytest.mark.e2e
def test_gating_lifecycle_09_admin_decreases_required_consec_now_awards(
    app, db, sample_user, client, admin_auth_headers
):
    """Admin decreases required_consecutive from 6 to 3 — previously insufficient run now awards."""
    with app.app_context():
        program = get_or_create_default_program()
        strike_a = make_strike_rule(program, name="A", window_days=30, bonus_points=50)
        consec_rule = make_consecutive_rule(
            program,
            strikes=[strike_a],
            name="Threshold Down",
            required_consecutive=6,  # start high
            bonus_points=500,
        )

        # Seed 3 achievements (insufficient at n=6)
        seed_consecutive_run(sample_user.id, strike_a, count=3)

        # Verify no award fires at n=6
        result_before = _svc().update_consecutive_strikes(sample_user.id, commit=True)
        assert result_before is False

        # Lower threshold to 3
        resp = client.put(
            f"/api/v1/admin/loyalty/consecutive-strike-rules/{consec_rule.id}",
            data=json.dumps({"required_consecutive": 3}),
            headers=admin_auth_headers,
        )
        assert resp.status_code == 200
        resp_data = json.loads(resp.data)
        assert resp_data["data"]["consecutive_strike_rule"]["required_consecutive"] == 3

        result = _svc().update_consecutive_strikes(sample_user.id, commit=True)

        assert result is True, "combined=3 >= n=3 → should award"
        awards = consecutive_awards(sample_user.id, consec_rule.id)
        assert len(awards) == 1, "Exactly 1 award expected"
        assert awards[0].points == 500
        assert (awards[0].extra_data or {}).get("milestone") == 1


# ===========================================================================
# Case gating_lifecycle-10
# ===========================================================================


@pytest.mark.e2e
def test_gating_lifecycle_10_switch_combine_mode_all_to_any_awards(
    app, db, sample_user, client, admin_auth_headers
):
    """Admin switches combine_mode from 'all' to 'any' — previously unqualified run now awards."""
    with app.app_context():
        program = get_or_create_default_program()
        strike_a = make_strike_rule(program, name="A", window_days=30, bonus_points=50)
        strike_b = make_strike_rule(program, name="B", window_days=30, bonus_points=50)
        consec_rule = make_consecutive_rule(
            program,
            strikes=[strike_a, strike_b],
            name="All→Any",
            required_consecutive=3,
            combine_mode="all",  # needs min(run_A, run_B) >= 3
            bonus_points=500,
        )

        # Seed 3 for A, 1 for B → combined(all)=min(3,1)=1 < 3
        seed_consecutive_run(sample_user.id, strike_a, count=3)
        seed_consecutive_run(sample_user.id, strike_b, count=1)

        # Verify no award under 'all' mode
        result_before = _svc().update_consecutive_strikes(sample_user.id, commit=True)
        assert result_before is False, "combine_mode=all: min(3,1)=1 < 3, no award expected"

        # Switch to 'any'
        resp = client.put(
            f"/api/v1/admin/loyalty/consecutive-strike-rules/{consec_rule.id}",
            data=json.dumps({"combine_mode": "any"}),
            headers=admin_auth_headers,
        )
        assert resp.status_code == 200
        resp_data = json.loads(resp.data)
        assert resp_data["data"]["consecutive_strike_rule"]["combine_mode"] == "any"

        result = _svc().update_consecutive_strikes(sample_user.id, commit=True)

        assert result is True, "combine_mode=any: max(3,1)=3 >= 3, should award"
        awards = consecutive_awards(sample_user.id, consec_rule.id)
        assert len(awards) == 1
        assert awards[0].points == 500
        assert (awards[0].extra_data or {}).get("milestone") == 1


# ===========================================================================
# Case gating_lifecycle-11
# ===========================================================================


@pytest.mark.e2e
def test_gating_lifecycle_11_switch_combine_mode_any_to_all_no_award(
    app, db, sample_user, client, admin_auth_headers
):
    """Admin switches combine_mode from 'any' to 'all' — award that would have fired no longer fires."""
    with app.app_context():
        program = get_or_create_default_program()
        strike_a = make_strike_rule(program, name="A", window_days=30, bonus_points=50)
        strike_b = make_strike_rule(program, name="B", window_days=30, bonus_points=50)
        consec_rule = make_consecutive_rule(
            program,
            strikes=[strike_a, strike_b],
            name="Any→All",
            required_consecutive=3,
            combine_mode="any",  # max(run_A, run_B) >= 3 would qualify
            bonus_points=500,
        )

        # Seed 3 for A, 0 for B → combined(any)=max(3,0)=3 >= 3 would award
        seed_consecutive_run(sample_user.id, strike_a, count=3)
        # No achievements for B

        # Switch to 'all' BEFORE calling update
        resp = client.put(
            f"/api/v1/admin/loyalty/consecutive-strike-rules/{consec_rule.id}",
            data=json.dumps({"combine_mode": "all"}),
            headers=admin_auth_headers,
        )
        assert resp.status_code == 200
        resp_data = json.loads(resp.data)
        assert resp_data["data"]["consecutive_strike_rule"]["combine_mode"] == "all"

        result = _svc().update_consecutive_strikes(sample_user.id, commit=True)

        # combined(all)=min(3,0)=0 < 3 → no award
        assert result is False, "combine_mode=all: min(3,0)=0 < 3, no award"
        awards = consecutive_awards(sample_user.id, consec_rule.id)
        assert len(awards) == 0


# ===========================================================================
# Case gating_lifecycle-12
# ===========================================================================


@pytest.mark.e2e
def test_gating_lifecycle_12_admin_detach_all_strikes_api_rejects(
    app, db, sample_user, client, admin_auth_headers
):
    """Admin detaches only strike via PUT with empty list — API rejects with 400/422."""
    with app.app_context():
        program = get_or_create_default_program()
        strike_a = make_strike_rule(program, name="A", window_days=30, bonus_points=50)
        consec_rule = make_consecutive_rule(
            program,
            strikes=[strike_a],
            name="No Empty Strikes",
            required_consecutive=3,
            bonus_points=500,
        )

        # Attempt to clear all attached strikes
        resp = client.put(
            f"/api/v1/admin/loyalty/consecutive-strike-rules/{consec_rule.id}",
            data=json.dumps({"strike_rule_ids": []}),
            headers=admin_auth_headers,
        )

        assert resp.status_code in (400, 422), (
            f"API must reject empty strike_rule_ids, got {resp.status_code}: {resp.data}"
        )
        resp_data = json.loads(resp.data)
        # validation_error_response returns {"message": "Validation failed", "errors": [<detail>...]}.
        # The specific rejection reason is in the "errors" list, not in "message".
        errors_list = resp_data.get("errors") or []
        error_detail = " ".join(str(e) for e in errors_list).lower()
        assert "least one" in error_detail or "strike" in error_detail, (
            f"errors list must mention the validation issue, got errors={errors_list!r}"
        )

        # Rule must still have strike A attached
        _db.session.expire_all()
        reloaded = LoyaltyConsecutiveStrikeRule.query.get(consec_rule.id)
        assert len(reloaded.strikes) == 1
        assert reloaded.strikes[0].id == strike_a.id


# ===========================================================================
# Case gating_lifecycle-13
# ===========================================================================


@pytest.mark.e2e
def test_gating_lifecycle_13_admin_reattach_different_strike_evaluates_new_set(
    app, db, sample_user, client, admin_auth_headers
):
    """Admin detaches B, keeps A only — evaluation uses new strike set and now awards."""
    with app.app_context():
        program = get_or_create_default_program()
        strike_a = make_strike_rule(program, name="A", window_days=30, bonus_points=50)
        strike_b = make_strike_rule(program, name="B", window_days=30, bonus_points=50)
        consec_rule = make_consecutive_rule(
            program,
            strikes=[strike_a, strike_b],
            name="Reattach Test",
            required_consecutive=3,
            combine_mode="all",
            bonus_points=500,
        )

        # Seed 3 for A, 0 for B → combined(all)=min(3,0)=0 < 3 → no award
        seed_consecutive_run(sample_user.id, strike_a, count=3)
        result_before = _svc().update_consecutive_strikes(sample_user.id, commit=True)
        assert result_before is False

        # Re-attach only A (detach B)
        resp = client.put(
            f"/api/v1/admin/loyalty/consecutive-strike-rules/{consec_rule.id}",
            data=json.dumps({"strike_rule_ids": [strike_a.id]}),
            headers=admin_auth_headers,
        )
        assert resp.status_code == 200
        resp_data = json.loads(resp.data)
        assert resp_data["data"]["consecutive_strike_rule"]["strike_rule_ids"] == [strike_a.id]

        result = _svc().update_consecutive_strikes(sample_user.id, commit=True)

        # Now combined=min([3])=3 >= n=3
        assert result is True, "After detaching B, combined=run_A=3 >= 3 → should award"
        awards = consecutive_awards(sample_user.id, consec_rule.id)
        assert len(awards) == 1
        assert awards[0].points == 500
        assert (awards[0].extra_data or {}).get("milestone") == 1


# ===========================================================================
# Case gating_lifecycle-14
# ===========================================================================


@pytest.mark.e2e
def test_gating_lifecycle_14_delete_rule_no_more_awards(
    app, db, sample_user, client, admin_auth_headers
):
    """DELETE rule — subsequent evaluation awards nothing, no crash."""
    with app.app_context():
        program = get_or_create_default_program()
        strike_a = make_strike_rule(program, name="A", window_days=30, bonus_points=50)
        consec_rule = make_consecutive_rule(
            program,
            strikes=[strike_a],
            name="To Be Deleted",
            required_consecutive=3,
            bonus_points=500,
        )

        seed_consecutive_run(sample_user.id, strike_a, count=3)

        # First call: confirm 1 award fires
        result1 = _svc().update_consecutive_strikes(sample_user.id, commit=True)
        assert result1 is True
        awards_before = consecutive_awards(sample_user.id, consec_rule.id)
        assert len(awards_before) == 1

        rule_id = consec_rule.id

        # Delete the rule via admin API
        resp = client.delete(
            f"/api/v1/admin/loyalty/consecutive-strike-rules/{rule_id}",
            headers=admin_auth_headers,
        )
        assert resp.status_code == 200

        # Confirm rule is gone
        assert LoyaltyConsecutiveStrikeRule.query.get(rule_id) is None

        # Second call: must not crash and must not award
        result2 = _svc().update_consecutive_strikes(sample_user.id, commit=True)
        assert result2 is False, "Deleted rule → no rules for program → False"

        # No new CONSECUTIVE_STREAK_BONUS rows (existing 1 row remains orphaned)
        from business_app.utils.constants import LoyaltyActionType
        all_bonus = [
            t for t in LoyaltyTransaction.query.filter_by(user_id=sample_user.id).all()
            if (t.extra_data or {}).get("action_type")
            == LoyaltyActionType.CONSECUTIVE_STREAK_BONUS.value
            and (t.extra_data or {}).get("consecutive_strike_rule_id") == rule_id
        ]
        # Only the pre-delete award remains; no new awards after delete
        assert len(all_bonus) == 1, "No new awards after deletion; only the original row remains"


# ===========================================================================
# Case gating_lifecycle-15
# ===========================================================================


@pytest.mark.e2e
def test_gating_lifecycle_15_cross_program_strike_rejected_at_create(
    app, db, client, admin_auth_headers
):
    """Cross-program strike rejected at create — rule cannot attach strikes from different program."""
    with app.app_context():
        p1 = LoyaltyProgram(name="P1", is_active=True, is_default=True, uzs_per_point=250)
        p2 = LoyaltyProgram(name="P2", is_active=False, is_default=False, uzs_per_point=250)
        _db.session.add_all([p1, p2])
        _db.session.commit()

        strike_a = make_strike_rule(p1, name="A from P1", window_days=30, bonus_points=50)
        strike_b = make_strike_rule(p2, name="B from P2", window_days=30, bonus_points=50)

        resp = client.post(
            "/api/v1/admin/loyalty/consecutive-strike-rules",
            data=json.dumps(
                {
                    "name": "Cross Program Rule",
                    "program_id": p1.id,
                    "required_consecutive": 3,
                    "bonus_points": 500,
                    "strike_rule_ids": [strike_a.id, strike_b.id],
                }
            ),
            headers=admin_auth_headers,
        )

        assert resp.status_code in (400, 422), (
            f"Cross-program strikes must be rejected, got {resp.status_code}: {resp.data}"
        )
        resp_data = json.loads(resp.data)
        # validation_error_response returns {"message": "Validation failed", "errors": [<detail>...]}.
        # The specific rejection reason is in the "errors" list, not in "message".
        errors_list = resp_data.get("errors") or []
        error_detail = " ".join(str(e) for e in errors_list).lower()
        assert "program" in error_detail or "same" in error_detail, (
            f"errors list must mention program mismatch, got errors={errors_list!r}"
        )

        # No rule created
        assert LoyaltyConsecutiveStrikeRule.query.filter_by(program_id=p1.id).first() is None


# ===========================================================================
# Case gating_lifecycle-16
# ===========================================================================


@pytest.mark.e2e
def test_gating_lifecycle_16_min_order_amount_strike_ledger_trusted(app, db, sample_user):
    """Strike with min_order_amount — consecutive evaluator trusts the ledger rows."""
    with app.app_context():
        program = get_or_create_default_program()
        # Strike with min_order_amount=60000 UZS — the filter applies at update_streak
        # time, not at consecutive evaluation time. We seed 3 ledger rows directly
        # (simulating 3 qualifying orders), so the evaluator trusts them.
        strike_a = make_strike_rule(
            program,
            name="High Amount Strike",
            window_days=30,
            bonus_points=50,
            min_order_amount=Decimal("60000"),
        )
        consec_rule = make_consecutive_rule(
            program,
            strikes=[strike_a],
            name="Amount-Gated Champion",
            required_consecutive=3,
            combine_mode="all",
            bonus_points=500,
        )

        # Seed 3 STREAK_BONUS rows (trusting they were written by update_streak)
        seed_consecutive_run(sample_user.id, strike_a, count=3)

        result = _svc().update_consecutive_strikes(sample_user.id, commit=True)

        assert result is True, "_strike_consecutive_run reads ledger directly; trusts seeded rows"
        awards = consecutive_awards(sample_user.id, consec_rule.id)
        assert len(awards) == 1
        assert awards[0].points == 500
        assert (awards[0].extra_data or {}).get("milestone") == 1


# ===========================================================================
# Case gating_lifecycle-17
# ===========================================================================


@pytest.mark.e2e
def test_gating_lifecycle_17_admin_expires_rule_stops_awards(
    app, db, sample_user, client, admin_auth_headers
):
    """Admin sets ends_at to a past time — expiry guard genuinely stops a NEW award.

    Design: N=3, repeat-every-N.  First call with 3 consecutive achievements → 1
    award fires (milestone 1, target=1). Then we extend the run to 6 consecutive
    achievements → target=6//3=2, already=1 → a SECOND award (milestone 2) is
    pending.  We expire the rule BEFORE that second call.  The assertion that
    result2 is False is now sensitive to the expiry guard: WITHOUT expiry the
    second call would fire milestone 2; WITH expiry it is skipped.
    """
    with app.app_context():
        program = get_or_create_default_program()
        strike_a = make_strike_rule(program, name="A", window_days=30, bonus_points=50)
        consec_rule = make_consecutive_rule(
            program,
            strikes=[strike_a],
            name="Expiring Rule",
            required_consecutive=3,
            bonus_points=500,
            is_active=True,
            ends_at=None,
        )

        # Seed first 3 consecutive achievements (run = 3, target_awards = 1)
        now = datetime.now(timezone.utc)
        seed_consecutive_run(sample_user.id, strike_a, count=3, now=now)

        # First call: confirm milestone-1 award fires (rule is still open)
        result1 = _svc().update_consecutive_strikes(sample_user.id, commit=True)
        assert result1 is True, "First call must award milestone 1"
        assert len(consecutive_awards(sample_user.id, consec_rule.id)) == 1

        # Extend to 6 consecutive achievements (run = 6, target_awards = 2, already = 1)
        # Spacing 30 days keeps all 6 within the < 2*30 day gap requirement.
        # We seed 3 more achievements OLDER than the existing ones (k=4,5,6 periods ago).
        seed_consecutive_run(sample_user.id, strike_a, count=3, now=now - timedelta(days=3 * 30))

        # Sanity: with the rule still open, target=2 and already=1 → would award milestone 2.
        # (We do NOT call update_consecutive_strikes here to preserve the state cleanly.)

        # Expire the rule now (set ends_at to 1 second in the past)
        past_time = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        resp = client.put(
            f"/api/v1/admin/loyalty/consecutive-strike-rules/{consec_rule.id}",
            data=json.dumps({"ends_at": past_time}),
            headers=admin_auth_headers,
        )
        assert resp.status_code == 200

        # Second call: rule is expired → is_effective=False → evaluator skips the rule.
        # WITHOUT the expiry guard this call WOULD fire milestone 2 (target=2 > already=1).
        result2 = _svc().update_consecutive_strikes(sample_user.id, commit=True)
        assert result2 is False, (
            "Expired rule must not award — result2=False proves the expiry guard fired, "
            "not idempotency (a second milestone WOULD be pending with the rule active)"
        )

        # Exactly 1 award remains — milestone 2 was suppressed by the expiry guard
        assert len(consecutive_awards(sample_user.id, consec_rule.id)) == 1, (
            "Only milestone-1 award must exist; milestone 2 must have been blocked by expiry"
        )


# ===========================================================================
# Case gating_lifecycle-18
# ===========================================================================


@pytest.mark.e2e
def test_gating_lifecycle_18_inactive_program_returns_false(app, db, sample_user):
    """Program deactivated (is_active=False) — evaluator returns False immediately."""
    with app.app_context():
        program = get_or_create_default_program()
        strike_a = make_strike_rule(program, name="A", window_days=30, bonus_points=50)
        consec_rule = make_consecutive_rule(
            program,
            strikes=[strike_a],
            name="Program Inactive",
            required_consecutive=3,
            bonus_points=500,
        )

        seed_consecutive_run(sample_user.id, strike_a, count=3)

        # Deactivate the program
        program.is_active = False
        _db.session.commit()

        result = _svc().update_consecutive_strikes(sample_user.id, commit=True)

        assert result is False, "Inactive program → no qualifying program → False"
        awards = consecutive_awards(sample_user.id, consec_rule.id)
        assert len(awards) == 0


# ===========================================================================
# Case gating_lifecycle-19
# ===========================================================================


@pytest.mark.e2e
def test_gating_lifecycle_19_get_list_returns_only_default_program_rules(
    app, db, client, admin_auth_headers
):
    """GET list endpoint returns only rules for the default program when no program_id param."""
    with app.app_context():
        p1 = LoyaltyProgram(name="P1 Default", is_active=True, is_default=True, uzs_per_point=250)
        p2 = LoyaltyProgram(name="P2 Non-Default", is_active=True, is_default=False, uzs_per_point=250)
        _db.session.add_all([p1, p2])
        _db.session.commit()

        strike_p1 = make_strike_rule(p1, name="Strike P1", window_days=30, bonus_points=50)
        strike_p2 = make_strike_rule(p2, name="Strike P2", window_days=30, bonus_points=50)
        r1 = make_consecutive_rule(p1, strikes=[strike_p1], name="Rule R1", required_consecutive=3, bonus_points=100)
        r2 = make_consecutive_rule(p2, strikes=[strike_p2], name="Rule R2", required_consecutive=3, bonus_points=100)

        resp = client.get(
            "/api/v1/admin/loyalty/consecutive-strike-rules",
            headers=admin_auth_headers,
        )
        assert resp.status_code == 200
        resp_data = json.loads(resp.data)
        rules = resp_data["data"]["consecutive_strike_rules"]
        rule_ids = [r["id"] for r in rules]

        assert r1.id in rule_ids, "R1 (default program) must appear in results"
        assert r2.id not in rule_ids, "R2 (non-default program) must NOT appear without program_id param"
        assert resp_data["data"]["count"] == 1


# ===========================================================================
# Case gating_lifecycle-20
# ===========================================================================


@pytest.mark.e2e
def test_gating_lifecycle_20_required_consecutive_1_boundary(app, db, sample_user):
    """required_consecutive=1 boundary — combined=1 qualifies; first achievement immediately awards."""
    with app.app_context():
        program = get_or_create_default_program()
        strike_a = make_strike_rule(program, name="A", window_days=30, bonus_points=50)
        consec_rule = make_consecutive_rule(
            program,
            strikes=[strike_a],
            name="N=1 Boundary",
            required_consecutive=1,
            combine_mode="all",
            bonus_points=200,
        )

        # Seed exactly 1 achievement
        now = datetime.now(timezone.utc)
        seed_strike_achievement(sample_user.id, strike_a, when=now - timedelta(days=30))

        result = _svc().update_consecutive_strikes(sample_user.id, commit=True)

        assert result is True, "combined=1 >= n=1 → should award immediately"
        awards = consecutive_awards(sample_user.id, consec_rule.id)
        assert len(awards) == 1, "Exactly 1 award expected"
        assert awards[0].points == 200, "Award must be 200 bonus_points"
        assert (awards[0].extra_data or {}).get("milestone") == 1, "milestone must be 1"
        # target_awards = 1 // 1 = 1; already = 0; so 1 award is issued


# ===========================================================================
# Case gating_lifecycle-21
# ===========================================================================


@pytest.mark.e2e
def test_gating_lifecycle_21_deactivated_attached_strike_historical_still_counts(
    app, db, sample_user
):
    """An attached strike deactivated (is_active=False) after the consecutive rule
    exists: historical achievements for that strike still count toward the
    consecutive run (the evaluator reads the ledger, not the strike's active flag),
    but update_streak no longer creates NEW achievements for the deactivated strike.

    Scenario (combine_mode='all', N=3):
    - Seed 3 achievements for strike B WHILE it is active.
    - Deactivate strike B (is_active=False).
    - Seed 3 achievements for strike A (active).
    - Evaluate: combined = min(run_A=3, run_B=3) = 3 >= N=3 → should award.
    This proves historical B achievements are honoured even after deactivation.
    """
    with app.app_context():
        program = get_or_create_default_program()
        strike_a = make_strike_rule(program, name="A-active", window_days=30, bonus_points=50)
        strike_b = make_strike_rule(program, name="B-to-deactivate", window_days=30, bonus_points=50)
        consec_rule = make_consecutive_rule(
            program,
            strikes=[strike_a, strike_b],
            name="Deactivated Strike Historical",
            required_consecutive=3,
            combine_mode="all",
            bonus_points=750,
        )

        # Phase 1: seed B's 3 achievements while it is still active (simulates
        # real history that update_streak would have written).
        now = datetime.now(timezone.utc)
        seed_consecutive_run(sample_user.id, strike_b, count=3, now=now)

        # Deactivate strike B — new update_streak calls will no longer write
        # achievements for B, but the ledger rows already exist.
        strike_b.is_active = False
        _db.session.commit()

        # Phase 2: seed A's 3 achievements after B was deactivated.
        seed_consecutive_run(sample_user.id, strike_a, count=3, now=now)

        # Verify strike B ledger is still intact (historical rows not removed)
        b_achievements = strike_achievement_count(sample_user.id, strike_b.id)
        assert b_achievements == 3, (
            f"B's historical ledger rows must survive deactivation, got {b_achievements}"
        )

        # Evaluate: the consecutive evaluator reads both strikes' ledgers via
        # rule.strikes (B is still in the M2M table) and counts run_B=3.
        result = _svc().update_consecutive_strikes(sample_user.id, commit=True)

        assert result is True, (
            "Historical B achievements count even after deactivation; "
            "combined = min(run_A=3, run_B=3) = 3 >= N=3 → must award"
        )
        awards = consecutive_awards(sample_user.id, consec_rule.id)
        assert len(awards) == 1, "Exactly 1 CONSECUTIVE_STREAK_BONUS award expected"
        assert awards[0].points == 750
        assert (awards[0].extra_data or {}).get("milestone") == 1


# ===========================================================================
# Case gating_lifecycle-22
# ===========================================================================


@pytest.mark.e2e
def test_gating_lifecycle_22_hard_deleted_strike_cascade_reduces_combined(
    app, db, sample_user
):
    """An attached strike is hard-deleted; the association CASCADE removes it from
    rule.strikes. The rule re-evaluates over the reduced strike set.

    Scenario (combine_mode='all', N=3):
    - Seed 3 for A, 3 for B → combined=min(3,3)=3 → milestone 1 awarded.
    - Hard-delete strike B → association CASCADE removes B; rule now has only A.
    - Seed 3 MORE for A (total=6) → with only A: combined=min([6])=6, target=2,
      already=1 → milestone 2 must fire (proves re-evaluation over reduced set).
    - The now-orphaned ledger rows for B have no bearing (they carry a deleted
      streak_rule_id that is no longer in rule.strikes, so they are ignored).
    """
    with app.app_context():
        program = get_or_create_default_program()
        strike_a = make_strike_rule(program, name="A-survivor", window_days=30, bonus_points=50)
        strike_b = make_strike_rule(program, name="B-to-delete", window_days=30, bonus_points=50)
        consec_rule = make_consecutive_rule(
            program,
            strikes=[strike_a, strike_b],
            name="Hard Delete Strike",
            required_consecutive=3,
            combine_mode="all",
            bonus_points=600,
        )

        # Phase 1: seed 3 for A and 3 for B; milestone 1 should fire.
        now = datetime.now(timezone.utc)
        seed_consecutive_run(sample_user.id, strike_a, count=3, now=now)
        seed_consecutive_run(sample_user.id, strike_b, count=3, now=now)

        result1 = _svc().update_consecutive_strikes(sample_user.id, commit=True)
        assert result1 is True, "Phase 1: combined=min(3,3)=3 >= 3 → milestone 1 must fire"
        awards_phase1 = consecutive_awards(sample_user.id, consec_rule.id)
        assert len(awards_phase1) == 1
        assert (awards_phase1[0].extra_data or {}).get("milestone") == 1

        # Phase 2: hard-delete strike B → CASCADE removes it from the M2M table.
        b_id = strike_b.id
        _db.session.delete(strike_b)
        _db.session.commit()

        # Confirm B is no longer in rule.strikes (CASCADE fired).
        _db.session.expire_all()
        reloaded_rule = LoyaltyConsecutiveStrikeRule.query.get(consec_rule.id)
        remaining_ids = [s.id for s in reloaded_rule.strikes]
        assert b_id not in remaining_ids, (
            f"After hard-delete, B must not appear in rule.strikes; got {remaining_ids}"
        )
        assert strike_a.id in remaining_ids, "Strike A must still be attached"

        # Phase 3: seed 3 more for A (spaced further back in time so spacing stays < 2*W=60).
        # Place these older than the phase-1 batch so the run is 6 total (newest-first).
        seed_consecutive_run(
            sample_user.id, strike_a, count=3, now=now - timedelta(days=3 * 30)
        )

        # Verify A now has 6 ledger rows.
        a_achievements = strike_achievement_count(sample_user.id, strike_a.id)
        assert a_achievements == 6, f"A must have 6 achievements, got {a_achievements}"

        # Phase 4: re-evaluate.  Rule now has only A attached.
        # combined = run_A = 6 // 1 → actually run_length = 6 consecutive periods.
        # target_awards = 6 // 3 = 2; already = 1 (milestone 1 from phase 1).
        # → milestone 2 must fire.
        result2 = _svc().update_consecutive_strikes(sample_user.id, commit=True)
        assert result2 is True, (
            "After B deleted, rule has only A; run_A=6, target=2, already=1 → milestone 2 must fire"
        )
        awards_phase2 = consecutive_awards(sample_user.id, consec_rule.id)
        assert len(awards_phase2) == 2, "Two total awards expected (milestones 1 and 2)"
        milestones = sorted((a.extra_data or {}).get("milestone", 0) for a in awards_phase2)
        assert milestones == [1, 2], f"Expected milestones [1, 2], got {milestones}"
