"""Click fiscalization and marking-code lifecycle workflows."""

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from flask import current_app
from sqlalchemy.orm import joinedload

from business_app import db
from business_app.models.audit import AuditEventType, AuditSeverity
from business_app.models.order import Order, OrderItem, OrderItemMarkingCodeAllocation
from business_app.models.payment import Payment, PaymentFiscalization
from business_app.models.product import Product, ProductMarkingCode
from business_app.services.product_fiscal_service import ProductFiscalService
from business_app.utils.audit_logger import audit_logger
from business_app.utils.constants import (
    FiscalizationStatus,
    MarkingCodeLedgerEventType,
    MarkingCodeStatus,
    PaymentMethod,
    PaymentStatus,
)
from business_app.utils.exceptions import NotFoundError, ValidationError


class PaymentFiscalizationService:
    """Own Click fiscalization execution and marking-code accounting."""

    def __init__(self, click_provider_service=None):
        self._click_provider_service = click_provider_service
        self._product_fiscal_service = ProductFiscalService()

    @property
    def click_provider_service(self):
        if self._click_provider_service is None:
            from business_app.services.click_provider_service import ClickProviderService

            self._click_provider_service = ClickProviderService()
        return self._click_provider_service

    def _append_payment_audit_trail(
        self,
        payment: Payment,
        *,
        action: str,
        actor_user_id: Optional[int] = None,
        fiscalization: Optional[PaymentFiscalization] = None,
        success: bool = True,
        error_message: Optional[str] = None,
        additional_data: Optional[Dict[str, Any]] = None,
    ) -> None:
        provider_data = dict(payment.provider_data or {})
        trail = list(provider_data.get('fiscalization_audit_trail') or [])
        trail.append({
            'action': action,
            'occurred_at': datetime.now(timezone.utc).isoformat(),
            'actor_user_id': actor_user_id,
            'payment_id': payment.id,
            'order_id': payment.order_id,
            'fiscalization_id': fiscalization.id if fiscalization else None,
            'fiscalization_status': (
                fiscalization.status.value if fiscalization and hasattr(fiscalization.status, 'value') else None
            ),
            'success': bool(success),
            'error_message': error_message,
            'additional_data': additional_data or {},
        })
        provider_data['fiscalization_audit_trail'] = trail[-50:]
        payment.provider_data = provider_data

    def _log_payment_activity(
        self,
        payment: Payment,
        *,
        action: str,
        description: str,
        event_type: AuditEventType,
        severity: AuditSeverity = AuditSeverity.MEDIUM,
        actor_user_id: Optional[int] = None,
        fiscalization: Optional[PaymentFiscalization] = None,
        success: bool = True,
        error_message: Optional[str] = None,
        additional_data: Optional[Dict[str, Any]] = None,
    ) -> None:
        payload = dict(additional_data or {})
        payload.update({
            'payment_id': payment.id,
            'order_id': payment.order_id,
            'payment_method': (
                payment.payment_method.value if hasattr(payment.payment_method, 'value') else payment.payment_method
            ),
            'payment_provider': payment.payment_provider,
            'actor_user_id': actor_user_id,
        })
        if fiscalization is not None:
            payload.update({
                'fiscalization_id': fiscalization.id,
                'fiscalization_status': (
                    fiscalization.status.value if hasattr(fiscalization.status, 'value') else fiscalization.status
                ),
                'fiscalization_attempts': int(fiscalization.attempts or 0),
                'provider_receipt_id': fiscalization.provider_receipt_id,
            })

        self._append_payment_audit_trail(
            payment,
            action=action,
            actor_user_id=actor_user_id,
            fiscalization=fiscalization,
            success=success,
            error_message=error_message,
            additional_data=payload,
        )
        audit_logger.log_event(
            event_type=event_type,
            action=action,
            severity=severity,
            resource_type='payment',
            resource_id=str(payment.id),
            description=description,
            success=success,
            error_message=error_message,
            additional_data=payload,
        )

    def ensure_fiscalization_record(self, payment: Payment) -> PaymentFiscalization:
        fiscalization = payment.fiscalization
        if fiscalization:
            return fiscalization

        fiscalization = PaymentFiscalization(
            payment_id=payment.id,
            provider_name=PaymentMethod.CLICK.value,
            status=FiscalizationStatus.PENDING,
        )
        db.session.add(fiscalization)
        db.session.flush()
        return fiscalization

    def payment_requires_click_fiscalization(self, payment: Payment) -> bool:
        method_value = payment.payment_method.value if hasattr(payment.payment_method, 'value') else payment.payment_method
        if method_value not in {PaymentMethod.CLICK.value, PaymentMethod.CARD.value}:
            return False
        if payment.status != PaymentStatus.COMPLETED:
            return False
        order = payment.order
        if not order:
            return False
        if Decimal(str(order.delivery_fee or 0)) > Decimal('0'):
            return True
        return any(
            item.product and item.product.fiscalization_enabled
            for item in (order.order_items or [])
        )

    def queue_click_fiscalization(
        self,
        payment_id: int,
        *,
        actor_user_id: Optional[int] = None,
    ) -> PaymentFiscalization:
        payment = self._get_payment(payment_id)
        fiscalization = self.ensure_fiscalization_record(payment)

        if not self.payment_requires_click_fiscalization(payment):
            fiscalization.status = FiscalizationStatus.NOT_REQUIRED
            fiscalization.queued_at = datetime.now(timezone.utc)
            self._log_payment_activity(
                payment,
                action='payment_fiscalization_not_required',
                description=f'Fiscalization is not required for payment {payment.id}',
                event_type=AuditEventType.PAYMENT_PROCESSED,
                severity=AuditSeverity.LOW,
                actor_user_id=actor_user_id,
                fiscalization=fiscalization,
            )
            return fiscalization

        fiscalization.status = FiscalizationStatus.PENDING
        fiscalization.queued_at = datetime.now(timezone.utc)
        self._log_payment_activity(
            payment,
            action='payment_fiscalization_queued',
            description=f'Queued Click fiscalization for payment {payment.id}',
            event_type=AuditEventType.PAYMENT_PROCESSED,
            severity=AuditSeverity.LOW,
            actor_user_id=actor_user_id,
            fiscalization=fiscalization,
        )
        return fiscalization

    def process_click_fiscalization(
        self,
        payment_id: int,
        *,
        force: bool = False,
        actor_user_id: Optional[int] = None,
    ) -> PaymentFiscalization:
        payment = self._get_payment(payment_id)
        if payment.status != PaymentStatus.COMPLETED:
            raise ValidationError("Only completed payments can be fiscalized")

        fiscalization = self.ensure_fiscalization_record(payment)
        if fiscalization.status == FiscalizationStatus.COMPLETED and not force:
            return fiscalization

        if not self.payment_requires_click_fiscalization(payment):
            fiscalization.status = FiscalizationStatus.NOT_REQUIRED
            fiscalization.completed_at = datetime.now(timezone.utc)
            return fiscalization

        fiscalization.status = FiscalizationStatus.PROCESSING
        fiscalization.last_attempt_at = datetime.now(timezone.utc)
        fiscalization.attempts = int(fiscalization.attempts or 0) + 1
        db.session.flush()

        self._log_payment_activity(
            payment,
            action='payment_fiscalization_processing_started',
            description=f'Started Click fiscalization attempt {fiscalization.attempts} for payment {payment.id}',
            event_type=AuditEventType.PAYMENT_PROCESSED,
            severity=AuditSeverity.MEDIUM,
            actor_user_id=actor_user_id,
            fiscalization=fiscalization,
        )

        self.reserve_required_marking_codes(payment, actor_user_id=actor_user_id)
        payload = self.build_click_fiscalization_payload(payment)
        fiscalization.request_payload = payload

        try:
            response = self.click_provider_service.submit_fiscalization(payment, payload)
        except Exception as exc:
            fiscalization.status = FiscalizationStatus.FAILED
            fiscalization.failure_reason = str(exc)
            fiscalization.response_payload = {'error': str(exc)}
            fiscalization.next_retry_at = datetime.now(timezone.utc) + timedelta(minutes=5)
            self.release_reserved_marking_codes(
                payment,
                reason='click_fiscalization_failed',
                actor_user_id=actor_user_id,
            )
            self._log_payment_activity(
                payment,
                action='payment_fiscalization_failed',
                description=f'Click fiscalization failed for payment {payment.id}',
                event_type=AuditEventType.PAYMENT_FAILED,
                severity=AuditSeverity.HIGH,
                actor_user_id=actor_user_id,
                fiscalization=fiscalization,
                success=False,
                error_message=str(exc),
            )
            raise

        fiscalization.response_payload = response or {}
        fiscalization.receipt_payload = response.get('receipt') or response.get('data') or response
        fiscalization.provider_receipt_id = (
            response.get('receipt_id')
            or response.get('receiptId')
            or (response.get('receipt') or {}).get('id')
            or (response.get('data') or {}).get('id')
        )
        fiscalization.provider_receipt_url = (
            response.get('receipt_url')
            or response.get('receiptUrl')
            or (response.get('receipt') or {}).get('url')
        )
        fiscalization.provider_status = response.get('status') or response.get('state') or 'completed'
        fiscalization.status = FiscalizationStatus.COMPLETED
        fiscalization.failure_reason = None
        fiscalization.completed_at = datetime.now(timezone.utc)

        used_codes = self.mark_reserved_codes_used(payment, fiscalization, actor_user_id=actor_user_id)
        self._log_payment_activity(
            payment,
            action='payment_fiscalization_completed',
            description=f'Completed Click fiscalization for payment {payment.id}',
            event_type=AuditEventType.PAYMENT_PROCESSED,
            severity=AuditSeverity.MEDIUM,
            actor_user_id=actor_user_id,
            fiscalization=fiscalization,
            additional_data={
                'used_marking_codes': used_codes,
            },
        )
        return fiscalization

    def consume_marking_codes_for_business_account(
        self,
        payment: Payment,
        *,
        actor_user_id: Optional[int] = None,
    ) -> PaymentFiscalization:
        method_value = payment.payment_method.value if hasattr(payment.payment_method, 'value') else payment.payment_method
        if method_value != PaymentMethod.BUSINESS_ACCOUNT.value or not payment.consume_marking_codes:
            raise ValidationError("Payment is not eligible for manual marking-code consumption")
        if payment.status != PaymentStatus.COMPLETED:
            raise ValidationError("Only completed business account payments can consume marking codes")

        fiscalization = self.ensure_fiscalization_record(payment)
        fiscalization.provider_name = PaymentMethod.BUSINESS_ACCOUNT.value
        fiscalization.status = FiscalizationStatus.PROCESSING
        fiscalization.last_attempt_at = datetime.now(timezone.utc)
        fiscalization.attempts = int(fiscalization.attempts or 0) + 1

        self._reserve_codes_for_manual_consumption(payment, actor_user_id=actor_user_id)
        fiscalization.request_payload = {'mode': 'manual_business_account_consumption'}
        fiscalization.response_payload = {'status': 'completed'}
        fiscalization.receipt_payload = {'mode': 'manual_business_account_consumption'}
        fiscalization.provider_status = 'completed'
        fiscalization.status = FiscalizationStatus.COMPLETED
        fiscalization.completed_at = datetime.now(timezone.utc)

        used_codes = self.mark_reserved_codes_used(payment, fiscalization, actor_user_id=actor_user_id)
        self._log_payment_activity(
            payment,
            action='business_account_marking_codes_consumed',
            description=f'Consumed marking codes for business account payment {payment.id}',
            event_type=AuditEventType.PAYMENT_PROCESSED,
            severity=AuditSeverity.MEDIUM,
            actor_user_id=actor_user_id,
            fiscalization=fiscalization,
            additional_data={
                'used_marking_codes': used_codes,
            },
        )
        return fiscalization

    def build_click_fiscalization_payload(self, payment: Payment) -> Dict[str, Any]:
        order = payment.order
        if not order:
            raise ValidationError("Payment has no order")

        reserved_lookup = self._reserved_code_lookup(payment)
        items_payload: List[Dict[str, Any]] = []
        for order_item in order.order_items or []:
            product = order_item.product
            if not product or not product.fiscalization_enabled:
                continue

            quantity = int(order_item.quantity or 0)
            unit_price = Decimal(str(order_item.unit_price or 0)).quantize(Decimal('0.01'))
            total_price = Decimal(str(order_item.total_price or 0)).quantize(Decimal('0.01'))
            vat_percent = Decimal(str(product.vat_percent or 0)).quantize(Decimal('0.01'))
            vat_amount = (total_price * vat_percent / Decimal('100')).quantize(Decimal('0.01'))
            item_payload = {
                'Name': product.name,
                'Barcode': product.barcode,
                'SPIC': product.spic,
                'Units': product.units,
                'PackageCode': product.package_code,
                'GoodPrice': float(unit_price),
                'Price': float(total_price),
                'Amount': quantity,
                'VAT': float(vat_amount),
                'VATPercent': float(vat_percent),
                'Discount': 0,
                'Other': str(order_item.id),
            }
            labels = reserved_lookup.get(order_item.id, [])
            if product.requires_marking_codes:
                if len(labels) != quantity:
                    raise ValidationError(
                        f"Expected {order_item.quantity} labels for product {product.name}, got {len(labels)}"
                    )
                item_payload['Labels'] = labels

            commission_info = self._build_commission_info(payment)
            if commission_info:
                item_payload['CommissionInfo'] = commission_info
            items_payload.append(item_payload)

        delivery_fee = Decimal(str(order.delivery_fee or 0)).quantize(Decimal('0.01'))
        if delivery_fee > Decimal('0'):
            delivery_item = {
                'Name': 'Delivery',
                'Barcode': None,
                'SPIC': None,
                'Units': 'service',
                'PackageCode': None,
                'GoodPrice': float(delivery_fee),
                'Price': float(delivery_fee),
                'Amount': 1,
                'VAT': 0,
                'VATPercent': 0,
                'Discount': 0,
                'Other': 'delivery_fee',
            }
            commission_info = self._build_commission_info(payment)
            if commission_info:
                delivery_item['CommissionInfo'] = commission_info
            items_payload.append(delivery_item)

        payload: Dict[str, Any] = {
            'service_id': self.click_provider_service.service_id,
            'payment_id': payment.provider_transaction_id or payment.payment_id,
            'merchant_trans_id': payment.payment_id,
            'received_cash': 0,
            'received_ecash': 0,
            'received_card': float(Decimal(str(payment.amount or 0)).quantize(Decimal('0.01'))),
            'items': items_payload,
        }
        delivery_fee = Decimal(str(order.delivery_fee or 0))
        return payload

    def marking_code_allocation_summary(self, order: Order) -> Dict[str, Any]:
        summary = defaultdict(int)
        codes_by_item: Dict[int, List[str]] = defaultdict(list)
        for allocation in order.marking_code_allocations or []:
            action_value = allocation.action.value if hasattr(allocation.action, 'value') else str(allocation.action)
            summary[action_value] += 1
            if allocation.marking_code:
                codes_by_item[allocation.order_item_id].append(allocation.marking_code.code)
        return {
            'events': dict(summary),
            'codes_by_order_item': dict(codes_by_item),
        }

    def reserve_required_marking_codes(self, payment: Payment, *, actor_user_id: Optional[int] = None) -> Dict[str, Any]:
        order = payment.order
        if not order:
            return {'reserved': 0, 'skipped': True}

        method_value = payment.payment_method.value if hasattr(payment.payment_method, 'value') else payment.payment_method
        if method_value not in {PaymentMethod.CLICK.value, PaymentMethod.CARD.value}:
            return {'reserved': 0, 'skipped': True}

        reserved_count = 0
        for order_item in order.order_items or []:
            product = order_item.product
            if not product or not product.fiscalization_enabled or not product.requires_marking_codes:
                continue

            already_allocated = self._get_active_allocated_codes(payment, order_item)
            if len(already_allocated) >= int(order_item.quantity or 0):
                continue

            missing_count = int(order_item.quantity or 0) - len(already_allocated)
            available_codes = (
                ProductMarkingCode.query.filter_by(
                    product_id=order_item.product_id,
                    status=MarkingCodeStatus.AVAILABLE,
                )
                .order_by(ProductMarkingCode.created_at.asc(), ProductMarkingCode.id.asc())
                .with_for_update()
                .limit(missing_count)
                .all()
            )
            if len(available_codes) != missing_count:
                raise ValidationError(
                    f"Not enough marking codes for product {product.name}. Required: {missing_count}, available: {len(available_codes)}"
                )

            for code in available_codes:
                code.status = MarkingCodeStatus.RESERVED
                code.reserved_at = datetime.now(timezone.utc)
                db.session.add(
                    OrderItemMarkingCodeAllocation(
                        order_item_id=order_item.id,
                        order_id=order.id,
                        payment_id=payment.id,
                        product_marking_code_id=code.id,
                        action=MarkingCodeLedgerEventType.RESERVED,
                        actor_user_id=actor_user_id,
                        event_metadata={
                            'payment_method': method_value,
                            'order_number': order.order_number,
                        },
                    )
                )
                reserved_count += 1

        if reserved_count:
            self._log_payment_activity(
                payment,
                action='payment_marking_codes_reserved',
                description=f'Reserved {reserved_count} marking codes for payment {payment.id}',
                event_type=AuditEventType.PAYMENT_PROCESSED,
                severity=AuditSeverity.MEDIUM,
                actor_user_id=actor_user_id,
                additional_data={'reserved_marking_codes': reserved_count},
            )

        return {'reserved': reserved_count}

    def release_reserved_marking_codes(
        self,
        payment: Payment,
        *,
        reason: str,
        actor_user_id: Optional[int] = None,
    ) -> int:
        reserved_codes = self._get_reserved_codes(payment)
        released = 0
        for order_item, code in reserved_codes:
            if code.status != MarkingCodeStatus.RESERVED:
                continue
            code.status = MarkingCodeStatus.AVAILABLE
            code.reserved_at = None
            db.session.add(
                OrderItemMarkingCodeAllocation(
                    order_item_id=order_item.id,
                    order_id=order_item.order_id,
                    payment_id=payment.id,
                    product_marking_code_id=code.id,
                    action=MarkingCodeLedgerEventType.RELEASED,
                    actor_user_id=actor_user_id,
                    notes=reason,
                    event_metadata={'reason': reason},
                )
            )
            released += 1
        if released:
            self._log_payment_activity(
                payment,
                action='payment_marking_codes_released',
                description=f'Released {released} marking codes for payment {payment.id}',
                event_type=AuditEventType.PAYMENT_FAILED,
                severity=AuditSeverity.MEDIUM,
                actor_user_id=actor_user_id,
                additional_data={
                    'released_marking_codes': released,
                    'reason': reason,
                },
            )
        return released

    def _reserve_codes_for_manual_consumption(
        self,
        payment: Payment,
        *,
        actor_user_id: Optional[int] = None,
    ) -> None:
        order = payment.order
        if not order:
            return

        reserved_count = 0
        for order_item in order.order_items or []:
            product = order_item.product
            if not product or not product.requires_marking_codes:
                continue

            existing_codes = self._get_active_allocated_codes(payment, order_item)
            missing_count = int(order_item.quantity or 0) - len(existing_codes)
            if missing_count <= 0:
                continue

            available_codes = (
                ProductMarkingCode.query.filter_by(
                    product_id=order_item.product_id,
                    status=MarkingCodeStatus.AVAILABLE,
                )
                .order_by(ProductMarkingCode.created_at.asc(), ProductMarkingCode.id.asc())
                .with_for_update()
                .limit(missing_count)
                .all()
            )
            if len(available_codes) != missing_count:
                raise ValidationError(
                    f"Not enough marking codes for product {product.name}. Required: {missing_count}, available: {len(available_codes)}"
                )

            for code in available_codes:
                code.status = MarkingCodeStatus.RESERVED
                code.reserved_at = datetime.now(timezone.utc)
                db.session.add(
                    OrderItemMarkingCodeAllocation(
                        order_item_id=order_item.id,
                        order_id=order.id,
                        payment_id=payment.id,
                        product_marking_code_id=code.id,
                        action=MarkingCodeLedgerEventType.RESERVED,
                        actor_user_id=actor_user_id,
                        event_metadata={'mode': 'manual_business_account_consumption'},
                    )
                )
                reserved_count += 1

        if reserved_count:
            self._log_payment_activity(
                payment,
                action='payment_marking_codes_reserved',
                description=f'Reserved {reserved_count} marking codes for business account payment {payment.id}',
                event_type=AuditEventType.PAYMENT_PROCESSED,
                severity=AuditSeverity.MEDIUM,
                actor_user_id=actor_user_id,
                additional_data={
                    'reserved_marking_codes': reserved_count,
                    'mode': 'manual_business_account_consumption',
                },
            )

    def mark_reserved_codes_used(
        self,
        payment: Payment,
        fiscalization: PaymentFiscalization,
        *,
        actor_user_id: Optional[int] = None,
    ) -> int:
        used = 0
        for order_item, code in self._get_reserved_codes(payment):
            if code.status == MarkingCodeStatus.USED:
                continue
            code.status = MarkingCodeStatus.USED
            code.used_at = datetime.now(timezone.utc)
            db.session.add(
                OrderItemMarkingCodeAllocation(
                    order_item_id=order_item.id,
                    order_id=order_item.order_id,
                    payment_id=payment.id,
                    product_marking_code_id=code.id,
                    payment_fiscalization_id=fiscalization.id,
                    action=MarkingCodeLedgerEventType.USED,
                    actor_user_id=actor_user_id,
                    event_metadata={
                        'provider_name': fiscalization.provider_name,
                        'provider_receipt_id': fiscalization.provider_receipt_id,
                    },
                )
            )
            used += 1
        if used:
            self._log_payment_activity(
                payment,
                action='payment_marking_codes_used',
                description=f'Marked {used} marking codes as used for payment {payment.id}',
                event_type=AuditEventType.PAYMENT_PROCESSED,
                severity=AuditSeverity.MEDIUM,
                actor_user_id=actor_user_id,
                fiscalization=fiscalization,
                additional_data={'used_marking_codes': used},
            )
        return used

    def _reserved_code_lookup(self, payment: Payment) -> Dict[int, List[str]]:
        lookup: Dict[int, List[str]] = defaultdict(list)
        for order_item, code in self._get_reserved_codes(payment):
            lookup[order_item.id].append(code.code)
        return lookup

    def _get_reserved_codes(self, payment: Payment) -> List[Tuple[OrderItem, ProductMarkingCode]]:
        allocations = (
            OrderItemMarkingCodeAllocation.query.options(
                joinedload(OrderItemMarkingCodeAllocation.marking_code),
                joinedload(OrderItemMarkingCodeAllocation.order_item),
            )
            .filter(
                OrderItemMarkingCodeAllocation.payment_id == payment.id,
                OrderItemMarkingCodeAllocation.action == MarkingCodeLedgerEventType.RESERVED,
            )
            .order_by(OrderItemMarkingCodeAllocation.created_at.asc(), OrderItemMarkingCodeAllocation.id.asc())
            .all()
        )
        reserved_items: List[Tuple[OrderItem, ProductMarkingCode]] = []
        seen = set()
        for allocation in allocations:
            code = allocation.marking_code
            order_item = allocation.order_item
            if not code or not order_item:
                continue
            key = (order_item.id, code.id)
            if key in seen:
                continue
            if code.status in {MarkingCodeStatus.RESERVED, MarkingCodeStatus.USED}:
                reserved_items.append((order_item, code))
                seen.add(key)
        return reserved_items

    def _get_active_allocated_codes(self, payment: Payment, order_item: OrderItem) -> List[ProductMarkingCode]:
        allocations = (
            OrderItemMarkingCodeAllocation.query.options(joinedload(OrderItemMarkingCodeAllocation.marking_code))
            .filter(
                OrderItemMarkingCodeAllocation.payment_id == payment.id,
                OrderItemMarkingCodeAllocation.order_item_id == order_item.id,
                OrderItemMarkingCodeAllocation.action.in_(
                    [MarkingCodeLedgerEventType.RESERVED, MarkingCodeLedgerEventType.USED]
                ),
            )
            .order_by(OrderItemMarkingCodeAllocation.created_at.asc(), OrderItemMarkingCodeAllocation.id.asc())
            .all()
        )
        result: List[ProductMarkingCode] = []
        seen = set()
        for allocation in allocations:
            code = allocation.marking_code
            if not code or code.id in seen:
                continue
            if code.status in {MarkingCodeStatus.RESERVED, MarkingCodeStatus.USED}:
                result.append(code)
                seen.add(code.id)
        return result

    def _get_payment(self, payment_id: int) -> Payment:
        payment = Payment.query.options(
            joinedload(Payment.user),
            joinedload(Payment.fiscalization),
            joinedload(Payment.order)
            .joinedload(Order.order_items)
            .joinedload(OrderItem.product)
            .joinedload(Product.fiscal_profile),
        ).get(payment_id)
        if not payment:
            raise NotFoundError("Payment not found")
        return payment

    @staticmethod
    def _format_amount(value: Any) -> str:
        return f"{Decimal(str(value or 0)).quantize(Decimal('0.01'))}"

    @staticmethod
    def _build_commission_info(payment: Payment) -> Dict[str, Any]:
        # Comission info is meant of our company not the customers
        commission_info: Dict[str, Any] = {}
        commission_info['TIN'] = current_app.config.get('COMPANY_TIN')
        return commission_info
