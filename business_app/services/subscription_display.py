"""What a subscription payload shows, as labels (spec 2026-10-26 §5 B2, B3, B5, B6, B11, §12).

`SubscriptionService` decides the facts (which order the next-delivery card shows and in which state, the
waterline's dates, the estimate, the history rows); this module only words them, in the request's
language, through `business_app.utils.display_labels` and the page-unique `api.subscriptions.page.*` copy.
The website renders these labels as they are: it does no status logic, date maths or money maths.

Status chips: one palette (UX brief "STATUS CHIPS"), published as `{code, label, tone}`. The `code` is
the machine answer a client may branch on; the `label` is the text; the `tone` is one of success,
warning, info, neutral, danger.
"""

from datetime import date
from typing import Any, Dict, List, Optional, Sequence, Tuple

from business_app.services import subscription_schedule
from business_app.services.customer_order_actions import pay_answer
from business_app.utils import display_labels
from business_app.utils.address_helpers import get_address_label, get_address_line
from business_app.utils.payment_projection import (
    PAY_REFUSAL_ALREADY_PAID,
    PAY_REFUSAL_DEADLINE_PASSED,
    PAY_REFUSAL_PARTLY_PAID,
    pay_refusal,
)
from business_app.utils.translations import get_translation
from shared.enums import OrderStatus, PaymentMethod, PaymentStatus, SubscriptionFrequency
from shared.i18n_rendering import plural_key

PAGE = "api.subscriptions.page"
ACTIONS = "api.subscriptions.actions"
# The reasons a customer may give for skipping a delivery, in the order the chips are shown (spec 2026-10-26 §12):
# what `SkipReasonFields.reason_code` accepts and `skip_reasons` words.
SKIP_REASON_CODES = ("still_have_water", "away", "other")
# The kinds of delivery time (spec 2026-10-27 §6.3), published as `time_kind` beside the `time_label` that words them.
TIME_WINDOW = "window"  # a window the delivery will keep
TIME_ANY = "any_time"  # no slot was chosen
TIME_TBC = "to_be_confirmed"  # a slot is set but switched off, or not offered on that day
TIME = "api.subscriptions.time"

# The one chip palette (UX brief tokens): code -> tone.
_TONES = {
    "paid": "success",
    "delivered": "success",
    "awaiting_payment": "warning",
    "awaiting_new_date": "warning",
    "placed": "info",
    "on_the_way": "info",
    "pay_on_delivery": "info",
    "bank_transfer": "info",
    "cancelled": "neutral",
    "returned": "neutral",
    "skipped": "neutral",
    "payment_failed": "danger",
}


def _page(key: str, **values) -> str:
    return get_translation(f"{PAGE}.{key}", **values)


def action_text(key: str, **values) -> str:
    """A published action's sentence, `api.subscriptions.actions.<key>`, in the request's language."""
    return get_translation(f"{ACTIONS}.{key}", **values)


def chip(code: str) -> Dict[str, str]:
    return {"code": code, "label": _page(f"status.{code}"), "tone": _TONES[code]}


def skip_reasons(language: Optional[str] = None) -> List[Dict[str, str]]:
    """The skip reason chips, `[{code, label}]`, in `language`: every surface shows these words and sends the code
    back as `reason_code` (spec 2026-10-27 §5.2)."""
    return [
        {"code": code, "label": get_translation(f"api.subscriptions.skip_reason.{code}", language)}
        for code in SKIP_REASON_CODES
    ]


def date_item(day: date, state: str) -> Dict[str, Any]:
    """One waterline bottle: `{date, label_short, label_long, state}`."""
    return {
        "date": day.isoformat(),
        "label_short": display_labels.date_short(day),
        "label_long": display_labels.date_long(day),
        "state": state,
    }


def _weekday_when_weekly(subscription) -> Optional[int]:
    frequency = subscription.frequency
    frequency = frequency if isinstance(frequency, SubscriptionFrequency) else SubscriptionFrequency(frequency)
    if frequency in (SubscriptionFrequency.WEEKLY, SubscriptionFrequency.BIWEEKLY):
        return subscription.delivery_day_of_week
    return None


