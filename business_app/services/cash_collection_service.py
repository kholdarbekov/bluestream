"""Service for COD cash collection, receivable allocation, and debt rules."""

from datetime import datetime, UTC
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy.orm import contains_eager, joinedload

from business_app import db
from business_app.models.delivery import Delivery, DeliveryPerson
from business_app.models.order import Order
from business_app.models.payment import (
    CashCollectionAllocation,
    CashCollectionEvent,
    Payment,
)
from business_app.models.user import User
from business_app.utils.audit_logger import AuditEventType, AuditSeverity, audit_logger
from business_app.utils.constants import (
    CashCollectionSource,
    OrderStatus,
    PaymentMethod,
    PaymentStatus,
    UserRole,
)
from business_app.utils.exceptions import NotFoundError, ValidationError


class CashCollectionService:
    """COD receivable and cash collection service."""

    COD_ACTIVE_DEBT_LIMIT = 2

    @staticmethod
    def _to_decimal(value: Any) -> Decimal:
        if value is None:
            return Decimal('0.00')
        return Decimal(str(value)).quantize(Decimal('0.01'))

    @staticmethod
    def _normalize_source(source: Any) -> CashCollectionSource:
        if isinstance(source, CashCollectionSource):
            return source
        try:
            return CashCollectionSource(str(source))
        except ValueError as exc:
            raise ValidationError("Invalid cash collection source") from exc

    def ensure_cod_payment_for_order(
        self,
        order: Order,
        *,
        actor_user_id: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Payment:
        """Ensure every COD order has a canonical payment record."""
        if not order:
            raise NotFoundError("Order not found")
        if order.payment_method != PaymentMethod.CASH:
            raise ValidationError("Order is not configured for cash on delivery")

        payment = order.payment
        provider_data = dict(payment.provider_data or {}) if payment else {}
        provider_data.setdefault('settlement_mode', 'cash_on_delivery')
        if metadata:
            provider_data.update(metadata)
        if actor_user_id is not None:
            provider_data['actor_user_id'] = actor_user_id

        if not payment:
            payment = Payment(
                order_id=order.id,
                user_id=order.user_id,
                amount=order.total_amount,
                currency='UZS',
                payment_method=PaymentMethod.CASH,
                status=PaymentStatus.PENDING,
                description=f'Cash on delivery for order #{order.order_number}',
                provider_data=provider_data,
                amount_collected=Decimal('0.00'),
                outstanding_amount=order.total_amount,
            )
            db.session.add(payment)
            db.session.flush()
        else:
            payment.user_id = order.user_id
            payment.payment_method = PaymentMethod.CASH
            payment.amount = order.total_amount
            payment.currency = payment.currency or 'UZS'
            payment.provider_data = provider_data
            self.sync_payment_projection(payment)

        return payment

    def get_active_cod_payments_for_customer(
        self,
        customer_id: int,
        *,
        for_update: bool = False,
    ) -> List[Payment]:
        query = self._active_cod_payments_query(customer_id)
        if for_update:
            # Lock only payment rows to avoid Postgres FOR UPDATE errors with nullable eager joins.
            query = query.with_for_update(of=Payment)
        return query.all()

    def _active_cod_payments_query(self, customer_id: int):
        return (
            Payment.query.join(Order, Payment.order_id == Order.id)
            .options(contains_eager(Payment.order))
            .filter(
                Payment.user_id == customer_id,
                Payment.payment_method == PaymentMethod.CASH,
                Payment.outstanding_amount > 0,
                Order.status == OrderStatus.DELIVERED,
            )
            .order_by(Order.created_at.asc(), Payment.created_at.asc(), Payment.id.asc())
        )

    def get_active_cod_debt_count(self, customer_id: int) -> int:
        return len(self.get_active_cod_payments_for_customer(customer_id))

    def is_customer_cod_restricted(self, customer_id: int) -> bool:
        return self.get_active_cod_debt_count(customer_id) >= self.COD_ACTIVE_DEBT_LIMIT

    def get_cod_restriction_context(self, customer_id: int) -> Dict[str, Any]:
        active_debt_count = self.get_active_cod_debt_count(customer_id)
        return {
            'active_cod_debt_count': active_debt_count,
            'cod_restricted': active_debt_count >= self.COD_ACTIVE_DEBT_LIMIT,
            'cod_restriction_reason': (
                'customer_has_max_active_cod_debts'
                if active_debt_count >= self.COD_ACTIVE_DEBT_LIMIT
                else None
            ),
        }

    def validate_customer_can_use_cod(self, customer_id: int) -> Dict[str, Any]:
        context = self.get_cod_restriction_context(customer_id)
        if context['cod_restricted']:
            raise ValidationError(
                "Customer has reached the maximum number of active cash on delivery debts.",
                error_code='COD_DEBT_LIMIT_REACHED',
            )
        return context

    def get_customer_cod_statement(self, customer_id: int) -> Dict[str, Any]:
        customer = User.query.get(customer_id)
        if not customer:
            raise NotFoundError("Customer not found")

        payments = (
            Payment.query.join(Order, Payment.order_id == Order.id)
            .options(joinedload(Payment.order))
            .filter(
                Payment.user_id == customer_id,
                Payment.payment_method == PaymentMethod.CASH,
            )
            .order_by(Order.created_at.desc(), Payment.id.desc())
            .all()
        )

        items = []
        total_outstanding = Decimal('0.00')
        for payment in payments:
            outstanding_amount = self._to_decimal(payment.outstanding_amount)
            total_outstanding += outstanding_amount
            items.append({
                'payment_id': payment.id,
                'order_id': payment.order_id,
                'order_number': payment.order.order_number if payment.order else None,
                'order_status': payment.order.status.value if payment.order and hasattr(payment.order.status, 'value') else getattr(payment.order, 'status', None),
                'amount': float(payment.amount or 0),
                'amount_collected': float(payment.amount_collected or 0),
                'outstanding_amount': float(outstanding_amount),
                'status': payment.status.value if hasattr(payment.status, 'value') else payment.status,
                'created_at': payment.created_at.isoformat() if payment.created_at else None,
                'paid_at': payment.paid_at.isoformat() if payment.paid_at else None,
            })

        return {
            'customer_id': customer_id,
            'active_cod_debt_count': self.get_active_cod_debt_count(customer_id),
            'cod_restricted': self.is_customer_cod_restricted(customer_id),
            'total_outstanding_amount': float(total_outstanding),
            'items': items,
        }

    def get_order_payment_timeline(self, order_id: int) -> Dict[str, Any]:
        order = Order.query.options(joinedload(Order.payment)).get(order_id)
        if not order:
            raise NotFoundError("Order not found")

        payment = order.payment
        if not payment:
            return {
                'order_id': order_id,
                'order_number': order.order_number,
                'timeline': [],
            }

        timeline = [{
            'type': 'payment_created',
            'timestamp': payment.created_at.isoformat() if payment.created_at else None,
            'amount': float(payment.amount or 0),
            'amount_collected': float(payment.amount_collected or 0),
            'outstanding_amount': float(payment.outstanding_amount or 0),
            'status': payment.status.value if hasattr(payment.status, 'value') else payment.status,
        }]

        allocations = (
            CashCollectionAllocation.query.options(
                joinedload(CashCollectionAllocation.cash_collection_event),
            )
            .filter(CashCollectionAllocation.payment_id == payment.id)
            .order_by(CashCollectionAllocation.allocated_at.asc(), CashCollectionAllocation.id.asc())
            .all()
        )
        for allocation in allocations:
            event = allocation.cash_collection_event
            timeline.append({
                'type': 'cash_collection_allocation',
                'timestamp': allocation.allocated_at.isoformat() if allocation.allocated_at else None,
                'allocated_amount': float(allocation.allocated_amount or 0),
                'allocation_mode': allocation.allocation_mode,
                'collection_event_id': allocation.cash_collection_event_id,
                'collection_amount': float(event.amount or 0) if event else None,
                'collection_source': event.source.value if event and hasattr(event.source, 'value') else getattr(event, 'source', None),
                'delivery_id': event.delivery_id if event else None,
                'notes': event.notes if event else None,
                'reversed_at': allocation.reversed_at.isoformat() if allocation.reversed_at else None,
            })

        return {
            'order_id': order_id,
            'order_number': order.order_number,
            'payment_id': payment.id,
            'amount': float(payment.amount or 0),
            'amount_collected': float(payment.amount_collected or 0),
            'outstanding_amount': float(payment.outstanding_amount or 0),
            'status': payment.status.value if hasattr(payment.status, 'value') else payment.status,
            'timeline': timeline,
        }

    def sync_payment_projection(
        self,
        payment: Payment,
        *,
        collected_at: Optional[datetime] = None,
    ) -> Payment:
        amount = self._to_decimal(payment.amount)
        amount_collected = max(Decimal('0.00'), self._to_decimal(payment.amount_collected))
        amount_collected = min(amount, amount_collected)
        payment.amount_collected = amount_collected
        payment.outstanding_amount = max(Decimal('0.00'), amount - amount_collected)

        if payment.outstanding_amount <= Decimal('0.00'):
            payment.status = PaymentStatus.COMPLETED
            payment.paid_at = collected_at or payment.paid_at or datetime.now(UTC)
        elif payment.amount_collected > Decimal('0.00'):
            payment.status = PaymentStatus.PARTIALLY_PAID
            payment.paid_at = None
        else:
            payment.status = PaymentStatus.PENDING
            payment.paid_at = None

        if payment.amount_collected > Decimal('0.00'):
            payment.last_collected_at = collected_at or payment.last_collected_at or datetime.now(UTC)

        if payment.order:
            payment.order.is_paid = payment.status == PaymentStatus.COMPLETED
            payment.order.paid_at = payment.paid_at if payment.order.is_paid else None

        return payment

    def post_collection(
        self,
        *,
        customer_id: int,
        amount: Any,
        source: Any,
        collector_user_id: Optional[int] = None,
        recorded_by_user_id: Optional[int] = None,
        order_id: Optional[int] = None,
        delivery_id: Optional[int] = None,
        driver_cash_session_id: Optional[int] = None,
        notes: Optional[str] = None,
        proof_data: Optional[Dict[str, Any]] = None,
        occurred_at: Optional[datetime] = None,
        manual_allocations: Optional[Iterable[Dict[str, Any]]] = None,
        allocation_mode: str = 'auto',
        idempotency_key: Optional[str] = None,
    ) -> CashCollectionEvent:
        customer = User.query.get(customer_id)
        if not customer:
            raise NotFoundError("Customer not found")

        normalized_amount = self._to_decimal(amount)
        if normalized_amount < Decimal('0.00'):
            raise ValidationError("Collection amount cannot be negative")
        if normalized_amount == Decimal('0.00') and not notes:
            raise ValidationError("Notes are required when no cash is collected")

        if idempotency_key:
            existing_event = CashCollectionEvent.query.filter_by(idempotency_key=idempotency_key).first()
            if existing_event:
                return existing_event

        source_enum = self._normalize_source(source)
        occurred_at = occurred_at or datetime.now(UTC)
        if occurred_at.tzinfo is None:
            occurred_at = occurred_at.replace(tzinfo=UTC)

        self._validate_collection_context(
            customer_id=customer_id,
            source=source_enum,
            collector_user_id=collector_user_id,
            recorded_by_user_id=recorded_by_user_id,
            order_id=order_id,
            delivery_id=delivery_id,
            notes=notes,
            manual_allocations=manual_allocations,
        )

        event = CashCollectionEvent(
            customer_id=customer_id,
            collector_user_id=collector_user_id,
            recorded_by_user_id=recorded_by_user_id,
            order_id=order_id,
            delivery_id=delivery_id,
            driver_cash_session_id=driver_cash_session_id,
            amount=normalized_amount,
            currency='UZS',
            source=source_enum,
            occurred_at=occurred_at,
            notes=notes,
            proof_data=proof_data or {},
            unapplied_amount=normalized_amount,
            idempotency_key=idempotency_key,
        )
        db.session.add(event)
        db.session.flush()

        if collector_user_id and not event.driver_cash_session_id:
            from business_app.services.driver_reconciliation_service import DriverReconciliationService

            session = DriverReconciliationService().get_or_create_session(
                driver_user_id=collector_user_id,
                business_date=occurred_at.date(),
            )
            event.driver_cash_session_id = session.id

        allocations = list(manual_allocations or [])
        if allocations:
            allocation_order = 0
            for allocation in allocations:
                allocation_order += 1
                payment = Payment.query.with_for_update(of=Payment).get(allocation['payment_id'])
                if not payment:
                    raise NotFoundError("Payment not found for manual allocation")
                allocated_amount = self._to_decimal(allocation.get('amount'))
                self._allocate_to_payment(
                    event=event,
                    payment=payment,
                    amount=allocated_amount,
                    allocation_order=allocation_order,
                    allocation_mode='manual',
                )
        else:
            self._allocate_oldest_first(
                event=event,
                customer_id=customer_id,
                order_id=order_id,
                allocation_mode=allocation_mode,
            )

        if event.driver_cash_session_id:
            from business_app.services.driver_reconciliation_service import DriverReconciliationService
            from business_app.models.payment import DriverCashSession

            if event.collector_user_id:
                session = DriverReconciliationService().get_or_create_session(
                    driver_user_id=event.collector_user_id,
                    business_date=event.occurred_at.date(),
                )
                event.driver_cash_session_id = session.id
            else:
                session = DriverCashSession.query.get(event.driver_cash_session_id)

            if session:
                DriverReconciliationService().refresh_expected_cash(session)

        self._refresh_legacy_cash_projections(
            delivery_id=event.delivery_id,
            collector_user_id=event.collector_user_id,
        )

        audit_logger.log_event(
            event_type=AuditEventType.PAYMENT_PROCESSED,
            action='cash_collection_posted',
            severity=AuditSeverity.MEDIUM,
            resource_type='cash_collection_event',
            resource_id=str(event.id),
            additional_data={
                'customer_id': customer_id,
                'collector_user_id': collector_user_id,
                'order_id': order_id,
                'delivery_id': delivery_id,
                'amount': float(normalized_amount),
                'unapplied_amount': float(event.unapplied_amount or 0),
                'source': source_enum.value,
            },
        )

        db.session.commit()
        return event

    def _validate_collection_context(
        self,
        *,
        customer_id: int,
        source: CashCollectionSource,
        collector_user_id: Optional[int],
        recorded_by_user_id: Optional[int],
        order_id: Optional[int],
        delivery_id: Optional[int],
        notes: Optional[str],
        manual_allocations: Optional[Iterable[Dict[str, Any]]],
    ) -> None:
        if source == CashCollectionSource.DELIVERY_COMPLETION and not delivery_id:
            raise ValidationError("delivery_id is required for delivery completion collections")
        if source == CashCollectionSource.NEXT_DELIVERY and not delivery_id:
            raise ValidationError("delivery_id is required for next-delivery collections")
        if source == CashCollectionSource.ADMIN_ADJUSTMENT and not notes:
            raise ValidationError("Notes are required for admin adjustments")
        if source in {CashCollectionSource.STANDALONE_MEETING, CashCollectionSource.NEXT_DELIVERY} and not notes:
            raise ValidationError("Notes are required for late or standalone COD collections")

        if collector_user_id:
            collector = User.query.get(collector_user_id)
            if not collector:
                raise NotFoundError("Collector user not found")
            staff_roles = getattr(collector, 'staff_roles', []) or []
            if isinstance(staff_roles, str):
                staff_roles = [role.strip().strip('"\'') for role in staff_roles.strip('[]').split(',') if role.strip()]
            role_values = {getattr(collector.role, 'value', collector.role)}
            role_values.update(staff_roles)
            if UserRole.DELIVERY_DRIVER.value not in role_values:
                raise ValidationError("Collector must be an authorized delivery driver")

            from business_app.services.driver_reconciliation_service import DriverReconciliationService

            if DriverReconciliationService().is_driver_blocked_from_cod(collector_user_id):
                raise ValidationError(
                    "Driver is blocked from new cash on delivery collections until reconciliation issues are resolved",
                    error_code='COD_DRIVER_BLOCKED',
                )

        if order_id:
            order = Order.query.get(order_id)
            if not order:
                raise NotFoundError("Order not found")
            if order.user_id != customer_id:
                raise ValidationError("Order does not belong to the selected customer")
            if order.payment_method != PaymentMethod.CASH:
                raise ValidationError("Only COD orders can be targeted for COD collections")
            if order.status != OrderStatus.DELIVERED:
                raise ValidationError("Only delivered COD orders can be targeted for collection")

        if delivery_id:
            delivery = Delivery.query.options(joinedload(Delivery.order)).get(delivery_id)
            if not delivery:
                raise NotFoundError("Delivery not found")
            if delivery.order and delivery.order.user_id != customer_id:
                raise ValidationError("Delivery does not belong to the selected customer")
            if order_id and delivery.order_id != order_id:
                raise ValidationError("delivery_id does not match the selected order")

        if manual_allocations:
            allocations = list(manual_allocations)
            if not allocations:
                raise ValidationError("manual_allocations cannot be empty when provided")
            for allocation in allocations:
                payment_id = allocation.get('payment_id')
                payment = Payment.query.options(joinedload(Payment.order)).get(payment_id)
                if not payment:
                    raise NotFoundError("Payment not found for manual allocation")
                if payment.user_id != customer_id:
                    raise ValidationError("Manual allocations must belong to the selected customer")
                if payment.payment_method != PaymentMethod.CASH:
                    raise ValidationError("Manual allocations can only target COD payments")
                if payment.order and payment.order.status != OrderStatus.DELIVERED:
                    raise ValidationError("Manual allocations can only target delivered COD orders")
                if self._to_decimal(payment.outstanding_amount) <= Decimal('0.00'):
                    raise ValidationError("Manual allocations can only target payments with outstanding balance")

        if source == CashCollectionSource.ADMIN_ADJUSTMENT and not recorded_by_user_id:
            raise ValidationError("recorded_by_user_id is required for admin adjustments")

    def reverse_collection_event(
        self,
        event_id: int,
        *,
        reversed_by_user_id: int,
        reason: str,
    ) -> CashCollectionEvent:
        event = CashCollectionEvent.query.options(
            joinedload(CashCollectionEvent.allocations).joinedload(CashCollectionAllocation.payment),
        ).get(event_id)
        if not event:
            raise NotFoundError("Cash collection event not found")
        if event.voided_at:
            raise ValidationError("Cash collection event is already voided")
        if not reason:
            raise ValidationError("A reversal reason is required")

        now = datetime.now(UTC)
        for allocation in event.allocations:
            if allocation.reversed_at:
                continue
            allocation.reversed_at = now
            allocation.reversed_by_user_id = reversed_by_user_id
            allocation.reversal_reason = reason
            payment = allocation.payment
            payment.amount_collected = self._to_decimal(payment.amount_collected) - self._to_decimal(allocation.allocated_amount)
            self.sync_payment_projection(payment)

        event.unapplied_amount = self._to_decimal(event.amount)
        event.voided_at = now
        event.voided_by_user_id = reversed_by_user_id
        event.void_reason = reason

        self._refresh_legacy_cash_projections(
            delivery_id=event.delivery_id,
            collector_user_id=event.collector_user_id,
        )

        audit_logger.log_event(
            event_type=AuditEventType.PAYMENT_REFUNDED,
            action='cash_collection_reversed',
            severity=AuditSeverity.HIGH,
            resource_type='cash_collection_event',
            resource_id=str(event.id),
            additional_data={
                'reason': reason,
                'reversed_by_user_id': reversed_by_user_id,
            },
        )

        db.session.commit()
        return event

    def _allocate_oldest_first(
        self,
        *,
        event: CashCollectionEvent,
        customer_id: int,
        order_id: Optional[int],
        allocation_mode: str,
    ) -> None:
        if self._to_decimal(event.amount) <= Decimal('0.00'):
            return

        allocation_order = 0
        payments = self.get_active_cod_payments_for_customer(customer_id, for_update=True)

        if order_id:
            current_order_payment = Payment.query.options(joinedload(Payment.order)).filter_by(order_id=order_id).first()
            if (
                current_order_payment
                and current_order_payment.payment_method == PaymentMethod.CASH
                and self._to_decimal(current_order_payment.outstanding_amount) > Decimal('0.00')
                and current_order_payment.id not in {payment.id for payment in payments}
            ):
                payments.append(current_order_payment)

        for payment in payments:
            if self._to_decimal(event.unapplied_amount) <= Decimal('0.00'):
                break
            allocation_order += 1
            allocatable = min(
                self._to_decimal(payment.outstanding_amount),
                self._to_decimal(event.unapplied_amount),
            )
            if allocatable <= Decimal('0.00'):
                continue
            self._allocate_to_payment(
                event=event,
                payment=payment,
                amount=allocatable,
                allocation_order=allocation_order,
                allocation_mode=allocation_mode,
            )

    def _allocate_to_payment(
        self,
        *,
        event: CashCollectionEvent,
        payment: Payment,
        amount: Decimal,
        allocation_order: int,
        allocation_mode: str,
    ) -> None:
        amount = self._to_decimal(amount)
        if amount <= Decimal('0.00'):
            return
        if amount > self._to_decimal(event.unapplied_amount):
            raise ValidationError("Allocated amount exceeds unapplied event balance")
        if amount > self._to_decimal(payment.outstanding_amount):
            raise ValidationError("Allocated amount exceeds payment outstanding balance")

        allocation = CashCollectionAllocation(
            cash_collection_event_id=event.id,
            payment_id=payment.id,
            order_id=payment.order_id,
            allocated_amount=amount,
            allocation_order=allocation_order,
            allocation_mode=allocation_mode,
            allocation_metadata={
                'order_number': payment.order.order_number if payment.order else None,
            },
        )
        db.session.add(allocation)
        payment.amount_collected = self._to_decimal(payment.amount_collected) + amount
        event.unapplied_amount = self._to_decimal(event.unapplied_amount) - amount
        previous_status = payment.status
        self.sync_payment_projection(payment, collected_at=event.occurred_at)

        if previous_status != PaymentStatus.COMPLETED and payment.status == PaymentStatus.COMPLETED:
            try:
                from business_app.tasks.notification_tasks import send_payment_confirmation_task

                send_payment_confirmation_task.delay(payment.id)
            except Exception:
                pass

    def _refresh_legacy_cash_projections(
        self,
        *,
        delivery_id: Optional[int],
        collector_user_id: Optional[int],
    ) -> None:
        if delivery_id:
            delivery = Delivery.query.get(delivery_id)
            if delivery:
                total_for_delivery = (
                    db.session.query(db.func.coalesce(db.func.sum(CashCollectionEvent.amount), 0))
                    .filter(
                        CashCollectionEvent.delivery_id == delivery_id,
                        CashCollectionEvent.voided_at.is_(None),
                    )
                    .scalar()
                )
                delivery.cash_collected = self._to_decimal(total_for_delivery)

        if collector_user_id:
            profile = DeliveryPerson.query.filter_by(user_id=collector_user_id).first()
            if profile:
                total_for_driver = (
                    db.session.query(db.func.coalesce(db.func.sum(CashCollectionEvent.amount), 0))
                    .filter(
                        CashCollectionEvent.collector_user_id == collector_user_id,
                        CashCollectionEvent.voided_at.is_(None),
                    )
                    .scalar()
                )
                profile.total_cash_collected = self._to_decimal(total_for_driver)
