"""E2E tests: consecutive-strike bonus rule — order-flow / ledger integration.

These tests drive the REAL award path:
  OrderService._handle_status_change_actions(order, DELIVERED)
  → maybe_award_purchase_points + update_streak → update_consecutive_strikes

Each test asserts on the REAL ledger (LoyaltyTransaction / LoyaltyPoints) with
exact field values anchored to the spec:
  docs/superpowers/specs/2026-06-24-consecutive-strike-bonus-rule-design.md

Shared helpers imported from tests/e2e/_consecutive_strike_helpers.py.
Registered marker: @pytest.mark.e2e  (loyalty is NOT registered — never use it).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest

from business_app import db as _db
from business_app.models.loyalty import (
    LoyaltyPoints,
    LoyaltyProgram,
    LoyaltyTierConfig,
    LoyaltyTransaction,
)
from business_app.models.order import Order, OrderItem
from business_app.models.payment import Payment
from business_app.services.loyalty_service import LoyaltyService
from business_app.services.order_service import OrderService
from business_app.services.payment_service import PaymentService
from business_app.utils.constants import LoyaltyActionType, LoyaltyTransactionType
from shared.enums import OrderStatus, PaymentMethod, PaymentStatus

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
# Module-level marker
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.e2e


# ---------------------------------------------------------------------------
# Autouse fixture: silence notification side effects in every test
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _silence(monkeypatch):
    silence_loyalty_notifications(monkeypatch)


# ---------------------------------------------------------------------------
# Shared order-service fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def order_service(mock_inventory_service):
    from types import SimpleNamespace

    mock_inventory_service.check_multiple_products_availability.return_value = [
        SimpleNamespace(
            product_id=0,
            requested_quantity=2,
            available_quantity=100,
            reserved_quantity=0,
            is_available=True,
            reason="Available",
        )
    ]
    mock_inventory_service.reserve_inventory.return_value = {"success": True, "expires_at": None}
    mock_inventory_service.release_reservations.return_value = {"success": True}
    return OrderService(inventory_service=mock_inventory_service)


# ---------------------------------------------------------------------------
# Helper: get LoyaltyPoints row for a user (may not exist yet)
# ---------------------------------------------------------------------------


def _account(user_id: int):
    return LoyaltyPoints.query.filter_by(user_id=user_id).first()


def _balance(user_id: int) -> int:
    acct = _account(user_id)
    return acct.current_balance if acct else 0


def _total_earned(user_id: int) -> int:
    acct = _account(user_id)
    return acct.total_earned if acct else 0


def _purchase_txns(user_id: int):
    return [
        t
        for t in LoyaltyTransaction.query.filter_by(user_id=user_id).all()
        if (t.extra_data or {}).get("action_type") == LoyaltyActionType.PURCHASE.value
    ]


def _streak_txns(user_id: int):
    return [
        t
        for t in LoyaltyTransaction.query.filter_by(user_id=user_id).all()
        if (t.extra_data or {}).get("action_type") == LoyaltyActionType.STREAK_BONUS.value
    ]


# ===========================================================================
# CASE order_flow_ledger-001
# Prepaid order completes Nth strike via real delivery — consecutive bonus fires
# ===========================================================================


class TestCase001PrepaidConsecutiveBonusFires:
    def test_prepaid_nth_strike_triggers_consecutive_bonus(
        self, app, db, sample_user, order_service
    ):
        """Spec §5.4: N-1 prior backdated achievements + 1 real delivery = run of N
        → update_consecutive_strikes fires milestone 1.
        Expected: 3 ledger rows — purchase EARNED (300), streak EARNED, consecutive BONUS (500).
        """
        program = get_or_create_default_program()
        # Confirm uzs_per_point=250 from helper (see make default program)
        program.uzs_per_point = 250
        program.points_expiry_days = 365
        _db.session.commit()

        strike = make_strike_rule(
            program, name="3 in 30", required_orders=3, window_days=30, bonus_points=100
        )
        crule = make_consecutive_rule(
            program, strikes=[strike], name="Champion", required_consecutive=3,
            combine_mode="all", bonus_points=500
        )

        # Seed N-1=2 backdated consecutive achievements
        seed_consecutive_run(sample_user.id, strike, count=2)

        # Seed required_orders-1=2 delivered orders in the window so the final
        # deliver_paid_order becomes the 3rd qualifying order and update_streak fires.
        seed_delivered_orders(sample_user.id, count=2, total=Decimal("50000"), newest_days_ago=5, spacing_days=10)

        # Real delivery — sets is_paid=True, then fires DELIVERED edge
        order = deliver_paid_order(
            order_service, sample_user.id, total=75_000, payment="prepaid"
        )

        # --- Assertions ---
        all_txns = LoyaltyTransaction.query.filter_by(user_id=sample_user.id).all()

        purchase_rows = [
            t for t in all_txns
            if (t.extra_data or {}).get("action_type") == LoyaltyActionType.PURCHASE.value
        ]
        streak_rows = [
            t for t in all_txns
            if (t.extra_data or {}).get("action_type") == LoyaltyActionType.STREAK_BONUS.value
            and (t.extra_data or {}).get("streak_rule_id") == strike.id
        ]
        consec_rows = consecutive_awards(sample_user.id, crule.id)

        # Exactly 3 ledger rows from this delivery (2 seeded + new ones)
        # The 2 backdated rows are also in all_txns but this verifies new rows exist
        assert len(purchase_rows) == 1, f"Expected 1 purchase row, got {len(purchase_rows)}"
        assert len(streak_rows) == 3, (
            f"Expected 3 streak rows total (2 seeded + 1 new), got {len(streak_rows)}"
        )
        assert len(consec_rows) == 1, f"Expected 1 consecutive BONUS row, got {len(consec_rows)}"

        # Purchase row: EARNED type, points=floor(75000/250)=300
        pr = purchase_rows[0]
        assert pr.transaction_type == LoyaltyTransactionType.EARNED
        assert pr.points == 300
        assert pr.order_id == order.id
        assert pr.remaining_points == 300

        # Streak row (the new one): EARNED, action_type=streak_bonus, streak_rule_id set
        from business_app.utils.timezone_utils import ensure_utc
        new_streak = [
            t for t in streak_rows
            if t.created_at and ensure_utc(t.created_at) > datetime.now(timezone.utc) - timedelta(minutes=5)
        ]
        assert len(new_streak) >= 1
        sr = new_streak[0]
        assert sr.transaction_type == LoyaltyTransactionType.EARNED
        assert (sr.extra_data or {}).get("streak_rule_id") == strike.id

        # Consecutive BONUS row
        cr = consec_rows[0]
        assert cr.transaction_type == LoyaltyTransactionType.BONUS
        assert cr.points == 500
        assert (cr.extra_data or {}).get("consecutive_strike_rule_id") == crule.id
        assert (cr.extra_data or {}).get("milestone") == 1

        # expires_at within ~365+1 days from now
        _now = datetime.now(timezone.utc)
        cutoff = _now + timedelta(days=364)
        for t in [pr, sr, cr]:
            if t.expires_at:
                expires_utc = ensure_utc(t.expires_at)
                assert expires_utc > cutoff, (
                    f"expires_at {t.expires_at} too early (expected ~365d from now)"
                )

        # Balance: 300 (purchase) + 100 (streak bonus) + 500 (consecutive) = 900
        expected_balance = 300 + strike.bonus_points + 500
        assert _balance(sample_user.id) == expected_balance
        assert _total_earned(sample_user.id) == expected_balance


# ===========================================================================
# CASE order_flow_ledger-002
# COD cash-collection edge: consecutive bonus fires at delivery, purchase at cash
# ===========================================================================


class TestCase002CodCashEdge:
    def test_cod_delivery_fires_consecutive_then_cash_adds_purchase(
        self, app, db, sample_user, sample_product, order_service
    ):
        """Spec §5.4: update_streak called at DELIVERED edge only.
        Cash-collection only calls maybe_award_purchase_points (not update_streak again).
        """
        program = get_or_create_default_program()
        program.uzs_per_point = 250
        program.points_expiry_days = 365
        _db.session.commit()

        strike = make_strike_rule(
            program, name="3 in 30 COD", required_orders=3, window_days=30, bonus_points=100
        )
        crule = make_consecutive_rule(
            program, strikes=[strike], name="COD Champion", required_consecutive=3,
            combine_mode="all", bonus_points=500
        )

        # Seed N-1=2 backdated consecutive achievements
        seed_consecutive_run(sample_user.id, strike, count=2)

        # Seed required_orders-1=2 delivered orders in the window so the COD order
        # becomes the 3rd qualifying order and update_streak fires at delivery.
        seed_delivered_orders(sample_user.id, count=2, total=Decimal("50000"), newest_days_ago=5, spacing_days=10)

        # Build order directly as COD using a real product so FK constraints pass
        import uuid
        total = Decimal("75000")
        order = Order(
            user_id=sample_user.id,
            order_number=f"COD-{uuid.uuid4().hex[:8].upper()}",
            status=OrderStatus.PENDING,
            subtotal=total,
            total_amount=total,
            payment_method=PaymentMethod.CASH,
        )
        _db.session.add(order)
        _db.session.flush()
        _db.session.add(
            OrderItem(
                order_id=order.id,
                product_id=sample_product.id,
                quantity=1,
                unit_price=total,
                total_price=total,
            )
        )
        _db.session.commit()

        # Verify order starts unpaid
        assert order.is_paid is not True, "COD order must start unpaid"

        # Set DELIVERED but NOT paid
        now_dt = datetime.now(timezone.utc)
        order.status = OrderStatus.DELIVERED
        order.delivered_at = now_dt
        _db.session.commit()

        # Fire the DELIVERED edge (real trigger path)
        order_service._handle_status_change_actions(order, OrderStatus.DELIVERED, commit=True)

        # --- After delivery edge ---
        all_txns_after_delivery = LoyaltyTransaction.query.filter_by(user_id=sample_user.id).all()
        streak_after = [
            t for t in all_txns_after_delivery
            if (t.extra_data or {}).get("action_type") == LoyaltyActionType.STREAK_BONUS.value
        ]
        consec_after = consecutive_awards(sample_user.id, crule.id)
        purchase_after = [
            t for t in all_txns_after_delivery
            if (t.extra_data or {}).get("action_type") == LoyaltyActionType.PURCHASE.value
        ]

        # Streak bonus awarded at delivery (total=3: 2 seeded + 1 new)
        assert len(streak_after) == 3, (
            f"Expected 3 streak rows after delivery, got {len(streak_after)}"
        )
        # Consecutive BONUS awarded at delivery (is_paid=False didn't block update_streak)
        assert len(consec_after) == 1, (
            f"Expected 1 consecutive BONUS row after delivery edge, got {len(consec_after)}"
        )
        # Purchase NOT yet awarded (is_paid=False)
        assert len(purchase_after) == 0, (
            f"Expected 0 purchase rows before cash collection, got {len(purchase_after)}"
        )

        balance_after_delivery = _balance(sample_user.id)
        assert balance_after_delivery == strike.bonus_points + 500  # no purchase yet

        # --- Now simulate cash collection to trigger maybe_award_purchase_points ---
        # We patch update_streak to verify it is NOT called again by the cash edge
        update_streak_call_count = [0]
        original_update_streak = LoyaltyService.update_streak

        def _counting_update_streak(self_svc, user_id, commit=True):
            update_streak_call_count[0] += 1
            return original_update_streak(self_svc, user_id, commit=commit)

        with patch.object(LoyaltyService, "update_streak", _counting_update_streak):
            # Mark paid via cash collection simulation
            order.is_paid = True
            order.paid_at = datetime.now(timezone.utc)
            _db.session.commit()
            # Call maybe_award_purchase_points directly (what cash collection does)
            order_service.maybe_award_purchase_points(order, commit=True)

        # Assert is_paid actually flipped
        _db.session.refresh(order)
        assert order.is_paid is True, "order.is_paid must be True after cash collection"

        # update_streak was NOT called by the cash edge
        assert update_streak_call_count[0] == 0, (
            "update_streak must NOT be called by the cash-payment edge"
        )

        # Purchase EARNED row now exists
        purchase_final = [
            t for t in LoyaltyTransaction.query.filter_by(user_id=sample_user.id).all()
            if (t.extra_data or {}).get("action_type") == LoyaltyActionType.PURCHASE.value
        ]
        assert len(purchase_final) == 1, "Purchase row should appear after cash collection"

        # No duplicate consecutive rows
        consec_final = consecutive_awards(sample_user.id, crule.id)
        assert len(consec_final) == 1, "Consecutive BONUS must not be duplicated by cash edge"

        # Balance = purchase_pts + strike_pts + consec_pts
        expected_final = int(total) // 250 + strike.bonus_points + 500
        assert _balance(sample_user.id) == expected_final


# ===========================================================================
# CASE order_flow_ledger-003
# Exact ledger field assertions for all three award types
# ===========================================================================


class TestCase003ExactLedgerFields:
    def test_exact_ledger_fields_all_three_award_types(
        self, app, db, sample_user, order_service
    ):
        """Spec §4.4 / award_points: verify transaction_type, action_type, points,
        remaining_points, expires_at for each of the 3 rows produced.
        """
        program = get_or_create_default_program()
        program.uzs_per_point = 250
        program.points_expiry_days = 180  # Non-default to verify SSOT
        _db.session.commit()

        strike = make_strike_rule(
            program, name="3 in 30 fields", required_orders=3, window_days=30, bonus_points=75
        )
        crule = make_consecutive_rule(
            program, strikes=[strike], name="Exact Fields", required_consecutive=2,
            combine_mode="all", bonus_points=750
        )

        # Seed 1 backdated achievement 40 days ago: OUTSIDE the 30-day streak
        # cooldown (so this delivery can fire a NEW strike achievement) yet within
        # gap < 2*30=60d of that new one (so the consecutive run reaches 2).
        now = datetime.now(timezone.utc)
        seed_strike_achievement(sample_user.id, strike, when=now - timedelta(days=40))

        # Seed required_orders-1=2 delivered orders in the window so this delivery
        # becomes the 3rd qualifying order and update_streak fires.
        seed_delivered_orders(sample_user.id, count=2, total=Decimal("50000"), newest_days_ago=5, spacing_days=10)

        total = 100_000
        order = deliver_paid_order(
            order_service, sample_user.id, total=total, payment="prepaid"
        )

        from business_app.utils.timezone_utils import ensure_utc as _ensure_utc

        all_txns = LoyaltyTransaction.query.filter_by(user_id=sample_user.id).all()
        by_action = {}
        for t in all_txns:
            action = (t.extra_data or {}).get("action_type")
            if action:
                by_action.setdefault(action, []).append(t)

        purchase_rows = by_action.get(LoyaltyActionType.PURCHASE.value, [])
        streak_rows = by_action.get(LoyaltyActionType.STREAK_BONUS.value, [])
        consec_rows = by_action.get(LoyaltyActionType.CONSECUTIVE_STREAK_BONUS.value, [])

        assert len(purchase_rows) == 1, "Expected exactly 1 purchase row"
        assert len(consec_rows) == 1, "Expected exactly 1 consecutive BONUS row"
        # 1 seeded + 1 new streak row
        cutoff_5m = now - timedelta(minutes=5)
        new_streak_rows = [
            t for t in streak_rows
            if (t.extra_data or {}).get("streak_rule_id") == strike.id
            and t.created_at and _ensure_utc(t.created_at) > cutoff_5m
        ]
        assert len(new_streak_rows) == 1, "Expected exactly 1 NEW streak row from this delivery"

        # (1) Purchase row
        pr = purchase_rows[0]
        assert pr.transaction_type == LoyaltyTransactionType.EARNED
        assert (pr.extra_data or {}).get("action_type") == LoyaltyActionType.PURCHASE.value
        assert pr.order_id == order.id
        expected_purchase_pts = total // 250  # floor(100000/250) = 400
        assert pr.points == expected_purchase_pts
        assert pr.remaining_points == expected_purchase_pts
        # expires_at within ~180+1 days from now (normalize before comparing)
        assert pr.expires_at is not None
        assert _ensure_utc(pr.expires_at) > now + timedelta(days=179)
        assert _ensure_utc(pr.expires_at) < now + timedelta(days=182)

        # (2) Streak row
        sr = new_streak_rows[0]
        assert sr.transaction_type == LoyaltyTransactionType.EARNED
        assert (sr.extra_data or {}).get("action_type") == LoyaltyActionType.STREAK_BONUS.value
        assert (sr.extra_data or {}).get("streak_rule_id") == strike.id
        assert sr.points == strike.bonus_points
        assert sr.remaining_points == strike.bonus_points
        assert sr.expires_at is not None
        assert _ensure_utc(sr.expires_at) > now + timedelta(days=179)
        assert _ensure_utc(sr.expires_at) < now + timedelta(days=182)

        # (3) Consecutive BONUS row
        cr = consec_rows[0]
        assert cr.transaction_type == LoyaltyTransactionType.BONUS
        assert (cr.extra_data or {}).get("action_type") == LoyaltyActionType.CONSECUTIVE_STREAK_BONUS.value
        assert (cr.extra_data or {}).get("consecutive_strike_rule_id") == crule.id
        assert (cr.extra_data or {}).get("milestone") == 1
        assert cr.points == 750
        assert cr.remaining_points == 750
        assert cr.expires_at is not None
        assert _ensure_utc(cr.expires_at) > now + timedelta(days=179)
        assert _ensure_utc(cr.expires_at) < now + timedelta(days=182)

        # Balance = 400 + 75 + 750 = 1225
        expected_balance = expected_purchase_pts + strike.bonus_points + 750
        assert _balance(sample_user.id) == expected_balance
        assert _total_earned(sample_user.id) == expected_balance


# ===========================================================================
# CASE order_flow_ledger-004
# Repeat-every-N at 2N — second milestone awarded in same trigger
# ===========================================================================


class TestCase004RepeatEveryNTwoMilestones:
    def test_two_milestones_awarded_when_run_reaches_2n(
        self, app, db, sample_user, order_service
    ):
        """Spec §5.3: 6//3=2 target_awards → two BONUS rows (milestone 1 and 2) in one call."""
        program = get_or_create_default_program()
        program.uzs_per_point = 250
        _db.session.commit()

        strike = make_strike_rule(
            program, name="3 in 30 repeat", required_orders=3, window_days=30, bonus_points=50
        )
        crule = make_consecutive_rule(
            program, strikes=[strike], name="Repeat Champion", required_consecutive=3,
            combine_mode="all", bonus_points=500
        )

        # Seed 5 backdated achievements spaced window_days=30 apart
        # k=0→oldest (150d ago), k=1→120d, k=2→90d, k=3→60d, k=4→30d
        # Each gap = 30d < 2*30=60d → all consecutive, run_length=5
        seed_consecutive_run(sample_user.id, strike, count=5)

        # Seed required_orders-1=2 delivered orders in the window so the final
        # deliver_paid_order becomes the 3rd qualifying order and update_streak fires.
        seed_delivered_orders(sample_user.id, count=2, total=Decimal("50000"), newest_days_ago=5, spacing_days=10)

        # Real delivery → 6th streak achievement → run=6 → 6//3=2 milestones
        total = 75_000
        deliver_paid_order(order_service, sample_user.id, total=total, payment="prepaid")

        consec_rows = consecutive_awards(sample_user.id, crule.id)
        assert len(consec_rows) == 2, (
            f"Expected 2 consecutive BONUS rows (milestones 1 and 2), got {len(consec_rows)}"
        )

        milestones = sorted((r.extra_data or {}).get("milestone") for r in consec_rows)
        assert milestones == [1, 2], f"Expected milestones [1,2], got {milestones}"

        for r in consec_rows:
            assert r.points == 500
            assert r.transaction_type == LoyaltyTransactionType.BONUS

        assert consecutive_award_total(sample_user.id, crule.id) == 1000