def time_kind(subject, *, day: Optional[date] = None) -> str:
    """The ONE answer to "when in the day": `TIME_WINDOW`, `TIME_ANY` or `TIME_TBC` (spec 2026-10-27 §6.3).

    An Order has a window when both ends are set; without one it is TBC when its subscription has a slot (switched
    off, or not offered that day) and Any time otherwise. A subscription-like subject is Any time without a slot;
    with one, the window it gives on `day`, or, with no `day`, whether the slot is offered for its schedule (a
    weekly day is the one it is on)."""
    from business_app.models.order import Order

    if isinstance(subject, Order):
        if subject.delivery_window_start is not None and subject.delivery_window_end is not None:
            return TIME_WINDOW
        subscription = subject.subscription
        return TIME_TBC if subscription is not None and subscription.delivery_time_slot is not None else TIME_ANY
    slot = subject.delivery_time_slot
    if slot is None:
        return TIME_ANY
    if day is not None:
        return TIME_WINDOW if subscription_schedule.window_for(slot, day) else TIME_TBC
    offered = subscription_schedule.slot_offered_for(slot, subject.frequency, _weekday_when_weekly(subject))
    return TIME_WINDOW if offered else TIME_TBC


def time_words(kind: str, window_label: Optional[str], *, language: Optional[str] = None, inline: bool = False) -> str:
    """A time kind in words: the window itself, "Any time" or "Time to be confirmed"; `inline` is the lower-case
    form that follows a comma ("Every Thursday, any time"). Every surface says these words from here."""
    if kind == TIME_WINDOW and window_label:
        return window_label
    name = "any_time" if kind == TIME_ANY else "to_be_confirmed"
    return get_translation(f"{TIME}.{name}{'_inline' if inline else ''}", language)


def time_label(subject, *, day: Optional[date] = None, language: Optional[str] = None, inline: bool = False) -> str:
    """The delivery time of an order, or of a subscription (on `day`), as its customer reads it. Never None."""
    kind = time_kind(subject, day=day)
    window_label = None
    if kind == TIME_WINDOW:
        if hasattr(subject, "delivery_window_start"):
            window_label = display_labels.window(subject.delivery_window_start, subject.delivery_window_end)
        else:
            window = (
                subscription_schedule.window_for(subject.delivery_time_slot, day)
                if day is not None
                else subscription_schedule.slot_window(subject.delivery_time_slot)
            )
            window_label = display_labels.window(*window) if window else None
    return time_words(kind, window_label, language=language, inline=inline)


def order_labels(order) -> Dict[str, Optional[str]]:
    """The labels an order reference carries: "30 October", "Thursday, 30 October", "10:00–13:00", and its time
    (`time_kind`, `time_label`: the window, "Any time" or "Time to be confirmed")."""
    day = order.delivery_date
    return {
        "date_label": display_labels.day_month(day) if day else None,
        "label_long": display_labels.date_long(day) if day else None,
        "window_label": display_labels.window(order.delivery_window_start, order.delivery_window_end),
        "time_kind": time_kind(order),
        "time_label": time_label(order),
    }


def date_with_window(day: date, window) -> str:
    """A date and its window ("Thursday, 6 November, 10:00–13:00"), or the date alone when the time is still to
    be confirmed."""
    label = display_labels.date_long(day)
    window_label = display_labels.window(*window) if window else None
    return _page("date_with_window", date=label, window=window_label) if window_label else label


def action_label(action: str) -> str:
    """The button's words for a subscription action ("Skip this delivery", "Pause", "Resume deliveries",
    "Cancel subscription"). The Pay button's words are the order's own (`pay_action`)."""
    return action_text(f"{action}.label")


