from datetime import datetime, timezone
from business_app import db
from business_app.models.loyalty import LoyaltyProgram, LoyaltyStreakRule
from business_app.models.user import User
from business_app.utils.password_security import hash_password


def _default_program():
    p = LoyaltyProgram.query.filter_by(is_default=True).first()
    if not p:
        p = LoyaltyProgram(name="Default", is_active=True, is_default=True)
        db.session.add(p)
        db.session.commit()
    return p


def _make_user():
    import uuid
    u = User(
        first_name="Streak",
        last_name="Tester",
        phone="+99890" + uuid.uuid4().hex[:7],
        password_hash=hash_password("TestPassword123!"),
    )
    db.session.add(u)
    db.session.commit()
    return u


def _make_delivered_order(user_id, total, created_at):
    from business_app.models.order import Order
    from decimal import Decimal
    from shared.enums import OrderStatus
    o = Order(
        user_id=user_id,
        status=OrderStatus.DELIVERED,
        subtotal=Decimal(str(total)),
        total_amount=Decimal(str(total)),
    )
    db.session.add(o)
    db.session.flush()
    o.created_at = created_at
    db.session.commit()
    return o


def test_qualifying_order_count(app, db):
    from datetime import timedelta
    from business_app.services.loyalty_service import LoyaltyService
    with app.app_context():
        svc = LoyaltyService()
        user = _make_user()
        now = datetime.now(timezone.utc)
        _make_delivered_order(user.id, 60000, now - timedelta(days=2))
        _make_delivered_order(user.id, 40000, now - timedelta(days=3))   # below min
        _make_delivered_order(user.id, 70000, now - timedelta(days=40))  # outside window
        rule = LoyaltyStreakRule(name="r", required_orders=2, window_days=30,
                                 bonus_points=100, min_order_amount=50000)
        assert svc._qualifying_order_count(user.id, rule, now) == 1   # min applied
        rule.min_order_amount = None
        assert svc._qualifying_order_count(user.id, rule, now) == 2   # no min


def test_streak_rule_to_dict(app, db):
    with app.app_context():
        program = _default_program()
        rule = LoyaltyStreakRule(
            program_id=program.id, name="Frequent Buyer", required_orders=3,
            window_days=30, bonus_points=300, min_order_amount=50000,
            display_order=1, is_active=True,
        )
        db.session.add(rule)
        db.session.commit()

        d = rule.to_dict()
        assert d["name"] == "Frequent Buyer"
        assert d["required_orders"] == 3
        assert d["window_days"] == 30
        assert d["bonus_points"] == 300
        assert float(d["min_order_amount"]) == 50000.0
        assert d["is_active"] is True
        assert d["starts_at"] is None and d["ends_at"] is None


def test_streak_rule_is_effective():
    from datetime import timedelta
    now = datetime(2026, 6, 16, tzinfo=timezone.utc)
    r = LoyaltyStreakRule(name="x", required_orders=1, window_days=1, bonus_points=1, is_active=True)
    assert r.is_effective(now) is True
    r.is_active = False
    assert r.is_effective(now) is False
    r.is_active = True
    r.starts_at = now + timedelta(days=1)
    assert r.is_effective(now) is False
    r.starts_at = now - timedelta(days=1)
    r.ends_at = now - timedelta(hours=1)
    assert r.is_effective(now) is False
    r.ends_at = now + timedelta(days=1)
    assert r.is_effective(now) is True


def test_award_points_merges_extra_data(app, db):
    from business_app.services.loyalty_service import LoyaltyService
    from business_app.utils.constants import LoyaltyActionType
    with app.app_context():
        svc = LoyaltyService()
        user = _make_user()
        txn = svc.award_points(
            user.id, 300, "3 orders in 30 days",
            LoyaltyActionType.STREAK_BONUS, extra_data={"streak_rule_id": 7},
        )
        assert txn.extra_data["streak_rule_id"] == 7
        assert txn.extra_data["action_type"] == "streak_bonus"


def _make_program_with_rule(**kw):
    p = _default_program()
    defaults = dict(program_id=p.id, name="3 in 30", required_orders=3,
                    window_days=30, bonus_points=300, is_active=True)
    defaults.update(kw)
    r = LoyaltyStreakRule(**defaults)
    db.session.add(r)
    db.session.commit()
    return p, r


def _streak_points(user_id):
    from business_app.models.loyalty import LoyaltyTransaction
    txns = LoyaltyTransaction.query.filter_by(user_id=user_id).all()
    return sum(t.points for t in txns
               if (t.extra_data or {}).get("action_type") == "streak_bonus")


def test_update_streak_awards_when_threshold_met(app, db):
    from datetime import timedelta
    from business_app.services.loyalty_service import LoyaltyService
    with app.app_context():
        svc = LoyaltyService(); user = _make_user(); _make_program_with_rule()
        now = datetime.now(timezone.utc)
        for i in range(3): _make_delivered_order(user.id, 10000, now - timedelta(days=i))
        svc.update_streak(user.id)
        assert _streak_points(user.id) == 300