# ===========================================================================
# CASE order_flow_ledger-005
# Re-delivery idempotency — same order's delivery edge re-fired never double-awards
# ===========================================================================


class TestCase005RedeliveryIdempotency:
    def test_redelivery_does_not_double_award(self, app, db, sample_user, order_service):
        """Spec §5.3 idempotency guarantee + cooldown guard prevent double-award."""
        program = get_or_create_default_program()
        program.uzs_per_point = 250
        _db.session.commit()

        strike = make_strike_rule(
            program, name="3 in 30 idempotent", required_orders=3, window_days=30, bonus_points=100
        )
        crule = make_consecutive_rule(
            program, strikes=[strike], name="Idempotent Rule", required_consecutive=3,
            combine_mode="all", bonus_points=400
        )

        # Seed N-1=2 backdated consecutive achievements
        seed_consecutive_run(sample_user.id, strike, count=2)

        # Seed required_orders-1=2 delivered orders in the window so the first
        # deliver_paid_order becomes the 3rd qualifying order and update_streak fires.
        seed_delivered_orders(sample_user.id, count=2, total=Decimal("50000"), newest_days_ago=5, spacing_days=10)

        # First delivery
        order = deliver_paid_order(
            order_service, sample_user.id, total=75_000, payment="prepaid"
        )

        # Record counts after first delivery
        purchase_count_1 = len(_purchase_txns(sample_user.id))
        streak_count_1 = strike_achievement_count(sample_user.id, strike.id)
        consec_count_1 = len(consecutive_awards(sample_user.id, crule.id))
        balance_1 = _balance(sample_user.id)

        assert purchase_count_1 == 1
        assert streak_count_1 == 3  # 2 seeded + 1 new
        assert consec_count_1 == 1

        # Second call — same order, same DELIVERED status
        order_service._handle_status_change_actions(order, OrderStatus.DELIVERED, commit=True)

        # Nothing should change
        assert len(_purchase_txns(sample_user.id)) == purchase_count_1, (
            "Purchase must not be double-awarded"
        )
        assert strike_achievement_count(sample_user.id, strike.id) == streak_count_1, (
            "Streak must not be double-awarded (cooldown guard)"
        )
        assert len(consecutive_awards(sample_user.id, crule.id)) == consec_count_1, (
            "Consecutive BONUS must not be double-awarded (idempotency guard)"
        )
        assert _balance(sample_user.id) == balance_1, "Balance must not change on re-delivery"


