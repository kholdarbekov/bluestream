"""Schedule, edit and cancel a price change; the one stage rule and the row actions (spec 2026-11-16 §5.2).

One change is pending at a time (`uq_price_changes_one_pending`). It is editable until the announcement starts;
afterwards only an ADMIN may cancel; operators read.

The rules below have one expression each: `is_editable` and `cancel_needs_admin` are read by the write guards AND by
the row actions, so a button and the route behind it cannot disagree; `price_change_stage` is the only place the
timeline is read into a word. `REFUSAL_MESSAGES` is the one sentence per code: the exception message and the
action's `block_reason` are the same text.
"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Dict, List, Mapping, Optional

from sqlalchemy.exc import IntegrityError

from business_app import db
from business_app.models.price_change import (
    DEFAULT_COURTESY_DAYS,
    DEFAULT_REMINDER_LEAD_DAYS,
    PENDING_PRICE_CHANGE_STATUSES,
    PriceChange,
    PriceChangeItem,
    PriceChangeNotice,
    PriceChangeNoticeKind,
    PriceChangeStatus,
)
from business_app.models.product import Product
from business_app.models.user import User
from business_app.services.price_change.permissions import (
    PRICE_CHANGE_CANCEL_ANNOUNCED_PERMISSIONS,
    PRICE_CHANGE_MANAGE_PERMISSIONS,
)
from business_app.utils.decorators import has_admin_permission
from business_app.utils.exceptions import ConflictError, ForbiddenError, NotFoundError, ValidationError
from business_app.utils.local_windows import local_date
from business_app.utils.timezone_utils import ensure_utc, get_utc_now
from business_app.utils.transactions import transactional

# Sanity ceiling: keeps courtesy_last_day / courtesy_ends_at far from date overflow and the integer column.
MAX_COURTESY_DAYS = 365

PRICE_CHANGE_STAGES = ("scheduled", "announcing", "announced", "reminded", "applied", "window_closed", "cancelled")

# One English sentence per code. Product-level codes are `str.format` templates filled with the product's name when
# raised; the codes that are also a `block_reason` (ALREADY_PENDING, LOCKED, NOT_CANCELLABLE) stay fixed sentences.
REFUSAL_MESSAGES: Dict[str, str] = {
    "PRICE_CHANGE_ITEMS_REQUIRED": "A price change needs at least one product.",
    "PRICE_CHANGE_DUPLICATE_PRODUCT": "{product} is listed more than once.",
    "PRICE_CHANGE_PRODUCT_NOT_FOUND": "{product} is not a known product.",
    "PRICE_CHANGE_ANNOUNCE_IN_PAST": "The announcement day cannot be in the past.",
    "PRICE_CHANGE_DATES_INVALID": "The price change day must be after the announcement day.",
    "PRICE_CHANGE_REMINDER_INVALID": (
        "The reminder must fall after the announcement day and before the price change day."
    ),
    "PRICE_CHANGE_COURTESY_DAYS_INVALID": f"The courtesy period must be between 1 and {MAX_COURTESY_DAYS} days.",
    "PRICE_CHANGE_NEW_PRICE_INVALID": "The new price of {product} must be above zero.",
    "PRICE_CHANGE_NOT_AN_INCREASE": "The new price of {product} must be above its current price.",
    "PRICE_CHANGE_CAP_BELOW_MIN_ORDER": "The courtesy cap of {product} is below its minimum order quantity.",
    "PRICE_CHANGE_PRODUCT_HAS_DISCOUNT_PRICE": "{product} has a discount price; remove it before scheduling a change.",
    "PRICE_CHANGE_ALREADY_PENDING": "A price change is already scheduled; cancel it or wait until it is applied.",
    "PRICE_CHANGE_LOCKED": "This price change can no longer be edited because its announcement has started.",
    "PRICE_CHANGE_NOT_CANCELLABLE": "This price change is already applied or cancelled.",
    "PRICE_CHANGE_CANCEL_ADMIN_ONLY": "Only an admin can cancel a price change after its announcement has started.",
    "PRICE_CHANGE_NOT_FOUND": "Price change not found.",
}

# The direct price edit of a product held by a pending change (`PUT /admin/products/<id>`): one code, one sentence.
PRODUCT_PRICE_LOCKED_BY_PRICE_CHANGE = "PRODUCT_PRICE_LOCKED_BY_PRICE_CHANGE"
PRICE_LOCKED_MESSAGE = "A price change is scheduled for this product. Change its price on the Price changes page."

_MONEY_STEP = Decimal("0.01")


def is_editable(price_change: PriceChange) -> bool:
    """SCHEDULED and the announcement not started."""
    return price_change.status == PriceChangeStatus.SCHEDULED and price_change.announcement_started_at is None


def cancel_needs_admin(price_change: PriceChange) -> bool:
    """After the announcement starts, only an ADMIN cancels."""
    return price_change.announcement_started_at is not None


def price_change_stage(price_change: PriceChange, *, now: datetime) -> str:
    """The one stage rule: the timeline read into a word."""
    if price_change.status == PriceChangeStatus.CANCELLED:
        return "cancelled"
    if price_change.status == PriceChangeStatus.APPLIED:
        return "window_closed" if ensure_utc(now) >= price_change.courtesy_ends_at else "applied"
    if price_change.reminder_started_at is not None:
        return "reminded"
    if price_change.announcement_started_at is not None:
        queued = (
            PriceChangeNotice.query.filter(
                PriceChangeNotice.price_change_id == price_change.id,
                PriceChangeNotice.kind == PriceChangeNoticeKind.ANNOUNCEMENT,
                PriceChangeNotice.channel.is_(None),
            ).first()
            is not None
        )
        return "announcing" if queued else "announced"
    return "scheduled"


_HIDDEN = {"allowed": False, "block_reason": None}
_ALLOWED = {"allowed": True, "block_reason": None}


def _blocked(code: str) -> Dict[str, Any]:
    return {"allowed": False, "block_reason": REFUSAL_MESSAGES[code]}


def create_action(viewer: User) -> Dict[str, Any]:
    if not has_admin_permission(viewer, PRICE_CHANGE_MANAGE_PERMISSIONS):
        return dict(_HIDDEN)
    if PriceChangeService().pending() is not None:
        return _blocked("PRICE_CHANGE_ALREADY_PENDING")
    return dict(_ALLOWED)


def row_actions(price_change: PriceChange, viewer: User) -> Dict[str, Dict[str, Any]]:
    """`{"edit": ACTION, "cancel": ACTION}`: a missing role hides, a state refusal says why."""
    if not has_admin_permission(viewer, PRICE_CHANGE_MANAGE_PERMISSIONS):
        return {"edit": dict(_HIDDEN), "cancel": dict(_HIDDEN)}
    edit = dict(_ALLOWED) if is_editable(price_change) else _blocked("PRICE_CHANGE_LOCKED")
    if price_change.status not in PENDING_PRICE_CHANGE_STATUSES:
        cancel = _blocked("PRICE_CHANGE_NOT_CANCELLABLE")
    elif cancel_needs_admin(price_change) and not has_admin_permission(
        viewer, PRICE_CHANGE_CANCEL_ANNOUNCED_PERMISSIONS
    ):
        cancel = dict(_HIDDEN)
    else:
        cancel = dict(_ALLOWED)
    return {"edit": edit, "cancel": cancel}


def _refusal(exc_class, code: str, *, product: Optional[str] = None, product_id: Optional[int] = None):
    message = REFUSAL_MESSAGES[code]
    if product is not None:
        message = message.format(product=product)
    details = {"product_id": product_id} if product_id is not None else None
    return exc_class(message, details=details, error_code=code)


def _money(value: Any) -> Decimal:
    return Decimal(str(value)).quantize(_MONEY_STEP, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class UpcomingPrice:
    """The price a product takes on `change_on`; the old price is the list price through `price_valid_until`."""

    product_id: int
    new_price: Decimal
    change_on: date
    price_valid_until: date


def _same_money(current: Any, sent: Any) -> bool:
    """By value: None equals None; a non-number never equals anything (it is a change)."""
    if current is None or sent is None:
        return current is None and sent is None
    try:
        return Decimal(str(current)) == Decimal(str(sent))
    except InvalidOperation:
        return False


def price_fields_changed(product: Product, payload: Mapping[str, Any]) -> bool:
    """True when a PUT /admin/products body changes the base price or the discount price.

    The route reads the base price from "price", else "base_price" (the same precedence it applies on write); an
    echoed unchanged value is no change.
    """
    base_key = "price" if "price" in payload else "base_price" if "base_price" in payload else None
    if base_key is not None and not _same_money(product.base_price, payload[base_key]):
        return True
    return "discount_price" in payload and not _same_money(product.discount_price, payload["discount_price"])


class PriceChangeService:
    @transactional
    def create(self, data: Dict[str, Any], actor_id: int) -> PriceChange:
        items = self._validated_items(data)
        if self.pending() is not None:
            raise _refusal(ValidationError, "PRICE_CHANGE_ALREADY_PENDING")
        price_change = PriceChange(created_by_id=actor_id, status=PriceChangeStatus.SCHEDULED)
        self._fill(price_change, data, items)
        db.session.add(price_change)
        try:
            db.session.flush()
        except IntegrityError as exc:
            if "uq_price_changes_one_pending" not in str(exc):
                raise
            # Two concurrent POSTs: the partial unique index let one through.
            db.session.rollback()
            raise _refusal(ValidationError, "PRICE_CHANGE_ALREADY_PENDING") from exc
        return price_change

    @transactional
    def update(self, price_change_id: int, data: Dict[str, Any], actor_id: int) -> PriceChange:
        """A full replacement, items included. The state is checked before any input."""
        price_change = self._locked(price_change_id)
        if not is_editable(price_change):
            raise _refusal(ConflictError, "PRICE_CHANGE_LOCKED")
        items = self._validated_items(data)
        other = self.pending()
        if other is not None and other.id != price_change.id:
            raise _refusal(ValidationError, "PRICE_CHANGE_ALREADY_PENDING")
        price_change.items.clear()
        db.session.flush()
        self._fill(price_change, data, items)
        db.session.flush()
        return price_change

    @transactional
    def cancel(self, price_change_id: int, actor_id: int, actor_is_admin: bool) -> PriceChange:
        price_change = self._locked(price_change_id)
        if price_change.status not in PENDING_PRICE_CHANGE_STATUSES:
            raise _refusal(ConflictError, "PRICE_CHANGE_NOT_CANCELLABLE")
        if cancel_needs_admin(price_change) and not actor_is_admin:
            raise _refusal(ForbiddenError, "PRICE_CHANGE_CANCEL_ADMIN_ONLY")
        price_change.status = PriceChangeStatus.CANCELLED
        price_change.cancelled_at = get_utc_now()
        price_change.cancelled_by_id = actor_id
        db.session.flush()
        return price_change

    def get(self, price_change_id: int) -> PriceChange:
        price_change = db.session.get(PriceChange, price_change_id)
        if price_change is None:
            raise _refusal(NotFoundError, "PRICE_CHANGE_NOT_FOUND")
        return price_change

    def list_all(self) -> List[PriceChange]:
        return PriceChange.query.order_by(PriceChange.created_at.desc(), PriceChange.id.desc()).all()

    def pending(self) -> Optional[PriceChange]:
        return PriceChange.query.filter(PriceChange.status.in_(PENDING_PRICE_CHANGE_STATUSES)).first()

    def pending_for_product(self, product_id: int) -> Optional[PriceChange]:
        """The pending change holding the product, if any."""
        return (
            PriceChange.query.join(PriceChangeItem, PriceChangeItem.price_change_id == PriceChange.id)
            .filter(PriceChange.status.in_(PENDING_PRICE_CHANGE_STATUSES), PriceChangeItem.product_id == product_id)
            .first()
        )

    def blocks_direct_price_edit(self, product_id: int) -> bool:
        return self.pending_for_product(product_id) is not None

    def upcoming_price(self, product_id: int) -> Optional[UpcomingPrice]:
        """The coming price, only once the announcement has started and before the switch applies it."""
        price_change = self.pending_for_product(product_id)
        if price_change is None or price_change.status != PriceChangeStatus.ANNOUNCED:
            return None
        item = next(i for i in price_change.items if i.product_id == product_id)
        return UpcomingPrice(
            product_id, Decimal(item.new_price), price_change.change_on, price_change.price_valid_until
        )

    @staticmethod
    def _locked(price_change_id: int) -> PriceChange:
        price_change = (
            PriceChange.query.filter(PriceChange.id == price_change_id).with_for_update().populate_existing().first()
        )
        if price_change is None:
            raise _refusal(NotFoundError, "PRICE_CHANGE_NOT_FOUND")
        return price_change

    @staticmethod
    def _validated_items(data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Every input rule, in table order. Returns the items as `{product, new_price, courtesy_cap}`."""
        raw_items = data.get("items") or []
        if not raw_items:
            raise _refusal(ValidationError, "PRICE_CHANGE_ITEMS_REQUIRED")
        product_ids = [raw["product_id"] for raw in raw_items]
        products = {p.id: p for p in Product.query.filter(Product.id.in_(set(product_ids))).all()}
        seen = set()
        for product_id in product_ids:
            product = products.get(product_id)
            name = product.name if product else f"#{product_id}"
            if product_id in seen:
                raise _refusal(ValidationError, "PRICE_CHANGE_DUPLICATE_PRODUCT", product=name, product_id=product_id)
            seen.add(product_id)
        for product_id in product_ids:
            if product_id not in products:
                raise _refusal(
                    NotFoundError, "PRICE_CHANGE_PRODUCT_NOT_FOUND", product=f"#{product_id}", product_id=product_id
                )

        announce_on: date = data["announce_on"]
        change_on: date = data["change_on"]
        lead = data.get("reminder_lead_days", DEFAULT_REMINDER_LEAD_DAYS)
        if announce_on < local_date():
            raise _refusal(ValidationError, "PRICE_CHANGE_ANNOUNCE_IN_PAST")
        if change_on <= announce_on:
            raise _refusal(ValidationError, "PRICE_CHANGE_DATES_INVALID")
        # Bound the lead by the announce-to-change gap BEFORE building a timedelta (a huge lead overflows it).
        if not 1 <= lead < (change_on - announce_on).days:
            raise _refusal(ValidationError, "PRICE_CHANGE_REMINDER_INVALID")
        if not 1 <= data.get("courtesy_days", DEFAULT_COURTESY_DAYS) <= MAX_COURTESY_DAYS:
            raise _refusal(ValidationError, "PRICE_CHANGE_COURTESY_DAYS_INVALID")

        validated = []
        for raw in raw_items:
            product = products[raw["product_id"]]
            new_price = _money(raw["new_price"])
            kwargs = {"product": product.name, "product_id": product.id}
            if new_price <= 0:
                raise _refusal(ValidationError, "PRICE_CHANGE_NEW_PRICE_INVALID", **kwargs)
            if new_price <= product.catalogue_unit_price:
                raise _refusal(ValidationError, "PRICE_CHANGE_NOT_AN_INCREASE", **kwargs)
            if raw["courtesy_cap"] < int(product.min_order_quantity or 1):
                raise _refusal(ValidationError, "PRICE_CHANGE_CAP_BELOW_MIN_ORDER", **kwargs)
            if product.charges_discount_price:
                raise _refusal(ValidationError, "PRICE_CHANGE_PRODUCT_HAS_DISCOUNT_PRICE", **kwargs)
            validated.append({"product": product, "new_price": new_price, "courtesy_cap": raw["courtesy_cap"]})
        return validated

    @staticmethod
    def _fill(price_change: PriceChange, data: Dict[str, Any], items: List[Dict[str, Any]]) -> None:
        # PUT and POST are full replacements: an absent length takes its default (the payload drops unset fields).
        price_change.announce_on = data["announce_on"]
        price_change.change_on = data["change_on"]
        price_change.reminder_lead_days = data.get("reminder_lead_days", DEFAULT_REMINDER_LEAD_DAYS)
        price_change.courtesy_days = data.get("courtesy_days", DEFAULT_COURTESY_DAYS)
        for item in items:
            price_change.items.append(
                PriceChangeItem(
                    product_id=item["product"].id, new_price=item["new_price"], courtesy_cap=item["courtesy_cap"]
                )
            )
