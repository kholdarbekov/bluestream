"""E2E tests for get_consecutive_strike_progress, get_account_dashboard_for_user,
the admin CRUD endpoints, /api/public/loyalty.json, and /loyalty-guide rendering
of the consecutive-strike bonus rule.

Dimension: progress_public (cases progress_public-01 through progress_public-20).

Each test asserts on REAL state (DB / service / HTTP) — no award is stubbed.
Shared helpers are imported from tests/e2e/_consecutive_strike_helpers.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from business_app import db as _db
from business_app.models.loyalty import (
    LoyaltyConsecutiveStrikeRule,
    LoyaltyProgram,
    LoyaltyStreakRule,
    LoyaltyTransaction,
)
from business_app.models.translation import Translation
from business_app.services.loyalty_service import LoyaltyService
from business_app.utils.translations import translation_service

from tests.e2e._consecutive_strike_helpers import (
    consecutive_awards,
    get_or_create_default_program,
    make_consecutive_rule,
    make_strike_rule,
    seed_consecutive_run,
    seed_strike_achievement,
    silence_loyalty_notifications,
)

# ---------------------------------------------------------------------------
# Module-level mark
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.e2e

# ---------------------------------------------------------------------------
# Autouse notification silencer
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _silence(monkeypatch):
    """Silence all LoyaltyService notification side-effects in every test."""
    silence_loyalty_notifications(monkeypatch)


# ---------------------------------------------------------------------------
# Helper: seed a translation row (insert-or-update)
# ---------------------------------------------------------------------------


def _seed_t(key: str, value: str, language: str = "uz") -> Translation:
    """Insert or update a Translation row for the given key/language."""
    existing = Translation.query.filter_by(key=key, language=language).first()
    if existing:
        existing.value = value
        existing.is_active = True
        return existing
    row = Translation(
        key=key,
        language=language,
        value=value,
        category=key.split(".")[0] if "." in key else "general",
        is_active=True,
    )
    _db.session.add(row)
    return row


def _commit_translations(*args):
    """Flush and commit translation rows to the DB."""
    _db.session.commit()


# ---------------------------------------------------------------------------
# Case progress_public-01
# ---------------------------------------------------------------------------


def test_progress_public_01_no_program_returns_empty(app, db, sample_user):
    """get_consecutive_strike_progress returns [] when no LoyaltyProgram exists.

    Spec §6.1 and implementation line 1733-1735: if no program, return [].
    """
    # db fixture starts fresh — no LoyaltyProgram rows.
    svc = LoyaltyService()
    result = svc.get_consecutive_strike_progress(sample_user.id)
    assert result == [], f"Expected [] but got {result}"


# ---------------------------------------------------------------------------
# Case progress_public-02
# ---------------------------------------------------------------------------


def test_progress_public_02_inactive_rule_and_no_strikes_omitted(app, db, sample_user):
    """get_consecutive_strike_progress omits inactive rules and rules with no strikes.

    R1 (is_active=False) and R2 (no attached strikes) must both be absent.
    Spec §5.3; implementation line 1740-1746.
    """
    program = get_or_create_default_program()
    strike_a = make_strike_rule(program, name="Strike-A", window_days=30)

    # R1: inactive, has a strike
    r1 = make_consecutive_rule(
        program, strikes=[strike_a], name="Inactive Rule",
        required_consecutive=3, is_active=False,
    )
    # R2: active but no attached strikes
    r2_obj = LoyaltyConsecutiveStrikeRule(
        program_id=program.id,
        name="No-Strike Rule",
        required_consecutive=3,
        combine_mode="all",
        bonus_points=100,
        is_active=True,
    )
    r2_obj.strikes = []
    _db.session.add(r2_obj)
    _db.session.commit()

    # Seed 6 achievements for the user on strike A (would satisfy a real rule)
    seed_consecutive_run(sample_user.id, strike_a, count=6)

    svc = LoyaltyService()
    result = svc.get_consecutive_strike_progress(sample_user.id)

    assert result == [], (
        f"Expected [] — inactive R1 and no-strike R2 must be excluded. Got {result}"
    )


# ---------------------------------------------------------------------------
# Case progress_public-03
# ---------------------------------------------------------------------------


def test_progress_public_03_combined_current_capped_at_n(app, db, sample_user):
    """combined_current and per-strike current are capped at N even when run > N.

    Setup: N=3, seed 5 consecutive achievements (run=5 > N=3).
    Expected: combined_current=3, per_strike[0].current=3.
    Spec §6.1; implementation lines 1758, 1764, 1771.
    """
    program = get_or_create_default_program()
    strike_a = make_strike_rule(program, name="A", window_days=30)
    rule = make_consecutive_rule(
        program, strikes=[strike_a], name="Capped Rule",
        required_consecutive=3, combine_mode="all", bonus_points=200,
    )

    # Seed 5 consecutive achievements (gap = window_days = 30 < 2*30 = 60 → all consecutive)
    seed_consecutive_run(sample_user.id, strike_a, count=5)

    svc = LoyaltyService()
    result = svc.get_consecutive_strike_progress(sample_user.id)

    assert len(result) == 1
    entry = result[0]
    assert entry["required_consecutive"] == 3
    assert entry["combined_current"] == 3, (
        f"combined_current should be capped at N=3 but got {entry['combined_current']}"
    )
    assert len(entry["per_strike"]) == 1
    ps = entry["per_strike"][0]
    assert ps["current"] == 3, (
        f"per_strike current should be capped at N=3 but got {ps['current']}"
    )
    assert ps["target"] == 3
    assert ps["window_days"] == 30


# ---------------------------------------------------------------------------
# Case progress_public-04
# ---------------------------------------------------------------------------


def test_progress_public_04_combine_mode_all_uses_min_any_uses_max(app, db, sample_user):
    """combine_mode=all → min(per-strike runs); combine_mode=any → max(per-strike runs).

    Strike A has run=2, Strike B has run=4.
    ALL rule: combined=min(2,4)=2.  ANY rule: combined=max(2,4)=4 (capped at N=4).
    Spec §5.2; implementation lines 1764, 1771.
    """
    program = get_or_create_default_program()
    strike_a = make_strike_rule(program, name="A-30", window_days=30, bonus_points=100)
    strike_b = make_strike_rule(program, name="B-40", window_days=40, bonus_points=100)

    rule_all = make_consecutive_rule(
        program, strikes=[strike_a, strike_b], name="ALL Rule",
        required_consecutive=4, combine_mode="all", bonus_points=300,
    )
    rule_any = make_consecutive_rule(
        program, strikes=[strike_a, strike_b], name="ANY Rule",
        required_consecutive=4, combine_mode="any", bonus_points=300,
    )

    now = datetime.now(timezone.utc)
    # Seed 2 consecutive achievements for A (spaced 30d apart)
    for k in range(2):
        when = now - timedelta(days=30 * (2 - k))
        seed_strike_achievement(sample_user.id, strike_a, when)
    # Seed 4 consecutive achievements for B (spaced 40d apart)
    for k in range(4):
        when = now - timedelta(days=40 * (4 - k))
        seed_strike_achievement(sample_user.id, strike_b, when)

    svc = LoyaltyService()
    result = svc.get_consecutive_strike_progress(sample_user.id)

    entries = {e["name"]: e for e in result}
    assert "ALL Rule" in entries, f"ALL rule missing from result: {result}"
    assert "ANY Rule" in entries, f"ANY rule missing from result: {result}"

    entry_all = entries["ALL Rule"]
    # min(2, 4) = 2; capped at N=4 → still 2
    assert entry_all["combined_current"] == 2, (
        f"ALL combined_current expected 2 (min) but got {entry_all['combined_current']}"
    )

    entry_any = entries["ANY Rule"]
    # max(2, 4) = 4; capped at N=4 → 4
    assert entry_any["combined_current"] == 4, (
        f"ANY combined_current expected 4 (max) but got {entry_any['combined_current']}"
    )

    # Per-strike entries should reflect individual runs (capped at N=4)
    # A has run=2, B has run=4
    for e in [entry_all, entry_any]:
        per_strike_map = {ps["strike_name"]: ps for ps in e["per_strike"]}
        assert per_strike_map["A-30"]["current"] == 2, (
            f"A-30 per-strike current should be 2 but got {per_strike_map['A-30']['current']}"
        )
        assert per_strike_map["B-40"]["current"] == 4, (
            f"B-40 per-strike current should be 4 but got {per_strike_map['B-40']['current']}"
        )


# ---------------------------------------------------------------------------
# Case progress_public-05
# ---------------------------------------------------------------------------


def test_progress_public_05_active_flag_based_on_2x_window(app, db, sample_user):
    """per_strike active=False when last achievement >= 2*window_days ago, True otherwise.

    Strike A: window=30, last achievement 61 days ago → active=False (61 >= 60).
    Strike B: window=14, last achievement 10 days ago → active=True (10 < 28).
    Spec §6.1; implementation lines 1752-1753.
    """
    program = get_or_create_default_program()
    strike_a = make_strike_rule(program, name="StrikeA", window_days=30, bonus_points=100)
    strike_b = make_strike_rule(program, name="StrikeB", window_days=14, bonus_points=100)

    rule = make_consecutive_rule(
        program, strikes=[strike_a, strike_b], name="Active-Flag Rule",
        required_consecutive=3, combine_mode="any", bonus_points=200,
    )

    now = datetime.now(timezone.utc)
    # A: last achievement at now - 61 days (>= 2*30=60 → inactive)
    seed_strike_achievement(sample_user.id, strike_a, now - timedelta(days=61))
    # B: last achievement at now - 10 days (< 2*14=28 → active)
    seed_strike_achievement(sample_user.id, strike_b, now - timedelta(days=10))

    svc = LoyaltyService()
    result = svc.get_consecutive_strike_progress(sample_user.id)

    assert len(result) == 1
    per_strike_map = {ps["strike_name"]: ps for ps in result[0]["per_strike"]}

    assert "StrikeA" in per_strike_map, "StrikeA missing from per_strike"
    assert "StrikeB" in per_strike_map, "StrikeB missing from per_strike"

    assert per_strike_map["StrikeA"]["active"] is False, (
        "StrikeA should be inactive (last achievement 61 >= 2*30=60 days ago)"
    )
    assert per_strike_map["StrikeB"]["active"] is True, (
        "StrikeB should be active (last achievement 10 < 2*14=28 days ago)"
    )


# ---------------------------------------------------------------------------
# Case progress_public-06
# ---------------------------------------------------------------------------


def test_progress_public_06_no_achievements_yields_active_false_and_zero(app, db, sample_user):
    """per_strike active=False and current=0 when no achievements exist for a strike.

    Implementation line 1752-1753: active = bool(last_times) and ...; empty → False.
    Spec §6.1.
    """
    program = get_or_create_default_program()
    strike_a = make_strike_rule(program, name="ZeroStrike", window_days=30, bonus_points=100)
    rule = make_consecutive_rule(
        program, strikes=[strike_a], name="No-Achievement Rule",
        required_consecutive=3, combine_mode="all", bonus_points=100,
    )

    # No ledger rows for sample_user on strike_a

    svc = LoyaltyService()
    result = svc.get_consecutive_strike_progress(sample_user.id)

    assert len(result) == 1
    entry = result[0]
    assert entry["combined_current"] == 0, (
        f"combined_current should be 0 but got {entry['combined_current']}"
    )
    assert len(entry["per_strike"]) == 1
    ps = entry["per_strike"][0]
    assert ps["current"] == 0, f"per_strike current should be 0 but got {ps['current']}"
    assert ps["active"] is False, "active should be False when no achievements exist"


# ---------------------------------------------------------------------------
# Case progress_public-07
# ---------------------------------------------------------------------------


def test_progress_public_07_dashboard_includes_consecutive_strike_progress(app, db, sample_user):
    """get_account_dashboard_for_user includes consecutive_strike_progress key.

    Spec §6.1: 'Surfaced in get_account_dashboard_for_user alongside streak_progress'.
    Implementation lines 315-316.
    """
    program = get_or_create_default_program()
    strike_a = make_strike_rule(program, name="DashStrike", window_days=30, bonus_points=100)
    rule = make_consecutive_rule(
        program, strikes=[strike_a], name="DashRule",
        required_consecutive=3, combine_mode="all", bonus_points=500,
    )
    # Seed 3 consecutive achievements
    seed_consecutive_run(sample_user.id, strike_a, count=3)

    svc = LoyaltyService()
    dashboard = svc.get_account_dashboard_for_user(sample_user.id)

    assert "consecutive_strike_progress" in dashboard, (
        "'consecutive_strike_progress' key missing from dashboard"
    )
    csp = dashboard["consecutive_strike_progress"]
    assert isinstance(csp, list), f"consecutive_strike_progress should be a list, got {type(csp)}"
    assert len(csp) >= 1, "Expected at least one entry in consecutive_strike_progress"

    entry = csp[0]
    # The rule name may be "DashRule" (as set)
    assert entry["required_consecutive"] == 3
    assert entry["combine_mode"] == "all"
    assert entry["bonus_points"] == 500
    assert "combined_current" in entry
    assert "per_strike" in entry

    # streak_progress key must also be present (parity)
    assert "streak_progress" in dashboard, "'streak_progress' key missing from dashboard"


# ---------------------------------------------------------------------------
# Case progress_public-08
# ---------------------------------------------------------------------------


def test_progress_public_08_admin_crud_round_trip(app, db, client, admin_auth_headers, sample_user):
    """Admin CRUD round-trip: POST, GET, PUT, DELETE, GET again.

    Spec §6.2; implementation admin.py lines 7376-7575.
    """
    program = get_or_create_default_program()
    s1 = make_strike_rule(program, name="S1-30", window_days=30, bonus_points=100)

    # 1) POST — create
    create_resp = client.post(
        "/api/v1/admin/loyalty/consecutive-strike-rules",
        headers=admin_auth_headers,
        json={
            "name": "X",
            "required_consecutive": 4,
            "combine_mode": "all",
            "bonus_points": 500,
            "strike_rule_ids": [s1.id],
            "program_id": program.id,
        },
    )
    assert create_resp.status_code == 201, create_resp.get_json()
    created = create_resp.get_json()["data"]["consecutive_strike_rule"]
    assert created["name"] == "X"
    assert created["required_consecutive"] == 4
    assert created["bonus_points"] == 500
    assert created["combine_mode"] == "all"
    assert s1.id in created["strike_rule_ids"]
    new_id = created["id"]

    # 2) GET list — one rule present; assert exact echoed fields
    list_resp = client.get(
        f"/api/v1/admin/loyalty/consecutive-strike-rules?program_id={program.id}",
        headers=admin_auth_headers,
    )
    assert list_resp.status_code == 200
    list_data = list_resp.get_json()["data"]
    assert list_data["count"] == 1
    listed_rule = list_data["consecutive_strike_rules"][0]
    assert listed_rule["id"] == new_id
    assert listed_rule["required_consecutive"] == 4, (
        f"GET list: required_consecutive should be 4 but got {listed_rule['required_consecutive']}"
    )
    assert listed_rule["combine_mode"] == "all", (
        f"GET list: combine_mode should be 'all' but got {listed_rule['combine_mode']}"
    )
    assert listed_rule["strike_rule_ids"] == [s1.id], (
        f"GET list: strike_rule_ids should be [{s1.id}] but got {listed_rule['strike_rule_ids']}"
    )

    # 3) PUT — update required_consecutive and bonus_points; echoed fields must match
    put_resp = client.put(
        f"/api/v1/admin/loyalty/consecutive-strike-rules/{new_id}",
        headers=admin_auth_headers,
        json={"required_consecutive": 6, "bonus_points": 750},
    )
    assert put_resp.status_code == 200, put_resp.get_json()
    updated = put_resp.get_json()["data"]["consecutive_strike_rule"]
    assert updated["required_consecutive"] == 6, (
        f"PUT: required_consecutive should be 6 but got {updated['required_consecutive']}"
    )
    assert updated["bonus_points"] == 750, (
        f"PUT: bonus_points should be 750 but got {updated['bonus_points']}"
    )
    # combine_mode and strike_rule_ids must be preserved (not changed by this PUT)
    assert updated["combine_mode"] == "all", (
        f"PUT: combine_mode should still be 'all' but got {updated['combine_mode']}"
    )
    assert updated["strike_rule_ids"] == [s1.id], (
        f"PUT: strike_rule_ids should still be [{s1.id}] but got {updated['strike_rule_ids']}"
    )

    # 4) DELETE
    del_resp = client.delete(
        f"/api/v1/admin/loyalty/consecutive-strike-rules/{new_id}",
        headers=admin_auth_headers,
    )
    assert del_resp.status_code == 200, del_resp.get_json()

    # 5) GET again — count=0
    list_resp2 = client.get(
        f"/api/v1/admin/loyalty/consecutive-strike-rules?program_id={program.id}",
        headers=admin_auth_headers,
    )
    assert list_resp2.status_code == 200
    assert list_resp2.get_json()["data"]["count"] == 0


# ---------------------------------------------------------------------------
# Case progress_public-09
# ---------------------------------------------------------------------------


def test_progress_public_09_admin_post_rejects_empty_strike_ids(app, db, client, admin_auth_headers):
    """POST with empty strike_rule_ids returns 422 or 400 with a validation error.

    _resolve_strikes returns error 'At least one order-strike must be attached'
    when ids list is empty.  Spec §4.1 and §6.2; admin.py line 7367.
    """
    program = get_or_create_default_program()

    resp = client.post(
        "/api/v1/admin/loyalty/consecutive-strike-rules",
        headers=admin_auth_headers,
        json={
            "name": "Y",
            "required_consecutive": 3,
            "bonus_points": 100,
            "strike_rule_ids": [],
            "program_id": program.id,
        },
    )
    assert resp.status_code in (400, 422), (
        f"Expected 400 or 422 for empty strike_rule_ids, got {resp.status_code}: {resp.get_json()}"
    )
    body = resp.get_json()
    # The error message should reference "At least one order-strike must be attached"
    body_str = str(body)
    assert "At least one order-strike" in body_str or "strike" in body_str.lower(), (
        f"Expected validation error mentioning strikes, got: {body}"
    )

    # Confirm the rule was NOT persisted
    count = LoyaltyConsecutiveStrikeRule.query.filter_by(program_id=program.id).count()
    assert count == 0, f"Rule should not have been persisted, but found {count} rows"


# ---------------------------------------------------------------------------
# Case progress_public-10
# ---------------------------------------------------------------------------


def test_progress_public_10_admin_post_rejects_cross_program_strike(app, db, client, admin_auth_headers):
    """POST with a strike from a different program returns 400/422.

    _resolve_strikes validates program membership.  Spec §6.2; admin.py line 7371-7372.
    """
    program_p1 = get_or_create_default_program()  # default program

    # Create a second non-default program
    p2 = LoyaltyProgram(name="Second Program", is_active=True, is_default=False)
    _db.session.add(p2)
    _db.session.commit()

    # Strike belonging to P2
    s_other = make_strike_rule(p2, name="Other-Strike", window_days=30, bonus_points=50)

    resp = client.post(
        "/api/v1/admin/loyalty/consecutive-strike-rules",
        headers=admin_auth_headers,
        json={
            "name": "Z",
            "required_consecutive": 3,
            "bonus_points": 50,
            "strike_rule_ids": [s_other.id],
            "program_id": program_p1.id,
        },
    )
    assert resp.status_code in (400, 422), (
        f"Expected 400 or 422 for cross-program strike, got {resp.status_code}: {resp.get_json()}"
    )
    body_str = str(resp.get_json())
    assert "program" in body_str.lower() or "strike" in body_str.lower(), (
        f"Expected error mentioning program or strike, got: {resp.get_json()}"
    )

    # Rule must NOT be persisted under P1
    count = LoyaltyConsecutiveStrikeRule.query.filter_by(program_id=program_p1.id).count()
    assert count == 0, f"Rule should not have been persisted under P1, found {count}"


# ---------------------------------------------------------------------------
# Case progress_public-11
# ---------------------------------------------------------------------------


def test_progress_public_11_admin_put_updates_combine_mode_and_reattaches_strike(
    app, db, client, admin_auth_headers
):
    """PUT updates combine_mode and re-attaches a different strike.

    Spec §6.2: PUT supports re-attach.  Implementation admin.py lines 7521-7542.
    """
    program = get_or_create_default_program()
    s1 = make_strike_rule(program, name="S1", window_days=30, bonus_points=100)
    s2 = make_strike_rule(program, name="S2", window_days=14, bonus_points=50)

    rule = make_consecutive_rule(
        program, strikes=[s1], name="Original-Rule",
        required_consecutive=3, combine_mode="all", bonus_points=200,
    )

    # PUT: change combine_mode to 'any' and re-attach s2 only (removing s1)
    put_resp = client.put(
        f"/api/v1/admin/loyalty/consecutive-strike-rules/{rule.id}",
        headers=admin_auth_headers,
        json={"combine_mode": "any", "strike_rule_ids": [s2.id]},
    )
    assert put_resp.status_code == 200, put_resp.get_json()
    updated = put_resp.get_json()["data"]["consecutive_strike_rule"]
    assert updated["combine_mode"] == "any", (
        f"combine_mode should be 'any' but got {updated['combine_mode']}"
    )
    assert updated["strike_rule_ids"] == [s2.id], (
        f"strike_rule_ids should be [{s2.id}] (s1 removed) but got {updated['strike_rule_ids']}"
    )

    # Verify DB reflects the change
    _db.session.expire_all()
    from business_app.models.loyalty import LoyaltyConsecutiveStrikeRule as LCSR
    db_rule = LCSR.query.get(rule.id)
    assert db_rule.combine_mode == "any"
    assert [s.id for s in db_rule.strikes] == [s2.id], (
        f"DB strikes should be [s2.id] only but got {[s.id for s in db_rule.strikes]}"
    )


# ---------------------------------------------------------------------------
# Case progress_public-12
# ---------------------------------------------------------------------------


def test_progress_public_12_loyalty_json_includes_effective_rules_only(
    app, db, client
):
    """/api/public/loyalty.json includes consecutiveStrikeBonuses with effective rules only.

    BAD (inactive) and EXPIRED (ends_at in past) must be absent.
    Spec §8; implementation routes.py line 2099 + get_public_loyalty_facts filter.
    """
    program = get_or_create_default_program()
    strike_a = make_strike_rule(program, name="3 in 30", window_days=30, bonus_points=100)

    now = datetime.now(timezone.utc)

    # GOOD: active, no date restrictions
    good_rule = make_consecutive_rule(
        program, strikes=[strike_a], name="GOOD Rule",
        required_consecutive=4, combine_mode="all", bonus_points=800,
        is_active=True,
    )

    # BAD: inactive
    bad_rule = make_consecutive_rule(
        program, strikes=[strike_a], name="BAD Rule",
        required_consecutive=3, combine_mode="all", bonus_points=100,
        is_active=False,
    )

    # EXPIRED: active but ends_at in the past
    expired_rule = make_consecutive_rule(
        program, strikes=[strike_a], name="EXPIRED Rule",
        required_consecutive=2, combine_mode="all", bonus_points=50,
        is_active=True,
        ends_at=now - timedelta(days=1),
    )

    resp = client.get("/api/public/loyalty.json")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "consecutiveStrikeBonuses" in data, (
        "'consecutiveStrikeBonuses' key missing from /api/public/loyalty.json"
    )

    bonuses = data["consecutiveStrikeBonuses"]
    rule_names = [b.get("name") for b in bonuses]

    # GOOD must appear
    # The name may be a dict (multilingual) or a string depending on implementation.
    # Check for required_consecutive=4 as the canonical identifier.
    effective = [b for b in bonuses if b.get("required_consecutive") == 4 and b.get("bonus_points") == 800]
    assert len(effective) == 1, (
        f"Expected exactly 1 entry with required_consecutive=4 and bonus_points=800 in {bonuses}"
    )

    # BAD (inactive) must be absent
    inactive_entries = [b for b in bonuses if b.get("bonus_points") == 100 and b.get("required_consecutive") == 3]
    assert len(inactive_entries) == 0, (
        f"Inactive rule should not appear in consecutiveStrikeBonuses: {inactive_entries}"
    )

    # EXPIRED must be absent
    expired_entries = [b for b in bonuses if b.get("ends_at") or (b.get("bonus_points") == 50 and b.get("required_consecutive") == 2)]
    assert len(expired_entries) == 0, (
        f"Expired rule should not appear in consecutiveStrikeBonuses: {expired_entries}"
    )


# ---------------------------------------------------------------------------
# Case progress_public-13
# ---------------------------------------------------------------------------


def test_progress_public_13_loyalty_guide_renders_all_rule_with_andjoiner(
    app, db, client
):
    """/loyalty-guide renders the consecutive-strike card with ANDJOINER sentinel for combine_mode=all.

    Spec §8; template line 161 uses unpadded key lookup.
    Regression: padded key ' loyalty_guide.earn.consec_and ' never resolves.
    """
    program = get_or_create_default_program()

    alpha = make_strike_rule(program, name="Alpha30", window_days=30, bonus_points=100)
    beta = make_strike_rule(program, name="Beta40", window_days=40, bonus_points=100)

    consec = make_consecutive_rule(
        program, strikes=[alpha, beta], name="Consecutive Streaks",
        required_consecutive=6, combine_mode="all", bonus_points=1000,
    )

    # Seed translation sentinels (lang=uz, which is the default)
    _seed_t("loyalty_guide.earn.consec_and", "ANDJOINER", "uz")
    _seed_t("loyalty_guide.earn.consec_line_all", "DOlist {strikes} END", "uz")
    _seed_t("loyalty_guide.earn.consec_title", "Consecutive Streaks", "uz")
    _seed_t("loyalty_guide.earn.consec_repeat", "Repeats every {n}", "uz")
    _seed_t("loyalty_guide.unit.points", "AC", "uz")
    _db.session.commit()

    # See test_14's note: Jinja constant-folds ``'literal' | t`` at compile time
    # and caches the compiled template on the session-scoped app. Force a recompile
    # so this test's freshly-seeded translations are baked in regardless of which
    # test rendered /loyalty-guide first.
    translation_service.clear_cache(language="uz")
    translation_service.warm_cache_for_category("loyalty_guide", languages=["uz"])
    app.jinja_env.cache.clear()

    try:
        resp = client.get("/loyalty-guide?lang=uz", follow_redirects=True)
        assert resp.status_code == 200, resp.get_data(as_text=True)[:500]
        html = resp.get_data(as_text=True)

        # ANDJOINER sentinel must be present (key resolved correctly)
        assert "ANDJOINER" in html, (
            "Expected 'ANDJOINER' in HTML but it was not present. "
            "The unpadded key 'loyalty_guide.earn.consec_and' should resolve."
        )
        # Raw key must NOT appear
        assert "loyalty_guide.earn.consec_and" not in html, (
            "Raw key 'loyalty_guide.earn.consec_and' leaked into HTML."
        )
        # Title sentinel
        assert "Consecutive Streaks" in html, "consec_title sentinel not found in HTML"
        # Strike names must appear
        assert "Alpha30" in html, "Strike name 'Alpha30' not found in HTML"
        assert "Beta40" in html, "Strike name 'Beta40' not found in HTML"
        # Raw template keys for line/repeat must not appear
        assert "loyalty_guide.earn.consec_line_all" not in html
        assert "loyalty_guide.earn.consec_repeat" not in html
    finally:
        # Clean up translation rows to avoid bleed-through
        Translation.query.filter(
            Translation.language == "uz",
            Translation.key.in_([
                "loyalty_guide.earn.consec_and",
                "loyalty_guide.earn.consec_line_all",
                "loyalty_guide.earn.consec_title",
                "loyalty_guide.earn.consec_repeat",
                "loyalty_guide.unit.points",
            ]),
        ).delete(synchronize_session=False)
        _db.session.commit()


# ---------------------------------------------------------------------------
# Case progress_public-14
# ---------------------------------------------------------------------------


def test_progress_public_14_loyalty_guide_renders_any_rule_with_orjoiner(
    app, db, client
):
    """/loyalty-guide uses 'or' joiner for combine_mode='any' and does not bleed 'and' joiner.

    Template line 161: joiner is 'consec_or' when combine_mode != 'all'.
    Spec §8.
    """
    program = get_or_create_default_program()

    sx = make_strike_rule(program, name="StrX", window_days=20, bonus_points=100)
    sy = make_strike_rule(program, name="StrY", window_days=25, bonus_points=100)

    consec = make_consecutive_rule(
        program, strikes=[sx, sy], name="Any Streaks",
        required_consecutive=3, combine_mode="any", bonus_points=200,
    )

    _seed_t("loyalty_guide.earn.consec_or", "ORJOINER", "uz")
    _seed_t("loyalty_guide.earn.consec_line_any", "ANY {strikes} {n}", "uz")
    _seed_t("loyalty_guide.earn.consec_title", "Consecutive Streaks Title", "uz")
    _seed_t("loyalty_guide.earn.consec_repeat", "Repeats {n}", "uz")
    _seed_t("loyalty_guide.unit.points", "AQ", "uz")
    # Ensure the 'all' joiner key has no value seeded — no translation row means
    # the filter returns the raw key, but it must not appear in HTML either.
    # We explicitly seed it to something that would be conspicuous if it appeared.
    _seed_t("loyalty_guide.earn.consec_and", "ANDJOINER_SHOULDNOTAPPEAR", "uz")
    _db.session.commit()

    # Clear the entire loyalty_guide translation cache namespace before rendering so
    # this test is not affected by stale Redis entries written by a prior test in
    # the same worker (e.g. test_13 caches 'translations:uz:loyalty_guide.earn.consec_and'
    # = 'ANDJOINER'; even though test_13 deletes the DB row, the Redis entry survives
    # across the DB rollback-on-exception path until it expires).  After clearing,
    # warm the cache from the freshly-seeded DB rows so the render is deterministic.
    translation_service.clear_cache(language="uz")
    translation_service.warm_cache_for_category("loyalty_guide", languages=["uz"])

    # Drop the compiled-template bytecode cached on the session-scoped app's
    # jinja_env. Jinja constant-folds the no-argument ``'literal' | t`` filter
    # expressions (e.g. the ``consec_or`` joiner on template line 161) at COMPILE
    # time and bakes the result into the cached compiled template. A prior test
    # (test_13) renders /loyalty-guide first and compiles it while ``consec_or``
    # is unseeded, baking the RAW key; reusing that cached template here means
    # this test's freshly-seeded ``consec_or`` is never looked up. Forcing a
    # recompile makes the constant-fold pick up the current translation.
    app.jinja_env.cache.clear()

    try:
        resp = client.get("/loyalty-guide?lang=uz", follow_redirects=True)
        assert resp.status_code == 200, resp.get_data(as_text=True)[:500]
        html = resp.get_data(as_text=True)

        # ORJOINER must appear (correct joiner for 'any')
        assert "ORJOINER" in html, "Expected 'ORJOINER' in HTML for combine_mode='any'"
        # Raw key must NOT appear
        assert "loyalty_guide.earn.consec_or" not in html, (
            "Raw key 'loyalty_guide.earn.consec_or' leaked into HTML"
        )
        # AND joiner must NOT appear (wrong joiner must not bleed in)
        assert "ANDJOINER_SHOULDNOTAPPEAR" not in html, (
            "The AND joiner sentinel appeared in an ANY-mode rule — wrong joiner used"
        )
        # Strike names must appear
        assert "StrX" in html, "Strike name 'StrX' not found in HTML"
        assert "StrY" in html, "Strike name 'StrY' not found in HTML"
    finally:
        Translation.query.filter(
            Translation.language == "uz",
            Translation.key.in_([
                "loyalty_guide.earn.consec_or",
                "loyalty_guide.earn.consec_line_any",
                "loyalty_guide.earn.consec_title",
                "loyalty_guide.earn.consec_repeat",
                "loyalty_guide.unit.points",
                "loyalty_guide.earn.consec_and",
            ]),
        ).delete(synchronize_session=False)
        _db.session.commit()


# ---------------------------------------------------------------------------
# Case progress_public-15
# ---------------------------------------------------------------------------


def test_progress_public_15_no_effective_rule_no_trophy_card(app, db, client):
    """/loyalty-guide does NOT render the fa-trophy card when no effective rule exists.

    Template line 152: {% if handbook.consecutive_strike_rules %} block is absent.
    Spec §8: 'rendered only when >=1 effective rule exists'.
    """
    program = get_or_create_default_program()

    # Create an inactive rule (should be filtered out)
    strike_x = make_strike_rule(program, name="InactiveStrike", window_days=30, bonus_points=50)
    make_consecutive_rule(
        program, strikes=[strike_x], name="Inactive Consec",
        required_consecutive=3, combine_mode="all", bonus_points=100,
        is_active=False,
    )
    # Create an expired rule
    now = datetime.now(timezone.utc)
    make_consecutive_rule(
        program, strikes=[strike_x], name="Expired Consec",
        required_consecutive=2, combine_mode="all", bonus_points=50,
        is_active=True,
        ends_at=now - timedelta(days=1),
    )

    resp = client.get("/loyalty-guide", follow_redirects=True)
    assert resp.status_code == 200, resp.get_data(as_text=True)[:500]
    html = resp.get_data(as_text=True)

    # fa-trophy is used exclusively in the consecutive-strike card
    assert "fa-trophy" not in html, (
        "'fa-trophy' icon found in HTML — the consecutive-strike card should not render "
        "when no effective rules exist"
    )


# ---------------------------------------------------------------------------
# Case progress_public-16
# ---------------------------------------------------------------------------


def test_progress_public_16_effective_date_gating(app, db, sample_user):
    """get_consecutive_strike_progress excludes future and past rules; includes current.

    Spec §5.3: 'A rule evaluates only if is_active AND is_effective(now)'.
    Implementation line 1744: 'if not rule.is_effective(now) or not rule.strikes: continue'.
    """
    program = get_or_create_default_program()
    strike_a = make_strike_rule(program, name="DateStrike", window_days=30, bonus_points=100)

    now = datetime.now(timezone.utc)

    # FUTURE: starts in the future
    future_rule = make_consecutive_rule(
        program, strikes=[strike_a], name="Future Rule",
        required_consecutive=3, combine_mode="all", bonus_points=100,
        is_active=True,
        starts_at=now + timedelta(days=1),
    )
    # PAST: ended in the past
    past_rule = make_consecutive_rule(
        program, strikes=[strike_a], name="Past Rule",
        required_consecutive=3, combine_mode="all", bonus_points=100,
        is_active=True,
        ends_at=now - timedelta(days=1),
    )
    # CURRENT: no date restrictions
    current_rule = make_consecutive_rule(
        program, strikes=[strike_a], name="Current Rule",
        required_consecutive=3, combine_mode="all", bonus_points=200,
        is_active=True,
    )

    # Seed 3 consecutive achievements for the user
    seed_consecutive_run(sample_user.id, strike_a, count=3)

    svc = LoyaltyService()
    result = svc.get_consecutive_strike_progress(sample_user.id)

    names = [e["name"] for e in result]
    assert "Current Rule" in names, (
        f"CURRENT rule should appear in progress but names={names}"
    )
    assert "Future Rule" not in names, (
        f"FUTURE rule should be excluded from progress but names={names}"
    )
    assert "Past Rule" not in names, (
        f"PAST rule should be excluded from progress but names={names}"
    )
    assert len(result) == 1, (
        f"Expected exactly 1 entry (Current Rule) but got {len(result)}: {names}"
    )

    # Verify the current rule's fields
    entry = result[0]
    assert entry["required_consecutive"] == 3
    assert entry["bonus_points"] == 200


# ---------------------------------------------------------------------------
# Case progress_public-17
# ---------------------------------------------------------------------------


def test_progress_public_17_admin_get_empty_list(app, db, client, admin_auth_headers):
    """Admin GET returns empty list and count=0 when no rules exist.

    Implementation admin.py lines 7393-7400.
    """
    program = get_or_create_default_program()
    # No LoyaltyConsecutiveStrikeRule rows (db fixture starts fresh)

    resp = client.get(
        f"/api/v1/admin/loyalty/consecutive-strike-rules?program_id={program.id}",
        headers=admin_auth_headers,
    )
    assert resp.status_code == 200, resp.get_json()
    data = resp.get_json()["data"]
    assert data["count"] == 0, f"Expected count=0 but got {data['count']}"
    assert data["consecutive_strike_rules"] == [], (
        f"Expected empty list but got {data['consecutive_strike_rules']}"
    )


# ---------------------------------------------------------------------------
# Case progress_public-18
# ---------------------------------------------------------------------------


def test_progress_public_18_loyalty_json_consecutive_bonuses_empty_when_no_rules(
    app, db, client
):
    """/api/public/loyalty.json always has consecutiveStrikeBonuses key; value=[] when no rules.

    Spec §8; implementation routes.py line 2099.
    Other top-level keys (streakBonuses, tiers, etc.) must still be present.
    """
    program = get_or_create_default_program()
    # No LoyaltyConsecutiveStrikeRule rows (db fixture starts fresh)

    resp = client.get("/api/public/loyalty.json")
    assert resp.status_code == 200
    assert "application/json" in resp.content_type

    data = resp.get_json()
    assert "consecutiveStrikeBonuses" in data, (
        "'consecutiveStrikeBonuses' key must always be present in /api/public/loyalty.json"
    )
    assert data["consecutiveStrikeBonuses"] == [], (
        f"Expected [] when no active rules, got {data['consecutiveStrikeBonuses']}"
    )
    # Other keys must still be present
    for key in ("streakBonuses", "tiers"):
        assert key in data, f"Key '{key}' should be present in loyalty.json regardless"


# ---------------------------------------------------------------------------
# Case progress_public-19
# ---------------------------------------------------------------------------


def test_progress_public_19_admin_get_returns_strikes_and_ids_in_to_dict(
    app, db, client, admin_auth_headers
):
    """Admin GET returns each rule's attached strikes in to_dict (strikes + strike_rule_ids fields).

    Spec §6.2; to_dict model lines 366-389; list endpoint annotates with translations.
    """
    program = get_or_create_default_program()
    s1 = make_strike_rule(program, name="Strike-One", window_days=30, bonus_points=100)
    s2 = make_strike_rule(program, name="Strike-Two", window_days=14, bonus_points=50)

    rule = make_consecutive_rule(
        program, strikes=[s1, s2], name="Two-Strike Rule",
        required_consecutive=5, combine_mode="any", bonus_points=300,
    )

    resp = client.get(
        f"/api/v1/admin/loyalty/consecutive-strike-rules?program_id={program.id}",
        headers=admin_auth_headers,
    )
    assert resp.status_code == 200, resp.get_json()
    data = resp.get_json()["data"]
    assert data["count"] == 1

    r = data["consecutive_strike_rules"][0]

    # Both strike IDs must be present
    assert set(r["strike_rule_ids"]) == {s1.id, s2.id}, (
        f"strike_rule_ids should contain both {s1.id} and {s2.id}, got {r['strike_rule_ids']}"
    )

    # strikes list should have 2 entries with expected fields
    assert len(r["strikes"]) == 2, f"Expected 2 strike objects, got {r['strikes']}"
    strike_names_in_response = {s["name"] for s in r["strikes"]}
    assert strike_names_in_response == {"Strike-One", "Strike-Two"}, (
        f"Strike names mismatch: {strike_names_in_response}"
    )
    for s_entry in r["strikes"]:
        assert "id" in s_entry
        assert "name" in s_entry
        assert "window_days" in s_entry

    assert r["combine_mode"] == "any"
    assert r["required_consecutive"] == 5

    # translations key should also be present (admin.py line 7396)
    assert "translations" in r, "Admin list response should include 'translations' key per rule"


# ---------------------------------------------------------------------------
# Case progress_public-20
# ---------------------------------------------------------------------------


def test_progress_public_20_admin_delete_removes_rule_second_delete_404(
    app, db, client, admin_auth_headers
):
    """Admin DELETE removes rule; GET shows count=0; second DELETE returns 404.

    Spec §6.2: 'DELETE ... hard delete (no per-user state; safe)'.
    Implementation admin.py lines 7558-7575.
    """
    program = get_or_create_default_program()
    s1 = make_strike_rule(program, name="DeleteStrike", window_days=30, bonus_points=50)
    rule = make_consecutive_rule(
        program, strikes=[s1], name="Delete-Me Rule",
        required_consecutive=3, combine_mode="all", bonus_points=100,
    )
    rule_id = rule.id

    # First DELETE → 200
    del_resp = client.delete(
        f"/api/v1/admin/loyalty/consecutive-strike-rules/{rule_id}",
        headers=admin_auth_headers,
    )
    assert del_resp.status_code == 200, del_resp.get_json()

    # GET list → count=0
    list_resp = client.get(
        f"/api/v1/admin/loyalty/consecutive-strike-rules?program_id={program.id}",
        headers=admin_auth_headers,
    )
    assert list_resp.status_code == 200
    assert list_resp.get_json()["data"]["count"] == 0

    # Second DELETE → 404 (rule no longer exists)
    del_resp2 = client.delete(
        f"/api/v1/admin/loyalty/consecutive-strike-rules/{rule_id}",
        headers=admin_auth_headers,
    )
    assert del_resp2.status_code == 404, (
        f"Second DELETE on deleted rule should return 404, got {del_resp2.status_code}"
    )

    # DB confirms 0 LoyaltyConsecutiveStrikeRule rows
    count = LoyaltyConsecutiveStrikeRule.query.filter_by(id=rule_id).count()
    assert count == 0, f"Rule should be absent from DB, found {count} rows"


# ---------------------------------------------------------------------------
# Case progress_public-21
# ---------------------------------------------------------------------------


def test_progress_public_21_combine_mode_all_per_strike_active_flags_disagree(
    app, db, sample_user
):
    """combine_mode=all: per_strike active flags are per-strike (not derived from combined).

    When one strike has a recent achievement (active=True) but the other has no
    achievement in the last 2*window_days (active=False), the combined_current
    reflects min(active_run, reset_run) = 0, but the per_strike entries must
    have DISAGREEING active flags: one True and one False.

    This exercises the spec §6.1 invariant: 'active=false signals the run is at
    risk / already broken for the next achievement (display hint only)' and
    confirms that active is a per-strike flag, NOT derived from combined_current.

    Setup
    -----
    - Strike A (window=30): 3 consecutive achievements ending 25 days ago.
      Last achievement 25d ago < 2*30=60d → active=True, current=3.
    - Strike B (window=20): last achievement 45 days ago.
      45d >= 2*20=40d → active=False (run broken), current=1 (only latest counts
      before gap resets).  Because gap > 2*W the consecutive run is length 1
      (the single latest achievement still counts as a run-of-1).
    - N=3, combine_mode=all → combined_current = min(3, 1) = 1.

    Assertions
    ----------
    - per_strike map has StrikeA.active=True and StrikeB.active=False (DISAGREE).
    - combined_current == 1 (the min, reflecting the weaker / reset strike).
    - StrikeA.current == 3, StrikeB.current == 1 (exact values, not capped beyond N).
    """
    program = get_or_create_default_program()

    strike_a = make_strike_rule(
        program, name="StrikeA-W30", window_days=30, bonus_points=100
    )
    strike_b = make_strike_rule(
        program, name="StrikeB-W20", window_days=20, bonus_points=100
    )

    rule = make_consecutive_rule(
        program,
        strikes=[strike_a, strike_b],
        name="AllRule-PartialReset",
        required_consecutive=3,
        combine_mode="all",
        bonus_points=500,
    )

    # Capture now close to the evaluation call so boundary arithmetic is exact.
    def _now():
        from datetime import datetime, timezone
        return datetime.now(timezone.utc)

    now = _now()

    # Strike A: 3 consecutive achievements, each spaced 30d apart.
    # Most recent: 25 days ago (25 < 2*30=60 → active).
    for k in range(3):
        # k=0 → oldest (85d ago), k=1 → 55d ago, k=2 → most recent (25d ago)
        days_ago = 25 + 30 * (2 - k)
        seed_strike_achievement(sample_user.id, strike_a, now - timedelta(days=days_ago))

    # Strike B: single achievement 45 days ago (45 >= 2*20=40 → active=False).
    # The run is length 1 (the most-recent achievement is always counted as run-of-1
    # even though it is stale — the run logic returns min 1 when there is an entry).
    seed_strike_achievement(sample_user.id, strike_b, now - timedelta(days=45))

    svc = LoyaltyService()
    result = svc.get_consecutive_strike_progress(sample_user.id)

    assert len(result) == 1, f"Expected exactly 1 rule entry, got {result}"
    entry = result[0]

    per_strike_map = {ps["strike_name"]: ps for ps in entry["per_strike"]}
    assert "StrikeA-W30" in per_strike_map, "StrikeA-W30 missing from per_strike"
    assert "StrikeB-W20" in per_strike_map, "StrikeB-W20 missing from per_strike"

    ps_a = per_strike_map["StrikeA-W30"]
    ps_b = per_strike_map["StrikeB-W20"]

    # Active flags must DISAGREE — this is the core invariant being tested.
    assert ps_a["active"] is True, (
        "StrikeA-W30 should be active (last achievement 25d < 2*30=60d ago) "
        f"but got active={ps_a['active']}"
    )
    assert ps_b["active"] is False, (
        "StrikeB-W20 should be inactive (last achievement 45d >= 2*20=40d ago) "
        f"but got active={ps_b['active']}"
    )
    assert ps_a["active"] != ps_b["active"], (
        "per_strike active flags should DISAGREE for a partial-reset scenario "
        f"but got A={ps_a['active']}, B={ps_b['active']}"
    )

    # Per-strike current values
    assert ps_a["current"] == 3, (
        f"StrikeA-W30 current should be 3 (3 consecutive) but got {ps_a['current']}"
    )
    assert ps_b["current"] == 1, (
        f"StrikeB-W20 current should be 1 (single stale achievement) but got {ps_b['current']}"
    )

    # combined_current = min(3, 1) = 1 for combine_mode=all
    assert entry["combined_current"] == 1, (
        f"combined_current should be min(3,1)=1 for combine_mode=all but got "
        f"{entry['combined_current']}"
    )