# ===========================================================================
# CASE order_flow_ledger-006
# Consecutive bonus blocked when loyalty error is swallowed — delivery still completes
# ===========================================================================


class TestCase006ErrorSwallowed:
    def test_loyalty_error_swallowed_delivery_still_completes(
        self, app, db, sample_user, order_service
    ):
        """Spec §5.4 / order_service.py:1503-1518: update_streak wrapped in try/except.
        Raising RuntimeError must not propagate or roll back the order delivery.
        """
        import uuid
        from decimal import Decimal

        program = get_or_create_default_program()
        program.uzs_per_point = 250
        _db.session.commit()

        strike = make_strike_rule(
            program, name="3 in 30 error", required_orders=3, window_days=30, bonus_points=100
        )
        crule = make_consecutive_rule(
            program, strikes=[strike], name="Error Rule", required_consecutive=3,
            combine_mode="all", bonus_points=500
        )
        seed_consecutive_run(sample_user.id, strike, count=2)

        # Capture pre-delivery counts (the 2 seeded streak rows exist)
        pre_strike_count = strike_achievement_count(sample_user.id, strike.id)
        pre_consec_count = len(consecutive_awards(sample_user.id, crule.id))
        assert pre_strike_count == 2, f"Expected 2 seeded streak rows, got {pre_strike_count}"
        assert pre_consec_count == 0, "No consecutive BONUS rows before delivery"

        # Build a prepaid order
        total = Decimal("75000")
        order = Order(
            user_id=sample_user.id,
            order_number=f"ERR-{uuid.uuid4().hex[:8].upper()}",
            status=OrderStatus.PENDING,
            subtotal=total,
            total_amount=total,
            payment_method=PaymentMethod.CLICK,
        )
        _db.session.add(order)
        _db.session.flush()
        _db.session.add(
            OrderItem(
                order_id=order.id,
                product_id=1,
                quantity=1,
                unit_price=total,
                total_price=total,
            )
        )
        _db.session.commit()

        order.is_paid = True
        order.paid_at = datetime.now(timezone.utc)
        order.status = OrderStatus.DELIVERED
        order.delivered_at = datetime.now(timezone.utc)
        _db.session.commit()

        # Patch update_streak to raise
        with patch.object(LoyaltyService, "update_streak", side_effect=RuntimeError("injected failure")):
            # Must not propagate
            order_service._handle_status_change_actions(order, OrderStatus.DELIVERED, commit=True)

        # Delivery still completed
        _db.session.refresh(order)
        assert order.status == OrderStatus.DELIVERED, "Order must remain DELIVERED even when loyalty errors"

        # No NEW streak or consecutive rows added (update_streak was blocked by the exception)
        assert strike_achievement_count(sample_user.id, strike.id) == pre_strike_count, (
            "No new streak rows should be added when update_streak raised"
        )
        assert len(consecutive_awards(sample_user.id, crule.id)) == pre_consec_count, (
            "No consecutive BONUS rows should be added when update_streak raised"
        )


