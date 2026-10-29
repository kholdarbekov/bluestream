"""What a customer may do with an order, published as finished answers (spec 2026-10-27 §4.1, §4.2, §4.4, §5).

Every customer order payload (`serialize_order`: the list, the detail and POST /orders) publishes
`customer_actions(order)`. Its `pay` is the ONE Pay rule (`payment_projection.pay_refusal`) worded in the customer's
language: the button's label, the amount the Click link will charge, and the "Pay by" sentence. Its `cancel` is the
ONE Cancel rule (`OrderService.customer_cancel_block_code` and `customer_cancel_via`): skip, cancel or decline, with
the button's label and the confirm's sentence. Its `confirm` is the store's Confirm of an order its sales agent placed.
The bot, the website and the subscription page render these and never work them out again, and
`POST /orders/<id>/retry-payment` and `POST /orders/<id>/cancel` enforce the same functions, so the button a customer
is shown and the write it sends cannot disagree.
"""

import copy
import logging
from datetime import datetime
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Tuple

from business_app.utils import display_labels
from business_app.utils.local_windows import local_date
from business_app.utils.payment_projection import PAY_REFUSAL_ALREADY_PAID, PAY_REFUSAL_ORDER_CLOSED, pay_refusal
from business_app.utils.timezone_utils import get_utc_now
from business_app.utils.translations import get_translation

logger = logging.getLogger(__name__)

ACTIONS = "api.orders.actions"
PAY = f"{ACTIONS}.pay"

# A Pay the customer may not use: nothing to show, nothing to send.
_CLOSED_PAY: Dict[str, Any] = {
    "allowed": False,
    "refusal": None,
    "label": None,
    "expected_amount": None,
    "amount_label": None,
    "deadline_label": None,
}

# A removal the customer may not use. `cancel_action` fills in the refusal and the subscription it publishes.
_CLOSED_CANCEL: Dict[str, Any] = {
    "allowed": False,
    "refusal": None,
    "via": None,
    "label": None,
    "confirm_text": None,
    "subscription_id": None,
    "reasons": [],
}

_CLOSED_CONFIRM: Dict[str, Any] = {"allowed": False, "label": None}

# What a payload publishes when it cannot work the real answer out (`serialize_order`'s fallback): every action
# closed. Callers publish a copy, never this dict itself.
CLOSED_CUSTOMER_ACTIONS: Dict[str, Dict] = {"pay": _CLOSED_PAY, "cancel": _CLOSED_CANCEL, "confirm": _CLOSED_CONFIRM}

# The refusals the retry-payment route has always answered in these words.
_REFUSAL_KEYS = {
    PAY_REFUSAL_ALREADY_PAID: "api.payments.error.already_paid",
    PAY_REFUSAL_ORDER_CLOSED: "api.payments.error.lifecycle.advice_order_already_cancelled",
}


def money_value(amount) -> str:
    """An amount as a write echoes it back in `expected_amount`: two decimals, no grouping (`"30000.00"`)."""
    return str(Decimal(str(amount or 0)).quantize(Decimal("0.01")))


def pay_answer(order, *, now: Optional[datetime] = None) -> Tuple[Optional[str], Optional[datetime]]:
    """The Pay rule's two facts: why the customer may not pay (`pay_refusal`, None when they may), and when the
    Pay they may use closes (`OrderService.payment_deadline`), None when it is refused or has no deadline
    (`OrderService.payment_deadline_applies`). `pay_action` words them; the subscription page's next-delivery card
    reads them too, so "payable, and until when" is decided here once."""
    from business_app.services.order_service import OrderService

    refusal = pay_refusal(order, now=now or get_utc_now())
    if refusal is not None or not OrderService.payment_deadline_applies(order):
        return refusal, None
    return None, OrderService.payment_deadline(order)


def pay_action(order, *, language: Optional[str] = None, now: Optional[datetime] = None) -> Dict[str, Any]:
    """`customer_actions.pay`: `{allowed, refusal, label, expected_amount, amount_label, deadline_label}`.

    `label` ("Pay 30 000 so'm"), `expected_amount` (what the write sends back) and `amount_label` (the total the
    Click link will charge) are None unless Pay is allowed. `deadline_label` ("Pay by 13:00 tomorrow.") is set only
    when Pay is allowed and has a deadline, counted in local days."""
    now = now or get_utc_now()
    refusal, closes_at = pay_answer(order, now=now)
    if refusal is not None:
        return {**_CLOSED_PAY, "refusal": refusal}
    amount_label = display_labels.money(order.total_amount, language)
    deadline_label = None
    if closes_at is not None:
        deadline = display_labels.deadline(closes_at, local_date(now), language)
        deadline_label = get_translation(f"{PAY}.deadline", language, deadline=deadline)
    return {
        "allowed": True,
        "refusal": None,
        "label": get_translation(f"{PAY}.label", language, amount=amount_label),
        "expected_amount": money_value(order.total_amount),
        "amount_label": amount_label,
        "deadline_label": deadline_label,
    }


