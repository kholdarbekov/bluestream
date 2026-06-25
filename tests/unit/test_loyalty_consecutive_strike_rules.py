from datetime import datetime, timezone, timedelta

from business_app import db
from business_app.models.loyalty import (
    LoyaltyProgram,
    LoyaltyStreakRule,
    LoyaltyConsecutiveStrikeRule,
)
from business_app.utils.constants import LoyaltyActionType


def _program():
    p = LoyaltyProgram(name="Default", is_active=True, is_default=True)
    db.session.add(p)
    db.session.commit()
    return p


def _strike(program, name="3 in 30", required_orders=3, window_days=30, bonus_points=300):
    r = LoyaltyStreakRule(
        program_id=program.id,
        name=name,
        required_orders=required_orders,
        window_days=window_days,
        bonus_points=bonus_points,
        is_active=True,
    )
    db.session.add(r)
    db.session.commit()
    return r


def test_action_type_value():
    assert LoyaltyActionType.CONSECUTIVE_STREAK_BONUS.value == "consecutive_streak_bonus"


def test_model_to_dict_and_strikes(app, db):
    with app.app_context():
        program = _program()
        s1 = _strike(program, name="3 in 30", window_days=30)
        s2 = _strike(program, name="5 in 40", required_orders=5, window_days=40)
        rule = LoyaltyConsecutiveStrikeRule(
            program_id=program.id,
            name="6-in-a-row Champion",
            required_consecutive=6,
            combine_mode="all",
            bonus_points=1000,
            is_active=True,
        )
        rule.strikes = [s1, s2]
        db.session.add(rule)
        db.session.commit()

        d = rule.to_dict()
        assert d["required_consecutive"] == 6
        assert d["combine_mode"] == "all"
        assert d["bonus_points"] == 1000
        assert sorted(d["strike_rule_ids"]) == sorted([s1.id, s2.id])
        assert {s["name"] for s in d["strikes"]} == {"3 in 30", "5 in 40"}


def test_is_effective_window(app, db):
    with app.app_context():
        program = _program()
        now = datetime.now(timezone.utc)
        rule = LoyaltyConsecutiveStrikeRule(
            program_id=program.id,
            name="r",
            required_consecutive=6,
            combine_mode="all",
            bonus_points=100,
            is_active=True,
            starts_at=now - timedelta(days=1),
            ends_at=now + timedelta(days=1),
        )
        db.session.add(rule)
        db.session.commit()
        assert rule.is_effective(now) is True

        rule.is_active = False
        assert rule.is_effective(now) is False
        rule.is_active = True
        rule.ends_at = now - timedelta(hours=1)
        assert rule.is_effective(now) is False


from business_app.models.loyalty import LoyaltyTransaction
from business_app.utils.constants import LoyaltyTransactionType
from business_app.services.loyalty_service import LoyaltyService


def _award_strike(user_id, strike_rule_id, when):
    """Insert a raw STREAK_BONUS ledger row dated ``when`` (mirrors how
    update_streak records an order-strike achievement)."""
    t = LoyaltyTransaction(
        user_id=user_id,
        transaction_type=LoyaltyTransactionType.EARNED,
        points=300,
        description="strike",
        remaining_points=300,
        extra_data={"action_type": "streak_bonus", "streak_rule_id": strike_rule_id},
    )
    db.session.add(t)
    db.session.flush()
    t.created_at = when
    db.session.commit()
    return t


def test_consecutive_run_counts_back_to_back(app, sample_user):
    with app.app_context():
        program = _program()
        s = _strike(program, window_days=30)
        now = datetime.now(timezone.utc)
        # 4 achievements ~30 days apart (each gap < 60d) -> run of 4
        for k in range(4):
            _award_strike(sample_user.id, s.id, now - timedelta(days=30 * (3 - k)))
        svc = LoyaltyService()
        count, run_start = svc._strike_consecutive_run(sample_user.id, s, now)
        assert count == 4
        assert run_start is not None


def test_consecutive_run_resets_on_skipped_period(app, sample_user):
    with app.app_context():
        program = _program()
        s = _strike(program, window_days=30)
        now = datetime.now(timezone.utc)
        # old pair, then a 90-day gap (> 60d), then 2 recent back-to-back
        _award_strike(sample_user.id, s.id, now - timedelta(days=200))
        _award_strike(sample_user.id, s.id, now - timedelta(days=170))
        _award_strike(sample_user.id, s.id, now - timedelta(days=30))
        _award_strike(sample_user.id, s.id, now - timedelta(days=1))
        svc = LoyaltyService()
        count, _ = svc._strike_consecutive_run(sample_user.id, s, now)
        assert count == 2  # only the most-recent unbroken run