# ===========================================================================
# CASE order_flow_ledger-007
# 'all' mode: both strikes simultaneously reach N — bonus fires
# ===========================================================================


class TestCase007AllModeBothStrikesReachN:
    def test_all_mode_bonus_fires_when_both_reach_n_simultaneously(
        self, app, db, sample_user, order_service
    ):
        """Spec §5.2: combine_mode='all' → combined=min(per-strike counts).
        Both A and B seeded to N-1=2 → this delivery awards both → combined=3 >= N=3.
        """
        program = get_or_create_default_program()
        program.uzs_per_point = 250
        _db.session.commit()

        strike_a = make_strike_rule(
            program, name="Strike A", required_orders=3, window_days=30, bonus_points=80
        )
        strike_b = make_strike_rule(
            program, name="Strike B", required_orders=3, window_days=30, bonus_points=60
        )
        crule = make_consecutive_rule(
            program, strikes=[strike_a, strike_b], name="All Mode Rule",
            required_consecutive=3, combine_mode="all", bonus_points=700
        )

        # Seed 2 backdated consecutive achievements for each strike
        seed_consecutive_run(sample_user.id, strike_a, count=2)
        seed_consecutive_run(sample_user.id, strike_b, count=2)

        # This delivery qualifies for both A and B (both have required_orders=3 in 30d)
        # We need 3 delivered orders in the window; seed 2 old delivered orders
        now = datetime.now(timezone.utc)
        for days_ago in [29, 20]:
            old_order = Order(
                user_id=sample_user.id,
                order_number=f"OLD-{days_ago}-007",
                status=OrderStatus.DELIVERED,
                subtotal=Decimal("10000"),
                total_amount=Decimal("10000"),
                payment_method=PaymentMethod.CLICK,
            )
            _db.session.add(old_order)
            _db.session.flush()
            old_order.created_at = now - timedelta(days=days_ago)
            _db.session.commit()

        # Real delivery with is_paid=True (3rd qualifying order in window)
        order = deliver_paid_order(
            order_service, sample_user.id, total=30_000, payment="prepaid"
        )

        # Both strikes should have a new streak row
        a_count = strike_achievement_count(sample_user.id, strike_a.id)
        b_count = strike_achievement_count(sample_user.id, strike_b.id)
        # 2 seeded + 1 new for each
        assert a_count == 3, f"Strike A should have 3 streak rows, got {a_count}"
        assert b_count == 3, f"Strike B should have 3 streak rows, got {b_count}"

        # Consecutive BONUS awarded (combined = min(3,3) = 3 >= N=3)
        consec_rows = consecutive_awards(sample_user.id, crule.id)
        assert len(consec_rows) == 1, (
            f"Expected 1 consecutive BONUS row (both at N=3), got {len(consec_rows)}"
        )
        assert consec_rows[0].points == 700
        assert (consec_rows[0].extra_data or {}).get("milestone") == 1


# ===========================================================================
# CASE order_flow_ledger-008
# 'all' mode: one strike lags — bonus withheld
# ===========================================================================


class TestCase008AllModeOneLagging:
    def test_all_mode_bonus_withheld_when_one_strike_lags(
        self, app, db, sample_user, order_service
    ):
        """Spec §5.2: combine_mode='all' → combined=min; B still at 1 → combined=1 < N=3."""
        program = get_or_create_default_program()
        program.uzs_per_point = 250
        _db.session.commit()

        # Strike A: window=30, required=3
        strike_a = make_strike_rule(
            program, name="A 3in30", required_orders=3, window_days=30, bonus_points=80
        )
        # Strike B: window=40, required=10 — VERY high threshold so this delivery won't qualify
        strike_b = make_strike_rule(
            program, name="B 10in40 lag", required_orders=10, window_days=40, bonus_points=60
        )
        crule = make_consecutive_rule(
            program, strikes=[strike_a, strike_b], name="All Lag Rule",
            required_consecutive=3, combine_mode="all", bonus_points=600
        )

        # Seed 2 consecutive achievements for A; 1 for B
        seed_consecutive_run(sample_user.id, strike_a, count=2)
        seed_strike_achievement(
            sample_user.id, strike_b,
            when=datetime.now(timezone.utc) - timedelta(days=35)
        )

        # Seed 2 old delivered orders in window for A (so 3rd qualifies A)
        now = datetime.now(timezone.utc)
        for days_ago in [25, 15]:
            old_order = Order(
                user_id=sample_user.id,
                order_number=f"OLD-A-{days_ago}-008",
                status=OrderStatus.DELIVERED,
                subtotal=Decimal("10000"),
                total_amount=Decimal("10000"),
                payment_method=PaymentMethod.CLICK,
            )
            _db.session.add(old_order)
            _db.session.flush()
            old_order.created_at = now - timedelta(days=days_ago)
            _db.session.commit()

        # This delivery = 3rd for A (A now qualifies) but only 1st recent for B
        deliver_paid_order(order_service, sample_user.id, total=30_000, payment="prepaid")

        # A should have new streak achievement (2 seeded + 1 new = exactly 3)
        a_count = strike_achievement_count(sample_user.id, strike_a.id)
        assert a_count == 3, f"Strike A should have exactly 3 rows (2 seeded + 1 new), got {a_count}"

        # B should NOT have a new streak (only 1 order in 40d window < 10 required)
        b_count = strike_achievement_count(sample_user.id, strike_b.id)
        # B still at 1 (the seeded one), no new award
        assert b_count == 1, f"Strike B should remain at 1 row, got {b_count}"

        # NO consecutive BONUS (combined = min(3, 1) = 1 < N=3)
        consec_rows = consecutive_awards(sample_user.id, crule.id)
        assert len(consec_rows) == 0, (
            f"Expected 0 consecutive BONUS rows (B lags), got {len(consec_rows)}"
        )