def cancel_action(order, *, language: Optional[str] = None) -> Dict[str, Any]:
    """`customer_actions.cancel`: `{allowed, refusal, via, label, confirm_text, subscription_id, reasons}`.

    `via` is `OrderService.customer_cancel_via` ("skip", "cancel" or "decline"), which POST /orders/<id>/cancel
    expects back as `expected_via`; `refusal` is `customer_cancel_block_code` when the customer may not remove the
    order. `label` and `confirm_text` are None unless allowed. A skip's sentence names the delivery it skips and the
    next one (`SubscriptionService.next_delivery_after_skip`), and only a skip carries its `reasons` chips.
    `subscription_id` is the order's subscription whether or not the removal is allowed."""
    from business_app.services.order_service import CUSTOMER_CANCEL_VIA_SKIP, OrderService
    from business_app.services.subscription_display import skip_reasons
    from business_app.services.subscription_service import SubscriptionService

    via = OrderService.customer_cancel_via(order)
    if via is None:
        return {
            **_CLOSED_CANCEL,
            "refusal": OrderService.customer_cancel_block_code(order),
            "subscription_id": order.subscription_id,
            "reasons": [],
        }
    key = f"{ACTIONS}.{via}"
    reasons: List[Dict[str, str]] = []
    if via == CUSTOMER_CANCEL_VIA_SKIP:
        skipped = display_labels.date_long(order.delivery_date, language)
        next_day = SubscriptionService.next_delivery_after_skip(order.subscription, order)
        confirm_text = (
            get_translation(
                f"{key}.confirm_text", language, date=skipped, next=display_labels.date_long(next_day, language)
            )
            if next_day is not None
            else get_translation(f"{key}.confirm_text_no_next", language, date=skipped)
        )
        reasons = skip_reasons(language)
    else:
        confirm_text = get_translation(f"{key}.confirm_text", language)
    return {
        "allowed": True,
        "refusal": None,
        "via": via,
        "label": get_translation(f"{key}.label", language),
        "confirm_text": confirm_text,
        "subscription_id": order.subscription_id,
        "reasons": reasons,
    }


def confirm_action(order, *, language: Optional[str] = None) -> Dict[str, Any]:
    """`customer_actions.confirm`: `{allowed, label}`, the store's Confirm of an order its sales agent placed, allowed
    while that question is still open (`AgentOrderConfirmationService.pending_request`: unanswered, and the order
    neither cancelled nor delivered), through the route the bot's Confirm uses (POST /orders/<id>/agent-confirmation),
    which enforces the same function."""
    from business_app.services.sales.agent_order_confirmation_service import AgentOrderConfirmationService

    if AgentOrderConfirmationService.pending_request(order) is None:
        return dict(_CLOSED_CONFIRM)
    return {"allowed": True, "label": get_translation(f"{ACTIONS}.confirm.label", language)}


def customer_actions(
    order, *, language: Optional[str] = None, now: Optional[datetime] = None
) -> Dict[str, Dict[str, Any]]:
    """Every action a customer order payload publishes, by its one rule.

    Each action is worked out on its own: one whose rule raises (a state it does not foresee) is logged and published
    closed, and the other actions and the rest of the payload still answer. One action's fault never closes Pay with
    it, as `serialize_order`'s whole-payload fallback would."""
    return {
        "pay": _answer("pay", pay_action, order, language=language, now=now),
        "cancel": _answer("cancel", cancel_action, order, language=language),
        "confirm": _answer("confirm", confirm_action, order, language=language),
    }


def _answer(name: str, action: Callable[..., Dict[str, Any]], order, **kwargs) -> Dict[str, Any]:
    """`action(order, **kwargs)`, or a copy of `CLOSED_CUSTOMER_ACTIONS[name]` when it raises."""
    try:
        return action(order, **kwargs)
    except Exception:
        logger.exception("customer_actions.%s could not be worked out for order %s; published closed", name, order.id)
        return copy.deepcopy(CLOSED_CUSTOMER_ACTIONS[name])


def pay_refusal_message(refusal: str, *, language: Optional[str] = None) -> str:
    """The sentence a refused Pay answers with, in the customer's language."""
    return get_translation(_REFUSAL_KEYS.get(refusal, f"{PAY}.refusal.{refusal}"), language)
