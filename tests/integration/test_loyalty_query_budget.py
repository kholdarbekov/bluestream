"""Query budget for the loyalty tier resolution on the checkout hot path.

`effective_tier` runs on every cart estimate and every points award. These
tests pin how many SQL statements that costs, so a future change cannot
quietly reintroduce a per-call fan-out.

Note: the "0 additional queries" property for a second ladder read only holds
before a `db.session.commit()` in the same request — a commit expires the
memoized `LoyaltyTierConfig` instances, so a later read costs one refresh
SELECT per tier object instead of one ladder query.
"""

from decimal import Decimal

import pytest
from flask import g
from sqlalchemy import event

from business_app import db as _db
from business_app.models.loyalty import LoyaltyPoints
from business_app.models.user import User
from business_app.services.cross_platform_sync_service import cross_platform_sync_service
from business_app.services.loyalty_service import (
    LoyaltyService,
    effective_tier,
    _calculate_qualifying_points_cached,
    _LOYALTY_ACCOUNT_MEMO_KEY,
    _QUALIFYING_POINTS_MEMO_KEY,
)
from business_app.utils.password_security import hash_password
from shared.enums import PaymentMethod, UserRole, UserType
from tests.integration.tier_discount_factory import seed_account, seed_program, seed_tier


class _Counter:
    def __init__(self):
        self.statements = []

    def __enter__(self):
        self._fn = lambda conn, cur, stmt, params, ctx, many: self.statements.append(stmt)
        event.listen(_db.engine, "before_cursor_execute", self._fn)
        return self

    def __exit__(self, *exc):
        event.remove(_db.engine, "before_cursor_execute", self._fn)
        return False

    def __len__(self):
        return len(self.statements)


@pytest.fixture
def ladder(db, sample_user):
    program = seed_program(db)
    seed_tier(db, program, name="Bronze", rate=Decimal("0"), min_points=0, display_order=0)
    seed_tier(db, program, name="Silver", rate=Decimal("1.5"), min_points=4000, display_order=1)
    seed_tier(db, program, name="Gold", rate=Decimal("2"), min_points=15000, display_order=2)
    seed_account(db, sample_user, program, qualifying_points=3488, balance=988)
    account = LoyaltyPoints.query.filter_by(user_id=sample_user.id).first()
    account.current_tier = "Silver"
    db.session.commit()
    return account


def test_effective_tier_single_call_query_budget(db, ladder, capsys):
    _db.session.expire_all()
    with _Counter() as counted:
        effective_tier(ladder)
    with capsys.disabled():
        print(f"\n[budget] effective_tier single call: {len(counted)} statements")
        for s in counted.statements:
            print("   ", " ".join(s.split())[:110])
    # 1: the `ladder` account row, reloaded because this test's own expire_all()
    #    left it expired (not part of effective_tier's real cost — a fresh,
    #    unexpired account costs nothing here).
    # 2: one tier-ladder query (replaces the old separate stored+live lookups).
    # 3: the qualifying-points SUM (cold — nothing memoized yet this request).
    assert len(counted) <= 3


def test_cart_estimate_path_query_budget(db, ladder, sample_user, capsys):
    """What one cart estimate costs: the COD quote plus the points-earned estimate."""
    service = LoyaltyService()
    _db.session.expire_all()
    with _Counter() as counted:
        service.quote_tier_discount(sample_user, Decimal("54000"), PaymentMethod.CASH)
        service.calculate_points_for_purchase(sample_user.id, 54000)
    with capsys.disabled():
        print(f"\n[budget] cart-estimate loyalty path: {len(counted)} statements")
        for s in counted.statements:
            print("   ", " ".join(s.split())[:110])
    # 1: users row, reloaded because this test's expire_all() left `sample_user` expired.
    # 2: the loyalty_points account row — fetched ONCE and memoized (quote_tier_discount's
    #    lookup and calculate_points_for_purchase's get_or_create_loyalty_account share it).
    # 3: one tier-ladder query, memoized — reused by both effective_tier calls AND
    #    _get_tier_multiplier.
    # 4: the qualifying-points SUM, memoized — reused by both effective_tier calls.
    # 5: account.program, lazy-loaded once inside calculate_points_for_purchase.
    assert len(counted) <= 5


def test_effective_tier_second_call_is_free(db, ladder, capsys):
    """Both the ladder and the qualifying-points SUM are memoized per request:
    a second effective_tier call for the same account costs nothing more."""
    _db.session.expire_all()
    effective_tier(ladder)  # cold call: warms both memos
    with _Counter() as counted:
        effective_tier(ladder)
    with capsys.disabled():
        print(f"\n[budget] effective_tier second call: {len(counted)} statements")
    assert len(counted) == 0


