"""Service for COD cash collection, receivable allocation, and debt rules."""

import logging
from datetime import datetime, UTC
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import func
from sqlalchemy.orm import contains_eager, joinedload

from business_app import db
from business_app.models.delivery import Delivery, DeliveryPerson
from business_app.models.order import Order
from business_app.models.payment import (
    CashCollectionAllocation,
    CashCollectionEvent,
    DriverCashSession,
    Payment,
)
from business_app.models.user import User
from business_app.utils.audit_logger import AuditEventType, AuditSeverity, audit_logger
from shared.enums import (
    CashCollectionSource,
    OrderStatus,
    PaymentMethod,
    PaymentStatus,
    UserRole,
)
from business_app.utils.exceptions import NotFoundError, ValidationError
from business_app.utils.payment_projection import get_payment_projection
from business_app.utils.state_validators import assert_cash_payment_collector


logger = logging.getLogger(__name__)


class CashCollectionService:
    """COD receivable and cash collection service."""

    COD_ACTIVE_DEBT_LIMIT = 2

    @staticmethod
    def _to_decimal(value: Any) -> Decimal:
        if value is None:
            return Decimal("0.00")
        return Decimal(str(value)).quantize(Decimal("0.01"))

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
        provider_data.setdefault("settlement_mode", "cash_on_delivery")
        if metadata:
            provider_data.update(metadata)
        if actor_user_id is not None:
            provider_data["actor_user_id"] = actor_user_id

        if not payment:
            payment = Payment(
                order_id=order.id,
                user_id=order.user_id,
                amount=order.total_amount,
                currency="UZS",
                payment_method=PaymentMethod.CASH,
                status=PaymentStatus.PENDING,
                description=f"Cash on delivery for order #{order.order_number}",
                provider_data=provider_data,
                amount_collected=Decimal("0.00"),
                outstanding_amount=order.total_amount,
            )
            db.session.add(payment)
            db.session.flush()
        else:
            payment.user_id = order.user_id
            payment.payment_method = PaymentMethod.CASH
            payment.amount = order.total_amount
            payment.currency = payment.currency or "UZS"
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
        # Admin-granted exemption for trusted customers takes precedence:
        # they may always use COD regardless of outstanding debts.
        # Grocery stores carry money debt by design and are also exempt
        # from the active-COD-debt cap.
        customer = User.query.get(customer_id)
        if customer and customer.cod_debt_check_exempt:
            return False
        if customer and customer.is_grocery_store:
            return False
        return self.get_active_cod_debt_count(customer_id) >= self.COD_ACTIVE_DEBT_LIMIT

    def get_cod_restriction_context(self, customer_id: int) -> Dict[str, Any]:
        active_debt_count = self.get_active_cod_debt_count(customer_id)
        customer = User.query.get(customer_id)
        is_cod_exempt = bool(customer and customer.cod_debt_check_exempt)
        is_grocery_store = bool(customer and customer.is_grocery_store)

        # Order matches is_customer_cod_restricted(): admin exemption first,
        # then structural grocery-store exemption, then the debt cap.
        if is_cod_exempt:
            is_restricted, reason = False, "customer_is_cod_exempt"
        elif is_grocery_store:
            is_restricted, reason = False, None
        elif active_debt_count >= self.COD_ACTIVE_DEBT_LIMIT:
            is_restricted, reason = True, "customer_has_max_active_cod_debts"
        else:
            is_restricted, reason = False, None

        return {
            "active_cod_debt_count": active_debt_count,
            "cod_restricted": is_restricted,
            "available_prepayment_balance": float(self.get_customer_prepaid_balance(customer_id)),
            "cod_restriction_reason": reason,
            "cod_exempt": is_cod_exempt,
        }

    def get_customer_prepaid_balance(self, customer_id: int) -> Decimal:
        """Return customer's unapplied COD over-collection balance."""
        total = db.session.query(func.coalesce(func.sum(CashCollectionEvent.unapplied_amount), Decimal("0.00"))).filter(
            CashCollectionEvent.customer_id == customer_id,
            CashCollectionEvent.voided_at.is_(None),
            CashCollectionEvent.unapplied_amount > 0,
        ).scalar() or Decimal("0.00")
        return self._to_decimal(total)

    def apply_customer_prepaid_credit_to_payment(self, payment: Payment) -> Payment:
        """Auto-apply unapplied customer cash credit to a COD payment."""
        if not payment:
            return payment
        if payment.payment_method != PaymentMethod.CASH:
            return payment
        if self._to_decimal(payment.outstanding_amount) <= Decimal("0.00"):
            return payment

        unapplied_events = (
            CashCollectionEvent.query.filter(
                CashCollectionEvent.customer_id == payment.user_id,
                CashCollectionEvent.voided_at.is_(None),
                CashCollectionEvent.unapplied_amount > 0,
            )
            .order_by(CashCollectionEvent.occurred_at.asc(), CashCollectionEvent.id.asc())
            .with_for_update(of=CashCollectionEvent)
            .all()
        )

        for event in unapplied_events:
            outstanding = self._to_decimal(payment.outstanding_amount)
            if outstanding <= Decimal("0.00"):
                break

            available = self._to_decimal(event.unapplied_amount)
            if available <= Decimal("0.00"):
                continue

            allocatable = min(available, outstanding)
            self._allocate_to_payment(
                event=event,
                payment=payment,
                amount=allocatable,
                allocation_order=self._next_allocation_order(event.id),
                allocation_mode="prepaid_credit",
                trigger_completion_notification=False,
            )

        return payment

    def reserve_customer_prepaid_credit_for_payment(
        self,
        payment: Payment,
        *,
        actor_user_id: Optional[int] = None,
    ) -> Decimal:
        """Reserve available customer COD prepayment for a pending COD order payment."""
        if not payment or payment.payment_method != PaymentMethod.CASH:
            return Decimal("0.00")

        outstanding = self._to_decimal(payment.outstanding_amount)
        existing_reserved = self._get_reserved_prepayment_amount(payment.id)
        remaining_capacity = max(Decimal("0.00"), outstanding - existing_reserved)
        if remaining_capacity <= Decimal("0.00"):
            self._sync_reserved_prepayment_projection(payment)
            return Decimal("0.00")

        unapplied_events = (
            CashCollectionEvent.query.filter(
                CashCollectionEvent.customer_id == payment.user_id,
                CashCollectionEvent.voided_at.is_(None),
                CashCollectionEvent.unapplied_amount > 0,
            )
            .order_by(CashCollectionEvent.occurred_at.asc(), CashCollectionEvent.id.asc())
            .with_for_update(of=CashCollectionEvent)
            .all()
        )

        total_reserved = Decimal("0.00")
        for event in unapplied_events:
            remaining = remaining_capacity - total_reserved
            if remaining <= Decimal("0.00"):
                break

            available = self._to_decimal(event.unapplied_amount)
            if available <= Decimal("0.00"):
                continue

            reservable = min(remaining, available)
            self._allocate_to_payment(
                event=event,
                payment=payment,
                amount=reservable,
                allocation_order=self._next_allocation_order(event.id),
                allocation_mode="prepaid_reservation",
                trigger_completion_notification=False,
                affect_payment_projection=False,
                allocation_metadata={
                    "reservation_state": "reserved",
                    "reserved_by_user_id": actor_user_id,
                },
            )
            total_reserved += reservable

        self._sync_reserved_prepayment_projection(payment)
        return self._to_decimal(total_reserved)

    def consume_reserved_prepayment_for_payment(
        self,
        payment: Payment,
        *,
        collected_at: Optional[datetime] = None,
    ) -> Decimal:
        """Convert reserved prepayment allocations into settled COD payment amounts."""
        if not payment or payment.payment_method != PaymentMethod.CASH:
            return Decimal("0.00")

        now = datetime.now(UTC)
        effective_collected_at = collected_at or now
        if effective_collected_at.tzinfo is None:
            effective_collected_at = effective_collected_at.replace(tzinfo=UTC)

        reservations = (
            CashCollectionAllocation.query.filter(
                CashCollectionAllocation.payment_id == payment.id,
                CashCollectionAllocation.reversed_at.is_(None),
                CashCollectionAllocation.allocation_mode == "prepaid_reservation",
            )
            .order_by(CashCollectionAllocation.allocated_at.asc(), CashCollectionAllocation.id.asc())
            .with_for_update(of=CashCollectionAllocation)
            .all()
        )

        consumed_total = Decimal("0.00")
        for allocation in reservations:
            amount = self._to_decimal(allocation.allocated_amount)
            if amount <= Decimal("0.00"):
                continue
            payment.amount_collected = self._to_decimal(payment.amount_collected) + amount
            consumed_total += amount
            allocation.allocation_mode = "prepaid_credit"
            metadata = dict(allocation.allocation_metadata or {})
            metadata["reservation_state"] = "consumed"
            metadata["reservation_consumed_at"] = now.isoformat()
            metadata["affects_payment_projection"] = True
            allocation.allocation_metadata = metadata

        if consumed_total > Decimal("0.00"):
            self.sync_payment_projection(payment, collected_at=effective_collected_at)

        self._sync_reserved_prepayment_projection(payment)
        return self._to_decimal(consumed_total)

    def release_reserved_prepayment_for_order(
        self,
        order_id: int,
        *,
        actor_user_id: Optional[int] = None,
        reason: Optional[str] = None,
    ) -> Decimal:
        """Release reserved prepayment back to customer balance for a non-delivered order."""
        payment = (
            Payment.query.options(
                joinedload(Payment.order),
                joinedload(Payment.cash_collection_allocations).joinedload(
                    CashCollectionAllocation.cash_collection_event
                ),
            )
            .filter_by(order_id=order_id)
            .first()
        )
        if not payment or payment.payment_method != PaymentMethod.CASH:
            return Decimal("0.00")

        if payment.order and payment.order.status == OrderStatus.DELIVERED:
            self._sync_reserved_prepayment_projection(payment)
            return Decimal("0.00")

        now = datetime.now(UTC)
        release_reason = reason or "Order was cancelled/returned before delivery"
        released_total = Decimal("0.00")

        for allocation in payment.cash_collection_allocations:
            if allocation.reversed_at or allocation.allocation_mode != "prepaid_reservation":
                continue
            amount = self._to_decimal(allocation.allocated_amount)
            event = allocation.cash_collection_event
            if event:
                event.unapplied_amount = self._to_decimal(event.unapplied_amount) + amount
            allocation.reversed_at = now
            allocation.reversed_by_user_id = actor_user_id
            allocation.reversal_reason = release_reason
            metadata = dict(allocation.allocation_metadata or {})
            metadata["reservation_state"] = "released"
            metadata["reservation_released_at"] = now.isoformat()
            metadata["affects_payment_projection"] = False
            allocation.allocation_metadata = metadata
            released_total += amount

        self._sync_reserved_prepayment_projection(payment)
        return self._to_decimal(released_total)

    RESERVABLE_ORDER_STATUSES = frozenset(
        {
            OrderStatus.PENDING,
            OrderStatus.CONFIRMED,
            OrderStatus.PREPARING,
            OrderStatus.OUT_FOR_DELIVERY,
        }
    )

    def auto_reserve_against_pending_payments(
        self,
        customer_id: int,
        *,
        actor_user_id: Optional[int] = None,
    ) -> Decimal:
        """Reserve any unapplied customer prepayment against the customer's
        non-delivered CASH payments (oldest-first). Idempotent. Best-effort:
        skips locked rows (Postgres only) so concurrent order creation doesn't
        block the sweep; the new order's own creation path retriggers reservation.
        """
        if self.get_customer_prepaid_balance(customer_id) <= Decimal("0.00"):
            return Decimal("0.00")

        query = (
            Payment.query.join(Order, Payment.order_id == Order.id)
            .options(contains_eager(Payment.order))
            .filter(
                Payment.user_id == customer_id,
                Payment.payment_method == PaymentMethod.CASH,
                Payment.outstanding_amount > Decimal("0.00"),
                Order.status.in_(self.RESERVABLE_ORDER_STATUSES),
            )
            .order_by(Order.created_at.asc(), Payment.id.asc())
        )

        # Lock payment rows so concurrent reservation/collection workflows
        # don't race against this sweep. Postgres supports skip_locked; SQLite
        # (used in unit tests) silently ignores the locking clause, so we only
        # apply it when the dialect actually understands it.
        if db.engine.dialect.name == "postgresql":
            # Count first (without lock) so we can detect rows silently
            # dropped by skip_locked when a concurrent transaction holds them.
            expected_count = query.order_by(None).with_entities(func.count(Payment.id)).scalar() or 0
            query = query.with_for_update(of=Payment, skip_locked=True)
            payments = query.all()
            actual_count = len(payments)
            if actual_count != expected_count:
                logger.warning(
                    "auto_reserve_against_pending_payments: skip_locked dropped "
                    "rows for customer_id=%s expected_count=%s actual_count=%s; "
                    "rows skipped due to concurrent locks; next post_collection "
                    "or order creation will retry reservation",
                    customer_id,
                    expected_count,
                    actual_count,
                )
        else:
            payments = query.all()

        total_reserved = Decimal("0.00")
        for payment in payments:
            if self.get_customer_prepaid_balance(customer_id) <= Decimal("0.00"):
                break
            reserved = self.reserve_customer_prepaid_credit_for_payment(
                payment,
                actor_user_id=actor_user_id,
            )
            total_reserved += self._to_decimal(reserved)

        return self._to_decimal(total_reserved)

    def _open_cod_debtors_query(self):
        """Grouped query of users with at least one open delivered COD debt.

        Shared by the admin limit-based list and the staff paginated list so
        the debt definition and ordering stay identical.
        """
        return (
            db.session.query(
                User.id.label("user_id"),
                User.first_name,
                User.last_name,
                User.phone,
                User.role,
                User.user_type,
                func.count(Payment.id).label("active_cod_debt_count"),
                func.coalesce(func.sum(Payment.outstanding_amount), Decimal("0.00")).label("total_outstanding_amount"),
            )
            .join(Payment, Payment.user_id == User.id)
            .join(Order, Order.id == Payment.order_id)
            .filter(
                Payment.payment_method == PaymentMethod.CASH,
                Payment.outstanding_amount > 0,
                Order.status == OrderStatus.DELIVERED,
            )
            .group_by(
                User.id,
                User.first_name,
                User.last_name,
                User.phone,
                User.role,
                User.user_type,
            )
            .order_by(
                func.sum(Payment.outstanding_amount).desc(),
                func.count(Payment.id).desc(),
                User.id.asc(),
            )
        )

    def _serialize_open_cod_debtor_row(self, row) -> Dict[str, Any]:
        active_count = int(row.active_cod_debt_count or 0)
        role_value = row.role.value if hasattr(row.role, "value") else row.role
        user_type_value = row.user_type.value if hasattr(row.user_type, "value") else row.user_type
        return {
            "id": int(row.user_id),
            "first_name": row.first_name,
            "last_name": row.last_name,
            "phone": row.phone,
            "role": role_value,
            "user_type": user_type_value,
            "active_cod_debt_count": active_count,
            "total_outstanding_amount": float(row.total_outstanding_amount or 0),
            "cod_restricted": active_count >= self.COD_ACTIVE_DEBT_LIMIT,
        }

    def list_users_with_open_cod_debts(self, *, limit: int = 200) -> List[Dict[str, Any]]:
        """Return users that currently have at least one open delivered COD debt."""
        safe_limit = max(1, min(int(limit or 200), 1000))
        rows = self._open_cod_debtors_query().limit(safe_limit).all()
        return [self._serialize_open_cod_debtor_row(row) for row in rows]

    def paginate_users_with_open_cod_debts(self, *, page: int = 1, per_page: int = 10) -> Dict[str, Any]:
        """Page through users with open delivered COD debts (staff bot list)."""
        safe_page = max(1, int(page or 1))
        safe_per_page = max(1, min(int(per_page or 10), 100))

        query = self._open_cod_debtors_query()
        total = query.count()
        pages = (total + safe_per_page - 1) // safe_per_page
        rows = query.offset((safe_page - 1) * safe_per_page).limit(safe_per_page).all()

        return {
            "items": [self._serialize_open_cod_debtor_row(row) for row in rows],
            "pagination": {
                "page": safe_page,
                "per_page": safe_per_page,
                "total": total,
                "pages": pages,
            },
        }

    def validate_customer_can_use_cod(self, customer_id: int) -> Dict[str, Any]:
        context = self.get_cod_restriction_context(customer_id)
        if context["cod_restricted"]:
            raise ValidationError(
                "Customer has reached the maximum number of active cash on delivery debts.",
                error_code="COD_DEBT_LIMIT_REACHED",
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
        total_outstanding = Decimal("0.00")
        total_reserved = Decimal("0.00")
        total_net_outstanding = Decimal("0.00")
        for payment in payments:
            outstanding_amount = self._to_decimal(payment.outstanding_amount)
            total_outstanding += outstanding_amount
            reserved_amount = self._to_decimal(
                (payment.provider_data or {}).get("cod_prepayment_reserved_amount", 0) or 0
            )
            net_outstanding = max(Decimal("0.00"), outstanding_amount - reserved_amount)
            total_reserved += reserved_amount
            total_net_outstanding += net_outstanding
            items.append(
                {
                    "payment_id": payment.id,
                    "order_id": payment.order_id,
                    "order_number": payment.order.order_number if payment.order else None,
                    "order_status": (
                        payment.order.status.value
                        if payment.order and hasattr(payment.order.status, "value")
                        else getattr(payment.order, "status", None)
                    ),
                    "amount": float(payment.amount or 0),
                    "amount_collected": float(payment.amount_collected or 0),
                    "outstanding_amount": float(outstanding_amount),
                    "reserved_prepayment_amount": float(reserved_amount),
                    "net_outstanding_amount": float(net_outstanding),
                    "status": payment.status.value if hasattr(payment.status, "value") else payment.status,
                    "created_at": payment.created_at.isoformat() if payment.created_at else None,
                    "paid_at": payment.paid_at.isoformat() if payment.paid_at else None,
                }
            )

        # For grocery stores, surface the headline contract debt (money mode)
        # alongside per-payment outstandings so the bot/admin can show the full
        # picture. Workplace and individual customers leave these as None.
        grocery_debt: Optional[Dict[str, Any]] = None
        if customer.is_grocery_store:
            try:
                from business_app.services.corporate_contract_service import CorporateContractService

                corporate_service = CorporateContractService()
                contract = corporate_service.get_active_amount_contract_for_user(customer.id)
                if contract and contract.prepayment_account:
                    account = contract.prepayment_account
                    grocery_debt = {
                        "contract_id": contract.id,
                        "currency": contract.currency,
                        "outstanding_amount": float(account.outstanding_amount or 0),
                        "lifetime_charged": float(account.lifetime_charged or 0),
                        "lifetime_collected": float(account.lifetime_collected or 0),
                        "last_charged_at": account.last_charged_at.isoformat() if account.last_charged_at else None,
                        "last_collected_at": (
                            account.last_collected_at.isoformat() if account.last_collected_at else None
                        ),
                    }
            except Exception:
                # Defensive: never fail the COD statement just because debt
                # lookup hit an edge case. Log via audit if needed.
                grocery_debt = None

        # Compute once and reuse: available_prepayment_balance and
        # unreserved_prepayment_balance are aliases of the same value, so we
        # must avoid issuing two identical SUM queries here.
        unreserved_balance = float(self.get_customer_prepaid_balance(customer_id))

        return {
            "customer_id": customer_id,
            "entity_subtype": (
                customer.entity_subtype.value
                if customer.entity_subtype is not None and hasattr(customer.entity_subtype, "value")
                else customer.entity_subtype
            ),
            "active_cod_debt_count": self.get_active_cod_debt_count(customer_id),
            "cod_restricted": self.is_customer_cod_restricted(customer_id),
            "total_outstanding_amount": float(total_outstanding),
            # Alias of total_outstanding_amount; named for UI clarity so the
            # admin modal can show gross vs. net side by side.
            "gross_outstanding_amount": float(total_outstanding),
            "reserved_prepayment_total": float(total_reserved),
            "net_outstanding_amount": float(total_net_outstanding),
            "available_prepayment_balance": unreserved_balance,
            # get_customer_prepaid_balance already returns unreserved balance
            # (reservations decrement the event's unapplied_amount). Exposed
            # under a clearer name for the UI.
            "unreserved_prepayment_balance": unreserved_balance,
            "grocery_debt": grocery_debt,
            "items": items,
        }

    def get_customer_prepayment_history(
        self,
        customer_id: int,
        *,
        include_voided: bool = True,
        include_fully_applied: bool = True,
        limit: int = 200,
    ) -> Dict[str, Any]:
        """Return a customer's full COD cash-collection ledger with allocations.

        The result powers the admin "Customer Prepayments" view. It surfaces every
        cash collection event for the customer alongside the allocations that
        consumed (or are reserving) each event, plus aggregate totals.

        Args:
            customer_id: The customer's user id.
            include_voided: Include voided events when True (default). The UI
                visually mutes them.
            include_fully_applied: Include events whose ``unapplied_amount`` is 0
                (i.e. fully consumed) when True. Default True so admins see the
                complete history; pass False to focus on events with credit left.
            limit: Maximum number of events to return (clamped 1..1000).
        """
        customer = User.query.get(customer_id)
        if not customer:
            raise NotFoundError("Customer not found")

        safe_limit = max(1, min(int(limit or 200), 1000))

        query = CashCollectionEvent.query.options(
            joinedload(CashCollectionEvent.allocations)
            .joinedload(CashCollectionAllocation.payment)
            .joinedload(Payment.order),
            joinedload(CashCollectionEvent.order),
        ).filter(CashCollectionEvent.customer_id == customer_id)

        if not include_voided:
            query = query.filter(CashCollectionEvent.voided_at.is_(None))
        if not include_fully_applied:
            query = query.filter(CashCollectionEvent.unapplied_amount > 0)

        events = (
            query.order_by(
                CashCollectionEvent.occurred_at.desc(),
                CashCollectionEvent.id.desc(),
            )
            .limit(safe_limit)
            .all()
        )

        # Lifetime aggregates are computed without limit/filters so the headline
        # numbers always reflect the customer's full COD history. Voided events
        # are excluded (they did not actually collect cash).
        lifetime_row = (
            db.session.query(
                func.coalesce(func.sum(CashCollectionEvent.amount), Decimal("0.00")).label("lifetime_collected"),
                func.coalesce(func.sum(CashCollectionEvent.unapplied_amount), Decimal("0.00")).label(
                    "lifetime_unapplied"
                ),
            )
            .filter(
                CashCollectionEvent.customer_id == customer_id,
                CashCollectionEvent.voided_at.is_(None),
            )
            .one()
        )
        lifetime_collected = self._to_decimal(lifetime_row.lifetime_collected)
        lifetime_unapplied = self._to_decimal(lifetime_row.lifetime_unapplied)
        lifetime_applied = lifetime_collected - lifetime_unapplied
        if lifetime_applied < Decimal("0.00"):
            # Defensive: allocations cannot exceed collections, but keep the
            # public field non-negative if a data anomaly slips through.
            lifetime_applied = Decimal("0.00")

        serialized_events: List[Dict[str, Any]] = []
        for event in events:
            allocations_payload: List[Dict[str, Any]] = []
            for allocation in sorted(
                event.allocations or [],
                key=lambda a: (a.allocated_at or datetime.now(UTC), a.id or 0),
            ):
                payment = allocation.payment
                order = payment.order if payment else None
                allocations_payload.append(
                    {
                        "id": allocation.id,
                        "payment_id": allocation.payment_id,
                        "order_id": allocation.order_id,
                        "order_number": order.order_number if order else None,
                        "order_status": (
                            order.status.value
                            if order and hasattr(order.status, "value")
                            else getattr(order, "status", None)
                        ),
                        "allocated_amount": float(allocation.allocated_amount or 0),
                        "allocation_mode": allocation.allocation_mode,
                        "allocated_at": (allocation.allocated_at.isoformat() if allocation.allocated_at else None),
                        "reversed_at": (allocation.reversed_at.isoformat() if allocation.reversed_at else None),
                        "reversal_reason": allocation.reversal_reason,
                    }
                )

            serialized_events.append(
                {
                    "id": event.id,
                    "event_id": event.event_id,
                    "amount": float(event.amount or 0),
                    "unapplied_amount": float(event.unapplied_amount or 0),
                    "currency": event.currency,
                    "source": (event.source.value if hasattr(event.source, "value") else event.source),
                    "occurred_at": event.occurred_at.isoformat() if event.occurred_at else None,
                    "notes": event.notes,
                    "voided_at": event.voided_at.isoformat() if event.voided_at else None,
                    "void_reason": event.void_reason,
                    "collector_user_id": event.collector_user_id,
                    "recorded_by_user_id": event.recorded_by_user_id,
                    "order_id": event.order_id,
                    "order_number": event.order.order_number if event.order else None,
                    "allocations": allocations_payload,
                }
            )

        return {
            "customer_id": customer_id,
            "first_name": customer.first_name,
            "last_name": customer.last_name,
            "phone": customer.phone,
            "available_prepayment_balance": float(self.get_customer_prepaid_balance(customer_id)),
            "lifetime_collected": float(lifetime_collected),
            "lifetime_applied": float(lifetime_applied),
            "events": serialized_events,
        }

    def list_customers_with_prepayment_balance(
        self,
        *,
        limit: int = 200,
        search: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Return customers carrying an unapplied COD over-collection balance.

        Mirrors :meth:`list_users_with_open_cod_debts` but aggregates
        ``unapplied_amount`` from non-voided ``CashCollectionEvent`` rows.
        """
        safe_limit = max(1, min(int(limit or 200), 1000))

        balance_expr = func.coalesce(func.sum(CashCollectionEvent.unapplied_amount), Decimal("0.00"))
        last_collection_expr = func.max(CashCollectionEvent.occurred_at)

        query = (
            db.session.query(
                User.id.label("user_id"),
                User.first_name,
                User.last_name,
                User.phone,
                User.role,
                User.user_type,
                balance_expr.label("available_prepayment_balance"),
                last_collection_expr.label("last_collection_at"),
            )
            .join(CashCollectionEvent, CashCollectionEvent.customer_id == User.id)
            .filter(
                CashCollectionEvent.voided_at.is_(None),
                CashCollectionEvent.unapplied_amount > 0,
            )
        )

        if search:
            normalized = f"%{search.strip().lower()}%"
            query = query.filter(
                db.or_(
                    func.lower(User.first_name).like(normalized),
                    func.lower(User.last_name).like(normalized),
                    func.lower(User.phone).like(normalized),
                )
            )

        rows = (
            query.group_by(
                User.id,
                User.first_name,
                User.last_name,
                User.phone,
                User.role,
                User.user_type,
            )
            .order_by(balance_expr.desc(), last_collection_expr.desc(), User.id.asc())
            .limit(safe_limit)
            .all()
        )

        items: List[Dict[str, Any]] = []
        for row in rows:
            role_value = row.role.value if hasattr(row.role, "value") else row.role
            user_type_value = row.user_type.value if hasattr(row.user_type, "value") else row.user_type
            items.append(
                {
                    "id": int(row.user_id),
                    "first_name": row.first_name,
                    "last_name": row.last_name,
                    "phone": row.phone,
                    "role": role_value,
                    "user_type": user_type_value,
                    "available_prepayment_balance": float(row.available_prepayment_balance or 0),
                    "last_collection_at": (row.last_collection_at.isoformat() if row.last_collection_at else None),
                }
            )
        return items

    def get_order_payment_timeline(self, order_id: int) -> Dict[str, Any]:
        order = Order.query.options(joinedload(Order.payment)).get(order_id)
        if not order:
            raise NotFoundError("Order not found")

        payment = order.payment
        if not payment:
            return {
                "order_id": order_id,
                "order_number": order.order_number,
                "timeline": [],
            }

        payment_projection = get_payment_projection(payment)
        timeline = [
            {
                "type": "payment_created",
                "timestamp": payment.created_at.isoformat() if payment.created_at else None,
                "amount": float(payment_projection["amount"]),
                "amount_collected": float(payment_projection["amount_collected"]),
                "outstanding_amount": float(payment_projection["outstanding_amount"]),
                "status": payment.status.value if hasattr(payment.status, "value") else payment.status,
            }
        ]

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
            timeline.append(
                {
                    "type": "cash_collection_allocation",
                    "timestamp": allocation.allocated_at.isoformat() if allocation.allocated_at else None,
                    "allocated_amount": float(allocation.allocated_amount or 0),
                    "allocation_mode": allocation.allocation_mode,
                    "collection_event_id": allocation.cash_collection_event_id,
                    "collection_amount": float(event.amount or 0) if event else None,
                    "collection_source": (
                        event.source.value
                        if event and hasattr(event.source, "value")
                        else getattr(event, "source", None)
                    ),
                    "delivery_id": event.delivery_id if event else None,
                    "notes": event.notes if event else None,
                    "reversed_at": allocation.reversed_at.isoformat() if allocation.reversed_at else None,
                }
            )

        return {
            "order_id": order_id,
            "order_number": order.order_number,
            "payment_id": payment.id,
            "amount": float(payment_projection["amount"]),
            "amount_collected": float(payment_projection["amount_collected"]),
            "outstanding_amount": float(payment_projection["outstanding_amount"]),
            "status": payment.status.value if hasattr(payment.status, "value") else payment.status,
            "timeline": timeline,
        }

    def sync_payment_projection(
        self,
        payment: Payment,
        *,
        collected_at: Optional[datetime] = None,
    ) -> Payment:
        amount = self._to_decimal(payment.amount)
        amount_collected = max(Decimal("0.00"), self._to_decimal(payment.amount_collected))
        amount_collected = min(amount, amount_collected)
        payment.amount_collected = amount_collected
        payment.outstanding_amount = max(Decimal("0.00"), amount - amount_collected)

        if payment.outstanding_amount <= Decimal("0.00"):
            payment.status = PaymentStatus.COMPLETED
            payment.paid_at = collected_at or payment.paid_at or datetime.now(UTC)
        elif payment.amount_collected > Decimal("0.00"):
            payment.status = PaymentStatus.PARTIALLY_PAID
            payment.paid_at = None
        else:
            payment.status = PaymentStatus.PENDING
            payment.paid_at = None

        if payment.amount_collected > Decimal("0.00"):
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
        allocation_mode: str = "auto",
        idempotency_key: Optional[str] = None,
        commit: bool = True,
        bypass_driver_block_check: bool = False,
    ) -> CashCollectionEvent:
        customer = User.query.get(customer_id)
        if not customer:
            raise NotFoundError("Customer not found")

        normalized_amount = self._to_decimal(amount)
        if normalized_amount < Decimal("0.00"):
            raise ValidationError("Collection amount cannot be negative")
        if normalized_amount == Decimal("0.00") and not notes:
            raise ValidationError("Notes are required when no cash is collected")

        if idempotency_key:
            existing_event = CashCollectionEvent.query.filter_by(idempotency_key=idempotency_key).first()
            if existing_event:
                return existing_event

        source_enum = self._normalize_source(source)
        occurred_at = occurred_at or datetime.now(UTC)
        if occurred_at.tzinfo is None:
            occurred_at = occurred_at.replace(tzinfo=UTC)

        target_payment: Optional[Payment] = None
        self._validate_collection_context(
            customer_id=customer_id,
            source=source_enum,
            collector_user_id=collector_user_id,
            recorded_by_user_id=recorded_by_user_id,
            order_id=order_id,
            delivery_id=delivery_id,
            driver_cash_session_id=driver_cash_session_id,
            notes=notes,
            manual_allocations=manual_allocations,
            bypass_driver_block_check=bypass_driver_block_check,
        )
        if source_enum == CashCollectionSource.PERSONAL_CARD_TRANSFER:
            target_payment = self._resolve_target_payment_for_personal_card_transfer(
                order_id=order_id,
                actor_user_id=recorded_by_user_id,
            )

        event = CashCollectionEvent(
            customer_id=customer_id,
            collector_user_id=collector_user_id,
            recorded_by_user_id=recorded_by_user_id,
            order_id=order_id,
            delivery_id=delivery_id,
            driver_cash_session_id=driver_cash_session_id,
            amount=normalized_amount,
            currency="UZS",
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
            )
            event.driver_cash_session_id = session.id

        allocations = list(manual_allocations or [])
        if source_enum == CashCollectionSource.PERSONAL_CARD_TRANSFER:
            allocatable = min(
                self._to_decimal(event.unapplied_amount),
                self._to_decimal(target_payment.outstanding_amount if target_payment else 0),
            )
            if allocatable > Decimal("0.00") and target_payment:
                self._allocate_to_payment(
                    event=event,
                    payment=target_payment,
                    amount=allocatable,
                    allocation_order=1,
                    allocation_mode="manual",
                    allocation_metadata={"allocation_origin": CashCollectionSource.PERSONAL_CARD_TRANSFER.value},
                )
        elif allocations:
            allocation_order = 0
            for allocation in allocations:
                allocation_order += 1
                payment = Payment.query.with_for_update(of=Payment).get(allocation["payment_id"])
                if not payment:
                    raise NotFoundError("Payment not found for manual allocation")
                allocated_amount = self._to_decimal(allocation.get("amount"))
                self._allocate_to_payment(
                    event=event,
                    payment=payment,
                    amount=allocated_amount,
                    allocation_order=allocation_order,
                    allocation_mode="manual",
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

            session = DriverCashSession.query.get(event.driver_cash_session_id)

            if session:
                DriverReconciliationService().refresh_expected_cash(session)

        self._refresh_legacy_cash_projections(
            delivery_id=event.delivery_id,
            collector_user_id=event.collector_user_id,
        )

        # Sweep any leftover unapplied prepayment onto the customer's
        # non-delivered CASH payments so the next driver/admin sees the right
        # cash-to-collect figure and the customer modal shows the net debt.
        if self._to_decimal(event.unapplied_amount) > Decimal("0.00"):
            self.auto_reserve_against_pending_payments(
                customer_id,
                actor_user_id=recorded_by_user_id or collector_user_id,
            )

        # Mirror collected money onto the grocery-store contract debt ledger.
        # One COLLECT entry per cash event covers the full amount: outstanding_amount
        # decreases by the collected sum regardless of how it allocates across
        # individual order Payments. Residual cash (unapplied_amount > 0) takes
        # the contract balance into credit territory (negative outstanding_amount).
        if customer.is_grocery_store and normalized_amount > Decimal("0.00"):
            from business_app.services.corporate_contract_service import CorporateContractService

            corporate_service = CorporateContractService()
            amount_contract = corporate_service.get_active_amount_contract_for_user(customer.id)
            if amount_contract:
                corporate_service.record_money_collection(
                    contract=amount_contract,
                    amount=normalized_amount,
                    source=source_enum.value,
                    order_id=order_id,
                    delivery_id=delivery_id,
                    cash_event_id=event.id,
                    actor_user_id=recorded_by_user_id or collector_user_id,
                    notes=notes,
                )
            elif order_id is not None:
                # Legacy grocery-store users on a UNITS-mode contract: mirror the
                # cash as TOPUP entries matching the CONSUME entries written at
                # delivery. Standalone collections (no order_id) deliberately
                # skip this path; TOPUP requires per-product CONSUME context.
                from business_app.models.corporate import CorporateContract
                from business_app.models.order import OrderItem
                from shared.enums import CorporateContractTrackingMode

                contract_ids = {
                    row.contract_id
                    for row in OrderItem.query.filter(
                        OrderItem.order_id == order_id,
                        OrderItem.contract_id.isnot(None),
                    ).all()
                }
                if contract_ids:
                    units_contracts = CorporateContract.query.filter(
                        CorporateContract.id.in_(contract_ids),
                        CorporateContract.tracking_mode == CorporateContractTrackingMode.UNITS,
                    ).all()
                    for units_contract in units_contracts:
                        corporate_service.topup_from_cash_collection(
                            contract=units_contract,
                            order_id=order_id,
                            cash_event_id=event.id,
                            delivery_id=delivery_id,
                            actor_user_id=recorded_by_user_id or collector_user_id,
                            source=source_enum.value,
                            notes=notes,
                        )

        audit_logger.log_event(
            event_type=AuditEventType.PAYMENT_PROCESSED,
            action="cash_collection_posted",
            severity=AuditSeverity.MEDIUM,
            resource_type="cash_collection_event",
            resource_id=str(event.id),
            additional_data={
                "customer_id": customer_id,
                "collector_user_id": collector_user_id,
                "order_id": order_id,
                "delivery_id": delivery_id,
                "amount": float(normalized_amount),
                "unapplied_amount": float(event.unapplied_amount or 0),
                "source": source_enum.value,
            },
        )

        if commit:
            db.session.commit()
        else:
            db.session.flush()
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
        driver_cash_session_id: Optional[int],
        notes: Optional[str],
        manual_allocations: Optional[Iterable[Dict[str, Any]]],
        bypass_driver_block_check: bool = False,
    ) -> None:
        if source == CashCollectionSource.PERSONAL_CARD_TRANSFER:
            if order_id is None:
                raise ValidationError("order_id is required for personal card transfer collections")
            if not notes:
                raise ValidationError("Notes are required for personal card transfer collections")
            if recorded_by_user_id is None:
                raise ValidationError("recorded_by_user_id is required for personal card transfer collections")
            if collector_user_id is not None:
                raise ValidationError("collector_user_id is not allowed for personal card transfer collections")
            if delivery_id is not None:
                raise ValidationError("delivery_id is not allowed for personal card transfer collections")
            if driver_cash_session_id is not None:
                raise ValidationError("driver_cash_session_id is not allowed for personal card transfer collections")
            if manual_allocations:
                raise ValidationError("manual_allocations are not allowed for personal card transfer collections")
        elif source == CashCollectionSource.BACKFILL and collector_user_id and driver_cash_session_id is None:
            raise ValidationError("driver_cash_session_id is required for driver cash backfill collections")
        if source == CashCollectionSource.BACKFILL and not notes:
            raise ValidationError("Notes are required for backfill collections")

        target_session = None
        if driver_cash_session_id is not None:
            target_session = DriverCashSession.query.get(driver_cash_session_id)
            if not target_session:
                raise NotFoundError("Driver cash session not found")
            if collector_user_id and target_session.driver_user_id != collector_user_id:
                raise ValidationError("driver_cash_session_id does not belong to the selected collector")

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
            staff_roles = getattr(collector, "staff_roles", []) or []
            if isinstance(staff_roles, str):
                staff_roles = [role.strip().strip("\"'") for role in staff_roles.strip("[]").split(",") if role.strip()]
            role_values = {getattr(collector.role, "value", collector.role)}
            role_values.update(staff_roles)
            if UserRole.DELIVERY_DRIVER.value not in role_values:
                raise ValidationError("Collector must be an authorized delivery driver")

            from business_app.services.driver_reconciliation_service import DriverReconciliationService

            if not bypass_driver_block_check and DriverReconciliationService().is_driver_blocked_from_cod(
                collector_user_id
            ):
                raise ValidationError(
                    "Driver is blocked from new cash on delivery collections until reconciliation issues are resolved",
                    error_code="COD_DRIVER_BLOCKED",
                )

        if order_id:
            order = Order.query.get(order_id)
            if not order:
                raise NotFoundError("Order not found")
            if order.user_id != customer_id:
                raise ValidationError("Order does not belong to the selected customer")
            _electronic_methods = {PaymentMethod.CLICK, PaymentMethod.PAYME, PaymentMethod.CARD}
            if order.payment_method != PaymentMethod.CASH:
                # PERSONAL_CARD_TRANSFER and ADMIN_ADJUSTMENT may target a
                # non-CASH order. The former records a card→owner transfer;
                # the latter records a customer-credit prepayment that the
                # order-edit cascade creates when an admin reduces a card-
                # paid order (the card is never refunded — the value lives
                # as cash-only-usable customer credit).
                if (
                    not (
                        source == CashCollectionSource.PERSONAL_CARD_TRANSFER
                        and order.payment_method in _electronic_methods
                    )
                    and source != CashCollectionSource.ADMIN_ADJUSTMENT
                ):
                    raise ValidationError("Only COD orders can be targeted for COD collections")
            order_status = order.status.value if hasattr(order.status, "value") else str(order.status or "")
            if source == CashCollectionSource.PERSONAL_CARD_TRANSFER:
                if order_status in {OrderStatus.CANCELLED.value, OrderStatus.RETURNED.value}:
                    raise ValidationError(
                        "Cancelled or returned COD orders cannot be targeted for personal card transfer collection"
                    )
            elif source == CashCollectionSource.ADMIN_ADJUSTMENT:
                # Admin adjustments may target any order — they're already
                # gated by admin permission and an OrderEditHistory audit row.
                pass
            elif order_status != OrderStatus.DELIVERED.value:
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
                payment_id = allocation.get("payment_id")
                payment = Payment.query.options(joinedload(Payment.order)).get(payment_id)
                if not payment:
                    raise NotFoundError("Payment not found for manual allocation")
                if payment.user_id != customer_id:
                    raise ValidationError("Manual allocations must belong to the selected customer")
                if payment.payment_method != PaymentMethod.CASH:
                    raise ValidationError("Manual allocations can only target COD payments")
                if payment.order and payment.order.status != OrderStatus.DELIVERED:
                    raise ValidationError("Manual allocations can only target delivered COD orders")
                if self._to_decimal(payment.outstanding_amount) <= Decimal("0.00"):
                    raise ValidationError("Manual allocations can only target payments with outstanding balance")

        if source == CashCollectionSource.ADMIN_ADJUSTMENT and not recorded_by_user_id:
            raise ValidationError("recorded_by_user_id is required for admin adjustments")

    def _resolve_target_payment_for_personal_card_transfer(
        self,
        *,
        order_id: Optional[int],
        actor_user_id: Optional[int],
    ) -> Payment:
        if order_id is None:
            raise ValidationError("order_id is required for personal card transfer collections")

        order = Order.query.options(joinedload(Order.payment)).get(order_id)
        if not order:
            raise NotFoundError("Order not found")

        _electronic_methods = {PaymentMethod.CLICK, PaymentMethod.PAYME, PaymentMethod.CARD}
        if order.payment_method in _electronic_methods:
            payment = order.payment
            if not payment or payment.status != PaymentStatus.PENDING:
                raise ValidationError(
                    "Only orders with a pending electronic payment can be converted to a personal card payment"
                )

            # Release any marking codes reserved during Click PREPARE — fiscalization must not apply to personal card payments  # noqa: E501
            from business_app.services.payment_fiscalization_service import PaymentFiscalizationService

            try:
                PaymentFiscalizationService().release_reserved_marking_codes(
                    payment,
                    reason="converted_to_cash_personal_card",
                    actor_user_id=actor_user_id,
                )
            except Exception as exc:
                logger.error("Failed to release marking codes for order %s: %s", order.id, exc)

            # Convert payment and order to CASH in-place
            payment.payment_method = PaymentMethod.CASH
            order.payment_method = PaymentMethod.CASH
            db.session.flush()

            locked_payment = Payment.query.with_for_update(of=Payment).get(payment.id)
            if not locked_payment:
                raise NotFoundError("Payment not found")
            return locked_payment

        if order.payment_method != PaymentMethod.CASH:
            raise ValidationError("Only COD orders can be targeted for personal card transfer collection")

        payment = order.payment
        if not payment:
            payment = self.ensure_cod_payment_for_order(
                order,
                actor_user_id=actor_user_id,
                metadata={"collection_origin": CashCollectionSource.PERSONAL_CARD_TRANSFER.value},
            )
            db.session.flush()

        locked_payment = Payment.query.with_for_update(of=Payment).get(payment.id)
        if not locked_payment:
            raise NotFoundError("Payment not found")
        return locked_payment

    def reverse_collection_event(
        self,
        event_id: int,
        *,
        reversed_by_user_id: int,
        reason: str,
        commit: bool = True,
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
            if self._allocation_affects_payment_projection(allocation):
                payment.amount_collected = self._to_decimal(payment.amount_collected) - self._to_decimal(
                    allocation.allocated_amount
                )
                self.sync_payment_projection(payment)
            else:
                self._sync_reserved_prepayment_projection(payment)

        event.unapplied_amount = self._to_decimal(event.amount)
        event.voided_at = now
        event.voided_by_user_id = reversed_by_user_id
        event.void_reason = reason

        if event.driver_cash_session_id:
            from business_app.services.driver_reconciliation_service import DriverReconciliationService
            from business_app.models.payment import DriverCashSession

            session = DriverCashSession.query.get(event.driver_cash_session_id)
            if session:
                DriverReconciliationService().refresh_expected_cash(session)

        self._refresh_legacy_cash_projections(
            delivery_id=event.delivery_id,
            collector_user_id=event.collector_user_id,
        )

        audit_logger.log_event(
            event_type=AuditEventType.PAYMENT_REFUNDED,
            action="cash_collection_reversed",
            severity=AuditSeverity.HIGH,
            resource_type="cash_collection_event",
            resource_id=str(event.id),
            additional_data={
                "reason": reason,
                "reversed_by_user_id": reversed_by_user_id,
            },
        )

        if commit:
            db.session.commit()
        else:
            db.session.flush()
        return event

    ADJUSTABLE_SESSION_STATUSES = frozenset({"submitted", "partial", "mismatch", "overdue"})

    def adjust_event_amount(
        self,
        event_id: int,
        *,
        new_amount: Any,
        adjusted_by_user_id: int,
        reason: str,
    ) -> CashCollectionEvent:
        """Admin correction for a recorded cash collection.

        Voids the original event (reversing any allocations including
        downstream prepayment auto-application) and creates a replacement
        carrying the same context with the corrected amount. Cross-linked
        via entry_metadata so the audit trail survives.
        """
        normalized_amount = self._to_decimal(new_amount)
        if normalized_amount <= Decimal("0.00"):
            raise ValidationError("Adjusted amount must be positive")
        reason = (reason or "").strip()
        if not reason:
            raise ValidationError("An adjustment reason is required")

        event = CashCollectionEvent.query.with_for_update(of=CashCollectionEvent).get(event_id)
        if not event:
            raise NotFoundError("Cash collection event not found")
        if event.voided_at:
            raise ValidationError("Cannot adjust a voided cash collection event")

        if event.driver_cash_session_id:
            session = DriverCashSession.query.get(event.driver_cash_session_id)
            if session:
                status_value = getattr(session.status, "value", session.status)
                if status_value not in self.ADJUSTABLE_SESSION_STATUSES:
                    raise ValidationError(f"Cannot adjust event on session with status '{status_value}'")

        original_amount = self._to_decimal(event.amount)
        original_context = {
            "customer_id": event.customer_id,
            "collector_user_id": event.collector_user_id,
            "recorded_by_user_id": event.recorded_by_user_id,
            "order_id": event.order_id,
            "delivery_id": event.delivery_id,
            "driver_cash_session_id": event.driver_cash_session_id,
            "source": event.source,
            "occurred_at": event.occurred_at,
            "notes": event.notes,
            "proof_data": dict(event.proof_data or {}),
        }

        self.reverse_collection_event(
            event.id,
            reversed_by_user_id=adjusted_by_user_id,
            reason=f"Amount adjustment: {reason}",
            commit=False,
        )

        existing_metadata = dict(event.entry_metadata or {})
        replacement_proof = dict(original_context["proof_data"])
        replacement_proof["adjustment_source"] = "admin_correction"
        replacement_proof["original_event_id"] = event.id

        replacement = self.post_collection(
            customer_id=original_context["customer_id"],
            amount=normalized_amount,
            source=original_context["source"],
            collector_user_id=original_context["collector_user_id"],
            recorded_by_user_id=adjusted_by_user_id,
            order_id=original_context["order_id"],
            delivery_id=original_context["delivery_id"],
            driver_cash_session_id=original_context["driver_cash_session_id"],
            notes=original_context["notes"],
            proof_data=replacement_proof,
            occurred_at=original_context["occurred_at"],
            commit=False,
            bypass_driver_block_check=True,
        )

        replacement_metadata = dict(replacement.entry_metadata or {})
        replacement_metadata.update(
            {
                "adjustment_source": "admin_correction",
                "original_event_id": event.id,
                "adjusted_by_user_id": adjusted_by_user_id,
                "adjustment_reason": reason,
                "original_amount": float(original_amount),
            }
        )
        replacement.entry_metadata = replacement_metadata

        existing_metadata.update(
            {
                "adjusted_replacement_event_id": replacement.id,
                "adjustment_reason": reason,
            }
        )
        event.entry_metadata = existing_metadata

        audit_logger.log_event(
            event_type=AuditEventType.PAYMENT_PROCESSED,
            action="cash_collection_amount_adjusted",
            severity=AuditSeverity.HIGH,
            resource_type="cash_collection_event",
            resource_id=str(event.id),
            additional_data={
                "adjusted_by_user_id": adjusted_by_user_id,
                "original_amount": float(original_amount),
                "new_amount": float(normalized_amount),
                "replacement_event_id": replacement.id,
                "reason": reason,
                "driver_cash_session_id": event.driver_cash_session_id,
            },
        )

        db.session.commit()
        return replacement

    def _allocate_oldest_first(
        self,
        *,
        event: CashCollectionEvent,
        customer_id: int,
        order_id: Optional[int],
        allocation_mode: str,
    ) -> None:
        if self._to_decimal(event.amount) <= Decimal("0.00"):
            return

        allocation_order = 0
        payments = self.get_active_cod_payments_for_customer(customer_id, for_update=True)

        if order_id:
            current_order_payment = (
                Payment.query.options(joinedload(Payment.order)).filter_by(order_id=order_id).first()
            )
            if (
                current_order_payment
                and current_order_payment.payment_method == PaymentMethod.CASH
                and self._to_decimal(current_order_payment.outstanding_amount) > Decimal("0.00")
                and current_order_payment.id not in {payment.id for payment in payments}
            ):
                payments.append(current_order_payment)

        for payment in payments:
            if self._to_decimal(event.unapplied_amount) <= Decimal("0.00"):
                break
            allocation_order += 1
            allocatable = min(
                self._to_decimal(payment.outstanding_amount),
                self._to_decimal(event.unapplied_amount),
            )
            if allocatable <= Decimal("0.00"):
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
        trigger_completion_notification: bool = True,
        affect_payment_projection: bool = True,
        allocation_metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        amount = self._to_decimal(amount)
        if amount <= Decimal("0.00"):
            return
        if amount > self._to_decimal(event.unapplied_amount):
            raise ValidationError("Allocated amount exceeds unapplied event balance")
        if affect_payment_projection and amount > self._to_decimal(payment.outstanding_amount):
            raise ValidationError("Allocated amount exceeds payment outstanding balance")

        metadata = dict(allocation_metadata or {})
        metadata.setdefault("order_number", payment.order.order_number if payment.order else None)
        metadata["affects_payment_projection"] = bool(affect_payment_projection)
        allocation = CashCollectionAllocation(
            cash_collection_event_id=event.id,
            payment_id=payment.id,
            order_id=payment.order_id,
            allocated_amount=amount,
            allocation_order=allocation_order,
            allocation_mode=allocation_mode,
            allocation_metadata=metadata,
        )
        db.session.add(allocation)
        event.unapplied_amount = self._to_decimal(event.unapplied_amount) - amount
        previous_status = payment.status

        if affect_payment_projection:
            payment.amount_collected = self._to_decimal(payment.amount_collected) + amount
            self.sync_payment_projection(payment, collected_at=event.occurred_at)

            # ARCH-006: when a cash payment crosses into COMPLETED via this
            # allocation, propagate an auditable identity onto the payment row.
            # Prefer the on-route collector; fall back to whoever recorded the
            # event (e.g. an admin booking a balance-application allocation).
            if (
                payment.payment_method == PaymentMethod.CASH
                and payment.status == PaymentStatus.COMPLETED
                and not payment.collected_by
            ):
                payment.collected_by = event.collector_user_id or event.recorded_by_user_id

            assert_cash_payment_collector(payment, payment.status)
        else:
            self._sync_reserved_prepayment_projection(payment)

        if (
            affect_payment_projection
            and trigger_completion_notification
            and previous_status != PaymentStatus.COMPLETED
            and payment.status == PaymentStatus.COMPLETED
        ):
            try:
                from business_app.tasks.notification_tasks import send_payment_confirmation_task

                send_payment_confirmation_task.delay(payment.id)
            except Exception:
                pass

    @staticmethod
    def _next_allocation_order(event_id: int) -> int:
        return int(
            (
                db.session.query(func.coalesce(func.max(CashCollectionAllocation.allocation_order), 0))
                .filter(CashCollectionAllocation.cash_collection_event_id == event_id)
                .scalar()
                or 0
            )
            + 1
        )

    @staticmethod
    def _allocation_affects_payment_projection(allocation: CashCollectionAllocation) -> bool:
        metadata = allocation.allocation_metadata or {}
        if isinstance(metadata, dict) and "affects_payment_projection" in metadata:
            return bool(metadata.get("affects_payment_projection"))
        return allocation.allocation_mode != "prepaid_reservation"

    def _sync_reserved_prepayment_projection(self, payment: Payment) -> None:
        if not payment:
            return
        reserved_total = self._get_reserved_prepayment_amount(payment.id)
        provider_data = dict(payment.provider_data or {})
        provider_data["cod_prepayment_reserved_amount"] = float(self._to_decimal(reserved_total))
        payment.provider_data = provider_data

    @staticmethod
    def _get_reserved_prepayment_amount(payment_id: int) -> Decimal:
        return db.session.query(
            func.coalesce(func.sum(CashCollectionAllocation.allocated_amount), Decimal("0.00"))
        ).filter(
            CashCollectionAllocation.payment_id == payment_id,
            CashCollectionAllocation.reversed_at.is_(None),
            CashCollectionAllocation.allocation_mode == "prepaid_reservation",
        ).scalar() or Decimal(
            "0.00"
        )

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
