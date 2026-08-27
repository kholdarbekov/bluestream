"""Shared helpers for consecutive-strike bonus rule E2E tests.

This module is intentionally prefixed with ``_`` so pytest does not collect it
as a test file. Import from it; do not duplicate these factories.

Usage pattern::

    from tests.e2e._consecutive_strike_helpers import (
        get_or_create_default_program,
        make_strike_rule,
        make_consecutive_rule,
        seed_strike_achievement,
        seed_consecutive_run,
        consecutive_awards,
        consecutive_award_total,
        strike_achievement_count,
        deliver_paid_order,
        silence_loyalty_notifications,
        build_entity_user,
    )

All functions are plain callables (NOT pytest fixtures) so any file can import
and call them freely.  They must be called inside an active Flask app context
(e.g., inside a test that receives the ``app`` fixture, or wrapped in
``with app.app_context()``).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import List, Optional
from unittest.mock import patch

from business_app import db
from business_app.models.corporate import CorporateContract, CorporateContractStatus
from business_app.models.loyalty import (
    LoyaltyConsecutiveStrikeRule,
    LoyaltyProgram,
    LoyaltyStreakRule,
    LoyaltyTransaction,
)
from business_app.models.order import Order, OrderItem
from business_app.models.user import User, UserAddress
from business_app.services.loyalty_service import LoyaltyService
from business_app.services.order_service import OrderService
from business_app.utils.constants import LoyaltyActionType, LoyaltyTransactionType
from business_app.utils.password_security import hash_password
from shared.enums import (
    CorporateContractTrackingMode,
    EntitySubtype,
    OrderStatus,
    PaymentMethod,
    UserRole,
    UserType,
)

# ---------------------------------------------------------------------------
# Program / rule factories
# ---------------------------------------------------------------------------


def get_or_create_default_program() -> LoyaltyProgram:
    """Return the active default LoyaltyProgram, creating one if absent."""
    program = LoyaltyProgram.query.filter_by(is_default=True, is_active=True).first()
    if not program:
        program = LoyaltyProgram(
            name="Default Program",
            is_active=True,
            is_default=True,
            uzs_per_point=250,
            points_expiry_days=365,
        )
        db.session.add(program)
        db.session.commit()
    return program


def make_strike_rule(
    program: LoyaltyProgram,
    name: str = "3 in 30",
    required_orders: int = 3,
    window_days: int = 30,
    bonus_points: int = 100,
    min_order_amount: Optional[Decimal] = None,
    is_active: bool = True,
    starts_at: Optional[datetime] = None,
    ends_at: Optional[datetime] = None,
) -> LoyaltyStreakRule:
    """Create and persist a LoyaltyStreakRule with sensible defaults."""
    rule = LoyaltyStreakRule(
        program_id=program.id,
        name=name,
        required_orders=required_orders,
        window_days=window_days,
        bonus_points=bonus_points,
        min_order_amount=min_order_amount,
        is_active=is_active,
        starts_at=starts_at,
        ends_at=ends_at,
        display_order=0,
    )
    db.session.add(rule)
    db.session.commit()
    return rule


def make_consecutive_rule(
    program: LoyaltyProgram,
    strikes: List[LoyaltyStreakRule],
    name: str = "Champion",
    required_consecutive: int = 6,
    combine_mode: str = "all",
    bonus_points: int = 1000,
    is_active: bool = True,
    starts_at: Optional[datetime] = None,
    ends_at: Optional[datetime] = None,
) -> LoyaltyConsecutiveStrikeRule:
    """Create and persist a LoyaltyConsecutiveStrikeRule with the given strike rules attached."""
    rule = LoyaltyConsecutiveStrikeRule(
        program_id=program.id,
        name=name,
        required_consecutive=required_consecutive,
        combine_mode=combine_mode,
        bonus_points=bonus_points,
        is_active=is_active,
        starts_at=starts_at,
        ends_at=ends_at,
        display_order=0,
    )
    rule.strikes = list(strikes)
    db.session.add(rule)
    db.session.commit()
    return rule


# ---------------------------------------------------------------------------
# Backdated ledger helpers
# ---------------------------------------------------------------------------


def seed_strike_achievement(
    user_id: int,
    strike_rule: LoyaltyStreakRule,
    when: datetime,
) -> LoyaltyTransaction:
    """Insert a single backdated STREAK_BONUS ledger row for one order-strike.

    Mirrors exactly how ``update_streak`` records an achievement, so
    ``_strike_consecutive_run`` and ``_strike_achievement_times`` count it.
    ``when`` should be a UTC-aware datetime.
    """
    txn = LoyaltyTransaction(
        user_id=user_id,
        transaction_type=LoyaltyTransactionType.EARNED,
        points=strike_rule.bonus_points,
        description=strike_rule.name,
        remaining_points=strike_rule.bonus_points,
        extra_data={
            "action_type": LoyaltyActionType.STREAK_BONUS.value,
            "streak_rule_id": strike_rule.id,
        },
    )
    db.session.add(txn)
    db.session.flush()
    # Override the auto-set created_at so the timing logic sees the back-date.
    txn.created_at = when
    db.session.commit()
    return txn


def seed_consecutive_run(
    user_id: int,
    strike_rule: LoyaltyStreakRule,
    count: int,
    now: Optional[datetime] = None,
    spacing_days: Optional[int] = None,
) -> List[LoyaltyTransaction]:
    """Seed ``count`` consecutive strike achievements ending near ``now``.

    Achievements are spaced ``spacing_days`` apart (default: ``window_days``
    which ensures gap < 2 * window_days, i.e. all consecutive).  The most
    recent achievement is placed at ``now - spacing_days``.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    if spacing_days is None:
        spacing_days = strike_rule.window_days

    txns: List[LoyaltyTransaction] = []
    for k in range(count):
        # k=0 → oldest; k=count-1 → most recent (spacing_days ago)
        days_ago = spacing_days * (count - k)
        when = now - timedelta(days=days_ago)
        txns.append(seed_strike_achievement(user_id, strike_rule, when))
    return txns


