"""Admin bulk 'confirm' / 'mark_delivered' must delegate to the SSOT
order/delivery services instead of mutating `.status` directly.

Regression coverage for the bug where `_bulk_action_orders` and
`_bulk_action_deliveries` set `order.status` / `delivery.status` and committed
directly, bypassing `OrderService.update_order_status` /
`DeliveryService.complete_delivery`. That skipped every side-effect those
services fire (loyalty AquaCoins award, inventory deduction, delivery sync,
`OrderStatusHistory`) and wrote to `confirmed_at` / `delivered_at` attributes
that do not exist as columns on `Order` (silently discarded).

The old guard also hard-coded `[PREPARING, OUT_FOR_DELIVERY]` as valid
predecessors for `mark_delivered`, which diverged from the real SSOT transition
table in `shared/status_transitions.py` (PREPARING -> DELIVERED is invalid;
only CONFIRMED/OUT_FOR_DELIVERY -> DELIVERED are). These tests pin the
corrected, SSOT-aligned behaviour.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from business_app.models.delivery import Delivery, DeliveryPerson
from business_app.models.order import Order, OrderItem, OrderStatusHistory
from business_app.models.loyalty import LoyaltyTransaction
from business_app.models.user import User, UserAddress
from business_app.services.admin_bulk_action_service import AdminBulkActionService
from business_app.utils.constants import LoyaltyTransactionType
from business_app.utils.password_security import hash_password
from shared.enums import DeliveryStatus, OrderStatus, PaymentMethod, UserRole, UserType


def _address(db, user):
    address = UserAddress(
        user_id=user.id,
        title="Home",
        full_address="Street 1",
        street_address="Street 1",
        city="Tashkent",
        latitude=41.31,
        longitude=69.28,
        is_default=True,
    )
    db.session.add(address)
    db.session.commit()
    return address


def _order(
    db,
    user,
    address,
    status,
    number,
    *,
    payment_method=PaymentMethod.CLICK,
    is_paid=False,
    with_item=None,
):
    order = Order(
        order_number=number,
        user_id=user.id,
        status=status,
        subtotal=Decimal("15000.00"),
        delivery_fee=Decimal("3000.00"),
        total_amount=Decimal("18000.00"),
        delivery_address_id=address.id,
        payment_method=payment_method,
        is_paid=is_paid,
        order_source="web",
        created_at=datetime.now(UTC),
    )
    db.session.add(order)
    db.session.commit()

    if with_item is not None:
        db.session.add(
            OrderItem(
                order_id=order.id,
                product_id=with_item.id,
                quantity=1,
                unit_price=Decimal("15000.00"),
                total_price=Decimal("15000.00"),
            )
        )
        db.session.commit()

    return order


def _driver(db, phone):
    user = User(
        phone=phone,
        first_name="D",
        last_name="R",
        password_hash=hash_password("TestPassword123!"),
        role=UserRole.DELIVERY_DRIVER,
        user_type=UserType.STAFF,
        is_verified=True,
        created_at=datetime.now(UTC),
    )
    db.session.add(user)
    db.session.flush()
    db.session.add(
        DeliveryPerson(
            user_id=user.id,
            full_name="D R",
            phone=phone,
            is_active=True,
            is_available=True,
            working_hours_start="00:00",
            working_hours_end="23:59",
        )
    )
    db.session.commit()
    return user


@pytest.mark.unit
class TestBulkConfirmDelegatesToOrderService:
    def test_confirm_pending_order_creates_history_row(self, db, sample_user, admin_user):
        """Bulk confirm on a PENDING order must succeed via OrderService and
        leave a real OrderStatusHistory row (the old direct-mutation path wrote
        none)."""
        address = _address(db, sample_user)
        order = _order(
            db,
            sample_user,
            address,
            OrderStatus.PENDING,
            "ORD-BULK-CONFIRM-1",
            payment_method=PaymentMethod.CASH,
        )

        result = AdminBulkActionService.perform(
            action="confirm",
            target_type="order",
            target_ids=[order.id],
            parameters={},
            reason="bulk confirm",
            admin_id=admin_user.id,
        )

        assert result == {"success_count": 1, "failed_count": 0, "errors": [], "total_errors": 0}

        db.session.refresh(order)
        assert order.status == OrderStatus.CONFIRMED

        history = OrderStatusHistory.query.filter_by(
            order_id=order.id, new_status=OrderStatus.CONFIRMED
        ).first()
        assert history is not None
        assert history.old_status == OrderStatus.PENDING
        assert history.changed_by == admin_user.id
        assert history.notes == "bulk confirm"

    def test_confirm_rejects_non_pending_order(self, db, sample_user, admin_user):
        """A CONFIRMED (or later) order is not a valid predecessor for confirm."""
        address = _address(db, sample_user)
        order = _order(
            db,
            sample_user,
            address,
            OrderStatus.CONFIRMED,
            "ORD-BULK-CONFIRM-REJECT",
            payment_method=PaymentMethod.CASH,
        )

        result = AdminBulkActionService.perform(
            action="confirm",
            target_type="order",
            target_ids=[order.id],
            parameters={},
            reason="bulk confirm",
            admin_id=admin_user.id,
        )

        assert result["success_count"] == 0
        assert result["failed_count"] == 1
        assert result["errors"][0]["order_id"] == order.id
        db.session.refresh(order)
        assert order.status == OrderStatus.CONFIRMED


@pytest.mark.unit
class TestBulkMarkDeliveredAwardsLoyalty:
    def test_mark_delivered_awards_aquacoins_for_paid_eligible_order(
        self, db, sample_user, sample_product, admin_user
    ):
        """A paid, loyalty-eligible customer's OUT_FOR_DELIVERY order bulk
        marked delivered must earn real AquaCoins (LoyaltyTransaction row) —
        the direct-mutation path never invoked OrderService, so it never did."""
        address = _address(db, sample_user)
        order = _order(
            db,
            sample_user,
            address,
            OrderStatus.OUT_FOR_DELIVERY,
            "ORD-BULK-DELIVER-1",
            payment_method=PaymentMethod.CLICK,
            is_paid=True,
            with_item=sample_product,
        )

        result = AdminBulkActionService.perform(
            action="mark_delivered",
            target_type="order",
            target_ids=[order.id],
            parameters={},
            reason="bulk deliver",
            admin_id=admin_user.id,
        )

        assert result["success_count"] == 1
        assert result["failed_count"] == 0

        db.session.refresh(order)
        assert order.status == OrderStatus.DELIVERED

        award = LoyaltyTransaction.query.filter_by(
            order_id=order.id, transaction_type=LoyaltyTransactionType.EARNED
        ).first()
        assert award is not None
        assert award.points > 0

        history = OrderStatusHistory.query.filter_by(
            order_id=order.id, new_status=OrderStatus.DELIVERED
        ).first()
        assert history is not None
        assert history.old_status == OrderStatus.OUT_FOR_DELIVERY


@pytest.mark.unit
class TestBulkMarkDeliveredMixedBatch:
    def test_mixed_batch_reports_success_and_failure_per_item(
        self, db, sample_user, sample_product, admin_user
    ):
        """One invalid (PENDING) + one valid (OUT_FOR_DELIVERY) order in the
        same batch: the invalid one is skipped and reported, the valid one
        still succeeds — per-item isolation must be preserved."""
        address = _address(db, sample_user)
        invalid_order = _order(
            db,
            sample_user,
            address,
            OrderStatus.PENDING,
            "ORD-BULK-MIXED-PENDING",
            payment_method=PaymentMethod.CLICK,
        )
        valid_order = _order(
            db,
            sample_user,
            address,
            OrderStatus.OUT_FOR_DELIVERY,
            "ORD-BULK-MIXED-VALID",
            payment_method=PaymentMethod.CLICK,
            is_paid=True,
            with_item=sample_product,
        )

        result = AdminBulkActionService.perform(
            action="mark_delivered",
            target_type="order",
            target_ids=[invalid_order.id, valid_order.id],
            parameters={},
            reason="bulk deliver",
            admin_id=admin_user.id,
        )

        assert result["success_count"] == 1
        assert result["failed_count"] == 1
        assert result["total_errors"] == 1
        assert result["errors"][0]["order_id"] == invalid_order.id

        db.session.refresh(invalid_order)
        db.session.refresh(valid_order)
        assert invalid_order.status == OrderStatus.PENDING
        assert valid_order.status == OrderStatus.DELIVERED


@pytest.mark.unit
class TestBulkMarkDeliveredRejectsPreparing:
    def test_preparing_order_cannot_be_bulk_marked_delivered(self, db, sample_user, admin_user):
        """SSOT-alignment regression: PREPARING -> DELIVERED is NOT a valid
        transition (must go through OUT_FOR_DELIVERY first). The old hard-coded
        guard `[PREPARING, OUT_FOR_DELIVERY]` wrongly allowed this shortcut."""
        address = _address(db, sample_user)
        order = _order(
            db,
            sample_user,
            address,
            OrderStatus.PREPARING,
            "ORD-BULK-PREPARING",
            payment_method=PaymentMethod.CLICK,
        )

        result = AdminBulkActionService.perform(
            action="mark_delivered",
            target_type="order",
            target_ids=[order.id],
            parameters={},
            reason="bulk deliver",
            admin_id=admin_user.id,
        )

        assert result["success_count"] == 0
        assert result["failed_count"] == 1
        assert result["errors"][0]["order_id"] == order.id
        db.session.refresh(order)
        assert order.status == OrderStatus.PREPARING


@pytest.mark.unit
class TestBulkActionDeliveriesMarkDelivered:
    def test_mark_delivered_syncs_linked_order_status(
        self, app, db, sample_user, sample_product, admin_user
    ):
        """`_bulk_action_deliveries` mark_delivered must delegate to
        DeliveryService.complete_delivery so the linked Order is synced to
        DELIVERED too (no Order/Delivery desync)."""
        address = _address(db, sample_user)
        driver = _driver(db, "+998901900001")
        order = _order(
            db,
            sample_user,
            address,
            OrderStatus.OUT_FOR_DELIVERY,
            "ORD-BULK-DELIVERY-SYNC",
            payment_method=PaymentMethod.CLICK,
            is_paid=True,
            with_item=sample_product,
        )
        delivery = Delivery(
            order_id=order.id,
            status=DeliveryStatus.SCHEDULED,
            delivery_person_id=driver.id,
            scheduled_date=datetime.now(UTC),
            scheduled_time_slot="09:00-12:00",
        )
        db.session.add(delivery)
        db.session.commit()

        result = AdminBulkActionService.perform(
            action="mark_delivered",
            target_type="delivery",
            target_ids=[delivery.id],
            parameters={},
            reason="bulk deliver",
            admin_id=admin_user.id,
        )

        assert result["success_count"] == 1
        assert result["failed_count"] == 0

        db.session.refresh(delivery)
        db.session.refresh(order)
        assert delivery.status == DeliveryStatus.DELIVERED
        assert order.status == OrderStatus.DELIVERED

    def test_mark_delivered_attributes_history_to_admin_not_driver(
        self, app, db, sample_user, sample_product, admin_user
    ):
        """The OrderStatusHistory row created for the DELIVERED transition must
        record the acting admin as `changed_by` — not the delivering driver.
        The old direct `DeliveryService().complete_delivery(sync_order_status=True)`
        call routed the sync through `_handle_delivery_status_change`, which
        always attributes the change to `delivery.delivery_person_id`, silently
        dropping admin attribution for a bulk action an admin explicitly took."""
        address = _address(db, sample_user)
        driver = _driver(db, "+998901900002")
        order = _order(
            db,
            sample_user,
            address,
            OrderStatus.OUT_FOR_DELIVERY,
            "ORD-BULK-DELIVERY-ATTRIBUTION",
            payment_method=PaymentMethod.CLICK,
            is_paid=True,
            with_item=sample_product,
        )
        delivery = Delivery(
            order_id=order.id,
            status=DeliveryStatus.SCHEDULED,
            delivery_person_id=driver.id,
            scheduled_date=datetime.now(UTC),
            scheduled_time_slot="09:00-12:00",
        )
        db.session.add(delivery)
        db.session.commit()

        result = AdminBulkActionService.perform(
            action="mark_delivered",
            target_type="delivery",
            target_ids=[delivery.id],
            parameters={},
            reason="bulk deliver",
            admin_id=admin_user.id,
        )

        assert result["success_count"] == 1
        assert result["failed_count"] == 0

        history = OrderStatusHistory.query.filter_by(
            order_id=order.id, new_status=OrderStatus.DELIVERED
        ).first()
        assert history is not None
        assert history.changed_by == admin_user.id
        assert history.changed_by != driver.id


@pytest.mark.unit
class TestBulkActionDeliveriesMarkDeliveredAtomicity:
    def test_invalid_order_transition_does_not_leave_delivery_committed_as_delivered(
        self, app, db, sample_user, admin_user
    ):
        """Critical atomicity regression (C1): if the delivery's linked ORDER
        is in a state from which DELIVERED is an invalid transition (e.g.
        PREPARING), the whole mark_delivered action must fail cleanly — the
        delivery must NOT be left committed as DELIVERED.

        The old buggy path called
        `DeliveryService().complete_delivery(delivery.id, sync_order_status=True)`,
        which commits `delivery.status = DELIVERED` FIRST (inside
        `update_delivery_status`), and only afterwards — in a separate,
        already-committed transaction — validates the order transition via
        `_handle_delivery_status_change` -> `OrderService.update_order_status`.
        When that raises, the per-item `except: db.session.rollback()` cannot
        undo the delivery row that is already durably committed as DELIVERED
        (with `delivered_at` left None), permanently desyncing Order/Delivery.

        The fix routes mark_delivered through `OrderService.update_order_status`
        directly, which validates the order transition BEFORE any mutation is
        made, so an invalid transition raises before anything is written.
        """
        address = _address(db, sample_user)
        driver = _driver(db, "+998901900003")
        # PREPARING -> DELIVERED is NOT a valid order transition (must go
        # through OUT_FOR_DELIVERY first; see shared/status_transitions.py).
        order = _order(
            db,
            sample_user,
            address,
            OrderStatus.PREPARING,
            "ORD-BULK-DELIVERY-ATOMICITY",
            payment_method=PaymentMethod.CLICK,
        )
        # SCHEDULED -> DELIVERED IS a valid *delivery* transition on its own,
        # so the delivery-side check alone would not have caught this — only
        # checking order-validity first prevents the desync.
        delivery = Delivery(
            order_id=order.id,
            status=DeliveryStatus.SCHEDULED,
            delivery_person_id=driver.id,
            scheduled_date=datetime.now(UTC),
            scheduled_time_slot="09:00-12:00",
        )
        db.session.add(delivery)
        db.session.commit()

        result = AdminBulkActionService.perform(
            action="mark_delivered",
            target_type="delivery",
            target_ids=[delivery.id],
            parameters={},
            reason="bulk deliver",
            admin_id=admin_user.id,
        )

        assert result["success_count"] == 0
        assert result["failed_count"] == 1
        assert result["total_errors"] == 1
        assert result["errors"][0]["delivery_id"] == delivery.id

        # Re-query fresh from the DB (not just refresh the in-memory object)
        # to make sure nothing was actually committed.
        db.session.expire_all()
        persisted_delivery = Delivery.query.get(delivery.id)
        persisted_order = Order.query.get(order.id)
        assert persisted_delivery.status != DeliveryStatus.DELIVERED
        assert persisted_delivery.status == DeliveryStatus.SCHEDULED
        assert persisted_delivery.delivered_at is None
        assert persisted_order.status == OrderStatus.PREPARING