def kept_line(order, code: str) -> str:
    """The sentence for an order a pause or cancel keeps, by the customer's own cancel rule's answer `code`
    (`OrderService.customer_cancel_block_code`): paid, with the driver, waiting for a new date, or kept as it is."""
    from business_app.services.order_service import (
        CUSTOMER_CANCEL_AWAITING_NEW_DATE,
        CUSTOMER_CANCEL_PAID,
        CUSTOMER_CANCEL_WITH_DRIVER,
    )

    when = date_with_window(order.delivery_date, (order.delivery_window_start, order.delivery_window_end))
    if code == CUSTOMER_CANCEL_PAID:
        return action_text("kept.paid", date=when)
    if code == CUSTOMER_CANCEL_WITH_DRIVER:
        return action_text("kept.with_driver", date=when)
    if code == CUSTOMER_CANCEL_AWAITING_NEW_DATE:
        return action_text("kept.awaiting_new_date", number=order.order_number)
    return action_text("kept.other", number=order.order_number)


def skip_confirm(plan, shown) -> Tuple[str, str]:
    """The Skip's confirm sentence and its keep button, `(confirm_text, keep_label)`, for what the skip does
    (`SubscriptionService._skip_plan`) with `shown`, the order the page shows coming (or None). The first match wins:
    an unpaid order it cancels, a paid order it keeps, an order the driver already has, otherwise a plain skip."""
    from business_app.services.order_service import OrderService

    day = display_labels.day_month(plan.date)
    if plan.cancel:
        text = action_text("skip.confirm_unpaid", date=display_labels.date_long(plan.date), next=_next_label(plan))
        return text, action_text("skip.keep")
    if plan.kept:
        kept_day = display_labels.day_month(plan.kept[0][0].delivery_date)
        return action_text("skip.confirm_paid", kept=kept_day, date=day), action_text("skip.keep_both")
    if shown is not None and OrderService.order_is_with_driver(shown):
        text = action_text("skip.confirm_on_the_way", kept=display_labels.date_long(shown.delivery_date), date=day)
        return text, action_text("skip.keep_both")
    text = action_text("skip.confirm_plain", date=display_labels.date_long(plan.date), next=_next_label(plan))
    return text, action_text("skip.keep")


def _next_label(plan) -> Optional[str]:
    return display_labels.date_long(plan.next_after) if plan.next_after else None


def address_label(address) -> str:
    """An address as the customer names it: its title ("Office"), else its street line."""
    return get_address_label(address) or get_address_line(address)


def address_consequences(plan, method) -> List[str]:
    """An address change's confirm (spec 2026-10-27 §7, §17 Q6), one sentence each: where the deliveries go from now
    on; then, soonest first, each order already placed that moves there too or still goes to its own address (the
    driver has it, or the move is refused); and, when `method` (the subscription's) is not offered at the new address,
    that the customer must choose one. `plan` is `SubscriptionService._address_change_plan`'s answer."""
    lines = [action_text("address.goes_to", address=address_label(plan.address))]
    stays = {order.id for order, _code in plan.stays}
    named = [*plan.moves, *(order for order, _code in plan.stays)]
    for order in sorted(named, key=lambda order: (order.delivery_date, order.id)):
        when = display_labels.date_long(order.delivery_date)
        if order.id in stays:
            lines.append(action_text("address.order_stays", date=when, address=address_label(order.delivery_address)))
        else:
            lines.append(action_text("address.order_moves", date=when))
    if not plan.payment_method_offered:
        lines.append(action_text("address.choose_method", method=display_labels.payment_method(method)))
    return lines


def settle_lines(plan: Sequence[Tuple[Any, Optional[str]]]) -> Tuple[List[str], List[str]]:
    """A pause's or cancel's `(kept_lines, cancel_lines)`: one `kept_line` per order it keeps, one "We'll cancel
    your order for …" per order it cancels (`SubscriptionService._settle_plan`: code None is cancelled)."""
    kept = [kept_line(order, code) for order, code in plan if code is not None]
    cancelled = [
        action_text("will_cancel", date=display_labels.day_month(order.delivery_date))
        for order, code in plan
        if code is None
    ]
    return kept, cancelled


def _payment_failed(order) -> bool:
    payment = getattr(order, "payment", None)
    return payment is not None and payment.status == PaymentStatus.FAILED


