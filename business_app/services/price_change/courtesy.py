"""The price-change courtesy: who may keep the old price for one order after the switch (spec §5.6).

A person is a linked-phone cluster (`customer_key_sql`); a business account is its own person, because entities are
never linked. With A the instant the announcement started and C the instant the switch applied, a person is on the
list when they have a DELIVERED order placed before A, no order placed in [A, C) that is still alive (every source,
subscription auto-orders included), and no corporate contract in force at C (contract accounts are outside the
courtesy). "Placed" is `Order.created_at`; "delivered" is the order's status, so a legacy delivered order without a
history row counts.

The list is decided once, inside the switch's transaction: `build_courtesy_list` flushes and never commits.
"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

from sqlalchemy import func

from business_app import db
from business_app.models.order import Order
from business_app.models.price_change import CourtesySource, PriceChange, PriceChangeStatus, PriceCourtesy
from business_app.models.user import User
from business_app.services.corporate_contract_service import CorporateContractService
from business_app.services.customer_link_service import CustomerLinkService
from business_app.utils.exceptions import ConflictError, NotFoundError, ValidationError
from business_app.utils.line_money import line_money
from business_app.utils.payment_projection import DEAD_ORDER_STATUSES
from business_app.utils.timezone_utils import ensure_utc, get_utc_now
from business_app.utils.transactions import atomic_transaction
from shared.enums import OrderStatus


def build_courtesy_list(price_change: PriceChange) -> int:
    """Write one AUTOMATIC courtesy entry per eligible person, anchored on the lowest id among the person's accounts
    with a delivered order placed before the announcement, in anchor order. Returns how many rows it wrote.

    Raises ValueError for a change that was never announced or has not been applied: the rule reads both instants.
    """
    # facts.py imports OrderService, so the customer key is imported where it is used.
    from business_app.services.reorder_reminders.facts import customer_key_sql

    if price_change.announcement_started_at is None:
        raise ValueError(f"Price change {price_change.id} was never announced")
    if price_change.applied_at is None:
        raise ValueError(f"Price change {price_change.id} has not been applied")
    announced_at = ensure_utc(price_change.announcement_started_at)
    applied_at = ensure_utc(price_change.applied_at)
    key = customer_key_sql(User)

    existing = (
        db.session.query(key, func.min(User.id))
        .select_from(User)
        .join(Order, Order.user_id == User.id)
        .filter(Order.status == OrderStatus.DELIVERED, Order.created_at < announced_at)
        .group_by(key)
        .all()
    )
    ordered_at_the_old_price = {
        person
        for (person,) in db.session.query(key)
        .select_from(User)
        .join(Order, Order.user_id == User.id)
        .filter(
            Order.created_at >= announced_at,
            Order.created_at < applied_at,
            Order.status.notin_(DEAD_ORDER_STATUSES),
        )
        .distinct()
    }
    under_contract = CorporateContractService().user_ids_with_active_contract(effective_at=applied_at)

    anchors = sorted(
        anchor for person, anchor in existing if person not in ordered_at_the_old_price and anchor not in under_contract
    )
    db.session.add_all(
        [
            PriceCourtesy(
                price_change_id=price_change.id,
                user_id=anchor,
                source=CourtesySource.AUTOMATIC,
                reason=None,
                granted_by_id=None,
                granted_at=applied_at,
                expires_at=price_change.courtesy_ends_at,
            )
            for anchor in anchors
        ]
    )
    db.session.flush()
    return len(anchors)


COURTESY_AVAILABLE, COURTESY_USED, COURTESY_NONE = "available", "used", "none"


@dataclass(frozen=True)
class CourtesyCap:
    product_id: int
    product_name: str
    cap: int


@dataclass(frozen=True)
class CourtesyStatus:
    state: str
    expires_on: Optional[date]
    caps: Tuple[CourtesyCap, ...]  # sorted by product_id
    used_order_id: Optional[int]
    used_order_number: Optional[str]


_NONE = CourtesyStatus(COURTESY_NONE, None, (), None, None)


def _open_window_change(at: datetime) -> Optional[PriceChange]:
    """The latest APPLIED change with applied_at <= at < courtesy_ends_at, or None."""
    at = ensure_utc(at)
    latest = (
        PriceChange.query.filter(PriceChange.status == PriceChangeStatus.APPLIED, PriceChange.applied_at <= at)
        .order_by(PriceChange.applied_at.desc(), PriceChange.id.desc())
        .first()
    )
    if latest is None or at >= ensure_utc(latest.courtesy_ends_at):
        return None
    return latest


def _cluster_entries(price_change_id: int, user_id: int, *, lock: bool = False) -> List[PriceCourtesy]:
    """Every entry of the person (all linked accounts) for the change, ordered by id. `lock` takes the row locks."""
    cluster_ids = CustomerLinkService().get_cluster_user_ids(user_id)
    query = PriceCourtesy.query.filter(
        PriceCourtesy.price_change_id == price_change_id, PriceCourtesy.user_id.in_(cluster_ids)
    ).order_by(PriceCourtesy.id)
    if lock:
        query = query.with_for_update().populate_existing()
    return query.all()


class _PersonCourtesy(NamedTuple):
    price_change: PriceChange
    entries: List[PriceCourtesy]  # every entry of the person for the change, ordered by id
    used: Optional[PriceCourtesy]  # the entry that holds an order, or None while the courtesy is unused


def _person_courtesy(user_id: int, at: datetime, *, lock: bool = False) -> Optional[_PersonCourtesy]:
    """The one courtesy gate for a person at `at`, read by both `courtesy_status` and `apply_courtesy`: None when no
    courtesy window is open, the account is under a contract, or the person has no entry for the open change.
    `lock` takes the row locks on every entry of the person."""
    price_change = _open_window_change(at)
    if price_change is None:
        return None
    if CorporateContractService().has_active_contract(user_id, effective_at=at):
        return None
    entries = _cluster_entries(price_change.id, user_id, lock=lock)
    if not entries:
        return None
    used = next((entry for entry in entries if entry.order_id is not None), None)
    return _PersonCourtesy(price_change, entries, used)


def courtesy_status(user_id: int, *, at: datetime) -> CourtesyStatus:
    """The person's courtesy at `at`: none outside the window or under a contract, used once an entry holds an order,
    available while an entry is unused."""
    person = _person_courtesy(user_id, at)
    if person is None:
        return _NONE
    price_change, used = person.price_change, person.used
    if used is not None:
        return CourtesyStatus(COURTESY_USED, price_change.courtesy_last_day, (), used.order_id, used.order.order_number)
    caps = tuple(
        CourtesyCap(item.product_id, item.product.name, item.courtesy_cap)
        for item in sorted(price_change.items, key=lambda item: item.product_id)
    )
    return CourtesyStatus(COURTESY_AVAILABLE, price_change.courtesy_last_day, caps, None, None)


def apply_courtesy(
    lines: List[Dict[str, Any]], *, user_id: int, at: datetime, is_subscription_order: bool, claim: bool
) -> Optional[PriceCourtesy]:
    """Price the person's lines with the courtesy: up to each product's cap sells at the item's old price.

    Reads per line `product_id`, `quantity`, `unit_price`, `is_reward_item` (absent: False) and
    `contract_product_price_id` (absent: None). A line it lowers gets `courtesy_quantity`, `courtesy_unit_price` and
    the `line_money` fields `discount_amount`, `total_price` and `regular_quantity`; every other line is left exactly
    as given. Returns the person's lowest-id entry when it lowered at least one line, else None with no line changed.

    None, nothing changed: a subscription order, no open courtesy window at `at`, an account under contract, a person
    without an entry, or a person any of whose entries already holds an order. `claim=False` (a preview) reads the
    entries without locks; `claim=True` (order creation) locks every entry of the person first. Neither writes one:
    order creation hands the answered entry to `mark_used` in the same transaction.
    """
    if is_subscription_order:
        return None
    person = _person_courtesy(user_id, at, lock=claim)
    if person is None or person.used is not None:
        return None

    plan = _plan_courtesy(lines, person.price_change)
    if not plan:
        return None
    for line, unit_price, courtesy_quantity, old_price in plan:
        money = line_money(unit_price, int(line["quantity"]), courtesy_quantity, old_price)
        line.update(
            courtesy_quantity=courtesy_quantity,
            courtesy_unit_price=old_price,
            discount_amount=money.discount_amount,
            total_price=money.total_price,
            regular_quantity=money.regular_quantity,
        )
    return person.entries[0]


def _plan_courtesy(
    lines: List[Dict[str, Any]], price_change: PriceChange
) -> List[Tuple[Dict[str, Any], Decimal, int, Decimal]]:
    """(line, unit price, courtesy units, old price) for each line the cap reaches. The cap is per product across all
    its lines: in line order, each eligible line takes min(quantity, what is left of the cap). Eligible = a product
    of the change, not a reward line, not contract-priced, and priced above the old price."""
    items = {item.product_id: item for item in price_change.items}
    remaining = {product_id: item.courtesy_cap for product_id, item in items.items()}
    plan = []
    for line in lines:
        item = items.get(line["product_id"])
        if item is None or line.get("is_reward_item", False) or line.get("contract_product_price_id") is not None:
            continue
        unit_price = Decimal(str(line["unit_price"]))
        if not item.old_price < unit_price:
            continue
        courtesy_quantity = min(int(line["quantity"]), remaining[item.product_id])
        if courtesy_quantity > 0:
            plan.append((line, unit_price, courtesy_quantity, item.old_price))
            remaining[item.product_id] -= courtesy_quantity
    return plan


def mark_used(entry: PriceCourtesy, order: Order) -> None:
    """The claimed entry now belongs to `order`: the courtesy is used for the whole person until it is given back.

    Called with the entry `apply_courtesy(..., claim=True)` answered, in the SAME transaction as that claim with no
    commit between them, so the claim's row locks are still held; the order row is flushed first, so it has its id.
    Flushes and leaves the commit to the caller. Raises ConflictError (PRICE_COURTESY_ALREADY_USED) for an entry that
    already holds an order.
    """
    if entry.order_id is not None:
        raise ConflictError("This courtesy is already used by an order", error_code="PRICE_COURTESY_ALREADY_USED")
    entry.order_id = order.id
    entry.used_at = get_utc_now()
    db.session.flush()


def release_for_order(order_id: int) -> None:
    """Give back the courtesy an order used: every entry holding `order_id` is unused again, for the rest of the
    window. A no-op for an order that used nothing, so calling it twice is harmless.

    The entry it gives back was claimed and marked used in ONE transaction with no commit between them, the order
    row flushed first (`mark_used`). Flushes and leaves the commit to the caller (the cancellation's own).
    """
    for entry in PriceCourtesy.query.filter(PriceCourtesy.order_id == order_id):
        entry.order_id = None
        entry.used_at = None
    db.session.flush()


_REFUSAL_MESSAGES = {
    "PRICE_COURTESY_USER_NOT_FOUND": "Customer not found.",
    "PRICE_COURTESY_WINDOW_CLOSED": "There is no open courtesy window to grant the old price in.",
    "PRICE_COURTESY_CONTRACT_ACCOUNT": "Accounts under a contract are outside the courtesy.",
    "PRICE_COURTESY_ALREADY_EXISTS": "This person already has the old price for this change.",
}
_REFUSAL_ERRORS = {
    "PRICE_COURTESY_USER_NOT_FOUND": NotFoundError,
    "PRICE_COURTESY_WINDOW_CLOSED": ValidationError,
    "PRICE_COURTESY_CONTRACT_ACCOUNT": ValidationError,
    "PRICE_COURTESY_ALREADY_EXISTS": ConflictError,
}


def _refusal(code: str) -> Tuple[str, str]:
    return code, _REFUSAL_MESSAGES[code]


def grant_refusal(user_id: int, *, at: Optional[datetime] = None) -> Optional[Tuple[str, str]]:
    """`(error_code, message)` of the first reason the manager exception cannot be granted to this account at `at`
    (default now), else None. THE rule: `grant_exception` raises it and the admin page's grant button reads it.

    In order: no such user; no open courtesy window; an account under contract (outside the courtesy, spec §5.6);
    any entry for the person, used or not (one per person).
    """
    at = at if at is not None else get_utc_now()
    if db.session.get(User, user_id) is None:
        return _refusal("PRICE_COURTESY_USER_NOT_FOUND")
    price_change = _open_window_change(at)
    if price_change is None:
        return _refusal("PRICE_COURTESY_WINDOW_CLOSED")
    if CorporateContractService().has_active_contract(user_id, effective_at=at):
        return _refusal("PRICE_COURTESY_CONTRACT_ACCOUNT")
    if _cluster_entries(price_change.id, user_id):
        return _refusal("PRICE_COURTESY_ALREADY_EXISTS")
    return None


def grant_exception(user_id: int, reason: str, actor_id: int) -> PriceCourtesy:
    """Give one person the old price for the open window, on a manager's or admin's word (the route checks the role).

    The open change row is locked first, so two managers granting to two phones of one person serialise and the
    second sees the first's entry. Commits its own transaction.
    """
    reason = reason.strip()
    if not reason:
        raise ValidationError(
            "A reason is required to grant the old price.", error_code="PRICE_COURTESY_REASON_REQUIRED"
        )
    with atomic_transaction():
        now = get_utc_now()
        open_change = _open_window_change(now)
        if open_change is not None:
            PriceChange.query.filter(PriceChange.id == open_change.id).with_for_update().populate_existing().one()
        refusal = grant_refusal(user_id, at=now)
        if refusal is not None:
            code, message = refusal
            raise _REFUSAL_ERRORS[code](message, error_code=code)
        entry = PriceCourtesy(
            price_change_id=open_change.id,
            user_id=user_id,
            source=CourtesySource.MANAGER,
            reason=reason,
            granted_by_id=actor_id,
            granted_at=now,
            expires_at=open_change.courtesy_ends_at,
        )
        db.session.add(entry)
        db.session.flush()
    return entry


def _grant_action(user_id: int, *, can_grant: bool) -> Dict[str, Any]:
    """The grant button's state. A role refusal and "no open window" answer `block_reason: None` (hidden: nothing
    the viewer could do); every other refusal keeps the sentence the write itself would raise (pf G-17)."""
    if not can_grant:
        return {"allowed": False, "block_reason": None}
    refusal = grant_refusal(user_id)
    if refusal is None:
        return {"allowed": True, "block_reason": None}
    code, message = refusal
    return {"allowed": False, "block_reason": None if code == "PRICE_COURTESY_WINDOW_CLOSED" else message}


def customer_price_courtesy_view(user_id: int, *, can_grant: bool) -> Dict[str, Any]:
    """What both admin routes answer: the person's courtesy status now and what the viewer may do about it.

    Raises NotFoundError (PRICE_COURTESY_USER_NOT_FOUND) for every viewer first, so the role half never decides
    whether an account exists.
    """
    if db.session.get(User, user_id) is None:
        code, message = _refusal("PRICE_COURTESY_USER_NOT_FOUND")
        raise NotFoundError(message, error_code=code)
    # Imported here: the serializer module imports this one for `CourtesyStatus`.
    from business_app.serializers.price_courtesy_serializers import serialize_courtesy_status

    return {
        "courtesy_status": serialize_courtesy_status(courtesy_status(user_id, at=get_utc_now())),
        "allowed_actions": {"grant": _grant_action(user_id, can_grant=can_grant)},
    }
