"""Returnable bottle tracking: balances, ledger, fines, and driver accountability."""

import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from flask import current_app
from sqlalchemy import func, or_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import joinedload

from business_app import db
from business_app.models.bottle import (
    BottleBalance,
    BottleFine,
    BottleLedger,
    DriverBottleSession,
    DriverBottleSessionOrder,
    DriverBottleTransfer,
    DriverSessionMembership,
)
from business_app.models.order import Order, OrderItem
from business_app.models.user import User
from shared.enums import (
    BottleFineStatus,
    BottleLedgerEventType,
    DriverBottleSessionStatus,
    DriverBottleTransferStatus,
    DriverSessionMembershipStatus,
    OrderStatus,
    UserStatus,
)
from business_app.utils.audit_logger import AuditEventType, AuditSeverity, audit_logger
from business_app.utils.exceptions import ConflictError, NotFoundError, ValidationError
from business_app.utils.transactions import transactional

if TYPE_CHECKING:
    from business_app.models.delivery import Delivery

logger = logging.getLogger(__name__)


class BottleTrackingService:
    """Manages returnable bottle balances, ledger, fines, and driver accountability."""

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _as_decimal(value: Any) -> Decimal:
        return Decimal(str(value or 0))

    @staticmethod
    def _utc_now() -> datetime:
        return datetime.now(timezone.utc)

    # ------------------------------------------------------------------
    # Balance management
    # ------------------------------------------------------------------

    @staticmethod
    def get_or_create_balance(user_id: int, address_id: int) -> BottleBalance:
        """Get the balance row LOCKED FOR UPDATE, or create a zero row then lock it.

        Locking serialises concurrent read-modify-write on the same (user_id, address_id)
        pair — matching cash_collection_service's pattern — so concurrent
        deliveries/returns/adjustments cannot lose updates. The
        uq_bottle_balance_user_address constraint makes the create race safe via
        ON CONFLICT DO NOTHING + re-select. Runs inside the caller's transaction;
        does NOT commit.
        """
        balance = BottleBalance.query.filter_by(user_id=user_id, address_id=address_id).with_for_update().first()
        if balance is not None:
            return balance

        # Row doesn't exist yet — insert tolerating a concurrent insert, then
        # re-select FOR UPDATE so we hold the lock on whichever row won.
        insert_stmt = (
            pg_insert(BottleBalance.__table__)
            .values(user_id=user_id, address_id=address_id, balance=Decimal("0.00"))
            .on_conflict_do_nothing(index_elements=["user_id", "address_id"])
        )
        db.session.execute(insert_stmt)
        db.session.flush()
        return BottleBalance.query.filter_by(user_id=user_id, address_id=address_id).with_for_update().first()

    def _update_balance(
        self,
        user_id: int,
        address_id: int,
        quantity_delta: Decimal,
        *,
        is_delivery: bool = False,
        is_return: bool = False,
    ) -> BottleBalance:
        """Atomically update balance and timestamp fields."""
        balance = self.get_or_create_balance(user_id, address_id)
        balance.balance = (balance.balance or Decimal("0.00")) + quantity_delta
        now = self._utc_now()
        if is_delivery:
            balance.last_delivery_at = now
        if is_return:
            balance.last_return_at = now
        return balance

    # ------------------------------------------------------------------
    # Ledger writes
    # ------------------------------------------------------------------

    def _create_ledger_entry(
        self,
        *,
        user_id: int,
        address_id: int,
        event_type: BottleLedgerEventType,
        quantity: Decimal,
        actor_user_id: int = None,
        order_id: int = None,
        delivery_id: int = None,
        notes: str = None,
        idempotency_key: str = None,
        metadata: dict = None,
    ) -> BottleLedger:
        """Create a ledger entry and update the materialized balance."""
        # Check idempotency
        if idempotency_key:
            existing = BottleLedger.query.filter_by(idempotency_key=idempotency_key).first()
            if existing:
                logger.info("Duplicate ledger entry skipped: %s", idempotency_key)
                return existing

        # Determine if delivery or return for balance timestamp tracking
        is_delivery = event_type in (
            BottleLedgerEventType.DELIVERY,
            BottleLedgerEventType.INITIAL_BALANCE,
        )
        is_return = event_type in (
            BottleLedgerEventType.RETURN_ON_DELIVERY,
            BottleLedgerEventType.STANDALONE_COLLECTION,
        )

        balance_record = self._update_balance(
            user_id,
            address_id,
            quantity,
            is_delivery=is_delivery,
            is_return=is_return,
        )

        entry = BottleLedger(
            user_id=user_id,
            address_id=address_id,
            order_id=order_id,
            delivery_id=delivery_id,
            event_type=event_type,
            quantity=quantity,
            balance_after=balance_record.balance,
            actor_user_id=actor_user_id,
            occurred_at=self._utc_now(),
            notes=notes,
            idempotency_key=idempotency_key,
            entry_metadata=metadata or {},
        )
        db.session.add(entry)
        db.session.flush()
        return entry

    # ------------------------------------------------------------------
    # Public ledger operations
    # ------------------------------------------------------------------

    def record_bottles_delivered(
        self,
        order_id: int,
        user_id: int,
        address_id: int,
        quantity: Decimal,
        actor_user_id: int = None,
    ) -> BottleLedger:
        """Record bottles delivered to customer via an order (+quantity)."""
        logger.info(
            "[BOTTLE] record_bottles_delivered order=%s user=%s address=%s qty=%s actor=%s",
            order_id,
            user_id,
            address_id,
            quantity,
            actor_user_id,
        )
        entry = self._create_ledger_entry(
            user_id=user_id,
            address_id=address_id,
            event_type=BottleLedgerEventType.DELIVERY,
            quantity=self._as_decimal(quantity),
            actor_user_id=actor_user_id,
            order_id=order_id,
            idempotency_key=f"delivery:{order_id}",
            metadata={"source": "order_delivery"},
        )
        logger.info(
            "[BOTTLE] record_bottles_delivered OK order=%s ledger_id=%s balance_after=%s",
            order_id,
            entry.id,
            entry.balance_after,
        )
        return entry

    def record_bottles_returned(
        self,
        user_id: int,
        address_id: int,
        quantity: Decimal,
        *,
        order_id: int = None,
        delivery_id: int = None,
        actor_user_id: int = None,
        notes: str = None,
    ) -> BottleLedger:
        """Record bottles returned by customer during a delivery (-quantity)."""
        logger.info(
            "[BOTTLE] record_bottles_returned order=%s delivery=%s user=%s address=%s qty=%s actor=%s",
            order_id,
            delivery_id,
            user_id,
            address_id,
            quantity,
            actor_user_id,
        )
        qty = self._as_decimal(quantity)
        if qty <= 0:
            raise ValidationError("Return quantity must be positive")
        entry = self._create_ledger_entry(
            user_id=user_id,
            address_id=address_id,
            event_type=BottleLedgerEventType.RETURN_ON_DELIVERY,
            quantity=-qty,
            actor_user_id=actor_user_id,
            order_id=order_id,
            delivery_id=delivery_id,
            notes=notes,
            idempotency_key=f"return:{order_id}:{delivery_id}" if order_id else None,
            metadata={"source": "return_on_delivery"},
        )
        logger.info(
            "[BOTTLE] record_bottles_returned OK order=%s ledger_id=%s balance_after=%s",
            order_id,
            entry.id,
            entry.balance_after,
        )
        return entry

    @transactional
    def record_standalone_collection(
        self,
        user_id: int,
        address_id: int,
        quantity: Decimal,
        actor_user_id: int,
        notes: str = None,
    ) -> BottleLedger:
        """Record standalone bottle pickup by driver outside order flow (-quantity)."""
        qty = self._as_decimal(quantity)
        if qty <= 0:
            raise ValidationError("Collection quantity must be positive")
        entry = self._create_ledger_entry(
            user_id=user_id,
            address_id=address_id,
            event_type=BottleLedgerEventType.STANDALONE_COLLECTION,
            quantity=-qty,
            actor_user_id=actor_user_id,
            notes=notes,
            metadata={"source": "standalone_collection"},
        )
        # Tally against the driver's open session so session inventory stays accurate
        self.update_session_delivery_tally(
            actor_user_id,
            bottles_collected=int(qty),
        )
        return entry

    @transactional
    def admin_adjust_balance(
        self,
        user_id: int,
        address_id: int,
        adjustment: Decimal,
        actor_user_id: int,
        notes: str,
    ) -> BottleLedger:
        """Admin manually adjusts balance. Positive = customer owes more bottles."""
        if not notes:
            raise ValidationError("Notes are required for admin adjustments")
        return self._create_ledger_entry(
            user_id=user_id,
            address_id=address_id,
            event_type=BottleLedgerEventType.ADMIN_ADJUSTMENT,
            quantity=self._as_decimal(adjustment),
            actor_user_id=actor_user_id,
            notes=notes,
            metadata={"source": "admin_adjustment"},
        )

    @transactional
    def set_initial_balance(
        self,
        user_id: int,
        address_id: int,
        quantity: Decimal,
        actor_user_id: int,
        notes: str = None,
    ) -> BottleLedger:
        """Set initial bottle balance for admin data population."""
        qty = self._as_decimal(quantity)
        return self._create_ledger_entry(
            user_id=user_id,
            address_id=address_id,
            event_type=BottleLedgerEventType.INITIAL_BALANCE,
            quantity=qty,
            actor_user_id=actor_user_id,
            notes=notes or "Initial balance set by admin",
            idempotency_key=f"initial:{user_id}:{address_id}",
            metadata={"source": "initial_balance"},
        )

    # ------------------------------------------------------------------
    # Order bottle calculation
    # ------------------------------------------------------------------

    @staticmethod
    def calculate_bottles_for_order(order: Order) -> Decimal:
        """Sum returnable bottles across all items in an order."""
        total = Decimal("0.00")
        items = order.order_items if hasattr(order, "order_items") else []
        logger.debug(
            "[BOTTLE] calculate_bottles_for_order order=%s item_count=%s",
            order.id,
            len(items),
        )
        for item in items:
            product = item.product
            if product and product.tracks_returnable_bottles:
                bottles_per_unit = Decimal(str(product.returnable_bottles_per_unit or 0))
                line_qty = bottles_per_unit * Decimal(str(item.quantity or 0))
                total += line_qty
                logger.debug(
                    "[BOTTLE] order=%s item=%s product=%s tracks_bottles=True "
                    "bottles_per_unit=%s item_qty=%s line_bottles=%s",
                    order.id,
                    item.id,
                    product.id,
                    bottles_per_unit,
                    item.quantity,
                    line_qty,
                )
            else:
                logger.debug(
                    "[BOTTLE] order=%s item=%s product=%s tracks_bottles=%s — skipped",
                    order.id,
                    item.id,
                    product.id if product else None,
                    product.tracks_returnable_bottles if product else "no product",
                )
        logger.debug("[BOTTLE] calculate_bottles_for_order order=%s total=%s", order.id, total)
        return total

    # ------------------------------------------------------------------
    # Fine management (always manual)
    # ------------------------------------------------------------------

    @transactional
    def issue_fine(
        self,
        user_id: int,
        bottle_balance_id: int,
        quantity: Decimal,
        fine_amount: Decimal,
        actor_user_id: int,
        notes: str = None,
    ) -> BottleFine:
        """Manually issue a fine for missing bottles."""
        balance = BottleBalance.query.get(bottle_balance_id)
        if not balance:
            raise NotFoundError("Bottle balance not found")
        if balance.user_id != user_id:
            raise ValidationError("Balance does not belong to this user")

        qty = self._as_decimal(quantity)
        amount = self._as_decimal(fine_amount)
        if qty <= 0:
            raise ValidationError("Fine quantity must be positive")
        if amount <= 0:
            raise ValidationError("Fine amount must be positive")

        fine = BottleFine(
            user_id=user_id,
            bottle_balance_id=bottle_balance_id,
            quantity=qty,
            fine_amount=amount,
            status=BottleFineStatus.PENDING,
            issued_by=actor_user_id,
            issued_at=self._utc_now(),
            notes=notes,
        )
        db.session.add(fine)

        # Record in ledger
        self._create_ledger_entry(
            user_id=user_id,
            address_id=balance.address_id,
            event_type=BottleLedgerEventType.FINE_ISSUED,
            quantity=Decimal("0"),
            actor_user_id=actor_user_id,
            notes=f"Fine issued: {qty} bottles, {amount} UZS" + (f" — {notes}" if notes else ""),
            metadata={
                "fine_id": fine.id if fine.id else None,
                "fine_quantity": float(qty),
                "fine_amount": float(amount),
            },
        )

        db.session.flush()
        return fine

    @transactional
    def waive_fine(self, fine_id: int, actor_user_id: int, notes: str = None) -> BottleFine:
        """Waive an existing fine."""
        fine = BottleFine.query.get(fine_id)
        if not fine:
            raise NotFoundError("Fine not found")
        if fine.status in (BottleFineStatus.PAID, BottleFineStatus.WAIVED):
            raise ConflictError(f"Fine is already {fine.status.value}")

        fine.status = BottleFineStatus.WAIVED
        fine.waived_at = self._utc_now()
        fine.waived_by = actor_user_id
        if notes:
            fine.notes = (fine.notes or "") + f"\nWaived: {notes}"

        # Record in ledger
        balance = BottleBalance.query.get(fine.bottle_balance_id)
        if balance:
            self._create_ledger_entry(
                user_id=fine.user_id,
                address_id=balance.address_id,
                event_type=BottleLedgerEventType.FINE_REVERSED,
                quantity=Decimal("0"),
                actor_user_id=actor_user_id,
                notes=f"Fine #{fine.id} waived" + (f" — {notes}" if notes else ""),
                metadata={"fine_id": fine.id},
            )

        db.session.flush()
        return fine

    @transactional
    def mark_fine_paid(self, fine_id: int, actor_user_id: int, notes: str = None) -> BottleFine:
        """Mark a fine as paid and reduce the customer's bottle balance by the fine quantity."""
        fine = BottleFine.query.get(fine_id)
        if not fine:
            raise NotFoundError("Fine not found")
        if fine.status in (BottleFineStatus.PAID, BottleFineStatus.WAIVED):
            raise ConflictError(f"Fine is already {fine.status.value}")

        fine.status = BottleFineStatus.PAID
        fine.paid_at = self._utc_now()
        if notes:
            fine.notes = (fine.notes or "") + f"\nPaid: {notes}"

        # Reduce the balance by the fine quantity.
        # The customer has settled their monetary debt; the bottles are
        # considered accounted for, so the outstanding balance decreases.
        balance = BottleBalance.query.get(fine.bottle_balance_id)
        if balance:
            self._create_ledger_entry(
                user_id=fine.user_id,
                address_id=balance.address_id,
                event_type=BottleLedgerEventType.FINE_PAID,
                quantity=-self._as_decimal(fine.quantity),
                actor_user_id=actor_user_id,
                notes=f"Fine #{fine.id} paid" + (f" — {notes}" if notes else ""),
                idempotency_key=f"fine_paid:{fine.id}",
                metadata={"fine_id": fine.id},
            )

        db.session.flush()
        return fine

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    @staticmethod
    def get_balance(user_id: int, address_id: int) -> Optional[BottleBalance]:
        """Get balance for a specific user+address pair."""
        return BottleBalance.query.filter_by(user_id=user_id, address_id=address_id).first()

    @staticmethod
    def get_customer_balances(user_id: int) -> List[BottleBalance]:
        """Get all balances across addresses for a customer."""
        return (
            BottleBalance.query.filter_by(user_id=user_id)
            .options(joinedload(BottleBalance.address))
            .order_by(BottleBalance.balance.desc())
            .all()
        )

    def get_customer_summary(self, user_id: int) -> Dict:
        """Aggregate bottle stats for a customer."""
        balances = self.get_customer_balances(user_id)
        total_balance = sum(float(b.balance or 0) for b in balances)
        active_fines = BottleFine.query.filter(
            BottleFine.user_id == user_id,
            BottleFine.status.in_([BottleFineStatus.PENDING, BottleFineStatus.INVOICED]),
        ).count()
        total_fine_amount = (
            db.session.query(func.coalesce(func.sum(BottleFine.fine_amount), 0))
            .filter(
                BottleFine.user_id == user_id,
                BottleFine.status.in_([BottleFineStatus.PENDING, BottleFineStatus.INVOICED]),
            )
            .scalar()
        )

        return {
            "user_id": user_id,
            "total_balance": total_balance,
            "addresses": [
                {
                    "address_id": b.address_id,
                    "address_title": b.address.title if b.address else None,
                    "full_address": b.address.full_address if b.address else None,
                    "balance": float(b.balance or 0),
                    "last_delivery_at": b.last_delivery_at.isoformat() if b.last_delivery_at else None,
                    "last_return_at": b.last_return_at.isoformat() if b.last_return_at else None,
                    "bottle_balance_id": b.id,
                }
                for b in balances
            ],
            "active_fines_count": active_fines,
            "total_fine_amount": float(total_fine_amount or 0),
        }

    @staticmethod
    def get_address_ledger(user_id: int, address_id: int, page: int = 1, per_page: int = 20) -> Dict:
        """Get paginated ledger for a specific user+address."""
        query = BottleLedger.query.filter_by(user_id=user_id, address_id=address_id).order_by(
            BottleLedger.occurred_at.desc()
        )
        total = query.count()
        entries = query.offset((page - 1) * per_page).limit(per_page).all()
        return {
            "items": [e.to_dict() for e in entries],
            "total": total,
            "page": page,
            "per_page": per_page,
            "pages": (total + per_page - 1) // per_page if per_page else 0,
        }

    @staticmethod
    def get_all_balances(
        page: int = 1,
        per_page: int = 20,
        min_balance: float = None,
        user_id: int = None,
        search: str = None,
    ) -> Dict:
        """Get paginated list of all balances with optional filters."""
        query = BottleBalance.query.options(
            joinedload(BottleBalance.user),
            joinedload(BottleBalance.address),
        )
        if min_balance is not None:
            query = query.filter(BottleBalance.balance >= Decimal(str(min_balance)))
        if user_id:
            query = query.filter(BottleBalance.user_id == user_id)
        if search:
            query = query.join(User, BottleBalance.user_id == User.id).filter(
                or_(
                    User.first_name.ilike(f"%{search}%"),
                    User.last_name.ilike(f"%{search}%"),
                    User.phone.ilike(f"%{search}%"),
                )
            )

        query = query.order_by(BottleBalance.balance.desc())
        total = query.count()
        items = query.offset((page - 1) * per_page).limit(per_page).all()
        return {
            "items": items,
            "total": total,
            "page": page,
            "per_page": per_page,
            "pages": (total + per_page - 1) // per_page if per_page else 0,
        }

    @staticmethod
    def get_all_fines(
        page: int = 1,
        per_page: int = 20,
        status: str = None,
        user_id: int = None,
    ) -> Dict:
        """Get paginated list of fines."""
        query = BottleFine.query.options(
            joinedload(BottleFine.user),
            joinedload(BottleFine.bottle_balance).joinedload(BottleBalance.address),
            joinedload(BottleFine.issuer),
        )
        if status:
            query = query.filter(BottleFine.status == BottleFineStatus(status))
        if user_id:
            query = query.filter(BottleFine.user_id == user_id)

        query = query.order_by(BottleFine.issued_at.desc())
        total = query.count()
        items = query.offset((page - 1) * per_page).limit(per_page).all()
        return {
            "items": [f.to_dict() for f in items],
            "total": total,
            "page": page,
            "per_page": per_page,
            "pages": (total + per_page - 1) // per_page if per_page else 0,
        }

    @staticmethod
    def get_all_ledger_entries(
        page: int = 1,
        per_page: int = 20,
        user_id: int = None,
        address_id: int = None,
        event_type: str = None,
    ) -> Dict:
        """Get paginated ledger entries with optional filters.

        Returns ORM objects in ``items`` so callers can use serializer
        functions that access relationships (actor_user, address, etc.).
        """
        query = BottleLedger.query.options(
            joinedload(BottleLedger.user),
            joinedload(BottleLedger.address),
            joinedload(BottleLedger.actor_user),
        ).order_by(BottleLedger.occurred_at.desc())
        if user_id:
            query = query.filter(BottleLedger.user_id == user_id)
        if address_id:
            query = query.filter(BottleLedger.address_id == address_id)
        if event_type:
            query = query.filter(BottleLedger.event_type == BottleLedgerEventType(event_type))

        total = query.count()
        items = query.offset((page - 1) * per_page).limit(per_page).all()
        return {
            "items": items,
            "total": total,
            "page": page,
            "per_page": per_page,
            "pages": (total + per_page - 1) // per_page if per_page else 0,
        }

    # ------------------------------------------------------------------
    # Dashboard stats
    # ------------------------------------------------------------------

    @staticmethod
    def get_dashboard_stats() -> Dict:
        """Aggregate stats for the admin bottle tracking dashboard."""
        total_bottles_out = (
            db.session.query(func.coalesce(func.sum(BottleBalance.balance), 0))
            .filter(BottleBalance.balance > 0)
            .scalar()
        )

        customers_with_balance = BottleBalance.query.filter(BottleBalance.balance > 0).count()

        active_fines = BottleFine.query.filter(
            BottleFine.status.in_([BottleFineStatus.PENDING, BottleFineStatus.INVOICED])
        ).count()

        total_fine_amount = (
            db.session.query(func.coalesce(func.sum(BottleFine.fine_amount), 0))
            .filter(BottleFine.status.in_([BottleFineStatus.PENDING, BottleFineStatus.INVOICED]))
            .scalar()
        )

        # Top debtors
        top_debtors = (
            db.session.query(
                BottleBalance.user_id,
                func.sum(BottleBalance.balance).label("total_balance"),
            )
            .filter(BottleBalance.balance > 0)
            .group_by(BottleBalance.user_id)
            .order_by(func.sum(BottleBalance.balance).desc())
            .limit(10)
            .all()
        )

        top_debtor_details = []
        for user_id, total_bal in top_debtors:
            user = User.query.get(user_id)
            if user:
                top_debtor_details.append(
                    {
                        "user_id": user_id,
                        "name": f"{user.first_name or ''} {user.last_name or ''}".strip(),
                        "phone": user.phone,
                        "total_balance": float(total_bal or 0),
                    }
                )

        return {
            "total_bottles_out": float(total_bottles_out or 0),
            "customers_with_balance": customers_with_balance,
            "active_fines": active_fines,
            "total_fine_amount": float(total_fine_amount or 0),
            "top_debtors": top_debtor_details,
        }

    # ------------------------------------------------------------------
    # Balance reconciliation
    # ------------------------------------------------------------------

    @transactional
    def reconcile_balance(self, user_id: int, address_id: int) -> Dict:
        """Recalculate balance from ledger entries and report discrepancy."""
        ledger_sum = (
            db.session.query(func.coalesce(func.sum(BottleLedger.quantity), 0))
            .filter(
                BottleLedger.user_id == user_id,
                BottleLedger.address_id == address_id,
            )
            .scalar()
        )

        balance = self.get_or_create_balance(user_id, address_id)
        current = float(balance.balance or 0)
        expected = float(ledger_sum or 0)
        discrepancy = round(current - expected, 2)

        if discrepancy != 0:
            logger.warning(
                "Bottle balance discrepancy for user=%s address=%s: " "current=%s expected=%s diff=%s",
                user_id,
                address_id,
                current,
                expected,
                discrepancy,
            )
            balance.balance = Decimal(str(expected))
            db.session.flush()

        return {
            "user_id": user_id,
            "address_id": address_id,
            "previous_balance": current,
            "recalculated_balance": expected,
            "discrepancy": discrepancy,
            "corrected": discrepancy != 0,
        }

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # Driver bottle sessions
    # ------------------------------------------------------------------

    @transactional
    def open_bottle_session(
        self,
        driver_user_id: int,
        bottles_loaded: int,
        *,
        actor_user_id: int = None,
        notes: str = None,
    ) -> DriverBottleSession:
        """Open a new trip session for the driver (load from warehouse).

        Raises ConflictError if the driver already has an OPEN session.
        The DB partial unique index on (driver_user_id) WHERE status='open'
        acts as a second safety net against concurrent opens.
        """
        existing = DriverBottleSession.query.filter_by(
            driver_user_id=driver_user_id,
            status=DriverBottleSessionStatus.OPEN,
        ).first()
        if existing:
            raise ConflictError(
                f"Driver already has an open bottle session (id={existing.id}). "
                "Close the current session before starting a new one.",
                error_code="BOTTLE_SESSION_ALREADY_OPEN",
            )
        if bottles_loaded <= 0:
            raise ValidationError("bottles_loaded must be greater than zero")

        session = DriverBottleSession(
            driver_user_id=driver_user_id,
            bottles_loaded=bottles_loaded,
            status=DriverBottleSessionStatus.OPEN,
            loaded_by_user_id=actor_user_id or driver_user_id,
            started_at=self._utc_now(),
            notes=notes,
        )
        db.session.add(session)
        db.session.flush()
        return session

    @transactional
    def close_bottle_session(
        self,
        driver_user_id: int,
        bottles_returned_to_warehouse: int,
        *,
        actor_user_id: int = None,
        notes: str = None,
    ) -> DriverBottleSession:
        """Close the driver's active trip session (return to warehouse).

        Computes and persists the discrepancy.
        Raises NotFoundError if no open session exists.

        A driver may close at any time. Any still-undelivered order bound to the
        session is released here (its binding deleted) rather than blocking the
        close; it re-binds to the driver's next open session on the next forward
        transition (late-bind guard), or an admin delivers it unbound.
        """
        session = self._get_open_session_or_raise(driver_user_id)

        if bottles_returned_to_warehouse < 0:
            raise ValidationError("bottles_returned_to_warehouse cannot be negative")

        # Release still-undelivered orders instead of blocking the close. Deleting
        # the binding (vs. leaving it on the now-closed session) matters: the delivery
        # tally credits binding.session_id with no open-session check, so a stale
        # binding would corrupt this sealed session's counters if later delivered.
        released = self._release_open_bindings_for_session(session.id)
        if released:
            logger.info(
                "[BOTTLE] close_bottle_session released %s carried order binding(s) for session=%s: %s",
                len(released),
                session.id,
                released,
            )

        session.bottles_returned_to_warehouse = bottles_returned_to_warehouse
        session.status = DriverBottleSessionStatus.CLOSED
        session.closed_at = self._utc_now()
        session.closed_by_user_id = actor_user_id or driver_user_id
        session.compute_discrepancy()
        if notes:
            session.notes = (session.notes or "") + f"\n{notes}" if session.notes else notes
        revoked = self.revoke_all_memberships(session.id)
        if revoked:
            logger.info(
                "[BOTTLE] close_bottle_session revoked %s co-driver membership(s) for session=%s", revoked, session.id
            )
        db.session.flush()
        return session

    @transactional
    def admin_force_close_session(
        self,
        session_id: int,
        actor_user_id: int,
        *,
        bottles_returned_to_warehouse: int = 0,
        reason: str,
    ) -> DriverBottleSession:
        """Admin force-closes an abandoned open session.

        The discrepancy will reflect the full unaccounted load.
        A reason is mandatory for the audit trail.
        """
        session = DriverBottleSession.query.get(session_id)
        if not session:
            raise NotFoundError("Bottle session not found")
        if session.status != DriverBottleSessionStatus.OPEN:
            raise ConflictError(f"Session is already {session.status.value}, cannot force close")
        if not reason or not reason.strip():
            raise ValidationError("A reason is required for force-closing a session")

        # Mirror the normal-close release: an abandoned session's still-undelivered
        # orders must not stay bound to this sealed session (a later delivery would
        # corrupt its counters — see _release_open_bindings_for_session). They
        # re-bind to whichever session physically carries them next.
        released = self._release_open_bindings_for_session(session.id)
        if released:
            logger.info(
                "[BOTTLE] admin_force_close_session released %s carried order binding(s) for session=%s: %s",
                len(released),
                session.id,
                released,
            )

        session.bottles_returned_to_warehouse = max(0, bottles_returned_to_warehouse)
        session.status = DriverBottleSessionStatus.FORCE_CLOSED
        session.force_closed = True
        session.force_close_reason = reason.strip()
        session.closed_at = self._utc_now()
        session.closed_by_user_id = actor_user_id
        session.compute_discrepancy()
        revoked = self.revoke_all_memberships(session.id)
        if revoked:
            logger.info(
                "[BOTTLE] admin_force_close_session revoked %s co-driver membership(s) for session=%s",
                revoked,
                session.id,
            )
        db.session.flush()
        return session

    def reopen_session(
        self,
        session_id: int,
        actor_user_id: int,
        *,
        reason: str,
        commit: bool = True,
    ) -> DriverBottleSession:
        """Reopen a CLOSED / FORCE_CLOSED bottle session for retroactive adjustment.

        Used when an admin edits a delivered order whose driver session has
        already been closed. The session transitions back to OPEN so the
        order-edit cascade can write balancing ledger entries that re-tally
        cleanly. The driver re-closes the session afterward (compute_discrepancy
        is reset so close-time math runs from the updated tallies).

        Raises:
            ValidationError: reason is empty, or status is not CLOSED/FORCE_CLOSED.
            ConflictError: driver already has another OPEN session (partial
                unique index `uq_dbs_driver_open` would otherwise be violated).
        """
        if not reason or not reason.strip():
            raise ValidationError("A reason is required to reopen a bottle session")

        session = DriverBottleSession.query.get(session_id)
        if not session:
            raise NotFoundError("Bottle session not found")

        if session.status not in {
            DriverBottleSessionStatus.CLOSED,
            DriverBottleSessionStatus.FORCE_CLOSED,
        }:
            raise ValidationError(
                f"Cannot reopen session {session.id}: status is "
                f"{session.status.value if hasattr(session.status, 'value') else session.status}; "
                "only CLOSED or FORCE_CLOSED sessions can be reopened.",
                error_code="BOTTLE_SESSION_NOT_REOPENABLE",
            )

        active_conflict = (
            DriverBottleSession.query.filter(
                DriverBottleSession.driver_user_id == session.driver_user_id,
                DriverBottleSession.id != session.id,
                DriverBottleSession.status == DriverBottleSessionStatus.OPEN,
            )
            .order_by(DriverBottleSession.started_at.desc())
            .first()
        )
        if active_conflict:
            raise ConflictError(
                f"Cannot reopen session {session.id}: driver already has an "
                f"OPEN session (id={active_conflict.id}). Close the active "
                "session before reopening this one.",
                error_code="BOTTLE_SESSION_ACTIVE_CONFLICT",
            )

        previous_status = session.status.value if hasattr(session.status, "value") else session.status
        session.status = DriverBottleSessionStatus.OPEN
        session.reopened_at = self._utc_now()
        session.reopened_by_user_id = actor_user_id
        session.reopened_reason = reason.strip()
        session.reopen_count = (session.reopen_count or 0) + 1
        # Reset close-side state so the post-adjustment re-close recomputes
        # discrepancy from the updated tallies.
        session.bottles_returned_to_warehouse = None
        session.closed_at = None
        session.closed_by_user_id = None
        session.discrepancy = None
        # The session was force-closed by admin previously? Drop the flag so the
        # next normal close treats it as a clean trip.
        session.force_closed = False

        audit_logger.log_event(
            event_type=AuditEventType.SESSION_REOPENED,
            action="driver_bottle_session_reopened",
            severity=AuditSeverity.HIGH,
            resource_type="driver_bottle_session",
            resource_id=str(session.id),
            additional_data={
                "driver_user_id": session.driver_user_id,
                "actor_user_id": actor_user_id,
                "previous_status": previous_status,
                "reopen_count": session.reopen_count,
                "reason": session.reopened_reason,
            },
        )

        if commit:
            db.session.commit()
        else:
            db.session.flush()
        return session

    def get_open_session(self, driver_user_id: int) -> Optional[DriverBottleSession]:
        """Return the driver's current open session, or None."""
        logger.debug("[BOTTLE] get_open_session driver=%s", driver_user_id)
        session = DriverBottleSession.query.filter_by(
            driver_user_id=driver_user_id,
            status=DriverBottleSessionStatus.OPEN,
        ).first()
        logger.debug(
            "[BOTTLE] get_open_session driver=%s → %s",
            driver_user_id,
            f"session_id={session.id}" if session else "None",
        )
        return session

    def _get_open_session_or_raise(self, driver_user_id: int) -> DriverBottleSession:
        session = self.get_open_session(driver_user_id)
        if not session:
            raise NotFoundError(
                "No open bottle session found for this driver",
                error_code="BOTTLE_SESSION_NOT_FOUND",
            )
        return session

    # ------------------------------------------------------------------
    # Co-driver session membership
    # ------------------------------------------------------------------

    def get_effective_session(self, driver_user_id: int) -> Optional[DriverBottleSession]:
        """Return the session this driver should operate under.

        Priority:
          1. Driver's own OPEN session (if they have one).
          2. The OPEN session they have joined as a co-driver member.
          3. None — driver has no access to any session.
        """
        own = self.get_open_session(driver_user_id)
        if own:
            return own
        membership = self.get_active_membership(driver_user_id)
        if membership:
            session = DriverBottleSession.query.get(membership.session_id)
            if session and session.status == DriverBottleSessionStatus.OPEN:
                return session
        return None

    def get_active_membership(self, driver_user_id: int) -> Optional[DriverSessionMembership]:
        """Return the driver's current active co-driver membership, if any."""
        return DriverSessionMembership.query.filter_by(
            member_driver_id=driver_user_id,
            status=DriverSessionMembershipStatus.ACTIVE,
        ).first()

    def get_joinable_sessions(self, excluding_driver_id: int) -> List[DriverBottleSession]:
        """Return all OPEN sessions not owned by this driver, available to join."""
        return (
            DriverBottleSession.query.filter(
                DriverBottleSession.status == DriverBottleSessionStatus.OPEN,
                DriverBottleSession.driver_user_id != excluding_driver_id,
            )
            .order_by(DriverBottleSession.started_at.desc())
            .all()
        )

    @transactional
    def join_session(
        self,
        member_driver_id: int,
        session_id: int,
        *,
        notes: str = None,
    ) -> DriverSessionMembership:
        """Allow a driver to join another driver's open session as a co-driver.

        Raises:
          - ConflictError if the driver already has their own OPEN session.
          - ConflictError if the driver is already an active member of another session.
          - NotFoundError if the target session is not found.
          - ValidationError if the target session is not OPEN.
          - ValidationError if driver tries to join their own session.
        """
        session = DriverBottleSession.query.get(session_id)
        if not session:
            raise NotFoundError(
                "Bottle session not found",
                error_code="BOTTLE_SESSION_NOT_FOUND",
            )
        if session.driver_user_id == member_driver_id:
            raise ValidationError(
                "Cannot join your own session",
                error_code="BOTTLE_SESSION_JOIN_OWN",
            )
        if session.status != DriverBottleSessionStatus.OPEN:
            raise ValidationError(
                "Can only join an OPEN session",
                error_code="BOTTLE_SESSION_NOT_OPEN",
            )

        own_session = self.get_open_session(member_driver_id)
        if own_session:
            raise ConflictError(
                "Close your own open session before joining another driver's session",
                error_code="BOTTLE_SESSION_ALREADY_OPEN",
            )

        existing_membership = self.get_active_membership(member_driver_id)
        if existing_membership:
            raise ConflictError(
                f"Already an active co-driver member of session {existing_membership.session_id}. "
                "Leave that session before joining another.",
                error_code="BOTTLE_SESSION_MEMBERSHIP_ALREADY_ACTIVE",
            )

        membership = DriverSessionMembership(
            session_id=session_id,
            session_owner_id=session.driver_user_id,
            member_driver_id=member_driver_id,
            status=DriverSessionMembershipStatus.ACTIVE,
            notes=notes,
        )
        db.session.add(membership)
        db.session.flush()
        logger.info(
            "[BOTTLE] join_session member=%s joined session=%s (owner=%s)",
            member_driver_id,
            session_id,
            session.driver_user_id,
        )
        return membership

    @transactional
    def leave_session(self, member_driver_id: int) -> DriverSessionMembership:
        """Voluntarily leave the current co-driver session membership.

        Raises NotFoundError if the driver has no active membership.
        """
        membership = self.get_active_membership(member_driver_id)
        if not membership:
            raise NotFoundError(
                "No active co-driver session membership found",
                error_code="BOTTLE_SESSION_MEMBERSHIP_NOT_FOUND",
            )
        membership.status = DriverSessionMembershipStatus.LEFT
        membership.left_at = self._utc_now()
        db.session.flush()
        logger.info(
            "[BOTTLE] leave_session member=%s left session=%s",
            member_driver_id,
            membership.session_id,
        )
        return membership

    def revoke_all_memberships(self, session_id: int) -> int:
        """Revoke all active memberships for a session (called on close/force-close).

        Returns the count of memberships revoked.
        """
        now = self._utc_now()
        memberships = DriverSessionMembership.query.filter_by(
            session_id=session_id,
            status=DriverSessionMembershipStatus.ACTIVE,
        ).all()
        for m in memberships:
            m.status = DriverSessionMembershipStatus.REVOKED
            m.left_at = now
        return len(memberships)

    def list_eligible_co_drivers(self, owner_driver_id: int) -> List[Dict[str, Any]]:
        """Drivers who can be invited to ``owner_driver_id``'s open session.

        Eligibility: active delivery driver, not the owner, no own open session,
        and not currently a member of any other session. Encapsulated here so
        the staff API stays free of direct ``User.query`` access (boundary rule
        enforced by ``test_api_boundary_coupling_scores_do_not_regress``).
        """
        owner_session = self.get_open_session(owner_driver_id)
        if not owner_session:
            raise ConflictError(
                "You must have an open bottle session to invite co-drivers",
                error_code="BOTTLE_SESSION_NOT_FOUND",
            )

        drivers = User.query.filter(
            User.role == "delivery_driver",
            User.id != owner_driver_id,
            User.status == UserStatus.ACTIVE,
        ).all()

        eligible: List[Dict[str, Any]] = []
        for driver in drivers:
            if self.get_open_session(driver.id):
                continue
            if self.get_active_membership(driver.id):
                continue
            eligible.append(
                {
                    "user_id": driver.id,
                    "name": f"{driver.first_name or ''} {driver.last_name or ''}".strip(),
                    "phone": driver.phone,
                }
            )
        return eligible

    # ------------------------------------------------------------------
    # Order binding & capacity enforcement
    # ------------------------------------------------------------------

    def bind_order_to_session(
        self,
        session_id: int,
        order_id: int,
        *,
        accepted_by_driver_id: int = None,
    ) -> DriverBottleSessionOrder:
        """Attach an order to a session. Idempotent — safe to call multiple times.

        accepted_by_driver_id: the driver who actually accepted the order.
        May differ from the session owner when a co-driver (member) accepts.
        """
        logger.info(
            f"[BOTTLE] bind_order_to_session session={session_id} order={order_id} accepted_by={accepted_by_driver_id}"
        )
        existing = DriverBottleSessionOrder.query.filter_by(order_id=order_id).first()
        if existing:
            if existing.session_id != session_id:
                logger.warning(
                    f"[BOTTLE] bind_order_to_session CONFLICT order={order_id} already bound to session={existing.session_id}, requested session={session_id}"  # noqa: E501
                )
                raise ConflictError(f"Order {order_id} is already bound to session {existing.session_id}")
            logger.info(
                f"[BOTTLE] bind_order_to_session order={order_id} already bound to session={session_id} (idempotent)"
            )
            return existing  # already bound to this session

        binding = DriverBottleSessionOrder(
            session_id=session_id,
            order_id=order_id,
            accepted_by_driver_id=accepted_by_driver_id,
        )
        db.session.add(binding)
        db.session.flush()
        logger.info(f"[BOTTLE] bind_order_to_session OK binding_id={binding.id}")
        return binding

    def rebind_order_to_session(
        self,
        order_id: int,
        new_session_id: int,
        *,
        accepted_by_driver_id: int = None,
    ) -> DriverBottleSessionOrder:
        """Move an existing order binding to a different session (carry-over).

        Used when an order accepted under a now-closed session is carried
        forward and delivered under the driver's new open session. Updates the
        existing ``DriverBottleSessionOrder`` row in place so the UNIQUE
        ``order_id`` invariant holds; falls back to creating a binding if none
        exists yet. Idempotent when the order is already on ``new_session_id``.

        Unlike :meth:`bind_order_to_session` (which refuses to move a binding),
        this is the deliberate cross-session migration path: the bottle tally
        follows the open session the driver is actually operating under.
        """
        binding = DriverBottleSessionOrder.query.filter_by(order_id=order_id).first()
        if binding is None:
            return self.bind_order_to_session(new_session_id, order_id, accepted_by_driver_id=accepted_by_driver_id)
        if binding.session_id == new_session_id:
            return binding
        old_session_id = binding.session_id
        binding.session_id = new_session_id
        if accepted_by_driver_id is not None:
            binding.accepted_by_driver_id = accepted_by_driver_id
        db.session.flush()
        logger.info(
            "[BOTTLE] rebind_order_to_session order=%s session %s→%s accepted_by=%s",
            order_id,
            old_session_id,
            new_session_id,
            accepted_by_driver_id,
        )
        return binding

    def unbind_order(self, order_id: int) -> bool:
        """Remove an order's bottle-session binding (e.g. when the delivery is
        returned to the pool and no driver owns it). Idempotent; returns True if
        a binding row was deleted. The order rebinds on the next assignment."""
        binding = DriverBottleSessionOrder.query.filter_by(order_id=order_id).first()
        if binding is None:
            return False
        db.session.delete(binding)
        db.session.flush()
        logger.info("[BOTTLE] unbind_order order=%s removed binding from session=%s", order_id, binding.session_id)
        return True

    @staticmethod
    def assert_delivery_within_session_capacity(session: DriverBottleSession, bottles_to_deliver: int) -> None:
        """Raise ValidationError if the session cannot cover this delivery."""
        available = session.current_inventory
        if bottles_to_deliver > available:
            raise ValidationError(
                f"Session {session.id} only has {available} bottle(s) available; "
                f"cannot deliver {bottles_to_deliver}.",
                error_code="BOTTLE_SESSION_CAPACITY_EXCEEDED",
            )

    @staticmethod
    def _strict_enforcement_enabled() -> bool:
        """Whether session-invariant violations should raise (strict) or warn (legacy)."""
        try:
            return bool(current_app.config.get("BOTTLE_SESSION_ENFORCEMENT_STRICT", False))
        except RuntimeError:
            return False

    def assert_driver_can_progress_delivery(self, delivery: "Delivery") -> Optional[DriverBottleSession]:
        """Guard called before any post-assignment delivery transition.

        Ensures the order is delivered under an OPEN bottle session whose
        binding follows the session the driver is currently operating under, so
        bottle counts tally against the load the order is physically on. Orders
        with no returnable bottles need no session and return ``None``.

        Role since the assignment SSOT landed: ``DeliveryAssignmentService.
        assign_driver`` now binds the order to the driver's session at
        assignment time, so the common case is already bound here. This guard is
        the *post-assignment backstop* — it (re)binds when the binding is absent
        or stale because the driver had no open session at assignment time, or
        rotated/closed their session between assignment and progress. It is no
        longer the primary fix for missing-binding-at-assignment.

        Carry-over / late-bind: whenever the driver has an open session, the
        order is (re)bound onto it — provided that session has capacity,
        otherwise ``BOTTLE_SESSION_CAPACITY_EXCEEDED`` is raised. This covers
        both an order accepted under a now-closed/different session (carry-over)
        and an order that was never bound at all because it was assigned outside
        the bot-accept flow — admin assignment and ``auto_assign_delivery_task``
        set the driver without creating a binding (late-bind). Either way the
        order lives on the session the driver is physically operating under.

        Only when the driver has *no* open session at all is this a hard error:
        under strict enforcement (``BOTTLE_SESSION_ENFORCEMENT_STRICT``) it
        raises ``BOTTLE_SESSION_REQUIRED``; in legacy mode it logs and returns
        ``None`` so the legacy flow is unaffected. Returns the session the order
        is (now) bound to, or ``None`` when no enforcement applies.
        """
        order = getattr(delivery, "order", None)
        if not order:
            return None
        bottles_needed = self.calculate_bottles_for_order(order)
        if bottles_needed <= 0:
            return None

        strict = self._strict_enforcement_enabled()

        def _violation(message: str, error_code: str) -> None:
            if strict:
                raise ValidationError(message, error_code=error_code)
            logger.warning(
                "[BOTTLE] (legacy) %s order=%s delivery=%s code=%s",
                message,
                order.id,
                delivery.id,
                error_code,
            )

        if delivery.delivery_person_id is None:
            _violation(
                f"Delivery {delivery.id} has no driver assigned; " "cannot validate bottle session.",
                "BOTTLE_SESSION_REQUIRED",
            )
            return None

        binding = DriverBottleSessionOrder.query.filter_by(order_id=order.id).first()
        bound = DriverBottleSession.query.get(binding.session_id) if binding else None
        effective = self.get_effective_session(delivery.delivery_person_id)

        # Happy path: the order is already on the driver's current open session.
        if (
            binding is not None
            and bound is not None
            and bound.status == DriverBottleSessionStatus.OPEN
            and effective is not None
            and effective.id == bound.id
        ):
            return bound

        # (Re)bind the order onto the driver's current open session. One path
        # covers two shapes that both mean "the order needs to live on the
        # session the driver is physically operating under":
        #   * carry-over — the order was bound under a different / now-closed
        #     session, but the driver has since opened (or joined) another, and
        #   * late-bind — the order was never bound at all because it was
        #     assigned outside the bot-accept flow (admin assignment /
        #     auto_assign_delivery_task create no binding), then surfaced to a
        #     driver who has an open session.
        # Capacity is enforced here exactly as at accept time: refuse if the
        # session can't cover the load. rebind_order_to_session creates the
        # binding when none exists yet, so both shapes share this code path.
        if effective is not None:
            self.assert_delivery_within_session_capacity(effective, int(bottles_needed))
            previous_session_id = binding.session_id if binding else None
            self.rebind_order_to_session(
                order.id,
                effective.id,
                accepted_by_driver_id=delivery.delivery_person_id,
            )
            logger.info(
                "[BOTTLE] %s order=%s delivery=%s session=%s→%s",
                "carry-over" if binding is not None else "late-bind",
                order.id,
                delivery.id,
                previous_session_id,
                effective.id,
            )
            return effective

        # The driver has no open session at all — they must open a new one
        # before they can progress (and thereby take ownership of) this order.
        if binding is None:
            _violation(
                f"Order {order.id} has no bottle-session binding and driver "
                f"{delivery.delivery_person_id} has no open bottle session; open a "
                "session to continue delivering this order.",
                "BOTTLE_SESSION_REQUIRED",
            )
        else:
            _violation(
                f"Order {order.id}'s bound session {binding.session_id} is not open and "
                f"driver {delivery.delivery_person_id} has no open bottle session; open a "
                "new session to continue delivering this order.",
                "BOTTLE_SESSION_REQUIRED",
            )
        return None

    @staticmethod
    def _open_bindings_query_for_session(session_id: int):
        """Bindings on this session whose order is not in a terminal status."""
        return (
            DriverBottleSessionOrder.query.join(Order, DriverBottleSessionOrder.order_id == Order.id)
            .filter(DriverBottleSessionOrder.session_id == session_id)
            .filter(Order.status.notin_([OrderStatus.DELIVERED, OrderStatus.CANCELLED, OrderStatus.RETURNED]))
        )

    @staticmethod
    def _open_bindings_count_for_session(session_id: int) -> int:
        """Count bindings on this session whose order is not in a terminal status."""
        return BottleTrackingService._open_bindings_query_for_session(session_id).count()

    def _release_open_bindings_for_session(self, session_id: int) -> list:
        """Release (unbind) every non-terminal order bound to this session so it can
        close. Terminal (delivered/cancelled/returned) bindings are KEPT for the
        historical tally. Returns the released order ids.

        Deleting the binding — rather than leaving it on the now-closed session — is
        deliberate: the delivery-time tally credits ``binding.session_id`` with no
        open-session check, so a stale binding would corrupt this sealed session's
        counters if the order were later delivered. Each released order re-binds to
        the driver's next open session via the late-bind guard when next progressed.
        """
        order_ids = [
            row[0]
            for row in self._open_bindings_query_for_session(session_id)
            .with_entities(DriverBottleSessionOrder.order_id)
            .all()
        ]
        for order_id in order_ids:
            self.unbind_order(order_id)
        return order_ids

    def update_session_delivery_tally(
        self,
        driver_user_id: int,
        *,
        bottles_delivered: int = 0,
        bottles_collected: int = 0,
    ) -> Optional[DriverBottleSession]:
        """Increment session delivery/collection counters after each ledger write.

        Uses the driver's *effective* session — their own OPEN session if they
        have one, otherwise the session they have joined as a co-driver member.
        No-op if the driver has no effective session (backward-compatible).
        """
        logger.info(
            "[BOTTLE] update_session_delivery_tally driver=%s delivered=%s collected=%s",
            driver_user_id,
            bottles_delivered,
            bottles_collected,
        )
        session = self.get_effective_session(driver_user_id)
        if not session:
            logger.info(
                "[BOTTLE] update_session_delivery_tally driver=%s no effective session, skipping", driver_user_id
            )
            return None
        prev_delivered = session.bottles_delivered or 0
        prev_collected = session.bottles_collected_from_customers or 0
        session.bottles_delivered = prev_delivered + bottles_delivered
        session.bottles_collected_from_customers = prev_collected + bottles_collected
        db.session.flush()
        logger.info(
            f"[BOTTLE] update_session_delivery_tally OK "
            f"session={session.id} "
            f"delivered={prev_delivered}→{session.bottles_delivered} "
            f"collected={prev_collected}→{session.bottles_collected_from_customers}"
        )
        return session

    # ------------------------------------------------------------------
    # Driver-to-driver bottle transfers
    # ------------------------------------------------------------------

    @transactional
    def initiate_bottle_transfer(
        self,
        sender_driver_id: int,
        receiver_driver_id: int,
        declared_quantity: int,
        *,
        notes: str = None,
    ) -> DriverBottleTransfer:
        """Sender initiates a mid-route transfer of bottles to another driver.

        Immediately deducts declared_quantity from sender's session inventory.
        Raises ConflictError if sender has no open session.
        Raises ValidationError if quantity exceeds sender's current inventory.
        """
        if sender_driver_id == receiver_driver_id:
            raise ValidationError("Sender and receiver cannot be the same driver")
        if declared_quantity <= 0:
            raise ValidationError("Transfer quantity must be greater than zero")

        sender_session = self._get_open_session_or_raise(sender_driver_id)

        if declared_quantity > sender_session.current_inventory:
            raise ValidationError(
                f"Cannot transfer {declared_quantity} bottle(s); "
                f"sender only has {sender_session.current_inventory} on truck."
            )

        # Deduct immediately (pessimistic) to prevent over-delivery
        sender_session.bottles_transferred_out = (sender_session.bottles_transferred_out or 0) + declared_quantity

        transfer = DriverBottleTransfer(
            sender_session_id=sender_session.id,
            sender_driver_id=sender_driver_id,
            receiver_driver_id=receiver_driver_id,
            declared_quantity=declared_quantity,
            status=DriverBottleTransferStatus.PENDING,
            notes=notes,
        )
        db.session.add(transfer)
        db.session.flush()
        return transfer

    @transactional
    def confirm_bottle_transfer(
        self,
        transfer_id: int,
        receiver_driver_id: int,
        confirmed_quantity: int,
        *,
        notes: str = None,
    ) -> DriverBottleTransfer:
        """Receiver confirms (or disputes) a pending transfer.

        Quantities match → CONFIRMED; mismatch → DISPUTED.
        Credits confirmed_quantity to receiver's open session.
        Receiver must have an open session before confirming.
        """
        transfer = DriverBottleTransfer.query.get(transfer_id)
        if not transfer:
            raise NotFoundError("Transfer not found")
        if transfer.receiver_driver_id != receiver_driver_id:
            raise ConflictError("Only the designated receiver can confirm this transfer")
        if transfer.status != DriverBottleTransferStatus.PENDING:
            raise ConflictError(f"Transfer is already {transfer.status.value}")
        if confirmed_quantity < 0:
            raise ValidationError("confirmed_quantity cannot be negative")

        receiver_session = self.get_open_session(receiver_driver_id)
        if not receiver_session:
            raise ConflictError(
                "Receiver must have an open bottle session to accept a transfer. " "Open a session first.",
                error_code="BOTTLE_SESSION_NOT_FOUND",
            )

        # Credit the receiver's session
        receiver_session.bottles_transferred_in = (receiver_session.bottles_transferred_in or 0) + confirmed_quantity
        transfer.receiver_session_id = receiver_session.id
        transfer.confirmed_quantity = confirmed_quantity
        transfer.confirmed_at = self._utc_now()
        if notes:
            transfer.notes = (transfer.notes or "") + f"\n{notes}"

        if confirmed_quantity == transfer.declared_quantity:
            transfer.status = DriverBottleTransferStatus.CONFIRMED
        else:
            transfer.status = DriverBottleTransferStatus.DISPUTED
            if notes:
                transfer.dispute_notes = notes

        db.session.flush()
        return transfer

    @transactional
    def admin_resolve_transfer_dispute(
        self,
        transfer_id: int,
        actor_user_id: int,
        resolved_quantity: int,
        *,
        resolution_notes: str,
    ) -> DriverBottleTransfer:
        """Admin arbitrates a disputed transfer.

        Adjusts sender and receiver session tallies to use resolved_quantity.
        """
        transfer = DriverBottleTransfer.query.get(transfer_id)
        if not transfer:
            raise NotFoundError("Transfer not found")
        if transfer.status != DriverBottleTransferStatus.DISPUTED:
            raise ConflictError("Can only resolve DISPUTED transfers")
        if not resolution_notes or not resolution_notes.strip():
            raise ValidationError("resolution_notes is required")
        if resolved_quantity < 0:
            raise ValidationError("resolved_quantity cannot be negative")

        # Adjust sender session: replace declared with resolved
        delta_out = resolved_quantity - transfer.declared_quantity
        transfer.sender_session.bottles_transferred_out = (
            transfer.sender_session.bottles_transferred_out or 0
        ) + delta_out

        # Adjust receiver session: replace confirmed with resolved
        if transfer.receiver_session:
            delta_in = resolved_quantity - (transfer.confirmed_quantity or 0)
            transfer.receiver_session.bottles_transferred_in = (
                transfer.receiver_session.bottles_transferred_in or 0
            ) + delta_in

        transfer.confirmed_quantity = resolved_quantity
        transfer.status = DriverBottleTransferStatus.RESOLVED
        transfer.resolved_at = self._utc_now()
        transfer.resolved_by_user_id = actor_user_id
        transfer.resolution_notes = resolution_notes.strip()
        db.session.flush()
        return transfer

    # ------------------------------------------------------------------
    # Session read operations
    # ------------------------------------------------------------------

    @staticmethod
    def get_session_detail(session_id: int) -> Optional[DriverBottleSession]:
        """Fetch a session with orders, transfers, and memberships pre-loaded."""
        return DriverBottleSession.query.options(
            joinedload(DriverBottleSession.driver),
            joinedload(DriverBottleSession.session_orders)
            .joinedload(DriverBottleSessionOrder.order)
            .joinedload(Order.user),
            joinedload(DriverBottleSession.session_orders)
            .joinedload(DriverBottleSessionOrder.order)
            .joinedload(Order.order_items)
            .joinedload(OrderItem.product),
            joinedload(DriverBottleSession.session_orders).joinedload(DriverBottleSessionOrder.accepted_by_driver),
            joinedload(DriverBottleSession.transfers_out).joinedload(DriverBottleTransfer.receiver_driver),
            joinedload(DriverBottleSession.transfers_in).joinedload(DriverBottleTransfer.sender_driver),
            joinedload(DriverBottleSession.memberships).joinedload(DriverSessionMembership.member_driver),
        ).get(session_id)

    @staticmethod
    def get_driver_sessions(
        driver_user_id: int,
        page: int = 1,
        per_page: int = 20,
        status: str = None,
    ) -> Dict:
        """Get paginated session history for a specific driver."""
        query = DriverBottleSession.query.filter_by(driver_user_id=driver_user_id).options(
            joinedload(DriverBottleSession.driver)
        )

        if status:
            query = query.filter(DriverBottleSession.status == DriverBottleSessionStatus(status))

        query = query.order_by(DriverBottleSession.started_at.desc())
        total = query.count()
        items = query.offset((page - 1) * per_page).limit(per_page).all()
        return {
            "items": items,
            "total": total,
            "page": page,
            "per_page": per_page,
            "pages": (total + per_page - 1) // per_page if per_page else 0,
        }

    @staticmethod
    def get_all_sessions(
        page: int = 1,
        per_page: int = 20,
        driver_user_id: int = None,
        status: str = None,
        only_discrepancies: bool = False,
        start_date: date = None,
        end_date: date = None,
    ) -> Dict:
        """Get paginated session list for admin with optional filters."""
        query = DriverBottleSession.query.options(joinedload(DriverBottleSession.driver))

        if driver_user_id:
            query = query.filter(DriverBottleSession.driver_user_id == driver_user_id)
        if status:
            query = query.filter(DriverBottleSession.status == DriverBottleSessionStatus(status))
        if only_discrepancies:
            query = query.filter(
                DriverBottleSession.discrepancy.isnot(None),
                DriverBottleSession.discrepancy != 0,
            )
        if start_date:
            query = query.filter(func.date(DriverBottleSession.started_at) >= start_date)
        if end_date:
            query = query.filter(func.date(DriverBottleSession.started_at) <= end_date)

        query = query.order_by(DriverBottleSession.started_at.desc())
        total = query.count()
        items = query.offset((page - 1) * per_page).limit(per_page).all()
        return {
            "items": items,
            "total": total,
            "page": page,
            "per_page": per_page,
            "pages": (total + per_page - 1) // per_page if per_page else 0,
        }

    @staticmethod
    def get_pending_transfers_for_driver(
        driver_user_id: int,
    ) -> List[DriverBottleTransfer]:
        """Return transfers pending confirmation by this driver (as receiver)."""
        return (
            DriverBottleTransfer.query.filter_by(
                receiver_driver_id=driver_user_id,
                status=DriverBottleTransferStatus.PENDING,
            )
            .options(
                joinedload(DriverBottleTransfer.sender_driver),
                joinedload(DriverBottleTransfer.sender_session),
            )
            .order_by(DriverBottleTransfer.sent_at.desc())
            .all()
        )

    @staticmethod
    def get_all_transfers(
        page: int = 1,
        per_page: int = 20,
        status: str = None,
        driver_user_id: int = None,
    ) -> Dict:
        """Get paginated transfer list for admin."""
        query = DriverBottleTransfer.query.options(
            joinedload(DriverBottleTransfer.sender_driver),
            joinedload(DriverBottleTransfer.receiver_driver),
        )

        if status:
            query = query.filter(DriverBottleTransfer.status == DriverBottleTransferStatus(status))
        if driver_user_id:
            query = query.filter(
                or_(
                    DriverBottleTransfer.sender_driver_id == driver_user_id,
                    DriverBottleTransfer.receiver_driver_id == driver_user_id,
                )
            )

        query = query.order_by(DriverBottleTransfer.sent_at.desc())
        total = query.count()
        items = query.offset((page - 1) * per_page).limit(per_page).all()
        return {
            "items": [t.to_dict() for t in items],
            "total": total,
            "page": page,
            "per_page": per_page,
            "pages": (total + per_page - 1) // per_page if per_page else 0,
        }