# ---------------------------------------------------------------------------
# Ledger query helpers
# ---------------------------------------------------------------------------


def consecutive_awards(user_id: int, rule_id: int) -> List[LoyaltyTransaction]:
    """All CONSECUTIVE_STREAK_BONUS ledger rows for a given rule and user."""
    result: List[LoyaltyTransaction] = []
    for txn in LoyaltyTransaction.query.filter_by(user_id=user_id).all():
        ed = txn.extra_data or {}
        if (
            ed.get("action_type") == LoyaltyActionType.CONSECUTIVE_STREAK_BONUS.value
            and ed.get("consecutive_strike_rule_id") == rule_id
        ):
            result.append(txn)
    return result


def consecutive_award_total(user_id: int, rule_id: int) -> int:
    """Sum of points from CONSECUTIVE_STREAK_BONUS rows for a given rule."""
    return sum(txn.points for txn in consecutive_awards(user_id, rule_id))


def strike_achievement_count(user_id: int, strike_rule_id: int) -> int:
    """Count of streak-bonus ledger rows for a given strike rule."""
    count = 0
    for txn in LoyaltyTransaction.query.filter_by(user_id=user_id).all():
        ed = txn.extra_data or {}
        if (
            ed.get("action_type") == LoyaltyActionType.STREAK_BONUS.value
            and ed.get("streak_rule_id") == strike_rule_id
        ):
            count += 1
    return count


# ---------------------------------------------------------------------------
# Order helpers (purchase + delivery trigger)
# ---------------------------------------------------------------------------


def _make_inventory_service(product, quantity: int = 2):
    """Return a mock InventoryService pre-loaded with availability for product."""
    from unittest.mock import MagicMock

    mock_inv = MagicMock()
    mock_inv.check_multiple_products_availability.return_value = [
        SimpleNamespace(
            product_id=product.id,
            requested_quantity=quantity,
            available_quantity=100,
            reserved_quantity=0,
            is_available=True,
            reason="Available",
        )
    ]
    mock_inv.reserve_inventory.return_value = {"success": True, "expires_at": None}
    mock_inv.release_reservations.return_value = {"success": True}
    return mock_inv


def _ensure_delivery_address(user_id: int) -> UserAddress:
    """Return (or create) a delivery address for the user."""
    addr = UserAddress.query.filter_by(user_id=user_id, is_default=True).first()
    if addr:
        return addr
    addr = UserAddress(
        user_id=user_id,
        title="Home",
        full_address="Test Street 1",
        street_address="Test Street 1",
        city="Tashkent",
        latitude=41.31,
        longitude=69.28,
        is_default=True,
    )
    db.session.add(addr)
    db.session.commit()
    return addr