# ===========================================================================
# CASE order_flow_ledger-009
# Tier upgrade threshold crossed after consecutive bonus — tier upgrade triggered
# ===========================================================================


class TestCase009TierUpgrade:
    def test_consecutive_bonus_triggers_tier_upgrade(
        self, app, db, sample_user, order_service
    ):
        """Spec §2 + loyalty_service.award_points→_check_tier_upgrade.
        Bronze (0-999) + Silver (1000+); user at 950 + delivery rewards crosses 1000.
        """
        program = get_or_create_default_program()
        program.uzs_per_point = 250
        program.points_expiry_days = 365
        _db.session.commit()

        # Create Bronze and Silver tiers
        bronze = LoyaltyTierConfig(
            program_id=program.id, name="Bronze", display_order=0,
            min_points=0, max_points=999, points_multiplier=1.0, is_active=True
        )
        silver = LoyaltyTierConfig(
            program_id=program.id, name="Silver", display_order=1,
            min_points=1000, max_points=None, points_multiplier=1.5, is_active=True
        )
        _db.session.add_all([bronze, silver])
        _db.session.commit()

        # Strike: bonus_points=25
        strike = make_strike_rule(
            program, name="Tier Upgrade Strike", required_orders=3, window_days=30, bonus_points=25
        )
        # Consecutive rule: N=2, bonus=100
        crule = make_consecutive_rule(
            program, strikes=[strike], name="Tier Upgrade Consec",
            required_consecutive=2, combine_mode="all", bonus_points=100
        )

        # Seed 1 backdated achievement (so run becomes 2 after delivery)
        seed_consecutive_run(sample_user.id, strike, count=1)

        # Give user 950 points manually via award_points (so later crossing 1000 triggers Silver)
        svc = LoyaltyService()
        # Pre-populate the account with 950 points
        svc.award_points(
            sample_user.id, 950, "Pre-seeded points", LoyaltyActionType.PURCHASE, commit=True
        )

        # Verify starting at Bronze
        acct = _account(sample_user.id)
        assert acct.current_balance == 950

        # total = 12_500 → purchase = floor(12500/250) = 50 pts
        # + strike bonus = 25 pts
        # + consecutive bonus = 100 pts
        # → total added = 175 → new balance = 1125 > 1000 → Silver
        now = datetime.now(timezone.utc)
        # Seed 2 old delivered orders so this delivery is the 3rd qualifying
        for days_ago in [25, 15]:
            old_order = Order(
                user_id=sample_user.id,
                order_number=f"TIER-OLD-{days_ago}",
                status=OrderStatus.DELIVERED,
                subtotal=Decimal("10000"),
                total_amount=Decimal("10000"),
                payment_method=PaymentMethod.CLICK,
            )
            _db.session.add(old_order)
            _db.session.flush()
            old_order.created_at = now - timedelta(days=days_ago)
            _db.session.commit()

        deliver_paid_order(order_service, sample_user.id, total=12_500, payment="prepaid")

        # Verify tier upgraded to Silver
        _db.session.refresh(acct)
        assert acct.current_tier == "Silver", (
            f"Expected tier=Silver after crossing 1000, got {acct.current_tier}"
        )

        # Verify all 3 award types present. NOTE: the 950-point pre-seed above is
        # also a PURCHASE row created at ~now (so a time-window filter alone would
        # catch it). The delivery's purchase row is the only PURCHASE row tied to
        # an order (order_id set); the manual pre-seed has order_id=None — filter
        # on that to isolate the row produced by this delivery.
        all_txns = LoyaltyTransaction.query.filter_by(user_id=sample_user.id).all()
        purchase_rows = [
            t for t in all_txns
            if (t.extra_data or {}).get("action_type") == LoyaltyActionType.PURCHASE.value
            and t.order_id is not None
        ]
        consec_rows = consecutive_awards(sample_user.id, crule.id)

        assert len(purchase_rows) == 1, "Expected 1 purchase row from this delivery"
        assert len(consec_rows) == 1, "Expected 1 consecutive BONUS row"
        assert purchase_rows[0].points == 50  # floor(12500/250)

        # Balance = 950 + 50 + 25 + 100 = 1125
        assert _balance(sample_user.id) == 1125


# ===========================================================================
# CASE order_flow_ledger-010
# Skipped period resets run — boundary verification with proper gaps
# ===========================================================================


class TestCase010SkippedPeriodBoundary:
    def test_run_breaks_at_2w_gap_between_adjacent_achievements(
        self, app, db, sample_user, order_service
    ):
        """Spec §5.1: gap < 2*W is consecutive. Test that old achievements with
        gap < 2W are still counted; verify the consecutive run walks correctly.

        Setup: 3 achievements at [3W+1, 2W+1, W+1] days ago (all < 2W from neighbor).
        Delivery → 4th achievement at ~now. Run = 4; 4//3=1 → BONUS fired.
        """
        W = 30
        program = get_or_create_default_program()
        program.uzs_per_point = 250
        _db.session.commit()

        strike = make_strike_rule(
            program, name="Boundary 010", required_orders=3, window_days=W, bonus_points=50
        )
        crule = make_consecutive_rule(
            program, strikes=[strike], name="Boundary Rule 010",
            required_consecutive=3, combine_mode="all", bonus_points=300
        )

        now = datetime.now(timezone.utc)
        # Seed 3 achievements with gaps = W days each (< 2W → consecutive)
        for k, days_ago in enumerate([3*W+1, 2*W+1, W+1]):
            seed_strike_achievement(sample_user.id, strike, when=now - timedelta(days=days_ago))

        # Seed 2 old delivered orders in window (so this delivery = 3rd in W=30 days)
        for days_ago in [25, 15]:
            old_order = Order(
                user_id=sample_user.id,
                order_number=f"BD-{days_ago}-010",
                status=OrderStatus.DELIVERED,
                subtotal=Decimal("10000"),
                total_amount=Decimal("10000"),
                payment_method=PaymentMethod.CLICK,
            )
            _db.session.add(old_order)
            _db.session.flush()
            old_order.created_at = now - timedelta(days=days_ago)
            _db.session.commit()

        deliver_paid_order(order_service, sample_user.id, total=30_000, payment="prepaid")

        # Run = 4 (3 seeded + 1 new; all gaps < 2W=60d); 4//3=1 milestone
        consec_rows = consecutive_awards(sample_user.id, crule.id)
        assert len(consec_rows) == 1, (
            f"Expected 1 consecutive BONUS (run=4, 4//3=1), got {len(consec_rows)}"
        )
        assert consec_rows[0].points == 300


