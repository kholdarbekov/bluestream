"""The switch: a due price change writes its new prices (spec 2026-11-16 §5.5).

A change is due once its change instant (`effective_at`, 00:00 Tashkent on `change_on`) has passed. Only an ANNOUNCED
change is applied: one still SCHEDULED at its instant was never announced and is held back, because customers must
never pay a price nobody told them about (owner decision 2); the tick's alert reports it.

Each change is applied in ONE transaction: every item's `old_price` is stamped from the product row and the product
takes the item's `new_price`, the change is APPLIED at the run's instant, the courtesy list is built and the live
subscription plans are refreshed. `build_courtesy_list` and `reprice_items_for_products` flush and never commit, so a
failure anywhere rolls the whole change back and it stays due for the next run. A product with a charged discount
price (`Product.charges_discount_price`) holds the whole change back the same way: the new base price would not move
what that product costs, and only cancelling the change ends the hold.

After each commit one audit row records the change (`audit_logger` commits in its own session, so it is never written
inside the switch's transaction). After the run, when anything applied, the cached featured and popular product lists
are cleared. The `base_price` write itself requests the IndexNow submission (`utils/indexnow_dispatch.py`).

Idempotent: a second run finds the change APPLIED and does nothing.
"""

import logging
from datetime import datetime
from decimal import Decimal
from typing import Dict, List, NamedTuple, Optional, Tuple

from flask import current_app

from business_app import db
from business_app.models.price_change import PriceChange, PriceChangeStatus
from business_app.models.product import Product
from business_app.services.price_change.courtesy import build_courtesy_list
from business_app.utils.audit_logger import AuditEventType, AuditSeverity, audit_logger
from business_app.utils.decorators import invalidate_cache
from business_app.utils.exceptions import ValidationError
from business_app.utils.service_factory import get_subscription_service
from business_app.utils.timezone_utils import ensure_utc, get_utc_now
from business_app.utils.transactions import atomic_transaction

logger = logging.getLogger(__name__)


class _Applied(NamedTuple):
    price_change_id: int
    applied_at: datetime
    prices: Dict[int, Tuple[Decimal, Decimal]]  # product id -> (old price, new price)
    courtesy_entries: int
    subscriptions_refreshed: int


def apply_due_price_changes(now: Optional[datetime] = None) -> List[int]:
    """Apply every ANNOUNCED change whose change instant is at or before `now` (default: the current instant), each in
    its own transaction. Returns the ids this call applied; a change that fails is logged, rolled back and skipped."""
    now = ensure_utc(now) if now is not None else get_utc_now()
    announced = PriceChange.query.filter(PriceChange.status == PriceChangeStatus.ANNOUNCED).order_by(PriceChange.id)
    due = [price_change.id for price_change in announced if price_change.effective_at <= now]

    applied_ids = []
    for price_change_id in due:
        try:
            applied = _apply_one(price_change_id, now)
        except Exception:  # noqa: BLE001 - one change's failure never stops the next; it stays due
            logger.exception("price_change_apply_failed price_change=%s", price_change_id)
            continue
        if applied is None:
            continue
        _audit(applied)
        applied_ids.append(applied.price_change_id)

    if applied_ids:
        _clear_cached_product_responses()
    return applied_ids


def _apply_one(price_change_id: int, now: datetime) -> Optional[_Applied]:
    """Apply one change in one transaction, or None when another run got there first or it is no longer due.

    Raises ValidationError (PRICE_CHANGE_PRODUCT_DISCOUNTED) for a product with a charged discount price; any raise
    rolls the whole change back."""
    with atomic_transaction():
        price_change = (
            PriceChange.query.filter(PriceChange.id == price_change_id).with_for_update().populate_existing().first()
        )
        if (
            price_change is None
            or price_change.status != PriceChangeStatus.ANNOUNCED
            or price_change.effective_at > now
        ):
            return None

        items = sorted(price_change.items, key=lambda item: item.product_id)
        product_ids = [item.product_id for item in items]
        products = {
            product.id: product
            for product in Product.query.filter(Product.id.in_(product_ids))
            .order_by(Product.id)
            .with_for_update()
            .populate_existing()
        }
        prices = {}
        for item in items:
            product = products[item.product_id]
            # Scheduling's PRICE_CHANGE_PRODUCT_HAS_DISCOUNT_PRICE reads the same rule. A pending change locks the
            # product's discount edits, so cancelling the change is the only way out.
            if product.charges_discount_price:
                raise ValidationError(
                    f"{product.name} has a discount price, so this price change cannot be applied. "
                    "An admin must cancel the price change.",
                    error_code="PRICE_CHANGE_PRODUCT_DISCOUNTED",
                )
            item.old_price = product.base_price
            product.base_price = item.new_price
            prices[product.id] = (item.old_price, item.new_price)
        price_change.applied_at = now
        price_change.status = PriceChangeStatus.APPLIED
        db.session.flush()

        courtesy_entries = build_courtesy_list(price_change)
        subscriptions_refreshed = get_subscription_service().reprice_items_for_products(product_ids)
        return _Applied(price_change_id, now, prices, courtesy_entries, subscriptions_refreshed)


def _audit(applied: _Applied) -> None:
    """The change's audit row, written after its commit."""
    audit_logger.log_event(
        event_type=AuditEventType.PRODUCT_UPDATED,
        action="price_change_applied",
        severity=AuditSeverity.HIGH,
        resource_type="price_change",
        resource_id=str(applied.price_change_id),
        old_values={"prices": {str(product_id): str(old) for product_id, (old, _new) in applied.prices.items()}},
        new_values={"prices": {str(product_id): str(new) for product_id, (_old, new) in applied.prices.items()}},
        additional_data={
            "applied_at": applied.applied_at.isoformat(),
            "courtesy_entries": applied.courtesy_entries,
            "subscriptions_refreshed": applied.subscriptions_refreshed,
        },
    )


def _clear_cached_product_responses() -> None:
    """The cached featured and popular product lists, in every language and query, so neither serves the old price."""
    prefix = current_app.config["API_PREFIX"]
    invalidate_cache(f"response:*:{prefix}/products/featured*")
    invalidate_cache(f"response:*:{prefix}/products/popular*")