def deliver_paid_order(
    order_service: OrderService,
    user_id: int,
    total: Decimal,
    when: Optional[datetime] = None,
    payment: str = "prepaid",
    product=None,
):
    """Build an Order, mark it paid, and fire the DELIVERED status edge.

    This drives the REAL award trigger path:
    ``OrderService._handle_status_change_actions(order, OrderStatus.DELIVERED)``
    → ``maybe_award_purchase_points`` + ``update_streak``
    → ``update_consecutive_strikes``.

    Parameters
    ----------
    order_service:
        An ``OrderService`` instance (inventory side-effects should be patched
        externally via ``mock_inventory_service`` fixture or inline mocking).
    user_id:
        The user who places the order.
    total:
        The order total in UZS as a Decimal.
    when:
        UTC datetime to use as ``created_at`` / ``delivered_at`` (defaults to now).
    payment:
        ``"prepaid"`` sets ``is_paid=True`` before delivery; ``"cod"`` leaves it
        False (delivery edge alone won't award for COD — you'd need a cash-collection
        call to complete the flow).
    product:
        A product to attach as an OrderItem.  When omitted the order is created
        directly without ``OrderService.create_order`` (no inventory reservation).

    .. note::
        The ``product is not None`` path routes through
        ``OrderService.create_order`` → ``_generate_order_number`` which executes
        a Postgres ``NOW()`` and therefore FAILS under sqlite.  The direct-Order
        path (``product=None``) — and ``seed_delivered_orders`` below — are how
        tests must drive real deliveries on a sqlite test DB.
    """
    now = when or datetime.now(timezone.utc)

    if product is not None:
        # Use real OrderService.create_order with corporate/payment side-effects patched
        addr = _ensure_delivery_address(user_id)
        mock_inv = _make_inventory_service(product)
        # Re-configure the passed order_service's inventory mock if present,
        # otherwise create a standalone service.
        if hasattr(order_service, "inventory_service"):
            order_service.inventory_service.check_multiple_products_availability.return_value = [
                SimpleNamespace(
                    product_id=product.id,
                    requested_quantity=2,
                    available_quantity=100,
                    reserved_quantity=0,
                    is_available=True,
                    reason="Available",
                )
            ]

        order_data = {
            "items": [{"product_id": product.id, "quantity": 2}],
            "delivery_address": {
                "delivery_address_id": addr.id,
                "street": addr.street_address,
                "latitude": addr.latitude,
                "longitude": addr.longitude,
            },
            "payment_method": "click" if payment == "prepaid" else "cash",
        }
        with patch(
            "business_app.services.corporate_contract_service.CorporateContractService.reserve_for_order",
            return_value=None,
        ), patch(
            "business_app.services.payment_service.PaymentService.initialize_order_payment",
            return_value=None,
        ):
            order = order_service.create_order(user_id, order_data)
    else:
        # Build an order row directly (no inventory machinery needed).
        order = Order(
            user_id=user_id,
            order_number=f"CSH-{uuid.uuid4().hex[:8].upper()}",
            status=OrderStatus.PENDING,
            subtotal=Decimal(str(total)),
            total_amount=Decimal(str(total)),
            payment_method=PaymentMethod.CLICK if payment == "prepaid" else PaymentMethod.CASH,
        )
        db.session.add(order)
        db.session.flush()
        # Attach one OrderItem so the purchase-points calculator sees eligible items.
        db.session.add(
            OrderItem(
                order_id=order.id,
                product_id=1,  # placeholder — product_id 1 may not exist but the amount calc uses total_amount
                quantity=1,
                unit_price=Decimal(str(total)),
                total_price=Decimal(str(total)),
            )
        )
        db.session.commit()

    # Backdate created_at so any trailing-window calculations see the right date.
    order.created_at = now
    db.session.commit()

    if payment == "prepaid":
        order.is_paid = True
        order.paid_at = now
        db.session.commit()

    # Fire the DELIVERED edge (the real award trigger).
    order.status = OrderStatus.DELIVERED
    order.delivered_at = now
    db.session.commit()
    order_service._handle_status_change_actions(order, OrderStatus.DELIVERED, commit=True)
    return order