def test_update_streak_not_awarded_below_threshold(app, db):
    from datetime import timedelta
    from business_app.services.loyalty_service import LoyaltyService
    with app.app_context():
        svc = LoyaltyService(); user = _make_user(); _make_program_with_rule()
        now = datetime.now(timezone.utc)
        _make_delivered_order(user.id, 10000, now)
        svc.update_streak(user.id)
        assert _streak_points(user.id) == 0


def test_update_streak_cooldown_blocks_then_allows(app, db):
    from datetime import timedelta
    from business_app.services.loyalty_service import LoyaltyService
    from business_app.models.loyalty import LoyaltyTransaction
    with app.app_context():
        svc = LoyaltyService(); user = _make_user(); _make_program_with_rule()
        now = datetime.now(timezone.utc)
        for i in range(3): _make_delivered_order(user.id, 10000, now - timedelta(days=i))
        svc.update_streak(user.id)
        svc.update_streak(user.id)
        assert _streak_points(user.id) == 300
        for t in LoyaltyTransaction.query.filter_by(user_id=user.id).all():
            if (t.extra_data or {}).get("action_type") == "streak_bonus":
                t.created_at = now - timedelta(days=31)
        for i in range(3): _make_delivered_order(user.id, 10000, now - timedelta(days=i))
        db.session.commit()
        svc.update_streak(user.id)
        assert _streak_points(user.id) == 600


def test_update_streak_all_qualifying_rules_award(app, db):
    from datetime import timedelta
    from business_app.services.loyalty_service import LoyaltyService
    with app.app_context():
        svc = LoyaltyService(); user = _make_user(); p = _default_program()
        db.session.add(LoyaltyStreakRule(program_id=p.id, name="3 in 30", required_orders=3,
                                         window_days=30, bonus_points=300, is_active=True))
        db.session.add(LoyaltyStreakRule(program_id=p.id, name="5 in 30", required_orders=5,
                                         window_days=30, bonus_points=200, is_active=True))
        db.session.commit()
        now = datetime.now(timezone.utc)
        for i in range(5): _make_delivered_order(user.id, 10000, now - timedelta(days=i))
        svc.update_streak(user.id)
        assert _streak_points(user.id) == 500


def test_update_streak_skips_inactive_and_out_of_date(app, db):
    from datetime import timedelta
    from business_app.services.loyalty_service import LoyaltyService
    with app.app_context():
        svc = LoyaltyService(); user = _make_user(); p = _default_program()
        now = datetime.now(timezone.utc)
        db.session.add(LoyaltyStreakRule(program_id=p.id, name="inactive", required_orders=1,
                                         window_days=30, bonus_points=100, is_active=False))
        db.session.add(LoyaltyStreakRule(program_id=p.id, name="future", required_orders=1,
                                         window_days=30, bonus_points=100, is_active=True,
                                         starts_at=now + timedelta(days=5)))
        db.session.commit()
        _make_delivered_order(user.id, 10000, now)
        svc.update_streak(user.id)
        assert _streak_points(user.id) == 0


def test_get_streak_progress(app, db):
    from datetime import timedelta
    from business_app.services.loyalty_service import LoyaltyService
    with app.app_context():
        svc = LoyaltyService(); user = _make_user(); p = _default_program()
        db.session.add(LoyaltyStreakRule(program_id=p.id, name="3 in 30", required_orders=3,
                                         window_days=30, bonus_points=300, is_active=True, display_order=0))
        db.session.commit()
        now = datetime.now(timezone.utc)
        for i in range(2): _make_delivered_order(user.id, 10000, now - timedelta(days=i))
        progress = svc.get_streak_progress(user.id)
        assert len(progress) == 1
        assert progress[0]["name"] == "3 in 30"
        assert progress[0]["required_orders"] == 3
        assert progress[0]["current_orders"] == 2
        assert progress[0]["bonus_points"] == 300


def test_program_and_tier_translatable(app, db):
    from business_app.models.loyalty import LoyaltyProgram, LoyaltyTierConfig
    with app.app_context():
        p = LoyaltyProgram(name="Club", is_active=True)
        db.session.add(p); db.session.commit()
        p.set_translations({"name": {"ru": "Клуб"}})
        db.session.commit()
        assert p.get_translated("name", "ru") == "Клуб"
        # "uz" is the default language — returns base column value when no uz translation exists
        assert p.get_translated("name", "uz") == "Club"  # fallback to base column
        t = LoyaltyTierConfig(program_id=p.id, name="Gold", min_points=0)
        db.session.add(t); db.session.commit()
        t.set_translations({"name": {"uz": "Oltin"}})
        db.session.commit()
        assert t.get_translated("name", "uz") == "Oltin"