# ===========================================================================
# CASE order_flow_ledger-011
# Boundary: gap exactly = 2*window_days BREAKS the run
# ===========================================================================


class TestCase011GapExactly2WBreaksRun:
    def test_gap_exactly_2w_breaks_consecutive_run(
        self, app, db, sample_user, order_service
    ):
        """Spec §5.1: gap < 2*W (strict); gap == 2W fails → run reset to 1.
        Setup: 1 prior achievement at exactly 60 days ago (gap from now = 60d).
        Delivery adds new achievement at ~now. Gap(now, 60d_ago) = 60d = 2*30.
        60d < 60d is False → break → run = 1 < N=2 → no BONUS.
        """
        W = 30
        program = get_or_create_default_program()
        program.uzs_per_point = 250
        _db.session.commit()

        strike = make_strike_rule(
            program, name="Boundary 011", required_orders=3, window_days=W, bonus_points=50
        )
        crule = make_consecutive_rule(
            program, strikes=[strike], name="Gap 2W Rule",
            required_consecutive=2, combine_mode="all", bonus_points=300
        )

        now = datetime.now(timezone.utc)
        # Exactly 60 days ago = 2 * W = 60d (not strictly less than 2W)
        seed_strike_achievement(sample_user.id, strike, when=now - timedelta(days=2*W))

        # Seed 2 old delivered orders so this delivery qualifies for the strike
        for days_ago in [25, 15]:
            old_order = Order(
                user_id=sample_user.id,
                order_number=f"BD-{days_ago}-011",
                status=OrderStatus.DELIVERED,
                subtotal=Decimal("10000"),
                total_amount=Decimal("10000"),
                payment_method=PaymentMethod.CLICK,
            )
            _db.session.add(old_order)
            _db.session.flush()
            old_order.created_at = now - timedelta(days=days_ago)
            _db.session.commit()

        deliver_paid_order(order_service, sample_user.id, total=30_000, payment="prepaid")

        # New streak achievement created (3rd qualifying order in window)
        total_streak = strike_achievement_count(sample_user.id, strike.id)
        assert total_streak == 2, f"Expected 2 streak rows (1 seeded + 1 new), got {total_streak}"

        # The run breaks: gap = 60d = 2W, NOT < 2W → run = 1 only
        # combined = 1 < N=2 → no BONUS
        consec_rows = consecutive_awards(sample_user.id, crule.id)
        assert len(consec_rows) == 0, (
            f"Expected 0 consecutive BONUS rows (gap=2W breaks run), got {len(consec_rows)}"
        )

        # Only purchase + streak (no consecutive)
        purchase_rows = _purchase_txns(sample_user.id)
        assert len(purchase_rows) == 1


# ===========================================================================
# CASE order_flow_ledger-012
# Boundary: gap exactly = 2*window_days - 1 second KEEPS the run
# ===========================================================================


class TestCase012GapJustUnder2WKeepsRun:
    def test_gap_just_under_2w_keeps_consecutive_run(
        self, app, db, sample_user, order_service
    ):
        """Spec §5.1: gap strictly < 2W keeps the run.
        Gap = 59 days 23h 59m 59s < 60d → consecutive run=2 >= N=2 → BONUS fired.
        """
        W = 30
        program = get_or_create_default_program()
        program.uzs_per_point = 250
        _db.session.commit()

        strike = make_strike_rule(
            program, name="Boundary 012", required_orders=3, window_days=W, bonus_points=50
        )
        crule = make_consecutive_rule(
            program, strikes=[strike], name="Gap Just Under 2W",
            required_consecutive=2, combine_mode="all", bonus_points=300
        )

        now = datetime.now(timezone.utc)
        # Exactly 59d 23h 59m 59s ago — strictly less than 2*30=60d
        prior_when = now - (timedelta(days=2*W) - timedelta(seconds=1))
        seed_strike_achievement(sample_user.id, strike, when=prior_when)

        # Seed 2 old delivered orders so this delivery qualifies for the strike
        for days_ago in [25, 15]:
            old_order = Order(
                user_id=sample_user.id,
                order_number=f"BD-{days_ago}-012",
                status=OrderStatus.DELIVERED,
                subtotal=Decimal("10000"),
                total_amount=Decimal("10000"),
                payment_method=PaymentMethod.CLICK,
            )
            _db.session.add(old_order)
            _db.session.flush()
            old_order.created_at = now - timedelta(days=days_ago)
            _db.session.commit()

        deliver_paid_order(order_service, sample_user.id, total=30_000, payment="prepaid")

        # Run = 2 (1 seeded at just-under-60d + 1 new at ~now; gap < 60d → consecutive)
        consec_rows = consecutive_awards(sample_user.id, crule.id)
        assert len(consec_rows) == 1, (
            f"Expected 1 consecutive BONUS (gap<2W, run=2>=N=2), got {len(consec_rows)}"
        )
        assert consec_rows[0].points == 300

        purchase_rows = _purchase_txns(sample_user.id)
        assert len(purchase_rows) == 1

        streak_total = strike_achievement_count(sample_user.id, strike.id)
        assert streak_total == 2  # 1 seeded + 1 new


# ===========================================================================
# CASE order_flow_ledger-013
# Inactive consecutive rule — bonus not awarded
# ===========================================================================


class TestCase013InactiveRule:
    def test_inactive_rule_does_not_award(self, app, db, sample_user, order_service):
        """Spec §5: 'is_active=False' → rule skipped by update_consecutive_strikes."""
        program = get_or_create_default_program()
        program.uzs_per_point = 250
        _db.session.commit()

        strike = make_strike_rule(
            program, name="3in30 inactive", required_orders=3, window_days=30, bonus_points=100
        )
        # is_active=False → rule is skipped
        crule = make_consecutive_rule(
            program, strikes=[strike], name="Inactive Rule",
            required_consecutive=2, combine_mode="all", bonus_points=500,
            is_active=False
        )

        seed_consecutive_run(sample_user.id, strike, count=1)

        # Seed 2 old delivered orders
        now = datetime.now(timezone.utc)
        for days_ago in [25, 15]:
            old_order = Order(
                user_id=sample_user.id,
                order_number=f"INACT-{days_ago}",
                status=OrderStatus.DELIVERED,
                subtotal=Decimal("10000"),
                total_amount=Decimal("10000"),
                payment_method=PaymentMethod.CLICK,
            )
            _db.session.add(old_order)
            _db.session.flush()
            old_order.created_at = now - timedelta(days=days_ago)
            _db.session.commit()

        deliver_paid_order(order_service, sample_user.id, total=30_000, payment="prepaid")

        # Streak EARNED row created (update_streak runs active strike rules)
        assert strike_achievement_count(sample_user.id, strike.id) >= 1

        # But NO consecutive BONUS (rule is inactive)
        consec_rows = consecutive_awards(sample_user.id, crule.id)
        assert len(consec_rows) == 0, (
            f"Expected 0 consecutive BONUS rows (rule inactive), got {len(consec_rows)}"
        )

        purchase_rows = _purchase_txns(sample_user.id)
        assert len(purchase_rows) == 1


# ===========================================================================
# CASE order_flow_ledger-014
# Expired consecutive rule (ends_at in the past) — bonus not awarded
# ===========================================================================


