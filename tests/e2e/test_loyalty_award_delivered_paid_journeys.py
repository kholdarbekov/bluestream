"""End-to-end journeys for the AquaCoins purchase-accrual rule.

Business rule (owner-confirmed 2026-06-20): purchase AquaCoins
(1 per ``uzs_per_point`` UZS, default 250) are awarded **only when an order is
both DELIVERED and fully paid**, exactly once. These journeys drive the REAL
award triggers — the ``_handle_status_change_actions`` status edges, the COD
cash-collection entry point (``CashCollectionService.post_collection``), and the
prepaid payment-success handler (``PaymentService._handle_successful_payment``) —
and assert on the REAL ledger (no award is stubbed). This mirrors the
delivery-trigger style already used by tests/integration/test_loyalty_streak_rules.py
(call ``_handle_status_change_actions`` directly with ``new_status=DELIVERED``
rather than the full ``update_order_status`` pipeline, which needs driver
assignment / fiscalization / marking-codes machinery unrelated to loyalty).

Coverage:
- Prepaid (Click): paid-then-delivered awards; merely-CONFIRMED never awards
  (the original bug, incl. admin/manual confirm with no payment); paid-but-not-
  delivered never awards; delivered-but-unpaid never awards; paid-after-delivery
  awards via the payment edge; paid-but-cancelled never awards.
- COD (cash): delivered-but-uncollected never awards; full collection awards;
  partial-then-full awards once; collected the next day still awards.
- Idempotency: re-firing the delivery edge / collecting again never double-awards.
- Amount/eligibility: sub-one-coin earns 0; an order with no eligible items earns
  0; the awarded amount equals floor(eligible / uzs_per_point).
- Strict guard: a fully-paid order at every pre-delivery status earns nothing.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from business_app import db as _db
from business_app.models.delivery import Delivery, DeliveryPerson
from business_app.models.loyalty import LoyaltyProgram, LoyaltyTierConfig, LoyaltyTransaction
from business_app.models.order import Order, OrderItem
from business_app.models.payment import Payment
from business_app.models.user import UserAddress
from business_app.services.cash_collection_service import CashCollectionService
from business_app.services.loyalty_service import LoyaltyService
from business_app.services.order_service import OrderService
from business_app.services.payment_service import PaymentService
from business_app.utils.constants import LoyaltyTransactionType
from shared.enums import DeliveryStatus, OrderStatus, PaymentMethod, PaymentStatus


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _silence_loyalty_notifications(loyalty_notification_spy):
    """Signature-enforcing spies rather than no-ops.

    A ``lambda *a, **k: None`` stub accepts ANY call, so a sender whose
    payload or signature drifts keeps every test green — that is how the
    tier-upgrade notification shipped rendering the wrong template. The
    shared fixture binds each call against the real signature instead.
    """
    return loyalty_notification_spy


@pytest.fixture
def loyalty_program(db):
    program = LoyaltyProgram(
        name="Default", is_active=True, is_default=True, uzs_per_point=250, points_expiry_days=365
    )
    db.session.add(program)
    db.session.commit()
    return program


@pytest.fixture
def bronze_tier(db, loyalty_program):
    """Pin the multiplier to 1.0 so points == floor(eligible / 250)."""
    tier = LoyaltyTierConfig(
        program_id=loyalty_program.id, name="Bronze", display_order=0,
        min_points=0, max_points=None, points_multiplier=1.0, is_active=True,
    )
    db.session.add(tier)
    db.session.commit()
    return tier


@pytest.fixture
def delivery_address(db, sample_user):
    address = UserAddress(
        user_id=sample_user.id, title="Home", full_address="Home Street 1",
        street_address="Home Street 1", city="Tashkent",
        latitude=41.31, longitude=69.28, is_default=True,
    )
    db.session.add(address)
    db.session.commit()
    return address


@pytest.fixture
def delivery_driver_profile(db, delivery_driver):
    profile = DeliveryPerson(
        user_id=delivery_driver.id, full_name="Delivery Driver",
        phone=delivery_driver.phone, email=delivery_driver.email,
        is_active=True, is_available=True,
    )
    db.session.add(profile)
    db.session.commit()
    return profile


@pytest.fixture
def order_service(mock_inventory_service):
    mock_inventory_service.check_multiple_products_availability.return_value = [
        SimpleNamespace(
            product_id=0, requested_quantity=2, available_quantity=100,
            reserved_quantity=0, is_available=True, reason="Available",
        )
    ]
    mock_inventory_service.reserve_inventory.return_value = {"success": True, "expires_at": None}
    mock_inventory_service.release_reservations.return_value = {"success": True}
    return OrderService(inventory_service=mock_inventory_service)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _availability(product, quantity=2):
    return SimpleNamespace(
        product_id=product.id, requested_quantity=quantity, available_quantity=100,
        reserved_quantity=0, is_available=True, reason="Available",
    )


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


def _create_order(order_service, product, user_id, address, *, payment_method="click"):
    """Real OrderService.create_order (PENDING, with items), payment/corporate
    side effects patched out — the loyalty path itself runs for real."""
    order_service.inventory_service.check_multiple_products_availability.return_value = [
        _availability(product)
    ]
    with patch(
        "business_app.services.corporate_contract_service.CorporateContractService.reserve_for_order",
        return_value=None,
    ), patch(
        "business_app.services.payment_service.PaymentService.initialize_order_payment",
        return_value=None,
    ):
        return order_service.create_order(
            user_id, _order_data(product, address, payment_method=payment_method)
        )


def _purchase_txns(user_id):
    """The purchase-award ledger rows for a user (EARNED + order_id)."""
    return (
        LoyaltyTransaction.query.filter_by(
            user_id=user_id, transaction_type=LoyaltyTransactionType.EARNED
        )
        .filter(LoyaltyTransaction.order_id.isnot(None))
        .all()
    )


def _points(user_id):
    return LoyaltyService().get_available_points(user_id)


def _expected_points(order):
    """floor(eligible / 250) with Bronze multiplier 1.0; eligible == total_amount
    for a normal (non-contract) order with items."""
    return int(order.total_amount) // 250


def _mark_paid(order, *, when=None):
    order.is_paid = True
    order.paid_at = when or datetime.now(timezone.utc)
    _db.session.commit()


def _confirm_edge(order_service, order):
    order.status = OrderStatus.CONFIRMED
    _db.session.commit()
    order_service._handle_status_change_actions(order, OrderStatus.CONFIRMED, commit=True)


def _deliver_edge(order_service, order, *, when=None):
    order.status = OrderStatus.DELIVERED
    order.delivered_at = when or datetime.now(timezone.utc)
    _db.session.commit()
    order_service._handle_status_change_actions(order, OrderStatus.DELIVERED, commit=True)


def _make_delivered_delivery(order, driver, *, when=None):
    when = when or datetime.now(timezone.utc)
    deliv = Delivery(
        order_id=order.id, delivery_person_id=driver.id, status=DeliveryStatus.DELIVERED,
        scheduled_date=when, scheduled_time_slot="09:00-12:00",
        actual_delivery_time=when, delivered_at=when,
    )
    _db.session.add(deliv)
    _db.session.commit()
    return deliv


def _collect_cash(order, driver, amount, *, delivery, when=None):
    return CashCollectionService().post_collection(
        customer_id=order.user_id,
        amount=Decimal(str(amount)),
        source="delivery_completion",
        collector_user_id=driver.id,
        recorded_by_user_id=driver.id,
        order_id=order.id,
        delivery_id=delivery.id,
        notes="Driver collected cash on delivery",
        occurred_at=when,
    )


# --------------------------------------------------------------------------- #
# Prepaid (Click) journeys
# --------------------------------------------------------------------------- #


@pytest.mark.integration
@pytest.mark.order
class TestPrepaidJourneys:
    def test_paid_then_delivered_awards_once(
        self, app, db, sample_user, sample_product, delivery_address, loyalty_program, bronze_tier, order_service
    ):
        order = _create_order(order_service, sample_product, sample_user.id, delivery_address)
        _mark_paid(order)  # prepaid payment completed before delivery

        # Paid but not delivered yet -> nothing earned.
        assert _points(sample_user.id) == 0
        assert _purchase_txns(sample_user.id) == []

        # Delivered AND paid -> earned exactly once.
        _deliver_edge(order_service, order)
        assert _points(sample_user.id) == _expected_points(order)
        assert len(_purchase_txns(sample_user.id)) == 1

    def test_confirmed_unpaid_awards_nothing(
        self, app, db, sample_user, sample_product, delivery_address, loyalty_program, bronze_tier, order_service
    ):
        """The original bug: a non-cash order confirmed with no completed payment
        (e.g. admin manual/bulk confirm) must NOT earn AquaCoins."""
        order = _create_order(order_service, sample_product, sample_user.id, delivery_address)
        assert order.is_paid is False

        _confirm_edge(order_service, order)

        assert _points(sample_user.id) == 0
        assert _purchase_txns(sample_user.id) == []

    def test_delivered_but_unpaid_awards_nothing(
        self, app, db, sample_user, sample_product, delivery_address, loyalty_program, bronze_tier, order_service
    ):
        order = _create_order(order_service, sample_product, sample_user.id, delivery_address)
        assert order.is_paid is False

        _deliver_edge(order_service, order)

        assert _points(sample_user.id) == 0
        assert _purchase_txns(sample_user.id) == []

    def test_paid_after_delivery_awards(
        self, app, db, sample_user, sample_product, delivery_address, loyalty_program, bronze_tier, order_service
    ):
        """Order force-delivered while unpaid, then the prepaid payment completes
        — the payment edge awards retroactively."""
        order = _create_order(order_service, sample_product, sample_user.id, delivery_address)
        _deliver_edge(order_service, order)
        assert _points(sample_user.id) == 0

        payment = Payment(
            order_id=order.id, user_id=sample_user.id, payment_method=PaymentMethod.CLICK,
            amount=order.total_amount, currency="UZS", status=PaymentStatus.COMPLETED,
            amount_collected=order.total_amount, outstanding_amount=Decimal("0.00"),
            paid_at=datetime.now(timezone.utc),
        )
        db.session.add(payment)
        db.session.commit()

        PaymentService()._handle_successful_payment(payment, trigger_notifications=False)

        assert _points(sample_user.id) == _expected_points(order)
        assert len(_purchase_txns(sample_user.id)) == 1

    def test_paid_but_cancelled_awards_nothing(
        self, app, db, sample_user, sample_product, delivery_address, loyalty_program, bronze_tier, order_service
    ):
        order = _create_order(order_service, sample_product, sample_user.id, delivery_address)
        _mark_paid(order)
        _confirm_edge(order_service, order)

        # Cancelled before ever being delivered -> earns nothing.
        order.status = OrderStatus.CANCELLED
        db.session.commit()

        assert _points(sample_user.id) == 0
        assert _purchase_txns(sample_user.id) == []


# --------------------------------------------------------------------------- #
# COD (cash) journeys
# --------------------------------------------------------------------------- #


@pytest.mark.integration
@pytest.mark.order
class TestCodJourneys:
    def test_delivered_uncollected_awards_nothing(
        self, app, db, sample_user, sample_product, delivery_address, delivery_driver,
        delivery_driver_profile, loyalty_program, bronze_tier, order_service
    ):
        order = _create_order(order_service, sample_product, sample_user.id, delivery_address, payment_method="cash")
        _make_delivered_delivery(order, delivery_driver)

        _deliver_edge(order_service, order)  # delivered, but cash not yet collected

        assert order.is_paid is False
        assert _points(sample_user.id) == 0
        assert _purchase_txns(sample_user.id) == []

    def test_full_collection_awards(
        self, app, db, sample_user, sample_product, delivery_address, delivery_driver,
        delivery_driver_profile, loyalty_program, bronze_tier, order_service
    ):
        order = _create_order(order_service, sample_product, sample_user.id, delivery_address, payment_method="cash")
        delivery = _make_delivered_delivery(order, delivery_driver)
        _deliver_edge(order_service, order)
        assert _points(sample_user.id) == 0

        _collect_cash(order, delivery_driver, order.total_amount, delivery=delivery)

        db.session.refresh(order)
        assert order.is_paid is True
        assert _points(sample_user.id) == _expected_points(order)
        assert len(_purchase_txns(sample_user.id)) == 1

    def test_partial_then_full_collection_awards_once(
        self, app, db, sample_user, sample_product, delivery_address, delivery_driver,
        delivery_driver_profile, loyalty_program, bronze_tier, order_service
    ):
        order = _create_order(order_service, sample_product, sample_user.id, delivery_address, payment_method="cash")
        delivery = _make_delivered_delivery(order, delivery_driver)
        _deliver_edge(order_service, order)

        half = (order.total_amount / 2).quantize(Decimal("0.01"))
        _collect_cash(order, delivery_driver, half, delivery=delivery)
        db.session.refresh(order)
        assert order.is_paid is False
        assert _points(sample_user.id) == 0

        _collect_cash(order, delivery_driver, order.total_amount - half, delivery=delivery)
        db.session.refresh(order)
        assert order.is_paid is True
        assert _points(sample_user.id) == _expected_points(order)
        assert len(_purchase_txns(sample_user.id)) == 1

    def test_collected_next_day_still_awards(
        self, app, db, sample_user, sample_product, delivery_address, delivery_driver,
        delivery_driver_profile, loyalty_program, bronze_tier, order_service
    ):
        """Decision 2026-06-20: award whenever the order becomes fully paid, even
        if the cash is collected on a later day."""
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)
        order = _create_order(order_service, sample_product, sample_user.id, delivery_address, payment_method="cash")
        delivery = _make_delivered_delivery(order, delivery_driver, when=yesterday)
        _deliver_edge(order_service, order, when=yesterday)
        assert _points(sample_user.id) == 0

        _collect_cash(order, delivery_driver, order.total_amount, delivery=delivery)

        db.session.refresh(order)
        assert order.is_paid is True
        assert _points(sample_user.id) == _expected_points(order)
        assert len(_purchase_txns(sample_user.id)) == 1


# --------------------------------------------------------------------------- #
# Idempotency
# --------------------------------------------------------------------------- #


@pytest.mark.integration
@pytest.mark.order
class TestIdempotency:
    def test_redelivery_edge_does_not_double_award(
        self, app, db, sample_user, sample_product, delivery_address, loyalty_program, bronze_tier, order_service
    ):
        order = _create_order(order_service, sample_product, sample_user.id, delivery_address)
        _mark_paid(order)
        _deliver_edge(order_service, order)
        assert len(_purchase_txns(sample_user.id)) == 1
        first = _points(sample_user.id)

        # Re-firing the delivery edge must not award a second time.
        _deliver_edge(order_service, order)

        assert _points(sample_user.id) == first
        assert len(_purchase_txns(sample_user.id)) == 1

    def test_cod_collection_then_delivery_edge_no_double_award(
        self, app, db, sample_user, sample_product, delivery_address, delivery_driver,
        delivery_driver_profile, loyalty_program, bronze_tier, order_service
    ):
        order = _create_order(order_service, sample_product, sample_user.id, delivery_address, payment_method="cash")
        delivery = _make_delivered_delivery(order, delivery_driver)
        _deliver_edge(order_service, order)
        _collect_cash(order, delivery_driver, order.total_amount, delivery=delivery)
        assert len(_purchase_txns(sample_user.id)) == 1

        # Now both conditions are true; re-firing the delivery edge is a no-op.
        _deliver_edge(order_service, order)

        assert len(_purchase_txns(sample_user.id)) == 1
        assert _points(sample_user.id) == _expected_points(order)


# --------------------------------------------------------------------------- #
# Amount / eligibility edge cases
# --------------------------------------------------------------------------- #


@pytest.mark.integration
@pytest.mark.order
class TestAmountAndEligibility:
    def _direct_order(self, db, user_id, product, *, total, with_item=True):
        order = Order(
            user_id=user_id, order_number=f"E2E-{user_id}-{int(total)}",
            status=OrderStatus.PENDING, subtotal=Decimal(str(total)),
            total_amount=Decimal(str(total)), payment_method=PaymentMethod.CLICK,
        )
        db.session.add(order)
        db.session.flush()
        if with_item:
            db.session.add(
                OrderItem(
                    order_id=order.id, product_id=product.id, quantity=1,
                    unit_price=Decimal(str(total)), total_price=Decimal(str(total)),
                )
            )
        db.session.commit()
        return order

    def test_amount_below_one_coin_awards_zero(
        self, app, db, sample_user, sample_product, loyalty_program, bronze_tier, order_service
    ):
        order = self._direct_order(db, sample_user.id, sample_product, total=200)  # < 250 UZS
        _mark_paid(order)

        _deliver_edge(order_service, order)

        assert _points(sample_user.id) == 0
        assert _purchase_txns(sample_user.id) == []

    def test_order_without_eligible_items_awards_zero(
        self, app, db, sample_user, sample_product, loyalty_program, bronze_tier, order_service
    ):
        """No order items => 0 eligible amount => no award even when delivered+paid."""
        order = self._direct_order(db, sample_user.id, sample_product, total=30000, with_item=False)
        _mark_paid(order)

        _deliver_edge(order_service, order)

        assert _points(sample_user.id) == 0
        assert _purchase_txns(sample_user.id) == []

    def test_award_amount_matches_floor_division(
        self, app, db, sample_user, sample_product, loyalty_program, bronze_tier, order_service
    ):
        order = self._direct_order(db, sample_user.id, sample_product, total=31200)  # 31200/250 = 124.8
        _mark_paid(order)

        _deliver_edge(order_service, order)

        assert _points(sample_user.id) == 124  # floor(31200 / 250)
        assert len(_purchase_txns(sample_user.id)) == 1


# --------------------------------------------------------------------------- #
# Strict status guard
# --------------------------------------------------------------------------- #


@pytest.mark.integration
@pytest.mark.order
class TestStrictStatusGuard:
    @pytest.mark.parametrize(
        "status",
        [OrderStatus.PENDING, OrderStatus.CONFIRMED, OrderStatus.PREPARING, OrderStatus.OUT_FOR_DELIVERY],
    )
    def test_paid_order_before_delivery_awards_nothing(
        self, app, db, sample_user, sample_product, delivery_address,
        loyalty_program, bronze_tier, order_service, status
    ):
        """A fully-paid order at any pre-delivery status earns nothing — the
        guard requires DELIVERED, not merely paid."""
        order = _create_order(order_service, sample_product, sample_user.id, delivery_address)
        _mark_paid(order)
        order.status = status
        db.session.commit()

        # The guard is the single chokepoint every trigger routes through.
        order_service.maybe_award_purchase_points(order, commit=True)

        assert _points(sample_user.id) == 0
        assert _purchase_txns(sample_user.id) == []
