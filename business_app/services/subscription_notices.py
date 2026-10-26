"""What a subscription owes its customer a message about: recorded with the change, sent after commit.

Spec 2026-10-23 §2.3, §2.7, §2.8 and §9 (Messages). Every such event is a `SubscriptionLog` row written
in the transaction that made it happen: its `action` is one of the constants below and its `extra_data`
carries the facts. `deliver(log)` is called only AFTER that transaction has committed, never from inside
it, and never fails the caller: it publishes `send_subscription_notice_task(log.id)`, and the task calls
`send(log_id)`, which renders the notice through `customer_notices` (the customer bot's route, or email
when the customer has no Telegram).

`extra_data` by action:
    ORDER_CREATED     order_id, order_number, delivery_date
    DELIVERY_SKIPPED  skipped_by="system", reason_code (a SKIP_* below), skipped_dates, next_delivery_date,
                      detail (the refusal's own message, or None for a passed date)
    PAID_ORDER_KEPT   order_id, order_number, delivery_date, trigger ("skip" | "pause" | "cancel"),
                      next_delivery_date (None when no delivery follows: paused or cancelled)

The customer's own skip also writes a `delivery_skipped` row (`skipped_by="customer"`). It owes no notice:
the customer asked for it and the response tells them. So a notice is chosen by the action AND, for a skip,
by who skipped.
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)

ORDER_CREATED = "order_created"
DELIVERY_SKIPPED = "delivery_skipped"
PAID_ORDER_KEPT = "paid_order_kept"

# Why the system could not create a scheduled order (§2.8). Each is also a seeded label,
# `api.subscriptions.skip_reason.<code>`, which the notice quotes.
SKIP_DATE_PASSED = "date_passed"
SKIP_OUT_OF_STOCK = "out_of_stock"
SKIP_BELOW_MINIMUM = "below_minimum"
SKIP_CUSTOMER_BARRED = "customer_barred"
SKIP_COD_DEBT_LIMIT = "cod_debt_limit"

# The `OrderService.create_order` error codes that are §2.8 refusals. Only these, a product that no longer
# exists (`refusal_reason`), the customer-barred pre-check and a passed date skip a delivery. Any other failure
# skips nothing, including a ValidationError with no code, which is how `create_order` reports a Redis or
# database outage. The next run then tries again.
REFUSAL_REASONS = {
    "COD_DEBT_LIMIT_REACHED": SKIP_COD_DEBT_LIMIT,
    "ORDER_STOCK_UNAVAILABLE": SKIP_OUT_OF_STOCK,
    "ORDER_PRODUCT_UNAVAILABLE": SKIP_OUT_OF_STOCK,
    "ORDER_MIN_AMOUNT": SKIP_BELOW_MINIMUM,
    "ORDER_ITEM_BELOW_MIN_QUANTITY": SKIP_BELOW_MINIMUM,
}


def refusal_reason(exc: Exception) -> Optional[str]:
    """The §2.8 reason a `create_order` failure for a subscription order is a refusal, or None when it is not
    one (the next run tries again).

    A `NotFoundError` names a product deleted since the subscription was made: the subscription's customer
    is loaded and checked before `create_order` is called, so a missing product is all it can be. It is the
    product being unavailable, the same reason as out of stock."""
    from business_app.utils.exceptions import NotFoundError, ValidationError

    if isinstance(exc, NotFoundError):
        return SKIP_OUT_OF_STOCK
    if isinstance(exc, ValidationError):
        return REFUSAL_REASONS.get(exc.error_code)
    return None


def owes_notice(log) -> bool:
    """Whether this committed row owes the customer a message (see the module docstring)."""
    if log.action in (ORDER_CREATED, PAID_ORDER_KEPT):
        return True
    return log.action == DELIVERY_SKIPPED and (log.extra_data or {}).get("skipped_by") == "system"


def deliver(log) -> None:
    """Hand one committed row on to the notice task. Best effort: a failure is logged, never raised."""
    try:
        if not owes_notice(log):
            return
        from business_app.tasks.subscription_tasks import send_subscription_notice_task

        send_subscription_notice_task.delay(log.id)
    except Exception:
        logger.exception("Subscription notice hand-off failed for log %s", getattr(log, "id", None))


def send(log_id: int) -> str:
    """Send the notice one committed row owes, reading the order as it is now. Returns how it went
    ("telegram", "email", "no_channel", "skipped:<why>"); raises `customer_notices.RetryableNoticeError`
    when the bot failed and a retry may succeed."""
    from business_app import db
    from business_app.models.order import Order
    from business_app.models.subscription import SubscriptionLog
    from business_app.services import customer_notices

    log = db.session.get(SubscriptionLog, log_id)
    if log is None:
        return "skipped:log_missing"
    if not owes_notice(log):
        return "skipped:not_a_notice"
    facts = dict(log.extra_data or {})
    notice_id = f"subscription-log:{log.id}"
    if log.action == DELIVERY_SKIPPED:
        return customer_notices.send_delivery_skipped(log.subscription, facts, notice_id=notice_id)
    order = db.session.get(Order, facts.get("order_id"))
    if order is None:
        return "skipped:order_missing"
    if log.action == ORDER_CREATED:
        return customer_notices.send_order_created(order, notice_id=notice_id)
    return customer_notices.send_paid_order_kept(order, facts, notice_id=notice_id)