class TestCase014ExpiredRule:
    def test_expired_rule_does_not_award(self, app, db, sample_user, order_service):
        """Spec §5 and loyalty_service.py:1811: is_effective(now) returns False when ends_at < now."""
        program = get_or_create_default_program()
        program.uzs_per_point = 250
        _db.session.commit()

        strike = make_strike_rule(
            program, name="3in30 expired", required_orders=3, window_days=30, bonus_points=100
        )
        now = datetime.now(timezone.utc)
        # ends_at = 1 hour ago → is_effective(now) = False
        crule = make_consecutive_rule(
            program, strikes=[strike], name="Expired Rule",
            required_consecutive=2, combine_mode="all", bonus_points=500,
            is_active=True, ends_at=now - timedelta(hours=1)
        )

        seed_consecutive_run(sample_user.id, strike, count=1)

        # Seed 2 old delivered orders
        for days_ago in [25, 15]:
            old_order = Order(
                user_id=sample_user.id,
                order_number=f"EXP-{days_ago}",
                status=OrderStatus.DELIVERED,
                subtotal=Decimal("10000"),
                total_amount=Decimal("10000"),
                payment_method=PaymentMethod.CLICK,
            )
            _db.session.add(old_order)
            _db.session.flush()
            old_order.created_at = now - timedelta(days=days_ago)
            _db.session.commit()

        deliver_paid_order(order_service, sample_user.id, total=30_000, payment="prepaid")

        # Streak EARNED row created
        assert strike_achievement_count(sample_user.id, strike.id) >= 1

        # NO consecutive BONUS (rule expired)
        consec_rows = consecutive_awards(sample_user.id, crule.id)
        assert len(consec_rows) == 0, (
            f"Expected 0 consecutive BONUS rows (rule expired), got {len(consec_rows)}"
        )

        purchase_rows = _purchase_txns(sample_user.id)
        assert len(purchase_rows) == 1


# ===========================================================================
# CASE order_flow_ledger-015
# Ineligible entity user — observe actual behavior and flag for product review
# ===========================================================================


class TestCase015IneligibleEntityUser:
    def test_entity_ineligible_user_earns_nothing(
        self, app, db, order_service
    ):
        """Gated behavior (product-owner decision 2026-06-24): ineligible entity users
        (entity with no active is_loyalty_points_eligible contract) earn NOTHING —
        update_streak and update_consecutive_strikes both early-return for ineligible
        entities; maybe_award_purchase_points is also gated via is_user_loyalty_eligible.
        The order must still reach DELIVERED status.
        """
        program = get_or_create_default_program()
        program.uzs_per_point = 250
        _db.session.commit()

        strike = make_strike_rule(
            program, name="Entity 015", required_orders=3, window_days=30, bonus_points=100
        )
        crule = make_consecutive_rule(
            program, strikes=[strike], name="Entity Consec 015",
            required_consecutive=2, combine_mode="all", bonus_points=500
        )

        # Build entity user with NO loyalty-eligible contract
        entity_user = build_entity_user(loyalty_eligible=False)

        # Verify eligibility returns False — pass the USER OBJECT (not id), it is a @staticmethod
        assert LoyaltyService.is_user_loyalty_eligible(entity_user) is False

        # No seeding of prior achievements — we want the gating to suppress all awards

        now = datetime.now(timezone.utc)
        # Seed delivered orders to satisfy the strike threshold (would qualify if user were eligible)
        for days_ago in [25, 15]:
            old_order = Order(
                user_id=entity_user.id,
                order_number=f"ENT-{days_ago}-015",
                status=OrderStatus.DELIVERED,
                subtotal=Decimal("10000"),
                total_amount=Decimal("10000"),
                payment_method=PaymentMethod.CLICK,
            )
            _db.session.add(old_order)
            _db.session.flush()
            old_order.created_at = now - timedelta(days=days_ago)
            _db.session.commit()

        # Deliver — fire the real path
        order = deliver_paid_order(
            order_service, entity_user.id, total=75_000, payment="prepaid"
        )

        # Delivery must have completed (status=DELIVERED)
        _db.session.refresh(order)
        assert order.status == OrderStatus.DELIVERED, (
            "Order must be DELIVERED even for ineligible entity user"
        )

        # Ineligible entity earns ZERO purchase, streak, or consecutive rows.
        # update_streak and update_consecutive_strikes both early-return for ineligible users;
        # maybe_award_purchase_points is also gated.
        all_txns = LoyaltyTransaction.query.filter_by(user_id=entity_user.id).all()
        purchase_rows = [
            t for t in all_txns
            if (t.extra_data or {}).get("action_type") == LoyaltyActionType.PURCHASE.value
        ]
        streak_rows = [
            t for t in all_txns
            if (t.extra_data or {}).get("action_type") == LoyaltyActionType.STREAK_BONUS.value
        ]
        consec_rows = consecutive_awards(entity_user.id, crule.id)

        assert len(purchase_rows) == 0, (
            f"Ineligible entity must earn 0 purchase rows, got {len(purchase_rows)}"
        )
        assert len(streak_rows) == 0, (
            f"Ineligible entity must earn 0 streak rows from delivery, got {len(streak_rows)}"
        )
        assert len(consec_rows) == 0, (
            f"Ineligible entity must earn 0 consecutive BONUS rows, got {len(consec_rows)}"
        )


# ===========================================================================
# CASE order_flow_ledger-016
# No active loyalty program — all loyalty paths are no-ops, delivery unblocked
# ===========================================================================


class TestCase016NoActiveProgram:
    def test_no_active_program_delivery_still_completes(
        self, app, db, sample_user, order_service
    ):
        """Spec: both update_streak and update_consecutive_strikes short-circuit on
        'if not program: return'. Delivery must still complete without exception.
        """
        import uuid
        # Do NOT call get_or_create_default_program — leave DB with no program

        total = Decimal("50000")
        order = Order(
            user_id=sample_user.id,
            order_number=f"NOPROG-{uuid.uuid4().hex[:8].upper()}",
            status=OrderStatus.PENDING,
            subtotal=total,
            total_amount=total,
            payment_method=PaymentMethod.CLICK,
        )
        _db.session.add(order)
        _db.session.flush()
        _db.session.add(
            OrderItem(
                order_id=order.id,
                product_id=1,
                quantity=1,
                unit_price=total,
                total_price=total,
            )
        )
        _db.session.commit()

        order.is_paid = True
        order.paid_at = datetime.now(timezone.utc)
        order.status = OrderStatus.DELIVERED
        order.delivered_at = datetime.now(timezone.utc)
        _db.session.commit()

        # Must not raise
        order_service._handle_status_change_actions(order, OrderStatus.DELIVERED, commit=True)

        _db.session.refresh(order)
        assert order.status == OrderStatus.DELIVERED, "Order must be DELIVERED"

        # Scope: with no active program, streak and consecutive-strike features must
        # produce NO rows (their first guard is "no program → return").
        # Purchase accrual is program-independent (out of consecutive-strike scope),
        # so we do NOT assert on PURCHASE rows here.
        all_txns = LoyaltyTransaction.query.filter_by(user_id=sample_user.id).all()
        streak_txns = [
            t for t in all_txns
            if (t.extra_data or {}).get("action_type") == LoyaltyActionType.STREAK_BONUS.value
        ]
        consec_txns = [
            t for t in all_txns
            if (t.extra_data or {}).get("action_type") == LoyaltyActionType.CONSECUTIVE_STREAK_BONUS.value
        ]
        assert len(streak_txns) == 0, (
            f"Expected 0 STREAK_BONUS rows with no program, got {len(streak_txns)}"
        )
        assert len(consec_txns) == 0, (
            f"Expected 0 CONSECUTIVE_STREAK_BONUS rows with no program, got {len(consec_txns)}"
        )


# ===========================================================================
# CASE order_flow_ledger-017
# 'any' mode: only one of two strikes reaches N — bonus awarded
# ===========================================================================