def seed_delivered_orders(
    user_id: int,
    count: int,
    total: Decimal = Decimal("50000"),
    newest_days_ago: int = 0,
    spacing_days: int = 10,
) -> List[Order]:
    """Seed ``count`` DELIVERED + paid orders backdated into a strike window.

    Each row is a real ``Order`` (status=DELIVERED, is_paid=True,
    total_amount=total) constructed directly — exactly like
    ``deliver_paid_order(product=None)`` — so it satisfies ``update_streak``'s
    ``_qualifying_order_count`` (delivered, inside the trailing window, total ≥
    ``min_order_amount``).

    The newest order is dated ``now - newest_days_ago`` days; each older one is a
    further ``spacing_days`` back (so ``count`` orders span
    ``newest_days_ago + (count-1)*spacing_days`` days — keep that inside the
    rule's ``window_days``).

    .. note::
        Built directly rather than via ``OrderService.create_order`` because that
        path runs ``_generate_order_number`` → Postgres ``NOW()`` and FAILS under
        sqlite (see ``deliver_paid_order``).

    Returns the list of created orders (newest first).
    """
    now = datetime.now(timezone.utc)
    orders: List[Order] = []
    for k in range(count):
        when = now - timedelta(days=newest_days_ago + k * spacing_days)
        order = Order(
            user_id=user_id,
            order_number=f"SDO-{uuid.uuid4().hex[:8].upper()}",
            status=OrderStatus.DELIVERED,
            subtotal=Decimal(str(total)),
            total_amount=Decimal(str(total)),
            payment_method=PaymentMethod.CLICK,
            is_paid=True,
        )
        db.session.add(order)
        db.session.flush()
        # Override the auto-set created_at so window calculations see the back-date.
        order.created_at = when
        order.delivered_at = when
        order.paid_at = when
        orders.append(order)
    db.session.commit()
    return orders


# ---------------------------------------------------------------------------
# Notification silencer
# ---------------------------------------------------------------------------


def silence_loyalty_notifications(monkeypatch) -> None:
    """Monkeypatch all LoyaltyService notification side-effects to no-ops.

    Call this in an ``autouse=True`` fixture inside each test module::

        @pytest.fixture(autouse=True)
        def _silence(monkeypatch):
            silence_loyalty_notifications(monkeypatch)
    """
    # Signature-enforcing spies, not no-ops — see the conftest docstring: a
    # ``lambda *a, **k: None`` stub swallows a drifted payload silently.
    from tests.conftest import install_loyalty_notification_spies

    return install_loyalty_notification_spies(monkeypatch)


# ---------------------------------------------------------------------------
# Entity user builder
# ---------------------------------------------------------------------------


def build_entity_user(loyalty_eligible: bool = True) -> User:
    """Create an entity (corporate) user with or without a loyalty-eligible contract.

    ``LoyaltyService.is_user_loyalty_eligible`` returns True for an entity only
    when they have at least one contract that is ``is_currently_active`` AND
    ``is_loyalty_points_eligible``.  Setting ``loyalty_eligible=False`` creates
    the user without any such contract (no contracts at all).
    """
    uid = uuid.uuid4().hex[:8]
    user = User(
        email=f"entity_{uid}@example.com",
        phone=f"+998{uid[:9]}",
        password_hash=hash_password("TestPassword123!"),
        first_name="Entity",
        last_name="Corp",
        user_type=UserType.ENTITY,
        entity_subtype=EntitySubtype.WORKPLACE,
        role=UserRole.CUSTOMER,
        is_verified=True,
        company_name=f"Corp {uid}",
    )
    db.session.add(user)
    db.session.flush()

    if loyalty_eligible:
        contract = CorporateContract(
            user_id=user.id,
            contract_number=f"CC-{uid}",
            name=f"Contract for Corp {uid}",
            status=CorporateContractStatus.ACTIVE,
            is_active=True,
            is_loyalty_points_eligible=True,
            start_date=datetime.now(timezone.utc) - timedelta(days=30),
            tracking_mode=CorporateContractTrackingMode.UNITS,
        )
        db.session.add(contract)

    db.session.commit()
    return user