def test_get_consecutive_strike_progress_caps_at_n(app, sample_user):
    with app.app_context():
        program = _program()
        s = _strike(program, window_days=30)
        now = datetime.now(timezone.utc)
        for k in range(8):
            _award_strike(sample_user.id, s.id, now - timedelta(days=30 * (7 - k)))
        rule = LoyaltyConsecutiveStrikeRule(
            program_id=program.id, name="champ", required_consecutive=6,
            combine_mode="all", bonus_points=1000, is_active=True,
        )
        rule.strikes = [s]
        db.session.add(rule)
        db.session.commit()
        svc = LoyaltyService()
        prog = svc.get_consecutive_strike_progress(sample_user.id)
        assert len(prog) == 1
        assert prog[0]["required_consecutive"] == 6
        assert prog[0]["combined_current"] == 6  # capped at N even though run is 8
        assert prog[0]["per_strike"][0]["current"] == 6


def _consec_rule(program, strikes, n=6, combine_mode="all", bonus=1000):
    rule = LoyaltyConsecutiveStrikeRule(
        program_id=program.id, name="champ", required_consecutive=n,
        combine_mode=combine_mode, bonus_points=bonus, is_active=True,
    )
    rule.strikes = list(strikes)
    db.session.add(rule)
    db.session.commit()
    return rule


def _consec_award_total(user_id, rule_id):
    total = 0
    for t in LoyaltyTransaction.query.filter_by(user_id=user_id).all():
        ed = t.extra_data or {}
        if (
            ed.get("action_type") == "consecutive_streak_bonus"
            and ed.get("consecutive_strike_rule_id") == rule_id
        ):
            total += t.points
    return total


def test_update_consecutive_awards_when_all_reach_n(app, sample_user):
    with app.app_context():
        program = _program()
        a = _strike(program, name="A", window_days=30)
        b = _strike(program, name="B", required_orders=5, window_days=40)
        rule = _consec_rule(program, [a, b], n=6, combine_mode="all", bonus=1000)
        now = datetime.now(timezone.utc)
        for k in range(6):
            _award_strike(sample_user.id, a.id, now - timedelta(days=30 * (5 - k)))
            _award_strike(sample_user.id, b.id, now - timedelta(days=40 * (5 - k)))
        svc = LoyaltyService()
        svc.update_consecutive_strikes(sample_user.id)
        assert _consec_award_total(sample_user.id, rule.id) == 1000


def test_update_consecutive_all_blocks_when_one_short(app, sample_user):
    with app.app_context():
        program = _program()
        a = _strike(program, name="A", window_days=30)
        b = _strike(program, name="B", window_days=40)
        rule = _consec_rule(program, [a, b], n=6, combine_mode="all", bonus=1000)
        now = datetime.now(timezone.utc)
        for k in range(6):
            _award_strike(sample_user.id, a.id, now - timedelta(days=30 * (5 - k)))
        for k in range(3):  # B only reaches 3
            _award_strike(sample_user.id, b.id, now - timedelta(days=40 * (2 - k)))
        svc = LoyaltyService()
        svc.update_consecutive_strikes(sample_user.id)
        assert _consec_award_total(sample_user.id, rule.id) == 0


def test_update_consecutive_any_awards_on_one(app, sample_user):
    with app.app_context():
        program = _program()
        a = _strike(program, name="A", window_days=30)
        b = _strike(program, name="B", window_days=40)
        rule = _consec_rule(program, [a, b], n=6, combine_mode="any", bonus=500)
        now = datetime.now(timezone.utc)
        for k in range(6):
            _award_strike(sample_user.id, a.id, now - timedelta(days=30 * (5 - k)))
        svc = LoyaltyService()
        svc.update_consecutive_strikes(sample_user.id)
        assert _consec_award_total(sample_user.id, rule.id) == 500


def test_update_consecutive_is_idempotent(app, sample_user):
    with app.app_context():
        program = _program()
        a = _strike(program, name="A", window_days=30)
        rule = _consec_rule(program, [a], n=6, combine_mode="all", bonus=1000)
        now = datetime.now(timezone.utc)
        for k in range(6):
            _award_strike(sample_user.id, a.id, now - timedelta(days=30 * (5 - k)))
        svc = LoyaltyService()
        svc.update_consecutive_strikes(sample_user.id)
        svc.update_consecutive_strikes(sample_user.id)  # re-run must not double-award
        assert _consec_award_total(sample_user.id, rule.id) == 1000


def test_update_consecutive_repeats_every_n(app, sample_user):
    with app.app_context():
        program = _program()
        a = _strike(program, name="A", window_days=30)
        rule = _consec_rule(program, [a], n=6, combine_mode="all", bonus=1000)
        now = datetime.now(timezone.utc)
        for k in range(12):  # 12 back-to-back = two completed runs of 6
            _award_strike(sample_user.id, a.id, now - timedelta(days=30 * (11 - k)))
        svc = LoyaltyService()
        svc.update_consecutive_strikes(sample_user.id)
        assert _consec_award_total(sample_user.id, rule.id) == 2000