def payment_status_code(order) -> Optional[str]:
    """The order's payment chip, by the ONE pay rule (`payment_projection.pay_refusal`, which the
    retry-payment route enforces): paid; payable online (waiting, or the last attempt failed); partly paid,
    so the rest is paid at the door; none once the time to pay online has ended; otherwise by the rail (cash
    at the door, bank transfer)."""
    return _payment_status_code(order, pay_refusal(order))


def _payment_status_code(order, refusal: Optional[str]) -> Optional[str]:
    if refusal == PAY_REFUSAL_ALREADY_PAID:
        return "paid"
    if refusal is None:
        return "payment_failed" if _payment_failed(order) else "awaiting_payment"
    if refusal == PAY_REFUSAL_PARTLY_PAID:
        return "pay_on_delivery"
    if refusal == PAY_REFUSAL_DEADLINE_PASSED:
        return None
    method = getattr(order.payment_method, "value", order.payment_method)
    if method == PaymentMethod.CASH.value:
        return "pay_on_delivery"
    if method == PaymentMethod.BUSINESS_ACCOUNT.value:
        return "bank_transfer"
    return None


def payment_note(code: Optional[str], deadline_label: Optional[str]) -> Optional[str]:
    """The line under the order: when to pay (or that it can be paid whenever, when Pay has no deadline), or
    that it is paid, or how it is paid."""
    if code in ("awaiting_payment", "payment_failed"):
        if deadline_label is None:
            key = "pay_online" if code == "awaiting_payment" else "payment_failed_retry"
            return _page(f"payment_note.{key}")
        key = "pay_by" if code == "awaiting_payment" else "payment_failed"
        return _page(f"payment_note.{key}", deadline=deadline_label)
    if code in ("paid", "pay_on_delivery", "bank_transfer"):
        return _page(f"payment_note.{code}")
    return None


def history_chip(order) -> Dict[str, str]:
    """An order row's chip in "Orders from this subscription": its outcome once it is over, else where it
    is (waiting for a new date, with the driver), else its payment."""
    from business_app.services.order_schedule_service import OrderScheduleService
    from business_app.services.order_service import OrderService

    finished = {
        OrderStatus.DELIVERED: "delivered",
        OrderStatus.CANCELLED: "cancelled",
        OrderStatus.RETURNED: "returned",
    }
    if order.status in finished:
        return chip(finished[order.status])
    if OrderScheduleService.awaiting_new_date(order):
        return chip("awaiting_new_date")
    if OrderService.order_is_with_driver(order):
        return chip("on_the_way")
    code = payment_status_code(order)
    return chip(code if code in ("paid", "awaiting_payment", "payment_failed") else "placed")


def pay_note(order, today: date) -> Optional[str]:
    """The sentence under a Pay: for an order already delivered, which one it is; otherwise the same line the
    next-delivery card publishes as `payment_note` (when to pay, or how it is paid)."""
    if order.status == OrderStatus.DELIVERED and order.delivery_date is not None:
        return action_text("pay.delivered_note", date=display_labels.day_month(order.delivery_date))
    return _payment_summary(order, today)["payment_note"]


def _payment_summary(order, today: date) -> Dict[str, Any]:
    """The published Pay of an order as the next-delivery card words it: its chip, whether it can be paid, until
    when, and the note."""
    refusal, closes_at = pay_answer(order)
    code = _payment_status_code(order, refusal)
    deadline_label = display_labels.deadline(closes_at, today) if closes_at is not None else None
    return {
        "payment_status": chip(code) if code else None,
        "can_pay": refusal is None,
        "pay_deadline_label": deadline_label,
        "payment_note": payment_note(code, deadline_label),
    }


