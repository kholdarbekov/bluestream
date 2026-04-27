from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from business_app import db
from business_app.models.delivery import Delivery, DeliveryStatusHistory
from business_app.models.loyalty import LoyaltyTransaction
from business_app.models.notification import Notification
from business_app.models.order import Order, OrderItem, OrderStatusHistory
from business_app.models.payment import (
    CashCollectionAllocation,
    CashCollectionEvent,
    Payment,
    PaymentFiscalization,
    PaymentTransaction,
)
from business_app.services.order_deletion_service import OrderDeletionService
from business_app.utils.constants import (
    CashCollectionSource,
    DeliveryStatus,
    LoyaltyTransactionType,
    NotificationChannel,
    OrderStatus,
    PaymentMethod,
    PaymentStatus,
)


def _create_order(*, user_id: int, order_number: str, amount: Decimal) -> Order:
    order = Order(
        user_id=user_id,
        order_number=order_number,
        status=OrderStatus.PENDING,
        subtotal=amount,
        delivery_fee=Decimal('0.00'),
        total_amount=amount,
        payment_method=PaymentMethod.CASH,
        created_at=datetime.now(UTC),
    )
    db.session.add(order)
    db.session.flush()
    return order


def _create_payment(*, user_id: int, order_id: int, amount: Decimal, payment_id: str) -> Payment:
    payment = Payment(
        payment_id=payment_id,
        user_id=user_id,
        order_id=order_id,
        amount=amount,
        payment_method=PaymentMethod.CASH,
        status=PaymentStatus.COMPLETED,
    )
    db.session.add(payment)
    db.session.flush()
    return payment


