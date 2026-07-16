"""End-to-end, multi-step loyalty journeys for the BlueStream loyalty program.

These are realistic lifecycle tests that drive several LoyaltyService /
OrderService operations in sequence and assert the END STATE plus the
production reconciliation invariant:

    total_earned   == sum(positive EARNED+BONUS lot ``points``)
    current_balance== sum(live positive lot ``remaining_points``)
                   == total_earned - net_redeemed - expired
    total_redeemed == gross_redeemed - refunds

Order creation mirrors tests/integration/test_order_reward_redemption.py
exactly: a real LoyaltyService path with inventory reservation mocked and the
corporate-prepayment / payment-row steps patched out. Notifications are always
monkeypatched to no-ops (no Celery/notification side effects).

All amounts are characterization assertions of the CURRENT behavior of
business_app/services/loyalty_service.py.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from business_app import db as _db
from business_app.models.delivery import Delivery
from business_app.models.loyalty import (
    LoyaltyPoints,
    LoyaltyProgram,
    LoyaltyReward,
    LoyaltyTierConfig,
    LoyaltyTransaction,
    ReferralProgram,
    RewardRedemption,
)
from business_app.models.order import Order
from business_app.models.product import Product, ProductSizeEnum
from business_app.models.user import User, UserAddress
from business_app.services.loyalty_service import LoyaltyService
from business_app.services.order_service import OrderService
from business_app.tasks import loyalty_tasks
from business_app.utils.constants import LoyaltyActionType, LoyaltyTransactionType
from business_app.utils.exceptions import ConflictError, NotFoundError, ValidationError
from business_app.utils.password_security import hash_password
from shared.constants import DISPLAY_TIMEZONE
from shared.enums import DeliveryStatus, OrderStatus, UserRole, UserType


# --------------------------------------------------------------------------- #
# Fixtures (local — loyalty fixtures are NOT shared via conftest)
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _silence_loyalty_notifications(monkeypatch):
    """Every journey touches award/deduct/expire/tier paths — mute all of their
    notification side effects so no Celery enqueue or push is ever attempted."""
    monkeypatch.setattr(LoyaltyService, "_send_points_notification", lambda *a, **k: None)
    monkeypatch.setattr(LoyaltyService, "_send_tier_upgrade_notification", lambda *a, **k: None)
    monkeypatch.setattr(LoyaltyService, "_send_points_expiry_notification", lambda *a, **k: None)


@pytest.fixture
def loyalty_program(db):
    program = LoyaltyProgram(
        name="Default",
        is_active=True,
        is_default=True,
        uzs_per_point=250,
        points_expiry_days=365,
        signup_bonus=100,
        referral_bonus=50,
        birthday_bonus=25,
    )
    db.session.add(program)
    db.session.commit()
    return program


@pytest.fixture
def service():
    return LoyaltyService()


@pytest.fixture
def discount_reward(db, loyalty_program):
    reward = LoyaltyReward(
        program_id=loyalty_program.id,
        name="500 off",
        reward_type="discount",
        discount_type="fixed",
        discount_value=Decimal("500.00"),
        points_cost=100,
        is_active=True,
        max_uses_per_user=5,
        redemptions_used=0,
    )
    db.session.add(reward)
    db.session.commit()
    return reward


@pytest.fixture
def free_product(db, sample_category):
    p = Product(
        name="Free Bottle",
        base_price=Decimal("8000.00"),
        category_id=sample_category.id,
        size=ProductSizeEnum.SIZE_19L,
        is_active=True,
    )
    db.session.add(p)
    db.session.commit()
    return p


@pytest.fixture
def free_product_reward(db, loyalty_program, free_product):
    reward = LoyaltyReward(
        program_id=loyalty_program.id,
        name="2 free bottles",
        reward_type="free_product",
        points_cost=100,
        free_product_id=free_product.id,
        free_product_quantity=2,
        is_active=True,
        max_uses_per_user=5,
        redemptions_used=0,
    )
    db.session.add(reward)
    db.session.commit()
    return reward


@pytest.fixture
def delivery_address(db, sample_user):
    address = UserAddress(
        user_id=sample_user.id,
        title="Home",
        full_address="Home Street 1",
        street_address="Home Street 1",
        city="Tashkent",
        latitude=41.31,
        longitude=69.28,
        is_default=True,
    )
    db.session.add(address)
    db.session.commit()
    return address


@pytest.fixture
def tiers(db, loyalty_program):
    """Bronze (0) -> Silver (500) -> Gold (1000) tier ladder."""
    bronze = LoyaltyTierConfig(
        program_id=loyalty_program.id, name="Bronze", display_order=0,
        min_points=0, max_points=499, points_multiplier=1.0, is_active=True,
    )
    silver = LoyaltyTierConfig(
        program_id=loyalty_program.id, name="Silver", display_order=1,
        min_points=500, max_points=999, points_multiplier=1.0, is_active=True,
    )
    gold = LoyaltyTierConfig(
        program_id=loyalty_program.id, name="Gold", display_order=2,
        min_points=1000, max_points=None, points_multiplier=2.0, is_active=True,
    )
    db.session.add_all([bronze, silver, gold])
    db.session.commit()
    return {"bronze": bronze, "silver": silver, "gold": gold}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _order_data(product, address, **extra):
    data = {
        "items": [{"product_id": product.id, "quantity": 2}],
        "delivery_address": {
            "delivery_address_id": address.id,
            "street": address.street_address,
            "latitude": address.latitude,
            "longitude": address.longitude,
        },
        "payment_method": "click",
    }
    data.update(extra)
    return data


def _availability(product, quantity=2):
    return SimpleNamespace(
        product_id=product.id,
        requested_quantity=quantity,
        available_quantity=100,
        reserved_quantity=0,
        is_available=True,
        reason="Available",
    )


def _patched_create_order(service_obj, user_id, order_data):
    """Run OrderService.create_order with corporate + payment side effects patched
    out (the loyalty/reward path itself runs for real)."""
    with patch(
        "business_app.services.corporate_contract_service.CorporateContractService.reserve_for_order",
        return_value=None,
    ), patch(
        "business_app.services.payment_service.PaymentService.initialize_order_payment",
        return_value=None,
    ):
        return service_obj.create_order(user_id, order_data)


def _make_order_service(mock_inventory_service, availability):
    mock_inventory_service.check_multiple_products_availability.return_value = availability
    mock_inventory_service.reserve_inventory.return_value = {"success": True, "expires_at": None}
    mock_inventory_service.release_reservations.return_value = {"success": True}
    return OrderService(inventory_service=mock_inventory_service)


def _seed_earn_lot(user_id, points, *, created_at=None, expires_at=None,
                   txn_type=LoyaltyTransactionType.EARNED, description="seed"):
    """Insert a single positive earn lot (remaining == points)."""
    lot = LoyaltyTransaction(
        user_id=user_id,
        transaction_type=txn_type,
        points=points,
        remaining_points=points,
        description=description,
    )
    lot.expires_at = expires_at or datetime(2999, 1, 1, tzinfo=timezone.utc)
    if created_at is not None:
        lot.created_at = created_at
    _db.session.add(lot)
    _db.session.flush()
    return lot


def _account(user_id, program_id, balance):
    acc = LoyaltyPoints(
        user_id=user_id, program_id=program_id,
        total_earned=balance, current_balance=balance,
        total_redeemed=0, total_expired=0,
    )
    _db.session.add(acc)
    _db.session.flush()
    return acc


def _reconcile(service_obj, user_id):
    """Return the reconciliation snapshot used by the invariant assertions."""
    account = LoyaltyPoints.query.filter_by(user_id=user_id).first()
    txns = LoyaltyTransaction.query.filter_by(user_id=user_id).all()

    earned_lots_sum = sum(
        t.points
        for t in txns
        if t.points > 0
        and t.transaction_type in (LoyaltyTransactionType.EARNED, LoyaltyTransactionType.BONUS)
    )
    live_balance = service_obj.get_available_points(user_id)
    return {
        "account": account,
        "earned_lots_sum": earned_lots_sum,
        "live_balance": live_balance,
    }


# --------------------------------------------------------------------------- #
# J1 — Full lifecycle: welcome -> earn -> tier -> redeem -> cancel/refund
# --------------------------------------------------------------------------- #


@pytest.mark.integration
@pytest.mark.order
def test_j1_full_lifecycle_welcome_earn_tier_redeem_then_cancel_refund(
    app, db, sample_user, sample_product, mock_inventory_service,
    loyalty_program, tiers, discount_reward, delivery_address, service,
):
    # --- new account + welcome bonus (signup_bonus=100) ---
    granted = service.grant_welcome_bonus(sample_user.id)
    assert granted == 100
    assert service.get_available_points(sample_user.id) == 100
    # second call is idempotent
    assert service.grant_welcome_bonus(sample_user.id) == 0

    # --- 3 purchases worth of earnings (drive toward Gold: 100 + 1500 = 1600) ---
    service.award_points(sample_user.id, 400, "Purchase #1", action_type=LoyaltyActionType.PURCHASE)
    service.award_points(sample_user.id, 500, "Purchase #2", action_type=LoyaltyActionType.PURCHASE)
    service.award_points(sample_user.id, 600, "Purchase #3", action_type=LoyaltyActionType.PURCHASE)

    account = LoyaltyPoints.query.filter_by(user_id=sample_user.id).first()
    db.session.refresh(account)
    # qualifying points = 100 + 400 + 500 + 600 = 1600 -> Gold (>= 1000)
    assert service.calculate_qualifying_points(sample_user.id) == 1600
    assert account.current_tier == "Gold"
    assert account.tier_valid_until is not None
    assert service.get_available_points(sample_user.id) == 1600

    # --- apply a discount reward at order creation ---
    order_service = _make_order_service(mock_inventory_service, [_availability(sample_product)])
    order = _patched_create_order(
        order_service, sample_user.id,
        _order_data(sample_product, delivery_address, reward_id=discount_reward.id),
    )
    db.session.refresh(order)

    assert order.loyalty_discount == Decimal("500.00")
    assert order.total_amount == order.subtotal - Decimal("500.00") + order.delivery_fee
    assert RewardRedemption.query.filter_by(order_id=order.id, status="applied").count() == 1
    assert service.get_available_points(sample_user.id) == 1500  # 1600 - 100 cost
    db.session.refresh(discount_reward)
    assert discount_reward.redemptions_used == 1
    db.session.refresh(account)
    assert account.total_redeemed == 100

    # --- cancel the order -> points refunded, redemption cancelled, usage back to 0 ---
    with patch(
        "business_app.services.corporate_contract_service.CorporateContractService.release_for_order",
        return_value=None,
    ):
        cancelled = order_service.cancel_order(order.id, user_id=sample_user.id, reason="Customer request")

    assert cancelled.status == OrderStatus.CANCELLED
    redemption = RewardRedemption.query.filter_by(order_id=order.id).first()
    assert redemption.status == "cancelled"
    assert service.get_available_points(sample_user.id) == 1600  # fully restored
    db.session.refresh(discount_reward)
    assert discount_reward.redemptions_used == 0

    db.session.refresh(account)
    # Refund un-redeems the lifetime counter but does NOT inflate total_earned.
    assert account.total_redeemed == 0
    assert account.total_earned == 1600

    # Reconciliation: balance == earned lots (refund is an ADJUSTMENT credit lot,
    # so live balance now exceeds the EARNED+BONUS sum by the refunded 100).
    snap = _reconcile(service, sample_user.id)
    assert snap["earned_lots_sum"] == 1600
    assert snap["live_balance"] == 1600  # 1500 live earn lots + 100 refund adjustment lot


# --------------------------------------------------------------------------- #
# J2 — Free-product reward redeemed at order creation
# --------------------------------------------------------------------------- #


@pytest.mark.integration
@pytest.mark.order
def test_j2_free_product_reward_injects_zero_priced_item(
    app, db, sample_user, sample_product, sample_category, mock_inventory_service,
    loyalty_program, free_product, free_product_reward, delivery_address, service,
):
    _account(sample_user.id, loyalty_program.id, 1000)
    _seed_earn_lot(sample_user.id, 1000)
    db.session.commit()

    order_service = _make_order_service(
        mock_inventory_service,
        [_availability(sample_product), _availability(free_product)],
    )
    order = _patched_create_order(
        order_service, sample_user.id,
        _order_data(sample_product, delivery_address, reward_id=free_product_reward.id),
    )
    db.session.refresh(order)

    free_items = [i for i in order.order_items if i.product_id == free_product.id]
    assert len(free_items) == 1
    assert free_items[0].quantity == 2
    assert free_items[0].unit_price == Decimal("0.00")
    assert free_items[0].total_price == Decimal("0.00")

    redemption = RewardRedemption.query.filter_by(order_id=order.id, status="applied").first()
    assert redemption is not None
    assert redemption.reward_type == "free_product"
    assert redemption.points_spent == 100
    assert redemption.free_product_id == free_product.id

    assert service.get_available_points(sample_user.id) == 900  # 1000 - 100 cost


# --------------------------------------------------------------------------- #
# J3 — Expiry only sweeps unspent lapsed remainder (no double count)
# --------------------------------------------------------------------------- #


@pytest.mark.integration
@pytest.mark.order
def test_j3_expiry_only_removes_unspent_lapsed_remainder(
    app, db, sample_user, loyalty_program, service,
):
    now = datetime.now(timezone.utc)
    _account(sample_user.id, loyalty_program.id, 0)

    # Oldest lot lapses TOMORROW after we partially spend it; a second lot is
    # already past its expiry; a third lot is safely in the future.
    soon = now + timedelta(days=1)
    lapsed = now - timedelta(days=1)
    far = now + timedelta(days=200)

    old_lot = _seed_earn_lot(sample_user.id, 300, created_at=now - timedelta(days=10),
                             expires_at=soon, description="old")
    expired_lot = _seed_earn_lot(sample_user.id, 200, created_at=now - timedelta(days=400),
                                 expires_at=lapsed, description="already lapsed")
    future_lot = _seed_earn_lot(sample_user.id, 500, created_at=now - timedelta(days=2),
                                expires_at=far, description="future")
    acc = LoyaltyPoints.query.filter_by(user_id=sample_user.id).first()
    acc.total_earned = 1000
    acc.current_balance = 1000
    db.session.commit()

    # The expired_lot is not yet swept, so it does not count toward available.
    assert service.get_available_points(sample_user.id) == 800  # 300 + 500

    # Spend 150 via a redemption: FIFO draws the OLDEST *live* lot first. The
    # already-lapsed lot is excluded from live lots, so the 300-pt soon-to-expire
    # lot is drawn down to 150.
    service.deduct_points(sample_user.id, 150, "Spend", skip_notification=True)
    db.session.refresh(old_lot)
    db.session.refresh(future_lot)
    assert old_lot.remaining_points == 150
    assert future_lot.remaining_points == 500
    assert service.get_available_points(sample_user.id) == 650  # 150 + 500

    # get_points_expiring_soon surfaces lots lapsing within `days`. NOTE: the
    # method sums lot ``points`` (not ``remaining_points``), so the partially-spent
    # old_lot reports its full original 300 even though only 150 are unspent.
    # future_lot (200 days) is outside the 7-day window; the already-lapsed lot is
    # in the past, not "soon".
    expiring = service.get_points_expiring_soon(days=7)
    mine = [row for row in expiring if row["user_id"] == sample_user.id]
    assert len(mine) == 1
    assert mine[0]["expiring_points"] == 300

    # Lapse the old_lot by backdating its expiry, then run the REAL sweep (no clock
    # patching — that is fragile against tz-naive/aware SQL comparisons on SQLite).
    old_lot.expires_at = now - timedelta(days=1)
    db.session.commit()
    result = service.expire_points()

    db.session.refresh(old_lot)
    db.session.refresh(expired_lot)
    db.session.refresh(future_lot)
    db.session.refresh(acc)

    # old_lot had 150 unspent -> expires 150 (NOT the original 300: the spent 150
    # is not double-counted). expired_lot had its full 200 still unspent.
    assert old_lot.is_expired is True and old_lot.remaining_points == 0
    assert expired_lot.is_expired is True and expired_lot.remaining_points == 0
    assert future_lot.is_expired is False and future_lot.remaining_points == 500
    assert result["total_expired_points"] == 350  # 150 + 200

    assert acc.total_expired == 350
    assert service.get_available_points(sample_user.id) == 500  # only the future lot survives

    # Reconciliation: balance == total_earned - net_redeemed - expired.
    # 1000 earned - 150 redeemed - 350 expired = 500.
    assert acc.current_balance == 500
    assert acc.total_earned - acc.total_redeemed - acc.total_expired == 500


# --------------------------------------------------------------------------- #
# J4 — Tier progression, lock, no mid-window downgrade, then expiry downgrade
# --------------------------------------------------------------------------- #


@pytest.mark.integration
@pytest.mark.order
def test_j4_tier_progression_lock_and_downgrade_after_lock_expires(
    app, db, sample_user, loyalty_program, tiers, service,
):
    _account(sample_user.id, loyalty_program.id, 0)
    db.session.commit()

    # Bronze -> Silver after crossing 500 qualifying points.
    service.award_points(sample_user.id, 500, "to silver", action_type=LoyaltyActionType.PURCHASE)
    acc = LoyaltyPoints.query.filter_by(user_id=sample_user.id).first()
    db.session.refresh(acc)
    assert acc.current_tier == "Silver"
    silver_lock = acc.tier_valid_until
    assert silver_lock is not None

    # Silver -> Gold after crossing 1000 qualifying points.
    service.award_points(sample_user.id, 600, "to gold", action_type=LoyaltyActionType.PURCHASE)
    db.session.refresh(acc)
    assert acc.current_tier == "Gold"
    assert acc.tier_valid_until is not None
    assert service.calculate_qualifying_points(sample_user.id) == 1100

    # Drop qualifying points below Gold's threshold. calculate_qualifying_points is
    # TIME-WINDOWED (trailing 365 days of EARNED+BONUS), not balance/expiry based —
    # so we age the earning txns out of the window rather than flagging them expired.
    old_ts = datetime.now(timezone.utc) - timedelta(days=400)
    for lot in LoyaltyTransaction.query.filter_by(user_id=sample_user.id).all():
        lot.created_at = old_ts
    db.session.commit()
    assert service.calculate_qualifying_points(sample_user.id) == 0

    service.check_tier_expiration(sample_user.id)
    db.session.refresh(acc)
    assert acc.current_tier == "Gold"  # lock protects the tier

    # Force the lock into the past -> downgrade is now allowed, drops to Bronze
    # (0 qualifying points -> lowest tier).
    acc.tier_valid_until = datetime.now(timezone.utc) - timedelta(days=1)
    db.session.commit()
    service.check_tier_expiration(sample_user.id)
    db.session.refresh(acc)
    assert acc.current_tier == "Bronze"
    assert acc.tier_valid_until is None


# --------------------------------------------------------------------------- #
# J5 — Order-edit clawback clamped to available balance
# --------------------------------------------------------------------------- #


@pytest.mark.integration
@pytest.mark.order
def test_j5_order_edit_clawback_clamped_to_available_balance(
    app, db, sample_user, loyalty_program, service,
):
    _account(sample_user.id, loyalty_program.id, 0)
    db.session.commit()

    # Earn 200 from an order, then spend 150 (only 50 remain).
    earn_txn = service.award_points(
        sample_user.id, 200, "Order earnings", action_type=LoyaltyActionType.PURCHASE, reference_id=4242,
    )
    service.deduct_points(sample_user.id, 150, "Redeem", order_id=4242, skip_notification=True)
    assert service.get_available_points(sample_user.id) == 50

    acc = LoyaltyPoints.query.filter_by(user_id=sample_user.id).first()
    db.session.refresh(acc)
    assert acc.total_earned == 200
    assert acc.total_redeemed == 150

    # Order edited down: old=200, new=0 -> needs to claw back 200, but only 50 are
    # available (clamp=True default). The other 150 is uncollectible.
    result = service.reverse_earnings(sample_user.id, 4242, old_points_earned=200, new_points_earned=0)
    assert result["diff"] == 200
    assert result["clawback"] == 50
    assert result["uncollectible"] == 150
    assert result["award"] == 0
    assert result["transaction_id"] is not None

    db.session.refresh(acc)
    assert service.get_available_points(sample_user.id) == 0  # 50 - 50 clawback
    assert acc.current_balance == 0
    # total_earned reduced only by what was actually clawed back.
    assert acc.total_earned == 150
    # total_redeemed unchanged by a clawback (reserved for real redemptions).
    assert acc.total_redeemed == 150

    # The clawback is a negative ADJUSTMENT transaction.
    clawback_txn = LoyaltyTransaction.query.filter_by(id=result["transaction_id"]).first()
    assert clawback_txn.transaction_type == LoyaltyTransactionType.ADJUSTMENT
    assert clawback_txn.points == -50
    assert clawback_txn.extra_data.get("uncollectible") == 150
    assert earn_txn.id  # sanity: original earn lot exists


# --------------------------------------------------------------------------- #
# J6 — Gift points between two users by phone
# --------------------------------------------------------------------------- #


@pytest.mark.integration
@pytest.mark.order
def test_j6_gift_points_by_phone_moves_balance_between_users(
    app, db, sample_user, loyalty_program, service,
):
    recipient = User(
        email="recipient@example.com",
        phone="+998901112233",
        password_hash=hash_password("TestPassword123!"),
        first_name="Recipient",
        last_name="User",
        user_type=UserType.INDIVIDUAL,
        role=UserRole.CUSTOMER,
        is_verified=True,
    )
    db.session.add(recipient)
    db.session.commit()

    # Sender (sample_user) starts with 500 spendable points.
    _account(sample_user.id, loyalty_program.id, 500)
    _seed_earn_lot(sample_user.id, 500)
    db.session.commit()

    service.gift_points_by_phone(sample_user.id, recipient.phone, 200, message="Enjoy!")

    assert service.get_available_points(sample_user.id) == 300
    assert service.get_available_points(recipient.id) == 200

    sender_acc = LoyaltyPoints.query.filter_by(user_id=sample_user.id).first()
    recipient_acc = LoyaltyPoints.query.filter_by(user_id=recipient.id).first()
    db.session.refresh(sender_acc)
    db.session.refresh(recipient_acc)
    assert sender_acc.total_redeemed == 200  # gift deducts from sender
    assert recipient_acc.total_earned == 200  # gift credited as a new lot

    # Insufficient balance is rejected and leaves balances untouched.
    with pytest.raises(ValidationError, match="Insufficient points"):
        service.gift_points_by_phone(sample_user.id, recipient.phone, 10_000)
    assert service.get_available_points(sample_user.id) == 300

    # Unknown recipient phone -> NotFoundError.
    with pytest.raises(NotFoundError):
        service.gift_points_by_phone(sample_user.id, "+998905556677", 10)


# --------------------------------------------------------------------------- #
# J7 — Referral: pending until referee's first delivered order, then both paid
# --------------------------------------------------------------------------- #


@pytest.mark.integration
@pytest.mark.order
def test_j7_referral_grants_both_bonuses_after_referee_first_delivery(
    app, db, sample_user, sample_product, mock_inventory_service,
    loyalty_program, delivery_address, service,
):
    referrer = sample_user
    referrer_code = service.get_user_referral_code(referrer.id)
    assert referrer_code

    referee = User(
        email="referee@example.com",
        phone="+998901119988",
        password_hash=hash_password("TestPassword123!"),
        first_name="Referee",
        last_name="User",
        user_type=UserType.INDIVIDUAL,
        role=UserRole.CUSTOMER,
        is_verified=True,
    )
    db.session.add(referee)
    db.session.commit()

    # Referee registers with the referrer's code -> a PENDING referral.
    res = service.process_referral(referrer_code, referee.id)
    assert res["status"] == "pending"
    db.session.refresh(referee)
    assert referee.referred_by_user_id == referrer.id

    # Re-using a code is a conflict; self-referral is invalid.
    with pytest.raises(ConflictError):
        service.process_referral(referrer_code, referee.id)
    with pytest.raises(ValidationError):
        service.process_referral(referrer_code, referrer.id)

    # No bonuses before the referee's first delivered + fully paid order.
    processed = service.process_pending_referrals()
    assert processed["processed_count"] == 0
    assert ReferralProgram.query.filter_by(status="pending").count() == 1

    # Referee places an order, which is then delivered and fully paid.
    order_service = _make_order_service(mock_inventory_service, [_availability(sample_product)])
    referee_address = UserAddress(
        user_id=referee.id, title="Home", full_address="Ref Street 2",
        street_address="Ref Street 2", city="Tashkent",
        latitude=41.31, longitude=69.28, is_default=True,
    )
    db.session.add(referee_address)
    db.session.commit()
    order = _patched_create_order(
        order_service, referee.id, _order_data(sample_product, referee_address),
    )
    order.status = OrderStatus.DELIVERED
    db.session.commit()

    # Delivered but not yet fully paid (e.g. COD collection pending) -> no payout.
    processed = service.process_pending_referrals()
    assert processed["processed_count"] == 0
    assert ReferralProgram.query.filter_by(status="pending").count() == 1

    # Once the order is fully paid, both bonuses are granted (referrer=50, referee=25 = half of 50).
    order.is_paid = True
    db.session.commit()
    processed = service.process_pending_referrals()
    assert processed["processed_count"] == 1
    assert processed["total_points_awarded"] == 75

    referral = ReferralProgram.query.filter_by(referee_id=referee.id).first()
    assert referral.status == "completed"
    assert referral.first_order_id == order.id
    assert service.get_available_points(referrer.id) == 50
    assert service.get_available_points(referee.id) == 25


# --------------------------------------------------------------------------- #
# J8 — Redeem -> refund -> re-redeem on a NEW order; one-reward-per-order
# --------------------------------------------------------------------------- #


@pytest.mark.integration
@pytest.mark.order
def test_j8_redeem_refund_then_reredeem_on_new_order(
    app, db, sample_user, sample_product, mock_inventory_service,
    loyalty_program, discount_reward, delivery_address, service,
):
    _account(sample_user.id, loyalty_program.id, 1000)
    _seed_earn_lot(sample_user.id, 1000)
    db.session.commit()

    order_service = _make_order_service(mock_inventory_service, [_availability(sample_product)])

    # First order with the reward applied.
    order1 = _patched_create_order(
        order_service, sample_user.id,
        _order_data(sample_product, delivery_address, reward_id=discount_reward.id),
    )
    assert service.get_available_points(sample_user.id) == 900

    # Applying a second reward to the SAME order is rejected (one per order).
    with pytest.raises(ValidationError, match="already been applied"):
        service.apply_reward_to_order(order1, discount_reward.id, commit=True)

    # Cancel order1 -> refund the spent points.
    with patch(
        "business_app.services.corporate_contract_service.CorporateContractService.release_for_order",
        return_value=None,
    ):
        order_service.cancel_order(order1.id, user_id=sample_user.id, reason="changed mind")
    assert service.get_available_points(sample_user.id) == 1000
    db.session.refresh(discount_reward)
    assert discount_reward.redemptions_used == 0

    # Re-apply the SAME reward to a brand new order -> succeeds.
    order2 = _patched_create_order(
        order_service, sample_user.id,
        _order_data(sample_product, delivery_address, reward_id=discount_reward.id),
    )
    db.session.refresh(order2)
    assert order2.loyalty_discount == Decimal("500.00")
    assert RewardRedemption.query.filter_by(order_id=order2.id, status="applied").count() == 1
    assert service.get_available_points(sample_user.id) == 900
    db.session.refresh(discount_reward)
    assert discount_reward.redemptions_used == 1


# --------------------------------------------------------------------------- #
# J9 — Rejections leave no points lost
# --------------------------------------------------------------------------- #


@pytest.mark.integration
@pytest.mark.order
def test_j9_reward_rejections_do_not_lose_points(
    app, db, sample_user, loyalty_program, service,
):
    from tests.unit.test_loyalty_redemption import _order  # reuse simple order builder

    # --- Insufficient points ---
    _account(sample_user.id, loyalty_program.id, 50)
    _seed_earn_lot(sample_user.id, 50)
    db.session.commit()
    cheap = LoyaltyReward(
        program_id=loyalty_program.id, name="needs 100", reward_type="discount",
        discount_type="fixed", discount_value=Decimal("500"), points_cost=100,
        is_active=True, max_uses_per_user=5, redemptions_used=0,
    )
    db.session.add(cheap)
    db.session.commit()
    order_a = _order(db, sample_user.id, 10000)
    with pytest.raises(ValidationError, match="Insufficient points"):
        service.apply_reward_to_order(order_a, cheap.id, commit=True)
    assert service.get_available_points(sample_user.id) == 50
    assert RewardRedemption.query.filter_by(user_id=sample_user.id).count() == 0

    # Top up so the remaining rejections are about reward rules, not balance.
    _seed_earn_lot(sample_user.id, 1000)
    acc = LoyaltyPoints.query.filter_by(user_id=sample_user.id).first()
    acc.total_earned = 1050
    acc.current_balance = 1050
    db.session.commit()
    assert service.get_available_points(sample_user.id) == 1050

    # --- Below min_order_value ---
    big_min = LoyaltyReward(
        program_id=loyalty_program.id, name="min 20k", reward_type="discount",
        discount_type="fixed", discount_value=Decimal("100"), points_cost=10,
        is_active=True, min_order_value=Decimal("20000.00"),
        max_uses_per_user=5, redemptions_used=0,
    )
    db.session.add(big_min)
    db.session.commit()
    order_b = _order(db, sample_user.id, 10000)  # below 20000
    with pytest.raises(ValidationError, match="minimum value"):
        service.apply_reward_to_order(order_b, big_min.id, commit=True)
    assert service.get_available_points(sample_user.id) == 1050

    # --- max_uses_per_user exceeded ---
    once = LoyaltyReward(
        program_id=loyalty_program.id, name="once only", reward_type="discount",
        discount_type="fixed", discount_value=Decimal("100"), points_cost=10,
        is_active=True, max_uses_per_user=1, redemptions_used=0,
    )
    db.session.add(once)
    db.session.commit()
    order_c1 = _order(db, sample_user.id, 10000)
    service.apply_reward_to_order(order_c1, once.id, commit=True)
    balance_after_first = service.get_available_points(sample_user.id)
    order_c2 = _order(db, sample_user.id, 10000)
    with pytest.raises(ValidationError, match="limit"):
        service.apply_reward_to_order(order_c2, once.id, commit=True)
    # The rejected second attempt must not deduct points.
    assert service.get_available_points(sample_user.id) == balance_after_first

    # --- Outside valid window (expired) ---
    expired_reward = LoyaltyReward(
        program_id=loyalty_program.id, name="expired", reward_type="discount",
        discount_type="fixed", discount_value=Decimal("100"), points_cost=10,
        is_active=True, max_uses_per_user=5, redemptions_used=0,
        valid_until=datetime.now(timezone.utc) - timedelta(days=1),
    )
    db.session.add(expired_reward)
    db.session.commit()
    order_d = _order(db, sample_user.id, 10000)
    before = service.get_available_points(sample_user.id)
    with pytest.raises(ValidationError, match="expired"):
        service.apply_reward_to_order(order_d, expired_reward.id, commit=True)
    assert service.get_available_points(sample_user.id) == before

    # can_redeem_reward agrees with each rejection reason.
    assert service.can_redeem_reward(sample_user.id, big_min.id) is True  # balance ok; min checked at apply
    assert service.can_redeem_reward(sample_user.id, once.id) is False  # per-user limit reached
    assert service.can_redeem_reward(sample_user.id, expired_reward.id) is False  # window closed


# --------------------------------------------------------------------------- #
# J10 — Reconciliation invariant after earn/redeem/refund/expire/clawback
# --------------------------------------------------------------------------- #


@pytest.mark.integration
@pytest.mark.order
def test_j10_reconciliation_invariant_after_complex_sequence(
    app, db, sample_user, sample_product, mock_inventory_service,
    loyalty_program, tiers, discount_reward, delivery_address, service,
):
    _account(sample_user.id, loyalty_program.id, 0)
    db.session.commit()

    # EARN: two lots. The short lot is the NEWEST (created_at after the 1000-pt
    # award) so FIFO redemptions draw the older 1000-lot first and leave the short
    # lot fully unspent for the expiry step.
    now = datetime.now(timezone.utc)
    service.award_points(sample_user.id, 1000, "earn-1", action_type=LoyaltyActionType.PURCHASE)
    short_lot = _seed_earn_lot(
        sample_user.id, 200, created_at=now + timedelta(minutes=5),
        expires_at=now + timedelta(days=1), description="short",
    )
    acc = LoyaltyPoints.query.filter_by(user_id=sample_user.id).first()
    acc.total_earned = 1200
    acc.current_balance = 1200
    db.session.commit()
    assert service.get_available_points(sample_user.id) == 1200

    # REDEEM: apply a 100-pt discount reward at order creation.
    order_service = _make_order_service(mock_inventory_service, [_availability(sample_product)])
    order = _patched_create_order(
        order_service, sample_user.id,
        _order_data(sample_product, delivery_address, reward_id=discount_reward.id),
    )
    assert service.get_available_points(sample_user.id) == 1100  # 1200 - 100

    # REFUND: cancel that order -> 100 points refunded as an ADJUSTMENT credit.
    with patch(
        "business_app.services.corporate_contract_service.CorporateContractService.release_for_order",
        return_value=None,
    ):
        order_service.cancel_order(order.id, user_id=sample_user.id, reason="refund step")
    assert service.get_available_points(sample_user.id) == 1200

    # REDEEM again (net redemption that stays spent): deduct 300 directly.
    service.deduct_points(sample_user.id, 300, "manual redeem", order_id=order.id, skip_notification=True)
    assert service.get_available_points(sample_user.id) == 900

    # EXPIRE: the 200-pt short lot lapses. The FIFO 300 redemption drew from the
    # older 1000-lot, so short_lot is still fully unspent -> all 200 expire. Lapse
    # it by backdating its expiry, then run the REAL sweep (no fragile clock patch).
    db.session.refresh(short_lot)
    short_lot.expires_at = now - timedelta(days=1)
    db.session.commit()
    expire_result = service.expire_points()
    assert expire_result["total_expired_points"] == 200
    assert service.get_available_points(sample_user.id) == 700  # 900 - 200

    # CLAWBACK: order edited down by 100 (clamped). Balance is 700 so it claws
    # the full 100, nothing uncollectible.
    cb = service.reverse_earnings(sample_user.id, order.id, old_points_earned=1000, new_points_earned=900)
    assert cb["clawback"] == 100
    assert cb["uncollectible"] == 0
    assert service.get_available_points(sample_user.id) == 600  # 700 - 100

    # ---- Reconciliation invariant ----
    db.session.refresh(acc)
    txns = LoyaltyTransaction.query.filter_by(user_id=sample_user.id).all()

    earned_lots_points = sum(
        t.points for t in txns
        if t.points > 0 and t.transaction_type in (LoyaltyTransactionType.EARNED, LoyaltyTransactionType.BONUS)
    )
    live_remaining = service.get_available_points(sample_user.id)

    # total_earned tracks EARNED+BONUS lot points, reduced by the clawback:
    # 1200 earned - 100 clawback = 1100.
    assert earned_lots_points == 1200
    assert acc.total_earned == 1100

    # net_redeemed = gross redeemed (100 reward + 300 manual = 400) - refund (100) = 300.
    # total_redeemed mirrors this: 400 redeemed - 100 refunded = 300.
    assert acc.total_redeemed == 300

    # expired = 200.
    assert acc.total_expired == 200

    # Invariant 1: cached balance == live ledger remainder.
    assert acc.current_balance == live_remaining

    # Invariant 2 (the production reconciliation identity): the refund REDUCED
    # total_redeemed (cancel_redemption_for_order does total_redeemed -= refund),
    # so it is netted into the counter rather than being a credit on top. The
    # simple identity therefore holds:
    #   current_balance == total_earned - total_redeemed - total_expired
    #   600              == 1100         - 300            - 200
    assert acc.current_balance == acc.total_earned - acc.total_redeemed - acc.total_expired

    # Gross-ledger cross-check of the same number:
    #   1200 earned - 400 gross_redeemed + 100 refund - 200 expired - 100 clawback = 600.
    assert live_remaining == 1200 - 400 + 100 - 200 - 100


# --------------------------------------------------------------------------- #
# J9–J21 — Surprise rewards: nightly batch over the day's delivered+paid orders
# --------------------------------------------------------------------------- #
# These drive a real order through OrderService.create_order, put it into the
# delivered (+paid) end-state production produces (a Delivery row with
# delivered_at + order.is_paid/paid_at), then run the nightly batch / Celery task
# and assert the surprise-reward outcome. The win/lose roll is made deterministic.

_SR_TZ = ZoneInfo(DISPLAY_TIMEZONE)
_SR_DAY = date(2026, 6, 10)


def _sr_at(hour, *, day=10, month=6, year=2026):
    """A business-local wall-clock time as an aware UTC datetime."""
    return datetime(year, month, day, hour, tzinfo=_SR_TZ).astimezone(timezone.utc)


_SR_IN_DAY = _sr_at(12)              # noon of day D
_SR_PREV_DAY = _sr_at(12, day=9)    # noon of D-1 (prepaid: paid before delivery day)
_SR_NEXT_DAY = _sr_at(1, day=11)    # 01:00 of D+1 (COD paid next day)
_SR_LATE = _sr_at(23)              # 23:00 of D (near the day boundary)
_SR_AFTER_MIDNIGHT = _sr_at(0, day=11)  # 00:00 of D+1 == end of day D


def _sr_force_win(monkeypatch, amount=100):
    monkeypatch.setattr("random.random", lambda: 0.0)
    monkeypatch.setattr("random.choice", lambda seq: amount)


def _sr_force_lose(monkeypatch):
    monkeypatch.setattr("random.random", lambda: 0.999)


def _sr_user(db, suffix, user_type=UserType.INDIVIDUAL):
    user = User(
        email=f"srj{suffix}@example.com", phone=f"+99890222{suffix:04d}",
        password_hash=hash_password("TestPassword123!"),
        first_name="SR", last_name=str(suffix),
        user_type=user_type, role=UserRole.CUSTOMER, is_verified=True,
    )
    db.session.add(user)
    db.session.commit()
    addr = UserAddress(
        user_id=user.id, title="Home", full_address=f"SR {suffix} St",
        street_address=f"SR {suffix} St", city="Tashkent",
        latitude=41.31, longitude=69.28, is_default=True,
    )
    db.session.add(addr)
    db.session.commit()
    return user, addr


def _sr_deliver_and_pay(db, order_service, product, user, addr, *,
                        delivered_at=_SR_IN_DAY, paid_at=_SR_IN_DAY, is_paid=True,
                        delivery_status=DeliveryStatus.DELIVERED, via_service=True):
    """Create an order, then put it in the delivered (+paid) end-state the
    production flow produces — a Delivery with delivered_at and order.is_paid/
    paid_at — without re-triggering order-earn points (status set directly, like
    the other journeys), so the surprise BONUS is the only ledger movement.

    ``via_service`` uses the real OrderService.create_order (individuals).
    Entity orders need an admin-assigned subtype + contracts, which is out of
    scope here, so those are constructed directly."""
    if via_service:
        order = _patched_create_order(order_service, user.id, _order_data(product, addr))
    else:
        order = Order(
            user_id=user.id, order_number=f"SR-DIRECT-{user.id}",
            status=OrderStatus.PENDING,
            subtotal=Decimal("16000.00"), total_amount=Decimal("16000.00"),
        )
        db.session.add(order)
        db.session.commit()
    deliv = Delivery(
        order_id=order.id, scheduled_date=(delivered_at or _SR_IN_DAY),
        scheduled_time_slot="09:00-12:00",
        delivered_at=delivered_at, status=delivery_status,
    )
    db.session.add(deliv)
    if delivered_at is not None and delivery_status == DeliveryStatus.DELIVERED:
        order.status = OrderStatus.DELIVERED
    if is_paid:
        order.is_paid = True
        order.paid_at = paid_at
    db.session.commit()
    return order


def _sr_surprise_txn(user_id):
    return LoyaltyTransaction.query.filter_by(
        user_id=user_id, transaction_type=LoyaltyTransactionType.BONUS
    ).filter(
        LoyaltyTransaction.description.contains("Surprise")
    ).first()


@pytest.mark.integration
@pytest.mark.order
def test_j9_surprise_prepaid_delivered_and_paid_same_day_is_awarded(
    app, db, sample_product, mock_inventory_service, loyalty_program, service, monkeypatch,
):
    _sr_force_win(monkeypatch, amount=200)
    order_service = _make_order_service(mock_inventory_service, [_availability(sample_product)])
    user, addr = _sr_user(db, 1)
    # Prepaid: paid the day BEFORE the delivery day, delivered on day D.
    _sr_deliver_and_pay(db, order_service, sample_product, user, addr,
                        delivered_at=_SR_IN_DAY, paid_at=_SR_PREV_DAY)

    result = service.process_daily_surprise_rewards(for_date=_SR_DAY)

    assert result["awarded"] == 1
    assert service.get_available_points(user.id) == 200
    txn = _sr_surprise_txn(user.id)
    assert txn is not None
    assert txn.transaction_type == LoyaltyTransactionType.BONUS
    assert (txn.extra_data or {}).get("action_type") == LoyaltyActionType.SURPRICE_REWARD.value
    # Reconciliation invariant: a BONUS lot counts toward earned + balance.
    snap = _reconcile(service, user.id)
    assert snap["earned_lots_sum"] == 200
    assert snap["live_balance"] == 200


@pytest.mark.integration
@pytest.mark.order
def test_j10_surprise_cod_paid_same_day_is_awarded(
    app, db, sample_product, mock_inventory_service, loyalty_program, service, monkeypatch,
):
    _sr_force_win(monkeypatch, amount=50)
    order_service = _make_order_service(mock_inventory_service, [_availability(sample_product)])
    user, addr = _sr_user(db, 2)
    # COD: delivered at noon, cash recorded later the SAME day.
    _sr_deliver_and_pay(db, order_service, sample_product, user, addr,
                        delivered_at=_SR_IN_DAY, paid_at=_sr_at(18))

    assert service.process_daily_surprise_rewards(for_date=_SR_DAY)["awarded"] == 1
    assert service.get_available_points(user.id) == 50


@pytest.mark.integration
@pytest.mark.order
def test_j11_surprise_cod_paid_next_day_is_not_awarded(
    app, db, sample_product, mock_inventory_service, loyalty_program, service, monkeypatch,
):
    _sr_force_win(monkeypatch)
    order_service = _make_order_service(mock_inventory_service, [_availability(sample_product)])
    user, addr = _sr_user(db, 3)
    # Delivered day D, paid the NEXT day -> never eligible.
    _sr_deliver_and_pay(db, order_service, sample_product, user, addr,
                        delivered_at=_SR_IN_DAY, paid_at=_SR_NEXT_DAY)

    # Neither the delivery day's batch nor the next day's batch awards it.
    assert service.process_daily_surprise_rewards(for_date=_SR_DAY)["awarded"] == 0
    assert service.process_daily_surprise_rewards(for_date=date(2026, 6, 11))["awarded"] == 0
    assert service.get_available_points(user.id) == 0


@pytest.mark.integration
@pytest.mark.order
def test_j12_surprise_delivered_but_unpaid_is_not_awarded(
    app, db, sample_product, mock_inventory_service, loyalty_program, service, monkeypatch,
):
    _sr_force_win(monkeypatch)
    order_service = _make_order_service(mock_inventory_service, [_availability(sample_product)])
    user, addr = _sr_user(db, 4)
    _sr_deliver_and_pay(db, order_service, sample_product, user, addr,
                        delivered_at=_SR_IN_DAY, paid_at=None, is_paid=False)

    assert service.process_daily_surprise_rewards(for_date=_SR_DAY)["awarded"] == 0
    assert service.get_available_points(user.id) == 0


@pytest.mark.integration
@pytest.mark.order
def test_j13_surprise_excludes_entity_customers(
    app, db, sample_product, mock_inventory_service, loyalty_program, service, monkeypatch,
):
    _sr_force_win(monkeypatch)
    order_service = _make_order_service(mock_inventory_service, [_availability(sample_product)])
    user, addr = _sr_user(db, 5, user_type=UserType.ENTITY)  # workplace / grocery store
    _sr_deliver_and_pay(db, order_service, sample_product, user, addr,
                        delivered_at=_SR_IN_DAY, paid_at=_SR_IN_DAY, via_service=False)

    assert service.process_daily_surprise_rewards(for_date=_SR_DAY)["awarded"] == 0
    assert service.get_available_points(user.id) == 0


@pytest.mark.integration
@pytest.mark.order
def test_j14_surprise_losing_roll_awards_nothing(
    app, db, sample_product, mock_inventory_service, loyalty_program, service, monkeypatch,
):
    _sr_force_lose(monkeypatch)
    order_service = _make_order_service(mock_inventory_service, [_availability(sample_product)])
    user, addr = _sr_user(db, 6)
    _sr_deliver_and_pay(db, order_service, sample_product, user, addr)

    result = service.process_daily_surprise_rewards(for_date=_SR_DAY)
    assert result["candidates"] == 1  # it WAS a candidate...
    assert result["awarded"] == 0     # ...but lost the roll
    assert service.get_available_points(user.id) == 0


@pytest.mark.integration
@pytest.mark.order
def test_j15_surprise_disabled_program_awards_nothing(
    app, db, sample_product, mock_inventory_service, loyalty_program, service, monkeypatch,
):
    _sr_force_win(monkeypatch)
    loyalty_program.surprise_enabled = False
    db.session.commit()
    order_service = _make_order_service(mock_inventory_service, [_availability(sample_product)])
    user, addr = _sr_user(db, 7)
    _sr_deliver_and_pay(db, order_service, sample_product, user, addr)

    assert service.process_daily_surprise_rewards(for_date=_SR_DAY)["awarded"] == 0


@pytest.mark.integration
@pytest.mark.order
def test_j16_surprise_cooldown_blocks_recent_winner(
    app, db, sample_product, mock_inventory_service, loyalty_program, service, monkeypatch,
):
    _sr_force_win(monkeypatch)
    order_service = _make_order_service(mock_inventory_service, [_availability(sample_product)])
    user, addr = _sr_user(db, 8)
    # The user already won a surprise 2 days ago -> still inside the 7-day cooldown.
    prior = LoyaltyTransaction(
        user_id=user.id, points=100, transaction_type=LoyaltyTransactionType.BONUS,
        description="Surprise Reward! Thanks for being loyal 💙",
        extra_data={"action_type": LoyaltyActionType.SURPRICE_REWARD.value},
        created_at=datetime.now(timezone.utc) - timedelta(days=2),
    )
    db.session.add(prior)
    db.session.commit()
    _sr_deliver_and_pay(db, order_service, sample_product, user, addr)

    assert service.process_daily_surprise_rewards(for_date=_SR_DAY)["awarded"] == 0


@pytest.mark.integration
@pytest.mark.order
def test_j17_surprise_respects_global_daily_cap(
    app, db, sample_product, mock_inventory_service, loyalty_program, service, monkeypatch,
):
    _sr_force_win(monkeypatch, amount=50)
    loyalty_program.surprise_daily_cap = 2
    db.session.commit()
    order_service = _make_order_service(mock_inventory_service, [_availability(sample_product)])
    for i in range(3):
        u, a = _sr_user(db, 20 + i)
        _sr_deliver_and_pay(db, order_service, sample_product, u, a,
                            delivered_at=_sr_at(9 + i), paid_at=_sr_at(9 + i))

    result = service.process_daily_surprise_rewards(for_date=_SR_DAY)
    assert result["candidates"] == 3
    assert result["awarded"] == 2  # capped


@pytest.mark.integration
@pytest.mark.order
def test_j18_surprise_one_roll_per_user_per_day(
    app, db, sample_product, mock_inventory_service, loyalty_program, service, monkeypatch,
):
    _sr_force_win(monkeypatch, amount=100)
    order_service = _make_order_service(mock_inventory_service, [_availability(sample_product)])
    user, addr = _sr_user(db, 9)
    _sr_deliver_and_pay(db, order_service, sample_product, user, addr,
                        delivered_at=_sr_at(10), paid_at=_sr_at(10))
    _sr_deliver_and_pay(db, order_service, sample_product, user, addr,
                        delivered_at=_sr_at(15), paid_at=_sr_at(15))

    result = service.process_daily_surprise_rewards(for_date=_SR_DAY)
    assert result["candidates"] == 2  # both orders are candidates...
    assert result["awarded"] == 1     # ...but the user gets at most one
    assert service.get_available_points(user.id) == 100


@pytest.mark.integration
@pytest.mark.order
def test_j19_surprise_celery_task_awards_for_yesterday(
    app, db, sample_product, mock_inventory_service, loyalty_program, service, monkeypatch,
):
    _sr_force_win(monkeypatch, amount=50)
    # Drive the actual Celery task, which processes *yesterday* — place the order
    # on yesterday's business day so the run is deterministic regardless of date.
    yesterday = (datetime.now(_SR_TZ) - timedelta(days=1)).date()
    deliv = datetime(yesterday.year, yesterday.month, yesterday.day, 12, tzinfo=_SR_TZ).astimezone(timezone.utc)
    order_service = _make_order_service(mock_inventory_service, [_availability(sample_product)])
    user, addr = _sr_user(db, 30)
    _sr_deliver_and_pay(db, order_service, sample_product, user, addr,
                        delivered_at=deliv, paid_at=deliv)

    with app.app_context():
        result = loyalty_tasks.process_daily_surprise_rewards.run()

    assert result["success"] is True
    assert result["awarded"] == 1
    assert service.get_available_points(user.id) == 50


@pytest.mark.integration
@pytest.mark.order
def test_j20_surprise_batch_rerun_is_idempotent(
    app, db, sample_product, mock_inventory_service, loyalty_program, service, monkeypatch,
):
    _sr_force_win(monkeypatch, amount=100)
    order_service = _make_order_service(mock_inventory_service, [_availability(sample_product)])
    user, addr = _sr_user(db, 10)
    _sr_deliver_and_pay(db, order_service, sample_product, user, addr)

    first = service.process_daily_surprise_rewards(for_date=_SR_DAY)
    second = service.process_daily_surprise_rewards(for_date=_SR_DAY)

    assert first["awarded"] == 1
    assert second["awarded"] == 0  # cooldown sees the first award -> no double pay
    assert service.get_available_points(user.id) == 100


@pytest.mark.integration
@pytest.mark.order
def test_j21_surprise_day_boundary_paid_after_midnight_excluded(
    app, db, sample_product, mock_inventory_service, loyalty_program, service, monkeypatch,
):
    _sr_force_win(monkeypatch, amount=50)
    order_service = _make_order_service(mock_inventory_service, [_availability(sample_product)])

    # Delivered 23:00 day D, paid 23:30 day D -> in-window -> awarded.
    in_user, in_addr = _sr_user(db, 40)
    _sr_deliver_and_pay(db, order_service, sample_product, in_user, in_addr,
                        delivered_at=_SR_LATE, paid_at=_sr_at(23) + timedelta(minutes=30))

    # Delivered 23:00 day D, paid 00:00 day D+1 (== day_end) -> excluded.
    out_user, out_addr = _sr_user(db, 41)
    _sr_deliver_and_pay(db, order_service, sample_product, out_user, out_addr,
                        delivered_at=_SR_LATE, paid_at=_SR_AFTER_MIDNIGHT)

    result = service.process_daily_surprise_rewards(for_date=_SR_DAY)
    assert result["awarded"] == 1
    assert service.get_available_points(in_user.id) == 50
    assert service.get_available_points(out_user.id) == 0


@pytest.mark.integration
@pytest.mark.order
def test_j22_surprise_scheduled_delivery_not_completed_is_not_candidate(
    app, db, sample_product, mock_inventory_service, loyalty_program, service, monkeypatch,
):
    _sr_force_win(monkeypatch)
    order_service = _make_order_service(mock_inventory_service, [_availability(sample_product)])
    user, addr = _sr_user(db, 50)
    # Paid, but the Delivery is still SCHEDULED with no delivered_at -> not delivered.
    _sr_deliver_and_pay(db, order_service, sample_product, user, addr,
                        delivered_at=None, paid_at=_SR_IN_DAY,
                        delivery_status=DeliveryStatus.SCHEDULED)

    result = service.process_daily_surprise_rewards(for_date=_SR_DAY)
    assert result["candidates"] == 0
    assert result["awarded"] == 0


@pytest.mark.integration
@pytest.mark.order
def test_j23_surprise_mixed_day_awards_only_eligible(
    app, db, sample_product, mock_inventory_service, loyalty_program, service, monkeypatch,
):
    """A realistic day: only the prepaid + COD-same-day individuals win; entity,
    unpaid, and COD-next-day orders are skipped."""
    _sr_force_win(monkeypatch, amount=50)
    order_service = _make_order_service(mock_inventory_service, [_availability(sample_product)])

    prepaid_u, prepaid_a = _sr_user(db, 60)
    _sr_deliver_and_pay(db, order_service, sample_product, prepaid_u, prepaid_a,
                        delivered_at=_sr_at(9), paid_at=_SR_PREV_DAY)

    cod_u, cod_a = _sr_user(db, 61)
    _sr_deliver_and_pay(db, order_service, sample_product, cod_u, cod_a,
                        delivered_at=_sr_at(10), paid_at=_sr_at(16))

    entity_u, entity_a = _sr_user(db, 62, user_type=UserType.ENTITY)
    _sr_deliver_and_pay(db, order_service, sample_product, entity_u, entity_a,
                        delivered_at=_sr_at(11), paid_at=_sr_at(11), via_service=False)

    unpaid_u, unpaid_a = _sr_user(db, 63)
    _sr_deliver_and_pay(db, order_service, sample_product, unpaid_u, unpaid_a,
                        delivered_at=_sr_at(12), paid_at=None, is_paid=False)

    late_u, late_a = _sr_user(db, 64)
    _sr_deliver_and_pay(db, order_service, sample_product, late_u, late_a,
                        delivered_at=_sr_at(13), paid_at=_SR_NEXT_DAY)

    result = service.process_daily_surprise_rewards(for_date=_SR_DAY)

    assert result["awarded"] == 2  # prepaid + COD-same-day individuals only
    assert service.get_available_points(prepaid_u.id) == 50
    assert service.get_available_points(cod_u.id) == 50
    assert service.get_available_points(entity_u.id) == 0
    assert service.get_available_points(unpaid_u.id) == 0
    assert service.get_available_points(late_u.id) == 0