def test_award_points_invalidates_qualifying_points_memo(db, sample_user, monkeypatch):
    """Two independent invalidation paths, each proven so the other cannot
    rescue it:

    1. award_points' explicit ``_invalidate_qualifying_points_memo`` call
       must clear the memo BEFORE any flush runs. Proven from a
       ``before_flush`` session hook, which fires strictly before the
       LoyaltyTransaction ``after_insert`` listener — a check made only
       AFTER award_points returns can't tell the two apart, since
       award_points always flushes (or commits) on its way out regardless of
       ``commit=``, so the listener will always have fired by then too.
    2. The after_insert/after_update/after_delete listener alone must
       invalidate the memo for a write that never calls the explicit
       invalidator — proven via ``deduct_points``, which does not call it.
    """
    service = LoyaltyService()
    program = seed_program(db)
    seed_tier(db, program, name="Bronze", rate=Decimal("0"), min_points=0, display_order=0)
    seed_tier(db, program, name="Silver", rate=Decimal("1.5"), min_points=4000, display_order=1)

    account = service.get_or_create_loyalty_account(sample_user.id)
    account.current_tier = "Bronze"
    db.session.commit()

    # Badge pinned to Bronze throughout: isolates the "live" (qualifying-points)
    # half of effective_tier from the badge-upgrade machinery in _check_tier_upgrade.
    monkeypatch.setattr(service, "_check_tier_upgrade", lambda _account: None)

    assert effective_tier(account).name == "Bronze"  # warms the qualifying-points memo at 0

    # --- 1. explicit invalidation must beat the flush, not just the listener ---
    captured = {}

    def _capture_pre_flush(session, flush_context, instances):
        captured.setdefault("cleared_before_flush", sample_user.id not in g._loyalty_qualifying_points_memo)

    event.listen(_db.session, "before_flush", _capture_pre_flush)
    try:
        service.award_points(sample_user.id, 5000, "test award")
    finally:
        event.remove(_db.session, "before_flush", _capture_pre_flush)

    assert captured.get("cleared_before_flush") is True, (
        "the memo must already be clear by the time the first flush runs — "
        "only the explicit award_points invalidation can do that this early"
    )
    assert effective_tier(account).name == "Silver"

    # --- 2. the write listener alone, via a path that never explicitly invalidates ---
    assert sample_user.id in g._loyalty_qualifying_points_memo  # re-warmed by the effective_tier call above
    service.deduct_points(sample_user.id, 100, "test deduction", commit=True)
    assert sample_user.id not in g._loyalty_qualifying_points_memo, (
        "deduct_points never calls the explicit invalidator — only the "
        "LoyaltyTransaction write listener can be responsible for clearing this"
    )


def test_session_rollback_clears_request_memos(db, ladder, sample_user):
    """FIX 1: `_clear_request_memos_on_rollback`, an `after_rollback` listener on
    `db.session`, must drop both the qualifying-points memo and the loyalty-account
    memo. Without it, a value cached after a flush but before a rollback would stay
    cached at the uncommitted total and could feed a later read on a pricing path
    in the same request, even though the database no longer has that state.
    """
    service = LoyaltyService()
    _db.session.expire_all()

    # One call that warms both memos (mirrors the cart-estimate path above).
    service.quote_tier_discount(sample_user, Decimal("54000"), PaymentMethod.CASH)

    qp_memo = g.get(_QUALIFYING_POINTS_MEMO_KEY)
    account_memo = g.get(_LOYALTY_ACCOUNT_MEMO_KEY)
    assert qp_memo, "qualifying-points memo should be warmed by quote_tier_discount"
    assert account_memo, "loyalty-account memo should be warmed by quote_tier_discount"

    _db.session.rollback()

    assert g.get(_QUALIFYING_POINTS_MEMO_KEY) == {}, "qualifying-points memo must be cleared after rollback"
    assert g.get(_LOYALTY_ACCOUNT_MEMO_KEY) == {}, "loyalty-account memo must be cleared after rollback"


def test_cross_platform_merge_invalidates_both_users_qualifying_points_memo(db, sample_user):
    """FIX 2: `_merge_loyalty_membership` reassigns `LoyaltyTransaction.user_id` via a
    Core bulk `query.update(...)`, which fires no ORM event, so it must explicitly
    invalidate the qualifying-points memo for BOTH the primary and secondary user.

    Calls the real `cross_platform_sync_service._merge_loyalty_membership` (the same
    method the auth-link merge flow calls, per tests/unit/test_cross_platform_merge_loyalty.py)
    rather than reimplementing its bulk update here. This test covers ONLY the memo
    side effect of that one method — it does not exercise the rest of the merge
    (user deletion, cart/order/etc. reassignment, `auto_link_accounts`); see
    tests/unit/test_cross_platform_merge_loyalty.py for that broader coverage.
    """
    secondary = User(
        email="secondary-merge@example.com",
        phone="+998901234598",
        password_hash=hash_password("TestPassword123!"),
        first_name="Secondary",
        last_name="User",
        user_type=UserType.INDIVIDUAL,
        role=UserRole.CUSTOMER,
        is_verified=True,
    )
    db.session.add(secondary)
    db.session.commit()

    program = seed_program(db)
    seed_account(db, sample_user, program, qualifying_points=300, balance=300)
    seed_account(db, secondary, program, qualifying_points=150, balance=150)

    primary_id, secondary_id = sample_user.id, secondary.id

    # Warm the qualifying-points memo for both users.
    _calculate_qualifying_points_cached(primary_id)
    _calculate_qualifying_points_cached(secondary_id)
    memo = g.get(_QUALIFYING_POINTS_MEMO_KEY)
    assert primary_id in memo and secondary_id in memo

    cross_platform_sync_service._merge_loyalty_membership(primary_id, secondary_id)
    db.session.commit()

    memo = g.get(_QUALIFYING_POINTS_MEMO_KEY)
    assert primary_id not in memo, "primary user's stale qualifying-points memo must be invalidated by the merge"
    assert secondary_id not in memo, "secondary user's stale qualifying-points memo must be invalidated by the merge"
