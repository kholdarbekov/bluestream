"""Service for COD cash collection, receivable allocation, and debt rules."""

import logging
from dataclasses import dataclass, field
from datetime import datetime, UTC
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Callable, Dict, Iterable, List, Optional, Tuple

from sqlalchemy import and_, func, or_
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
from business_app.models.user import User, UserAddress
from business_app.utils.audit_logger import AuditEventType, AuditSeverity, audit_logger
from shared.enums import (
    CashCollectionSource,
    OrderStatus,
    PaymentMethod,
    PaymentStatus,
    UserRole,
)
from business_app.utils.exceptions import NotFoundError, ValidationError
from business_app.utils.payment_projection import (
    ONLINE_PAYABLE_METHOD_VALUES,
    unpaid_after_delivery_clause,
    get_payment_projection,
    has_open_receivable,
    is_ledger_receivable,
    net_open_receivable_amount,
    open_receivable_amount,
    open_receivable_clause,
    reserved_prepayment_amount,
)
from business_app.utils.state_validators import assert_cash_payment_collector


if TYPE_CHECKING:  # pragma: no cover - typing only
    from business_app.services.allocation_scope import AllocationScope


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Money the business ALREADY HOLDS, re-booked as customer credit.
# ---------------------------------------------------------------------------
# A `CashCollectionEvent` normally means "cash came in". These two flows do not:
# they take money that has already been counted once — a card/Click payment the
# gateway settled, or a door collection already booked as an allocation — and
# re-express it as spendable customer credit.
#
# 🔴 THIS IS A REPORTING DISCRIMINATOR, and the reason it has to exist:
# `AdminReportService.get_financial_summary` counts electronic money through
# `Payment.amount_collected` and cash money through live allocations. When one
# of these events is later allocated to a COD debt, the SAME money lands in both
# totals and `total_revenue` (their sum) reports it twice. `prepaid_reservation`
# allocations are excluded there for the same reason, one layer down.
#
# Written down ONCE, here, next to the code that stamps the marker.
CUSTOMER_CREDIT_REBOOK_FLOWS = frozenset(
    {
        # B4a: a card/Click order was cancelled. The gateway is never reversed
        # (the fiscal receipt cannot be un-filed), so the money becomes credit.
        "order_cancel_prepaid_credit",
        # OrderEditService._cascade_cash: a paid order was edited DOWN and the
        # difference handed back as credit rather than refunded to the card.
        "order_edit_refund",
    }
)

# Events written before `proof_data['flow']` was stamped carry the marker only in
# their idempotency key. `OrderEditService._cascade_cash` is the sole writer of
# this prefix, so matching it cannot over-net.
_LEGACY_REBOOK_IDEMPOTENCY_PREFIX = "order_edit_refund:"


def customer_credit_rebook_event_clause():
    """SQL predicate: is this event previously-counted money re-booked as credit?

    The ONE expression of :data:`CUSTOMER_CREDIT_REBOOK_FLOWS`, used both by the
    B4a netting (so a cancel never re-credits an edit-down that already paid the
    customer back) and by the financial summary (so the same money is not
    counted as revenue twice). Both `coalesce`s are load-bearing: without them a
    NULL `proof_data` or a NULL `idempotency_key` makes the whole disjunction
    NULL, and `~clause` then silently drops ordinary door-cash events from the
    report.
    """
    flow = func.coalesce(CashCollectionEvent.proof_data["flow"].as_string(), "")
    key = func.coalesce(CashCollectionEvent.idempotency_key, "")
    return or_(
        flow.in_(sorted(CUSTOMER_CREDIT_REBOOK_FLOWS)),
        key.like(f"{_LEGACY_REBOOK_IDEMPOTENCY_PREFIX}%"),
    )