class TestCase017AnyModeOneStrikeSuffices:
    def test_any_mode_bonus_fires_when_one_strike_reaches_n(
        self, app, db, sample_user, order_service
    ):
        """Spec §5.2: combine_mode='any' → combined=max(per-strike counts);
        one strike reaching N is sufficient even when the other is at 0.
        """
        program = get_or_create_default_program()
        program.uzs_per_point = 250
        _db.session.commit()

        # Strike A: window=30, required=3 → this delivery will qualify
        strike_a = make_strike_rule(
            program, name="Any A", required_orders=3, window_days=30, bonus_points=80
        )
        # Strike B: window=40, required=10 → very high, this delivery won't qualify
        strike_b = make_strike_rule(
            program, name="Any B lag", required_orders=10, window_days=40, bonus_points=60
        )
        crule = make_consecutive_rule(
            program, strikes=[strike_a, strike_b], name="Any Mode Rule",
            required_consecutive=3, combine_mode="any", bonus_points=600
        )

        # Seed 2 consecutive achievements for A; 0 for B
        seed_consecutive_run(sample_user.id, strike_a, count=2)

        now = datetime.now(timezone.utc)
        # Seed 2 old delivered orders so this delivery = 3rd for A
        for days_ago in [25, 15]:
            old_order = Order(
                user_id=sample_user.id,
                order_number=f"ANY-{days_ago}-017",
                status=OrderStatus.DELIVERED,
                subtotal=Decimal("10000"),
                total_amount=Decimal("10000"),
                payment_method=PaymentMethod.CLICK,
            )
            _db.session.add(old_order)
            _db.session.flush()
            old_order.created_at = now - timedelta(days=days_ago)
            _db.session.commit()

        deliver_paid_order(order_service, sample_user.id, total=30_000, payment="prepaid")

        # A should have 3 streak rows (2 seeded + 1 new)
        a_count = strike_achievement_count(sample_user.id, strike_a.id)
        assert a_count == 3, f"Strike A should have 3 rows, got {a_count}"

        # B should have 0 streak rows (threshold not met)
        b_count = strike_achievement_count(sample_user.id, strike_b.id)
        assert b_count == 0, f"Strike B should have 0 rows, got {b_count}"

        # combine_mode='any' → combined = max(3, 0) = 3 >= N=3 → BONUS awarded
        consec_rows = consecutive_awards(sample_user.id, crule.id)
        assert len(consec_rows) == 1, (
            f"Expected 1 consecutive BONUS (any-mode, A=3>=N=3), got {len(consec_rows)}"
        )
        assert consec_rows[0].points == 600
        assert (consec_rows[0].extra_data or {}).get("milestone") == 1
        assert (consec_rows[0].extra_data or {}).get("consecutive_strike_rule_id") == crule.id

        purchase_rows = _purchase_txns(sample_user.id)
        assert len(purchase_rows) == 1

        # Balance = purchase_pts + a_strike_pts + consec_pts
        expected = int(Decimal("30000")) // 250 + strike_a.bonus_points + 600
        assert _balance(sample_user.id) == expected


# ===========================================================================
# CASE order_flow_ledger-018
# Prepaid payment-after-delivery does NOT re-trigger update_streak or consecutive bonus
# ===========================================================================


class TestCase018PrepaidPaymentAfterDeliveryNoDoubleConsec:
    def test_payment_after_delivery_does_not_re_trigger_update_streak(
        self, app, db, sample_user, order_service
    ):
        """order_service.py: update_streak called ONLY at DELIVERED; _handle_successful_payment
        calls only maybe_award_purchase_points. Consecutive bonus must not be duplicated
        via the payment edge.
        """
        import uuid

        program = get_or_create_default_program()
        program.uzs_per_point = 250
        _db.session.commit()

        strike = make_strike_rule(
            program, name="3in30 payafter", required_orders=3, window_days=30, bonus_points=100
        )
        crule = make_consecutive_rule(
            program, strikes=[strike], name="Pay After Delivery Consec",
            required_consecutive=2, combine_mode="all", bonus_points=400
        )

        # Seed 1 backdated achievement → run will become 2 on delivery
        seed_consecutive_run(sample_user.id, strike, count=1)

        # Seed 2 old delivered orders so this delivery = 3rd qualifying order
        now = datetime.now(timezone.utc)
        for days_ago in [25, 15]:
            old_order = Order(
                user_id=sample_user.id,
                order_number=f"PAY-{days_ago}-018",
                status=OrderStatus.DELIVERED,
                subtotal=Decimal("10000"),
                total_amount=Decimal("10000"),
                payment_method=PaymentMethod.CLICK,
            )
            _db.session.add(old_order)
            _db.session.flush()
            old_order.created_at = now - timedelta(days=days_ago)
            _db.session.commit()

        # Build order that is NOT yet paid at delivery time
        total = Decimal("50000")
        order = Order(
            user_id=sample_user.id,
            order_number=f"PAY-AFTER-{uuid.uuid4().hex[:8].upper()}",
            status=OrderStatus.PENDING,
            subtotal=total,
            total_amount=total,
            payment_method=PaymentMethod.CLICK,
        )
        _db.session.add(order)
        _db.session.flush()
        _db.session.add(
            OrderItem(
                order_id=order.id,
                product_id=1,
                quantity=1,
                unit_price=total,
                total_price=total,
            )
        )
        _db.session.commit()

        # Deliver WITHOUT marking paid
        order.status = OrderStatus.DELIVERED
        order.delivered_at = now
        _db.session.commit()
        order_service._handle_status_change_actions(order, OrderStatus.DELIVERED, commit=True)

        # After delivery: streak + consecutive awarded; NO purchase (not paid)
        streak_after = strike_achievement_count(sample_user.id, strike.id)
        consec_after = consecutive_awards(sample_user.id, crule.id)
        purchase_after = _purchase_txns(sample_user.id)

        assert streak_after >= 1, "Streak should be awarded at delivery even when unpaid"
        assert len(consec_after) == 1, "Consecutive BONUS awarded at delivery even when unpaid"
        assert len(purchase_after) == 0, "No purchase row before payment completes"

        balance_after_delivery = _balance(sample_user.id)

        # Now simulate the payment completing AFTER delivery
        payment = Payment(
            order_id=order.id,
            user_id=sample_user.id,
            payment_method=PaymentMethod.CLICK,
            amount=total,
            currency="UZS",
            status=PaymentStatus.COMPLETED,
            amount_collected=total,
            outstanding_amount=Decimal("0.00"),
            paid_at=datetime.now(timezone.utc),
        )
        _db.session.add(payment)
        _db.session.commit()

        PaymentService()._handle_successful_payment(payment, trigger_notifications=False)

        # After payment: purchase row added, but NO new streak or consecutive rows
        _db.session.expire_all()
        streak_final = strike_achievement_count(sample_user.id, strike.id)
        consec_final = consecutive_awards(sample_user.id, crule.id)
        purchase_final = _purchase_txns(sample_user.id)

        # No new streak rows from payment edge
        assert streak_final == streak_after, (
            "update_streak must NOT be called again by the payment edge"
        )
        # No new consecutive rows from payment edge
        assert len(consec_final) == 1, (
            "Consecutive BONUS must not be duplicated via the payment edge"
        )
        assert len(consec_final) == len(consec_after), (
            "Consecutive rows must be unchanged after payment"
        )
        # Purchase row NOW exists
        assert len(purchase_final) == 1, "Purchase row must appear after payment completes"

        # Balance = balance_after_delivery + purchase_pts
        purchase_pts = int(total) // 250
        assert _balance(sample_user.id) == balance_after_delivery + purchase_pts