def test_delete_order_by_number_removes_dependency_tree_and_keeps_other_orders(
    db, sample_user, sample_product
):
    target_order = _create_order(
        user_id=sample_user.id,
        order_number='ORD-DEL-TARGET',
        amount=Decimal('18000.00'),
    )
    keep_order = _create_order(
        user_id=sample_user.id,
        order_number='ORD-DEL-KEEP',
        amount=Decimal('22000.00'),
    )

    target_item = OrderItem(
        order_id=target_order.id,
        product_id=sample_product.id,
        quantity=1,
        unit_price=Decimal('18000.00'),
        total_price=Decimal('18000.00'),
    )
    keep_item = OrderItem(
        order_id=keep_order.id,
        product_id=sample_product.id,
        quantity=1,
        unit_price=Decimal('22000.00'),
        total_price=Decimal('22000.00'),
    )
    db.session.add_all([target_item, keep_item])
    db.session.flush()

    target_delivery = Delivery(
        order_id=target_order.id,
        status=DeliveryStatus.SCHEDULED,
        scheduled_date=datetime.now(UTC),
        scheduled_time_slot='09:00-12:00',
    )
    keep_delivery = Delivery(
        order_id=keep_order.id,
        status=DeliveryStatus.SCHEDULED,
        scheduled_date=datetime.now(UTC),
        scheduled_time_slot='12:00-15:00',
    )
    db.session.add_all([target_delivery, keep_delivery])
    db.session.flush()

    db.session.add_all(
        [
            DeliveryStatusHistory(
                delivery_id=target_delivery.id,
                old_status=DeliveryStatus.SCHEDULED,
                new_status=DeliveryStatus.ASSIGNED,
                changed_at=datetime.now(UTC),
            ),
            DeliveryStatusHistory(
                delivery_id=keep_delivery.id,
                old_status=DeliveryStatus.SCHEDULED,
                new_status=DeliveryStatus.ASSIGNED,
                changed_at=datetime.now(UTC),
            ),
        ]
    )

    target_payment = _create_payment(
        user_id=sample_user.id,
        order_id=target_order.id,
        amount=Decimal('18000.00'),
        payment_id=f'pay-target-{uuid4().hex[:8]}',
    )
    keep_payment = _create_payment(
        user_id=sample_user.id,
        order_id=keep_order.id,
        amount=Decimal('22000.00'),
        payment_id=f'pay-keep-{uuid4().hex[:8]}',
    )

    db.session.add_all(
        [
            PaymentTransaction(
                payment_id=target_payment.id,
                transaction_type='charge',
                amount=Decimal('18000.00'),
                status='success',
                success=True,
            ),
            PaymentTransaction(
                payment_id=keep_payment.id,
                transaction_type='charge',
                amount=Decimal('22000.00'),
                status='success',
                success=True,
            ),
            PaymentFiscalization(payment_id=target_payment.id),
            PaymentFiscalization(payment_id=keep_payment.id),
        ]
    )

    target_event = CashCollectionEvent(
        customer_id=sample_user.id,
        order_id=target_order.id,
        delivery_id=target_delivery.id,
        amount=Decimal('18000.00'),
        source=CashCollectionSource.DELIVERY_COMPLETION,
        occurred_at=datetime.now(UTC),
    )
    keep_event = CashCollectionEvent(
        customer_id=sample_user.id,
        order_id=keep_order.id,
        delivery_id=keep_delivery.id,
        amount=Decimal('22000.00'),
        source=CashCollectionSource.DELIVERY_COMPLETION,
        occurred_at=datetime.now(UTC),
    )
    db.session.add_all([target_event, keep_event])
    db.session.flush()

    db.session.add_all(
        [
            CashCollectionAllocation(
                cash_collection_event_id=target_event.id,
                payment_id=target_payment.id,
                order_id=target_order.id,
                allocated_amount=Decimal('18000.00'),
            ),
            CashCollectionAllocation(
                cash_collection_event_id=keep_event.id,
                payment_id=keep_payment.id,
                order_id=keep_order.id,
                allocated_amount=Decimal('22000.00'),
            ),
        ]
    )

    db.session.add_all(
        [
            LoyaltyTransaction(
                user_id=sample_user.id,
                points=10,
                transaction_type=LoyaltyTransactionType.EARNED,
                description='Target loyalty transaction',
                order_id=target_order.id,
            ),
            LoyaltyTransaction(
                user_id=sample_user.id,
                points=20,
                transaction_type=LoyaltyTransactionType.EARNED,
                description='Keep loyalty transaction',
                order_id=keep_order.id,
            ),
        ]
    )

    db.session.add_all(
        [
            Notification(
                user_id=sample_user.id,
                notification_type='order_update',
                channel=NotificationChannel.SMS,
                title='Target order notification',
                message='Target message',
                order_id=target_order.id,
                delivery_id=target_delivery.id,
            ),
            Notification(
                user_id=sample_user.id,
                notification_type='order_update',
                channel=NotificationChannel.SMS,
                title='Keep order notification',
                message='Keep message',
                order_id=keep_order.id,
                delivery_id=keep_delivery.id,
            ),
        ]
    )

    db.session.add_all(
        [
            OrderStatusHistory(
                order_id=target_order.id,
                old_status=OrderStatus.PENDING,
                new_status=OrderStatus.CONFIRMED,
                changed_at=datetime.now(UTC),
            ),
            OrderStatusHistory(
                order_id=keep_order.id,
                old_status=OrderStatus.PENDING,
                new_status=OrderStatus.CONFIRMED,
                changed_at=datetime.now(UTC),
            ),
        ]
    )
    db.session.commit()

    target_order_id = target_order.id
    target_order_number = target_order.order_number
    target_delivery_id = target_delivery.id
    target_payment_id = target_payment.id
    keep_order_id = keep_order.id

    service = OrderDeletionService()
    dry_run = service.delete_order_by_number(target_order_number, apply_changes=False)
    assert dry_run['found'] is True
    assert dry_run['applied'] is False
    assert dry_run['rows_by_table']['orders'] == 1
    assert dry_run['rows_by_table']['order_items'] == 1
    assert dry_run['rows_by_table']['payments'] == 1
    assert dry_run['rows_by_table']['deliveries'] == 1

    result = service.delete_order_by_number(target_order_number, apply_changes=True)
    assert result['found'] is True
    assert result['applied'] is True
    assert result['deleted_rows_by_table']['orders'] == 1

    assert Order.query.filter_by(id=target_order_id).first() is None
    assert OrderItem.query.filter_by(order_id=target_order_id).count() == 0
    assert Delivery.query.filter_by(order_id=target_order_id).count() == 0
    assert DeliveryStatusHistory.query.filter_by(delivery_id=target_delivery_id).count() == 0
    assert Payment.query.filter_by(order_id=target_order_id).count() == 0
    assert PaymentTransaction.query.filter_by(payment_id=target_payment_id).count() == 0
    assert PaymentFiscalization.query.filter_by(payment_id=target_payment_id).count() == 0
    assert CashCollectionEvent.query.filter_by(order_id=target_order_id).count() == 0
    assert CashCollectionAllocation.query.filter_by(order_id=target_order_id).count() == 0
    assert LoyaltyTransaction.query.filter_by(order_id=target_order_id).count() == 0
    assert Notification.query.filter_by(order_id=target_order_id).count() == 0
    assert OrderStatusHistory.query.filter_by(order_id=target_order_id).count() == 0

    assert Order.query.filter_by(id=keep_order_id).first() is not None
    assert OrderItem.query.filter_by(order_id=keep_order_id).count() == 1
    assert Delivery.query.filter_by(order_id=keep_order_id).count() == 1
    assert Payment.query.filter_by(order_id=keep_order_id).count() == 1
    assert CashCollectionEvent.query.filter_by(order_id=keep_order_id).count() == 1
    assert LoyaltyTransaction.query.filter_by(order_id=keep_order_id).count() == 1
    assert Notification.query.filter_by(order_id=keep_order_id).count() == 1


def test_delete_order_by_number_returns_not_found_for_unknown_order(db):
    service = OrderDeletionService()

    result = service.delete_order_by_number('ORD-DOES-NOT-EXIST', apply_changes=True)

    assert result['found'] is False
    assert result['applied'] is False
    assert result['total_rows'] == 0