@dataclass
class PersonalCardTransferPlan:
    """Projection of where a personal card transfer's money would land.

    Mirrors the allocation order ``post_collection`` actually performs: the target
    order settles first (never over-allocated), the surplus spills onto the
    scope's other delivered COD debts oldest-first, and only what no debt can
    absorb becomes customer credit.
    """

    order_id: int
    order_number: Optional[str]
    amount: Decimal
    applied_to_order: Decimal
    order_outstanding_before: Decimal
    order_outstanding_after: Decimal
    applied_to_other_debts: Decimal
    remaining_as_credit: Decimal
    spill_allocations: List[Dict[str, Any]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def to_summary(self) -> Dict[str, Any]:
        return {
            "order_id": self.order_id,
            "order_number": self.order_number,
            "amount": float(self.amount),
            "applied_to_order": float(self.applied_to_order),
            "order_outstanding_before": float(self.order_outstanding_before),
            "order_outstanding_after": float(self.order_outstanding_after),
            "applied_to_other_debts": float(self.applied_to_other_debts),
            "remaining_as_credit": float(self.remaining_as_credit),
            "spill_allocations": [
                {
                    "order_id": spill["order_id"],
                    "order_number": spill["order_number"],
                    "amount": float(spill["amount"]),
                    "outstanding_before": float(spill["outstanding_before"]),
                    "outstanding_after": float(spill["outstanding_after"]),
                }
                for spill in self.spill_allocations
            ],
            "warnings": self.warnings,
        }


class CashCollectionService:
    """COD receivable and cash collection service."""

    COD_ACTIVE_DEBT_LIMIT = 2

    # Terminal, non-collectible order states.
    _TERMINAL_ORDER_STATUSES = frozenset({OrderStatus.CANCELLED, OrderStatus.RETURNED})

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

    def get_active_cod_payments_for_customer(self, customer_id: int) -> List[Payment]:
        """Read-only: the customer's open delivered COD debts, oldest-first.

        Deliberately lock-free. Row locks on ``payments`` are taken ONLY by
        :meth:`_lock_payments_by_ids` (ordered by ``payments.id``); a second
        locking query ordered by ``Order.created_at`` — which this method used
        to offer via a ``for_update`` flag — is the deadlock pair that flag
        existed to create.
        """
        return self._active_cod_payments_query(customer_id).all()

    def _active_cod_payments_query(self, customer_id: int):
        return self._active_cod_payments_query_for_users([customer_id])

    def _active_cod_payments_query_for_users(self, user_ids: List[int]):
        """Open delivered receivables for a set of users, oldest-first.

        🔴 RAIL-AGNOSTIC SINCE 2026-08-08 — read this before touching any of the
        eleven queries in this module that now call `open_receivable_clause()`.

        These used to read `Payment.payment_method == PaymentMethod.CASH` AND
        `outstanding_amount > 0`. The method conjunct was a PROXY for "is this a
        receivable" — the other two conjuncts already ARE the test — and the
        proxy silently hid every card-paid order whose total was edited upward
        after settlement. Prod order 961 (2026-08-07): 2 bottles paid by Click,
        a 3rd added at the door, `_recompute_totals` re-priced the payment
        correctly to PARTIALLY_PAID with a real outstanding, and the debt was
        then invisible to the debtor list, the driver's screen, the financial
        report and every collection flow simultaneously.

        `open_receivable_clause()` is the SSOT (business_app/utils/payment_projection.py).
        The `Order.status == DELIVERED` conjunct is NOT part of it and must stay
        at each call site — it is the only thing keeping a repriced-then-cancelled
        order out of these results.

        WHAT DID NOT WIDEN, deliberately: `auto_reserve_against_pending_payments`
        (a different axis — RESERVABLE_ORDER_STATUSES), the terminal allocator
        filter in `_plan_allocation` and its keep-settled preview mirrors (so an
        unrelated cash collection can never drift onto an electronic payment; the
        electronic receivable is reachable only through the explicit target
        path), and the whole customer-credit family (credit is cash-only-usable).
        """
        return (
            Payment.query.join(Order, Payment.order_id == Order.id)
            .options(contains_eager(Payment.order))
            .filter(
                Payment.user_id.in_(user_ids),
                open_receivable_clause(),
                Order.status == OrderStatus.DELIVERED,
            )
            .order_by(Order.created_at.asc(), Payment.created_at.asc(), Payment.id.asc())
        )

    def _active_cod_payment_ids_for_users(self, user_ids: List[int]) -> List[int]:
        """Ids only, no locks — phase 1 of the two-phase lock discipline (spec 5.3)."""
        rows = (
            db.session.query(Payment.id)
            .join(Order, Payment.order_id == Order.id)
            .filter(
                Payment.user_id.in_(user_ids),
                open_receivable_clause(),
                Order.status == OrderStatus.DELIVERED,
            )
            .all()
        )
        return [r[0] for r in rows]

    def _lock_payments_by_ids(self, payment_ids: Iterable[int]) -> Dict[int, Payment]:
        """Phase 2 of the two-phase lock discipline (spec 5.3 / R6).

        THE single place any allocation path takes payment row locks. Every
        candidate is acquired in ONE query ``ORDER BY payments.id ASC``, so
        concurrent posts over overlapping rows can never request the same two
        rows in opposite orders. Allocation ordering (ring, then oldest-first)
        is re-applied purely in memory over the already-locked rows.

        This must stay the ONLY lock order in play. A place- or cluster-scoped
        post and a plain personal post routinely touch the same payment (a ring-1
        member who is themselves unlinked still posts personally), so a second
        path locking by ``Order.created_at`` reintroduces the deadlock this
        query exists to prevent. The failure mode is worse than a 500:
        ``_allocate_to_payment`` may already have enqueued
        ``send_payment_confirmation_task`` — which performs no status re-check
        and does not roll back — before blocking, so a deadlock abort can tell a
        customer their payment was confirmed while the transaction rolled back.

        ``populate_existing()`` is NOT optional here. This batch deliberately
        includes the current order's payment, which need not be DELIVERED, so the
        business predicates that guard :meth:`_lock_credit_events_by_ids` (where
        ``FOR UPDATE`` re-qualification drops a row that stopped qualifying while
        we were blocked) cannot live on this query. Without re-qualification the
        ONLY protection left is that the locked rows carry live values — and a
        locking ``SELECT`` does not refresh the column attributes of a row already
        in the session identity map (the identity map wins; the fetched values are
        discarded). The stale-read is a real lost update: a staff delivery loads
        ``delivery.order.payment`` at outstanding=3000, a concurrent collection
        settles it in full and commits, we unblock, ``live_outstanding`` still
        reads 3000, ``_allocate_to_payment``'s over-allocation guard compares the
        same stale 3000 and passes, and ``amount_collected = stale(0) + 3000``
        CLOBBERS the committed write. ``populate_existing`` makes the lock refresh
        what it returns, so every downstream read sees the row we actually hold.
        """
        ids = sorted({int(pid) for pid in payment_ids})
        if not ids:
            return {}
        locked = (
            Payment.query.filter(Payment.id.in_(ids))
            .order_by(Payment.id.asc())
            .with_for_update(of=Payment)
            .populate_existing()
            .all()
        )
        return {payment.id: payment for payment in locked}

    def _active_place_cod_payment_ids(self, address_ids: List[int]) -> List[int]:
        """Ring-1 candidate ids: open COD debts delivered to a place's member
        addresses, ANY owner. Ids only, no locks (phase 1)."""
        rows = (
            db.session.query(Payment.id)
            .join(Order, Payment.order_id == Order.id)
            .filter(
                Order.delivery_address_id.in_(address_ids),
                open_receivable_clause(),
                Order.status == OrderStatus.DELIVERED,
            )
            .all()
        )
        return [r[0] for r in rows]

    def get_active_cod_payments_for_scope(self, scope: "AllocationScope") -> List[Payment]:
        """Active COD debts visible to a scope: place ring 1 + orderer-cluster
        ring 2 for PLACE, cluster for CLUSTER/PERSONAL. Read-only (no locks)."""
        cluster = self._active_cod_payments_query_for_users(list(scope.orderer_cluster_user_ids)).all()
        if scope.scope_type != "place":
            return cluster
        ring1_ids = set(self._active_place_cod_payment_ids(list(scope.address_ids)))
        if not ring1_ids:
            return cluster
        ring1 = Payment.query.options(joinedload(Payment.order)).filter(Payment.id.in_(ring1_ids)).all()
        rank1 = self._oldest_first_rank(list(ring1_ids))
        ring1.sort(key=lambda p: rank1[p.id])
        return ring1 + [p for p in cluster if p.id not in ring1_ids]

    def get_active_cod_debt_count(self, customer_id: int) -> int:
        return len(self.get_active_cod_payments_for_customer(customer_id))

    def _cluster_members(self, customer_id: int):
        from business_app.services.customer_link_service import CustomerLinkService

        cluster_ids = CustomerLinkService().get_cluster_user_ids(customer_id)
        return User.query.filter(User.id.in_(cluster_ids)).all()

    def _credit_pool_user_ids(self, user_ids: Iterable[int]) -> List[int]:
        """Every account whose credit ``user_ids`` can collectively draw on.

        The union of each id's cluster, resolved in two plain FK selects rather
        than one ``get_cluster_user_ids`` call per id (never ``join(User)`` —
        multi-FK gotcha). Used by the ring-3 sweep, whose candidate universe is
        supplied by the caller and therefore need not be a single cluster.
        """
        ids = {int(uid) for uid in user_ids}
        if not ids:
            return []
        canonical_ids = {
            r[0]
            for r in db.session.query(User.canonical_customer_id)
            .filter(User.id.in_(ids), User.canonical_customer_id.isnot(None))
            .all()
        }
        if canonical_ids:
            ids |= {r[0] for r in db.session.query(User.id).filter(User.canonical_customer_id.in_(canonical_ids)).all()}
        return sorted(ids)

    def _cluster_has_cod_exempt_member(self, customer_id: int) -> bool:
        return any(bool(m.cod_debt_check_exempt) for m in self._cluster_members(customer_id))

    def _cluster_has_grocery_member(self, customer_id: int) -> bool:
        return any(bool(m.is_grocery_store) for m in self._cluster_members(customer_id))

    def get_cluster_active_cod_debt_count(self, customer_id: int) -> int:
        """Active COD debts across the customer's whole linked cluster.

        One real person = one credit line. For an unlinked user the cluster is
        [customer_id], so this equals the per-account count. Selection for cash
        *allocation* is intentionally NOT changed (that stays per-account until
        Phase 2) — only the cap decision widens.
        """
        from business_app.services.customer_link_service import CustomerLinkService

        cluster_ids = CustomerLinkService().get_cluster_user_ids(customer_id)
        count = (
            db.session.query(func.count(Payment.id))
            .join(Order, Payment.order_id == Order.id)
            .filter(
                Payment.user_id.in_(cluster_ids),
                unpaid_after_delivery_clause(),
                Order.status == OrderStatus.DELIVERED,
            )
            .scalar()
        )
        return int(count or 0)

    def get_place_active_cod_debt_count(self, address_id: int) -> int:
        """Open delivered COD debts delivered to ANY member address of this
        address's place group — any owner, regardless of exemptions (spec 5.5).
        Ungrouped addresses degrade to the single address."""
        from business_app.services.customer_link_service import CustomerLinkService

        member_ids = CustomerLinkService().get_address_group_member_ids(address_id)
        count = (
            db.session.query(func.count(Payment.id))
            .join(Order, Payment.order_id == Order.id)
            .filter(
                Order.delivery_address_id.in_(member_ids),
                unpaid_after_delivery_clause(),
                Order.status == OrderStatus.DELIVERED,
            )
            .scalar()
        )
        return int(count or 0)

    def get_place_open_cod_debt_total(self, group_id: int) -> Decimal:
        """Sum of outstanding COD debt across a place group's addresses."""
        address_ids = [
            r[0] for r in db.session.query(UserAddress.id).filter(UserAddress.address_group_id == group_id).all()
        ]
        if not address_ids:
            return Decimal("0.00")
        total = db.session.query(func.coalesce(func.sum(Payment.outstanding_amount), Decimal("0.00"))).select_from(
            Payment
        ).join(Order, Payment.order_id == Order.id).filter(
            Order.delivery_address_id.in_(address_ids),
            open_receivable_clause(),
            Order.status == OrderStatus.DELIVERED,
        ).scalar() or Decimal(
            "0.00"
        )
        return self._to_decimal(total)

    def get_place_cod_context(self, address_id: Optional[int]) -> Dict[str, Any]:
        """Place-group COD context for one delivery address (spec 8).

        The block spread into every driver delivery card. Ungrouped (or absent)
        addresses return the all-falsy/zero shape, so an ungrouped customer's
        payload is byte-identical to today plus constant-false fields.
        Money values are floats because this is consumed straight by the API
        boundary; nothing here moves money.
        """
        empty = {
            "is_place_grouped": False,
            "place_group_id": None,
            "place_group_label": None,
            "place_outstanding_cod_total": 0.0,
            "place_active_cod_debt_count": 0,
        }
        if not address_id:
            return empty

        from business_app.models.customer_link import AddressGroup

        group_id = db.session.query(UserAddress.address_group_id).filter(UserAddress.id == address_id).scalar()
        if not group_id:
            return empty

        group = AddressGroup.query.get(group_id)
        return {
            "is_place_grouped": True,
            "place_group_id": group_id,
            "place_group_label": group.label if group else None,
            "place_outstanding_cod_total": float(self.get_place_open_cod_debt_total(group_id)),
            "place_active_cod_debt_count": self.get_place_active_cod_debt_count(address_id),
        }

    def is_customer_cod_restricted(self, customer_id: int) -> bool:
        # Exemption is OR-ed across the whole cluster: any exempt or grocery member
        # exempts the cluster. PERSON arm only — this is the address-less entry
        # point, so it deliberately mirrors get_cod_restriction_context(customer_id)
        # with no delivery_address_id. Callers that know the destination address
        # must use get_cod_restriction_context / validate_customer_can_use_cod so
        # the PLACE arm is evaluated too (spec 5.5).
        if self._cluster_has_cod_exempt_member(customer_id):
            return False
        if self._cluster_has_grocery_member(customer_id):
            return False
        return self.get_cluster_active_cod_debt_count(customer_id) >= self.COD_ACTIVE_DEBT_LIMIT

    def get_cod_restricted_flags(self, user_ids: List[int]) -> Dict[int, bool]:
        """Cluster-aware ``cod_restricted`` for a batch of users.

        Returns exactly what :meth:`is_customer_cod_restricted` returns for each
        id — that method is the SSOT for the PERSON-arm rule and this is only a
        batched way to ask it (pinned by
        ``test_get_cod_restricted_flags_matches_single_calls``). Unlinked users
        are their own singleton cluster, so their flag is byte-identical to the
        pre-Phase-2 per-account answer whenever they are neither exempt nor a
        grocery store.

        Resolved in a bounded three queries rather than ~7 per user, because the
        list surfaces that consume it — the 200-row admin debtor list and the
        *unpaginated* admin customer map — would otherwise turn one query into
        thousands. Like :meth:`is_customer_cod_restricted` this is the
        address-less PERSON arm only; callers that know the destination address
        must use :meth:`get_cod_restriction_context` so the PLACE arm is
        evaluated too (spec 5.5).
        """
        ids = sorted({int(uid) for uid in user_ids})
        if not ids:
            return {}

        # (1) canonical id per requested user — plain FK select, never join(User).
        canonical_by_user: Dict[int, Optional[int]] = {
            int(row[0]): row[1]
            for row in db.session.query(User.id, User.canonical_customer_id).filter(User.id.in_(ids)).all()
        }
        canonical_ids = sorted({c for c in canonical_by_user.values() if c is not None})

        # (2) every member of every touched cluster, as ORM rows so the exemption
        # reads stay `cod_debt_check_exempt` / `is_grocery_store` — the very
        # attributes `_cluster_has_cod_exempt_member` / `_cluster_has_grocery_member`
        # use, rather than a re-implementation of the grocery predicate.
        member_filter = User.id.in_(ids)
        if canonical_ids:
            member_filter = or_(member_filter, User.canonical_customer_id.in_(canonical_ids))
        members = User.query.filter(member_filter).all()

        def _cluster_key(user_id: int, canonical_id: Optional[int]) -> Tuple[str, int]:
            # Unlinked accounts are singleton clusters keyed on their own id;
            # the "c"/"u" tag keeps the two id spaces from colliding.
            return ("c", int(canonical_id)) if canonical_id is not None else ("u", int(user_id))

        cluster_members: Dict[Tuple[str, int], List[User]] = {}
        for member in members:
            key = _cluster_key(member.id, member.canonical_customer_id)
            cluster_members.setdefault(key, []).append(member)

        # (3) open delivered COD debts per member, one grouped count. Same debt
        # definition as get_cluster_active_cod_debt_count.
        debts_by_user: Dict[int, int] = {}
        member_ids = [m.id for m in members]
        if member_ids:
            debts_by_user = {
                int(row[0]): int(row[1] or 0)
                for row in db.session.query(Payment.user_id, func.count(Payment.id))
                .join(Order, Payment.order_id == Order.id)
                .filter(
                    Payment.user_id.in_(member_ids),
                    open_receivable_clause(),
                    Order.status == OrderStatus.DELIVERED,
                )
                .group_by(Payment.user_id)
                .all()
            }

        decisions: Dict[Tuple[str, int], bool] = {}
        for key, cluster in cluster_members.items():
            # Mirrors is_customer_cod_restricted: one exempt member OR one
            # grocery member exempts the whole cluster; otherwise the cluster's
            # total open COD debts are compared with the cap.
            if any(bool(m.cod_debt_check_exempt) for m in cluster) or any(m.is_grocery_store for m in cluster):
                decisions[key] = False
                continue
            cluster_debts = sum(debts_by_user.get(m.id, 0) for m in cluster)
            decisions[key] = cluster_debts >= self.COD_ACTIVE_DEBT_LIMIT

        # Ids with no user row degrade to "not restricted" — the same answer the
        # single-user path gives for a missing/deleted account.
        return {uid: decisions.get(_cluster_key(uid, canonical_by_user.get(uid)), False) for uid in ids}

    def get_cod_restriction_context(
        self, customer_id: int, delivery_address_id: Optional[int] = None
    ) -> Dict[str, Any]:
        """Cap by PLACE **and** by PERSON (spec 5.5).

        A new COD order is blocked when EITHER the orderer's linked cluster is at
        ``COD_ACTIVE_DEBT_LIMIT`` open delivered COD debts, OR the grouped place
        it would be delivered to is. ``restriction_scope`` tells downstream
        surfaces which arm fired so they can show the right message.

        Without ``delivery_address_id`` — or when the address is ungrouped — the
        place arm is simply not evaluated and the result is byte-identical to the
        person-only behaviour, so unlinked + ungrouped customers are unaffected.
        """
        active_debt_count = self.get_cluster_active_cod_debt_count(customer_id)
        is_cod_exempt = self._cluster_has_cod_exempt_member(customer_id)
        is_grocery_store = self._cluster_has_grocery_member(customer_id)

        # Place arm only applies when the delivery address is grouped.
        place_debt_count: Optional[int] = None
        if delivery_address_id is not None:
            group_id = (
                db.session.query(UserAddress.address_group_id).filter(UserAddress.id == delivery_address_id).scalar()
            )
            if group_id is not None:
                place_debt_count = self.get_place_active_cod_debt_count(delivery_address_id)

        # Admin exemption first, then structural grocery-store exemption, then
        # the person cap, then the place cap (spec 5.5).
        restriction_scope: Optional[str] = None
        if is_cod_exempt:
            is_restricted, reason = False, "customer_is_cod_exempt"
        elif is_grocery_store:
            is_restricted, reason = False, None
        elif active_debt_count >= self.COD_ACTIVE_DEBT_LIMIT:
            is_restricted, reason = True, "customer_has_max_active_cod_debts"
            restriction_scope = "person"
        elif place_debt_count is not None and place_debt_count >= self.COD_ACTIVE_DEBT_LIMIT:
            is_restricted, reason = True, "place_has_max_active_cod_debts"
            restriction_scope = "place"
        else:
            is_restricted, reason = False, None

        return {
            "active_cod_debt_count": active_debt_count,
            "place_active_cod_debt_count": place_debt_count,
            "cod_restricted": is_restricted,
            "restriction_scope": restriction_scope,
            # Cluster-fungible since Phase 2b (spec 5.3).
            "available_prepayment_balance": float(self.get_customer_prepaid_balance(customer_id)),
            "cod_restriction_reason": reason,
            "cod_exempt": is_cod_exempt,
            # Distinct from ``cod_exempt`` (the admin-granted flag, pinned
            # independent of grocery status by
            # test_cod_exempt_flag_independent_of_grocery_store): callers that
            # need "will the cap EVER apply to this cluster" must OR the two —
            # a grocery cluster is never restricted either, but for a
            # structural reason, not an admin one.
            "is_grocery_store": is_grocery_store,
        }

    # Sources that are physically cash handed over AT the delivery address.
    # Only these may resolve PLACE scope (spec 5.1). PERSONAL_CARD_TRANSFER is
    # identifiably the payer's own money and NEVER place-scoped; admin
    # adjustments / backfills are book corrections, not door cash.
    _PLACE_SCOPE_SOURCES = frozenset(
        {
            CashCollectionSource.DELIVERY_COMPLETION,
            CashCollectionSource.NEXT_DELIVERY,
            CashCollectionSource.STANDALONE_MEETING,
        }
    )

    def resolve_allocation_scope(
        self,
        customer_id: int,
        delivery_address_id: Optional[int] = None,
        source: Optional[Any] = None,
    ) -> "AllocationScope":
        """Resolve the allocation scope for a collection (spec 5.1). Single point.

        Grocery backstop: a grocery event customer is FORCED to personal scope
        so mirrored corporate money can never co-mingle with other accounts,
        even if the account is somehow linked or grouped (spec 5.8 layer 3).
        """
        from business_app.services.allocation_scope import AllocationScope
        from business_app.services.customer_link_service import CustomerLinkService

        customer = User.query.get(customer_id)
        if not customer:
            raise NotFoundError("Customer not found")

        if customer.is_grocery_store:
            return AllocationScope.personal(customer_id)

        source_enum = self._normalize_source(source) if source is not None else None
        link_service = CustomerLinkService()
        cluster_ids = link_service.get_cluster_user_ids(customer_id)

        if delivery_address_id is not None and source_enum in self._PLACE_SCOPE_SOURCES:
            group_id = (
                db.session.query(UserAddress.address_group_id).filter(UserAddress.id == delivery_address_id).scalar()
            )
            if group_id is not None:
                member_address_ids = link_service.get_address_group_member_ids(delivery_address_id)
                place_user_ids = sorted(
                    {
                        r[0]
                        for r in db.session.query(UserAddress.user_id)
                        .filter(UserAddress.id.in_(member_address_ids))
                        .all()
                    }
                )
                # The posting customer (or a cluster sibling) must actually be a
                # member of the place. Without this the scope-membership guard is
                # circular: it would authorise ANY stranger's order merely because
                # that order was delivered to a grouped address.
                if set(cluster_ids) & set(place_user_ids):
                    return AllocationScope.place(
                        group_id=group_id,
                        address_ids=member_address_ids,
                        place_user_ids=place_user_ids,
                        orderer_cluster_user_ids=cluster_ids,
                    )

        if len(cluster_ids) > 1:
            return AllocationScope.cluster(cluster_ids)
        return AllocationScope.personal(customer_id)

    @staticmethod
    def _scope_covers_order(scope: "AllocationScope", order: Order) -> bool:
        """Order-level arm of the scope-membership guard (spec 5.4).

        Mirrors AllocationScope.covers_payment for the pre-payment case: the
        order owner is in the scope's cluster arm, OR — place scope only —
        the order was delivered to a member address of the place.
        """
        if order.user_id in scope.orderer_cluster_user_ids:
            return True
        return scope.scope_type == "place" and order.delivery_address_id in scope.address_ids

    def _unapplied_credit_total(self, user_ids: Iterable[int]) -> Decimal:
        """Unapplied (non-voided) over-collection credit held by ``user_ids``.

        The single SQL shape behind every credit balance read, so the wallet
        cannot drift between the balance a gate checks and the pool a loop
        actually spends.
        """
        ids = [int(uid) for uid in user_ids]
        if not ids:
            return Decimal("0.00")
        total = db.session.query(func.coalesce(func.sum(CashCollectionEvent.unapplied_amount), Decimal("0.00"))).filter(
            CashCollectionEvent.customer_id.in_(ids),
            CashCollectionEvent.voided_at.is_(None),
            CashCollectionEvent.unapplied_amount > 0,
        ).scalar() or Decimal("0.00")
        return self._to_decimal(total)

    def get_customer_prepaid_balance(self, customer_id: int) -> Decimal:
        """Cluster-fungible unapplied COD over-collection balance (spec 5.3).

        One real person = one wallet, so a linked sibling's credit is part of
        this balance. For an unlinked user the cluster is ``[customer_id]``, so
        this equals the as-built per-account value byte-for-byte. Place groups
        NEVER pool credit — coworkers are different people; only their bottles
        and the place's COD debt are shared.
        """
        return self._unapplied_credit_total(self._credit_pool_for_anchor(customer_id))

    def _credit_pool_for_anchor(self, anchor_user_id: int) -> List[int]:
        """THE accounts whose unapplied credit ``anchor_user_id`` may spend.

        The single resolution point for the credit pool, so the balance a gate
        reads and the events a loop locks can never disagree about membership.

        Grocery backstop (spec 5.8 layer 3), mirroring ``resolve_allocation_scope``
        and ``post_collection``: grocery money is mirrored per-account onto a
        corporate contract, so it never co-mingles. The guard is two-sided — a
        grocery anchor is alone, and a grocery MEMBER is dropped from an
        individual anchor's pool — because either direction would otherwise let
        contract-mirrored cash settle an unrelated personal debt. Unreachable
        while linking rejects grocery accounts, but reachable the moment an
        already-linked individual is converted to a grocery entity.

        ``is_grocery_store`` is a derived Python property (user_type +
        entity_subtype), so it is evaluated on the User INSTANCE — never as a
        SQL filter.
        """
        from business_app.services.customer_link_service import CustomerLinkService

        anchor = User.query.get(anchor_user_id)
        if anchor is not None and anchor.is_grocery_store:
            return [int(anchor_user_id)]

        cluster_ids = [int(uid) for uid in CustomerLinkService().get_cluster_user_ids(anchor_user_id)]
        if len(cluster_ids) <= 1:
            return cluster_ids
        grocery_ids = {member.id for member in self._cluster_members(anchor_user_id) if member.is_grocery_store}
        if not grocery_ids:
            return cluster_ids
        return [uid for uid in cluster_ids if uid not in grocery_ids]

    def _lock_credit_events_by_ids(
        self,
        candidate_ids: Iterable[int],
        *,
        must_hold_event_ids: Iterable[int] = (),
    ) -> Dict[int, CashCollectionEvent]:
        """THE single place ``cash_collection_events`` rows are locked (spec 5.3 / R6).

        One query, ``ORDER BY cash_collection_events.id ASC``, so two transactions
        touching overlapping events always request them in the same order and can
        never deadlock. Never take a bare single-row ``FOR UPDATE`` on an event
        this batch may also contain: T1 holding E5 and requesting {E3} deadlocks
        against T2 holding E3 and blocking on E5 — and voiding E5 first does NOT
        help, because a concurrent transaction cannot see our uncommitted void
        and its own batch still blocks on the row.

        The business predicates (``voided_at IS NULL``, ``unapplied_amount > 0``)
        MUST live on THIS query rather than being applied to its result. Under
        READ COMMITTED, a locking ``SELECT`` that blocks on a row another
        transaction is updating re-evaluates *this query's own WHERE* against the
        row version that transaction committed and drops the row if it no longer
        qualifies. That re-qualification is what turns the predicates into a
        concurrency guard rather than a mere filter. Locking by id ALONE always
        re-qualifies, so we would unblock holding a just-voided event with its
        credit restored (``reverse_collection_event`` resets
        ``unapplied_amount = event.amount`` on void) and settle a real debt with
        money the business has declared never happened. An in-memory re-check is
        not an equivalent substitute either: the row is already in the session
        identity map, so a plain reload leaves its stale attributes in place.

        ``must_hold_event_ids`` is the second arm — rows the caller must hold
        regardless of credit state (an adjustment target is usually fully
        allocated, so the spendable arm would not contain it). They are acquired
        in the SAME id-ordered query; that is the entire point of the arm.

        ``populate_existing()`` covers what the predicates cannot. They correctly
        DROP a row that stopped qualifying, but a row that still qualifies can
        come back with a stale-high ``unapplied_amount``: a locking ``SELECT``
        does not refresh the column attributes of a row already in the session
        identity map, so the fetched values are discarded in favour of the stale
        ones. The consuming loops and ``_allocate_to_payment``'s "exceeds
        unapplied event balance" guard would then all agree on credit that a
        committed concurrent allocation already spent. Forcing the refresh means
        the lock and the values we allocate against describe the same row version.
        """
        ids = sorted({int(eid) for eid in candidate_ids})
        must_hold = sorted({int(eid) for eid in must_hold_event_ids})
        if not ids and not must_hold:
            return {}

        spendable_arm = and_(
            CashCollectionEvent.id.in_(ids),
            CashCollectionEvent.voided_at.is_(None),
            CashCollectionEvent.unapplied_amount > 0,
        )
        criterion = spendable_arm if not must_hold else or_(spendable_arm, CashCollectionEvent.id.in_(must_hold))
        locked = (
            CashCollectionEvent.query.filter(criterion)
            .order_by(CashCollectionEvent.id.asc())
            .with_for_update(of=CashCollectionEvent)
            .populate_existing()
            .all()
        )
        return {event.id: event for event in locked}

    def _locked_cluster_credit_events(self, anchor_user_id: int) -> List[CashCollectionEvent]:
        """Two-phase lock of the cluster's credit events (spec 5.3 / R6).

        Phase 1 resolves the candidate id set with no locks; phase 2 derives the
        consumption order IN SQL and locks all rows in ONE query ordered by id
        ASC so concurrent posts over overlapping clusters always acquire locks
        in the same order; the SQL-derived order is then reapplied in memory.

        Phase 2 repeats phase 1's business predicates rather than locking by id
        alone — see :meth:`_lock_credit_events_by_ids`: on this table they are a
        concurrency guard (``FOR UPDATE`` re-qualification), not just a filter,
        and dropping them lets a concurrently-voided event fund a live
        allocation. Neither consuming loop below re-checks ``voided_at``.

        The consumption order MUST come from SQL: CashCollectionEvent.occurred_at
        is DateTime(timezone=True) and its Python value is tz-AWARE for an event
        built+flushed in this transaction (post_collection sets
        occurred_at=datetime.now(UTC)) but NAIVE for a row reloaded from SQLite,
        so ``sorted(locked, key=lambda e: (e.occurred_at, e.id))`` raises
        ``TypeError: can't compare offset-naive and offset-aware datetimes`` on a
        live money path (post_collection -> auto_reserve_against_pending_payments
        -> reserve_customer_prepaid_credit_for_payment -> here, whenever the
        cluster already holds >= 1 other unapplied credit event).

        ``anchor_user_id`` is the account whose wallet is being spent — always
        the BENEFICIARY payment's owner, never the poster, so a payment can only
        ever draw on its own person's credit.
        """
        cluster_ids = self._credit_pool_for_anchor(anchor_user_id)
        candidate_ids = [
            r[0]
            for r in db.session.query(CashCollectionEvent.id)
            .filter(
                CashCollectionEvent.customer_id.in_(cluster_ids),
                CashCollectionEvent.voided_at.is_(None),
                CashCollectionEvent.unapplied_amount > 0,
            )
            .all()
        ]
        if not candidate_ids:
            return []
        ordered_ids = [
            r[0]
            for r in db.session.query(CashCollectionEvent.id)
            .filter(CashCollectionEvent.id.in_(candidate_ids))
            .order_by(CashCollectionEvent.occurred_at.asc(), CashCollectionEvent.id.asc())
            .all()
        ]
        by_id = self._lock_credit_events_by_ids(candidate_ids)
        return [by_id[i] for i in ordered_ids if i in by_id]

    def apply_customer_prepaid_credit_to_payment(self, payment: Payment) -> Payment:
        """Auto-apply unapplied customer cash credit to a COD payment.

        The credit pool is the payment owner's CLUSTER (spec 5.3), so a linked
        sibling's over-collection settles this debt. Never a place group.
        """
        if not payment:
            return payment
        if payment.payment_method != PaymentMethod.CASH:
            return payment
        # Net: credit already reserved against this payment is settled by
        # `consume_reserved_prepayment_for_payment`, so applying MORE credit to
        # cover that same slice would double-spend the customer's balance.
        if net_open_receivable_amount(payment) <= Decimal("0.00"):
            return payment

        unapplied_events = self._locked_cluster_credit_events(payment.user_id)

        for event in unapplied_events:
            outstanding = net_open_receivable_amount(payment)
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

    def estimate_settleable_credit_for_order(self, order: Order) -> Decimal:
        """Read-only: how much customer cash credit could settle an *increase* to
        this CASH order.

        Sums available (unapplied) prepayment plus any over-collection captured
        AT this order that is currently reserved against the customer's other
        pending orders (reclaimable). Over-collection credit is cash-only-usable,
        so non-cash orders return 0. Used by the order-edit preview/cascade to
        report how much of an increase the customer has already paid.

        The available leg is CLUSTER-fungible (spec 5.3) — it delegates to the
        widened ``get_customer_prepaid_balance``, so a linked sibling's credit
        counts toward what this order can already cover. Never place-pooled.
        """
        if not order or order.payment_method != PaymentMethod.CASH:
            return Decimal("0.00")
        available = self.get_customer_prepaid_balance(order.user_id)
        own_reserved = self._get_own_overcollection_reserved_amount(order.id)
        return self._to_decimal(available + own_reserved)

    def settle_payment_from_customer_credit(
        self,
        payment: Payment,
        *,
        actor_user_id: Optional[int] = None,
        reclaim_own_overcollection: bool = True,
    ) -> Payment:
        """Settle a CASH payment's outstanding balance from the customer's own
        cash credit, in priority order:

          1. prepayment reserved against THIS order (consume → collected),
          2. available (unapplied) prepayment credit,
          3. over-collection captured at THIS order's own delivery that is
             currently reserved against the customer's other pending orders —
             reclaimed back and re-applied.

        Used when an admin edits a paid order's total upward: the extra owed is
        first covered by cash the customer already paid (e.g. a driver who
        over-collected at the door). No-op for non-cash payments or payments
        with nothing outstanding. Caller controls the transaction (no commit).

        Steps 2 and 3 delegate to the widened primitives, so "the customer's own
        cash credit" means the payment owner's whole CLUSTER wallet (spec 5.3) —
        one person, one wallet. Credit is never pooled across a place group.
        """
        if not payment or payment.payment_method != PaymentMethod.CASH:
            return payment
        if self._to_decimal(payment.outstanding_amount) <= Decimal("0.00"):
            return payment

        # 1. This order's own reserved prepayment.
        self.consume_reserved_prepayment_for_payment(payment, collected_by=actor_user_id)

        # 2. Available (unapplied) prepayment credit.
        if self._to_decimal(payment.outstanding_amount) > Decimal("0.00"):
            self.apply_customer_prepaid_credit_to_payment(payment)

        # 3. Reclaim this order's own over-collection reserved against the
        #    customer's other pending orders, then re-apply.
        if (
            reclaim_own_overcollection
            and payment.order_id
            and self._to_decimal(payment.outstanding_amount) > Decimal("0.00")
        ):
            freed = self._reclaim_own_overcollection_reservations(
                order_id=payment.order_id,
                actor_user_id=actor_user_id,
            )
            if freed > Decimal("0.00"):
                self.apply_customer_prepaid_credit_to_payment(payment)

        return payment

    @staticmethod
    def _get_own_overcollection_reserved_amount(order_id: int) -> Decimal:
        """Sum of still-active prepaid reservations funded by cash events that
        were collected AT ``order_id`` (the order's own over-collection now held
        against the customer's other pending orders)."""
        return db.session.query(
            func.coalesce(func.sum(CashCollectionAllocation.allocated_amount), Decimal("0.00"))
        ).join(
            CashCollectionEvent,
            CashCollectionAllocation.cash_collection_event_id == CashCollectionEvent.id,
        ).filter(
            CashCollectionEvent.order_id == order_id,
            CashCollectionEvent.voided_at.is_(None),
            CashCollectionAllocation.allocation_mode == "prepaid_reservation",
            CashCollectionAllocation.reversed_at.is_(None),
        ).scalar() or Decimal(
            "0.00"
        )

    def _reclaim_own_overcollection_reservations(
        self,
        *,
        order_id: int,
        actor_user_id: Optional[int] = None,
    ) -> Decimal:
        """Release prepaid reservations funded by ``order_id``'s own over-
        collection (currently parked on the customer's other pending orders) so
        the corrected order can consume its own surplus. Returns the freed total
        (added back to the source events' unapplied balance)."""
        own_event_ids = [
            row.id
            for row in CashCollectionEvent.query.with_entities(CashCollectionEvent.id)
            .filter(
                CashCollectionEvent.order_id == order_id,
                CashCollectionEvent.voided_at.is_(None),
            )
            .all()
        ]
        if not own_event_ids:
            return Decimal("0.00")

        reservations = (
            CashCollectionAllocation.query.filter(
                CashCollectionAllocation.cash_collection_event_id.in_(own_event_ids),
                CashCollectionAllocation.allocation_mode == "prepaid_reservation",
                CashCollectionAllocation.reversed_at.is_(None),
            )
            .order_by(CashCollectionAllocation.allocated_at.asc(), CashCollectionAllocation.id.asc())
            .with_for_update(of=CashCollectionAllocation)
            .all()
        )
        if not reservations:
            return Decimal("0.00")

        now = datetime.now(UTC)
        freed = Decimal("0.00")
        affected_payments: Dict[int, Payment] = {}
        for allocation in reservations:
            amount = self._to_decimal(allocation.allocated_amount)
            event = allocation.cash_collection_event
            if event is not None:
                event.unapplied_amount = self._to_decimal(event.unapplied_amount) + amount
            allocation.reversed_at = now
            allocation.reversed_by_user_id = actor_user_id
            allocation.reversal_reason = f"Reclaimed over-collection for order #{order_id} after admin edit"
            metadata = dict(allocation.allocation_metadata or {})
            metadata["reservation_state"] = "released"
            metadata["affects_payment_projection"] = False
            allocation.allocation_metadata = metadata
            freed += amount
            if allocation.payment is not None:
                affected_payments[allocation.payment.id] = allocation.payment

        for affected_payment in affected_payments.values():
            self._sync_reserved_prepayment_projection(affected_payment)

        db.session.flush()
        return self._to_decimal(freed)

    def reserve_customer_prepaid_credit_for_payment(
        self,
        payment: Payment,
        *,
        actor_user_id: Optional[int] = None,
    ) -> Decimal:
        """Reserve available customer COD prepayment for a pending COD order payment.

        Draws on the payment owner's CLUSTER wallet (spec 5.3): one person's
        accounts share credit. Place groups never pool credit.
        """
        if not payment or payment.payment_method != PaymentMethod.CASH:
            return Decimal("0.00")

        outstanding = self._to_decimal(payment.outstanding_amount)
        existing_reserved = self._get_reserved_prepayment_amount(payment.id)
        remaining_capacity = max(Decimal("0.00"), outstanding - existing_reserved)
        if remaining_capacity <= Decimal("0.00"):
            self._sync_reserved_prepayment_projection(payment)
            return Decimal("0.00")

        unapplied_events = self._locked_cluster_credit_events(payment.user_id)

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
        collected_by: Optional[int] = None,
        settled_pre_delivery: bool = False,
    ) -> Decimal:
        """Convert reserved prepayment allocations into settled COD payment amounts.

        ``collected_by`` is a fallback collector id; the authoritative collector
        is derived from the consumed reservation's source cash-collection event
        (whoever physically collected the cash earlier).

        ``settled_pre_delivery`` tags the resulting applied allocations so a later
        cancellation/return of a not-yet-delivered order can recognise and refund
        credit that was applied at order creation (full prepaid coverage) rather
        than at delivery. See ``release_pre_delivery_prepaid_settlement_for_order``.
        """
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

        # CAPPED BY THE LIVE RECEIVABLE, and the overflow goes back to the
        # customer. A reservation can outlive the balance it was parked against:
        # the payment may have been settled from another source first (a card
        # transfer recorded before delivery — prod order AD_000630_26) or the
        # order edited down below the reserved amount. Adding it to
        # `amount_collected` regardless used to look harmless because
        # `sync_payment_projection` clamps to `payment.amount` — but the clamp
        # DESTROYS money: the allocation is stamped applied while the payment
        # cannot hold it and the funding event never gets it back, so the
        # customer's credit silently disappears and live allocations no longer
        # sum to `amount_collected`. Refunding the overflow is what keeps the
        # ledger's conservation law (live allocations + unapplied == event
        # amount) true through every ordering of settlement and delivery.
        remaining_capacity = open_receivable_amount(payment)
        consumed_total = Decimal("0.00")
        released_total = Decimal("0.00")
        collector_from_event: Optional[int] = None
        for allocation in reservations:
            amount = self._to_decimal(allocation.allocated_amount)
            if amount <= Decimal("0.00"):
                continue
            event = allocation.cash_collection_event
            consumable = min(amount, remaining_capacity)

            if consumable <= Decimal("0.00"):
                # Nothing left to settle — hand the whole reservation back.
                # `allocated_amount` is left intact so the reversed row still
                # records what had been held.
                if event is not None:
                    event.unapplied_amount = self._to_decimal(event.unapplied_amount) + amount
                allocation.reversed_at = now
                allocation.reversed_by_user_id = collected_by
                allocation.reversal_reason = "Released: payment no longer owes the reserved amount"
                metadata = dict(allocation.allocation_metadata or {})
                metadata["reservation_state"] = "released"
                metadata["reservation_released_at"] = now.isoformat()
                metadata["affects_payment_projection"] = False
                allocation.allocation_metadata = metadata
                released_total += amount
                continue

            overflow = amount - consumable
            if overflow > Decimal("0.00"):
                # Partially fits: shrink the row to what settles and refund the
                # rest, so the row keeps matching the money it moved.
                if event is not None:
                    event.unapplied_amount = self._to_decimal(event.unapplied_amount) + overflow
                allocation.allocated_amount = consumable
                released_total += overflow

            payment.amount_collected = self._to_decimal(payment.amount_collected) + consumable
            remaining_capacity -= consumable
            consumed_total += consumable
            # The cash was physically collected by the reservation's source
            # event collector (fall back to whoever recorded it).
            if collector_from_event is None and event is not None:
                collector_from_event = event.collector_user_id or event.recorded_by_user_id
            allocation.allocation_mode = "prepaid_credit"
            metadata = dict(allocation.allocation_metadata or {})
            metadata["reservation_state"] = "consumed"
            metadata["reservation_consumed_at"] = now.isoformat()
            metadata["affects_payment_projection"] = True
            if settled_pre_delivery:
                metadata["settled_pre_delivery"] = True
            allocation.allocation_metadata = metadata

        if consumed_total > Decimal("0.00"):
            self.sync_payment_projection(
                payment,
                collected_at=effective_collected_at,
                collected_by=collector_from_event or collected_by,
            )

        if released_total > Decimal("0.00"):
            # The reservation projection is a SQL SUM over live rows, so the
            # reversals/shrinks must be in the database before it is recomputed.
            db.session.flush()

        self._sync_reserved_prepayment_projection(payment)
        return self._to_decimal(consumed_total)

    def settle_new_cod_order_from_prepaid(
        self,
        payment: Payment,
        *,
        actor_user_id: Optional[int] = None,
    ) -> Decimal:
        """Settle a freshly-created COD order immediately when the customer's
        prepaid balance FULLY covers it.

        The customer already paid this cash up front, so a fully-covered new
        order should read as paid right away rather than waiting for delivery to
        consume the reservation. This consumes the reservation now (marking the
        order paid) and tags the applied credit ``settled_pre_delivery`` so a
        cancellation/return before delivery refunds it.

        Partial coverage is intentionally left as a reservation: the order still
        owes a balance the driver collects at delivery, so it must stay unpaid.
        Returns the consumed amount (``0.00`` when not fully covered).

        Must be called AFTER ``reserve_customer_prepaid_credit_for_payment`` and
        within the caller's transaction (no commit).
        """
        if not payment or payment.payment_method != PaymentMethod.CASH:
            return Decimal("0.00")

        outstanding = self._to_decimal(payment.outstanding_amount)
        if outstanding <= Decimal("0.00"):
            return Decimal("0.00")

        reserved = self._get_reserved_prepayment_amount(payment.id)
        if reserved < outstanding:
            # Only partially covered — keep the reservation; settle at delivery.
            return Decimal("0.00")

        return self.consume_reserved_prepayment_for_payment(
            payment,
            collected_by=actor_user_id,
            settled_pre_delivery=True,
        )

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

    def release_out_of_scope_reservations(
        self,
        leaving_user_ids: List[int],
        remaining_user_ids: List[int],
    ) -> int:
        """Release prepaid reservations that no longer resolve after an unlink
        (spec 5.7).

        Credit is fungible across ONE person's cluster, so a reservation may be
        funded by account A and parked on sibling account B's pending order. A
        reservation is out of scope when its funding event's customer and its
        target payment's user end up on OPPOSITE sides of the split — the two
        wallets are no longer one, so B's order may not keep holding A's money.
        Reservations whose two sides land on the SAME side stay untouched.

        Applied allocations are immutable history and are NEVER rewritten: only
        ``allocation_mode == 'prepaid_reservation'`` rows are considered, and
        the released amount goes straight back to the funding event's unapplied
        balance (conservation: live allocations + unapplied == event.amount).
        Affected payments are re-projected so the driver's expected-cash figure
        at the door does not still net out a released reservation.

        Idempotent (already-reversed rows are filtered out). Runs in the
        caller's transaction (no commit). Returns the count released.
        """
        leaving = {int(uid) for uid in (leaving_user_ids or [])}
        remaining = {int(uid) for uid in (remaining_user_ids or [])}
        if not leaving or not remaining:
            return 0

        candidates = (
            CashCollectionAllocation.query.options(
                joinedload(CashCollectionAllocation.cash_collection_event),
                joinedload(CashCollectionAllocation.payment),
            )
            .join(
                CashCollectionEvent,
                CashCollectionAllocation.cash_collection_event_id == CashCollectionEvent.id,
            )
            .filter(
                CashCollectionAllocation.allocation_mode == "prepaid_reservation",
                CashCollectionAllocation.reversed_at.is_(None),
                CashCollectionEvent.voided_at.is_(None),
                CashCollectionEvent.customer_id.in_(leaving | remaining),
            )
            .order_by(CashCollectionAllocation.id.asc())
            .with_for_update(of=CashCollectionAllocation)
            .all()
        )

        now = datetime.now(UTC)
        released_count = 0
        affected_payments: Dict[int, Payment] = {}
        for allocation in candidates:
            event = allocation.cash_collection_event
            payment = allocation.payment
            if event is None or payment is None:
                continue
            source_side = event.customer_id
            target_side = payment.user_id
            out_of_scope = (source_side in leaving and target_side in remaining) or (
                source_side in remaining and target_side in leaving
            )
            if not out_of_scope:
                continue
            amount = self._to_decimal(allocation.allocated_amount)
            event.unapplied_amount = self._to_decimal(event.unapplied_amount) + amount
            allocation.reversed_at = now
            allocation.reversal_reason = "Released: reservation out of scope after account unlink"
            metadata = dict(allocation.allocation_metadata or {})
            metadata["reservation_state"] = "released"
            metadata["reservation_released_at"] = now.isoformat()
            metadata["affects_payment_projection"] = False
            allocation.allocation_metadata = metadata
            released_count += 1
            affected_payments[payment.id] = payment

        if not released_count:
            return 0

        # Flush the reversals BEFORE re-projecting: the projection is recomputed
        # by a SQL SUM over live reservations, so the reversal stamps must be in
        # the database, not merely pending in the session.
        db.session.flush()
        for payment in affected_payments.values():
            self._sync_reserved_prepayment_projection(payment)
        db.session.flush()
        return released_count

    def release_pre_delivery_prepaid_settlement_for_order(
        self,
        order_id: int,
        *,
        actor_user_id: Optional[int] = None,
        reason: Optional[str] = None,
    ) -> Decimal:
        """Refund prepaid credit that was APPLIED at order creation (full
        coverage) when the order is cancelled/returned before delivery.

        The companion to :meth:`release_reserved_prepayment_for_order`: that one
        releases un-consumed *reservations*; this one reverses credit that was
        actually consumed (``settled_pre_delivery``) so a not-yet-delivered order
        could read as paid. It restores the source events' unapplied balance,
        rolls back ``amount_collected`` and re-projects the payment to unpaid.

        No-op for orders that were ever delivered — credit consumed at delivery
        is settled against received goods, and a return is handled by the return
        accounting, not by un-doing the creation-time settlement. Returns the
        refunded total. Caller controls the transaction (no commit).
        """
        payment = (
            Payment.query.options(
                joinedload(Payment.order).joinedload(Order.delivery),
                joinedload(Payment.cash_collection_allocations).joinedload(
                    CashCollectionAllocation.cash_collection_event
                ),
            )
            .filter_by(order_id=order_id)
            .first()
        )
        if not payment or payment.payment_method != PaymentMethod.CASH:
            return Decimal("0.00")

        # Only refund settlements for orders that were never delivered. A
        # delivered order's credit was rightfully consumed against goods.
        order = payment.order
        if order is not None:
            delivery = getattr(order, "delivery", None)
            if order.status == OrderStatus.DELIVERED or (delivery is not None and delivery.delivered_at is not None):
                return Decimal("0.00")

        now = datetime.now(UTC)
        release_reason = reason or "Pre-delivery prepaid settlement refunded on cancel/return"
        refunded_total = Decimal("0.00")

        for allocation in payment.cash_collection_allocations:
            if allocation.reversed_at:
                continue
            metadata = dict(allocation.allocation_metadata or {})
            if not metadata.get("settled_pre_delivery"):
                continue
            if not self._allocation_affects_payment_projection(allocation):
                continue

            amount = self._to_decimal(allocation.allocated_amount)
            event = allocation.cash_collection_event
            if event is not None:
                event.unapplied_amount = self._to_decimal(event.unapplied_amount) + amount
            payment.amount_collected = self._to_decimal(payment.amount_collected) - amount
            allocation.reversed_at = now
            allocation.reversed_by_user_id = actor_user_id
            allocation.reversal_reason = release_reason
            metadata["reservation_state"] = "released"
            metadata["reservation_released_at"] = now.isoformat()
            metadata["affects_payment_projection"] = False
            allocation.allocation_metadata = metadata
            refunded_total += amount

        if refunded_total > Decimal("0.00"):
            # Re-project: amount_collected dropped, so outstanding/status/is_paid
            # roll back to unpaid. The terminal-state cascade then cancels the
            # now-pending payment.
            self.sync_payment_projection(payment)

        return self._to_decimal(refunded_total)

    def credit_customer_for_dead_order_prepayment(
        self,
        payment: Payment,
        *,
        reason: str,
        actor_user_id: Optional[int] = None,
        commit: bool = False,
    ) -> Optional[CashCollectionEvent]:
        """Book a card/Click prepayment on a dead order as customer prepaid credit.

        Owner ruling 2026-08-24: "the payment that is done via click/card is
        non-returnable ... We can cancel the order itself, and in that case the
        payment will settle as prepaid customer balance."

        THE RAIL GATE IS THE PAYME CARVE-OUT. ``ONLINE_PAYABLE_METHOD_VALUES`` is
        {CLICK, CARD}; PAYME is excluded BY CONSTRUCTION, not by a hand-written
        exclusion that a future edit could drop. Payme keeps its protocol-mandated
        CancelTransaction reversal and never reaches here.

        NO ``order_id`` IS PASSED, and that is load-bearing, not an oversight.
        ``_validate_collection_context`` refuses a BACKFILL against a
        non-DELIVERED, non-CASH order — twice over for a cancelled card order.
        This is customer-level credit that applies to the NEXT order, exactly as
        ``ClickPaymentProviderService._credit_late_debit`` books it. It also
        keeps B4 structurally clear of the ``_sync_completed_prepayment_projection``
        landmine documented in ``open_receivable_clause``: a free-standing event
        with no order_id can never be mistaken for a reservation against this
        payment. The order linkage lives in ``proof_data``.

        THE AMOUNT IS NETTED, NOT GROSS. ``amount_collected`` on a card payment
        can already have been partly handed back (an edit-down posted the delta
        through ``_cascade_cash``) or partly funded by door cash (whose
        allocations are still booked on this payment). Crediting the gross figure
        books the same money twice. What we owe back is precisely the GATEWAY
        money still held.

        Returns the credit event, or ``None`` when there is nothing to credit.
        """
        from shared.enums import CashCollectionSource

        if payment is None:
            return None
        method_value = (
            payment.payment_method.value if hasattr(payment.payment_method, "value") else str(payment.payment_method)
        )
        if method_value not in ONLINE_PAYABLE_METHOD_VALUES:
            return None
        # The only two states in which a card/Click row is holding real money:
        # COMPLETED (paid in full) and PARTIALLY_PAID (paid, then repriced upward).
        if payment.status not in {PaymentStatus.COMPLETED, PaymentStatus.PARTIALLY_PAID}:
            return None

        gross = get_payment_projection(payment)["amount_collected"]

        # (1) Money on this payment that came from CASH, not the gateway — the
        #     door-collected slice of an order that was repriced upward and
        #     settled in place. Same predicate the financial report's
        #     `cash_on_payment` subquery uses: live allocations, excluding
        #     `prepaid_reservation` (reservations never bump `amount_collected`).
        #
        #     🔴 IT IS NOT MERELY SUBTRACTED — IT IS HANDED BACK. Subtracting it
        #     alone is arithmetically right and moral nonsense: the customer who
        #     paid 100k by card and 30k in banknotes would get 100k of credit and
        #     we would simply keep the 30k, with no refund route of any kind now
        #     that the gateway rail is closed. "We keep your cash" is precisely
        #     what the owner's rule exists to avoid. So each allocation is
        #     reversed back into its OWN event's unapplied pool, and only the
        #     gateway remainder becomes a new credit event. The customer ends up
        #     whole either way; the money simply returns by the road it came in
        #     on.
        cash_allocations = (
            CashCollectionAllocation.query.join(
                CashCollectionEvent,
                CashCollectionAllocation.cash_collection_event_id == CashCollectionEvent.id,
            )
            .filter(
                CashCollectionAllocation.payment_id == payment.id,
                CashCollectionAllocation.reversed_at.is_(None),
                CashCollectionAllocation.allocation_mode != "prepaid_reservation",
                CashCollectionEvent.voided_at.is_(None),
            )
            .all()
        )
        cash_funded = sum(
            (self._to_decimal(allocation.allocated_amount) for allocation in cash_allocations),
            Decimal("0.00"),
        )

        # (2) Gateway money already handed back as credit by the order-edit
        #     cascade, discovered through the ONE re-booking predicate rather
        #     than a second hand-written marker test.
        already_credited = Decimal("0.00")
        if payment.order_id is not None:
            already_credited = self._to_decimal(
                db.session.query(func.coalesce(func.sum(CashCollectionEvent.amount), 0))
                .filter(
                    CashCollectionEvent.order_id == payment.order_id,
                    CashCollectionEvent.voided_at.is_(None),
                    customer_credit_rebook_event_clause(),
                )
                .scalar()
                or 0
            )

        creditable = gross - cash_funded - already_credited

        # Hand the door cash back. Modelled on the shipped Case C precedent
        # (`ClickPaymentProviderService._restore_click_rail_after_offline_settlement`):
        # `reverse_allocation_to_payment`, never `reverse_collection_event`,
        # because it touches neither `event.amount` nor `driver_cash_session_id`
        # — the driver really did hand those notes over and still owes the office
        # exactly the same total.
        paid_at_before = payment.paid_at
        for allocation in cash_allocations:
            self.reverse_allocation_to_payment(
                allocation.id,
                reversed_by_user_id=actor_user_id,
                reason=(
                    f"Order {payment.order.order_number if payment.order else ''} died; "
                    "door cash re-booked as customer prepaid credit"
                ),
                commit=False,
            )

        # 🔴 RE-ASSERT THE DEAD-ORDER TERMINAL PROJECTION. A DEAD ORDER OWES
        # NOTHING, and this runs on EVERY dead-order credit — deliberately NOT
        # nested inside the reversal above, which was the bug this replaced.
        #
        # Two ways a positive `outstanding_amount` survives to here:
        #
        #  1. the reversal just removed the door-cash slice —
        #     `reverse_allocation_to_payment` calls `sync_payment_projection`,
        #     which re-derives status from `amount − amount_collected`; and
        #  2. the payment arrived PARTIALLY_PAID in the first place, which this
        #     method deliberately accepts: an order repriced UPWARD at the door
        #     (prod order 961) that is then cancelled. `_recompute_totals` left
        #     `amount` at the new higher total, and
        #     `_sync_payment_status_for_terminal_order_state` will not touch it
        #     because that guard only handles {PENDING, PROCESSING}.
        #
        # Either way the row would otherwise read as a PARTIALLY_PAID payment
        # with a positive outstanding, `is_paid = False`, on a CANCELLED order —
        # a PHANTOM RECEIVABLE, kept out of the allocators only by the
        # `Order.status == DELIVERED` conjunct every `open_receivable_clause()`
        # call site has to remember (see this module's note at the top of
        # `open_receivable_clause`'s consumers). Do not leave one behind.
        #
        # Reducing `amount` to what is actually still held is the same move
        # `_sync_payment_status_for_terminal_order_state` already makes on a
        # terminal order ("reduce amount to what was collected so outstanding
        # stays 0 through any later re-projection"), and it leaves the row a
        # truthful record of what the GATEWAY took. A no-op for an already
        # COMPLETED, fully-collected payment; PENDING never reaches this far.
        collected_now = self._to_decimal(payment.amount_collected)
        payment.amount = collected_now
        payment.outstanding_amount = Decimal("0.00")
        payment.status = PaymentStatus.COMPLETED
        payment.paid_at = paid_at_before or payment.paid_at
        if payment.order is not None:
            payment.order.is_paid = True
            payment.order.paid_at = payment.order.paid_at or payment.paid_at
        db.session.flush()

        if creditable <= Decimal("0.00"):
            return None

        order_number = payment.order.order_number if payment.order else ""
        return self.post_collection(
            customer_id=payment.user_id,
            amount=creditable,
            source=CashCollectionSource.BACKFILL,
            recorded_by_user_id=actor_user_id,
            notes=(
                f"Order {order_number} cancelled after a {method_value} payment. "
                f"The card/Click payment is never reversed (fiscal receipt cannot be "
                f"undone); {creditable} credited to the customer's prepaid balance. "
                f"Reason: {reason}"
            ),
            proof_data={
                "flow": "order_cancel_prepaid_credit",
                "payment_id": payment.id,
                "order_id": payment.order_id,
                "payment_method": method_value,
                "provider_transaction_id": payment.provider_transaction_id,
                "reason": reason,
            },
            # The double-credit defence: a retried cancel, a re-dispatched Celery
            # task and a second admin click all collapse to one event.
            idempotency_key=f"order-cancel-credit:{payment.id}",
            # 🔴 THE CORPORATE MIRROR IS SUPPRESSED FOR THIS FLOW (owner ruling).
            # A grocery customer is credited exactly like everyone else, but
            # `post_collection`'s `settle_order_collection` mirror must not fire:
            # for an AMOUNT-mode contract it posts a COLLECT that pays down
            # contract debt, and a cancelled order was never CHARGEd against that
            # contract (the CHARGE is posted at DELIVERED). Crediting them and
            # firing the mirror would double-count; refusing to credit them at
            # all would be worse still — before this rule they at least got a
            # gateway reversal, and that route no longer exists.
            mirror_to_corporate_contract=False,
            commit=commit,
        )

    def reverse_allocation_to_payment(
        self,
        allocation_id: int,
        *,
        reversed_by_user_id: int,
        reason: str,
        commit: bool = True,
    ) -> CashCollectionAllocation:
        """Reverse ONE allocation, turning its collected cash into customer
        prepaid credit. Unlike ``reverse_collection_event`` this touches only
        this allocation — never ``event.amount`` or ``driver_cash_session_id``
        — so it cannot disturb other orders paid by the same event or driver
        cash session totals.
        """
        allocation = (
            CashCollectionAllocation.query.options(
                joinedload(CashCollectionAllocation.cash_collection_event), joinedload(CashCollectionAllocation.payment)
            )
            .with_for_update(of=CashCollectionAllocation)
            .get(allocation_id)
        )
        if not allocation or allocation.reversed_at:
            raise ValidationError("Allocation not found or already reversed")
        event = allocation.cash_collection_event
        if event.voided_at:
            raise ValidationError("Parent cash collection event is voided")

        amount = self._to_decimal(allocation.allocated_amount)
        now = datetime.now(UTC)
        affects_projection = self._allocation_affects_payment_projection(allocation)

        allocation.reversed_at = now
        allocation.reversed_by_user_id = reversed_by_user_id
        allocation.reversal_reason = reason
        metadata = dict(allocation.allocation_metadata or {})
        metadata["affects_payment_projection"] = False
        allocation.allocation_metadata = metadata

        event.unapplied_amount = self._to_decimal(event.unapplied_amount) + amount  # -> customer prepaid credit

        payment = allocation.payment
        if affects_projection and payment is not None:
            payment.amount_collected = self._to_decimal(payment.amount_collected) - amount
            self.sync_payment_projection(payment)

        if commit:
            db.session.commit()
        else:
            db.session.flush()
        return allocation

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
        cluster_user_ids: Optional[Iterable[int]] = None,
    ) -> Decimal:
        """Sweeps the customer's CURRENT cluster's non-delivered CASH payments
        (ring 3, spec 5.2/5.6 — the sweep is forward-looking state, never
        frozen), reserving unapplied prepayment against them oldest-first.
        Idempotent. Best-effort: skips locked rows (Postgres only) so concurrent
        order creation doesn't block the sweep; the new order's own creation path
        retriggers reservation.

        ``cluster_user_ids`` is ring 3's candidate universe (spec 5.2): the
        pending CASH payments this sweep may reserve against. It MUST always be
        resolved from CURRENT topology, never from an event's frozen scope — see
        the carve-out comment at the call site in ``post_collection`` (spec 5.6).
        Omitted (the default) it stays the single account, which is what an
        unlinked customer's cluster resolves to anyway.

        Gate discipline: the loop body spends the PAYMENT OWNER's cluster wallet
        (``reserve_customer_prepaid_credit_for_payment`` anchors on
        ``payment.user_id``), so both the early return and the per-iteration
        check are evaluated on that same pool — never on the poster's balance.
        Gating on the poster stranded a sibling's pending order whose own credit
        was sitting available, because the walk broke out as soon as the poster's
        share hit zero.
        """
        owner_ids = [int(uid) for uid in cluster_user_ids] if cluster_user_ids else [customer_id]

        if self._unapplied_credit_total(self._credit_pool_user_ids(owner_ids)) <= Decimal("0.00"):
            return Decimal("0.00")

        query = (
            Payment.query.join(Order, Payment.order_id == Order.id)
            .options(contains_eager(Payment.order))
            .filter(
                Payment.user_id.in_(owner_ids),
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
        #
        # This is the ONE payments lock not ordered by Payment.id, and it is
        # exempt precisely because SKIP LOCKED never waits: a transaction that
        # cannot block cannot be an edge in a deadlock cycle. Do NOT drop
        # skip_locked here without also moving this onto _lock_payments_by_ids.
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
            # `continue`, not `break`: the universe may span several people (a
            # caller-supplied universe need not be one cluster), and one
            # exhausted wallet says nothing about the next payment's owner.
            if self.get_customer_prepaid_balance(payment.user_id) <= Decimal("0.00"):
                continue
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
                unpaid_after_delivery_clause(),
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

    def _serialize_open_cod_debtor_row(self, row, *, cod_restricted: bool) -> Dict[str, Any]:
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
            # Per-account slice of the debt; the restriction flag is
            # cluster-aware and exemption-aware (spec 5.5), so the two can
            # legitimately disagree for a linked or exempt customer.
            "active_cod_debt_count": active_count,
            "total_outstanding_amount": float(row.total_outstanding_amount or 0),
            "cod_restricted": cod_restricted,
        }

    def _serialize_open_cod_debtor_rows(self, rows) -> List[Dict[str, Any]]:
        """Serialize debtor rows with one batched cluster-flag lookup."""
        flags = self.get_cod_restricted_flags([int(row.user_id) for row in rows])
        return [
            self._serialize_open_cod_debtor_row(row, cod_restricted=flags.get(int(row.user_id), False)) for row in rows
        ]

    def get_place_cod_statement(self, group_id: int) -> Dict[str, Any]:
        """Unified open COD debt at one place group (any member's orders).

        Debt selection mirrors ring 1 of the scope engine: CASH payments with
        outstanding > 0 on DELIVERED orders whose ``delivery_address_id`` is a
        member address. Read-only; never ``join(User)`` from payments/orders
        (multi-FK gotcha) — owner names are resolved by a second id-filtered
        query.
        """
        from business_app.models.customer_link import AddressGroup

        group = AddressGroup.query.get(group_id)
        if group is None:
            raise NotFoundError("Place group not found")

        member_address_ids = [
            r[0] for r in db.session.query(UserAddress.id).filter(UserAddress.address_group_id == group_id).all()
        ]
        payments = []
        if member_address_ids:
            payments = (
                Payment.query.join(Order, Payment.order_id == Order.id)
                .options(joinedload(Payment.order))
                .filter(
                    Order.delivery_address_id.in_(member_address_ids),
                    open_receivable_clause(),
                    Order.status == OrderStatus.DELIVERED,
                )
                .order_by(Order.created_at.asc(), Payment.id.asc())
                .all()
            )

        owner_ids = sorted({p.user_id for p in payments})
        owners = {u.id: u for u in User.query.filter(User.id.in_(owner_ids)).all()} if owner_ids else {}

        items = []
        total = Decimal("0.00")
        for payment in payments:
            outstanding = self._to_decimal(payment.outstanding_amount)
            total += outstanding
            owner = owners.get(payment.user_id)
            items.append(
                {
                    "payment_id": payment.id,
                    "order_id": payment.order_id,
                    "order_number": payment.order.order_number if payment.order else None,
                    "owner_user_id": payment.user_id,
                    # Names only — a place spans different people, so no phone
                    # or other member PII leaves this payload (spec 7).
                    "owner_name": owner.full_name if owner else None,
                    "outstanding_amount": float(outstanding),
                    "created_at": payment.created_at.isoformat() if payment.created_at else None,
                }
            )

        # Distinct address OWNERS, not addresses: one person with two grouped
        # addresses is one member.
        member_count = (
            db.session.query(func.count(func.distinct(UserAddress.user_id)))
            .filter(UserAddress.address_group_id == group_id)
            .scalar()
        ) or 0

        return {
            "place_group_id": group_id,
            "label": group.label,
            "member_count": int(member_count),
            "total_outstanding_amount": float(total),
            "active_cod_debt_count": len(items),
            "items": items,
        }

    def get_place_cod_debtor_rows(self, limit: int = 200) -> List[Dict[str, Any]]:
        """One row per place group that currently has open delivered COD debt.

        Sorted by outstanding descending. A place with no open debt is absent.
        """
        from business_app.models.customer_link import AddressGroup

        rows = (
            db.session.query(
                UserAddress.address_group_id.label("place_group_id"),
                func.count(Payment.id).label("active_cod_debt_count"),
                func.coalesce(func.sum(Payment.outstanding_amount), Decimal("0.00")).label("total_outstanding_amount"),
                func.count(func.distinct(Payment.user_id)).label("debtor_member_count"),
            )
            .select_from(Payment)
            .join(Order, Order.id == Payment.order_id)
            # addresses has a single FK to users, and this join is on the order's
            # delivery address — no User join is involved.
            .join(UserAddress, UserAddress.id == Order.delivery_address_id)
            .filter(
                UserAddress.address_group_id.isnot(None),
                open_receivable_clause(),
                Order.status == OrderStatus.DELIVERED,
            )
            .group_by(UserAddress.address_group_id)
            .order_by(func.sum(Payment.outstanding_amount).desc())
            .limit(max(1, min(int(limit or 200), 1000)))
            .all()
        )
        if not rows:
            return []

        group_ids = [r.place_group_id for r in rows]
        labels = {g.id: g.label for g in AddressGroup.query.filter(AddressGroup.id.in_(group_ids)).all()}
        # `member_count` MUST be the number of distinct address OWNERS in the
        # group (same definition as get_place_cod_statement) — NOT the number of
        # indebted payers, or a 3-person office with one debtor would render
        # "(1 members)" on the list and "(3)" on the statement.
        member_counts = dict(
            db.session.query(UserAddress.address_group_id, func.count(func.distinct(UserAddress.user_id)))
            .filter(UserAddress.address_group_id.in_(group_ids))
            .group_by(UserAddress.address_group_id)
            .all()
        )
        return [
            {
                "row_type": "place",
                "place_group_id": int(r.place_group_id),
                "label": labels.get(r.place_group_id),
                "member_count": int(member_counts.get(r.place_group_id, 0)),
                "debtor_member_count": int(r.debtor_member_count or 0),
                "active_cod_debt_count": int(r.active_cod_debt_count or 0),
                "total_outstanding_amount": float(r.total_outstanding_amount or 0),
            }
            for r in rows
        ]

    def _collapse_debtor_rows_by_cluster(self, rows) -> List[Dict[str, Any]]:
        """Collapse per-account debtor rows into one row per linked person.

        Unlinked users pass through unchanged (singleton cluster) apart from the
        three additive keys, so their row is byte-identical to the pre-Phase-2
        one. The surviving row keeps the identity of the member with the largest
        outstanding — that is the account a collector is most likely to
        recognise — and sums counts/amounts across the cluster.

        ``cod_restricted`` comes from :meth:`_serialize_open_cod_debtor_rows`,
        i.e. the batched cluster- and exemption-aware
        :meth:`get_cod_restricted_flags` (2b Task 9). It is deliberately NOT
        recomputed from the collapsed counts.
        """
        serialized = self._serialize_open_cod_debtor_rows(rows)
        if not serialized:
            return []

        # One batched canonical lookup rather than a per-row
        # CustomerLinkService.resolve_canonical() — same semantics (the user's
        # canonical_customer_id, None when unlinked), same anti-N+1 reasoning as
        # get_cod_restricted_flags. Plain FK select on users, never join(User)
        # from payments/orders.
        row_ids = [row["id"] for row in serialized]
        canonical_by_user: Dict[int, Optional[int]] = {
            int(r[0]): r[1]
            for r in db.session.query(User.id, User.canonical_customer_id).filter(User.id.in_(row_ids)).all()
        }

        by_key: Dict[Tuple[str, int], Dict[str, Any]] = {}
        order_of_keys: List[Tuple[str, int]] = []
        for row in serialized:
            canonical = canonical_by_user.get(row["id"])
            # The "c"/"u" tag keeps the canonical and user id spaces from colliding.
            key = ("c", int(canonical)) if canonical is not None else ("u", int(row["id"]))
            existing = by_key.get(key)
            if existing is None:
                row = dict(row)
                row["row_type"] = "person"
                row["member_user_ids"] = [row["id"]]
                by_key[key] = row
                order_of_keys.append(key)
                continue
            if row["total_outstanding_amount"] > existing["total_outstanding_amount"]:
                for field_name in ("id", "first_name", "last_name", "phone", "role", "user_type"):
                    existing[field_name] = row[field_name]
            existing["active_cod_debt_count"] += row["active_cod_debt_count"]
            existing["total_outstanding_amount"] += row["total_outstanding_amount"]
            existing["member_user_ids"].append(row["id"])
            existing["cod_restricted"] = existing["cod_restricted"] or row["cod_restricted"]

        collapsed = [by_key[k] for k in order_of_keys]
        for row in collapsed:
            row["member_user_ids"] = sorted(row["member_user_ids"])
            row["cluster_member_count"] = len(row["member_user_ids"])
        collapsed.sort(key=lambda r: r["total_outstanding_amount"], reverse=True)
        return collapsed

    def list_users_with_open_cod_debts(self, *, limit: int = 200) -> List[Dict[str, Any]]:
        """Debtors with at least one open delivered COD debt, one row per person.

        Cluster-collapsed (spec 5.3: one real person, one credit line); no place
        rows — the admin surface pairs this list with its own place view.
        """
        safe_limit = max(1, min(int(limit or 200), 1000))
        rows = self._open_cod_debtors_query().limit(1000).all()
        return self._collapse_debtor_rows_by_cluster(rows)[:safe_limit]

    def paginate_users_with_open_cod_debts(self, *, page: int = 1, per_page: int = 10) -> Dict[str, Any]:
        """Page through COD debtors: place rows first, then cluster-collapsed
        person rows (staff bot list).

        Combined in memory — the debtor universe is small (bounded at 1000
        accounts) and cluster collapse cannot be expressed as SQL pagination.
        Note the two row families overlap by design: a debt delivered to a
        grouped address is counted both in its place row and in its orderer's
        person row, because a collector may settle it either way.
        """
        safe_page = max(1, int(page or 1))
        safe_per_page = max(1, min(int(per_page or 10), 100))

        person_rows = self._collapse_debtor_rows_by_cluster(self._open_cod_debtors_query().limit(1000).all())
        combined = self.get_place_cod_debtor_rows() + person_rows

        total = len(combined)
        pages = (total + safe_per_page - 1) // safe_per_page
        start = (safe_page - 1) * safe_per_page

        return {
            "items": combined[start : start + safe_per_page],
            "pagination": {
                "page": safe_page,
                "per_page": safe_per_page,
                "total": total,
                "pages": pages,
            },
        }

    def validate_customer_can_use_cod(
        self, customer_id: int, delivery_address_id: Optional[int] = None
    ) -> Dict[str, Any]:
        context = self.get_cod_restriction_context(customer_id, delivery_address_id=delivery_address_id)
        if context["cod_restricted"]:
            raise ValidationError(
                "Customer has reached the maximum number of active cash on delivery debts.",
                error_code="COD_DEBT_LIMIT_REACHED",
            )
        return context

    def get_customer_cod_statement(self, customer_id: int) -> Dict[str, Any]:
        """Full per-payment statement for a customer — ALL RAILS.

        Deliberately the one receivable query with no `outstanding > 0` conjunct:
        it is a statement, not a debtor list, so settled rows stay for context.
        Since 2026-08-08 it is also the one with no `payment_method` conjunct.
        An electronic order that still owes money is a receivable exactly like a
        COD one, and the owner's ruling is that the statement shows both rails in
        full rather than an asymmetric "cash in full, card only when owing".

        The driver's at-door screen renders only rows with
        `outstanding_amount > 0` (`staff_bot/handlers/delivery/cash_collection.py`
        skips the rest and slices to five), so the longer list does not reach
        that surface. See plan 2026-08-08-open-receivable-ssot.
        """
        customer = User.query.get(customer_id)
        if not customer:
            raise NotFoundError("Customer not found")

        payments = (
            Payment.query.join(Order, Payment.order_id == Order.id)
            .options(joinedload(Payment.order))
            .filter(
                Payment.user_id == customer_id,
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
            # Same helper the driver screen, the order modal and every allocator
            # use — this statement used to hand-roll the subtraction, which is
            # how the surfaces drifted apart in the first place.
            reserved_amount = reserved_prepayment_amount(payment)
            net_outstanding = net_open_receivable_amount(payment)
            # Cancelled/returned orders aren't collectible debt; keep them in
            # `items` for display but out of the totals.
            if payment.order is not None and payment.order.status not in self._TERMINAL_ORDER_STATUSES:
                total_outstanding += outstanding_amount
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
                    # The statement carries both rails now, so the consumer must
                    # be able to tell them apart.
                    "payment_method": (
                        payment.payment_method.value
                        if hasattr(payment.payment_method, "value")
                        else payment.payment_method
                    ),
                    # 🔴 THE ONLY FLAG A COLLECT SURFACE MAY FILTER ON.
                    # `outstanding_amount > 0` is NOT sufficient: this statement
                    # lists every rail in full (owner ruling 2026-08-08), so it
                    # includes settled rows and live gateway payments that
                    # `_validate_collection_context` will refuse. The admin
                    # Record Collection dropdown and the driver's at-door
                    # statement both key on this flag, so what a human is offered
                    # and what the endpoint accepts are one decision — the
                    # show-vs-settle rule this codebase keeps relearning.
                    "is_collectible_target": is_ledger_receivable(payment),
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

        # --- cluster + place context (Phase 2c read surfaces) -----------------
        # Everything below is DERIVED at read time; no cluster/place figure is
        # ever stored (plan 2c global constraint 1).
        from business_app.services.customer_link_service import CustomerLinkService

        cluster_ids = CustomerLinkService().get_cluster_user_ids(customer_id)
        cluster_delivered_outstanding = db.session.query(
            func.coalesce(func.sum(Payment.outstanding_amount), Decimal("0.00"))
        ).join(Order, Payment.order_id == Order.id).filter(
            Payment.user_id.in_(cluster_ids),
            open_receivable_clause(),
            Order.status == OrderStatus.DELIVERED,
        ).scalar() or Decimal(
            "0.00"
        )

        from business_app.models.customer_link import AddressGroup

        places: List[Dict[str, Any]] = []
        grouped_addresses = UserAddress.query.filter(
            UserAddress.user_id.in_(cluster_ids),
            UserAddress.address_group_id.isnot(None),
        ).all()
        # `UserAddress` has NO `address_group` relationship (models/user.py
        # declares only `user` and `orders`) — labels MUST come from an explicit
        # bulk lookup, never `addr.address_group`.
        group_labels = {}
        if grouped_addresses:
            group_labels = {
                g.id: g.label
                for g in AddressGroup.query.filter(
                    AddressGroup.id.in_({a.address_group_id for a in grouped_addresses})
                ).all()
            }
        seen_groups = set()
        for addr in grouped_addresses:
            if addr.address_group_id in seen_groups:
                continue
            seen_groups.add(addr.address_group_id)
            places.append(
                {
                    "address_id": addr.id,
                    "place_group_id": addr.address_group_id,
                    "label": group_labels.get(addr.address_group_id),
                    "place_open_cod_debt_total": float(self.get_place_open_cod_debt_total(addr.address_group_id)),
                    "place_active_cod_debt_count": self.get_place_active_cod_debt_count(addr.id),
                }
            )

        return {
            "customer_id": customer_id,
            "first_name": customer.first_name,
            "last_name": customer.last_name,
            "phone": customer.phone,
            "entity_subtype": (
                customer.entity_subtype.value
                if customer.entity_subtype is not None and hasattr(customer.entity_subtype, "value")
                else customer.entity_subtype
            ),
            # Cluster-wide (spec 5.5) so it agrees with `cod_restricted` and with
            # what the cap actually enforces. `items` / `total_outstanding_amount`
            # below stay per-account — a linked sibling can therefore report a
            # non-zero count with an empty item list, which is what the staff-bot
            # COD search (`only_with_open_cod`) needs in order to surface the
            # person whose cluster owes money.
            "active_cod_debt_count": self.get_cluster_active_cod_debt_count(customer_id),
            # The per-account half of that asymmetry, so a surface can *state*
            # it ("cluster owes 2, none on this account") instead of inferring
            # it from an empty `items` list. Equals `active_cod_debt_count` for
            # an unlinked customer.
            "account_active_cod_debt_count": self.get_active_cod_debt_count(customer_id),
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
            # Cluster/place context. For an unlinked + ungrouped customer these
            # degrade to: cluster_member_count 1, cluster total == this
            # account's delivered outstanding, places [].
            "cluster_member_count": len(cluster_ids),
            "cluster_delivered_outstanding_amount": float(cluster_delivered_outstanding),
            "places": places,
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
        cash collection event for the customer's whole CLUSTER (spec 5.3 — one
        person, one wallet) alongside the allocations that consumed (or are
        reserving) each event, plus aggregate totals. Each event carries its own
        ``customer_id`` and the payload carries ``cluster_member_ids`` so the UI
        can attribute a row to the account that actually collected it. For an
        unlinked customer the cluster is themselves and the payload is unchanged
        apart from the two new keys.

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

        # The ledger must describe the SAME pool as the balance it is shown next
        # to, so it goes through the one credit-pool resolver rather than reading
        # the raw cluster: ``available_prepayment_balance`` below is
        # ``get_customer_prepaid_balance``, which is grocery-guarded. Reading the
        # raw cluster here would list a grocery member's contract-mirrored events
        # (and sum them into the lifetime totals) beside a balance that
        # deliberately excludes them.
        cluster_ids = self._credit_pool_for_anchor(customer_id)

        query = CashCollectionEvent.query.options(
            joinedload(CashCollectionEvent.allocations)
            .joinedload(CashCollectionAllocation.payment)
            .joinedload(Payment.order),
            joinedload(CashCollectionEvent.order),
        ).filter(CashCollectionEvent.customer_id.in_(cluster_ids))

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
                CashCollectionEvent.customer_id.in_(cluster_ids),
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
                    # Which cluster member actually collected this cash. Only
                    # meaningful once the ledger spans a cluster, and the only
                    # per-row attribution the UI has.
                    "customer_id": event.customer_id,
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
            "cluster_member_ids": cluster_ids,
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

        Credit is cluster-fungible (spec 5.3), so linked accounts collapse into
        ONE row per person carrying the summed balance and a ``member_user_ids``
        list. Unlinked rows pass through unchanged (with a single-element
        ``member_user_ids``). Note ``limit`` is applied by the SQL query BEFORE
        the merge, so a caller may receive fewer rows than requested when linked
        accounts collapse — accepted for this phase.
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

        # Cluster-fungible credit: collapse linked accounts into one row per
        # person (spec 5.3). Unlinked rows pass through unchanged.
        user_ids = [item["id"] for item in items]
        canon = dict(
            db.session.query(User.id, User.canonical_customer_id)
            .filter(User.id.in_(user_ids), User.canonical_customer_id.isnot(None))
            .all()
        )
        merged: Dict[Any, Dict[str, Any]] = {}
        ordered_keys: List[Any] = []
        for item in items:
            key = ("c", canon[item["id"]]) if item["id"] in canon else ("u", item["id"])
            if key not in merged:
                item = dict(item)
                item["member_user_ids"] = [item["id"]]
                # Accumulate in Decimal; convert once at the end (money rule:
                # Decimal inside services, float only at the boundary).
                item["available_prepayment_balance"] = self._to_decimal(item["available_prepayment_balance"])
                merged[key] = item
                ordered_keys.append(key)
            else:
                row = merged[key]
                row["available_prepayment_balance"] += self._to_decimal(item["available_prepayment_balance"])
                row["member_user_ids"].append(item["id"])
                if (item["last_collection_at"] or "") > (row["last_collection_at"] or ""):
                    row["last_collection_at"] = item["last_collection_at"]
        # The SQL ORDER BY ran per-ACCOUNT, so merged cluster totals no longer
        # respect the descending-balance ordering the UI relies on — re-sort.
        collapsed = [merged[k] for k in ordered_keys]
        for row in collapsed:
            row["available_prepayment_balance"] = float(row["available_prepayment_balance"])
        return sorted(
            collapsed,
            key=lambda r: (-r["available_prepayment_balance"], r["id"]),
        )

    def get_order_payment_timeline(self, order_id: int, viewer_user_id: Optional[int] = None) -> Dict[str, Any]:
        """Payment timeline for one order.

        ``viewer_user_id`` selects the rendering arm (spec §7/§9):

        * ``None`` (default — every admin/staff caller) keeps today's full
          rendering and ADDS the scope + dual attribution stamps so a reviewer
          can trace a cross-customer settlement.
        * set (customer-facing callers) sanitizes allocations funded by an
          event owned OUTSIDE the viewer's cluster: since the money engine is
          scope-aware, another person's collection can settle this order, and
          that event's free-text notes and full amount are not the viewer's to
          see. Cluster-sibling events are the same person's own wallet and
          render exactly as today.
        """
        order = Order.query.options(
            joinedload(Order.payment),
            joinedload(Order.user),
        ).get(order_id)
        if not order:
            raise NotFoundError("Order not found")

        customer = order.user
        customer_identity = {
            "customer_id": order.user_id,
            "customer_name": customer.full_name if customer else None,
            "customer_phone": customer.phone if customer else None,
        }

        payment = order.payment
        if not payment:
            return {
                "order_id": order_id,
                "order_number": order.order_number,
                **customer_identity,
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
        viewer_cluster = None
        if viewer_user_id is not None:
            from business_app.services.customer_link_service import CustomerLinkService

            viewer_cluster = set(CustomerLinkService().get_cluster_user_ids(int(viewer_user_id)))

        group_label_cache: Dict[Any, Optional[str]] = {}

        def _group_label(group_id):
            if group_id is None:
                return None
            if group_id not in group_label_cache:
                from business_app.models.customer_link import AddressGroup

                group = AddressGroup.query.get(group_id)
                group_label_cache[group_id] = group.label if group else None
            return group_label_cache[group_id]

        for allocation in allocations:
            event = allocation.cash_collection_event
            entry = {
                "type": "cash_collection_allocation",
                "timestamp": allocation.allocated_at.isoformat() if allocation.allocated_at else None,
                "allocated_amount": float(allocation.allocated_amount or 0),
                "allocation_mode": allocation.allocation_mode,
                "reversed_at": allocation.reversed_at.isoformat() if allocation.reversed_at else None,
            }
            out_of_cluster = (
                viewer_cluster is not None and event is not None and event.customer_id not in viewer_cluster
            )
            if out_of_cluster:
                # Spec §7: neutral rendering — "settled by workplace collection".
                # No source event internals, no free-text notes, no full
                # collection amount, regardless of scope_type (also covers the
                # ex-member / frozen-snapshot correction case).
                entry.update(
                    {
                        "settled_by": "workplace_collection",
                        "collection_event_id": None,
                        "collection_amount": None,
                        "collection_source": None,
                        "delivery_id": None,
                        "notes": None,
                    }
                )
                timeline.append(entry)
                continue
            entry.update(
                {
                    "collection_event_id": allocation.cash_collection_event_id,
                    "collection_amount": float(event.amount or 0) if event else None,
                    "collection_source": (
                        event.source.value
                        if event and hasattr(event.source, "value")
                        else getattr(event, "source", None)
                    ),
                    "delivery_id": event.delivery_id if event else None,
                    "notes": event.notes if event else None,
                }
            )
            if viewer_cluster is None:
                # Admin/staff arm (spec §9): scope + dual attribution stamps.
                scope_type = (getattr(event, "scope_type", None) or "personal") if event else "personal"
                snapshot = (getattr(event, "scope_snapshot", None) or {}) if event else {}
                if not isinstance(snapshot, dict):  # pragma: no cover - defensive
                    snapshot = {}
                group_id = snapshot.get("group_id") if scope_type == "place" else None
                entry.update(
                    {
                        "scope_type": scope_type,
                        "scope_group_id": group_id,
                        "scope_group_label": _group_label(group_id),
                        "source_customer_id": getattr(allocation, "source_customer_id", None),
                        "beneficiary_user_id": getattr(allocation, "beneficiary_user_id", None),
                    }
                )
            timeline.append(entry)

        return {
            "order_id": order_id,
            "order_number": order.order_number,
            "payment_id": payment.id,
            **customer_identity,
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
        collected_by: Optional[int] = None,
    ) -> Payment:
        # Don't re-project a cancelled payment whose ORDER is terminal (keyed on
        # order status so an offline-settled cancel on a DELIVERED order still projects).
        order = payment.order
        if (
            payment.status == PaymentStatus.CANCELLED
            and order is not None
            and order.status in self._TERMINAL_ORDER_STATUSES
        ):
            payment.outstanding_amount = max(
                Decimal("0.00"),
                self._to_decimal(payment.amount) - self._to_decimal(payment.amount_collected),
            )
            return payment

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

        # ARCH-006, split into its two halves (plan 2026-08-08-open-receivable-ssot).
        #
        # STAMPING follows the MONEY: whoever handed over the cash that completed
        # this payment is recorded, on ANY rail. An electronic receivable settled
        # with physical cash — an order edited upward at the door — used to leave
        # `collected_by` NULL because the whole branch was CASH-gated, silently
        # losing the audit trail for exactly the case where a driver is holding
        # someone's banknotes.
        #
        # ASSERTING follows the RAIL: ck_payments_cash_completed_requires_collector
        # exempts non-cash rows by its first disjunct (`payment_method <> 'cash'`),
        # so enforcing the invariant on them would be wrong — see
        # test_non_cash_completing_needs_no_collector, which pins that a completed
        # CARD/CLICK payment needs no collector.
        if payment.status == PaymentStatus.COMPLETED:
            if not payment.collected_by and collected_by is not None:
                payment.collected_by = collected_by
            if payment.payment_method == PaymentMethod.CASH:
                assert_cash_payment_collector(payment, payment.status)

        if payment.order:
            order = payment.order
            became_fully_paid = (payment.status == PaymentStatus.COMPLETED) and not order.is_paid
            order.is_paid = payment.status == PaymentStatus.COMPLETED
            order.paid_at = payment.paid_at if order.is_paid else None

            if became_fully_paid:
                # The order just became fully paid (e.g. COD cash collected after
                # delivery). Award purchase AquaCoins if it is also delivered —
                # the guard self-checks (delivered AND paid) and is idempotent,
                # so this is a no-op for not-yet-delivered or already-awarded orders.
                from business_app.services.order_service import OrderService

                OrderService().maybe_award_purchase_points(order, commit=False)

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
        delivery_address_id: Optional[int] = None,
        replay_scope: Optional["AllocationScope"] = None,
        mirror_to_corporate_contract: bool = True,
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

        # Resolve the allocation scope ONCE and freeze it on the event.
        # A correction replay (adjust_event_amount) passes the original event's
        # frozen scope via replay_scope — never current topology (spec 5.6).
        # An explicit delivery_address_id seeds the scope address for order-less
        # standalone collections; order/delivery context overrides it when present.
        scope_address_id: Optional[int] = delivery_address_id
        if order_id:
            _scope_order = Order.query.get(order_id)
            if _scope_order is not None:
                scope_address_id = _scope_order.delivery_address_id
        elif delivery_id:
            _scope_delivery = Delivery.query.options(joinedload(Delivery.order)).get(delivery_id)
            if _scope_delivery is not None and _scope_delivery.order is not None:
                scope_address_id = _scope_delivery.order.delivery_address_id
        scope = replay_scope or self.resolve_allocation_scope(
            customer_id,
            delivery_address_id=scope_address_id,
            source=source_enum,
        )

        # Defense-in-depth: the grocery backstop (spec 5.8 layer 3) must hold
        # even when a caller supplies replay_scope directly, since replay_scope
        # bypasses resolve_allocation_scope's own grocery check entirely.
        # Grocery cash is mirrored into a corporate contract, so it may NEVER
        # carry cluster/place scope — no caller may override this.
        if customer.is_grocery_store:
            from business_app.services.allocation_scope import AllocationScope

            scope = AllocationScope.personal(customer_id)

        target_payment: Optional[Payment] = None
        self._validate_collection_context(
            customer_id=customer_id,
            scope=scope,
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
            # Acquire the WHOLE id-ordered candidate set — the transfer's target
            # included — BEFORE resolving it and before the target-first
            # allocation below. Two reasons this cannot be left to
            # `_allocate_scoped`: (1) a transfer that merely settles its target
            # never reaches the ring walk, so this is the only lock the target
            # ever gets and skipping it trades a deadlock for a lost update;
            # (2) resolution may CONVERT an electronic order to cash, which
            # writes the target row and therefore takes its lock — ahead of a
            # batch holding lower-id debts of the same customer, that is exactly
            # the inversion. A payment row created during resolution (COD order
            # with no payment yet) needs no lock: nobody else can see our insert.
            self._lock_scoped_payments(scope=scope, order_id=order_id)
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
            scope_type=scope.scope_type,
            scope_snapshot=scope.to_snapshot(),
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
            # NET, not gross: the reserved slice is money this customer has
            # already handed over. Filling it from the transfer would orphan the
            # reservation, and the transfer's own surplus would stop short of
            # becoming credit. Whatever the transfer leaves unpaid is exactly
            # what the reservation closes at delivery.
            allocatable = min(
                self._to_decimal(event.unapplied_amount),
                net_open_receivable_amount(target_payment) if target_payment else Decimal("0.00"),
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
            # Spill any surplus onto the customer's other delivered COD debts,
            # oldest-first — the same rule every other collection source follows.
            # Target-first ordering is load-bearing: this is the one source that
            # may be posted before delivery, and _allocation_candidates ranks the
            # current order last, so only the residual may spill. Whatever is
            # still unapplied afterwards falls through to the pending-order
            # reservation sweep below, and then to customer prepaid credit.
            #
            # Guarded rather than called unconditionally: the allocator locks the
            # customer's debt rows, and a transfer that merely settles its target
            # (the common case) has no reason to take those locks in an order
            # opposite to the oldest-first sources.
            if self._to_decimal(event.unapplied_amount) > Decimal("0.00"):
                self._allocate_scoped(
                    event=event,
                    scope=scope,
                    order_id=order_id,
                    allocation_mode=allocation_mode,
                )
        elif allocations:
            # One id-ordered batch lock, then the caller's allocation order in
            # memory: locking row-by-row in caller order is a second lock order
            # over the same rows the ring allocator takes.
            locked_manual = self._lock_payments_by_ids(
                [allocation["payment_id"] for allocation in allocations if allocation.get("payment_id") is not None]
            )
            allocation_order = 0
            for allocation in allocations:
                allocation_order += 1
                payment = locked_manual.get(allocation.get("payment_id"))
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
            self._allocate_scoped(
                event=event,
                scope=scope,
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
        #
        # Ring 3 — the ONE step carved out of the frozen-scope replay rule
        # (spec 5.6). Rings 1-2 above settle historical debt, so on a correction
        # they must replay `scope`, which may be the original event's frozen
        # snapshot. This sweep must NOT: reservations are releasable,
        # forward-looking state (spec 5.7), not history. Resolving it from a
        # frozen scope would let a correction posted after an unlink re-create
        # reservations on a departed sibling's pending orders — parking credit
        # against a stranger's order and understating the driver's expected cash
        # there. So the universe is always re-resolved from the CURRENT cluster
        # of the event's customer, never from `scope`. Grocery mirrors
        # resolve_allocation_scope's backstop: grocery cash never co-mingles, so
        # it stays the single account no matter what the topology says.
        if self._to_decimal(event.unapplied_amount) > Decimal("0.00"):
            sweep_user_ids: List[int] = [customer_id]
            if not customer.is_grocery_store:
                from business_app.services.customer_link_service import CustomerLinkService

                sweep_user_ids = CustomerLinkService().get_cluster_user_ids(customer_id)
            self.auto_reserve_against_pending_payments(
                customer_id,
                actor_user_id=recorded_by_user_id or collector_user_id,
                cluster_user_ids=sweep_user_ids,
            )

        # Mirror the collected money onto the customer's corporate contract via the
        # single settle entry point: AMOUNT-mode -> COLLECT (money debt down);
        # legacy UNITS-mode -> amount-scaled TOPUP against the order's reserved/
        # consumed units (funds pre-delivery reservations too). No-op otherwise.
        # `mirror_to_corporate_contract=False` is for the one shape where the
        # money is NOT a collection against contract debt: a dead order's
        # card/Click prepayment being handed back as customer credit
        # (`credit_customer_for_dead_order_prepayment`). A cancelled order was
        # never CHARGEd against the contract — the CHARGE is posted at DELIVERED
        # — so a COLLECT here would pay down a debt that does not exist. Owner
        # ruling: credit a grocery customer exactly like everyone else, and
        # suppress the mirror for that flow alone.
        if customer.is_grocery_store and normalized_amount > Decimal("0.00") and mirror_to_corporate_contract:
            from business_app.services.corporate_contract_service import CorporateContractService

            CorporateContractService().settle_order_collection(
                user=customer,
                order_id=order_id,
                collected_amount=normalized_amount,
                source=source_enum.value,
                cash_event_id=event.id,
                delivery_id=delivery_id,
                actor_user_id=recorded_by_user_id or collector_user_id,
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
        scope: "AllocationScope",
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
            if not self._scope_covers_order(scope, order):
                raise ValidationError("Order does not belong to the selected customer")
            _electronic_methods = {PaymentMethod.CLICK, PaymentMethod.PAYME, PaymentMethod.CARD}
            if order.payment_method != PaymentMethod.CASH:
                # PERSONAL_CARD_TRANSFER and ADMIN_ADJUSTMENT may target a
                # non-CASH order. The former records a card→owner transfer;
                # the latter records a customer-credit prepayment that the
                # order-edit cascade creates when an admin reduces a card-
                # paid order (the card is never refunded — the value lives
                # as cash-only-usable customer credit).
                #
                # DELIVERY_COMPLETION joins them for settle-in-place: a driver
                # collecting the unpaid delta of an edited-up electronic order at
                # the door (plan 2026-08-08-open-receivable-ssot). Guarded on an
                # ACTUAL open receivable so this can never widen into a general
                # "post cash against any card order" hole — a settled card order
                # still raises here.
                # 🔴 THE THIRD CLAUSE IS THE CONTRACT WITH EVERY COLLECT SURFACE.
                # It is source-AGNOSTIC on purpose: `get_customer_cod_statement`
                # marks a row `is_collectible_target` with the SAME
                # `is_ledger_receivable` predicate, and the admin Record
                # Collection modal offers exactly those rows against ANY source
                # (standalone_meeting, next_delivery, backfill…). Restricting
                # this to DELIVERY_COMPLETION shipped a modal that offered an
                # order the endpoint then refused with "Only COD orders can be
                # targeted for COD collections" — a 400 the endpoint does not
                # even log, so it was invisible in prod.
                #
                # `is_ledger_receivable` (not `has_open_receivable`) is what
                # keeps this safe: for an electronic rail it is true ONLY for
                # PARTIALLY_PAID, so a live gateway payment can never be settled
                # by a cash collection — that path still converts.
                allowed_non_cash = (
                    (
                        source == CashCollectionSource.PERSONAL_CARD_TRANSFER
                        and order.payment_method in _electronic_methods
                    )
                    or source == CashCollectionSource.ADMIN_ADJUSTMENT
                    or (order.payment_method in _electronic_methods and is_ledger_receivable(order.payment))
                )
                if not allowed_non_cash:
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
            if delivery.order and not self._scope_covers_order(scope, delivery.order):
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
                if not scope.covers_payment(payment, payment.order):
                    raise ValidationError("Manual allocations must belong to the selected customer")
                if payment.payment_method != PaymentMethod.CASH:
                    raise ValidationError("Manual allocations can only target COD payments")
                if payment.order and payment.order.status != OrderStatus.DELIVERED:
                    raise ValidationError("Manual allocations can only target delivered COD orders")
                if self._to_decimal(payment.outstanding_amount) <= Decimal("0.00"):
                    raise ValidationError("Manual allocations can only target payments with outstanding balance")

        if source == CashCollectionSource.ADMIN_ADJUSTMENT and not recorded_by_user_id:
            raise ValidationError("recorded_by_user_id is required for admin adjustments")

    _OFFLINE_SETTLEABLE_STATUSES = {
        PaymentStatus.PENDING,
        PaymentStatus.CANCELLED,
        PaymentStatus.FAILED,
    }

    def convert_electronic_order_to_cash(
        self,
        order: Order,
        *,
        actor_user_id: Optional[int],
        reason: str,
    ) -> Payment:
        """Convert an unsuccessful electronic (Click/Payme/Card) order to CASH so
        it can be settled offline.  Releases any reserved marking codes, marks
        fiscalization NOT_REQUIRED, flips payment+order method to CASH, and returns
        the row-locked payment.  Idempotent: if the order is already CASH, returns
        the locked payment unchanged.
        """
        payment = order.payment
        if not payment:
            raise NotFoundError("Order has no payment to settle")

        # BOTH rails, not just the order's: the allocator reads the payment's, so
        # an order already reading CASH over an electronic payment row is exactly
        # the shape this conversion has to repair rather than skip.
        if order.payment_method == PaymentMethod.CASH and payment.payment_method == PaymentMethod.CASH:
            locked = Payment.query.with_for_update(of=Payment).get(payment.id)
            if not locked:
                raise NotFoundError("Payment not found")
            return locked

        _electronic_methods = {PaymentMethod.CLICK, PaymentMethod.PAYME, PaymentMethod.CARD}
        if not {order.payment_method, payment.payment_method} & _electronic_methods:
            raise ValidationError("Only electronic-method orders can be converted to cash")
        if payment.status not in self._OFFLINE_SETTLEABLE_STATUSES:
            raise ValidationError(
                "Only orders with a pending, cancelled or failed electronic payment " "can be settled offline"
            )

        from business_app.services.payment_fiscalization_service import PaymentFiscalizationService

        fiscal_service = PaymentFiscalizationService()

        # Release any marking codes reserved during PREPARE; already-released codes
        # are caught and logged so we don't abort an otherwise valid settlement.
        try:
            fiscal_service.release_reserved_marking_codes(
                payment,
                reason=reason,
                actor_user_id=actor_user_id,
            )
        except Exception as exc:
            logger.error("Failed to release marking codes for order %s: %s", order.id, exc)

        payment.payment_method = PaymentMethod.CASH
        order.payment_method = PaymentMethod.CASH

        # A cancelled/failed payment may carry a stale outstanding_amount projection
        # (e.g. zero after the gateway cancelled it).  Re-derive it so the subsequent
        # allocation can fully cover the balance.
        payment.outstanding_amount = max(
            Decimal("0.00"),
            self._to_decimal(payment.amount) - self._to_decimal(payment.amount_collected),
        )
        db.session.flush()

        # Now that the method is CASH, payment_requires_click_fiscalization returns
        # False → queue_click_fiscalization sets status = NOT_REQUIRED.
        try:
            fiscal_service.queue_click_fiscalization(payment.id, actor_user_id=actor_user_id)
        except Exception as exc:
            logger.error("Failed to mark fiscalization not-required for order %s: %s", order.id, exc)

        locked = Payment.query.with_for_update(of=Payment).get(payment.id)
        if not locked:
            raise NotFoundError("Payment not found")
        return locked

    def _resolve_target_payment_for_personal_card_transfer(
        self,
        *,
        order_id: Optional[int],
        actor_user_id: Optional[int],
    ) -> Payment:
        """Resolve (creating/converting if needed) the payment a personal card
        transfer settles first.

        Deliberately takes NO row lock of its own. ``post_collection`` calls
        ``_lock_scoped_payments(scope=scope, order_id=order_id)`` immediately
        BEFORE this resolver, and that batch already contains this row: the
        scope's ``current_payment_id`` is resolved from ``Payment.order_id ==
        order_id`` regardless of payment method, so an electronic order awaiting
        conversion is in the batch too. Locking it here instead inverted the lock
        order against every oldest-first post that also holds a lower-id debt of
        the same customer.

        The one row this resolver may touch that the batch cannot contain is a
        payment it CREATES (a COD order with no payment yet) — that needs no
        lock, because no other transaction can see our un-committed insert.
        """
        if order_id is None:
            raise ValidationError("order_id is required for personal card transfer collections")

        order = Order.query.options(joinedload(Order.payment)).get(order_id)
        if not order:
            raise NotFoundError("Order not found")

        _electronic_methods = {PaymentMethod.CLICK, PaymentMethod.PAYME, PaymentMethod.CARD}
        if order.payment_method in _electronic_methods:
            payment = order.payment
            if payment is None:
                raise NotFoundError("Order has no payment to settle")

            # ORDER MATTERS. A PENDING/CANCELLED/FAILED electronic payment ALSO
            # has an open receivable, and CONVERTING it is the correct, pinned
            # behaviour (tests/integration/test_staff_delivery_offline_cash.py).
            # So the convert test comes first; settle-in-place is the residual.
            if payment.status in self._OFFLINE_SETTLEABLE_STATUSES:
                return self.convert_electronic_order_to_cash(
                    order,
                    actor_user_id=actor_user_id,
                    reason="converted_to_cash_personal_card",
                )

            # SETTLE IN PLACE (plan 2026-08-08-open-receivable-ssot). A
            # successfully-settled electronic order whose total was later edited
            # upward owes the delta. We allocate onto the ELECTRONIC payment and
            # never flip payment_method, because converting would:
            #   * call queue_click_fiscalization, which has NO completed-guard
            #     and would rewrite an issued receipt's status to NOT_REQUIRED —
            #     erasing the fiscal record of the CARD-PAID portion. Owner
            #     policy 2026-08-08: we fiscalize strictly what was paid by card;
            #     items added at the door and paid in cash are deliberately not
            #     fiscalized. Converting destroys the half that must be kept.
            #   * move the whole `payment.amount` out of the electronic revenue
            #     bucket rather than just the delta;
            #   * disarm the Click duplicate-charge auto-reversal, which
            #     recomputes `electronic` from the live payment_method.
            #
            # `_allocate_to_payment` has no payment_method predicate, and this
            # row is already inside the id-ordered batch `post_collection` locked
            # before calling us (`_scoped_candidate_payment_ids` resolves
            # `current_payment_id` from `Payment.order_id` with no method
            # filter), so no new lock order is introduced.
            if has_open_receivable(payment):
                # The stored column is stale on a gateway-cancelled payment —
                # the same shape `convert_electronic_order_to_cash` re-derives.
                # `_allocate_to_payment` refuses an amount above
                # `payment.outstanding_amount`, so normalise before returning.
                payment.outstanding_amount = open_receivable_amount(payment)
                db.session.flush()
                return payment

            raise ValidationError(
                "Only orders with a pending, cancelled or failed electronic payment " "can be settled offline"
            )

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

        return payment

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

        # Unwind the corporate-contract mirror post_collection posted for this cash
        # event (grocery-store customers only; no-op otherwise). On an admin
        # collected-cash correction the replacement re-posts the corrected amount
        # under its own event id, so the net contract effect matches the correction.
        from business_app.services.corporate_contract_service import CorporateContractService

        CorporateContractService().reverse_order_collection(
            cash_event_id=event.id,
            actor_user_id=reversed_by_user_id,
            reason=f"Cash collection reversed: {reason}",
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
        commit: bool = True,
        allowed_session_statuses: Optional[frozenset] = None,
    ) -> CashCollectionEvent:
        """Admin correction for a recorded cash collection.

        Voids the original event (reversing any allocations including
        downstream prepayment auto-application) and creates a replacement
        carrying the same context with the corrected amount. Cross-linked
        via entry_metadata so the audit trail survives.
        """
        normalized_amount = self._to_decimal(new_amount)
        if normalized_amount < Decimal("0.00"):
            # 0 is valid: it records that no cash was actually collected (the
            # replacement below carries a zero-amount "no cash collected" event,
            # which post_collection already models). Only negatives are rejected.
            raise ValidationError("Adjusted amount cannot be negative")
        reason = (reason or "").strip()
        if not reason:
            raise ValidationError("An adjustment reason is required")

        # Acquire the anchor cluster's event batch — the TARGET included — in ONE
        # id-ordered FOR UPDATE before touching anything (spec 5.3 / R6). A bare
        # single-row lock on the target here is a lock-order inversion: this
        # transaction goes on to void the target and then repost, whose credit
        # path locks the cluster batch {E3, ...}. Voiding first protects only
        # OUR transaction from re-requesting the row — a concurrent post cannot
        # see our uncommitted void, so its own batch still contains and blocks
        # on the target (T1 holds E5, wants E3; T2 holds E3, blocks on E5).
        #
        # The anchor is read column-only, so the ORM row for the target is first
        # materialised BY the locking query and its state (voided_at below) is
        # therefore the post-lock version, not an identity-map leftover.
        anchor_customer_id = (
            db.session.query(CashCollectionEvent.customer_id).filter(CashCollectionEvent.id == event_id).scalar()
        )
        if anchor_customer_id is None:
            raise NotFoundError("Cash collection event not found")
        cluster_candidate_ids = [
            r[0]
            for r in db.session.query(CashCollectionEvent.id)
            .filter(
                CashCollectionEvent.customer_id.in_(self._credit_pool_for_anchor(anchor_customer_id)),
                CashCollectionEvent.voided_at.is_(None),
                CashCollectionEvent.unapplied_amount > 0,
            )
            .all()
        ]
        locked_events = self._lock_credit_events_by_ids(
            cluster_candidate_ids,
            must_hold_event_ids=[event_id],
        )
        event = locked_events.get(event_id)
        if not event:
            raise NotFoundError("Cash collection event not found")
        if event.voided_at:
            raise ValidationError("Cannot adjust a voided cash collection event")

        if event.driver_cash_session_id:
            session = DriverCashSession.query.get(event.driver_cash_session_id)
            if session:
                status_value = getattr(session.status, "value", session.status)
                allowed = (
                    allowed_session_statuses
                    if allowed_session_statuses is not None
                    else self.ADJUSTABLE_SESSION_STATUSES
                )
                if status_value not in allowed:
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

        # Spec 5.6: the replacement replays the ORIGINAL event's FROZEN scope —
        # never current topology. A link/group created between the collection and
        # this correction must not re-route the money: without this the repost
        # would re-resolve a (possibly widened) scope and settle a sibling's older
        # debt instead of the order the cash was actually collected against. The
        # scope-membership guard cannot catch that — the order's owner IS inside
        # the widened scope. Captured HERE, before the reversal below, so no future
        # change to the reversal path can hand us a mutated row to read.
        from business_app.services.allocation_scope import AllocationScope

        frozen_scope = AllocationScope.from_event(event)

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

        # post_collection requires notes when the amount is 0. A real
        # delivery-completion event carries no notes unless nothing was
        # collected, so a correction *down to* 0 must supply its own.
        replacement_notes = original_context["notes"]
        if normalized_amount == Decimal("0.00") and not (replacement_notes or "").strip():
            replacement_notes = f"Corrected to 0 (no cash collected): {reason}"

        replacement = self.post_collection(
            customer_id=original_context["customer_id"],
            amount=normalized_amount,
            source=original_context["source"],
            collector_user_id=original_context["collector_user_id"],
            recorded_by_user_id=adjusted_by_user_id,
            order_id=original_context["order_id"],
            delivery_id=original_context["delivery_id"],
            driver_cash_session_id=original_context["driver_cash_session_id"],
            notes=replacement_notes,
            proof_data=replacement_proof,
            occurred_at=original_context["occurred_at"],
            commit=False,
            bypass_driver_block_check=True,
            replay_scope=frozen_scope,
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

        if commit:
            db.session.commit()
        return replacement

    def _cod_payments_ordered(self, customer_id: int) -> List[Payment]:
        """CASH payments on the customer's DELIVERED orders, oldest-first.

        Unlike _active_cod_payments_query this keeps settled rows, so a caller modelling a
        pending reversal can resurrect one whose outstanding is only zero because the event
        being reversed paid it. Read-only by construction — see
        :meth:`_lock_payments_by_ids` for the one place payment locks are taken.
        """
        return self._cod_payments_ordered_for_users([customer_id])

    def _cod_payments_ordered_for_users(self, user_ids: List[int]) -> List[Payment]:
        """Ring-2 keep-settled mirror of :meth:`_active_cod_payments_query_for_users`.

        CASH payments on DELIVERED orders of these users, oldest-first, settled
        rows INCLUDED — a projection modelling a pending reversal must be able to
        resurrect a payment whose outstanding is only zero because the event
        being reversed paid it.

        Deliberately lock-free and deliberately WITHOUT a ``for_update`` option:
        this query orders by ``Order.created_at``, and a locking variant of it
        would be a second payment lock order alongside
        :meth:`_lock_payments_by_ids` — precisely the deadlock pair that method's
        docstring exists to prevent.
        """
        if not user_ids:
            return []
        return (
            Payment.query.join(Order, Payment.order_id == Order.id)
            .options(contains_eager(Payment.order))
            .filter(
                Payment.user_id.in_(user_ids),
                Order.status == OrderStatus.DELIVERED,
            )
            .order_by(Order.created_at.asc(), Payment.created_at.asc(), Payment.id.asc())
            .all()
        )

    def _place_cod_payments_ordered(self, address_ids: List[int]) -> List[Payment]:
        """Ring-1 keep-settled mirror of :meth:`_active_place_cod_payment_ids`.

        CASH payments on DELIVERED orders at these addresses, ANY owner, pure
        oldest-first (decision 6 — there is no "own order" at a workplace).
        Settled rows kept, for the same reversal-modelling reason as
        :meth:`_cod_payments_ordered_for_users`. Lock-free.
        """
        if not address_ids:
            return []
        return (
            Payment.query.join(Order, Payment.order_id == Order.id)
            .options(contains_eager(Payment.order))
            .filter(
                Order.delivery_address_id.in_(address_ids),
                Order.status == OrderStatus.DELIVERED,
            )
            .order_by(Order.created_at.asc(), Payment.created_at.asc(), Payment.id.asc())
            .all()
        )

    def _scoped_settled_candidates(
        self,
        scope: "AllocationScope",
        order_id: Optional[int],
        outstanding_of: Callable[[Payment], Decimal],
    ) -> List[Payment]:
        """Frozen-scope candidate universe for a projection (spec 5.6).

        The read-only twin of :meth:`_allocate_scoped`'s phase-3 ring assembly:
        same rings, same order, same current-order tail — but over keep-settled
        rows and an arbitrary ``outstanding_of``, so a caller can model balances
        as they would be AFTER a pending reversal.

        This mirror is what makes preview == apply structural rather than
        coincidental: a projection walking a different candidate universe from
        the allocator means the admin approves one outcome and causes another.
        Any change to ``_allocate_scoped``'s ring assembly must be made here too.
        """
        cluster_ids = list(scope.orderer_cluster_user_ids)
        if scope.scope_type != "place":
            # CLUSTER/PERSONAL: active debts oldest-first, current order last —
            # the convention _allocate_scoped's non-place branch follows.
            return self._allocation_candidates(
                customer_id=cluster_ids[0] if cluster_ids else None,
                order_id=order_id,
                payments=self._cod_payments_ordered_for_users(cluster_ids),
                outstanding_of=outstanding_of,
            )

        ring1 = [
            payment
            for payment in self._place_cod_payments_ordered(list(scope.address_ids))
            if outstanding_of(payment) > Decimal("0.00")
        ]
        ring1_ids = {payment.id for payment in ring1}
        ring2 = [
            payment
            for payment in self._cod_payments_ordered_for_users(cluster_ids)
            if payment.id not in ring1_ids and outstanding_of(payment) > Decimal("0.00")
        ]
        candidates = ring1 + ring2
        # The same defensive tail _allocate_scoped appends LAST for a place post
        # whose own order is in neither ring (both rings filter DELIVERED).
        if order_id:
            current = Payment.query.options(joinedload(Payment.order)).filter_by(order_id=order_id).first()
            if (
                current is not None
                and outstanding_of(current) > Decimal("0.00")
                and is_ledger_receivable(current)
                and current.id not in {payment.id for payment in candidates}
            ):
                candidates.append(current)
        return candidates

    def _allocation_candidates(
        self,
        *,
        customer_id: int,
        order_id: Optional[int],
        payments: List[Payment],
        outstanding_of: Callable[[Payment], Decimal],
    ) -> List[Payment]:
        """Payments the oldest-first allocator walks, in order: the scope's active COD
        debts oldest-first, then the current order's own payment.

        `outstanding_of` lets a projection model balances that differ from the stored rows.
        """
        candidates = [payment for payment in payments if outstanding_of(payment) > Decimal("0.00")]
        if order_id:
            current_order_payment = (
                Payment.query.options(joinedload(Payment.order)).filter_by(order_id=order_id).first()
            )
            if (
                current_order_payment
                and outstanding_of(current_order_payment) > Decimal("0.00")
                and is_ledger_receivable(current_order_payment)
                and current_order_payment.id not in {payment.id for payment in candidates}
            ):
                candidates.append(current_order_payment)
        return candidates

    @staticmethod
    def _plan_allocation(
        candidates: List[Payment],
        amount: Decimal,
        outstanding_of: Callable[[Payment], Decimal],
    ) -> Tuple[List[Tuple[Payment, Decimal]], Decimal]:
        """Split `amount` across `candidates` in order. Returns (plan, unallocated residual)."""
        plan: List[Tuple[Payment, Decimal]] = []
        remaining = amount
        for payment in candidates:
            if remaining <= Decimal("0.00"):
                break
            allocatable = min(outstanding_of(payment), remaining)
            if allocatable <= Decimal("0.00"):
                continue
            plan.append((payment, allocatable))
            remaining -= allocatable
        return plan, remaining

    def _oldest_first_rank(self, payment_ids: List[int]) -> Dict[int, int]:
        """SQL-derived oldest-first rank for a set of payment ids.

        Ordering MUST come from the database: the tz-awareness of DateTime
        columns is mixed within a session (flushed-but-not-reloaded rows are
        aware, SQLite-reloaded rows are naive), so a Python sort over
        Order.created_at / Payment.created_at can raise TypeError.
        """
        rows = (
            db.session.query(Payment.id)
            .join(Order, Payment.order_id == Order.id)
            .filter(Payment.id.in_(payment_ids))
            .order_by(Order.created_at.asc(), Payment.created_at.asc(), Payment.id.asc())
            .all()
        )
        return {r[0]: idx for idx, r in enumerate(rows)}

    def _scoped_candidate_payment_ids(
        self,
        *,
        scope: "AllocationScope",
        order_id: Optional[int],
    ) -> Tuple[List[int], List[int], Optional[int]]:
        """Phase 1 of the two-phase lock discipline: the full cross-ring
        candidate id superset, with NO locks taken.

        Returns ``(ring1_ids, ring2_ids, current_payment_id)``. Ring 1 is empty
        for every non-place scope; PERSONAL is simply a cluster of one, so it
        shares this path (and therefore the one lock order).
        """
        ring1_ids: List[int] = []
        if scope.scope_type == "place":
            ring1_ids = self._active_place_cod_payment_ids(list(scope.address_ids))
        ring1_id_set = set(ring1_ids)
        ring2_ids = [
            pid
            for pid in self._active_cod_payment_ids_for_users(list(scope.orderer_cluster_user_ids))
            if pid not in ring1_id_set
        ]
        current_payment_id: Optional[int] = None
        if order_id:
            # Payment.order_id is UNIQUE, so this is at most one row.
            current_payment_id = db.session.query(Payment.id).filter(Payment.order_id == order_id).scalar()
        return ring1_ids, ring2_ids, current_payment_id

    def _lock_scoped_payments(self, *, scope: "AllocationScope", order_id: Optional[int]) -> Dict[int, Payment]:
        """Phases 1+2: resolve the scope's candidate ids, then acquire them all
        in ONE id-ordered ``FOR UPDATE`` (see :meth:`_lock_payments_by_ids`).

        Used on its own by the PERSONAL_CARD_TRANSFER path, which must hold the
        whole batch (its target included, via ``current_payment_id``) before it
        settles the target — that allocation runs before the ring walk, and
        sometimes instead of it.
        """
        ring1_ids, ring2_ids, current_payment_id = self._scoped_candidate_payment_ids(scope=scope, order_id=order_id)
        return self._lock_payments_by_ids(self._combine_candidate_ids(ring1_ids, ring2_ids, current_payment_id))

    def lock_order_settlement_candidates(self, order: Order, *, source: Any) -> Dict[int, Payment]:
        """Pre-acquire the id-ordered payment batch a settlement of ``order`` will
        touch, resolving the same scope ``post_collection`` will resolve.

        For callers that must write a single payment row BEFORE calling
        ``post_collection`` in the same transaction — today
        ``convert_electronic_order_to_cash`` on the staff delivery-completion
        path, which returns a ROW-LOCKED payment. Without this, that lone lock is
        taken ahead of the batch that follows, and if the customer has an older
        debt with a lower payment id the transaction holds P_target then requests
        a set containing lower ids: the exact inversion removed from the
        PERSONAL_CARD_TRANSFER path, and it deadlocks against any concurrent post
        walking the batch in id order. Holding the batch first makes the later
        single-row lock a re-request of a row we already own.
        """
        scope = self.resolve_allocation_scope(
            order.user_id,
            delivery_address_id=order.delivery_address_id,
            source=source,
        )
        return self._lock_scoped_payments(scope=scope, order_id=order.id)

    @staticmethod
    def _combine_candidate_ids(
        ring1_ids: Iterable[int],
        ring2_ids: Iterable[int],
        current_payment_id: Optional[int],
    ) -> set:
        all_ids = set(ring1_ids) | set(ring2_ids)
        if current_payment_id is not None:
            all_ids.add(current_payment_id)
        return all_ids

    def _allocate_scoped(
        self,
        *,
        event: CashCollectionEvent,
        scope: "AllocationScope",
        order_id: Optional[int],
        allocation_mode: str,
        trigger_completion_notification: bool = True,
    ) -> None:
        """Scope-aware ring allocator (spec 5.2).

        Every scope — PERSONAL included — follows the two-phase locking
        discipline (spec 5.3): resolve the full cross-ring candidate id superset
        first, lock every row in ONE query ordered by Payment.id ASC (so
        concurrent posts over overlapping rows always acquire locks in the same
        order), then apply ring/oldest-first ordering purely in memory over the
        locked rows.

        PERSONAL is a cluster of one and produces byte-identical allocations to
        the as-built oldest-first path; it shares this path precisely so it
        cannot lock in a different order from a place/cluster post touching the
        same rows.
        """
        if self._to_decimal(event.amount) <= Decimal("0.00"):
            return

        # Phase 1 — candidate id superset (no locks).
        ring1_ids, ring2_ids, current_payment_id = self._scoped_candidate_payment_ids(scope=scope, order_id=order_id)
        all_ids = self._combine_candidate_ids(ring1_ids, ring2_ids, current_payment_id)
        if not all_ids:
            return

        # Phase 2 — ONE deterministic lock query (deadlock avoidance).
        by_id = self._lock_payments_by_ids(all_ids)

        def live_outstanding(payment: Payment) -> Decimal:
            # NET of reserved prepayment — the same figure the driver's screen
            # quotes. A door collection of the GROSS amount (customer hands over
            # the full total even though only the net was due) must leave the
            # surplus as credit, not overwrite the reservation's slice and
            # strand it. Ring 1/2 candidates are DELIVERED orders, which
            # normally carry no reservation, so in practice this bites on the
            # current order appended pre-delivery.
            return net_open_receivable_amount(payment)

        # Phase 3 — ring ordering in memory over the already-locked rows, using
        # the SQL-derived rank (never a Python datetime sort — see
        # _oldest_first_rank).
        rank = self._oldest_first_rank(list(all_ids))
        ring1 = sorted((by_id[i] for i in ring1_ids if i in by_id), key=lambda p: rank[p.id])
        ring2 = sorted((by_id[i] for i in ring2_ids if i in by_id), key=lambda p: rank[p.id])

        if scope.scope_type == "place":
            # Decision 6: pure oldest-first — the just-delivered order
            # participates by age; there is no "own order" at a workplace.
            candidates = ring1 + ring2
            # Defensive fallback, mirroring CLUSTER below: both rings filter
            # Order.status == DELIVERED, so a place-scoped source posted against
            # an order that is not (yet) DELIVERED — the order-status guard is
            # skipped entirely in that shape — would otherwise send the
            # just-collected order's cash to coworkers/credit. Appending LAST
            # preserves pure oldest-first for every order that IS in ring 1.
            if current_payment_id is not None and current_payment_id not in {p.id for p in candidates}:
                current = by_id.get(current_payment_id)
                if (
                    current is not None
                    and live_outstanding(current) > Decimal("0.00")
                    and is_ledger_receivable(current)
                ):
                    candidates.append(current)
        else:
            # CLUSTER keeps the as-built _allocation_candidates convention:
            # active debts oldest-first; the current order's payment appended
            # only when not already among them (e.g. PCT posted pre-delivery).
            candidates = list(ring2)
            if current_payment_id is not None and current_payment_id not in {p.id for p in candidates}:
                current = by_id.get(current_payment_id)
                if (
                    current is not None
                    and live_outstanding(current) > Decimal("0.00")
                    and is_ledger_receivable(current)
                ):
                    candidates.append(current)

        # RAIL-AGNOSTIC TERMINAL FILTER (plan 2026-08-08-open-receivable-ssot).
        #
        # This used to end `and p.payment_method == PaymentMethod.CASH`. It HAD to
        # widen in lockstep with the ring queries above, and this is the sixth
        # instance of the show-vs-settle defect class if it does not:
        # `cluster_delivered_outstanding_amount` and the debtor rows now count an
        # electronic receivable as collectible debt, so a driver offered "collect
        # 50 000" against 20 000 COD + 30 000 electronic would have had the
        # 30 000 silently become prepaid credit while the electronic debt stayed
        # open forever — a figure advertised and then refused, which is strictly
        # worse than the invisible receivable this plan set out to fix.
        #
        # Safety is unchanged in substance: every candidate here already had to
        # clear `Order.status == DELIVERED` plus a positive live outstanding, and
        # `_allocate_to_payment` still refuses to over-allocate. What did NOT
        # widen is the customer-CREDIT family — reserving/spending prepaid credit
        # stays CASH-only, so no card order can ever consume a customer's cash
        # credit.
        candidates = [p for p in candidates if live_outstanding(p) > Decimal("0.00")]
        plan, _residual = self._plan_allocation(candidates, self._to_decimal(event.unapplied_amount), live_outstanding)
        base_allocation_order = self._next_allocation_order(event.id)
        for offset, (payment, allocatable) in enumerate(plan):
            self._allocate_to_payment(
                event=event,
                payment=payment,
                amount=allocatable,
                allocation_order=base_allocation_order + offset,
                allocation_mode=allocation_mode,
                trigger_completion_notification=trigger_completion_notification,
            )

    def _allocate_oldest_first(
        self,
        *,
        event: CashCollectionEvent,
        customer_id: int,
        order_id: Optional[int],
        allocation_mode: str,
        trigger_completion_notification: bool = True,
    ) -> None:
        """Personal-scope oldest-first allocation.

        Kept as a named entry point (``scripts/remediate_stranded_card_transfer_
        surplus.py`` calls it) but delegating to :meth:`_allocate_scoped` so
        there is exactly ONE payment lock order in the codebase. A personal post
        by an unlinked user routinely targets the same rows as a place-scoped
        post by a coworker; locking them in ``Order.created_at`` order here and
        ``Payment.id`` order there is precisely the deadlock pair.
        """
        from business_app.services.allocation_scope import AllocationScope

        self._allocate_scoped(
            event=event,
            scope=AllocationScope.personal(customer_id),
            order_id=order_id,
            allocation_mode=allocation_mode,
            trigger_completion_notification=trigger_completion_notification,
        )

    def preview_personal_card_transfer(
        self,
        *,
        order_id: int,
        amount: Any,
    ) -> PersonalCardTransferPlan:
        """Project ``post_collection(source=personal_card_transfer)`` without mutating.

        Reuses ``_plan_allocation`` — the same splitter the real path runs — so the
        admin modal cannot drift from what actually happens when they confirm.
        """
        amount = self._to_decimal(amount)
        if amount < Decimal("0.00"):
            raise ValidationError("Collection amount cannot be negative")

        order = Order.query.options(joinedload(Order.payment)).get(order_id)
        if not order:
            raise NotFoundError("Order not found")

        warnings: List[str] = []
        payment = order.payment
        if payment and payment.payment_method == PaymentMethod.CASH:
            # Net of reserved prepayment, matching the apply path above. Quoting
            # the gross told the admin the customer owed money their own credit
            # had already covered, and — because the modal pre-fills this figure
            # — invited a transfer that consumed the reservation's slice.
            target_outstanding = net_open_receivable_amount(payment)
            target_payment_id = payment.id
        elif payment and payment.status not in self._OFFLINE_SETTLEABLE_STATUSES and has_open_receivable(payment):
            # SETTLE IN PLACE: the rail is preserved and only the unpaid delta is
            # collectable. Quoting `order.total_amount` here would advertise
            # collecting the whole order from a customer who has already paid
            # most of it by card — exactly the preview/apply drift this method's
            # docstring exists to prevent. Mirrors the branch order in
            # `_resolve_target_payment_for_personal_card_transfer`.
            target_outstanding = open_receivable_amount(payment)
            target_payment_id = payment.id
        else:
            # An unsettled electronic order is converted to COD when the transfer
            # is recorded, and a COD order with no payment row yet gets one;
            # either way the target starts out owing the full order total.
            if order.payment_method in {PaymentMethod.CLICK, PaymentMethod.PAYME, PaymentMethod.CARD}:
                warnings.append("order_will_convert_to_cash")
            target_outstanding = self._to_decimal(order.total_amount)
            target_payment_id = None

        applied_to_order = min(amount, target_outstanding)
        residual = amount - applied_to_order

        # The target settles first, so it is excluded from the spill exactly as the
        # live allocator excludes it (its outstanding is 0 by then). The spill
        # universe comes from the SAME entry point post_collection uses —
        # resolve_allocation_scope + get_active_cod_payments_for_scope — rather
        # than a cluster lookup of its own. Two divergences that cost:
        # a preview reading only the single account under-reports where a linked
        # person's money lands, and a preview reading the cluster directly
        # bypasses the grocery backstop (spec 5.8 layer 3), promising a spill onto
        # a linked account that the forced-PERSONAL post can never perform.
        # PERSONAL_CARD_TRANSFER is not in _PLACE_SCOPE_SOURCES, so this resolves
        # CLUSTER or PERSONAL and never a place — coworkers are not a wallet.
        scope = self.resolve_allocation_scope(
            order.user_id,
            delivery_address_id=order.delivery_address_id,
            source=CashCollectionSource.PERSONAL_CARD_TRANSFER,
        )
        candidates = [
            candidate
            for candidate in self.get_active_cod_payments_for_scope(scope)
            if candidate.id != target_payment_id
        ]
        # Byte-identical to `_allocate_scoped`'s `live_outstanding`, which is what
        # the confirm actually walks. Spill candidates are DELIVERED orders and so
        # normally carry no reservation, making this the same number as the gross
        # today — but a preview that computes the balance a DIFFERENT way from the
        # apply is the drift this method exists to prevent, whatever the current
        # data happens to look like.
        plan, remaining_as_credit = self._plan_allocation(candidates, residual, net_open_receivable_amount)

        spill_allocations = []
        for candidate, allocatable in plan:
            outstanding_before = net_open_receivable_amount(candidate)
            spill_allocations.append(
                {
                    "order_id": candidate.order_id,
                    "order_number": candidate.order.order_number if candidate.order else None,
                    "amount": allocatable,
                    "outstanding_before": outstanding_before,
                    "outstanding_after": outstanding_before - allocatable,
                }
            )

        if remaining_as_credit > Decimal("0.00"):
            warnings.append("surplus_becomes_customer_credit")

        return PersonalCardTransferPlan(
            order_id=order.id,
            order_number=order.order_number,
            amount=amount,
            applied_to_order=applied_to_order,
            order_outstanding_before=target_outstanding,
            order_outstanding_after=target_outstanding - applied_to_order,
            applied_to_other_debts=residual - remaining_as_credit,
            remaining_as_credit=remaining_as_credit,
            spill_allocations=spill_allocations,
            warnings=warnings,
        )

    def simulate_event_amount_change(
        self,
        *,
        event: CashCollectionEvent,
        new_amount: Any,
        order_id: Optional[int],
    ) -> Dict[str, Decimal]:
        """Project adjust_event_amount(event, new_amount) without mutating anything.

        Mirrors the void-then-repost sequence: hand the event's live allocations back to
        their payments, then re-run the oldest-first split for the new amount. Callers get
        the truth an order-total-based estimate misses when the payment is already settled
        from another source (card transfer, prepaid credit), where nothing can be applied
        and the whole amount lands as customer credit.

        The candidate universe is the event's FROZEN scope (spec 5.6) — exactly
        what ``adjust_event_amount`` replays via ``replay_scope=`` — so the modal
        cannot promise one outcome and the confirmation cause another. Resolving
        it from the poster's single account under-reported every cluster/place
        event: the admin was told "10 000 settles this order" while the
        correction actually settled a sibling's or coworker's older debt first.

        Boundary, stated deliberately: this projects rings 1-2 (debt settlement).
        It does NOT model ring 3, the residual reservation sweep, whose
        allocations are releasable forward-looking state rather than debt
        payment — ``credit_after`` is therefore the credit BEFORE any of it is
        reserved against pending orders, which is the figure the modal's
        "customer credit" line has always meant.
        """
        new_amount = self._to_decimal(new_amount)
        restored: Dict[int, Decimal] = {}
        for allocation in event.allocations:
            if allocation.reversed_at or not self._allocation_affects_payment_projection(allocation):
                continue
            restored[allocation.payment_id] = restored.get(allocation.payment_id, Decimal("0.00")) + self._to_decimal(
                allocation.allocated_amount
            )

        def outstanding_after_reversal(payment: Payment) -> Decimal:
            amount = self._to_decimal(payment.amount)
            collected = self._to_decimal(payment.amount_collected) - restored.get(payment.id, Decimal("0.00"))
            collected = min(amount, max(Decimal("0.00"), collected))
            # Net of reserved prepayment, exactly as `_allocate_scoped`'s
            # `live_outstanding` is — the replay this projects runs through that
            # allocator, so quoting gross here would re-introduce the
            # preview/apply drift this method exists to prevent.
            gross = max(Decimal("0.00"), amount - collected)
            return max(Decimal("0.00"), gross - reserved_prepayment_amount(payment, ceiling=gross))

        from business_app.services.allocation_scope import AllocationScope

        scope = AllocationScope.from_event(event)
        # Mirrors the grocery backstop in post_collection (spec 5.8 layer 3):
        # if the event's customer is CURRENTLY a grocery store, the replay
        # apply forces PERSONAL scope regardless of what was stamped at post
        # time, so the projection must too — otherwise a customer converted
        # to grocery AFTER a cluster/place-scoped event gets a preview that
        # promises a spill the apply can never perform. Keep these two in
        # sync: any change to one backstop needs the mirrored change here.
        if event.customer is not None and event.customer.is_grocery_store:
            scope = AllocationScope.personal(event.customer_id)
        if scope.scope_type == "personal":
            # Kept explicit rather than folded into _scoped_settled_candidates so
            # the unlinked/ungrouped projection is structurally the as-built one
            # (the two are equivalent — personal is a cluster of one — but this
            # way "personal is unchanged" is readable, not inferred).
            candidates = self._allocation_candidates(
                customer_id=event.customer_id,
                order_id=order_id,
                payments=self._cod_payments_ordered(event.customer_id),
                outstanding_of=outstanding_after_reversal,
            )
        else:
            candidates = self._scoped_settled_candidates(scope, order_id, outstanding_after_reversal)
        plan, residual = self._plan_allocation(candidates, new_amount, outstanding_after_reversal)

        applied_to_order = sum(
            (amount for payment, amount in plan if payment.order_id == order_id),
            Decimal("0.00"),
        )
        order_payment = Payment.query.filter_by(order_id=order_id).first() if order_id else None
        outstanding_before = outstanding_after_reversal(order_payment) if order_payment else Decimal("0.00")
        return {
            "applied_to_order": applied_to_order,
            "applied_total": new_amount - residual,
            "credit_before": self._to_decimal(event.unapplied_amount),
            "credit_after": residual,
            "order_amount": self._to_decimal(order_payment.amount) if order_payment else Decimal("0.00"),
            "order_outstanding_before": outstanding_before,
            "order_outstanding_after": max(Decimal("0.00"), outstanding_before - applied_to_order),
        }

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
            # Denormalized at-allocation audit stamps (spec §4.3/R6). Captured
            # here, at allocation time, precisely because payment.user_id is
            # mutable — a later re-read would fabricate history. The FK pair
            # (event_id, payment_id) remains the source of truth; pre-migration
            # rows stay NULL by design. Once allocation widens beyond a single
            # account these two may legitimately differ, and they are the only
            # record of who paid for whom.
            source_customer_id=event.customer_id,
            beneficiary_user_id=payment.user_id,
        )
        db.session.add(allocation)
        event.unapplied_amount = self._to_decimal(event.unapplied_amount) - amount
        previous_status = payment.status

        if affect_payment_projection:
            payment.amount_collected = self._to_decimal(payment.amount_collected) + amount
            # ARCH-006: propagate an auditable collector identity onto the payment
            # row as it projects. Prefer the on-route collector; fall back to
            # whoever recorded the event (e.g. an admin booking a balance-
            # application allocation). sync_payment_projection stamps + enforces.
            self.sync_payment_projection(
                payment,
                collected_at=event.occurred_at,
                collected_by=event.collector_user_id or event.recorded_by_user_id,
            )
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