def upcoming_delivery(
    subscription, shown, state: Optional[str], today: date, kept_line: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """The next-delivery card (spec §5 B3, §12). `shown` is the order the card shows (or None: then the
    subscription's own next delivery, still to be placed), `state` the service's answer for it, `kept_line` the
    sentence a paused or cancelled subscription's kept order carries (None while ACTIVE)."""
    if shown is not None:
        day = shown.delivery_date
        window = (shown.delivery_window_start, shown.delivery_window_end)
    else:
        day = subscription.next_delivery_date
        window = subscription_schedule.window_for(subscription.delivery_time_slot, day) or (None, None)
    payload: Dict[str, Any] = {
        "date": day.isoformat(),
        "window_start": window[0].strftime("%H:%M") if window[0] else None,
        "window_end": window[1].strftime("%H:%M") if window[1] else None,
        "order_id": shown.id if shown is not None else None,
        "state": state,
        "label_long": display_labels.date_long(day),
        "label_short": display_labels.date_short(day),
        # `window_label` stays one release for clients that read it; `time_label` is the one words.
        "window_label": display_labels.window(*window),
        "time_kind": time_kind(shown if shown is not None else subscription, day=day),
        "time_label": time_label(shown if shown is not None else subscription, day=day),
        "days_away_label": display_labels.days_away(day, today),
        "order_number": None,
        "amount_label": None,
        "payment_method_label": display_labels.payment_method(
            shown.payment_method if shown is not None else subscription.payment_method
        ),
        "payment_status": None,
        "can_pay": False,
        "pay_deadline_label": None,
        "payment_note": None,
        "kept_line": kept_line,
        "will_place_on_label": None,
    }
    if shown is None:
        payload["will_place_on_label"] = display_labels.date_long(subscription_schedule.order_placed_on(day, today))
        return payload
    # The order's published Pay (`customer_actions.pay`): payable, and until when.
    payload.update(
        order_number=shown.order_number,
        amount_label=display_labels.money(shown.total_amount),
        **_payment_summary(shown, today),
    )
    return payload


def _cadence(subscription) -> str:
    frequency = subscription.frequency
    frequency = frequency if isinstance(frequency, SubscriptionFrequency) else SubscriptionFrequency(frequency)
    if frequency == SubscriptionFrequency.DAILY:
        return _page("cadence.daily")
    if frequency == SubscriptionFrequency.MONTHLY:
        day = subscription.delivery_day_of_month
        return _page("cadence.monthly", day=day) if day else _page("cadence.monthly_no_day")
    weekday = subscription.delivery_day_of_week
    if weekday is None:
        return _page(f"cadence.{frequency.value}_no_day")
    return _page(f"cadence.{frequency.value}", weekday=display_labels.weekday_name(weekday))


def schedule_sentence(subscription) -> str:
    """The schedule in one phrase ("Every Thursday, 10:00–13:00", "Every Thursday, any time"): the frequency, the day
    and the time (`time_label`)."""
    return _page("schedule_with_window", cadence=_cadence(subscription), window=time_label(subscription, inline=True))


def summary_sentence(subscription, start: Optional[date]) -> str:
    """The body header's one line: "Every Thursday, 10:00–13:00. Since 4 September."."""
    schedule = schedule_sentence(subscription)
    if start is None:
        return schedule
    return _page("summary", schedule=schedule, since=_page("since", date=display_labels.day_month(start)))


def kept_order_note(order) -> Optional[str]:
    """A schedule, items or payment-method change leaves a placed order alone: "Your order for 30 October
    stays as it is."."""
    if order is None or order.delivery_date is None:
        return None
    return _page("kept_order_note", date=display_labels.day_month(order.delivery_date))


def preview_sentence(first_new_label: str, paused: bool, kept_note: Optional[str], per_month, amount_label: str) -> str:
    """The preview's sentence: the first delivery with the change, the order it keeps, and the month."""
    from business_app.utils.helpers import get_current_language

    count = int(per_month.to_integral_value())
    parts = [_page("preview.first_delivery_paused" if paused else "preview.first_delivery", date=first_new_label)]
    if kept_note:
        parts.append(kept_note)
    parts.append(
        get_translation(
            plural_key(f"{PAGE}.preview.per_month", get_current_language(), count),
            count=count,
            amount=amount_label,
        )
    )
    return " ".join(parts)
